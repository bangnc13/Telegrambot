import os
import re
import json
import math
import glob
import requests
import networkx as nx
import pandas as pd
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ==========================================
# 1. TỐI ƯU & NẠP DỮ LIỆU TỪ EXCEL & JSON
# ==========================================
def normalize_node(node_str):
    if pd.isna(node_str): return ""
    s = str(node_str).strip()
    s = re.sub(r'/\d+$', '', s)
    match = re.match(r'([A-Za-z0-9]+)\.(\d+)/([A-Za-z0-9]+)', s)
    if match:
        prefix, num, suffix = match.groups()
        return f"{prefix}.{int(num):04d}/{suffix}"
    return s

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def calculate_polyline_length(coords):
    if not coords or len(coords) < 2: return 0.0
    return sum(haversine(coords[i][0], coords[i][1], coords[i+1][0], coords[i+1][1]) for i in range(len(coords) - 1))

def process_segment_geometry(raw_coords, u_coord, v_coord, target_length):
    if not raw_coords:
        if u_coord and v_coord:
            try:
                url = f"http://router.project-osrm.org/route/v1/driving/{u_coord[1]},{u_coord[0]};{v_coord[1]},{v_coord[0]}?overview=full&geometries=geojson"
                res = requests.get(url, timeout=2)
                if res.status_code == 200 and res.json().get("routes"):
                    return [[p[1], p[0]] for p in res.json()["routes"][0]["geometry"]["coordinates"]]
            except: pass
            return [[u_coord[0], u_coord[1]], [v_coord[0], v_coord[1]]]
        return []

    if u_coord:
        d_start = haversine(u_coord[0], u_coord[1], raw_coords[0][0], raw_coords[0][1])
        d_end = haversine(u_coord[0], u_coord[1], raw_coords[-1][0], raw_coords[-1][1])
        if d_end < d_start:
            raw_coords = list(reversed(raw_coords))

    first_p, last_p = raw_coords[0], raw_coords[-1]
    if haversine(first_p[0], first_p[1], last_p[0], last_p[1]) < 20.0 and len(raw_coords) > 4:
        mid_idx = len(raw_coords) // 2
        path_top = raw_coords[:mid_idx+1]
        path_bottom = raw_coords[mid_idx:] + [raw_coords[0]]
        return path_top if abs(calculate_polyline_length(path_top) - target_length) < abs(calculate_polyline_length(path_bottom) - target_length) else path_bottom

    return raw_coords

def interpolate_on_polyline_scaled(coords, target_offset, decl_length):
    if not coords or len(coords) < 2: return None
    geo_length = calculate_polyline_length(coords)
    scale_factor = geo_length / decl_length if (decl_length > 0 and geo_length > 0) else 1.0
    adjusted_target = target_offset * scale_factor

    accumulated = 0.0
    for i in range(len(coords) - 1):
        p1, p2 = coords[i], coords[i+1]
        seg_len = haversine(p1[0], p1[1], p2[0], p2[1])
        if accumulated + seg_len >= adjusted_target:
            remain = adjusted_target - accumulated
            ratio = remain / seg_len if seg_len > 0 else 0
            return p1[0] + ratio * (p2[0] - p1[0]), p1[1] + ratio * (p2[1] - p1[1])
        accumulated += seg_len
    return coords[-1][0], coords[-1][1]

print("⏳ Đang tải dữ liệu mạng cáp...")
file_path = "Data.xlsx"
df_uplink = pd.read_excel(file_path, sheet_name="uplink")
df_cable = pd.read_excel(file_path, sheet_name="Đoạn cáp")
df_hdn = pd.read_excel(file_path, sheet_name="HĐN")

# Load Tọa độ HĐN
df_hdn['Lat_clean'] = pd.to_numeric(df_hdn['Lat'].astype(str).str.replace(',', '.'), errors='coerce')
df_hdn['Lng_clean'] = pd.to_numeric(df_hdn['Lng'].astype(str).str.replace(',', '.'), errors='coerce')
hdn_coords = {}
for _, row in df_hdn.iterrows():
    name = normalize_node(row['Tên đối tượng'])
    lat, lng = row['Lat_clean'], row['Lng_clean']
    if pd.notnull(lat) and pd.notnull(lng):
        hdn_coords[name] = (float(lat), float(lng))

# Load Level Map (Sheet Uplink)
node_level_map = {}
has_col_d = df_uplink.shape[1] >= 4
for idx, row in df_uplink.iterrows():
    node_name = normalize_node(row['Tên đối tượng'] if 'Tên đối tượng' in row else row['TĐ'])
    if not node_name: continue
    level = None
    if has_col_d and pd.notnull(row.iloc[3]):
        val_d = str(row.iloc[3]).strip().lower()
        if "1" in val_d: level = 1
        elif "2" in val_d: level = 2
    if level is None:
        level = 2 if (node_name.endswith('/HO') or node_name.endswith('/MO')) else 1
    node_level_map[node_name] = level

def get_node_level(node_name):
    if node_name in node_level_map: return node_level_map[node_name]
    return 2 if (node_name.endswith('/HO') or node_name.endswith('/MO')) else 1

# Load Đồ thị NetworkX
G = nx.Graph()
for _, row in df_cable.iterrows():
    u = normalize_node(row['Điểm KN1'])
    v = normalize_node(row['Điểm KN2'])
    cable_name = str(row['Tên đoạn cáp']).strip()
    try: length = float(row['Chiều dài thực (m)'])
    except: length = 0.0
    cap_val = row.get('Dung lượng') if 'Dung lượng' in df_cable.columns else row.iloc[5]
    if pd.isna(cap_val) or str(cap_val).strip() in ["", "nan", "None"]: capacity_str = "Chưa xác định"
    else:
        try: capacity_str = f"{int(float(cap_val))} FO"
        except: capacity_str = f"{str(cap_val).strip()} FO" if "FO" not in str(cap_val).upper() else str(cap_val).strip()
    if u and v: G.add_edge(u, v, cable=cable_name, length=length, capacity=capacity_str)

# Load JSON Cáp
json_cable_shapes = {}
for json_file_path in glob.glob("*.json"):
    try:
        with open(json_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("type") == "FeatureCollection":
            for feature in data.get("features", []):
                props = feature.get("properties", {})
                cable_name = props.get("name") or props.get("TEN_DOAN_CAP") or props.get("code") or props.get("id")
                geom = feature.get("geometry", {})
                if geom.get("type") == "LineString":
                    coords = [[p[1], p[0]] for p in geom.get("coordinates", [])]
                    if cable_name: json_cable_shapes[str(cable_name).strip()] = coords
                elif geom.get("type") == "MultiLineString":
                    coords = []
                    for line in geom.get("coordinates", []): coords.extend([[p[1], p[0]] for p in line])
                    if cable_name: json_cable_shapes[str(cable_name).strip()] = coords
    except: pass

print("✅ Hệ thống đã sẵn sàng kết nối Telegram Bot!")

# ==========================================
# 2. XỬ LÝ LỆNH BOT TELEGRAM
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "📍 **BOT XÁC ĐỊNH VỊ TRÍ ĐỨT CÁP NETWORK**\n\n"
        "Cú pháp tra cứu sự cố:\n"
        "`/sucu [TĐ_Đo] [TĐ_Đến] [Khoảng_Cách_Mét]`\n\n"
        "Ví dụ:\n"
        "`/sucu TQGP001.0011/HO TQGP001.0012/HO 150`"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def sucu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "⚠️ **Cú pháp chưa chính xác!**\n\n"
            "Vui lòng nhập lại dạng:\n`/sucu [TĐ_Đo] [TĐ_Đến] [Khoảng_Cách]`\n"
            "Ví dụ: `/sucu TQGP001.0011/HO TQGP001.0012/HO 150`",
            parse_mode="Markdown"
        )
        return

    td_a = normalize_node(args[0])
    td_b = normalize_node(args[1])
    
    try:
        target_dist = float(args[2])
    except ValueError:
        await update.message.reply_text("❌ Khoảng cách mét phải là một con số hợp lệ!")
        return

    # Kiểm tra sự tồn tại trong đồ thị
    if not G.has_node(td_a):
        await update.message.reply_text(f"❌ Không tìm thấy tập điểm đo **{td_a}** trong hệ thống!", parse_mode="Markdown")
        return
    if not G.has_node(td_b):
        await update.message.reply_text(f"❌ Không tìm thấy tập điểm đến **{td_b}** trong hệ thống!", parse_mode="Markdown")
        return

    # Kiểm tra phân cấp & Loại trừ /TO, /FO
    if td_b.endswith('/TO') or td_b.endswith('/FO'):
        await update.message.reply_text(f"❌ TĐ Đến **{td_b}** thuộc loại đuôi `/TO` hoặc `/FO` (không hợp lệ)!", parse_mode="Markdown")
        return

    level_a = get_node_level(td_a)
    if level_a == 2:
        level_b = get_node_level(td_b)
        if level_b not in [1, 2]:
            await update.message.reply_text(f"❌ TĐ Đo là **Cấp 2**, TĐ Đến **{td_b}** phải là Cấp 1 hoặc Cấp 2 có liên quan!", parse_mode="Markdown")
            return

    if not nx.has_path(G, td_a, td_b):
        await update.message.reply_text(f"❌ Không tìm thấy tuyến cáp liên thông giữa **{td_a}** và **{td_b}**!", parse_mode="Markdown")
        return

    # Tìm đường ngắn nhất & tính toán khoảng cách
    node_path = nx.shortest_path(G, td_a, td_b, weight='length')
    accumulated_dist, target_segment = 0.0, None

    for i in range(len(node_path) - 1):
        u, v = node_path[i], node_path[i+1]
        edge_data = G[u][v]
        seg_len = edge_data['length']
        start_d = accumulated_dist
        accumulated_dist += seg_len

        if start_d <= target_dist <= accumulated_dist and target_segment is None:
            target_segment = {
                'u': u, 'v': v,
                'cable': edge_data['cable'],
                'length': seg_len,
                'capacity': edge_data.get('capacity', 'Chưa xác định'),
                'start_dist': start_d
            }

    if target_dist > accumulated_dist:
        await update.message.reply_text(
            f"❌ Khoảng cách nhập vào (**{target_dist}m**) vượt quá chiều dài toàn tuyến (**{accumulated_dist:.1f}m**)!",
            parse_mode="Markdown"
        )
        return

    if target_segment:
        cable_name = target_segment['cable']
        offset = target_dist - target_segment['start_dist']
        u_coord, v_coord = hdn_coords.get(target_segment['u']), hdn_coords.get(target_segment['v'])

        raw_coords = json_cable_shapes.get(cable_name, [])
        processed_coords = process_segment_geometry(raw_coords, u_coord, v_coord, target_segment['length'])
        fault_lat, fault_lng = interpolate_on_polyline_scaled(processed_coords, offset, target_segment['length'])

        gmaps_url = f"https://www.google.com/maps/dir/?api=1&destination={fault_lat},{fault_lng}"

        # Tạo Nút bấm mở Google Maps trực tiếp
        keyboard = [[InlineKeyboardButton("📍 Mở Chỉ Đường Google Maps", url=gmaps_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        reply_message = (
            f"⚠️ **KẾT QUẢ PHÂN TÍCH SỰ CỐ**\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📦 **Đoạn cáp sự cố:** `{cable_name}`\n"
            f"📍 **Lộ trình:** `{target_segment['u']}` ➔ `{target_segment['v']}`\n"
            f"📏 **Chiều dài đoạn:** `{target_segment['length']:.1f} m`\n"
            f"🔌 **Dung lượng:** `{target_segment['capacity']}`\n"
            f"🎯 **Khoảng cách điểm đứt:** `{target_dist} m` (Từ {td_a})\n"
            f"🌐 **Tọa độ GPS:** `{fault_lat:.6f}, {fault_lng:.6f}`"
        )

        await update.message.reply_text(reply_message, parse_mode="Markdown", reply_markup=reply_markup)

# ==========================================
# 3. KÍCH HOẠT BOT
# ==========================================
if __name__ == "__main__":
    # Thay CHUỖI TOKEN Telegram Bot của bạn vào đây
    TOKEN = "8844021111:AAEni2Du4X24eDCOr0jUghcflx3SlG1_Fdc"

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("sucu", sucu_command))

    print("🚀 Telegram Bot đang lắng nghe tin nhắn...")
    app.run_polling()
