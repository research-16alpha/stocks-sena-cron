"""
intraday_cron.py
================
Runs every 10 min between 9:15 AM - 3:30 PM IST, weekdays.
Fetches today's 1-min OHLCV for top 100 stocks + indices.

Source: Yahoo Finance Chart API DIRECT (no yfinance lib).
  - yfinance was getting silently blocked by Yahoo on GitHub Actions
    runners (intraday Storage was 100% empty as a result).
  - The Chart endpoint we use here works (same one our app uses successfully).

Output: intraday/{SYMBOL}.json in Supabase Storage bucket 'intraday'

JSON shape (unchanged - app code reads this format):
  { "symbol": "RELIANCE", "date": "2026-05-26", "bars": [["09:15", price, vol], ...] }

Used for: live sparklines on Home / Watchlist / Sector grid + IntradayChart.
"""

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta

import requests
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

BUCKET = "intraday"
SLEEP_BETWEEN = 0.8

YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

IST = timezone(timedelta(hours=5, minutes=30))

# Top 100 + indices + 4 newly-added sector indices for SectorDetailScreen
INDICES = [
    "^NSEI", "^BSESN", "^NSEBANK", "^CNXIT", "^CNXPHARMA",
    "^CNXFMCG", "^CNXAUTO", "^CNXMETAL", "INR=X",
    "^CNXFIN", "^CNXENERGY", "^CNXREALTY", "^CNXMEDIA",  # ^CNXFIN — Yahoo delisted ^CNXFINANCE 2026-05
]


def fetch_top_symbols(limit: int = 100) -> list[str]:
    """Top-N stocks by market cap, with the same filters as the shareholding
    cron: drop NULL mcap, drop BSE-only scrip codes, drop garbage mcap rows.
    Yahoo `.NS` endpoint returns 404 for those — wasted requests."""
    res = (
        sb.table("stock_master")
        .select("symbol,market_cap_cr")
        .not_.is_("market_cap_cr", "null")
        .lt("market_cap_cr", 10_000_000)
        .order("market_cap_cr", desc=True)
        .limit(limit * 2)
        .execute()
    )
    rows = res.data or []
    clean = [r["symbol"] for r in rows if not r["symbol"].startswith("BSE")]
    return [s + ".NS" for s in clean[:limit]]


def fetch_intraday(yahoo_sym: str) -> dict | None:
    """Pull today's 1-min from Yahoo Chart API. Returns compact bundle or None."""
    url = f"{YAHOO_BASE}/{yahoo_sym}"
    params = {"interval": "1m", "range": "1d"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[WARN] {yahoo_sym}: HTTP {r.status_code}", file=sys.stderr)
            return None
        payload = r.json()
        result = (payload.get("chart", {}).get("result") or [None])[0]
        if not result:
            return None

        timestamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        vols = quote.get("volume") or []

        if not timestamps:
            return None

        bars = []
        for i, ts in enumerate(timestamps):
            if i >= len(closes) or closes[i] is None:
                continue
            hhmm = datetime.fromtimestamp(ts, tz=IST).strftime("%H:%M")
            bars.append([
                hhmm,
                round(float(closes[i]), 2),
                int(vols[i]) if (i < len(vols) and vols[i] not in (None, 0)) else 0,
            ])

        if not bars:
            return None

        today = datetime.now(IST).strftime("%Y-%m-%d")
        sym_clean = yahoo_sym.replace(".NS", "").replace("^", "_").replace("=", "_")
        return {"symbol": sym_clean, "date": today, "bars": bars}
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
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 555 <= mins <= 930  # 9:15 - 15:30 IST


def main():
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
                if i <= 3:
                    print(f"[OK] {ys}: {len(bundle['bars'])} bars")
            else:
                failed.append(ys)
        else:
            failed.append(ys)
        time.sleep(SLEEP_BETWEEN)

    print(f"[OK] {datetime.now(IST).isoformat()} · refreshed={ok}/{len(symbols)} failed={len(failed)}")
    if failed:
        print(f"[FAIL] {failed[:15]}{'...' if len(failed) > 15 else ''}")


if __name__ == "__main__":
    main()
