"""
find_unmatched_stocks.py
========================
Diagnostic — identify which stock_master symbols have NO data in:
  • fundamentals-v2 bucket (missing XBRL bundle)
  • daily bucket (missing OHLCV chart)

Output: list of stocks needing symbol-alias mapping.
"""
import urllib.request
import json
import os

with open(r'e:\Stocks sena\.supabase-service-key', 'r') as f:
    KEY = f.read().strip()
URL = 'https://tbeadvvkqyrhtendttrg.supabase.co'
HEADERS = {'apikey': KEY, 'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json'}


def fetch_master():
    syms = {}
    offset = 0
    h = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}
    while True:
        url = f'{URL}/rest/v1/stock_master?select=symbol,name,exchange,isin&limit=1000&offset={offset}'
        r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=20)
        batch = json.loads(r.read())
        if not batch: break
        for s in batch:
            syms[s['symbol']] = s
        if len(batch) < 1000: break
        offset += 1000
    return syms


def fetch_bucket_files(bucket):
    out = set()
    offset = 0
    while True:
        req = urllib.request.Request(
            f'{URL}/storage/v1/object/list/{bucket}',
            data=json.dumps({'prefix': '', 'limit': 1000, 'offset': offset}).encode(),
            headers=HEADERS, method='POST',
        )
        r = urllib.request.urlopen(req, timeout=30)
        batch = json.loads(r.read())
        if not batch: break
        for f in batch:
            if f['name'].endswith('.json'):
                out.add(f['name'][:-5])
        if len(batch) < 1000: break
        offset += 1000
    return out


def main():
    print('Loading stock_master...')
    master = fetch_master()
    print(f'  {len(master)} stocks in app')

    print('Loading fundamentals-v2 bucket...')
    fund = fetch_bucket_files('fundamentals-v2')
    print(f'  {len(fund)} bundles')

    print('Loading daily bucket...')
    daily = fetch_bucket_files('daily')
    print(f'  {len(daily)} OHLCV files')

    no_fund = sorted([s for s in master if s not in fund])
    no_ohlcv = sorted([s for s in master if s not in daily])
    no_both = sorted([s for s in no_fund if s in set(no_ohlcv)])

    print()
    print(f'Missing fundamentals: {len(no_fund)}')
    print(f'Missing OHLCV      : {len(no_ohlcv)}')
    print(f'Missing both       : {len(no_both)}')

    # Dump to JSON for the alias-fix script
    out = {
        'missing_fundamentals': [{'symbol': s, 'name': master[s].get('name'), 'isin': master[s].get('isin')} for s in no_fund],
        'missing_ohlcv':        [{'symbol': s, 'name': master[s].get('name'), 'isin': master[s].get('isin')} for s in no_ohlcv],
        'missing_both':         [{'symbol': s, 'name': master[s].get('name'), 'isin': master[s].get('isin')} for s in no_both],
    }
    out_path = r'F:\expansion\stocks-sena\unmatched_stocks.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2)
    print(f'\nWritten to {out_path}')

    print()
    print('Sample missing fundamentals (first 30):')
    for s in no_fund[:30]:
        print(f'  {s:<14} {master[s].get("name","")[:50]:50s} isin={master[s].get("isin")}')


if __name__ == '__main__':
    main()
