"""
backfill_concall_transcripts.py
================================
One-shot backfill of concall transcript metadata for every NSE-listed stock.

Sources:
  - BSE Announcements API filtered by "transcript" keyword (last 5 years)
  - NSE corp announcements filtered by "transcript"

We store ONLY metadata + the source PDF URL. No PDFs are downloaded
(link-out approach — public regulatory filings, like Google indexing).

Quarter inference from filing subject + filing date:
  - If subject mentions Q1/Q2/Q3/Q4 → use that
  - Else infer from period: Jun=Q1, Sep=Q2, Dec=Q3, Mar=Q4
  - Combined with fiscal year (Apr-Mar) → e.g. "Q3FY25"

Run:
  python backfill_concall_transcripts.py            # all stocks
  python backfill_concall_transcripts.py --syms RELIANCE,TCS
  python backfill_concall_transcripts.py --limit 50
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
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

BSE_ANN_API = 'https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w'
BSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Origin': 'https://www.bseindia.com',
    'Referer': 'https://www.bseindia.com/corporates/ann.html',
}

# Concall keywords (case-insensitive)
KEYWORDS = ('transcript', 'earnings call', 'conference call', 'analyst call', 'investor call')


def is_transcript(subject: str) -> bool:
    s = (subject or '').lower()
    return any(k in s for k in KEYWORDS)


def fiscal_quarter(d: datetime) -> str:
    """Indian FY (Apr–Mar): Jun=Q1, Sep=Q2, Dec=Q3, Mar=Q4."""
    if d is None:
        return ''
    m = d.month
    if m in (4, 5, 6): q, fy_end = 'Q1', d.year + 1 if m < 4 else d.year + 1
    elif m in (7, 8, 9): q, fy_end = 'Q2', d.year + 1 if m < 4 else d.year + 1
    elif m in (10, 11, 12): q, fy_end = 'Q3', d.year + 1
    else: q, fy_end = 'Q4', d.year
    return f'{q}FY{fy_end % 100:02d}'


def infer_quarter_from_subject(subject: str, filed_at: datetime) -> str:
    """Try subject text first, then filing-date inference."""
    s = (subject or '').upper()
    m = re.search(r'Q([1-4])\s*FY?\s*(\d{2,4})', s)
    if m:
        q, fy = m.group(1), m.group(2)
        fy_short = int(fy) % 100
        return f'Q{q}FY{fy_short:02d}'
    # Filing date inference: results filed within ~30-90d of period end
    if filed_at:
        # Try the most recent quarter ending before this filing
        d = filed_at - timedelta(days=45)  # rough
        return fiscal_quarter(d)
    return ''


def fetch_bse_window(from_date: str, to_date: str, max_pages: int = 200) -> list:
    """Pull ALL BSE announcements in a date window. Filter client-side later.
    BSE per-scrip filter doesn't work reliably so we scan globally.
    Pages are 50 items each."""
    out = []
    page = 1
    consecutive_empty = 0
    while page <= max_pages:
        try:
            r = requests.get(BSE_ANN_API, headers=BSE_HEADERS, timeout=20, params={
                'pageno': page, 'strCat': '-1', 'strPrevDate': from_date,
                'strScrip': '', 'strSearch': 'P', 'strToDate': to_date,
                'strType': 'C',
            })
            if r.status_code != 200:
                break
            items = (r.json() or {}).get('Table') or []
            if not items:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
                page += 1; continue
            out.extend(items)
            consecutive_empty = 0
            if len(items) < 50:
                break
            page += 1
            time.sleep(0.25)
        except Exception as e:
            print(f'  [BSE ERR] page={page}: {e}', file=sys.stderr)
            break
    return out


def parse_bse_dt(raw: str):
    if not raw:
        return None
    try:
        return datetime.strptime(raw.split('.')[0], '%Y-%m-%dT%H:%M:%S')
    except Exception:
        return None


def upsert_rows(rows: list) -> bool:
    if not rows:
        return True
    headers = {**H, 'Content-Type': 'application/json',
               'Prefer': 'resolution=merge-duplicates,return=minimal'}
    r = requests.post(
        f'{URL}/rest/v1/concall_transcripts?on_conflict=symbol,quarter,source',
        headers=headers, data=json.dumps(rows), timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        print(f'  [UPSERT ERR] {r.status_code}: {r.text[:200]}', file=sys.stderr)
        return False
    return True


def fetch_stock_master_with_scrip() -> dict:
    """Returns {NSE_SYMBOL: bse_scrip_code} for stocks that have a scrip code."""
    out = {}
    offset = 0
    while True:
        r = requests.get(
            f'{URL}/rest/v1/stock_master?select=symbol,bse_scrip_code&bse_scrip_code=not.is.null',
            headers={**H, 'Range': f'{offset}-{offset+999}'}, timeout=30,
        )
        batch = r.json()
        if not batch:
            break
        for row in batch:
            if row.get('bse_scrip_code'):
                out[row['symbol']] = str(row['bse_scrip_code'])
        if len(batch) < 1000:
            break
        offset += 1000
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', type=int, default=5, help='How many years back')
    ap.add_argument('--from-date', type=str, default='', help='YYYYMMDD override')
    ap.add_argument('--to-date', type=str, default='', help='YYYYMMDD override')
    args = ap.parse_args()

    print('[INFO] Loading scrip -> symbol mapping from stock_master...')
    scrip_to_symbol = {v: k for k, v in fetch_stock_master_with_scrip().items()}
    print(f'[INFO]   {len(scrip_to_symbol)} BSE scrips mapped to NSE symbols')

    today = args.to_date or datetime.now().strftime('%Y%m%d')
    from_dt = args.from_date or (datetime.now() - timedelta(days=365 * args.years)).strftime('%Y%m%d')
    print(f'[INFO] Scanning BSE announcements {from_dt} -> {today}')

    # Walk in 3-month chunks to keep pages manageable (BSE caps total results)
    chunk_days = 90
    cur_to = datetime.strptime(today, '%Y%m%d')
    cur_from = datetime.strptime(from_dt, '%Y%m%d')
    chunks = []
    while cur_to > cur_from:
        next_from = max(cur_to - timedelta(days=chunk_days), cur_from)
        chunks.append((next_from.strftime('%Y%m%d'), cur_to.strftime('%Y%m%d')))
        cur_to = next_from - timedelta(days=1)
    chunks = list(reversed(chunks))
    print(f'[INFO] {len(chunks)} chunks (90d each)')

    total_anns = 0
    total_tx = 0
    total_rows = 0
    no_sym = no_attach = 0
    t0 = time.time()

    for ci, (cf, ct) in enumerate(chunks, 1):
        anns = fetch_bse_window(cf, ct)
        total_anns += len(anns)
        tx = [a for a in anns if is_transcript(a.get('NEWSSUB') or a.get('HEADLINE') or '')]
        total_tx += len(tx)

        # Build rows + upsert per chunk
        seen_pk = set()
        rows = []
        for ann in tx:
            scrip = str(ann.get('SCRIP_CD') or '')
            sym = scrip_to_symbol.get(scrip)
            if not sym:
                no_sym += 1; continue
            attachment = ann.get('ATTACHMENTNAME')
            if not attachment:
                no_attach += 1; continue
            filed_at = parse_bse_dt(ann.get('NEWS_DT') or ann.get('News_submission_dt'))
            quarter = infer_quarter_from_subject(
                ann.get('NEWSSUB') or ann.get('HEADLINE') or '', filed_at)
            if not quarter:
                continue
            pk = (sym, quarter, 'BSE')
            if pk in seen_pk:
                continue
            seen_pk.add(pk)
            rows.append({
                'symbol': sym,
                'quarter': quarter,
                'period_end': None,
                'filed_at': filed_at.isoformat() if filed_at else None,
                'source': 'BSE',
                'source_url': f'https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}',
                'file_size_kb': None,
                'title': (ann.get('NEWSSUB') or '')[:200],
            })

        if rows and upsert_rows(rows):
            total_rows += len(rows)

        elapsed = time.time() - t0
        print(f'  [{ci}/{len(chunks)}] {cf}-{ct}  anns={len(anns)} tx={len(tx)} '
              f'new_rows={len(rows)}  cumulative_rows={total_rows}  elapsed={elapsed:.0f}s')

    print()
    print('-' * 60)
    print(f'  Chunks scanned      : {len(chunks)}')
    print(f'  Total announcements : {total_anns}')
    print(f'  Transcript hits     : {total_tx}')
    print(f'  Upserted rows       : {total_rows}')
    print(f'  No symbol mapping   : {no_sym}')
    print(f'  No attachment       : {no_attach}')
    print(f'  Elapsed             : {time.time()-t0:.1f}s')


if __name__ == '__main__':
    main()
