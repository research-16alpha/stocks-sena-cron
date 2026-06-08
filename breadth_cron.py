"""
breadth_cron.py
===============
DAILY cron - computes market breadth signals for the Market Mood Index.
Runs via GitHub Actions at 4:30 PM IST (after daily_ohlcv_cron.py finishes).

For a FIXED market-wide universe (top-N ACTIVE stocks by market cap), computes:
  - pct_above_50dma  : % of stocks trading above their own 50-day SMA
  - pct_above_200dma : same for 200-day SMA (secondary)
  - highs_52w        : count of stocks making a new 52-week high today
  - lows_52w         : count making a new 52-week low today

Reads from Supabase Storage bucket 'daily' (the same JSON our app reads).
Writes a single row per trading day to public.breadth_daily.

Universe stability (fixed 2026-06-08): the old version fetched sequentially with a
single 15s timeout and NO retries, so on a slow run ~half the requests transiently
failed and those stocks silently dropped out — making universe_size swing wildly
(153 <-> 222 <-> 290) and the breadth % non-comparable day to day. It also had no
is_active filter, so delisted legacy tickers (HDFC, MINDTREE, RANBAXY...) burned
universe slots and always failed. Now: is_active only, parallel fetch WITH retries,
and a freshness gate (count only stocks whose latest bar is the current trading day)
so universe_size is the same stable set every day.

Run with --dry-run to print the result without writing to breadth_daily.
"""

import os
import sys
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

BUCKET = "daily"
UNIVERSE_N = 500          # top-N active stocks by market cap = broad, stable, market-wide set
FETCH_WORKERS = 24
FETCH_RETRIES = 3
DRY_RUN = "--dry-run" in sys.argv

# Shared pooled session: the default urllib3 pool holds only 10 connections, which throttled
# 16+ concurrent workers and silently dropped ~200 of 500 stocks per run (the real cause of the
# universe wobble). Size the pool to the worker count so every thread gets a live connection.
_session = requests.Session()
_session.mount("https://", HTTPAdapter(pool_connections=FETCH_WORKERS, pool_maxsize=FETCH_WORKERS))


def fetch_symbols(limit: int = UNIVERSE_N) -> list[str]:
    """Fixed market-wide universe: top-N ACTIVE stocks by market cap.
    is_active drops delisted legacy tickers (HDFC, MINDTREE, RANBAXY, CAIRN...) that
    have no daily bundle and otherwise burned universe slots / always failed."""
    syms: list[str] = []
    offset = 0
    while True:
        # gt("market_cap_cr", 0) is essential: supabase-py IGNORES nullsfirst=False, so NULL-mcap
        # rows sort FIRST and filled the whole universe with dataless micro-caps (EENTER, GLPL...)
        # instead of RELIANCE/HDFCBANK/TCS. Excluding NULL/0 mcap gives the real top-N by mcap.
        res = (sb.table("stock_master").select("symbol")
               .eq("is_active", True)
               .gt("market_cap_cr", 0)
               .order("market_cap_cr", desc=True)
               .range(offset, offset + 999)).execute()
        batch = res.data or []
        if not batch:
            break
        syms.extend(r["symbol"] for r in batch if r.get("symbol"))
        if len(batch) < 1000 or (limit and len(syms) >= limit):
            break
        offset += 1000
    return syms[:limit] if limit else syms


def fetch_bars(symbol: str) -> list[list] | None:
    """Pull daily JSON bundle from Storage, WITH retries. The old single-shot fetch
    dropped ~half the stocks on a slow run; retrying transient failures keeps the
    universe stable. A 404 is a genuine miss (delisted/new) - don't retry that."""
    url = sb.storage.from_(BUCKET).get_public_url(f"{symbol}.json")
    for attempt in range(FETCH_RETRIES):
        try:
            r = _session.get(url, timeout=20)
            if r.status_code == 200:
                return r.json().get("bars", [])
            if r.status_code == 404:
                return None
        except Exception as e:
            if attempt == FETCH_RETRIES - 1:
                print(f"[WARN] {symbol}: {e}", file=sys.stderr)
    return None


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def compute_for_symbol(bars: list[list]) -> dict | None:
    """Per-stock breadth flags, or None if not enough history (< 50 bars)."""
    if not bars or len(bars) < 50:
        return None

    closes = [b[4] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]

    last_close, last_high, last_low = closes[-1], highs[-1], lows[-1]
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200) if len(closes) >= 200 else None

    # 52w = 252 trading days. If we have fewer, use what we have (>= 50 days).
    window = min(252, len(bars))
    last_year_highs = highs[-window:-1] if window > 1 else []
    last_year_lows = lows[-window:-1] if window > 1 else []

    return {
        "above_50dma": (last_close > sma50) if sma50 else None,
        "above_200dma": (last_close > sma200) if sma200 else None,
        "is_52w_high": last_high >= max(last_year_highs) if last_year_highs else False,
        "is_52w_low": last_low <= min(last_year_lows) if last_year_lows else False,
        "last_date": bars[-1][0],
    }


def main():
    symbols = fetch_symbols()
    print(f"[INFO] Breadth over top {len(symbols)} active stocks by mcap "
          f"({'DRY-RUN' if DRY_RUN else 'LIVE'})")

    def work(sym):
        bars = fetch_bars(sym)
        return compute_for_symbol(bars) if bars else None

    results = [r for r in ThreadPoolExecutor(max_workers=FETCH_WORKERS).map(work, symbols) if r]
    fetched = len(results)

    # Freshness gate: the breadth day = the trading date most stocks closed on (the mode).
    # Count only stocks fresh to that date, so a handful of stale bundles can't distort the
    # signal and the universe is the same set every day.
    date_counts = Counter(r["last_date"] for r in results if r.get("last_date"))
    if not date_counts:
        print("[FAIL] No usable bars found in Storage. Aborting.")
        sys.exit(1)
    target_date, target_n = date_counts.most_common(1)[0]
    fresh = [r for r in results if r["last_date"] == target_date]
    stale = fetched - len(fresh)

    above50 = sum(1 for r in fresh if r["above_50dma"])
    universe50 = sum(1 for r in fresh if r["above_50dma"] is not None)
    above200 = sum(1 for r in fresh if r["above_200dma"])
    universe200 = sum(1 for r in fresh if r["above_200dma"] is not None)
    highs = sum(1 for r in fresh if r["is_52w_high"])
    lows = sum(1 for r in fresh if r["is_52w_low"])

    if universe50 == 0:
        print("[FAIL] No stocks with >= 50 days history on the target date. Aborting.")
        sys.exit(1)

    row = {
        "date": target_date,
        "universe_size": universe50,
        "pct_above_50dma": round(100 * above50 / universe50, 2),
        "pct_above_200dma": round(100 * above200 / universe200, 2) if universe200 else None,
        "highs_52w": highs,
        "lows_52w": lows,
    }
    print(f"[INFO] candidates={len(symbols)} fetched={fetched} fresh@{target_date}={len(fresh)} "
          f"stale_dropped={stale}  (universe50={universe50}, universe200={universe200})")
    print(f"[OK] Breadth: {row}")

    if DRY_RUN:
        print("[DRY-RUN] not writing to breadth_daily.")
        return
    sb.table("breadth_daily").upsert(row, on_conflict="date").execute()
    print(f"[OK] Wrote breadth_daily for {target_date}")


if __name__ == "__main__":
    main()
