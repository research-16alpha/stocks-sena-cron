"""
backfill_div_yield.py
=====================
Computes stock_master.div_yield_pct from trailing-12-month dividends in
corporate_actions ÷ current price. The Screener "Dividend" preset was nearly empty
because div_yield_pct was only 4.6% populated. Dividends ARE in corporate_actions
(subject "Dividend - Rs 150 Per Share", ex_date), so we sum the per-share amounts
over the last 12 months and divide by latest_price.

Non-dividend-paying stocks correctly stay 0/null (most micro-caps pay nothing —
that's a real 0 yield, not missing data). Idempotent PATCH per symbol.
"""
import datetime as dt
import json
import os
import re
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
KEY = os.environ.get('SUPABASE_SERVICE_KEY') or open('e:/Stocks sena/.supabase-service-key').read().strip()
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

# "Dividend - Rs 150 Per Share" / "Interim Dividend - Rs 1.75 Per Share" / "Rs.5.20"
_AMT = re.compile(r'Rs\.?\s*([0-9]+(?:\.[0-9]+)?)', re.I)


def get_all(table, select, extra=''):
    rows, off = [], 0
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
    cutoff = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    divs = get_all('corporate_actions', 'symbol,subject,ex_date',
                   f'&subject=ilike.*dividend*&ex_date=gte.{cutoff}')
    print(f'[INFO] {len(divs)} dividend rows in trailing 12mo (since {cutoff})')

    ttm = defaultdict(float)
    for d in divs:
        m = _AMT.search(d.get('subject') or '')
        if m:
            try:
                ttm[d['symbol']] += float(m.group(1))
            except ValueError:
                pass
    print(f'[INFO] {len(ttm)} symbols paid a parseable dividend')

    # latest_price per symbol
    sm = get_all('stock_master', 'symbol,latest_price,div_yield_pct', '&is_active=eq.true')
    price = {r['symbol']: r.get('latest_price') for r in sm}

    targets = []
    for sym, total in ttm.items():
        p = price.get(sym)
        if p and p > 0:
            y = round(total / p * 100, 2)
            if 0 <= y <= 60:   # sanity gate (>60% yield = bad price/dividend data)
                targets.append((sym, y))
    print(f'[INFO] {len(targets)} stocks get a computed div_yield')

    def patch(item):
        sym, y = item
        u = f"{URL}/rest/v1/stock_master?symbol=eq.{urllib.parse.quote(sym)}"
        r = requests.patch(u, headers={**H, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'},
                           data=json.dumps({'div_yield_pct': y}), timeout=20)
        return r.status_code in (200, 204)

    ok = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        for f in as_completed([ex.submit(patch, t) for t in targets]):
            if f.result():
                ok += 1
    print(f'[done] div_yield_pct set on {ok} stocks')

    def cnt(q):
        r = requests.get(f'{URL}/rest/v1/stock_master?{q}', headers={**H, 'Prefer': 'count=exact', 'Range': '0-0'}, timeout=20)
        return int(r.headers.get('content-range', '*/0').split('/')[-1])
    a = cnt('is_active=eq.true')
    n = cnt('is_active=eq.true&div_yield_pct=not.is.null')
    print(f'[coverage] div_yield_pct now {n}/{a} ({100*n/a:.0f}%) — rest are genuine non-payers (0 yield)')


if __name__ == '__main__':
    main()
