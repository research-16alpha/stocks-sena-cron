"""
fix_mcap_pe_coverage.py
=======================
For every active stock with latest_price but missing market_cap_cr or pe_ratio,
re-compute via multiple fallback formulas using bundle data.

Mcap = price × shares_outstanding
  shares from (in priority):
    1. annual_bs.equity_capital / snapshot.face_value (current method)
    2. annual_pl.net_profit / annual_pl.basic_eps   (works when EPS+NP both present)
    3. snapshot.market_cap_cr / snapshot.current_price (if existing snapshot mcap)
    4. (Sprint 5) Kite instrument data

PE = price / eps_ttm
  eps_ttm from (in priority):
    1. sum of last 4 quarterly EPS
    2. latest annual basic_eps
    3. annual eps if > 0
"""
import os
import sys
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SUPABASE_URL = 'https://tbeadvvkqyrhtendttrg.supabase.co'
with open(r'e:\Stocks sena\.supabase-service-key') as f:
    KEY = f.read().strip()
HEADERS = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}


def fetch_master():
    syms = []
    offset = 0
    while True:
        url = f'{SUPABASE_URL}/rest/v1/stock_master?select=symbol,latest_price,market_cap_cr,pe_ratio,is_active&is_active=eq.true&limit=1000&offset={offset}'
        r = urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=20)
        batch = json.loads(r.read())
        if not batch: break
        syms.extend(batch)
        if len(batch) < 1000: break
        offset += 1000
    return syms


def fetch_bundle(sym):
    try:
        r = urllib.request.urlopen(
            f'{SUPABASE_URL}/storage/v1/object/public/fundamentals-v2/{sym}.json',
            timeout=10,
        )
        return json.loads(r.read())
    except Exception:
        return None


def compute_shares_outstanding(bundle) -> float | None:
    """Returns shares outstanding in CRORE (millions of 10), or None."""
    if not bundle: return None
    snap = (bundle.get('snapshot') or [{}])[0]

    # Method 1: equity_capital / face_value (most reliable when both present)
    bs = bundle.get('annual_bs') or bundle.get('annual_bs_consolidated') or bundle.get('annual_bs_standalone') or []
    if bs:
        latest_bs = bs[-1] or {}
        eq_cap = latest_bs.get('equity_capital')  # in cr
        face_val = snap.get('face_value') or 10.0
        if eq_cap and eq_cap > 0 and face_val and face_val > 0:
            return eq_cap / face_val  # shares in cr

    # Method 2: derive from net_profit / basic_eps in latest annual
    pl = bundle.get('annual_pl') or bundle.get('annual_pl_consolidated') or bundle.get('annual_pl_standalone') or []
    if pl:
        latest = pl[-1] or {}
        np = latest.get('net_profit')  # in cr
        eps = latest.get('basic_eps') or latest.get('eps')  # in rupees per share
        if np and eps and eps != 0:
            # shares (count, in cr) = (np_cr × 1e7) / (eps_rupees × 1e7 per crore-of-shares)
            # = np_cr / eps
            shares_cr = abs(np / eps)
            if shares_cr > 0:
                return shares_cr

    # Method 3: existing snapshot mcap reversed (when bundle was previously computed)
    if snap.get('market_cap_cr') and snap.get('current_price') and snap['current_price'] > 0:
        return snap['market_cap_cr'] / snap['current_price']

    return None


def compute_eps_ttm(bundle) -> float | None:
    """Returns EPS TTM (rupees), or None if loss-making / insufficient data."""
    if not bundle: return None
    qrs = (bundle.get('quarterly_results') or bundle.get('quarterly_results_consolidated') or bundle.get('quarterly_results_standalone') or [])[-4:]
    if len(qrs) >= 4:
        eps_ttm = sum((r.get('eps') or r.get('basic_eps') or 0) for r in qrs)
        if eps_ttm and eps_ttm > 0:
            return eps_ttm
    # Fallback to latest annual EPS
    pl = bundle.get('annual_pl') or bundle.get('annual_pl_consolidated') or bundle.get('annual_pl_standalone') or []
    if pl:
        latest = pl[-1] or {}
        eps = latest.get('eps') or latest.get('basic_eps')
        if eps and eps > 0:
            return eps
    return None


def patch_row(sym, body):
    if not body: return 0
    url = f'{SUPABASE_URL}/rest/v1/stock_master?symbol=eq.{sym}'
    h = {**HEADERS, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method='PATCH')
        r = urllib.request.urlopen(req, timeout=15)
        return 1 if r.status in (200, 204) else 0
    except Exception:
        return 0


def process_one(s):
    sym = s['symbol']
    price = s.get('latest_price')
    if not price or price <= 0:
        return ('NO_PRICE', sym, {})

    bundle = fetch_bundle(sym)
    if not bundle:
        return ('NO_BUNDLE', sym, {})

    body = {}
    if not s.get('market_cap_cr'):
        shares_cr = compute_shares_outstanding(bundle)
        if shares_cr and shares_cr > 0:
            body['market_cap_cr'] = round(price * shares_cr, 2)
    if not s.get('pe_ratio'):
        eps_ttm = compute_eps_ttm(bundle)
        if eps_ttm and eps_ttm > 0:
            body['pe_ratio'] = round(price / eps_ttm, 2)

    if not body:
        return ('NO_NEW_DATA', sym, {})
    if patch_row(sym, body):
        return ('OK', sym, body)
    return ('PATCH_ERR', sym, body)


def main():
    print('[load] active stocks from stock_master...')
    master = fetch_master()
    print(f'  {len(master)} active stocks')

    needs_mcap = [s for s in master if not s.get('market_cap_cr') and s.get('latest_price')]
    needs_pe = [s for s in master if not s.get('pe_ratio') and s.get('latest_price')]
    target = [s for s in master if (not s.get('market_cap_cr') or not s.get('pe_ratio')) and s.get('latest_price')]
    print(f'  Need mcap: {len(needs_mcap)}')
    print(f'  Need PE  : {len(needs_pe)}')
    print(f'  Total to process: {len(target)}')

    t0 = time.time()
    counts = {'OK': 0, 'NO_PRICE': 0, 'NO_BUNDLE': 0, 'NO_NEW_DATA': 0, 'PATCH_ERR': 0}
    mcap_gained = pe_gained = 0

    with ThreadPoolExecutor(max_workers=20) as ex:
        for i, (status, sym, body) in enumerate(ex.map(process_one, target), 1):
            counts[status] = counts.get(status, 0) + 1
            if 'market_cap_cr' in body: mcap_gained += 1
            if 'pe_ratio' in body: pe_gained += 1
            if i % 500 == 0:
                elapsed = time.time() - t0
                rate = i / elapsed
                eta = (len(target) - i) / rate
                print(f'  {i}/{len(target)}  ok={counts["OK"]}  +mcap={mcap_gained}  +pe={pe_gained}  ETA={eta:.0f}s')

    print()
    print(f'[done] {time.time()-t0:.0f}s')
    for k, v in counts.items():
        print(f'  {k}: {v}')
    print(f'  Market cap filled: {mcap_gained}')
    print(f'  PE filled        : {pe_gained}')


if __name__ == '__main__':
    main()
