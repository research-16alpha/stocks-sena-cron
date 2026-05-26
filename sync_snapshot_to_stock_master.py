"""
sync_snapshot_to_stock_master.py
==================================
Reads `snapshot[0]` from every fundamentals-v2 bundle and upserts the
market-data fields back into the `stock_master` table.

Why: `daily_snapshot_refresh.py` writes price + market_cap_cr only to bundles,
but the app's search / screener / sector heatmap all read stock_master.
Without this sync, stock_master mcap is stuck at the initial seed (top ~200
stocks only), making search ranking broken for the rest of the universe.

Fields synced from snapshot → stock_master:
  current_price → latest_price
  market_cap_cr → market_cap_cr
  pe → pe_ratio
  pb → pb_ratio
  week52_high → high_52w
  week52_low → low_52w
  day_change_pct → price_change_pct

Runs in batches of 500 via PostgREST upsert. Safe to re-run.
"""
import os
import sys
import json
import time
import urllib.request

KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not KEY:
    with open('e:/Stocks sena/.supabase-service-key', 'r') as f:
        KEY = f.read().strip()
URL = 'https://tbeadvvkqyrhtendttrg.supabase.co'
HEADERS = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}


def fetch_all_stock_master_symbols():
    syms = []
    offset = 0
    while True:
        url = f'{URL}/rest/v1/stock_master?select=symbol&limit=1000&offset={offset}'
        r = urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=20)
        batch = json.loads(r.read())
        if not batch:
            break
        syms.extend(s['symbol'] for s in batch)
        offset += 1000
        if len(batch) < 1000:
            break
    return syms


def fetch_bundle_snapshot(sym):
    url = f'{URL}/storage/v1/object/public/fundamentals-v2/{sym}.json'
    try:
        r = urllib.request.urlopen(url, timeout=15)
        b = json.loads(r.read())
        return (b.get('snapshot') or [{}])[0]
    except Exception:
        return {}


def patch_one(row):
    """PATCH a single stock_master row by symbol. Returns 1 on success, 0 on failure."""
    sym = row['symbol']
    body = {k: v for k, v in row.items() if k != 'symbol'}
    if not body:
        return 0
    headers = {**HEADERS, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}
    url = f'{URL}/rest/v1/stock_master?symbol=eq.{sym}'
    payload = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers=headers, method='PATCH')
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return 1 if r.status in (200, 204) else 0
    except urllib.error.HTTPError as e:
        if e.code != 404:  # 404 = symbol not in master, skip silently
            print(f'[ERR] {sym} HTTP {e.code}: {e.read()[:200].decode("utf-8","ignore")}', file=sys.stderr)
        return 0
    except Exception as e:
        print(f'[ERR] {sym}: {e}', file=sys.stderr)
        return 0


def main(workers: int = 12, batch_size: int = 400):
    from concurrent.futures import ThreadPoolExecutor
    syms = fetch_all_stock_master_symbols()
    print(f'[sync] {len(syms)} symbols in stock_master')

    updates = []
    t0 = time.time()
    fetched = 0
    skipped = 0

    def fetch_one(sym):
        return (sym, fetch_bundle_snapshot(sym))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (sym, snap) in enumerate(ex.map(fetch_one, syms, chunksize=8)):
            if i % 200 == 0 and i:
                elapsed = time.time() - t0
                rate = i / elapsed
                eta = (len(syms) - i) / rate
                print(f'  {i}/{len(syms)}  with_snap={fetched}  empty={skipped}  rate={rate:.1f}/s  ETA={eta:.0f}s')
            if not snap or not snap.get('current_price'):
                skipped += 1
                continue
            fetched += 1
            row = {'symbol': sym}
            if snap.get('current_price') is not None:
                row['latest_price'] = snap['current_price']
            if snap.get('market_cap_cr') is not None:
                row['market_cap_cr'] = snap['market_cap_cr']
            if snap.get('pe') is not None:
                row['pe_ratio'] = snap['pe']
            if snap.get('pb') is not None:
                row['pb_ratio'] = snap['pb']
            if snap.get('week52_high') is not None:
                row['high_52w'] = snap['week52_high']
            if snap.get('week52_low') is not None:
                row['low_52w'] = snap['week52_low']
            if snap.get('day_change_pct') is not None:
                row['price_change_pct'] = snap['day_change_pct']
            updates.append(row)

    # PATCH per row, parallelised
    print(f'\n[push] {len(updates)} rows to PATCH (parallel × {workers})')
    pushed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, n in enumerate(ex.map(patch_one, updates, chunksize=4), 1):
            pushed += n
            if i % 200 == 0 or i == len(updates):
                elapsed = time.time() - t0
                rate = i / max(elapsed, 1e-6)
                print(f'  {i}/{len(updates)}  pushed={pushed}  rate={rate:.1f}/s')

    elapsed = time.time() - t0
    print(f'\n[done] {pushed}/{len(updates)} rows synced in {elapsed:.0f}s')


if __name__ == '__main__':
    main()
