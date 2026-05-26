"""
promoter_actions_scraper.py
============================
Scans BSE corp announcements for promoter / insider activity and pledges.

Uses the WORKING endpoint:
  https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w

Detected event types (matched against real BSE announcement titles):
  - sale: "Sale Of Shares By Promoter", "Disclosures under Reg. 29(2)"
  - purchase: "Acquisition of Shares", "Reg. 29(2)" variants
  - pledge_increase: pledge created/increase
  - pledge_decrease: pledge released/revocation
  - auditor_change: appointment / resignation / change of auditor
  - rpt: related party transaction disclosures

Maps BSE scrip -> NSE symbol via stock_master crosswalk.
Dedupes against existing rows in last 30 days.

Cron: runs daily 11:00 UTC = 4:30 PM IST (after post-close filings).
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
import requests

KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not KEY:
    try:
        with open('e:/Stocks sena/.supabase-service-key', 'r') as f:
            KEY = f.read().strip()
    except FileNotFoundError:
        print('[ERR] SUPABASE_SERVICE_KEY required', file=sys.stderr); sys.exit(1)
URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

BSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Origin': 'https://www.bseindia.com',
    'Referer': 'https://www.bseindia.com/corporates/ann.html',
}

# (regex, action_type, severity)
PATTERNS = [
    (r'sale of (equity )?shares.*by (promoter|insider)|promoter.+sale of shares', 'sale', 4),
    (r'acquisition of (equity )?shares|purchase of shares by promoter',           'purchase', 2),
    (r'pledge.+(creat|increas)|creation of pledge|pledge of shares.+increas',     'pledge_increase', 3),
    (r'release of pledge|pledge.+revok|pledge.+decreas',                          'pledge_decrease', 2),
    (r'reg\.?\s*29\(?[12]\)?\s*of\s*sebi.+sast',                                  'reg29_sast', 2),
    (r'appointment of auditor|resignation of auditor|change.+auditor',            'auditor_change', 3),
    (r'related party transaction|rpt disclos',                                    'rpt_disclosed', 2),
]


def fetch_window(from_date: str, to_date: str, max_pages: int = 200) -> list:
    """Pull all BSE announcements in window. Pages = 50/each."""
    out = []
    page = 1
    consecutive_empty = 0
    while page <= max_pages:
        try:
            r = requests.get(
                'https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w',
                headers=BSE_HEADERS, timeout=20,
                params={'pageno': page, 'strCat': '-1', 'strPrevDate': from_date,
                        'strScrip': '', 'strSearch': 'P', 'strToDate': to_date, 'strType': 'C'},
            )
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


def classify(text: str):
    t = (text or '').lower()
    for regex, action, sev in PATTERNS:
        if re.search(regex, t):
            return action, sev
    return None


def fetch_scrip_to_symbol() -> dict:
    out = {}
    offset = 0
    while True:
        r = requests.get(
            f'{URL}/rest/v1/stock_master?select=symbol,name,bse_scrip_code&bse_scrip_code=not.is.null',
            headers={**H, 'Range': f'{offset}-{offset+999}'}, timeout=30,
        )
        batch = r.json()
        if not batch: break
        for row in batch:
            scrip = row.get('bse_scrip_code')
            if scrip:
                out[str(scrip)] = {'symbol': row['symbol'], 'name': row.get('name') or row['symbol']}
        if len(batch) < 1000: break
        offset += 1000
    return out


def fetch_existing_keys(days_back: int = 30) -> set:
    """Returns set of (symbol, action_description_first_200) to dedupe against."""
    seen = set()
    since = (datetime.now() - timedelta(days=days_back)).isoformat()
    r = requests.get(
        f'{URL}/rest/v1/promoter_actions?select=symbol,action_description&filing_date=gte.{since}',
        headers={**H, 'Range': '0-99999'}, timeout=30,
    )
    if r.status_code == 200:
        for row in r.json():
            seen.add((row['symbol'], (row.get('action_description') or '')[:200]))
    return seen


def upsert(rows: list) -> bool:
    if not rows:
        return True
    headers = {**H, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}
    r = requests.post(f'{URL}/rest/v1/promoter_actions', headers=headers,
                      data=json.dumps(rows), timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f'  [INSERT ERR] {r.status_code} {r.text[:200]}', file=sys.stderr)
        return False
    return True


def main():
    today = datetime.now().strftime('%Y%m%d')
    # Default: last 7 days. Override via DAYS env.
    days = int(os.environ.get('DAYS', '7'))
    since = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')

    print(f'[INFO] Scanning BSE announcements {since} -> {today}')
    items = fetch_window(since, today)
    print(f'[INFO] Fetched {len(items)} announcements')

    print('[INFO] Loading scrip -> symbol mapping...')
    scrip_to = fetch_scrip_to_symbol()
    print(f'[INFO]   {len(scrip_to)} scrips mapped')

    print('[INFO] Loading existing rows to dedupe...')
    seen = fetch_existing_keys()
    print(f'[INFO]   {len(seen)} existing rows in last 30 days')

    rows = []
    classified = 0
    no_scrip = 0
    duplicates = 0

    for item in items:
        text = (item.get('NEWSSUB') or '') + ' ' + (item.get('HEADLINE') or '')
        result = classify(text)
        if not result:
            continue
        classified += 1
        scrip = str(item.get('SCRIP_CD') or '')
        meta = scrip_to.get(scrip)
        if not meta:
            no_scrip += 1
            continue
        action, severity = result
        description = (item.get('NEWSSUB') or '')[:500]
        pk = (meta['symbol'], description[:200])
        if pk in seen:
            duplicates += 1
            continue
        seen.add(pk)
        filed_at = None
        for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f'):
            try:
                filed_at = datetime.strptime((item.get('NEWS_DT') or '').split('.')[0], '%Y-%m-%dT%H:%M:%S').isoformat()
                break
            except Exception:
                continue
        attachment = item.get('ATTACHMENTNAME')
        source_url = (f'https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}'
                      if attachment else f'https://www.bseindia.com/corporates/anndet_new.aspx?scrip={scrip}')
        rows.append({
            'symbol': meta['symbol'],
            'company_name': meta['name'],
            'action_type': action,
            'action_description': description,
            'severity': severity,
            'filing_date': filed_at or datetime.now().isoformat(),
            'source_url': source_url,
            'raw_data': None,
        })

    print()
    print(f'  Classified hits     : {classified}')
    print(f'  No scrip mapping    : {no_scrip}')
    print(f'  Duplicates skipped  : {duplicates}')
    print(f'  New rows to insert  : {len(rows)}')

    if rows and upsert(rows):
        print(f'  [OK] inserted {len(rows)} rows')


if __name__ == '__main__':
    main()
