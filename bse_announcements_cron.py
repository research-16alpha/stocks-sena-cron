"""
bse_announcements_cron.py
=========================
Per-ticker corporate announcements / filings from BSE — the "Filings" feed that
gives EVERY listed stock news, including the ~2,100 microcaps that national RSS
media never covers. Primary source: BSE AnnGetData API.

Each filing already carries readable TEXT (subject + a 1-2 sentence HEADLINE),
so NO PDF parsing is needed for the feed — we store that text + link the PDF.
(AI summarisation of the PDF body is a later, paid layer.)

Writes -> public.corporate_announcements  (dedup on BSE NEWSID).
Also routes transcript-category filings -> concall_transcripts (concall for all).

Modes:
  python bse_announcements_cron.py                      # daily: global scan last 3 days
  python bse_announcements_cron.py --days 7             # global scan last 7 days
  python bse_announcements_cron.py --backfill --months 12   # per-scrip history (slow, complete)
  python bse_announcements_cron.py --backfill --syms RELIANCE,TCS --months 24
  python bse_announcements_cron.py --dry-run            # parse + report, write nothing
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
import requests

KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not KEY:
    with open('e:/Stocks sena/.supabase-service-key', 'r') as f:
        KEY = f.read().strip()
URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

BSE_ANN_API = 'https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w'
# AttachHis (historical) serves BOTH recent and old attachments; AttachLive only
# serves the last ~week, so it 404s ("page has been moved") for everything older.
BSE_ATTACH = 'https://www.bseindia.com/xml-data/corpfiling/AttachHis/'
BSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Origin': 'https://www.bseindia.com',
    'Referer': 'https://www.bseindia.com/corporates/ann.html',
}

# A real concall TRANSCRIPT filing says "transcript". The broad 'conference call' /
# 'earnings call' / 'investor call' keywords also match pre-call INTIMATIONS and
# audio-link notices (72% of hits) — those are NOT transcripts, so we exclude them.
CONCALL_EXCLUDE = ('intimation', 'schedule of', 'notice of', 'will be held',
                   'audio recording', 'audio clip', 'recording of', 'weblink',
                   'web link', 'link for', 'link of', 'newspaper')

# Subject keywords that mark a "Company Update" as ROUTINE boilerplate (not material
# news). Everything else in a meaningful category is treated as material.
NOISE_KEYWORDS = (
    'newspaper publication', 'newspaper clipping', 'trading window', 'duplicate',
    'loss of share', 'sub-division of share certificate', 'transfer/transmission',
    'compliance certificate', 'reg. 7', 'reg.7', 'regulation 7 ', 'reg. 74', 'reg.74',
    'regulation 74', 'reg. 40', 'investor complaint', 'reconciliation of share capital',
    'secretarial compliance', 'reg. 24', 'reg.24', 'closure of trading window',
    'change in registrar', 'forfeiture', 'postal ballot', 'record date',
)
# Categories that are inherently material regardless of subject text.
MATERIAL_CATEGORIES = {
    'Result', 'Board Meeting', 'Corp. Action', 'Company Update', 'Insider Trading / SAST',
    'AGM/EGM', 'New Listing', 'Integrated Filing', 'Credit Rating', 'Allotment',
}


def is_concall(text: str) -> bool:
    s = (text or '').lower()
    if 'transcript' not in s:          # must actually be a transcript filing
        return False
    return not any(k in s for k in CONCALL_EXCLUDE)


def is_material(category: str, subject: str) -> bool:
    cat = (category or '').strip()
    s = (subject or '').lower()
    if any(k in s for k in NOISE_KEYWORDS):
        return False
    if cat in MATERIAL_CATEGORIES:
        return True
    return False


def parse_dt(raw):
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).split('.')[0], '%Y-%m-%dT%H:%M:%S')
    except Exception:
        return None


def clean_subject(newssub: str, scrip: str, company: str) -> str:
    """BSE NEWSSUB is often 'Company - 500325 - <real subject>'. Strip the prefix."""
    s = (newssub or '').strip()
    parts = [p.strip() for p in s.split(' - ')]
    # drop leading parts that are the company name or the scrip code
    while parts and (parts[0] == scrip or (company and parts[0].lower() == company.lower())
                     or parts[0].isdigit()):
        parts.pop(0)
    return ' - '.join(parts) if parts else s


def fetch_scrip_map() -> dict:
    """{scrip_code(str): symbol}"""
    out, offset = {}, 0
    while True:
        r = requests.get(
            f'{URL}/rest/v1/stock_master?select=symbol,bse_scrip_code&bse_scrip_code=not.is.null',
            headers={**H, 'Range': f'{offset}-{offset+999}'}, timeout=30)
        batch = r.json()
        if not batch:
            break
        for row in batch:
            if row.get('bse_scrip_code'):
                out[str(row['bse_scrip_code'])] = row['symbol']
        if len(batch) < 1000:
            break
        offset += 1000
    return out


def fetch_window(from_d: str, to_d: str, scrip: str = '', max_pages: int = 400) -> list:
    out, page, empty = [], 1, 0
    while page <= max_pages:
        try:
            r = requests.get(BSE_ANN_API, headers=BSE_HEADERS, timeout=20, params={
                'pageno': page, 'strCat': '-1', 'strPrevDate': from_d, 'strScrip': scrip,
                'strSearch': 'P', 'strToDate': to_d, 'strType': 'C'})
            if r.status_code != 200:
                break
            items = (r.json() or {}).get('Table') or []
            if not items:
                empty += 1
                if empty >= 2:
                    break
                page += 1
                continue
            out.extend(items)
            empty = 0
            if len(items) < 50:
                break
            page += 1
            time.sleep(0.2)
        except Exception as e:
            print(f'  [BSE ERR] {scrip or "global"} page={page}: {e}', file=sys.stderr)
            break
    return out


def to_row(ann: dict, scrip_to_symbol: dict):
    scrip = str(ann.get('SCRIP_CD') or '')
    sym = scrip_to_symbol.get(scrip)
    if not sym:
        return None, None
    news_id = ann.get('NEWSID')
    if not news_id:
        return None, None
    company = ann.get('SLONGNAME') or ''
    subject = clean_subject(ann.get('NEWSSUB') or '', scrip, company)
    category = ann.get('CATEGORYNAME') or ''
    detail = (ann.get('HEADLINE') or '').strip()
    filed_at = parse_dt(ann.get('NEWS_DT') or ann.get('DT_TM'))
    attach = ann.get('ATTACHMENTNAME')
    row = {
        'news_id': news_id,
        'symbol': sym,
        'scrip_code': scrip,
        'category': category,
        'headline': subject[:300],
        'detail': detail[:1000],
        'pdf_url': (BSE_ATTACH + attach) if attach else None,
        'filed_at': filed_at.isoformat() if filed_at else None,
        'critical': bool(ann.get('CRITICALNEWS')),
        'material': is_material(category, subject + ' ' + detail),
        'company_name': company,
    }
    concall = None
    if is_concall(subject + ' ' + detail):
        concall = sym  # flag for concall routing (handled by caller)
    return row, concall


def upsert(table: str, rows: list, conflict: str) -> int:
    if not rows:
        return 0
    headers = {**H, 'Content-Type': 'application/json',
               'Prefer': 'resolution=merge-duplicates,return=minimal'}
    n = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        r = requests.post(f'{URL}/rest/v1/{table}?on_conflict={conflict}',
                          headers=headers, data=json.dumps(chunk), timeout=40)
        if r.status_code in (200, 201, 204):
            n += len(chunk)
        else:
            print(f'  [UPSERT ERR {table}] {r.status_code}: {r.text[:160]}', file=sys.stderr)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=3, help='daily mode: global scan last N days')
    ap.add_argument('--backfill', action='store_true', help='per-scrip history mode')
    ap.add_argument('--months', type=int, default=12, help='backfill depth')
    ap.add_argument('--syms', type=str, default='', help='backfill: comma-list of symbols')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    print('[INFO] loading scrip -> symbol map...')
    scrip_to_symbol = fetch_scrip_map()
    print(f'[INFO]   {len(scrip_to_symbol)} BSE scrips mapped')

    today = datetime.now()
    rows, concall_syms_pks = {}, []
    stats = {'anns': 0, 'mapped': 0, 'material': 0, 'concall': 0}

    def ingest(anns):
        for a in anns:
            stats['anns'] += 1
            row, concall = to_row(a, scrip_to_symbol)
            if not row:
                continue
            stats['mapped'] += 1
            if row['material']:
                stats['material'] += 1
            rows[row['news_id']] = row  # dedup by news_id
            if concall:
                stats['concall'] += 1

    if args.backfill:
        from_d = (today - timedelta(days=30 * args.months)).strftime('%Y%m%d')
        to_d = today.strftime('%Y%m%d')
        if args.syms:
            targets = [s.strip().upper() for s in args.syms.split(',') if s.strip()]
            sym_to_scrip = {v: k for k, v in scrip_to_symbol.items()}
            scrips = [(sym_to_scrip[s], s) for s in targets if s in sym_to_scrip]
        else:
            scrips = [(sc, sy) for sc, sy in scrip_to_symbol.items()]
        print(f'[INFO] BACKFILL per-scrip: {len(scrips)} stocks · {from_d}->{to_d}')
        # Incremental commit: flush every ~1500 rows so a mid-run failure can't lose
        # everything (the all-at-end design killed the first 12-month backfill).
        done = {'ann': 0, 'cc': 0}

        def commit():
            if not rows:
                return
            batch = list(rows.values())
            if args.dry_run:
                done['ann'] += len(batch)
                rows.clear()
                return
            done['ann'] += upsert('corporate_announcements', batch, 'news_id')
            cmap = {}
            for r in batch:
                if is_concall(r['headline'] + ' ' + r['detail']):
                    fa = parse_dt(r['filed_at'])
                    if not fa:
                        continue
                    d = fa - timedelta(days=45)
                    m = d.month
                    qn = 'Q1' if m in (4, 5, 6) else 'Q2' if m in (7, 8, 9) else 'Q3' if m in (10, 11, 12) else 'Q4'
                    q = f"{qn}FY{(d.year + 1 if m >= 4 else d.year) % 100:02d}"
                    cmap[(r['symbol'], q, 'BSE')] = {
                        'symbol': r['symbol'], 'quarter': q, 'period_end': None,
                        'filed_at': r['filed_at'], 'source': 'BSE', 'source_url': r['pdf_url'],
                        'file_size_kb': None, 'title': r['headline'][:200]}
            if cmap:
                done['cc'] += upsert('concall_transcripts', list(cmap.values()), 'symbol,quarter,source')
            rows.clear()

        for i, (scrip, sym) in enumerate(scrips, 1):
            ingest(fetch_window(from_d, to_d, scrip=scrip))
            if len(rows) >= 1500:
                commit()
            if i % 100 == 0:
                print(f'  [{i}/{len(scrips)}] committed={done["ann"]} material={stats["material"]}', flush=True)
            time.sleep(0.15)
        commit()
        print('-' * 60)
        print(f'  announcements scanned : {stats["anns"]}')
        print(f'  committed             : {done["ann"]} announcements, {done["cc"]} concall')
        return
    else:
        from_d = (today - timedelta(days=args.days)).strftime('%Y%m%d')
        to_d = today.strftime('%Y%m%d')
        print(f'[INFO] DAILY global scan {from_d}->{to_d}')
        ingest(fetch_window(from_d, to_d, scrip=''))

    all_rows = list(rows.values())
    print('-' * 60)
    print(f'  announcements scanned : {stats["anns"]}')
    print(f'  mapped to our symbols : {stats["mapped"]}')
    print(f'  unique (by news_id)   : {len(all_rows)}')
    print(f'  material              : {stats["material"]}')
    print(f'  concall transcripts   : {stats["concall"]}')
    print(f'  distinct stocks       : {len({r["symbol"] for r in all_rows})}')

    if args.dry_run:
        print('\nDRY RUN — nothing written. Sample rows:')
        for r in all_rows[:6]:
            print(f'  [{r["category"]}] {r["symbol"]}: {r["headline"][:80]}')
        return

    n = upsert('corporate_announcements', all_rows, 'news_id')
    print(f'[OK] upserted {n} announcements')

    # Route concall transcripts into concall_transcripts (quarter inferred loosely).
    # Dedup by the conflict key (symbol,quarter,source) — Postgres ON CONFLICT
    # rejects an INSERT that contains the same constrained tuple twice.
    concall_map = {}
    for r in all_rows:
        if is_concall(r['headline'] + ' ' + r['detail']):
            fa = parse_dt(r['filed_at'])
            if not fa:
                continue
            d = fa - timedelta(days=45)
            m = d.month
            qn = 'Q1' if m in (4, 5, 6) else 'Q2' if m in (7, 8, 9) else 'Q3' if m in (10, 11, 12) else 'Q4'
            fy = d.year + 1 if m >= 4 else d.year
            q = f'{qn}FY{fy % 100:02d}'
            concall_map[(r['symbol'], q, 'BSE')] = {
                'symbol': r['symbol'], 'quarter': q, 'period_end': None,
                'filed_at': r['filed_at'], 'source': 'BSE',
                'source_url': r['pdf_url'], 'file_size_kb': None,
                'title': r['headline'][:200]}
    concall_rows = list(concall_map.values())
    if concall_rows:
        cn = upsert('concall_transcripts', concall_rows, 'symbol,quarter,source')
        print(f'[OK] routed {cn} concall transcripts')


if __name__ == '__main__':
    main()
