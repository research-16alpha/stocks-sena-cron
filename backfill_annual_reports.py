"""
backfill_annual_reports.py
==========================
One-shot backfill of NSE annual report metadata for every NSE-listed stock.

Source: https://www.nseindia.com/api/annual-reports?index=equities&symbol=<SYM>
        Returns list with companyName, fromYr, toYr, broadcast_dttm, fileName (PDF URL).

NSE archives ~16 years per stock. We store ONLY metadata + the source URL —
no PDFs are downloaded (legal: link-out approach to public regulatory filings).

Output: upserts into `annual_reports` table in Supabase.

Run:
  python backfill_annual_reports.py              # all stocks in fundamentals-v2
  python backfill_annual_reports.py --syms RELIANCE,TCS
  python backfill_annual_reports.py --limit 50   # test
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_new_filings import prime_nse_session

KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not KEY:
    try:
        with open('e:/Stocks sena/.supabase-service-key', 'r') as f:
            KEY = f.read().strip()
    except FileNotFoundError:
        print('[ERR] SUPABASE_SERVICE_KEY required', file=sys.stderr); sys.exit(1)
URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

NSE_AR_API = 'https://www.nseindia.com/api/annual-reports'
SLEEP_BETWEEN = 0.4  # NSE pacing


def fiscal_year_label(from_yr, to_yr) -> str:
    """Convert 2023, 2024 -> 'FY24' (the higher year = FY closing year)."""
    try:
        end_yr = int(to_yr or from_yr)
        return f'FY{end_yr % 100:02d}'
    except Exception:
        return f'FY{from_yr}-{to_yr}'


def parse_size_kb(raw):
    """NSE returns 'attFileSize' as e.g. '16.62 MB' or '512 KB' or None. Convert to KB int."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip().upper()
    try:
        if 'MB' in s:
            return int(float(s.replace('MB', '').strip()) * 1024)
        if 'KB' in s:
            return int(float(s.replace('KB', '').strip()))
        if 'GB' in s:
            return int(float(s.replace('GB', '').strip()) * 1024 * 1024)
        # Bare number → assume KB
        return int(float(s))
    except Exception:
        return None


def parse_filed_at(raw: str):
    """NSE format: '07-AUG-2025 11:44:57'"""
    if not raw:
        return None
    for fmt in ('%d-%b-%Y %H:%M:%S', '%d-%B-%Y %H:%M:%S', '%d-%b-%Y'):
        try:
            return datetime.strptime(raw, fmt).isoformat()
        except ValueError:
            continue
    return None


def fetch_ar_list(session, symbol: str) -> list:
    try:
        r = session.get(NSE_AR_API, params={'index': 'equities', 'symbol': symbol}, timeout=20)
        if r.status_code != 200:
            return []
        return r.json().get('data') or []
    except Exception as e:
        print(f'  [WARN] {symbol}: {e}', file=sys.stderr)
        return []


def upsert_rows(rows: list) -> bool:
    if not rows:
        return True
    headers = {**H, 'Content-Type': 'application/json',
               'Prefer': 'resolution=merge-duplicates,return=minimal'}
    r = requests.post(
        f'{URL}/rest/v1/annual_reports?on_conflict=symbol,fiscal_year,source',
        headers=headers, data=json.dumps(rows), timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        # Surface first failure for debugging
        print(f'  [UPSERT ERR] {r.status_code}: {r.text[:200]}', file=sys.stderr)
        return False
    return True


def fetch_v2_symbols() -> list:
    syms = []
    offset = 0
    while True:
        r = requests.post(
            f'{URL}/storage/v1/object/list/fundamentals-v2',
            headers={**H, 'Content-Type': 'application/json'},
            json={'prefix': '', 'limit': 1000, 'offset': offset,
                  'sortBy': {'column': 'name', 'order': 'asc'}}, timeout=30,
        )
        b = r.json()
        if not b:
            break
        for o in b:
            n = o.get('name', '')
            if n.endswith('.json'):
                syms.append(n[:-5])
        if len(b) < 1000:
            break
        offset += 1000
    return [s for s in syms if not s.startswith('BSE_') and not (s.startswith('BSE') and s[3:].isdigit())]


def symbols_with_ar() -> set:
    """Symbols that already have at least one annual_reports row (for ordering)."""
    have, offset = set(), 0
    while True:
        try:
            r = requests.get(f'{URL}/rest/v1/annual_reports?select=symbol',
                             headers={**H, 'Range': f'{offset}-{offset + 999}'}, timeout=30)
            data = r.json() if r.status_code in (200, 206) else []
        except Exception:
            break
        if not data:
            break
        for row in data:
            if row.get('symbol'):
                have.add(row['symbol'])
        if len(data) < 1000:
            break
        offset += 1000
    return have


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', type=str, default='')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    if args.syms:
        syms = [s.strip().upper() for s in args.syms.split(',') if s.strip()]
    else:
        print('[INFO] Listing fundamentals-v2 stocks...')
        syms = fetch_v2_symbols()
        # Stocks with NO annual_reports yet go first, so a time-boxed run grows
        # coverage instead of re-walking the alphabetical head every run.
        have = symbols_with_ar()
        syms.sort(key=lambda s: s in have)
    if args.limit:
        syms = syms[:args.limit]
    budget = int(os.environ.get('PROCESS_BUDGET_MIN', '0')) * 60
    print(f'[INFO] Backfill ARs for {len(syms)} stocks'
          + (f' (budget {budget // 60}m)' if budget else ''))

    session = prime_nse_session()
    total_rows = 0
    no_data = err = 0
    t0 = time.time()

    for i, sym in enumerate(syms, 1):
        if budget and time.time() - t0 > budget:
            print(f'[INFO] budget reached at {i}/{len(syms)} — stopping; resumes next run')
            break
        ar_list = fetch_ar_list(session, sym)
        if not ar_list:
            no_data += 1
            continue
        # Dedupe by (symbol, fiscal_year, source) keeping the most recent broadcast_dttm
        by_pk = {}
        for rec in ar_list:
            url = rec.get('fileName')
            if not url:
                continue
            fy = fiscal_year_label(rec.get('fromYr'), rec.get('toYr'))
            filed = parse_filed_at(rec.get('broadcast_dttm') or rec.get('disseminationDateTime'))
            pk = (sym, fy, 'NSE')
            row = {
                'symbol': sym, 'fiscal_year': fy,
                'from_yr': int(rec['fromYr']) if rec.get('fromYr') else None,
                'to_yr':   int(rec['toYr']) if rec.get('toYr') else None,
                'filed_at': filed,
                'source': 'NSE', 'source_url': url,
                'file_size_kb': parse_size_kb(rec.get('attFileSize')),
                'title': f'Annual Report {fy}',
            }
            # Keep the row with the latest filed_at for this PK
            if pk not in by_pk or (filed or '') > (by_pk[pk].get('filed_at') or ''):
                by_pk[pk] = row
        rows = list(by_pk.values())

        if rows:
            if upsert_rows(rows):
                total_rows += len(rows)
            else:
                err += 1
        time.sleep(SLEEP_BETWEEN)

        if i % 50 == 0 or i <= 5:
            rate = i / (time.time() - t0)
            eta = (len(syms) - i) / rate if rate > 0 else 0
            print(f'  [{i}/{len(syms)}] rows={total_rows} no_data={no_data} err={err} '
                  f'rate={rate:.1f}/s eta={eta/60:.1f}m')

    print()
    print('-' * 60)
    print(f'  Stocks processed: {len(syms)}')
    print(f'  Total AR rows   : {total_rows}')
    print(f'  No NSE data     : {no_data}')
    print(f'  Errors          : {err}')
    print(f'  Elapsed         : {time.time()-t0:.1f}s')


if __name__ == '__main__':
    main()
