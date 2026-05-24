"""
macro_cron.py
=============
DAILY cron - refreshes ~10 macro indicators via Yahoo Chart API DIRECT.
Runs at 5 PM IST after Indian + US markets settle.

Indicators:
- Currency: USDINR, EURINR, GBPINR
- Commodities: Brent crude, WTI crude, Gold, Silver
- India indices: Nifty 50, Bank Nifty, India VIX
- US markets: Dow Jones, NASDAQ, S&P 500
- Bonds: India 10Y G-Sec, US 10Y

Source: Yahoo Chart API direct (yfinance was getting silently blocked since ~March 2026).
Output: appends to macro_indicators (upsert on ticker+date).

Note: RBI Repo Rate is updated manually (changes only at MPC meetings).
"""

import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
IST = timezone(timedelta(hours=5, minutes=30))

# (ticker stored in DB, category, yahoo_symbol)
INDICATORS = [
    ("USDINR",         "currency",    "INR=X"),
    ("EURINR",         "currency",    "EURINR=X"),
    ("GBPINR",         "currency",    "GBPINR=X"),
    ("CRUDE_BRENT",    "commodities", "BZ=F"),
    ("CRUDE_WTI",      "commodities", "CL=F"),
    ("GOLD",           "commodities", "GC=F"),
    ("SILVER",         "commodities", "SI=F"),
    ("NIFTY50_SPOT",   "indices",     "^NSEI"),
    ("BANKNIFTY_SPOT", "indices",     "^NSEBANK"),
    ("INDIAVIX",       "indices",     "^INDIAVIX"),
    ("DOW",            "us_markets",  "^DJI"),
    ("NASDAQ",         "us_markets",  "^IXIC"),
    ("SP500",          "us_markets",  "^GSPC"),
    ("US10Y_YIELD",    "bonds",       "^TNX"),
]


def fetch_one(yahoo_sym: str, days_back: int = 10) -> list[dict] | None:
    url = f"{YAHOO_BASE}/{yahoo_sym}"
    params = {"interval": "1d", "range": f"{days_back}d"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[macro] {yahoo_sym}: HTTP {r.status_code}", file=sys.stderr)
            return None
        payload = r.json()
        result = (payload.get("chart", {}).get("result") or [None])[0]
        if not result:
            return None
        ts = result.get("timestamp") or []
        q = (result.get("indicators", {}).get("quote") or [{}])[0]
        opens, highs, lows, closes, vols = (
            q.get("open") or [],
            q.get("high") or [],
            q.get("low") or [],
            q.get("close") or [],
            q.get("volume") or [],
        )
        out = []
        for i, t in enumerate(ts):
            if i >= len(closes) or closes[i] is None:
                continue
            d = datetime.fromtimestamp(t, tz=IST).strftime("%Y-%m-%d")
            out.append({
                "date": d,
                "value": round(float(closes[i]), 4),
                "open": round(float(opens[i]), 4) if (i < len(opens) and opens[i] is not None) else None,
                "high": round(float(highs[i]), 4) if (i < len(highs) and highs[i] is not None) else None,
                "low":  round(float(lows[i]), 4)  if (i < len(lows)  and lows[i] is not None)  else None,
                "volume": int(vols[i]) if (i < len(vols) and vols[i] not in (None, 0)) else None,
            })
        return out or None
    except Exception as e:
        print(f"[macro] {yahoo_sym}: {e}", file=sys.stderr)
        return None


def main():
    print(f"[macro] refreshing {len(INDICATORS)} indicators")
    ok = 0
    failed = []
    all_rows = []

    for ticker, category, yahoo in INDICATORS:
        rows = fetch_one(yahoo, days_back=10)
        if not rows:
            failed.append(ticker)
            continue
        for r in rows:
            all_rows.append({**r, "ticker": ticker, "category": category})
        ok += 1
        time.sleep(0.8)

    if all_rows:
        try:
            sb.table("macro_indicators").upsert(all_rows, on_conflict="ticker,date").execute()
            print(f"[macro] {datetime.now(IST).isoformat()} · upserted {len(all_rows)} rows ({ok}/{len(INDICATORS)} tickers)")
        except Exception as e:
            print(f"[macro] upsert failed: {e}", file=sys.stderr)
    else:
        print("[macro] no rows to upsert")

    if failed:
        print(f"[macro] failed: {failed}")


if __name__ == "__main__":
    main()
