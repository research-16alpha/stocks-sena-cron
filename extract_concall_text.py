"""
extract_concall_text.py
========================
Downloads each concall-transcript PDF and extracts the full text (management
remarks + analyst Q&A) so the app can show the actual call discussion inline,
not just a PDF link. Stores into concall_transcripts.transcript_text (+ has_text).

Uses the age-based BSE path (AttachLive for <8d, AttachHis for older) like the app.
Run:  python extract_concall_text.py            # all transcripts missing text
      python extract_concall_text.py --limit 20 # test
      python extract_concall_text.py --force     # re-extract even if has_text
"""
import argparse
import datetime
import io
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from pypdf import PdfReader
from supabase import create_client

KEY = os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
sb = create_client(URL, KEY)
H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36',
     'Referer': 'https://www.bseindia.com/'}
TODAY = datetime.date.today()


def age_url(u, filed_at):
    try:
        age = (TODAY - datetime.date.fromisoformat(str(filed_at)[:10])).days
    except Exception:
        age = 999
    return (u.replace('/AttachHis/', '/AttachLive/') if 0 <= age < 8
            else u.replace('/AttachLive/', '/AttachHis/'))


def clean(t):
    # collapse excess whitespace, keep paragraph breaks
    lines = [ln.rstrip() for ln in (t or '').splitlines()]
    out, blank = [], 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank <= 1:
                out.append('')
            continue
        blank = 0
        out.append(ln)
    return '\n'.join(out).strip()


def extract_one(row):
    url = age_url(row['source_url'], row.get('filed_at'))
    try:
        r = requests.get(url, headers=H, timeout=30)
        if r.status_code != 200 or 'pdf' not in r.headers.get('Content-Type', '').lower():
            # try the other path once
            alt = url.replace('/AttachHis/', '/AttachLive/') if '/AttachHis/' in url else url.replace('/AttachLive/', '/AttachHis/')
            r = requests.get(alt, headers=H, timeout=30)
            if r.status_code != 200 or 'pdf' not in r.headers.get('Content-Type', '').lower():
                return (row, None)
        reader = PdfReader(io.BytesIO(r.content))
        txt = clean('\n'.join((p.extract_text() or '') for p in reader.pages))
        if len(txt) < 400:        # too short to be a real transcript body
            return (row, None)
        return (row, txt[:400_000])   # cap to keep rows sane
    except Exception:
        return (row, None)


def fetch_targets(force, limit):
    out, off = [], 0
    while True:
        q = sb.table('concall_transcripts').select('symbol,quarter,source,source_url,filed_at,has_text')
        if not force:
            q = q.eq('has_text', False)
        d = q.range(off, off + 999).execute().data or []
        out += [r for r in d if r.get('source_url')]
        if len(d) < 1000:
            break
        off += 1000
        if limit and len(out) >= limit:
            break
    return out[:limit] if limit else out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--workers', type=int, default=12)
    args = ap.parse_args()

    targets = fetch_targets(args.force, args.limit)
    print(f'[INFO] extracting text for {len(targets)} transcripts ...', flush=True)
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, fut in enumerate(as_completed([ex.submit(extract_one, r) for r in targets]), 1):
            row, txt = fut.result()
            if txt:
                try:
                    (sb.table('concall_transcripts').update({'transcript_text': txt, 'has_text': True})
                     .eq('symbol', row['symbol']).eq('quarter', row['quarter']).eq('source', row['source']).execute())
                    ok += 1
                except Exception as e:
                    fail += 1
                    print('  upd err', row['symbol'], str(e)[:60], file=sys.stderr)
            else:
                fail += 1
            if i % 50 == 0:
                print(f'  [{i}/{len(targets)}] ok={ok} fail={fail}', flush=True)
    print(f'[OK] extracted {ok} · failed/empty {fail}')


if __name__ == '__main__':
    main()
