"""
returns_cron.py
===============
DAILY cron - computes return_1m_pct, return_3m_pct, return_6m_pct, return_1y_pct,
return_5y_pct for every stock in stock_master using our own daily Storage as
source of truth. Runs AFTER daily_ohlcv_cron.py so the closes are fresh.

Why this exists:
  - stock_master_scraper.py populates these fields via yfinance .info, which has
    been silently failing on GitHub Actions runners.
  - We already have clean 5y OHLCV history in our daily/{SYMBOL}.json Storage.
    Computing returns from that data is more reliable AND a single source of
    truth (no risk of latest_price disagreeing with last bar).

Output: bulk UPDATE on stock_master per symbol.
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

# Trading days lookback. NSE: ~252/yr.
PERIODS = {
    "return_1m_pct": 21,
    "return_3m_pct": 63,
    "return_6m_pct": 126,
    "return_1y_pct": 252,
    "return_5y_pct": 252 * 5,
}


def fetch_symbols() -> list[str]:
    res = sb.table("stock_master").select("symbol").execute()
    return [r["symbol"] for r in (res.data or [])]


def fetch_bars(symbol: str) -> list[list] | None:
    url = sb.storage.from_(BUCKET).get_public_url(f"{symbol}.json")
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return None
        return r.json().get("bars") or []
    except Exception as e:
        print(f"[WARN] {symbol}: {e}", file=sys.stderr)
        return None


def compute_returns(bars: list[list]) -> dict | None:
    if not bars or len(bars) < 2:
        return None
    closes = [b[4] for b in bars if b[4] is not None]
    if len(closes) < 2:
        return None
    today = closes[-1]
    out: dict = {"latest_price": round(today, 2)}
    for field, lookback in PERIODS.items():
        if len(closes) > lookback:
            past = closes[-1 - lookback]
            if past and past > 0:
                out[field] = round((today / past - 1) * 100, 2)
            else:
                out[field] = None
        else:
            out[field] = None
    # high_52w / low_52w from last 252 bars (or all if fewer)
    window = min(252, len(bars))
    last_year_highs = [b[2] for b in bars[-window:] if b[2] is not None]
    last_year_lows = [b[3] for b in bars[-window:] if b[3] is not None]
    if last_year_highs:
        out["high_52w"] = round(max(last_year_highs), 2)
    if last_year_lows:
        out["low_52w"] = round(min(last_year_lows), 2)
    # price_change_pct = today vs prev close
    if len(closes) >= 2 and closes[-2] > 0:
        out["price_change_pct"] = round((today / closes[-2] - 1) * 100, 2)
    return out


def main():
    symbols = fetch_symbols()
    print(f"[INFO] Returns compute over {len(symbols)} stocks")

    ok = 0
    skipped = 0
    failed = []

    for i, sym in enumerate(symbols, 1):
        bars = fetch_bars(sym)
        if not bars:
            skipped += 1
            continue
        updates = compute_returns(bars)
        if not updates:
            skipped += 1
            continue
        try:
            sb.table("stock_master").update(updates).eq("symbol", sym).execute()
            ok += 1
            if i <= 3 or i % 50 == 0:
                r1m = updates.get("return_1m_pct")
                r1y = updates.get("return_1y_pct")
                print(f"[OK] {sym}: 1m={r1m}% 1y={r1y}%")
        except Exception as e:
            print(f"[FAIL] {sym}: {e}", file=sys.stderr)
            failed.append(sym)

    print(f"[OK] {datetime.now().isoformat()} · updated={ok}/{len(symbols)} skipped={skipped} failed={len(failed)}")
    if failed:
        print(f"[FAIL] {failed[:15]}{'...' if len(failed) > 15 else ''}")


if __name__ == "__main__":
    main()
