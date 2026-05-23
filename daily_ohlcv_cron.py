"""
daily_ohlcv_cron.py
===================
DAILY cron — refreshes 5-year daily OHLCV for every stock in stock_master.
Runs via GitHub Actions at 4 PM IST (after market close).

Source: yfinance (Yahoo Finance, free, rate-limited)
Output: Updates {SYMBOL}.json in Supabase Storage bucket 'daily'

Strategy: For each symbol, fetch 5y daily history fresh (overwrite entire JSON).
Simpler than diff/append and ensures no gaps from missed days.

Rate: 1.5s sleep between calls + 30s pause every 50 stocks.
~6-7 min runtime for 200 stocks.
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta

import yfinance as yf
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

BUCKET = "daily"
SLEEP_BETWEEN = 1.5
BATCH_BREAK = 30
BATCH_SIZE = 50
YEARS_BACK = 5


def fetch_symbols(limit: int = 200) -> list[str]:
    """Top symbols by market_cap_cr from stock_master."""
    res = (
        sb.table("stock_master")
        .select("symbol")
        .order("market_cap_cr", desc=True)
        .limit(limit)
        .execute()
    )
    return [r["symbol"] for r in (res.data or [])]


def fetch_ohlcv(symbol: str) -> dict | None:
    """Pull 5y daily from yfinance, format as compact JSON bundle."""
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        hist = ticker.history(period=f"{YEARS_BACK}y", interval="1d")
        if hist.empty:
            return None

        # Normalize index to tz-naive
        if hist.index.tz is not None:
            hist.index = hist.index.tz_localize(None)

        bars = []
        for ts, row in hist.iterrows():
            bars.append([
                ts.strftime("%Y-%m-%d"),
                round(float(row["Open"]), 2),
                round(float(row["High"]), 2),
                round(float(row["Low"]), 2),
                round(float(row["Close"]), 2),
                int(row["Volume"]) if row["Volume"] > 0 else 0,
            ])

        return {
            "symbol": symbol,
            "interval": "1d",
            "from": bars[0][0],
            "to": bars[-1][0],
            "bars": bars,
        }
    except Exception as e:
        print(f"[WARN] {symbol}: {e}", file=sys.stderr)
        return None


def upload(symbol: str, bundle: dict) -> bool:
    payload = json.dumps(bundle, separators=(",", ":")).encode("utf-8")
    path = f"{symbol}.json"
    try:
        try:
            sb.storage.from_(BUCKET).update(path, payload, {"contentType": "application/json"})
        except Exception:
            sb.storage.from_(BUCKET).upload(path, payload, {"contentType": "application/json"})
        return True
    except Exception as e:
        print(f"[FAIL] upload {symbol}: {e}", file=sys.stderr)
        return False


def main():
    symbols = fetch_symbols(200)
    print(f"[INFO] Daily OHLCV refresh -> bucket '{BUCKET}' · {len(symbols)} stocks")

    ok = 0
    failed = []

    for i, sym in enumerate(symbols, 1):
        bundle = fetch_ohlcv(sym)
        if bundle and upload(sym, bundle):
            ok += 1
        else:
            failed.append(sym)

        time.sleep(SLEEP_BETWEEN)
        if (i % BATCH_SIZE) == 0:
            print(f"[INFO] {i}/{len(symbols)} · ok={ok} failed={len(failed)} · sleeping {BATCH_BREAK}s")
            time.sleep(BATCH_BREAK)

    print(f"[OK] {datetime.now().isoformat()} · refreshed={ok}/{len(symbols)} failed={len(failed)}")
    if failed:
        print(f"[FAIL] {failed[:20]}{'...' if len(failed) > 20 else ''}")


if __name__ == "__main__":
    main()
