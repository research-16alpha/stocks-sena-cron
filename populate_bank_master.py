"""
populate_bank_master.py
=======================
For every bundle in fundamentals-v2, if it has a _bank=true quarterly row,
update stock_master with the latest bank metrics so the screener can filter
on NPA / CET1 / RoA without loading bundles.

Runs idempotently. Designed for daily cron after nse_delta_quarterly.
"""
import os
import sys
import time
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not KEY:
    try:
        with open('e:/Stocks sena/.supabase-service-key', 'r') as f:
            KEY = f.read().strip()
    except FileNotFoundError:
        print('[ERR] SUPABASE_SERVICE_KEY required', file=sys.stderr)
        sys.exit(1)
URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}
BUCKET = 'fundamentals-v2'
WORKERS = 8


def list_v2() -> list:
    syms = []
    offset = 0
    while True:
        r = requests.post(f'{URL}/storage/v1/object/list/{BUCKET}',
            headers={**H, 'Content-Type': 'application/json'},
            json={'prefix': '', 'limit': 1000, 'offset': offset,
                  'sortBy': {'column': 'name', 'order': 'asc'}}, timeout=30)
        b = r.json()
        if not b: break
        for o in b:
            n = o.get('name', '')
            if n.endswith('.json'):
                syms.append(n[:-5])
        if len(b) < 1000: break
        offset += 1000
    return [s for s in syms if not s.startswith('BSE_') and not (s.startswith('BSE') and s[3:].isdigit())]


def fetch_bundle(sym: str) -> dict:
    try:
        r = requests.get(f'{URL}/storage/v1/object/public/{BUCKET}/{sym}.json', timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def extract_bank_metrics(bundle: dict) -> dict:
    """Returns dict with bank fields or {} if not a bank."""
    qrs = (bundle.get('quarterly_results') or [])
    bank_rows = [q for q in qrs if q.get('_bank')]
    if not bank_rows:
        return {}
    bank_rows.sort(key=lambda r: r.get('period') or '')
    latest = bank_rows[-1]
    return {
        'is_bank': True,
        'gross_npa_pct': latest.get('gross_npa_pct'),
        'net_npa_pct': latest.get('net_npa_pct'),
        'cet1_ratio': latest.get('cet1_ratio'),
        'capital_adequacy_ratio': latest.get('capital_adequacy_ratio'),
        'return_on_assets': latest.get('return_on_assets'),
        'interest_income_cr': latest.get('interest_income') or latest.get('sales'),
        'bank_data_updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }


def extract_quality_metrics(bundle: dict) -> dict:
    """Counts of periods + latest dates so UI can show 'data quality' badges."""
    pl = bundle.get('annual_pl') or []
    bs = bundle.get('annual_bs') or []
    cf = bundle.get('annual_cf') or []
    qr = bundle.get('quarterly_results') or []
    return {
        'pl_years_count': len(pl),
        'bs_years_count': len(bs),
        'cf_years_count': len(cf),
        'quarters_count': len(qr),
        'latest_annual_period': (pl[-1].get('period') if pl else None),
        'latest_quarter_period': (qr[-1].get('period') if qr else None),
        'quality_updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }


def update_master(sym: str, fields: dict) -> bool:
    """PATCH stock_master row for sym."""
    headers = {**H, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}
    r = requests.patch(f'{URL}/rest/v1/stock_master?symbol=eq.{sym}',
                       headers=headers, data=json.dumps(fields), timeout=20)
    return r.status_code in (200, 204)


def process(sym: str) -> tuple:
    b = fetch_bundle(sym)
    if not b:
        return (sym, 'NO_BUNDLE')
    # Always populate quality metrics
    fields = extract_quality_metrics(b)
    # Banks also get bank metrics
    bank = extract_bank_metrics(b)
    fields.update(bank)
    ok = update_master(sym, fields)
    return (sym, ('OK_BANK' if bank else 'OK') if ok else 'UPDATE_FAIL')


def main():
    print('[INFO] Listing v2 bundles...')
    syms = list_v2()
    print(f'[INFO] {len(syms)} stocks to scan')

    ok_total = ok_bank = nb = err = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(process, s): s for s in syms}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                _, status = fut.result()
            except Exception:
                err += 1; continue
            if status == 'OK': ok_total += 1
            elif status == 'OK_BANK': ok_total += 1; ok_bank += 1
            elif status == 'NO_BUNDLE': nb += 1
            else: err += 1
            if i % 500 == 0:
                print(f'  [{i}/{len(syms)}] updated={ok_total} (banks={ok_bank})')

    print()
    print('-' * 60)
    print(f'  Total updated   : {ok_total}')
    print(f'  Banks tagged    : {ok_bank}')
    print(f'  No bundle       : {nb}')
    print(f'  Errors          : {err}')
    print(f'  Elapsed         : {time.time()-t0:.1f}s')


if __name__ == '__main__':
    main()
