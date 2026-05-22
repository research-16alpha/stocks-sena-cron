"""
Macro Indicators Scraper · Stocks Sena
=========================================
Pulls USD/INR, Gold, Brent crude, NIFTY, BANKNIFTY, India VIX
from Yahoo Finance + sets RBI/MoSPI indicators.
"""

import os
from datetime import datetime
import yfinance as yf
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Yahoo Finance tickers → indicator names
YAHOO_INDICATORS = {
    "INR=X": ("USD/INR", "rupee"),
    "GC=F": ("Gold", "USD/oz"),
    "BZ=F": ("Brent crude", "USD/bbl"),
    "^NSEI": ("NIFTY 50", "points"),
    "^NSEBANK": ("NIFTY BANK", "points"),
    "^INDIAVIX": ("India VIX", "vol"),
    "^GSPC": ("S&P 500", "points"),
    "^DJI": ("Dow Jones", "points"),
    "^N225": ("Nikkei 225", "points"),
}

# Manual current data points (update monthly from RBI/MoSPI)
MANUAL_INDICATORS = [
    ("Repo rate", 6.50, "%", "RBI"),
    ("CPI inflation", 5.08, "%", "MoSPI"),
    ("WPI inflation", 2.04, "%", "MoSPI"),
    ("GDP growth (Q4)", 7.4, "%", "MoSPI"),
    ("IIP growth", 5.2, "%", "MoSPI"),
    ("Forex reserves", 640, "USD bn", "RBI"),
    ("10-yr G-sec yield", 7.04, "%", "RBI"),
    ("Trade balance", -22.5, "USD bn", "Commerce"),
]


def fetch_yahoo_indicators():
    today = datetime.now().strftime("%Y-%m-%d")
    records = []
    for ticker, (name, unit) in YAHOO_INDICATORS.items():
        try:
            data = yf.Ticker(ticker).info
            price = data.get("regularMarketPrice")
            if price:
                records.append({
                    "indicator": name,
                    "value": float(price),
                    "unit": unit,
                    "reading_date": today,
                    "source": "Yahoo Finance",
                })
                print(f"[INFO] {name}: {price}")
        except Exception as e:
            print(f"[WARN] {ticker} fetch failed: {e}")
    return records


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    all_records = []

    # Manual indicators
    for name, value, unit, source in MANUAL_INDICATORS:
        all_records.append({
            "indicator": name,
            "value": value,
            "unit": unit,
            "reading_date": today,
            "source": source,
        })

    # Yahoo indicators
    yahoo_records = fetch_yahoo_indicators()
    all_records.extend(yahoo_records)

    # Upsert with (indicator, reading_date) unique constraint
    inserted = 0
    for record in all_records:
        try:
            supabase.table("macro_indicators").upsert(
                record, on_conflict="indicator,reading_date"
            ).execute()
            inserted += 1
        except Exception as e:
            print(f"[WARN] Insert failed for {record['indicator']}: {e}")

    print(f"[OK] {datetime.now().isoformat()} · {inserted} macro indicators inserted")


if __name__ == "__main__":
    main()
