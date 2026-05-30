"""
backfill_sector.py
==================
Populates stock_master.sector / .industry from the NSE index-constituent data we
already pull. SectorHeatmap + sector drill-downs only had ~194/4186 stocks tagged;
this lifts coverage to the full NIFTY-500 investable universe (the names a sector
heatmap actually shows), plus sector-index membership for granular sector.

Sources (in priority):
  1. index_constituents 'NIFTY 500' rows → the NSE macro `industry` field
     (e.g. "Metals & Mining", "Healthcare") = our `sector` + `industry`.
  2. Sector-index membership (NIFTY IT/PHARMA/AUTO/METAL/FMCG/ENERGY/FIN SERVICE)
     → maps a stock to a clean sector even if not in NIFTY 500.

Fill-don't-overwrite: only sets sector where it's currently NULL (keeps any
existing curated value). Upsert via PATCH per symbol.
"""
import json
import os
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
KEY = os.environ.get('SUPABASE_SERVICE_KEY') or open('e:/Stocks sena/.supabase-service-key').read().strip()
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

# Sector-index name -> clean sector label
SECTOR_INDEX = {
    'NIFTY IT': 'IT', 'NIFTY PHARMA': 'Pharma', 'NIFTY AUTO': 'Auto',
    'NIFTY METAL': 'Metals', 'NIFTY FMCG': 'FMCG', 'NIFTY ENERGY': 'Energy',
    'NIFTY FIN SERVICE': 'Financial Services', 'NIFTY BANK': 'Banking',
}


def get_all(table, select, extra=''):
    rows = []
    off = 0
    while True:
        r = requests.get(f'{URL}/rest/v1/{table}?select={select}&limit=1000&offset={off}{extra}',
                         headers=H, timeout=30)
        b = r.json()
        if not b:
            break
        rows += b
        off += 1000
        if len(b) < 1000:
            break
    return rows


def main():
    ic = get_all('index_constituents', 'symbol,industry,index_name')
    # symbol -> (sector, industry)
    chosen = {}
    # pass 1: NIFTY 500 industry = macro sector
    for r in ic:
        if r['index_name'] == 'NIFTY 500' and r.get('industry'):
            chosen.setdefault(r['symbol'], (r['industry'], r['industry']))
    # pass 2: sector-index membership (fills names not in NIFTY 500)
    for r in ic:
        sec = SECTOR_INDEX.get(r['index_name'])
        if sec and r['symbol'] not in chosen:
            chosen[r['symbol']] = (sec, r.get('industry') or sec)
    print(f'[INFO] sector resolvable for {len(chosen)} symbols from index_constituents')

    # only active stocks currently missing sector
    sm = get_all('stock_master', 'symbol,sector,is_active', '&is_active=eq.true')
    targets = [(r['symbol'], chosen[r['symbol']]) for r in sm
               if not r.get('sector') and r['symbol'] in chosen]
    print(f'[INFO] {len(targets)} active stocks missing sector that we can fill')

    def patch(item):
        sym, (sector, industry) = item
        u = f"{URL}/rest/v1/stock_master?symbol=eq.{urllib.parse.quote(sym)}"
        r = requests.patch(u, headers={**H, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'},
                           data=json.dumps({'sector': sector, 'industry': industry}), timeout=20)
        return r.status_code in (200, 204)

    ok = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        for f in as_completed([ex.submit(patch, t) for t in targets]):
            if f.result():
                ok += 1
    print(f'[done] sector/industry set on {ok} stocks')
    # report new coverage
    def cnt(q):
        r = requests.get(f'{URL}/rest/v1/stock_master?{q}', headers={**H, 'Prefer': 'count=exact', 'Range': '0-0'}, timeout=20)
        return int(r.headers.get('content-range', '*/0').split('/')[-1])
    a = cnt('is_active=eq.true')
    s = cnt('is_active=eq.true&sector=not.is.null')
    print(f'[coverage] sector now {s}/{a} ({100*s/a:.0f}%)')


if __name__ == '__main__':
    main()
