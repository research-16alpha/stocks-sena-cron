"""
breadth_cron.py
===============
DAILY cron - computes market breadth signals for the Market Mood Index.
Runs via GitHub Actions at 4:30 PM IST (after daily_ohlcv_cron.py finishes).

For our tracked universe (currently top ~200 stocks by market cap), computes:
  - pct_above_50dma  : % of stocks trading above their own 50-day SMA
  - pct_above_200dma : same for 200-day SMA (secondary)
  - highs_52w        : count of stocks making a new 52-week high today
  - lows_52w         : count making a new 52-week low today

Reads from Supabase Storage bucket 'daily' (the same JSON our app reads).
Writes a single row per trading day to public.breadth_daily.

Source data: our own Supabase Storage (populated by daily_ohlcv_cron.py).
Output: one upsert per run to breadth_daily.
"""

import os
import sys
import json
from datetime import datetime

import requests
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

BUCKET = "daily"


def fetch_symbols(limit: int = 0) -> list[str]:
    """Full universe of stock_master for breadth calc.
    Was limited to 200 — that made breadth % meaningless. Now uses entire
    universe (top-down by mcap, NULLs last) for proper market-wide signal.
    """
    syms = []
    offset = 0
    while True:
        q = (sb.table("stock_master").select("symbol")
             .order("market_cap_cr", desc=True, nullsfirst=False)
             .range(offset, offset + 999))
        res = q.execute()
        batch = res.data or []
        if not batch: break
        syms.extend(r["symbol"] for r in batch if r.get("symbol"))
        if len(batch) < 1000: break
        offset += 1000
        if limit and len(syms) >= limit:
            syms = syms[:limit]
            break
    return syms


def fetch_bars(symbol: str) -> list[list] | None:
    """Pull daily JSON bundle for a symbol from Storage."""
    url = sb.storage.from_(BUCKET).get_public_url(f"{symbol}.json")
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        return data.get("bars", [])
    except Exception as e:
        print(f"[WARN] {symbol}: {e}", file=sys.stderr)
        return None


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def compute_for_symbol(bars: list[list]) -> dict | None:
    """
    Returns {
      'close': last close,
      'above_50dma': bool | None,
      'above_200dma': bool | None,
      'is_52w_high': bool,
      'is_52w_low': bool,
    } or None if not enough history.
    """
    if not bars or len(bars) < 50:
        return None

    closes = [b[4] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]

    last_close = closes[-1]
    last_high = highs[-1]
    last_low = lows[-1]

    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200) if len(closes) >= 200 else None

    # 52w = 252 trading days. If we have fewer, use what we have (≥ 50 days).
    window = min(252, len(bars))
    last_year_highs = highs[-window:-1] if window > 1 else []
    last_year_lows = lows[-window:-1] if window > 1 else []

    is_52w_high = last_high >= max(last_year_highs) if last_year_highs else False
    is_52w_low = last_low <= min(last_year_lows) if last_year_lows else False

    return {
        "close": last_close,
        "above_50dma": (last_close > sma50) if sma50 else None,
        "above_200dma": (last_close > sma200) if sma200 else None,
        "is_52w_high": is_52w_high,
        "is_52w_low": is_52w_low,
        "last_date": bars[-1][0],
    }


def main():
    symbols = fetch_symbols(300)  # safety - take up to 300, count what we get
    print(f"[INFO] Breadth compute over {len(symbols)} stocks")

    above50 = 0
    above200 = 0
    universe50 = 0          # stocks with ≥ 50 days history (eligible for 50-DMA)
    universe200 = 0
    highs = 0
    lows = 0
    latest_date = None

    for i, sym in enumerate(symbols, 1):
        bars = fetch_bars(sym)
        if not bars:
            continue
        res = compute_for_symbol(bars)
        if not res:
            continue

        if res["above_50dma"] is not None:
            universe50 += 1
            if res["above_50dma"]:
                above50 += 1

        if res["above_200dma"] is not None:
            universe200 += 1
            if res["above_200dma"]:
                above200 += 1

        if res["is_52w_high"]:
            highs += 1
        if res["is_52w_low"]:
            lows += 1

        if latest_date is None or res["last_date"] > latest_date:
            latest_date = res["last_date"]

        if i % 50 == 0:
            print(f"[INFO] {i}/{len(symbols)} processed")

    if universe50 == 0 or latest_date is None:
        print("[FAIL] No usable bars found in Storage. Aborting.")
        sys.exit(1)

    pct_50 = round(100 * above50 / universe50, 2)
    pct_200 = round(100 * above200 / universe200, 2) if universe200 > 0 else None

    row = {
        "date": latest_date,
        "universe_size": universe50,
        "pct_above_50dma": pct_50,
        "pct_above_200dma": pct_200,
        "highs_52w": highs,
        "lows_52w": lows,
    }
    print(f"[OK] Breadth: {row}")

    # Upsert on date (unique)
    sb.table("breadth_daily").upsert(row, on_conflict="date").execute()
    print(f"[OK] Wrote breadth_daily for {latest_date}")


if __name__ == "__main__":
    main()
