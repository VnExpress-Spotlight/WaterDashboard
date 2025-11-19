import re
import json  # Added missing import
import requests
import pandas as pd
from datetime import datetime, date, timedelta, timezone
from dateutil import tz
from tqdm import tqdm
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pathlib import Path

# =========================
# PATH & TIMEZONE CONFIG
# =========================

TZ_LOCAL = tz.gettz("Asia/Ho_Chi_Minh")  # GMT+7

OUT_DIR = Path("data")
OUT_DIR.mkdir(exist_ok=True)
OUT_CSV = OUT_DIR / "water_data_full_combined.csv"
CONFIG_FILE = "config_stations.json"

# ---- Determine fetch window based on existing CSV ----
DEFAULT_END_DATE = datetime.now(TZ_LOCAL).date()

if OUT_CSV.exists():
    try:
        df_old_meta = pd.read_csv(OUT_CSV, encoding="utf-8-sig", usecols=["Thời gian (UTC)"])
        last_dt = pd.to_datetime(df_old_meta["Thời gian (UTC)"]).max()
        if pd.isna(last_dt):
            raise ValueError("No valid timestamp in existing CSV")
        last_date = last_dt.date()
        # Avoid clock drift issues
        if last_date > DEFAULT_END_DATE:
            DEFAULT_START_DATE = DEFAULT_END_DATE - timedelta(days=6)
        else:
            DEFAULT_START_DATE = last_date
        print(f"🔁 Existing CSV found. Fetching data from {DEFAULT_START_DATE} to {DEFAULT_END_DATE} (GMT+7).")
    except Exception as e:
        print(f"⚠️ Could not infer start date from existing CSV, fallback to 7 days. Reason: {e}")
        DEFAULT_START_DATE = DEFAULT_END_DATE - timedelta(days=6)
else:
    # First run: 7 days history
    DEFAULT_START_DATE = DEFAULT_END_DATE - timedelta(days=6)
    print(f"🆕 No existing CSV. Fetching 7 days from {DEFAULT_START_DATE} to {DEFAULT_END_DATE} (GMT+7).")

TIMEOUT_SECONDS = 60

RIVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Referer": "https://vndms.dmptc.gov.vn/",
    "Origin": "https://vndms.dmptc.gov.vn",
}

def load_config():
    config_path = Path(CONFIG_FILE)
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found at {config_path}")
    
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

try:
    _config_data = load_config()
    LAKE_CONFIG = _config_data.get("lakes", {})
    STATION_CONFIG = _config_data.get("stations", {})
    
    LAKE_IDS = set(LAKE_CONFIG.keys())
    STATION_IDS = set(STATION_CONFIG.keys())
    
    print(f"✅ Loaded configuration: {len(LAKE_CONFIG)} lakes, {len(STATION_CONFIG)} stations.")
except Exception as e:
    print(f"❌ Error loading config: {e}")
    exit(1)

# =========================
# HELPERS
# =========================

def get_robust_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def get_first_num(d, key):
    v = d.get(key)
    if not v:
        return None
    parts = str(v).split(",")
    try:
        val = float(parts[0])
        return val if val > 0 else None
    except:
        return None

def classify_exceed(level, bd1, bd2, bd3, hist):
    if level is None:
        return 0
    if hist and level >= hist:
        return 4
    if bd3 and level >= bd3:
        return 3
    if bd2 and level >= bd2:
        return 2
    if bd1 and level >= bd1:
        return 1
    return 0

def alert_name_from_value(v):
    return {
        0: "Dưới BĐ1",
        1: "Trên BĐ1",
        2: "Trên BĐ2",
        3: "Trên BĐ3",
        4: "Trên lũ lịch sử",
    }.get(v, "Không xác định")

def calculate_alert_diff(level, val, bd1, bd2, bd3, hist):
    if val is None:
        return None
    try:
        if level == 4:
            diff = val - hist
        elif level == 3:
            diff = val - bd3
        elif level == 2:
            diff = val - bd2
        elif level == 1:
            diff = val - bd1
        elif bd1:
            diff = val - bd1
        else:
            return None
        return round(diff, 2)
    except:
        return None

def parse_river_dt(lbl, current_year):
    """
    Parses VNDMS time labels. 
    Fixes the issue where label is '0h \n15/11' (Hour / Day / Month).
    """
    if not lbl:
        return None

    clean = lbl.strip()

    # --- Case A: New Format (e.g., "0h \n15/11" -> Hour, Day, Month) ---
    # \s+ handles the newline char found in your JSON
    m_new = re.search(r"(\d{1,2})h\s+(\d{1,2})/(\d{1,2})", clean)
    if m_new:
        try:
            hour = int(m_new.group(1))
            minute = 0  # Format implies top of hour
            day = int(m_new.group(2))
            month = int(m_new.group(3))
            
            # Handle Year Boundary (e.g. Parsing Dec data in Jan)
            now = datetime.now(TZ_LOCAL)
            year_to_use = current_year
            if month == 12 and now.month == 1:
                year_to_use -= 1
            
            dt = datetime(year_to_use, month, day, hour, minute)
            if TZ_LOCAL:
                return dt.replace(tzinfo=TZ_LOCAL)
            return dt
        except ValueError:
            pass 

    # --- Case B: Old Format Fallback (e.g., "7h30/12" -> Hour, Minute, Day) ---
    m_old = re.search(r"(\d{1,2})h(\d{1,2})/(\d{1,2})", clean)
    if m_old:
        try:
            hour = int(m_old.group(1))
            minute = int(m_old.group(2))
            day = int(m_old.group(3))
            
            now_local = datetime.now(TZ_LOCAL)
            month = now_local.month
            
            dt = datetime(current_year, month, day, hour, minute)
            if TZ_LOCAL:
                return dt.replace(tzinfo=TZ_LOCAL)
            return dt
        except ValueError:
            pass

    return None

def ms_to_dt_local(ms_str):
    try:
        ms = int(re.search(r"\d+", str(ms_str)).group())
        ts = ms / 1000.0
        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        if TZ_LOCAL:
            return dt_utc.astimezone(TZ_LOCAL)
        return dt_utc
    except:
        return None

# =========================
# SCRAPERS
# =========================

def scrape_river_stations_list():
    url = "https://vndms.dmptc.gov.vn/water_level"
    session = get_robust_session()
    session.headers.update(RIVER_HEADERS)
    stations = {}

    for lv in ["0", "1", "2", "3"]:
        try:
            r = session.get(url, params={"lv": lv}, timeout=TIMEOUT_SECONDS)
            data = r.json()
            for f in data.get("features", []):
                props = f.get("properties", {})
                popup = props.get("popupInfo", "")

                m_id = re.search(r"Mã trạm:\s*<b>(\d+)</b>", popup, re.IGNORECASE)
                sid = m_id.group(1) if m_id else None
                if not sid:
                    continue
                
                # Allow user to add new stations dynamically if found in CONFIG
                if sid not in STATION_IDS:
                    continue

                river = "Unknown"
                m_riv = re.search(r"Sông:\s*<b>(.*?)</b>", popup, re.IGNORECASE)
                if m_riv:
                    river = m_riv.group(1).strip()

                x, y = None, None
                geom = f.get("geometry", {})
                if geom and geom.get("type") == "Point":
                    coords = geom.get("coordinates")
                    if coords and len(coords) == 2:
                        x, y = coords[0], coords[1]

                stations[sid] = {
                    "river": river,
                    "name": props.get("label", f"Station {sid}"),
                    "x": x,
                    "y": y,
                }
        except:
            pass

    return stations

def scan_lakes_via_api():
    url = "http://e15.thuyloivietnam.vn/CanhBaoSoLieu/ATCBDTHo"
    session = get_robust_session()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/5.0",
        "Referer": "http://e15.thuyloivietnam.vn/",
    }

    results = []
    curr = DEFAULT_START_DATE
    while curr <= DEFAULT_END_DATE:
        try:
            time_param = f"{curr.strftime('%Y-%m-%d')} 00:00:00,000"
            r = session.post(
                url,
                data={"time": time_param, "ishothuydien": "0"},
                headers=headers,
                timeout=TIMEOUT_SECONDS,
            )
            if r.status_code == 200:
                for rec in r.json():
                    lc = rec.get("LakeCode")
                    if lc in LAKE_IDS:
                        results.append(rec)
        except:
            pass
        curr += timedelta(days=1)

    return results

# =========================
# MAIN LOGIC
# =========================

def main():
    print("📡 Scanning selected river stations & lakes...")

    cached_river_stations = scrape_river_stations_list()
    lake_data = scan_lakes_via_api()

    final_rows = []

    # ----------- RIVERS -----------
    session = get_robust_session()
    session.headers.update(RIVER_HEADERS)
    curr_year = datetime.now(TZ_LOCAL).year

    print(f"🌊 Fetching {len(cached_river_stations)} river stations...")
    for sid, meta in tqdm(cached_river_stations.items(), desc="Rivers"):
        try:
            payload = {
                "id": sid,
                "timeSelect": "7",
                "source": "Water",
                "fromDate": "",
                "toDate": "",
            }
            r = session.post(
                "https://vndms.dmc.gov.vn/home/detailRain",
                data=payload,
                headers=RIVER_HEADERS,
                timeout=15,
            )
            if r.status_code != 200:
                continue
            d = r.json()

            bd1 = get_first_num(d, "bao_dong1")
            bd2 = get_first_num(d, "bao_dong2")
            bd3 = get_first_num(d, "bao_dong3")
            hist_m = get_first_num(d, "gia_tri_lu_lich_su")
            hist_y = d.get("nam_lu_lich_su")

            labels = (d.get("labels") or "").split(",")
            raw_vals = str(d.get("value", "")).split(",")
            values = [
                float(x.strip())
                if x.strip() not in ["-", "null", "", "NULL"]
                else None
                for x in raw_vals
            ]

            config = STATION_CONFIG.get(sid, {})
            recoded_basin = config.get(
                "basin_recode", d.get("river_name", meta["river"])
            )
            recoded_name = config.get("name", d.get("name_vn", meta["name"]))
            province = d.get("province_name", "")

            for i in range(min(len(labels), len(values))):
                # USE THE NEW PARSER HERE
                dt_local = parse_river_dt(labels[i], curr_year)
                
                if not dt_local:
                    continue
                if dt_local.date() < DEFAULT_START_DATE:
                    continue

                val = values[i]
                alert_val = classify_exceed(val, bd1, bd2, bd3, hist_m)
                alert_name = alert_name_from_value(alert_val)
                alert_diff = calculate_alert_diff(
                    alert_val, val, bd1, bd2, bd3, hist_m
                )

                final_rows.append(
                    {
                        "type": "River",
                        "id": sid,
                        "name": recoded_name,
                        "basin": recoded_basin,
                        "province": province,
                        "timestamp_utc": dt_local.strftime("%Y-%m-%d %H:%M"),
                        "water_level_m": val,
                        "alert_status": alert_name,
                        "alert_value": alert_val,
                        "alert_diff_m": alert_diff,
                        "bd1_m": bd1,
                        "bd2_m": bd2,
                        "bd3_m": bd3,
                        "hist_flood_level_m": hist_m,
                        "hist_flood_year": hist_y,
                        "x": meta.get("x"),
                        "y": meta.get("y"),
                    }
                )
        except Exception as e:
            print(f"  > Skipping station {sid}. Error: {e}")

    # ----------- LAKES -----------
    print("💧 Processing lake data...")
    for rec in lake_data:
        lc = rec.get("LakeCode")
        if lc not in LAKE_CONFIG:
            continue

        conf = LAKE_CONFIG[lc]
        recoded_basin = conf["basin_recode"]
        recoded_name = conf["name"]
        # Use recoded province if available, otherwise fall back to API province
        recoded_province = conf.get("province_recode", rec.get("ProvinceName"))

        dt_local = ms_to_dt_local(rec.get("ThoiGianCapNhat"))

        if dt_local and dt_local.date() < DEFAULT_START_DATE:
            continue

        final_rows.append(
            {
                "type": "Lake",
                "id": lc,
                "name": recoded_name,
                "basin": recoded_basin,
                "province": recoded_province,
                "timestamp_utc": dt_local.strftime("%Y-%m-%d %H:%M") if dt_local else None,
                "water_level_m": rec.get("TdMucNuoc"),
                "alert_status": rec.get("XuThe", "N/A"),
                "water_volume_m3": rec.get("TdDungTich"),
                "design_volume_m3": rec.get("TkDungTich"),
                "volume_percent": rec.get("TiLeDungTichTdSoTk"),
                "inflow_m3s": rec.get("QDen"),
                "outflow_m3s": rec.get("QXa"),
                "normal_level_m": rec.get("MucNuocDangBinhThuong"),
                "reinforced_level_m": rec.get("MucNuocDangGiaCuong"),
                "alert_code": rec.get("MucCanhBao"),
                "province_code": rec.get("ProvinceCode"),
                "basin_code": rec.get("BasinCode"),
                "x": rec.get("X"),
                "y": rec.get("Y"),
            }
        )

    # ----------- EXPORT + INCREMENTAL MERGE -----------
    if not final_rows:
        print("❌ No data collected for current window.")
        return

    df = pd.DataFrame(final_rows)
    df = df.sort_values(["type", "basin", "name", "timestamp_utc"])

    df = df.rename(
        columns={
            "id": "Mã trạm/LakeCode",
            "name": "Trạm/Hồ",
            "basin": "Tên sông/Lưu vực",
            "province": "Tên tỉnh",
            "timestamp_utc": "Thời gian (UTC)",
            "water_level_m": "Mực nước (m)",
            "alert_status": "Cảnh báo/Xu thế",
            "alert_value": "Cảnh báo value (0-4)",
            "alert_diff_m": "Chênh lệch cảnh báo (m)",
            "bd1_m": "BĐ1 (m)",
            "bd2_m": "BĐ2 (m)",
            "bd3_m": "BĐ3 (m)",
            "hist_flood_level_m": "Mực nước lịch sử (m)",
            "hist_flood_year": "Năm lũ lịch sử",
            "water_volume_m3": "Dung tích (m3)",
            "design_volume_m3": "Dung tích TK (m3)",
            "volume_percent": "Tỷ lệ dung tích (%)",
            "inflow_m3s": "Q đến (m3/s)",
            "outflow_m3s": "Q xả (m3/s)",
            "normal_level_m": "Mực nước BT (m)",
            "reinforced_level_m": "Mực nước GC (m)",
            "alert_code": "Mã Cảnh báo",
        }
    )

    if OUT_CSV.exists():
        try:
            df_old = pd.read_csv(OUT_CSV, encoding="utf-8-sig")
            combined = pd.concat([df_old, df], ignore_index=True)

            combined = combined.drop_duplicates(
                subset=["type", "Mã trạm/LakeCode", "Thời gian (UTC)"],
                keep="last",
            )

            combined = combined.sort_values(
                ["type", "Tên sông/Lưu vực", "Trạm/Hồ", "Thời gian (UTC)"]
            )

            combined.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
            print(f"✅ Appended & deduplicated. Now {len(combined)} rows in {OUT_CSV}")
        except Exception as e:
            print(f"⚠️ Error merging with existing CSV, overwriting file. Reason: {e}")
            df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
            print(f"✅ Saved {len(df)} rows to {OUT_CSV}")
    else:
        df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
        print(f"✅ Saved {len(df)} rows to {OUT_CSV}")

    print("⏱ Note: 'Thời gian (UTC)' currently stores timestamps in GMT+7 (Asia/Ho_Chi_Minh).")

if __name__ == "__main__":
    main()
