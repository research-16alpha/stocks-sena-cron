"""
bse_quarterly_cron.py
=====================
Primary-source quarterly results for BSE-listed stocks that NSE's
integrated-filing feed does not carry (BSE-only smallcaps + NSE names absent
from NSE's API). Companion to integrated_filing_quarterly.py (NSE side).

SOURCE
  BSE Angular results API:
    api.bseindia.com/BseIndiaAPI/api/Corp_FinanceResult_ng/w
      ?SCRIP_CD={scrip}&FlagDur={6|7}&HFQ=3&ISUBGROUP_CODE=
    FlagDur 6 = last 1 year, 7 = beyond 1 year (history); HFQ 3 = Quarterly.
    Each row → quarter_code (e.g. DQ2025-2026), audited, XMLName (standalone),
    Consol_XMLName (consolidated). XBRL lives under XBRLFILES/ (iXBRL .html / .xml).

  Scrip codes come from _symbol_identifiers.json (built by build_identifier_master.py).

PIPELINE  (reuses the NSE-side parsers — same in-capmkt iXBRL schema)
  fetch rows → build XBRL url → download iXBRL → parse_xbrl_text → pick the
  quarter context by end-date derived from quarter_code → map_quarter →
  fill/merge into the bundle's quarterly arrays (consolidated preferred for
  the merged view) → backup + upload.

GUARDS
  - scrip resolution was strict-name-matched at build time (unique match only).
  - fill-don't-clobber-primary: a period already tagged nse_integrated_filing is
    NOT overwritten (NSE primary wins for dual-listed names); everything else is
    upgraded to BSE primary.

USAGE
  py -3.11 bse_quarterly_cron.py --syms HAWKINCOOK,DRLAL --dry-run
  py -3.11 bse_quarterly_cron.py --stale            # all active stale-quarter stocks
  py -3.11 bse_quarterly_cron.py --stale --flagdur all --workers 12
  py -3.11 bse_quarterly_cron.py --all              # full resolvable universe (heavy)
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bse_local_parser import parse_xbrl_text
from integrated_filing_quarterly import (
    pick_quarter_ctx, map_quarter, detect_basis,
)
from fix_bank_quarterly_sales import (
    ensure_backup_bucket, backup_bundle, upload_bundle, URL, HEADERS, BUCKET,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SYMIDS_FILE = os.path.join(HERE, '_symbol_identifiers.json')

BSE_API = 'https://api.bseindia.com/BseIndiaAPI/api/Corp_FinanceResult_ng/w'
XBRL_BASE = 'https://www.bseindia.com/XBRLFILES/'
SOURCE_TAG = 'bse_integrated_filing'
NSE_PRIMARY = 'nse_integrated_filing'   # never overwrite this
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
XBRL_SLEEP = 0.05
WORKERS = 8

_QCODE = re.compile(r'^([JSDM])Q(\d{4})-(\d{4})$')


def qcode_to_end(qc: str):
    """'DQ2025-2026' -> '2025-12-31'. Quarterly codes only (second letter Q)."""
    m = _QCODE.match((qc or '').strip().upper())
    if not m:
        return None
    q, y1, y2 = m.group(1), m.group(2), m.group(3)
    return {'J': f'{y1}-06-30', 'S': f'{y1}-09-30',
            'D': f'{y1}-12-31', 'M': f'{y2}-03-31'}[q]


def build_xbrl_url(xml_name: str):
    if not xml_name:
        return None, None
    if '/' in xml_name:
        ext = '.xml' if xml_name.lower().endswith('.xml') else '.html'
        return XBRL_BASE + xml_name, ext
    if xml_name.startswith('Main_Ind_As_'):
        return XBRL_BASE + 'FourOneUploadDocument/' + xml_name, '.xml'
    base = re.sub(r'\.xml$', '', xml_name, flags=re.I)
    return XBRL_BASE + 'IFIndasDuplicateUploadDocument/' + base + '_IFIndAs.html', '.html'


# ── thread-local primed BSE session ───────────────────────────────────────────
_local = threading.local()


def session() -> requests.Session:
    s = getattr(_local, 's', None)
    if s is None:
        s = requests.Session()
        s.headers.update({'User-Agent': UA,
                          'Referer': 'https://www.bseindia.com/corporates/comp_resultsnew',
                          'Origin': 'https://www.bseindia.com',
                          'Accept': 'application/json, text/plain, */*'})
        try:
            s.get('https://www.bseindia.com/', timeout=20)
        except Exception:
            pass
        _local.s = s
    return s


def fetch_rows(scrip: str, flagdurs):
    s = session()
    rows = []
    for fd in flagdurs:
        try:
            r = s.get(BSE_API, params={'SCRIP_CD': scrip, 'FlagDur': fd,
                                       'HFQ': '3', 'ISUBGROUP_CODE': ''}, timeout=30)
            if r.status_code == 200 and r.text.strip():
                rows += (r.json() or {}).get('Table') or []
        except Exception:
            continue
    # dedupe by quarter_code, prefer a row that actually carries an XMLName
    by_q = {}
    for row in rows:
        q = row.get('quarter_code') or ''
        ex = by_q.get(q)
        if not ex or (not ex.get('XMLName') and row.get('XMLName')):
            by_q[q] = row
    return list(by_q.values())


def download(url: str):
    s = session()
    try:
        r = s.get(url, timeout=45)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    # BSE serves a ~13KB Angular shell (200) for missing files
    if len(r.content) < 20000 and 'data-critters-container' in r.text[:400]:
        return None
    return r.text


def process_symbol(sym: str, scrip: str, since_iso: str, flagdurs, dry: bool):
    rows = fetch_rows(scrip, flagdurs)
    if not rows:
        return (sym, 'NO_ROWS', {})

    by_period = {}   # end -> {basis: (row, audited)}
    for row in rows:
        end = qcode_to_end(row.get('quarter_code'))
        if not end or (since_iso and end < since_iso):
            continue
        audited = row.get('audited')
        for basis_key, xml in (('standalone', row.get('XMLName')),
                               ('consolidated', row.get('Consol_XMLName'))):
            url, _ext = build_xbrl_url(xml)
            if not url:
                continue
            txt = download(url)
            time.sleep(XBRL_SLEEP)
            if not txt:
                continue
            facts, ctxs = parse_xbrl_text(txt)
            ctx = pick_quarter_ctx(ctxs, end)
            if not ctx:
                continue
            ftype = 'bank' if facts.get('InterestEarned') else 'generic'
            mapped = map_quarter(facts, ctx, ftype)
            if not mapped.get('sales') and not mapped.get('net_profit'):
                continue
            # trust the filing's own basis tag over the API column
            basis = detect_basis(txt) or basis_key
            by_period.setdefault(end, {}).setdefault(basis, (mapped, audited))

    if not by_period:
        return (sym, 'NO_QUARTERS', {})

    r = requests.get(f'{URL}/storage/v1/object/public/{BUCKET}/{sym}.json', timeout=30)
    if r.status_code != 200:
        return (sym, 'NO_BUNDLE', {})
    bundle = r.json()
    raw = json.dumps(bundle, default=str, separators=(',', ':')).encode('utf-8')

    std = bundle.setdefault('quarterly_results_standalone', [])
    con = bundle.setdefault('quarterly_results_consolidated', [])
    merged = bundle.setdefault('quarterly_results', [])

    def fill(arr, period, row, aud):
        fields = dict(row)
        fields['period'] = period
        fields['_source'] = SOURCE_TAG
        fields['_audited'] = (aud or '').lower().startswith('aud') if aud else None
        fields.pop('_sales_unavailable_reason', None)
        for i, r0 in enumerate(arr):
            if r0.get('period') == period:
                # never overwrite an NSE-primary row (NSE wins for dual-listed)
                if r0.get('_source') == NSE_PRIMARY:
                    return 0
                merged = dict(r0)
                merged.update(fields)
                arr[i] = merged
                return 1
        arr.append(fields)
        return 1

    stats = {'std': 0, 'con': 0, 'merged': 0, 'periods': sorted(by_period)}
    for period, bases in by_period.items():
        if 'standalone' in bases:
            stats['std'] += fill(std, period, *bases['standalone'])
        if 'consolidated' in bases:
            stats['con'] += fill(con, period, *bases['consolidated'])
        pick = bases.get('consolidated') or bases.get('standalone')
        if pick:
            stats['merged'] += fill(merged, period, *pick)

    for arr in (std, con, merged):
        arr.sort(key=lambda r: r.get('period') or '')
    bundle.setdefault('provenance', {})['last_bse_quarterly'] = time.strftime(
        '%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    bundle['provenance']['bse_scrip_code'] = scrip

    if dry:
        return (sym, 'DRY', stats)
    if not backup_bundle(sym, raw):
        return (sym, 'BACKUP_FAIL', stats)
    if upload_bundle(sym, bundle):
        return (sym, 'OK', stats)
    return (sym, 'UPLOAD_FAIL', stats)


def load_targets(args, symids):
    if args.syms_file:
        with open(args.syms_file) as f:
            return [s.strip().upper() for s in json.load(f) if s.strip()]
    if args.syms:
        return [s.strip().upper() for s in args.syms.split(',') if s.strip()]
    # pull active universe + latest_quarter_period from stock_master
    rows = []
    off = 0
    while True:
        r = requests.get(f'{URL}/rest/v1/stock_master?select=symbol,latest_quarter_period'
                         f'&is_active=eq.true&limit=1000&offset={off}', headers=HEADERS, timeout=30)
        b = r.json()
        if not b:
            break
        rows += b
        off += 1000
        if len(b) < 1000:
            break
    if args.all_syms:
        return [r['symbol'] for r in rows]
    # default --stale: latest quarter older than cutoff
    cutoff = args.stale_before
    return [r['symbol'] for r in rows
            if not r.get('latest_quarter_period') or r['latest_quarter_period'] < cutoff]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', type=str, default='')
    ap.add_argument('--syms-file', dest='syms_file', type=str, default='',
                    help='JSON file with a list of symbols to process (overrides --syms/--stale).')
    ap.add_argument('--stale', action='store_true', help='target active stale-quarter stocks (default if no --syms)')
    ap.add_argument('--all', dest='all_syms', action='store_true', help='full resolvable universe')
    ap.add_argument('--stale-before', dest='stale_before', default='2025-03-31')
    ap.add_argument('--since', default='2024-06-30', help='only write quarters with end >= this')
    ap.add_argument('--flagdur', default='6', choices=['6', '7', 'all'],
                    help='6=last yr (default), 7=history, all=both')
    ap.add_argument('--workers', type=int, default=WORKERS)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if not os.path.exists(SYMIDS_FILE):
        print(f'[ERR] {SYMIDS_FILE} missing — run build_identifier_master.py first', file=sys.stderr)
        sys.exit(1)
    symids = json.load(open(SYMIDS_FILE))

    targets = load_targets(args, symids)
    # keep only those with a resolved BSE scrip
    pairs = [(s, symids[s]['bse_scrip']) for s in targets
             if s in symids and symids[s].get('bse_scrip')]
    if args.limit:
        pairs = pairs[:args.limit]
    print(f'[INFO] targets={len(targets)}  with BSE scrip={len(pairs)}  '
          f'flagdur={args.flagdur}  workers={args.workers}  dry={args.dry_run}')

    flagdurs = ['6', '7'] if args.flagdur == 'all' else [args.flagdur]
    if not args.dry_run and not ensure_backup_bucket():
        print('[ERR] backup bucket', file=sys.stderr)
        sys.exit(1)

    ok = filled = noq = norow = nob = err = 0
    t0 = time.time()

    def safe(p):
        s, sc = p
        try:
            return process_symbol(s, sc, args.since, flagdurs, args.dry_run)
        except Exception as e:
            return (s, f'EXC:{type(e).__name__}:{e}', {})

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(safe, p): p for p in pairs}
        for i, fut in enumerate(as_completed(futs), 1):
            sym, status, stats = fut.result()
            if status in ('OK', 'DRY'):
                ok += 1
                n = stats.get('merged', 0)
                filled += n
                if ok <= 25 or ok % 100 == 0:
                    print(f'  [{i}/{len(pairs)}] {sym:<14} {status} std+{stats.get("std",0)} '
                          f'con+{stats.get("con",0)} merged+{n}  {stats.get("periods")}')
            elif status == 'NO_QUARTERS':
                noq += 1
            elif status == 'NO_ROWS':
                norow += 1
            elif status == 'NO_BUNDLE':
                nob += 1
            else:
                err += 1
                if err <= 20:
                    print(f'  {sym}: {status}', file=sys.stderr)
            if i % 250 == 0:
                print(f'  ...[{i}/{len(pairs)}] ok={ok} filled_periods={filled} '
                      f'elapsed={time.time()-t0:.0f}s')

    print('\n' + '-' * 60)
    print(f'  updated bundles   : {ok} (+{filled} merged periods)')
    print(f'  no quarters parsed: {noq}')
    print(f'  no BSE rows       : {norow}')
    print(f'  no bundle         : {nob}')
    print(f'  errors            : {err}')
    print(f'  elapsed           : {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
