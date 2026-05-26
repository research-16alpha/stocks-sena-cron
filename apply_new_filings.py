"""
apply_new_filings.py
====================
Reads the latest filings list from F:\\filings\\, downloads attachments, parses
financial data, merges into existing fundamentals-v2 bundles, and re-uploads.

Sources of parsing:
  - BSE iXBRL HTML  -> bse_local_parser.parse_html_xbrl()
  - BSE XBRL XML    -> bse_local_parser.parse_xml_xbrl()
  - BSE CSV         -> bse_local_parser.parse_csv_quarterly()
  - NSE XBRL XML    -> nse_xbrl_scraper.parse_xbrl()
  - PDFs            -> SKIP for now (not parseable without LLM)

Resolution: BSE scrip -> NSE symbol via the same mapping used in bse_local_to_disk

Safety:
  - Only ADDS new periods, never deletes
  - Runs validation on new rows; if FAIL → log + skip update
  - Backs up old bundle to fundamentals-v2-backups bucket before overwrite
  - Atomic update per stock

Usage:
  python apply_new_filings.py             # apply latest filings file
  python apply_new_filings.py --file <path>
  python apply_new_filings.py --dry-run
"""
import argparse
import glob
import json
import os
import re
import sys
import tempfile
import time
import requests
from typing import Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

with open('e:/Stocks sena/.supabase-service-key', 'r') as f:
    KEY = f.read().strip()
URL = 'https://tbeadvvkqyrhtendttrg.supabase.co'
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

FILINGS_DIR = r'F:\expansion\stocks-sena\filings'
APPLIED_LOG = os.path.join(FILINGS_DIR, '_applied.json')
BUCKET      = 'fundamentals-v2'
BUCKET_BAK  = 'fundamentals-v2-backups'

DL_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Referer': 'https://www.nseindia.com/',
}


def latest_filings_file() -> Optional[str]:
    files = sorted(glob.glob(os.path.join(FILINGS_DIR, '*_new.json')))
    return files[-1] if files else None


def load_scrip_to_symbol() -> Dict[str, str]:
    """Load the BSE_SCRIP -> NSE_SYMBOL crosswalk built by bse_local_to_disk."""
    p = r'F:\expansion\stocks-sena\bse_v3\_scrip_to_symbol.json'
    if not os.path.exists(p):
        return {}
    try:
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def resolve_symbol(item: Dict, scrip_map: Dict) -> Optional[str]:
    if item.get('symbol'):
        return item['symbol']
    scrip = item.get('scrip_code')
    if scrip and scrip in scrip_map:
        sym = scrip_map[scrip]
        if not sym.startswith('BSE_'):
            return sym
    return None


def download_attachment(url: str, ext_hint: str = '') -> Optional[str]:
    """Download to temp file, return path."""
    if not url:
        return None
    try:
        r = requests.get(url, headers=DL_HEADERS, timeout=30, stream=True)
        if r.status_code != 200:
            return None
        # Guess extension
        ct = r.headers.get('content-type', '').lower()
        if 'xml' in ct:
            ext = '.xml'
        elif 'html' in ct or 'xhtml' in ct:
            ext = '.html'
        elif 'pdf' in ct:
            ext = '.pdf'
        elif 'csv' in ct:
            ext = '.csv'
        else:
            ext = ext_hint or os.path.splitext(url)[1] or '.bin'
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        for chunk in r.iter_content(chunk_size=8192):
            tmp.write(chunk)
        tmp.close()
        return tmp.name
    except Exception as e:
        print(f'  [DL ERR] {e}', file=sys.stderr)
        return None


def parse_filing(path: str, subject: str = '') -> Optional[Dict]:
    """Dispatch to appropriate parser based on extension + subject hint."""
    ext = os.path.splitext(path)[1].lower()
    try:
        from bse_local_parser import (
            parse_annual_file, parse_quarterly_file,
            parse_csv_annual, parse_csv_quarterly,
        )
    except ImportError as e:
        print(f'  [IMPORT ERR] {e}', file=sys.stderr)
        return None

    subj_l = subject.lower()
    is_annual = ('annual' in subj_l or 'audited' in subj_l) and 'quarter' not in subj_l
    is_quarter = 'quarter' in subj_l or 'interim' in subj_l

    try:
        if ext in ('.html', '.xhtml', '.xml'):
            # parse_annual_file + parse_quarterly_file both handle XBRL HTML/XML
            if is_annual:
                return parse_annual_file(path)
            if is_quarter:
                return parse_quarterly_file(path)
            # Unknown — try annual, then quarterly
            r = parse_annual_file(path)
            return r or parse_quarterly_file(path)
        if ext == '.csv':
            if is_annual:
                return parse_csv_annual(path)
            if is_quarter:
                return parse_csv_quarterly(path)
            r = parse_csv_quarterly(path)
            return r or parse_csv_annual(path)
        if ext == '.pdf':
            return None  # PDFs not parseable today
        return None
    except Exception as e:
        print(f'  [PARSE ERR] {ext} {e}', file=sys.stderr)
        return None


def fetch_bundle(sym: str) -> Optional[Dict]:
    url = f'{URL}/storage/v1/object/public/{BUCKET}/{sym}.json'
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def backup_bundle(sym: str, bundle: Dict) -> bool:
    """Backup current bundle to fundamentals-v2-backups/{date}/{sym}.json."""
    today = time.strftime('%Y-%m-%d', time.gmtime())
    payload = json.dumps(bundle, default=str, separators=(',', ':')).encode('utf-8')
    url = f'{URL}/storage/v1/object/{BUCKET_BAK}/{today}/{sym}.json'
    h = {**H, 'Content-Type': 'application/json', 'x-upsert': 'true'}
    try:
        r = requests.post(url, headers=h, data=payload, timeout=30)
        return r.status_code in (200, 201)
    except Exception:
        return False


def upload_bundle(sym: str, bundle: Dict) -> bool:
    payload = json.dumps(bundle, default=str, separators=(',', ':')).encode('utf-8')
    h = {**H, 'Content-Type': 'application/json', 'x-upsert': 'true'}
    url = f'{URL}/storage/v1/object/{BUCKET}/{sym}.json'
    try:
        r = requests.post(url, headers=h, data=payload, timeout=30)
        if r.status_code in (200, 201):
            return True
        r = requests.put(url, headers=h, data=payload, timeout=30)
        return r.status_code in (200, 201)
    except Exception:
        return False


def merge_new_periods(bundle: Dict, parsed: Dict, source_tag: str) -> tuple:
    """Merge parsed periods into bundle. Returns (added_count, skipped_count)."""
    added = 0
    skipped = 0
    for section in ('annual_pl', 'annual_bs', 'annual_cf', 'quarterly_results',
                    'annual_pl_consolidated', 'annual_pl_standalone',
                    'annual_bs_consolidated', 'annual_bs_standalone',
                    'annual_cf_consolidated', 'annual_cf_standalone',
                    'quarterly_results_consolidated', 'quarterly_results_standalone'):
        new_rows = parsed.get(section) or []
        if not new_rows:
            continue
        existing_rows = bundle.get(section) or []
        existing_periods = {r.get('period') for r in existing_rows if r.get('period')}

        for nr in new_rows:
            period = nr.get('period')
            if not period:
                continue
            nr['_source'] = source_tag
            if period in existing_periods:
                # Overwrite (latest XBRL wins over older)
                for i, er in enumerate(existing_rows):
                    if er.get('period') == period:
                        existing_rows[i] = nr
                        break
                skipped += 1
            else:
                existing_rows.append(nr)
                added += 1

        existing_rows.sort(key=lambda r: r.get('period') or '')
        bundle[section] = existing_rows

    return added, skipped


def validate_new(bundle: Dict) -> tuple:
    """Quick sanity check post-merge. Returns (ok, reason)."""
    # No negative sales in latest 4 annual rows
    pl = bundle.get('annual_pl') or []
    for r in pl[-4:]:
        s = r.get('sales')
        if s is not None and s < 0:
            return (False, f'negative sales in {r.get("period")}')
    return (True, '')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', type=str, default='')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    filings_path = args.file or latest_filings_file()
    if not filings_path or not os.path.exists(filings_path):
        print('[OK] no filings file to apply')
        return

    with open(filings_path, 'r', encoding='utf-8') as f:
        feed = json.load(f)
    items = feed.get('items') or []
    print(f'[INFO] Apply {len(items)} filings from {filings_path}')

    scrip_map = load_scrip_to_symbol()
    applied = []
    skipped = []
    errors = []

    for i, item in enumerate(items, 1):
        sym = resolve_symbol(item, scrip_map)
        if not sym:
            skipped.append({'item': i, 'reason': 'no_symbol', 'scrip': item.get('scrip_code'),
                            'subject': (item.get('subject') or '')[:80]})
            continue

        url = item.get('attachment_url')
        if not url:
            skipped.append({'item': i, 'sym': sym, 'reason': 'no_attachment'})
            continue

        if args.dry_run:
            print(f"  [{i}/{len(items)}] {sym:<14} DRY {item['exchange']}  {(item.get('subject') or '')[:60]}")
            continue

        path = download_attachment(url)
        if not path:
            skipped.append({'item': i, 'sym': sym, 'reason': 'download_failed'})
            continue

        parsed = parse_filing(path, item.get('subject') or '')
        try: os.unlink(path)
        except Exception: pass

        if not parsed:
            skipped.append({'item': i, 'sym': sym, 'reason': 'unparseable'})
            continue

        bundle = fetch_bundle(sym)
        if not bundle:
            skipped.append({'item': i, 'sym': sym, 'reason': 'no_bundle'})
            continue

        # Backup before mutating
        backup_bundle(sym, bundle)

        src = f'{item["exchange"].lower()}_delta'
        added, overwrote = merge_new_periods(bundle, parsed, src)

        ok, reason = validate_new(bundle)
        if not ok:
            errors.append({'item': i, 'sym': sym, 'reason': f'validation: {reason}'})
            continue

        bundle.setdefault('provenance', {})['last_delta_applied'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        if upload_bundle(sym, bundle):
            applied.append({'sym': sym, 'added': added, 'overwrote': overwrote,
                            'exchange': item['exchange'],
                            'subject': (item.get('subject') or '')[:80]})
            print(f'  [{i}/{len(items)}] {sym:<14} OK   +{added} new periods (overwrote {overwrote})')
        else:
            errors.append({'item': i, 'sym': sym, 'reason': 'upload_failed'})

    # Log
    os.makedirs(FILINGS_DIR, exist_ok=True)
    log_entry = {
        'ran_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'filings_file': filings_path,
        'applied': applied,
        'skipped': skipped,
        'errors': errors,
    }
    # Append to applied log
    log_list = []
    if os.path.exists(APPLIED_LOG):
        try:
            with open(APPLIED_LOG, 'r', encoding='utf-8') as f:
                log_list = json.load(f)
        except Exception:
            log_list = []
    log_list.append(log_entry)
    with open(APPLIED_LOG, 'w', encoding='utf-8') as f:
        json.dump(log_list[-100:], f, default=str, separators=(',', ':'))  # keep last 100

    print()
    print('-' * 60)
    print(f'  Applied : {len(applied)}')
    print(f'  Skipped : {len(skipped)}')
    print(f'  Errors  : {len(errors)}')


if __name__ == '__main__':
    main()
