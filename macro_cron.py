"""
macro_cron.py
=============
DAILY cron — refreshes 10 macro indicators via yfinance.
Runs at 5 PM IST after Indian + US markets settle.

Indicators:
- Currency: USDINR, EURINR, GBPINR
- Commodities: Brent crude, WTI crude, Gold
- India indices: Nifty 50, Bank Nifty
- US markets: Dow Jones, NASDAQ

Source: yfinance (Yahoo Finance, free)
Output: appends to macro_indicators table

Note: RBI Repo Rate is updated manually (changes only at MPC meetings).
"""

import os
import sys
import time
from datetime import datetime, timedelta

import yfinance as yf
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)


# (ticker stored in DB, category, yahoo_symbol)
INDICATORS = [
    ("USDINR",         "currency",    "USDINR=X"),
    ("EURINR",         "currency",    "EURINR=X"),
    ("GBPINR",         "currency",    "GBPINR=X"),
    ("CRUDE_BRENT",    "commodities", "BZ=F"),
    ("CRUDE_WTI",      "commodities", "CL=F"),
    ("GOLD",           "commodities", "GC=F"),
    ("NIFTY50_SPOT",   "indices",     "^NSEI"),
    ("BANKNIFTY_SPOT", "indices",     "^NSEBANK"),
    ("DOW",            "us_markets",  "^DJI"),
    ("NASDAQ",         "us_markets",  "^IXIC"),
]


def fetch_one(yahoo_sym: str, days_back: int = 10) -> list[dict] | None:
    """Pull last N days from yfinance. Returns list of rows."""
    try:
        ticker = yf.Ticker(yahoo_sym)
        hist = ticker.history(period=f"{days_back}d", interval="1d")
        if hist.empty:
            return None
        if hist.index.tz is not None:
            hist.index = hist.index.tz_localize(None)
        out = []
        for ts, row in hist.iterrows():
            out.append({
                "date": ts.strftime("%Y-%m-%d"),
                "value": round(float(row["Close"]), 4),
                "open": round(float(row["Open"]), 4) if row["Open"] else None,
                "high": round(float(row["High"]), 4) if row["High"] else None,
                "low": round(float(row["Low"]), 4) if row["Low"] else None,
                "volume": int(row["Volume"]) if row["Volume"] > 0 else None,
            })
        return out
    except Exception as e:
        print(f"[macro] {yahoo_sym}: {e}", file=sys.stderr)
        return None


def main():
    print(f"[macro] refreshing {len(INDICATORS)} indicators")
    ok = 0
    failed = []
    all_rows = []

    for ticker, category, yahoo in INDICATORS:
        rows = fetch_one(yahoo, days_back=10)  # last 10 days; upsert handles duplicates
        if not rows:
            failed.append(ticker)
            continue
        for r in rows:
            all_rows.append({**r, "ticker": ticker, "category": category})
        ok += 1
        time.sleep(1.0)

    if all_rows:
        try:
            sb.table("macro_indicators").upsert(all_rows, on_conflict="ticker,date").execute()
            print(f"[macro] {datetime.now().isoformat()} · upserted {len(all_rows)} rows ({ok}/{len(INDICATORS)} tickers)")
        except Exception as e:
            print(f"[macro] upsert failed: {e}", file=sys.stderr)
    else:
        print("[macro] no rows to upsert")

    if failed:
        print(f"[macro] failed: {failed}")


if __name__ == "__main__":
    main()
