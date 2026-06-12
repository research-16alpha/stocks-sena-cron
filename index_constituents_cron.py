"""
index_constituents_cron.py  (M7)
================================
Maintains the `index_constituents` table from NSE's official per-index CSVs so
the app stops relying on a HARDCODED NIFTY/BANKNIFTY list (which goes stale
silently when NSE reshuffles indices, ~twice a year).

SOURCE (official, public): nsearchives.nseindia.com/content/indices/<slug>.csv
  columns: Company Name, Industry, Symbol, Series, ISIN Code

Idempotent: upsert on PK (index_name, symbol, as_of_date). Each run stamps the
membership for TODAY — so the table doubles as a point-in-time membership history
(query the latest as_of_date for "current", or any past date for "as of then").

USAGE
  py -3.11 index_constituents_cron.py
  py -3.11 index_constituents_cron.py --dry-run
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
from io import StringIO

import requests

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not KEY:
    try:
        with open('e:/Stocks sena/.supabase-service-key') as f:
            KEY = f.read().strip()
    except FileNotFoundError:
        pass
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

# index display name -> NSE CSV slug
INDICES = {
    'NIFTY 50': 'ind_nifty50list',
    'NIFTY NEXT 50': 'ind_niftynext50list',
    'NIFTY 100': 'ind_nifty100list',
    'NIFTY 200': 'ind_nifty200list',
    'NIFTY 500': 'ind_nifty500list',
    'NIFTY BANK': 'ind_niftybanklist',
    'NIFTY IT': 'ind_niftyitlist',
    'NIFTY FMCG': 'ind_niftyfmcglist',
    'NIFTY AUTO': 'ind_niftyautolist',
    'NIFTY PHARMA': 'ind_niftypharmalist',
    'NIFTY METAL': 'ind_niftymetallist',
    'NIFTY ENERGY': 'ind_niftyenergylist',
    'NIFTY FIN SERVICE': 'ind_niftyfinancelist',
    'NIFTY MIDCAP 100': 'ind_niftymidcap100list',
    'NIFTY SMALLCAP 100': 'ind_niftysmallcap100list',
}


def fetch_index(slug):
    r = requests.get(f'https://nsearchives.nseindia.com/content/indices/{slug}.csv',
                     headers={'User-Agent': UA}, timeout=30)
    r.raise_for_status()
    out = []
    for row in csv.DictReader(StringIO(r.text)):
        sym = (row.get('Symbol') or '').strip()
        if not sym:
            continue
        out.append({
            'symbol': sym,
            'company_name': (row.get('Company Name') or '').strip(),
            'industry': (row.get('Industry') or '').strip(),
            'isin': (row.get('ISIN Code') or '').strip(),
        })
    return out


def upsert(rows):
    r = requests.post(f'{URL}/rest/v1/index_constituents',
                      headers={**H, 'Content-Type': 'application/json',
                               'Prefer': 'resolution=merge-duplicates,return=minimal'},
                      data=json.dumps(rows), timeout=40)
    return r.status_code in (200, 201, 204)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if not KEY:
        print('[ERR] no Supabase key', file=sys.stderr)
        sys.exit(2)

    today = dt.date.today().isoformat()
    total = 0
    failed = 0
    for name, slug in INDICES.items():
        try:
            members = fetch_index(slug)
        except Exception as e:
            print(f'  [WARN] {name}: fetch failed ({e})', file=sys.stderr)
            failed += 1
            continue
        if not members:
            print(f'  [WARN] {name}: 0 members', file=sys.stderr)
            failed += 1
            continue
        rows = [{**m, 'index_name': name, 'as_of_date': today} for m in members]
        print(f'  {name:<22} {len(rows)} members')
        if not args.dry_run and not upsert(rows):
            print(f'  [ERR] {name}: upsert failed', file=sys.stderr)
            failed += 1
            continue
        total += len(rows)

    print(f'\n[done] {total} memberships across {len(INDICES) - failed} indices for {today}'
          f'{" (dry-run)" if args.dry_run else ""}')
    # M7 fail-loud: if MOST indices failed, the NSE source is down — page it.
    if failed > len(INDICES) // 2 and not args.dry_run:
        print(f'[ERR] {failed}/{len(INDICES)} indices failed — source outage', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
