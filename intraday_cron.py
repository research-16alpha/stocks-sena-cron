"""
intraday_cron.py
================
Runs every 10 min during market hours (9:15 AM - 3:30 PM IST, weekdays).
Fetches today's 1-min OHLCV for top 100 stocks + 9 indices.

Source: yfinance (Yahoo's 1m endpoint, free)
Output: intraday/{SYMBOL}.json in Supabase Storage bucket 'intraday'

JSON shape (compact):
{
  "symbol": "RELIANCE",
  "date": "2026-05-23",
  "bars": [["09:15", price, vol], ["09:16", ...], ...]
}

Used for: live sparklines on Home, watchlist, sector grid.
"""

import os
import sys
import json
import time
from datetime import datetime

import yfinance as yf
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

BUCKET = "intraday"
SLEEP_BETWEEN = 1.0

# Top 100 + 9 indices
INDICES = [
    "^NSEI", "^BSESN", "^NSEBANK", "^CNXIT", "^CNXPHARMA",
    "^CNXFMCG", "^CNXAUTO", "^CNXMETAL", "INR=X",
]


def fetch_top_symbols(limit: int = 100) -> list[str]:
    res = (
        sb.table("stock_master")
        .select("symbol")
        .order("market_cap_cr", desc=True)
        .limit(limit)
        .execute()
    )
    return [r["symbol"] + ".NS" for r in (res.data or [])]


def fetch_intraday(yahoo_sym: str) -> dict | None:
    """Pull today's 1-min from yfinance. Returns compact bundle."""
    try:
        ticker = yf.Ticker(yahoo_sym)
        # period='1d' gets today's intraday only
        hist = ticker.history(period="1d", interval="1m")
        if hist.empty:
            return None

        if hist.index.tz is not None:
            hist.index = hist.index.tz_convert("Asia/Kolkata").tz_localize(None)

        bars = []
        for ts, row in hist.iterrows():
            bars.append([
                ts.strftime("%H:%M"),
                round(float(row["Close"]), 2),
                int(row["Volume"]) if row["Volume"] > 0 else 0,
            ])

        today = datetime.now().strftime("%Y-%m-%d")
        sym_clean = yahoo_sym.replace(".NS", "").replace("^", "_").replace("=", "_")
        return {
            "symbol": sym_clean,
            "date": today,
            "bars": bars,
        }
    except Exception as e:
        print(f"[WARN] {yahoo_sym}: {e}", file=sys.stderr)
        return None


def upload(sym_clean: str, bundle: dict) -> bool:
    payload = json.dumps(bundle, separators=(",", ":")).encode("utf-8")
    path = f"{sym_clean}.json"
    try:
        try:
            sb.storage.from_(BUCKET).update(path, payload, {"contentType": "application/json"})
        except Exception:
            sb.storage.from_(BUCKET).upload(path, payload, {"contentType": "application/json"})
        return True
    except Exception as e:
        print(f"[FAIL] {sym_clean}: {e}", file=sys.stderr)
        return False


def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    mins = now.hour * 60 + now.minute
    return 555 <= mins <= 930  # 9:15 - 15:30 IST


def main():
    # Optional skip outside market hours (GitHub Actions schedule still tries)
    if os.environ.get("SKIP_MARKET_CHECK") != "1" and not is_market_open():
        print("[INFO] Market closed, skipping.")
        return

    symbols = INDICES + fetch_top_symbols(100)
    print(f"[INFO] Intraday refresh -> bucket '{BUCKET}' · {len(symbols)} symbols")

    ok = 0
    failed = []
    for i, ys in enumerate(symbols, 1):
        bundle = fetch_intraday(ys)
        if bundle:
            sym_clean = ys.replace(".NS", "").replace("^", "_").replace("=", "_")
            if upload(sym_clean, bundle):
                ok += 1
            else:
                failed.append(ys)
        else:
            failed.append(ys)
        time.sleep(SLEEP_BETWEEN)

    print(f"[OK] {datetime.now().isoformat()} · refreshed={ok}/{len(symbols)} failed={len(failed)}")


if __name__ == "__main__":
    main()
