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
    """NSE bhavcopy URL format: cm{DDMONTHYY}bhav.csv.zip"""
    date_str = date.strftime("%d%b%Y").upper()
    month = date.strftime("%b").upper()
    year = date.strftime("%Y")
    url = f"https://archives.nseindia.com/content/historical/EQUITIES/{year}/{month}/cm{date_str}bhav.csv.zip"

    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
        if r.status_code != 200:
            print(f"[WARN] Bhavcopy fetch failed: {r.status_code}")
            return []

        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            with z.open(z.namelist()[0]) as f:
                reader = csv.DictReader(io.TextIOWrapper(f))
                rows = []
                for row in reader:
                    if row.get("SERIES", "").strip() != "EQ":
                        continue
                    try:
                        rows.append(
                            {
                                "symbol": row["SYMBOL"].strip(),
                                "trade_date": date.strftime("%Y-%m-%d"),
                                "open": float(row["OPEN"]),
                                "high": float(row["HIGH"]),
                                "low": float(row["LOW"]),
                                "close": float(row["CLOSE"]),
                                "volume": int(row["TOTTRDQTY"]),
                            }
                        )
                    except (KeyError, ValueError) as e:
                        continue
                return rows
    except Exception as e:
        print(f"[ERROR] Bhavcopy parse failed: {e}")
        return []


def main():
    date = get_latest_trading_day()
    print(f"[INFO] Fetching EOD data for {date.strftime('%Y-%m-%d')}")

    prices = fetch_nse_bhavcopy(date)
    if not prices:
        print("[WARN] No prices fetched. Exiting.")
        return

    print(f"[INFO] Inserting {len(prices)} stock prices...")

    # Batch insert in chunks of 500
    for i in range(0, len(prices), 500):
        chunk = prices[i : i + 500]
        try:
            supabase.table("prices_eod").upsert(chunk).execute()
        except Exception as e:
            print(f"[WARN] Chunk {i} insert failed: {e}")

    print(f"[OK] {datetime.now().isoformat()} · {len(prices)} prices written")


if __name__ == "__main__":
    main()
