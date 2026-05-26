"""
daily_ohlcv_cron.py
===================
DAILY cron - refreshes 5-year daily OHLCV for every stock in stock_master.
Runs via GitHub Actions at 5 PM IST (after market close).

Source: Yahoo Finance Chart API DIRECT (no yfinance lib).
  - yfinance was getting blocked by Yahoo on GitHub Actions runners
    ("Expecting value: line 1 column 1 (char 0)" silent failures since ~March 2026).
  - The Chart endpoint we use here works (same one our app uses successfully).

Output: Updates {SYMBOL}.json in Supabase Storage bucket 'daily'.
Same JSON shape as before: { symbol, interval, from, to, bars: [[date, o, h, l, c, v], ...] }

Rate: 1.0s sleep between calls. ~3-4 min for 200 stocks.
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

BUCKET = "daily"
SLEEP_BETWEEN = 1.0
BATCH_BREAK = 15
BATCH_SIZE = 50

YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# IST = UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))


def fetch_symbols(limit: int = 0) -> list[str]:
    """All symbols from stock_master. Default: no limit (= full universe).
    Yahoo will gracefully 404 on stocks that aren't on its system."""
    syms = []
    offset = 0
    while True:
        q = sb.table("stock_master").select("symbol").order("market_cap_cr", desc=True, nullsfirst=False)
        q = q.range(offset, offset + 999)
        res = q.execute()
        batch = res.data or []
        if not batch:
            break
        syms.extend(r["symbol"] for r in batch if r.get("symbol"))
        if len(batch) < 1000:
            break
        offset += 1000
        if limit and len(syms) >= limit:
            syms = syms[:limit]
            break
    # Skip BSE_<scrip> fallbacks - Yahoo won't have them
    syms = [s for s in syms if not s.startswith('BSE_')]
    return syms


def _fetch_ohlcv_single(yahoo_ticker: str) -> dict | None:
    """Fetch 5y OHLCV for ONE specific Yahoo ticker form. Returns parsed bundle or None."""
    url = f"{YAHOO_BASE}/{yahoo_ticker}"
    params = {"interval": "1d", "range": "5y"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        payload = r.json()
        result = (payload.get("chart", {}).get("result") or [None])[0]
        if not result:
            return None
        timestamps = result.get("timestamp") or []
        if not timestamps:
            return None
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        vols = quote.get("volume") or []
        bars = []
        for i, ts in enumerate(timestamps):
            if i >= len(closes) or closes[i] is None:
                continue
            d = datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d")
            bars.append([
                d,
                round(float(opens[i]), 2) if opens[i] is not None else None,
                round(float(highs[i]), 2) if highs[i] is not None else None,
                round(float(lows[i]), 2) if lows[i] is not None else None,
                round(float(closes[i]), 2),
                int(vols[i]) if vols[i] not in (None, 0) else 0,
            ])
        if not bars:
            return None
        return {
            "interval": "1d",
            "from": bars[0][0],
            "to": bars[-1][0],
            "bars": bars,
            "_ticker_used": yahoo_ticker,
        }
    except Exception:
        return None


def fetch_ohlcv(symbol: str) -> dict | None:
    """Pull 5y daily OHLCV with Yahoo fallback chain:
      1. .NS suffix (NSE)
      2. .BO suffix (BSE by symbol)
      3. <numeric>.BO (for BSE-prefixed names like BSE500014 → 500014.BO)
    Returns bundle with `symbol` set, or None if no ticker variant has data.
    """
    import re
    # 1. NSE
    bundle = _fetch_ohlcv_single(f"{symbol}.NS")
    if bundle:
        bundle["symbol"] = symbol
        return bundle
    # 2. BSE by symbol
    bundle = _fetch_ohlcv_single(f"{symbol}.BO")
    if bundle:
        bundle["symbol"] = symbol
        return bundle
    # 3. BSE by numeric scrip code (BSE500014 → 500014.BO, BSE_501242 → 501242.BO)
    m = re.match(r"^BSE_?(\d+)$", symbol)
    if m:
        bundle = _fetch_ohlcv_single(f"{m.group(1)}.BO")
        if bundle:
            bundle["symbol"] = symbol
            return bundle
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


def _process_one(sym: str) -> tuple:
    """Worker: fetch OHLCV + upload. Returns (sym, status, bar_count)."""
    bundle = fetch_ohlcv(sym)
    if not bundle:
        return (sym, 'NO_QUOTE', 0)
    if upload(sym, bundle):
        return (sym, 'OK', len(bundle['bars']))
    return (sym, 'UPLOAD_ERR', 0)


def main():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    # Default: full universe. Override via env LIMIT=200 to restrict.
    limit = int(os.environ.get("LIMIT", "0"))
    workers = int(os.environ.get("WORKERS", "10"))
    symbols = fetch_symbols(limit)
    print(f"[INFO] Daily OHLCV refresh -> bucket '{BUCKET}' · {len(symbols)} stocks · {workers} workers")

    ok = 0
    no_quote = 0
    upload_err = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_process_one, s): s for s in symbols}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                sym, status, bars = fut.result()
            except Exception:
                continue
            if status == 'OK': ok += 1
            elif status == 'NO_QUOTE': no_quote += 1
            elif status == 'UPLOAD_ERR': upload_err += 1
            if i % 200 == 0:
                elapsed = time.time() - t0
                rate = i / elapsed
                eta = (len(symbols) - i) / rate if rate > 0 else 0
                print(f'  {i}/{len(symbols)}  ok={ok} no_quote={no_quote} upload_err={upload_err}  '
                      f'rate={rate:.1f}/s  eta={eta:.0f}s')

    elapsed = time.time() - t0
    print(f'\n[done] {ok}/{len(symbols)} OK · {no_quote} no quote · {upload_err} upload errs · {elapsed:.0f}s')


if __name__ == "__main__":
    main()
