import time
import json
import requests
import pandas as pd
from datetime import datetime
from dateutil import tz
from pathlib import Path

# =========================
# CONFIGURATION
# =========================

ENDPOINT = "https://luquetsatlo.nchmf.gov.vn/LayerMapBox/getDSCanhbaoSLLQ"
SOGIO_DU_BAO = 6

# Severity ranking for sorting/deduplication
SEVERITY_RANK = {
    "Rất cao": 3,
    "Cao": 2,
    "Trung bình": 1
}

# Timezone: GMT+7
TZ_LOCAL = tz.gettz("Asia/Ho_Chi_Minh")

# Paths
OUT_DIR = Path("data")
OUT_DIR.mkdir(exist_ok=True) # Creates 'data' folder if it doesn't exist
OUT_CSV = OUT_DIR / "landslide.csv"

# =========================
# HELPERS
# =========================

def severity_score(row):
    """Calculate severity score for sorting (Keep highest warning)."""
    s1 = SEVERITY_RANK.get(str(row.get("nguycosatlo", "")).strip(), 0)
    s2 = SEVERITY_RANK.get(str(row.get("nguycoluquet", "")).strip(), 0)
    return max(s1, s2)

def post_with_retries(url, data, max_retries=3, timeout=30):
    """Robust POST request with retries."""
    delay = 2
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(url, data=data, timeout=timeout)
            if r.status_code == 200:
                return r
        except Exception as e:
            if attempt == max_retries:
                print(f"⚠️ Request failed: {e}")
            time.sleep(delay)
            delay *= 2
    return None

# =========================
# MAIN
# =========================

def main():
    # 1. Get Current Time (Rounded to Hour)
    now = datetime.now(TZ_LOCAL).replace(minute=0, second=0, microsecond=0)
    date_str = now.strftime("%Y-%m-%d %H:%M:%S")
    
    print(f"⏳ Fetching landslide warnings for: {date_str}")

    # 2. Fetch Data
    records = []
    payload = {
        "sogiodubao": SOGIO_DU_BAO,
        "date": date_str
    }
    
    resp = post_with_retries(ENDPOINT, data=payload, max_retries=3, timeout=45)
    
    if resp:
        try:
            try:
                data = resp.json()
            except:
                data = json.loads(resp.text)

            if isinstance(data, list):
                for row in data:
                    prov = str(row.get("provinceName_2cap", "")).strip()
                    
                    # Clean Commune Name: Remove 'P. ' prefix if present
                    commune = str(row.get("commune_name_2cap", "")).strip()
                    if commune.startswith("P. "):
                        commune = commune[3:].strip()
                    
                    records.append({
                        "time": date_str,
                        "commune_id_2cap": row.get("commune_id_2cap"),
                        "commune_name_2cap": commune,
                        "provinceName_2cap": prov,
                        "nguycosatlo": row.get("nguycosatlo"),
                        "nguycoluquet": row.get("nguycoluquet")
                    })
        except Exception as e:
            print(f"⚠️ Error parsing response: {e}")

    # 3. Process Data
    # Ensure DataFrame is created even if no records found (creates empty file with headers)
    if not records:
        print("✅ No active warnings found (or API returned empty).")
        df_new = pd.DataFrame(columns=["time", "commune_id_2cap", "commune_name_2cap", "provinceName_2cap", "nguycosatlo", "nguycoluquet"])
    else:
        print(f"📥 Found {len(records)} active records.")
        df_new = pd.DataFrame(records)
        
        # Deduplicate: Keep highest severity if duplicates exist
        df_new["_sev"] = df_new.apply(severity_score, axis=1)
        df_new = (
            df_new.sort_values(["provinceName_2cap", "commune_name_2cap"])
              .groupby(["commune_id_2cap"], as_index=False)
              .apply(lambda g: g.loc[g["_sev"].idxmax()])
              .reset_index(drop=True)
        ).drop(columns=["_sev"], errors="ignore")

    # 4. Save (Overwrite Mode)
    # .to_csv() automatically creates the file if it doesn't exist
    df_new.sort_values(["provinceName_2cap", "commune_name_2cap"]).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"✅ Data refreshed in {OUT_CSV}")

if __name__ == "__main__":
    main()
