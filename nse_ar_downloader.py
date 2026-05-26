"""
nse_ar_downloader.py
====================
Downloads Annual Report PDFs from NSE for all NSE-listed stocks.

Source: https://www.nseindia.com/api/annual-reports?index=equities&symbol=<SYM>
        Returns list with `fileName` = direct PDF URL on nsearchives.

Output: F:\\expansion\\stocks-sena\\ar_pdfs\\<SYMBOL>\\AR_YYYY-YYYY.pdf
        F:\\expansion\\stocks-sena\\ar_pdfs\\_index.json  (per-stock manifest)

This is the FIRST half of the AR PDF pipeline. PARSER comes next session — for now
we just download so they're locally available when we build the parser.

Storage estimate: ~5 MB/PDF × 16 yrs × ~2000 stocks = ~160 GB on F:.
Run with --limit 100 to test scale before full run.

Resume-capable: skips files already on disk.
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_new_filings import prime_nse_session

OUT_ROOT  = r'F:\expansion\stocks-sena\ar_pdfs'
INDEX     = os.path.join(OUT_ROOT, '_index.json')

NSE_AR_API = 'https://www.nseindia.com/api/annual-reports'
DL_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://www.nseindia.com/companies-listing/corporate-filings-annual-reports',
    'Accept': '*/*',
}

WORKERS = 4
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not KEY:
    try:
        with open('e:/Stocks sena/.supabase-service-key', 'r') as f:
            KEY = f.read().strip()
    except FileNotFoundError:
        pass
URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'} if KEY else {}


def fetch_ar_list(session, sym: str) -> list:
    try:
        r = session.get(NSE_AR_API, params={'index': 'equities', 'symbol': sym}, timeout=15)
        if r.status_code != 200:
            return []
        return (r.json().get('data') or [])
    except Exception:
        return []


def safe_filename(rec: dict) -> str:
    """Build a stable filename: AR_YYYY-YYYY_<source-fname>.pdf."""
    fr = rec.get('fromYr') or 'xxxx'
    to = rec.get('toYr') or 'xxxx'
    orig = rec.get('fileName', '').rsplit('/', 1)[-1]
    return f'AR_{fr}-{to}_{orig}'


def download_one(symbol: str, rec: dict) -> tuple:
    """Returns (symbol, year, status, bytes_or_err)."""
    url = rec.get('fileName')
    if not url or not url.lower().endswith('.pdf'):
        return (symbol, rec.get('fromYr'), 'NO_URL', 0)
    sym_dir = os.path.join(OUT_ROOT, symbol)
    os.makedirs(sym_dir, exist_ok=True)
    out_path = os.path.join(sym_dir, safe_filename(rec))
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
        return (symbol, rec.get('fromYr'), 'EXISTS', os.path.getsize(out_path))
    try:
        r = requests.get(url, headers=DL_HEADERS, timeout=120, stream=True)
        if r.status_code != 200:
            return (symbol, rec.get('fromYr'), f'HTTP_{r.status_code}', 0)
        tmp = out_path + '.tmp'
        with open(tmp, 'wb') as f:
            total = 0
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
                total += len(chunk)
        os.replace(tmp, out_path)
        return (symbol, rec.get('fromYr'), 'OK', total)
    except Exception as e:
        return (symbol, rec.get('fromYr'), f'ERR:{e}', 0)


def fetch_v2_symbols() -> list:
    if not KEY:
        return []
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
        if not b: break
        for o in b:
            n = o.get('name', '')
            if n.endswith('.json'):
                syms.append(n[:-5])
        if len(b) < 1000: break
        offset += 1000
    return [s for s in syms if not s.startswith('BSE_') and not (s.startswith('BSE') and s[3:].isdigit())]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', type=str, default='')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    os.makedirs(OUT_ROOT, exist_ok=True)

    if args.syms:
        syms = [s.strip().upper() for s in args.syms.split(',') if s.strip()]
    else:
        print('[INFO] Listing fundamentals-v2 stocks for AR download...')
        syms = fetch_v2_symbols()
    if args.limit:
        syms = syms[:args.limit]
    print(f'[INFO] Will download AR PDFs for {len(syms)} stocks')

    session = prime_nse_session()

    # Load existing index
    index = {}
    if os.path.exists(INDEX):
        try:
            with open(INDEX, 'r', encoding='utf-8') as f:
                index = json.load(f)
        except Exception:
            pass

    total_ok = total_exists = total_err = total_bytes = 0
    t0 = time.time()

    for i, sym in enumerate(syms, 1):
        ar_list = fetch_ar_list(session, sym)
        if not ar_list:
            index[sym] = {'fetched': 0, 'list_status': 'EMPTY'}
            continue

        # Update index with metadata
        index[sym] = {'list_status': 'OK', 'count': len(ar_list),
                      'records': [(r.get('fromYr'), r.get('toYr')) for r in ar_list]}

        # Concurrent download
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = [ex.submit(download_one, sym, rec) for rec in ar_list]
            for fut in as_completed(futs):
                _, _, status, n = fut.result()
                if status == 'OK':
                    total_ok += 1
                    total_bytes += n
                elif status == 'EXISTS':
                    total_exists += 1
                    total_bytes += n
                else:
                    total_err += 1
        # Pace NSE
        time.sleep(0.5)

        if i % 25 == 0 or i <= 5:
            rate = i / (time.time() - t0)
            eta = (len(syms) - i) / rate if rate > 0 else 0
            print(f'  [{i}/{len(syms)}] new={total_ok} have={total_exists} err={total_err}  '
                  f'total={total_bytes/1e9:.1f} GB  eta={eta/60:.0f} min')

    # Save index
    with open(INDEX, 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=2)

    print()
    print('-' * 60)
    print(f'  Stocks processed   : {len(syms)}')
    print(f'  New PDFs downloaded: {total_ok}')
    print(f'  Already on disk    : {total_exists}')
    print(f'  Errors             : {total_err}')
    print(f'  Total bytes        : {total_bytes/1e9:.2f} GB')
    print(f'  Elapsed            : {time.time()-t0:.1f}s')


if __name__ == '__main__':
    main()
