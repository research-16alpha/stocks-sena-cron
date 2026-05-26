"""
fetch_new_filings.py
====================
Fetches new financial result filings from NSE and BSE since the last successful run.

Sources:
  - NSE: https://www.nseindia.com/api/corporate-announcements
  - BSE: https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w

Filters for filings of type "Financial Results" / "Integrated Filing" only.

Output:
  F:\\expansion\\stocks-sena\\filings\\YYYY-MM-DD_HHMM_new.json
  F:\\expansion\\stocks-sena\\filings\\_last_run.json (timestamp of last successful pull)

The output is consumed by apply_new_filings.py.

Designed to run frequently (every 15 min during market hours) — exits fast if nothing new.

Usage:
  python fetch_new_filings.py                # since last run
  python fetch_new_filings.py --hours 24     # last 24h
  python fetch_new_filings.py --dry-run      # show, don't write
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
import requests

OUT_DIR = r'F:\expansion\stocks-sena\filings'
LAST_RUN = os.path.join(OUT_DIR, '_last_run.json')

IST = timezone(timedelta(hours=5, minutes=30))

NSE_BASE = 'https://www.nseindia.com'
NSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.nseindia.com/companies-listing/corporate-filings-announcements',
}

BSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Origin': 'https://www.bseindia.com',
    'Referer': 'https://www.bseindia.com/corporates/ann.html',
}

# Categories that signal financial results
RESULT_KEYWORDS = (
    'financial result', 'financial results', 'integrated filing',
    'audited result', 'unaudited result', 'quarterly result',
    'annual result', 'q1 result', 'q2 result', 'q3 result', 'q4 result',
    'half year', 'reg 33', 'regulation 33',
)


def prime_nse_session() -> requests.Session:
    """NSE needs cookie priming - hit homepage first."""
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    try:
        s.get(NSE_BASE, timeout=15)
        s.get(f'{NSE_BASE}/companies-listing/corporate-filings-announcements', timeout=15)
    except Exception as e:
        print(f'[WARN] NSE session priming: {e}', file=sys.stderr)
    return s


def fetch_nse_announcements(since_dt: datetime) -> list:
    """NSE returns latest 24h-48h of announcements - filter to since_dt."""
    s = prime_nse_session()
    out = []
    for index in ('equities',):
        url = f'{NSE_BASE}/api/corporate-announcements'
        params = {'index': index}
        try:
            r = s.get(url, params=params, timeout=20)
            if r.status_code != 200:
                print(f'[WARN] NSE {index}: HTTP {r.status_code}', file=sys.stderr)
                continue
            data = r.json()
            # Returns list directly OR {"data": [...]}
            items = data if isinstance(data, list) else data.get('data') or []
            for item in items:
                # NSE field names: desc, attchmntText, an_dt, sort_date, attchmntFile
                broad_dt_str = item.get('sort_date') or item.get('an_dt')
                if broad_dt_str:
                    parsed = False
                    for fmt in ('%Y-%m-%d %H:%M:%S', '%d-%b-%Y %H:%M:%S'):
                        try:
                            dt = datetime.strptime(broad_dt_str, fmt).replace(tzinfo=IST)
                            if dt < since_dt:
                                broad_dt_str = '__SKIP__'
                            parsed = True
                            break
                        except ValueError:
                            continue
                    if broad_dt_str == '__SKIP__':
                        continue
                subject_text = ((item.get('desc') or '') + ' ' + (item.get('attchmntText') or '')).lower()
                if not any(k in subject_text for k in RESULT_KEYWORDS):
                    continue
                out.append({
                    'exchange': 'NSE',
                    'symbol': item.get('symbol'),
                    'subject': item.get('desc') or item.get('attchmntText',''),
                    'filed_at': broad_dt_str,
                    'attachment_url': item.get('attchmntFile'),
                    'raw': item,
                })
        except Exception as e:
            print(f'[ERR] NSE fetch: {e}', file=sys.stderr)
    return out


def fetch_bse_announcements(since_dt: datetime) -> list:
    """BSE corp announcements - filter to financial results."""
    out = []
    # BSE format: strPrevDate/strToDate = YYYYMMDD
    today = datetime.now(IST).strftime('%Y%m%d')
    prev = (datetime.now(IST) - timedelta(days=2)).strftime('%Y%m%d')
    url = 'https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w'
    # strCat: 20 = Result, or empty for all
    params_list = [
        {'pageno': 1, 'strCat': '-1', 'strPrevDate': prev, 'strScrip': '',
         'strSearch': 'P', 'strToDate': today, 'strType': 'C'},
    ]
    for params in params_list:
        try:
            r = requests.get(url, params=params, headers=BSE_HEADERS, timeout=20)
            if r.status_code != 200:
                print(f'[WARN] BSE: HTTP {r.status_code}', file=sys.stderr)
                continue
            data = r.json()
            items = data.get('Table') or []
            for item in items:
                subject = (item.get('NEWSSUB') or item.get('HEADLINE') or '').lower()
                if not any(k in subject for k in RESULT_KEYWORDS):
                    continue
                # Filter by date
                news_dt_str = item.get('NEWS_DT') or item.get('News_submission_dt')
                if news_dt_str:
                    try:
                        dt = datetime.strptime(news_dt_str.split('.')[0], '%Y-%m-%dT%H:%M:%S')
                        dt = dt.replace(tzinfo=IST)
                        if dt < since_dt:
                            continue
                    except Exception:
                        pass
                attachment = item.get('ATTACHMENTNAME')
                attachment_url = f'https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}' if attachment else None
                out.append({
                    'exchange': 'BSE',
                    'scrip_code': str(item.get('SCRIP_CD')) if item.get('SCRIP_CD') else None,
                    'symbol': None,  # to be resolved via crosswalk
                    'subject': item.get('NEWSSUB') or item.get('HEADLINE'),
                    'filed_at': news_dt_str,
                    'attachment_url': attachment_url,
                    'raw': item,
                })
        except Exception as e:
            print(f'[ERR] BSE fetch: {e}', file=sys.stderr)
    return out


def load_last_run() -> datetime:
    if not os.path.exists(LAST_RUN):
        return datetime.now(IST) - timedelta(hours=24)
    try:
        with open(LAST_RUN, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return datetime.fromisoformat(d['last_run']).astimezone(IST)
    except Exception:
        return datetime.now(IST) - timedelta(hours=24)


def save_last_run(dt: datetime) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(LAST_RUN, 'w', encoding='utf-8') as f:
        json.dump({'last_run': dt.isoformat()}, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours', type=float, default=0,
                    help='Override since_dt to this many hours ago (ignores _last_run)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    now = datetime.now(IST)

    if args.hours > 0:
        since = now - timedelta(hours=args.hours)
    else:
        since = load_last_run()
    print(f'[INFO] Fetching filings since {since.isoformat()}')

    nse = fetch_nse_announcements(since)
    bse = fetch_bse_announcements(since)
    print(f'[INFO]   NSE results: {len(nse)}')
    print(f'[INFO]   BSE results: {len(bse)}')

    all_items = nse + bse
    if not all_items:
        print('[OK] no new filings')
        if not args.dry_run:
            save_last_run(now)
        return

    if args.dry_run:
        print('[DRY-RUN] First 10:')
        for it in all_items[:10]:
            print(f"  {it['exchange']:<3} {it.get('symbol') or it.get('scrip_code'):<12} {it['subject'][:80]}")
        return

    # Write output file
    out_name = now.strftime('%Y-%m-%d_%H%M') + '_new.json'
    out_path = os.path.join(OUT_DIR, out_name)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({
            'fetched_at': now.isoformat(),
            'since': since.isoformat(),
            'nse_count': len(nse),
            'bse_count': len(bse),
            'items': all_items,
        }, f, default=str, separators=(',', ':'))

    save_last_run(now)
    print(f'[OK] wrote {out_path} ({len(all_items)} items)')


if __name__ == '__main__':
    main()
