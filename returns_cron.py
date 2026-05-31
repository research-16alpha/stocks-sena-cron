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
from datetime import datetime, timedelta

import requests

# Make this module importable for its compute_returns() helper even where the
# supabase SDK / env vars aren't present (e.g. the stdlib-only one-off runner).
try:
    from supabase import create_client
except Exception:  # pragma: no cover - SDK absent
    create_client = None

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
sb = (
    create_client(SUPABASE_URL, SUPABASE_KEY)
    if (create_client and SUPABASE_URL and SUPABASE_KEY)
    else None
)

BUCKET = "daily"

# Date-based return / 52w math lives in one shared, dependency-free module so the
# daily Kite job, the 20y backfill and this cron can never drift apart again.
from returns_calc import compute_returns  # noqa: E402  (re-exported below)


def fetch_symbols() -> list[str]:
    # PostgREST caps a single select at 1000 rows — paginate, else returns were
    # only ever computed for the first 1000 stocks (the 26%-coverage bug).
    out: list[str] = []
    step, off = 1000, 0
    while True:
        res = sb.table("stock_master").select("symbol").range(off, off + step - 1).execute()
        batch = [r["symbol"] for r in (res.data or [])]
        out += batch
        if len(batch) < step:
            break
        off += step
    return out


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


def _patch(sym: str, updates: dict) -> bool:
    # Direct REST PATCH (thread-safe, unlike sharing the supabase client across threads).
    import urllib.parse
    url = f"{SUPABASE_URL}/rest/v1/stock_master?symbol=eq.{urllib.parse.quote(sym)}"
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
         "Content-Type": "application/json", "Prefer": "return=minimal"}
    try:
        r = requests.patch(url, headers=h, data=json.dumps(updates), timeout=20)
        return r.status_code in (200, 204)
    except Exception:
        return False


def main():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    symbols = fetch_symbols()
    print(f"[INFO] Returns compute over {len(symbols)} stocks (parallel)")

    def work(sym: str) -> str:
        bars = fetch_bars(sym)
        if not bars:
            return "skip"
        updates = compute_returns(bars)
        if not updates:
            return "skip"
        return "ok" if _patch(sym, updates) else "fail"

    ok = skipped = failed = 0
    t0 = datetime.now()
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(work, s): s for s in symbols}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            if r == "ok":
                ok += 1
            elif r == "skip":
                skipped += 1
            else:
                failed += 1
            if i % 500 == 0:
                print(f"  [{i}/{len(symbols)}] ok={ok} skipped={skipped} failed={failed}")

    print(f"[OK] {datetime.now().isoformat()} · updated={ok}/{len(symbols)} skipped={skipped} failed={failed} "
          f"elapsed={(datetime.now()-t0).total_seconds():.0f}s")


if __name__ == "__main__":
    main()
