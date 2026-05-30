"""
EOD Data Scraper · Stocks Sena
================================
Daily 6:30 PM IST job. Pulls:
- NSE Bhavcopy (EOD prices for all stocks)
- FII/DII daily flow
- Block deals
- Bulk deals

Writes to: prices_eod, fii_dii, block_deals, bulk_deals
"""

import os
import sys
import io
import zipfile
from datetime import datetime, timedelta
import requests
import csv
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml",
    "Referer": "https://www.nseindia.com/",
}


def get_latest_trading_day():
    """Returns last business day (skips weekend)."""
    d = datetime.now()
    while d.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        d -= timedelta(days=1)
    return d


def fetch_nse_bhavcopy(date):
    """NSE UDiFF Common BhavCopy (the legacy cm<DDMONYY>bhav.csv.zip path was
    retired — it 404s, which is why prices_eod was silently empty). New URL:
    nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip
    UDiFF columns differ: TckrSymb / SctySrs / OpnPric / HghPric / LwPric / ClsPric / TtlTradgVol."""
    ymd = date.strftime("%Y%m%d")
    url = f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
    r = requests.get(url, headers=HEADERS, timeout=60)
    if r.status_code != 200:
        print(f"[ERROR] BhavCopy fetch HTTP {r.status_code} for {ymd}")
        return None  # None = hard error (distinct from [] = parsed-but-empty)
    try:
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            with z.open(z.namelist()[0]) as f:
                reader = csv.DictReader(io.TextIOWrapper(f))
                rows = []
                for row in reader:
                    if (row.get("SctySrs") or "").strip() not in ("EQ", "BE"):
                        continue
                    try:
                        rows.append({
                            "symbol": row["TckrSymb"].strip(),
                            "trade_date": date.strftime("%Y-%m-%d"),
                            "open": float(row["OpnPric"]),
                            "high": float(row["HghPric"]),
                            "low": float(row["LwPric"]),
                            "close": float(row["ClsPric"]),
                            "volume": int(float(row["TtlTradgVol"])),
                        })
                    except (KeyError, ValueError):
                        continue
                return rows
    except Exception as e:
        print(f"[ERROR] BhavCopy parse failed: {e}")
        return None


def main():
    date = get_latest_trading_day()
    is_weekend = date.weekday() >= 5
    print(f"[INFO] Fetching EOD data for {date.strftime('%Y-%m-%d')}")

    prices = fetch_nse_bhavcopy(date)
    # C3: fail LOUD instead of exiting 0 on a dead URL / empty write. A 0-row
    # result on a trading day is a real outage that must page (monitor.yml).
    if prices is None or len(prices) == 0:
        msg = "[ERROR] BhavCopy returned no prices"
        if is_weekend:
            print(msg + " (weekend — non-fatal)")
            return
        print(msg + " on a trading day — failing loud", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Inserting {len(prices)} stock prices...")
    failed = 0
    for i in range(0, len(prices), 500):
        chunk = prices[i : i + 500]
        try:
            supabase.table("prices_eod").upsert(chunk).execute()
        except Exception as e:
            failed += 1
            print(f"[ERROR] Chunk {i} insert failed: {e}", file=sys.stderr)
    if failed:
        print(f"[ERROR] {failed} chunk(s) failed to write — failing loud", file=sys.stderr)
        sys.exit(1)
    print(f"[OK] {datetime.now().isoformat()} · {len(prices)} prices written")


if __name__ == "__main__":
    main()
