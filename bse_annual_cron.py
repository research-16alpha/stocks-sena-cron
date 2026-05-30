"""
bse_annual_cron.py
==================
Primary-source ANNUAL statements (P&L + Balance Sheet + Cash Flow) from BSE for
stocks whose annual data is stale or missing. Companion to bse_quarterly_cron.py
(quarters) — this fills the gap that actually drives book value / ROE / ROCE /
Piotroski, which the quarterly pass cannot.

WHY THIS EXISTS
  The quarterly cron made "latest quarter" current, but ratios are computed from
  the latest ANNUAL balance sheet. ~214 active stocks had fresh quarters yet a
  stale/None annual (e.g. TATAMOTORS, PFIZER, RAILTEL, MOIL, RALLIS), so their
  book value / ROE stayed blank. This pull closes that.

SOURCE
  Same BSE results API as the quarterly cron:
    Corp_FinanceResult_ng?SCRIP_CD=..&FlagDur={6,7}&HFQ=&ISUBGROUP_CODE=
  The March year-end rows (quarter_code M*) carry the audited annual iXBRL with a
  full 350-380-day period context + balance-sheet instant context. We feed each
  filing's XMLName (standalone) / Consol_XMLName (consolidated) to the existing
  parse_annual_file(), which self-filters (returns None for non-annual files) and
  returns {period, pl, bs, cf, ratios, segments} in our bundle schema.

MERGE (fill-don't-clobber + upgrade)
  Adds any FY the bundle lacks; upgrades a FY that exists only from a non-primary
  source (tagged _source=bse_annual_filing). Rebuilds the merged aliases
  annual_pl/annual_bs/annual_cf as consolidated-preferred, matching the parser.

USAGE
  py -3.11 bse_annual_cron.py --syms PFIZER,RAILTEL,MOIL --dry-run
  py -3.11 bse_annual_cron.py --annual-stale --since-fy 2022-03-31 --workers 10
  py -3.11 bse_annual_cron.py --syms-file C:/tmp/annual_targets.json
"""
import argparse
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bse_local_parser import parse_annual_file
from bse_quarterly_cron import session, build_xbrl_url, download, BSE_API
from fix_bank_quarterly_sales import (
    ensure_backup_bucket, backup_bundle, upload_bundle, URL, HEADERS, BUCKET,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SYMIDS_FILE = os.path.join(HERE, '_symbol_identifiers.json')
SOURCE_TAG = 'bse_annual_filing'
# M1 fix: never clobber ANY primary-source annual row (NSE integrated filing /
# MCA XBRL / our own BSE). Previously only protected our own tag → it replaced
# fresher NSE/MCA annuals wholesale. We field-merge into non-primary rows only.
PRIMARY_SOURCES = {'bse_annual_filing', 'nse_integrated_filing', 'xbrl_mca'}
WORKERS = 8


def fetch_annual_rows(scrip, flagdurs):
    """All March year-end (annual) result rows for a scrip, deduped by quarter_code."""
    s = session()
    rows = []
    for fd in flagdurs:
        try:
            r = s.get(BSE_API, params={'SCRIP_CD': scrip, 'FlagDur': fd,
                                       'HFQ': '', 'ISUBGROUP_CODE': ''}, timeout=30)
            if r.status_code == 200 and r.text.strip():
                rows += (r.json() or {}).get('Table') or []
        except Exception:
            continue
    by_q = {}
    for row in rows:
        qc = (row.get('quarter_code') or '')
        if not qc.startswith('M'):          # M = March year-end (annual audited)
            continue
        ex = by_q.get(qc)
        if not ex or (not ex.get('Consol_XMLName') and row.get('Consol_XMLName')):
            by_q[qc] = row
    return list(by_q.values())


def parse_filing(txt):
    """Save fetched iXBRL to a temp file and run parse_annual_file (path-based)."""
    tf = tempfile.NamedTemporaryFile('w', suffix='.html', delete=False, encoding='utf-8')
    try:
        tf.write(txt)
        tf.close()
        return parse_annual_file(tf.name)
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass


def _merge_alias(consol, standalone):
    """Consolidated-preferred per-period merge (matches parse_stock_folder)."""
    by_period = {}
    for r in standalone:
        by_period[r.get('period')] = r
    for r in consol:                          # consolidated overrides
        by_period[r.get('period')] = r
    return [by_period[p] for p in sorted(by_period) if p]


def process_symbol(sym, scrip, since_fy, flagdurs, dry):
    rows = fetch_annual_rows(scrip, flagdurs)
    if not rows:
        return (sym, 'NO_ROWS', {})

    # period -> {basis: {pl,bs,cf,ratios,segments}}
    by_period = {}
    for row in rows:
        for basis, key in (('standalone', 'XMLName'), ('consolidated', 'Consol_XMLName')):
            url, _ext = build_xbrl_url(row.get(key))
            if not url:
                continue
            txt = download(url)
            time.sleep(0.05)
            if not txt:
                continue
            rec = parse_annual_file_safe(txt)
            if not rec:
                continue
            period = rec.get('period')
            if not period or (since_fy and period < since_fy):
                continue
            by_period.setdefault(period, {}).setdefault(basis, rec)

    if not by_period:
        return (sym, 'NO_ANNUAL', {})

    r = requests.get(f'{URL}/storage/v1/object/public/{BUCKET}/{sym}.json', timeout=30)
    if r.status_code != 200:
        return (sym, 'NO_BUNDLE', {})
    bundle = r.json()
    raw = json.dumps(bundle, default=str, separators=(',', ':')).encode('utf-8')

    pl_c = bundle.setdefault('annual_pl_consolidated', [])
    pl_s = bundle.setdefault('annual_pl_standalone', [])
    bs_c = bundle.setdefault('annual_bs_consolidated', [])
    bs_s = bundle.setdefault('annual_bs_standalone', [])
    cf_c = bundle.setdefault('annual_cf_consolidated', [])
    cf_s = bundle.setdefault('annual_cf_standalone', [])
    ratios = bundle.setdefault('annual_ratios_filed', [])

    def upsert(arr, rec_part, period):
        rec_part = dict(rec_part)
        rec_part['period'] = period
        rec_part['_source'] = SOURCE_TAG
        for i, r0 in enumerate(arr):
            if r0.get('period') == period:
                # only upgrade non-primary rows; leave our own primary as-is
                if r0.get('_source') in PRIMARY_SOURCES:
                    return 0
                arr[i] = rec_part
                return 1
        arr.append(rec_part)
        return 1

    stats = {'pl': 0, 'bs': 0, 'periods': sorted(by_period)}
    for period, bases in by_period.items():
        for basis, rec in bases.items():
            if basis == 'consolidated':
                stats['pl'] += upsert(pl_c, rec['pl'], period)
                upsert(bs_c, rec['bs'], period)
                upsert(cf_c, rec['cf'], period)
            else:
                stats['pl'] += upsert(pl_s, rec['pl'], period)
                upsert(bs_s, rec['bs'], period)
                upsert(cf_s, rec['cf'], period)
            if rec.get('ratios') and not any(x.get('period') == period for x in ratios):
                ratios.append({**rec['ratios'], 'period': period})

    for arr in (pl_c, pl_s, bs_c, bs_s, cf_c, cf_s, ratios):
        arr.sort(key=lambda r: r.get('period') or '')

    # Rebuild merged aliases (consolidated-preferred) so compute_metrics + app see them
    bundle['annual_pl'] = _merge_alias(pl_c, pl_s)
    bundle['annual_bs'] = _merge_alias(bs_c, bs_s)
    bundle['annual_cf'] = _merge_alias(cf_c, cf_s)

    bundle.setdefault('provenance', {})['last_bse_annual'] = time.strftime(
        '%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    bundle['provenance']['bse_scrip_code'] = scrip

    if dry:
        return (sym, 'DRY', stats)
    if not backup_bundle(sym, raw):
        return (sym, 'BACKUP_FAIL', stats)
    if upload_bundle(sym, bundle):
        return (sym, 'OK', stats)
    return (sym, 'UPLOAD_FAIL', stats)


# parse wrapper kept module-level so workers can call it
def parse_annual_file_safe(txt):
    try:
        return parse_filing(txt)
    except Exception:
        return None


def load_targets(args, symids):
    if args.syms_file:
        with open(args.syms_file) as f:
            return [s.strip().upper() for s in json.load(f) if s.strip()]
    if args.syms:
        return [s.strip().upper() for s in args.syms.split(',') if s.strip()]
    rows = []
    off = 0
    while True:
        r = requests.get(f'{URL}/rest/v1/stock_master?select=symbol,latest_annual_period'
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
    cutoff = args.annual_before
    return [r['symbol'] for r in rows
            if not r.get('latest_annual_period') or r['latest_annual_period'] < cutoff]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', type=str, default='')
    ap.add_argument('--syms-file', dest='syms_file', type=str, default='')
    ap.add_argument('--annual-stale', action='store_true',
                    help='target active stocks with stale/missing annual (default if no --syms)')
    ap.add_argument('--all', dest='all_syms', action='store_true')
    ap.add_argument('--annual-before', dest='annual_before', default='2024-03-31',
                    help='a stock is "annual-stale" if latest_annual_period < this')
    ap.add_argument('--since-fy', dest='since_fy', default='2022-03-31',
                    help='only write annual periods >= this (keep ~4 recent FYs)')
    ap.add_argument('--flagdur', default='all', choices=['6', '7', 'all'])
    ap.add_argument('--workers', type=int, default=WORKERS)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if not os.path.exists(SYMIDS_FILE):
        print(f'[ERR] {SYMIDS_FILE} missing — run build_identifier_master.py first', file=sys.stderr)
        sys.exit(1)
    symids = json.load(open(SYMIDS_FILE))

    targets = load_targets(args, symids)
    pairs = [(s, symids[s]['bse_scrip']) for s in targets
             if s in symids and symids[s].get('bse_scrip')]
    if args.limit:
        pairs = pairs[:args.limit]
    flagdurs = ['6', '7'] if args.flagdur == 'all' else [args.flagdur]
    print(f'[INFO] targets={len(targets)}  with BSE scrip={len(pairs)}  '
          f'since_fy={args.since_fy}  flagdur={args.flagdur}  workers={args.workers}  dry={args.dry_run}')

    if not args.dry_run and not ensure_backup_bucket():
        print('[ERR] backup bucket', file=sys.stderr)
        sys.exit(1)

    ok = filled = noann = norow = nob = err = 0
    t0 = time.time()

    def safe(p):
        s, sc = p
        try:
            return process_symbol(s, sc, args.since_fy, flagdurs, args.dry_run)
        except Exception as e:
            return (s, f'EXC:{type(e).__name__}:{e}', {})

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(safe, p): p for p in pairs}
        for i, fut in enumerate(as_completed(futs), 1):
            sym, status, stats = fut.result()
            if status in ('OK', 'DRY'):
                ok += 1
                n = stats.get('pl', 0)
                filled += n
                if ok <= 25 or ok % 100 == 0:
                    print(f'  [{i}/{len(pairs)}] {sym:<14} {status} pl+{n}  {stats.get("periods")}')
            elif status == 'NO_ANNUAL':
                noann += 1
            elif status == 'NO_ROWS':
                norow += 1
            elif status == 'NO_BUNDLE':
                nob += 1
            else:
                err += 1
                if err <= 20:
                    print(f'  {sym}: {status}', file=sys.stderr)
            if i % 100 == 0:
                print(f'  ...[{i}/{len(pairs)}] ok={ok} filled_fy={filled} elapsed={time.time()-t0:.0f}s')

    print('\n' + '-' * 60)
    print(f'  updated bundles   : {ok} (+{filled} annual periods)')
    print(f'  no annual parsed  : {noann}')
    print(f'  no BSE rows       : {norow}')
    print(f'  no bundle         : {nob}')
    print(f'  errors            : {err}')
    print(f'  elapsed           : {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
