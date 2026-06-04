"""
vol_metrics_cron.py
===================
Computes RELATIVE VOLUME (RVOL) for every stock and writes it to stock_master,
powering the "Most Active by volume surge" movers tab.

RVOL = today's volume / average volume over the prior window:
  rvol_1d = today_vol / previous day's volume
  rvol_1w = today_vol / avg(prior 5 trading days)
  rvol_1m = today_vol / avg(prior 21 trading days)

A value of 1.0 = trading at its normal pace; 3.0 = 3x its usual volume (a surge).

Source: the per-symbol daily OHLCV bars in Supabase Storage bucket 'daily'
  ({SYMBOL}.json -> { bars: [[date, o, h, l, c, volume], ...] }).
Freshness guard: skip a symbol whose latest bar is older than STALE_DAYS so a
suspended/untraded stock never shows a bogus surge.

Run (after the daily OHLCV refresh):  py -3.11 vol_metrics_cron.py
Test:  py -3.11 vol_metrics_cron.py --dry-run --limit 40
"""
import os, sys, json, argparse, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("SUPABASE_URL", "https://tbeadvvkqyrhtendttrg.supabase.co").rstrip("/")
READ_KEY = os.environ.get("SUPABASE_ANON_KEY", "sb_publishable_U0d8_o8JiT-dQ_jGP2liJA_dPr58Tig")

def _service_key():
    k = os.environ.get("SUPABASE_SERVICE_KEY")
    if k:
        return k.strip()
    for p in (r"e:\Stocks sena\.supabase-service-key", os.path.expanduser("~/.supabase-service-key")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            pass
    return None

STALE_DAYS = 5            # latest bar must be within this many days
MIN_AVG_TURNOVER = 0      # liquidity is now a USER filter (Min-volume dropdown on Most
                          # Active), not a hard cut. Only skip never-traded names (turnover 0).
RVOL_CAP = 50.0           # clamp display so a quiet prior day can't show "1334x"
WORKERS = 24
IST = timezone(timedelta(hours=5, minutes=30))

def get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"apikey": READ_KEY, "Authorization": "Bearer " + READ_KEY})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def fetch_symbols(limit=0):
    syms, off = [], 0
    while True:
        rows = get_json(f"{BASE}/rest/v1/stock_master?select=symbol&is_active=eq.true&limit=1000&offset={off}")
        syms += [r["symbol"] for r in rows]
        if len(rows) < 1000:
            break
        off += 1000
        if limit and len(syms) >= limit:
            break
    return syms[:limit] if limit else syms

def _avg(vals):
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None

def compute(symbol, today):
    """Return (symbol, rvol_1d, rvol_1w, rvol_1m) or None if not computable/stale."""
    try:
        b = get_json(f"{BASE}/storage/v1/object/public/daily/{symbol}.json")
    except Exception:
        return None
    bars = b.get("bars") if isinstance(b, dict) else (b if isinstance(b, list) else None)
    if not bars or len(bars) < 2:
        return None
    # latest bar freshness
    try:
        last_date = datetime.strptime(str(bars[-1][0])[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    if (today - last_date).days > STALE_DAYS:
        return None
    vols, turns = [], []
    for row in bars:
        try:
            v = float(row[5]); c = float(row[4])
        except Exception:
            v, c = None, None
        vols.append(v)
        turns.append((v * c) if (v is not None and c is not None) else None)
    tv = vols[-1]
    if not tv or tv <= 0:
        return None
    # Liquidity gate: the stock must average real turnover, else RVOL is just
    # divide-by-near-zero noise on an illiquid name (not genuinely "active").
    base_turn = _avg([t for t in turns[-22:-1]]) if len(turns) >= 6 else _avg(turns[:-1])
    if base_turn is None or base_turn <= MIN_AVG_TURNOVER:
        return None  # never-traded baseline -> RVOL undefined
    prev = vols[-2] if len(vols) >= 2 else None
    avg5 = _avg(vols[-6:-1]) if len(vols) >= 6 else None
    avg21 = _avg(vols[-22:-1]) if len(vols) >= 22 else None
    def ratio(den):
        if not den or den <= 0:
            return None
        return round(min(tv / den, RVOL_CAP), 2)
    r1d, r1w, r1m = ratio(prev), ratio(avg5), ratio(avg21)
    if r1d is None and r1w is None and r1m is None:
        return None
    return (symbol, r1d, r1w, r1m)

def patch_row(args):
    """UPDATE one row's rvol columns (symbol has no unique constraint, so no upsert)."""
    symbol, a, w, m, key = args
    body = json.dumps({"rvol_1d": a, "rvol_1w": w, "rvol_1m": m}).encode("utf-8")
    url = f"{BASE}/rest/v1/stock_master?symbol=eq.{urllib.parse.quote(symbol, safe='')}"
    req = urllib.request.Request(
        url, data=body, method="PATCH",
        headers={
            "apikey": key, "Authorization": "Bearer " + key,
            "Content-Type": "application/json", "Prefer": "return=minimal",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
        return True
    except Exception:
        return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--syms", default="")
    args = ap.parse_args()

    today = datetime.now(IST).date()
    if args.syms:
        syms = [s.strip().upper() for s in args.syms.split(",") if s.strip()]
    else:
        syms = fetch_symbols(args.limit)
    print(f"[vol_metrics] {len(syms)} symbols · today={today} · dry_run={args.dry_run}")

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, res in enumerate(ex.map(lambda s: compute(s, today), syms)):
            if res:
                results.append(res)
            if i and i % 500 == 0:
                print(f"  scanned {i}/{len(syms)} computed={len(results)}")
    print(f"[vol_metrics] computed RVOL for {len(results)}/{len(syms)} symbols")

    # show a few biggest 1D surges as a sanity check
    sample = sorted([r for r in results if r[1] is not None], key=lambda r: -r[1])[:8]
    for s, a, w, m in sample:
        print(f"    {s:12} 1d={a}x 1w={w}x 1m={m}x")

    if args.dry_run:
        print("[vol_metrics] dry-run: no writes")
        return
    key = _service_key()
    if not key:
        print("[vol_metrics] ERROR: no service key for writes"); sys.exit(1)
    written = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, ok in enumerate(ex.map(patch_row, [(s, a, w, m, key) for (s, a, w, m) in results])):
            if ok:
                written += 1
            if i and i % 500 == 0:
                print(f"  wrote {written}/{len(results)}")
    print(f"[vol_metrics] DONE wrote {written}/{len(results)} rows")

if __name__ == "__main__":
    main()
