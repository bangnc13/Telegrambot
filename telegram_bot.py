import glob
import json
import logging
import math
import os
import re
import networkx as nx
import pandas as pd
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Cấu hình logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# TOKEN BOT TELEGRAM
BOT_TOKEN = "8844021111:AAHMak5ApfZ1P94Hb-ClMg2jL7qLRKN0-HQ"


# ---------------------------------------------------------
# 1. CÁC HÀM XỬ LÝ DỮ LIỆU & TOÁN HỌC
# ---------------------------------------------------------
def normalize_node(node_str):
    if pd.isna(node_str):
        return ""
    s = str(node_str).strip()
    s = re.sub(r"/\d+$", "", s)
    match = re.match(r"([A-Za-z0-9]+)\.(\d+)/([A-Za-z0-9]+)", s)
    if match:
        prefix, num, suffix = match.groups()
        return f"{prefix}.{int(num):04d}/{suffix}"
    return s


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def calculate_polyline_length(coords):
    if not coords or len(coords) < 2:
        return 0.0
    return sum(
        haversine(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
        for i in range(len(coords) - 1)
    )


def get_osrm_route(lat1, lon1, lat2, lon2):
    try:
        url = f"http://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=geojson"
        res = requests.get(url, timeout=3)
        if res.status_code == 200:
            data = res.json()
            if data.get("routes"):
                coords = data["routes"][0]["geometry"]["coordinates"]
                return [[p[1], p[0]] for p in coords]
    except Exception:
        pass
    return [[lat1, lon1], [lat2, lon2]]


def process_segment_geometry(raw_coords, u_coord, v_coord, target_length):
    if not raw_coords:
        if u_coord and v_coord:
            raw_coords = get_osrm_route(
                u_coord[0], u_coord[1], v_coord[0], v_coord[1]
            )
        else:
            return []

    if u_coord:
        d_start = haversine(
            u_coord[0], u_coord[1], raw_coords[0][0], raw_coords[0][1]
        )
        d_end = haversine(
            u_coord[0], u_coord[1], raw_coords[-1][0], raw_coords[-1][1]
        )
        if d_end < d_start:
            raw_coords = list(reversed(raw_coords))

    first_p, last_p = raw_coords[0], raw_coords[-1]
    if (
        haversine(first_p[0], first_p[1], last_p[0], last_p[1]) < 20.0
        and len(raw_coords) > 4
    ):
        mid_idx = len(raw_coords) // 2
        path_top = raw_coords[: mid_idx + 1]
        path_bottom = raw_coords[mid_idx:] + [raw_coords[0]]

        len_top = calculate_polyline_length(path_top)
        len_bottom = calculate_polyline_length(path_bottom)

        raw_coords = (
            path_top
            if abs(len_top - target_length) < abs(len_bottom - target_length)
            else path_bottom
        )

    return raw_coords


def interpolate_on_polyline_scaled(coords, target_offset, decl_length):
    if not coords or len(coords) < 2:
        return None, None

    geo_length = calculate_polyline_length(coords)
    scale_factor = (
        geo_length / decl_length if (decl_length > 0 and geo_length > 0) else 1.0
    )
    adjusted_target = target_offset * scale_factor

    accumulated = 0.0
    for i in range(len(coords) - 1):
        p1, p2 = coords[i], coords[i + 1]
        seg_len = haversine(p1[0], p1[1], p2[0], p2[1])
        if accumulated + seg_len >= adjusted_target:
            remain = adjusted_target - accumulated
            ratio = remain / seg_len if seg_len > 0 else 0
            return p1[0] + ratio * (p2[0] - p1[0]), p1[1] + ratio * (
                p2[1] - p1[1]
            )
        accumulated += seg_len
    return coords[-1][0], coords[-1][1]


# Load dữ liệu hệ thống
def load_data():
    file_path = "Data.xlsx"
    df_uplink = pd.read_excel(file_path, sheet_name="uplink")
    df_cable = pd.read_excel(file_path, sheet_name="Đoạn cáp")
    df_hdn = pd.read_excel(file_path, sheet_name="HĐN")

    df_hdn["Lat_clean"] = pd.to_numeric(
        df_hdn["Lat"].astype(str).str.replace(",", "."), errors="coerce"
    )
    df_hdn["Lng_clean"] = pd.to_numeric(
        df_hdn["Lng"].astype(str).str.replace(",", "."), errors="coerce"
    )

    hdn_coords = {}
    for _, row in df_hdn.iterrows():
        name = normalize_node(row["Tên đối tượng"])
        lat, lng = row["Lat_clean"], row["Lng_clean"]
        if pd.notnull(lat) and pd.notnull(lng):
            hdn_coords[name] = (float(lat), float(lng))

    node_level_map = {}
    has_col_d = df_uplink.shape[1] >= 4
    for idx, row in df_uplink.iterrows():
        node_name = normalize_node(
            row["Tên đối tượng"] if "Tên đối tượng" in row else row["TĐ"]
        )
        if not node_name:
            continue

        level = None
        if has_col_d and pd.notnull(row.iloc[3]):
            val_d = str(row.iloc[3]).strip().lower()
            if "1" in val_d:
                level = 1
            elif "2" in val_d:
                level = 2

        if level is None:
            level = (
                2
                if (node_name.endswith("/HO") or node_name.endswith("/MO"))
                else 1
            )

        node_level_map[node_name] = level

    G = nx.Graph()
    for _, row in df_cable.iterrows():
        u = normalize_node(row["Điểm KN1"])
        v = normalize_node(row["Điểm KN2"])
        cable_name = str(row["Tên đoạn cáp"]).strip()
        try:
            length = float(row["Chiều dài thực (m)"])
        except Exception:
            length = 0.0

        cap_val = (
            row.get("Dung lượng")
            if "Dung lượng" in df_cable.columns
            else row.iloc[5]
        )
        if pd.isna(cap_val) or str(cap_val).strip() in ["", "nan", "None"]:
            capacity_str = "Chưa xác định"
        else:
            try:
                capacity_str = f"{int(float(cap_val))} FO"
            except Exception:
                capacity_str = (
                    f"{str(cap_val).strip()} FO"
                    if "FO" not in str(cap_val).upper()
                    else str(cap_val).strip()
                )

        if u and v:
            G.add_edge(
                u, v, cable=cable_name, length=length, capacity=capacity_str
            )

    cable_shapes = {}

    # Hàm đọc file JSON và bóc tách tọa độ tuyến cáp
    def parse_json_file(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (
                isinstance(data, dict)
                and data.get("type") == "FeatureCollection"
            ):
                for feature in data.get("features", []):
                    props = feature.get("properties", {})
                    cable_name = (
                        props.get("name")
                        or props.get("TEN_DOAN_CAP")
                        or props.get("code")
                        or props.get("id")
                    )
                    geom = feature.get("geometry", {})
                    if geom.get("type") == "LineString":
                        coords = [
                            [p[1], p[0]] for p in geom.get("coordinates", [])
                        ]
                        if cable_name:
                            cable_shapes[str(cable_name).strip()] = coords
                    elif geom.get("type") == "MultiLineString":
                        coords = []
                        for line in geom.get("coordinates", []):
                            coords.extend([[p[1], p[0]] for p in line])
                        if cable_name:
                            cable_shapes[str(cable_name).strip()] = coords
        except Exception as e:
            logging.error(f"Lỗi đọc file {file_path}: {e}")

    # 1. Ưu tiên đọc file RING.json trước
    if os.path.exists("RING.json"):
        parse_json_file("RING.json")

    # 2. Đọc các file .json còn lại để bổ sung thêm dữ liệu
    for json_file_path in glob.glob("*.json"):
        if json_file_path != "RING.json":
            parse_json_file(json_file_path)

    return G, hdn_coords, node_level_map, cable_shapes


# Tải dữ liệu khởi tạo
G, hdn_coords, node_level_map, json_cable_shapes = load_data()


# ---------------------------------------------------------
# 2. XỬ LÝ LỆNH TELEGRAM BOT
# ---------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 **Hướng dẫn sử dụng Bot Tra Cứu Sự Cố Cáp:**\n\n"
        "Gõ lệnh theo cú pháp sau:\n"
        "`/sucu TĐ_Đo TĐ_Đến Khoảng_Cách`\n\n"
        "**Ví dụ:**\n"
        "`/sucu TQGP001.0155/HO TQGP001.0076/HO 1500`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def sucu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "❌ **Cú pháp chưa đúng!**\n\n"
            "Vui lòng gõ theo mẫu: `/sucu TĐ_Đo TĐ_Đến Khoảng_Cách`\n"
            "Ví dụ: `/sucu TQGP001.0155/HO TQGP001.0076/HO 1500`",
            parse_mode="Markdown",
        )
        return

    td_a = normalize_node(context.args[0])
    td_b = normalize_node(context.args[1])

    try:
        target_dist = float(context.args[2].replace(",", "."))
    except ValueError:
        await update.message.reply_text(
            "❌ Khoảng cách phải là một số thực hợp lệ (ví dụ: `1500` hoặc `1500.5`)."
        )
        return

    if not G.has_node(td_a):
        await update.message.reply_text(
            f"❌ Không tìm thấy TĐ Đo `{td_a}` trong dữ liệu!",
            parse_mode="Markdown",
        )
        return
    if not G.has_node(td_b):
        await update.message.reply_text(
            f"❌ Không tìm thấy TĐ Đến `{td_b}` trong dữ liệu!",
            parse_mode="Markdown",
        )
        return

    if not nx.has_path(G, td_a, td_b):
        await update.message.reply_text(
            f"❌ Không tìm thấy đường nối tuyến cáp giữa `{td_a}` và `{td_b}`!",
            parse_mode="Markdown",
        )
        return

    node_path = nx.shortest_path(G, td_a, td_b, weight="length")
    accumulated_dist, target_segment = 0.0, None

    for i in range(len(node_path) - 1):
        u, v = node_path[i], node_path[i + 1]
        edge_data = G[u][v]
        seg_len = edge_data["length"]
        start_d = accumulated_dist
        accumulated_dist += seg_len

        seg_info = {
            "u": u,
            "v": v,
            "cable": edge_data["cable"],
            "length": seg_len,
            "capacity": edge_data.get("capacity", "Chưa xác định"),
            "start_dist": start_d,
            "end_dist": accumulated_dist,
        }
        if start_d <= target_dist <= accumulated_dist and target_segment is None:
            target_segment = seg_info

    if target_dist > accumulated_dist:
        await update.message.reply_text(
            f"❌ Khoảng cách nhập vào ({target_dist}m) vượt quá tổng chiều dài tuyến cáp ({accumulated_dist:.1f}m)!",
            parse_mode="Markdown",
        )
        return

    if target_segment:
        cable_name = target_segment["cable"]
        offset_from_u = target_dist - target_segment["start_dist"]
        offset_from_v = target_segment["length"] - offset_from_u

        u_coord = hdn_coords.get(target_segment["u"])
        v_coord = hdn_coords.get(target_segment["v"])

        raw_coords = json_cable_shapes.get(cable_name, [])
        processed_coords = process_segment_geometry(
            raw_coords, u_coord, v_coord, target_segment["length"]
        )
        fault_lat, fault_lng = interpolate_on_polyline_scaled(
            processed_coords, offset_from_u, target_segment["length"]
        )

        if fault_lat and fault_lng:
            gmaps_url = f"https://www.google.com/maps/dir/?api=1&destination={fault_lat},{fault_lng}"

            response_msg = (
                f"⚠️ **VỊ TRÍ ĐỨT TRÊN ĐOẠN CÁP**\n\n"
                f"📦 **Đoạn cáp:** `{cable_name}`\n"
                f"📍 **Lộ trình:** `{target_segment['u']}` ➔ `{target_segment['v']}`\n"
                f"📏 **Chiều dài đoạn:** {target_segment['length']:.1f} m\n"
                f"🔌 **Dung lượng:** {target_segment['capacity']}\n"
                f"📏 **Tổng chiều dài tuyến:** {accumulated_dist:.1f} m\n\n"
                f"🎯 **Chi tiết vị trí sự cố:**\n"
                f"• Cách **{target_segment['u']}** (Đầu): `{offset_from_u:.1f} m`\n"
                f"• Cách **{target_segment['v']}** (Cuối): `{offset_from_v:.1f} m`\n\n"
                f"🗺 [Mở chỉ đường Google Maps]({gmaps_url})"
            )

            await update.message.reply_text(
                response_msg,
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
            await update.message.reply_location(
                latitude=fault_lat, longitude=fault_lng
            )
        else:
            await update.message.reply_text(
                "❌ Không thể xác định tọa độ địa lý cho vị trí điểm đứt!"
            )


# ---------------------------------------------------------
# 3. KHỞI CHẠY BOT
# ---------------------------------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("sucu", sucu))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
