"""
backfill_shp_table.py
=====================
Populates shareholding_periods.{fii_pct,dii_pct,government_pct} from the corrected
primary BSE shareholding now sitting in each bundle's `shareholding` array. The
app's Investors tab (useShareholding) reads category totals from this table; it
previously had only promoter_pct/public_pct, so FII/DII were (wrongly) summed from
1%+ named holders -> badly undercounted (FII 3% vs real 26%). This writes the
authoritative category percentages so the app shows the right split.

Upsert on (symbol, period) — idempotent.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
KEY = os.environ.get('SUPABASE_SERVICE_KEY') or open('e:/Stocks sena/.supabase-service-key').read().strip()
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}
BUCKET = 'fundamentals-v2'


def active_syms():
    out = []
    off = 0
    while True:
        r = requests.get(f'{URL}/rest/v1/stock_master?select=symbol&is_active=eq.true'
                         f'&limit=1000&offset={off}', headers=H, timeout=30)
        b = r.json()
        if not b:
            break
        out += [x['symbol'] for x in b]
        off += 1000
        if len(b) < 1000:
            break
    return out


def process(sym):
    try:
        d = requests.get(f'{URL}/storage/v1/object/public/{BUCKET}/{sym}.json', timeout=25).json()
    except Exception:
        return (sym, 0)
    sh = d.get('shareholding') or []
    rows = []
    for r in sh:
        p = r.get('period')
        if not p or r.get('_source') != 'bse_shp':
            continue  # only push primary BSE rows (don't overwrite table with screener)
        rows.append({
            'symbol': sym, 'period': p,
            'promoter_pct': r.get('promoters'),
            'public_pct': r.get('public'),
            'fii_pct': r.get('fii'),
            'dii_pct': r.get('dii'),
            'government_pct': r.get('government'),
            'source': 'bse_shp',
        })
    if not rows:
        return (sym, 0)
    rr = requests.post(f'{URL}/rest/v1/shareholding_periods?on_conflict=symbol,period',
                       headers={**H, 'Content-Type': 'application/json',
                                'Prefer': 'resolution=merge-duplicates,return=minimal'},
                       data=json.dumps(rows, default=str), timeout=30)
    return (sym, len(rows) if rr.status_code in (200, 201, 204) else 0)


def main():
    syms = active_syms()
    print(f'[INFO] {len(syms)} active stocks -> backfill shareholding_periods from bundles')
    ok = total = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=16) as ex:
        for i, f in enumerate(as_completed([ex.submit(process, s) for s in syms]), 1):
            sym, n = f.result()
            if n:
                ok += 1
                total += n
            if i % 500 == 0:
                print(f'  [{i}/{len(syms)}] stocks_written={ok} rows={total} elapsed={time.time()-t0:.0f}s')
    print(f'\n[done] {ok} stocks, {total} period-rows upserted in {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
