"""
nse_annual_cron.py
==================
Fill MISSING annual years from NSE's old `corporates-financial-results` feed
(period=Annual), which carries audited annual iXBRL back to ~2016 — including
years BSE's structured `Corp_FinanceResult_ng` lacks (e.g. UCOBANK FY2024).

Sibling to bse_annual_cron.py: identical parser (parse_annual_file) + merge +
upload — only the SOURCE differs (NSE instead of BSE). Fill-don't-clobber: never
overwrites a primary-source row; only adds periods the bundle is missing.

NOTE: insurers (LICI, ICICIGI, GICRE, NIACL) return 0 rows here — they file under
a different regulatory format and are not in this feed.

Usage:
  py -3.11 nse_annual_cron.py --syms UCOBANK,RAJESHEXPO --only-missing --dry-run
  py -3.11 nse_annual_cron.py --syms-file C:/tmp/gap_syms.json --only-missing
"""
import argparse
import datetime
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_new_filings import prime_nse_session
from bse_annual_cron import parse_filing, _merge_alias, PRIMARY_SOURCES
from fix_bank_quarterly_sales import backup_bundle, upload_bundle, URL, HEADERS, BUCKET

SOURCE_TAG = 'nse_annual_filing'
NSE_FIN = 'https://www.nseindia.com/api/corporates-financial-results'


def _to_period(td):
    """'31-Mar-2024' -> '2024-03-31'."""
    if not td:
        return None
    for fmt in ('%d-%b-%Y', '%d-%B-%Y', '%Y-%m-%d'):
        try:
            return datetime.datetime.strptime(str(td).strip(), fmt).strftime('%Y-%m-%d')
        except Exception:
            continue
    return None


def fetch_nse_annual(session, sym):
    """[(period, basis, xbrl_url)] of audited annual filings for a symbol."""
    try:
        r = session.get(f"{NSE_FIN}?index=equities&symbol={sym}&period=Annual", timeout=30)
        if r.status_code != 200:
            return []
        rows = r.json()
        if not isinstance(rows, list):
            return []
    except Exception:
        return []
    out = []
    for row in rows:
        xbrl = row.get('xbrl')
        period = _to_period(row.get('toDate') or row.get('to_date'))
        if not xbrl or not period:
            continue
        basis = 'consolidated' if str(row.get('consolidated', '')).lower().startswith('cons') else 'standalone'
        out.append((period, basis, xbrl))
    return out


def _download(session, url):
    try:
        r = session.get(url, timeout=45)
        return r.text if (r.status_code == 200 and r.text.strip()) else None
    except Exception:
        return None


def process_symbol(session, sym, since_fy, only_missing, dry):
    rows = fetch_nse_annual(session, sym)
    if not rows:
        return (sym, 'NO_ROWS', {})
    r = requests.get(f'{URL}/storage/v1/object/public/{BUCKET}/{sym}.json', timeout=30)
    if r.status_code != 200:
        return (sym, 'NO_BUNDLE', {})
    bundle = r.json()
    raw = json.dumps(bundle, default=str, separators=(',', ':')).encode('utf-8')
    have = set(x.get('period') for x in (bundle.get('annual_pl') or []))

    by_period = {}
    for period, basis, xbrl in rows:
        if since_fy and period < since_fy:
            continue
        if only_missing and period in have:
            continue
        txt = _download(session, xbrl)
        time.sleep(0.15)
        if not txt:
            continue
        try:
            rec = parse_filing(txt)
        except Exception:
            rec = None
        if not rec or not rec.get('period'):
            continue
        if only_missing and rec['period'] in have:
            continue
        by_period.setdefault(rec['period'], {}).setdefault(basis, rec)

    if not by_period:
        return (sym, 'NO_NEW', {})

    pl_c = bundle.setdefault('annual_pl_consolidated', []); pl_s = bundle.setdefault('annual_pl_standalone', [])
    bs_c = bundle.setdefault('annual_bs_consolidated', []); bs_s = bundle.setdefault('annual_bs_standalone', [])
    cf_c = bundle.setdefault('annual_cf_consolidated', []); cf_s = bundle.setdefault('annual_cf_standalone', [])
    ratios = bundle.setdefault('annual_ratios_filed', [])

    def upsert(arr, part, period):
        part = dict(part); part['period'] = period; part['_source'] = SOURCE_TAG
        for i, r0 in enumerate(arr):
            if r0.get('period') == period:
                if r0.get('_source') in PRIMARY_SOURCES:
                    return 0
                arr[i] = part; return 1
        arr.append(part); return 1

    stats = {'pl': 0, 'periods': sorted(by_period)}
    for period, bases in by_period.items():
        for basis, rec in bases.items():
            if basis == 'consolidated':
                stats['pl'] += upsert(pl_c, rec['pl'], period); upsert(bs_c, rec['bs'], period); upsert(cf_c, rec['cf'], period)
            else:
                stats['pl'] += upsert(pl_s, rec['pl'], period); upsert(bs_s, rec['bs'], period); upsert(cf_s, rec['cf'], period)
            if rec.get('ratios') and not any(x.get('period') == period for x in ratios):
                ratios.append({**rec['ratios'], 'period': period})

    for arr in (pl_c, pl_s, bs_c, bs_s, cf_c, cf_s, ratios):
        arr.sort(key=lambda r: r.get('period') or '')
    bundle['annual_pl'] = _merge_alias(pl_c, pl_s)
    bundle['annual_bs'] = _merge_alias(bs_c, bs_s)
    bundle['annual_cf'] = _merge_alias(cf_c, cf_s)
    bundle.setdefault('provenance', {})['last_nse_annual'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    if dry:
        return (sym, 'DRY', stats)
    if not backup_bundle(sym, raw):
        return (sym, 'BACKUP_FAIL', stats)
    if upload_bundle(sym, bundle):
        return (sym, 'OK', stats)
    return (sym, 'UPLOAD_FAIL', stats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', default='')
    ap.add_argument('--syms-file', dest='syms_file', default='')
    ap.add_argument('--since-fy', dest='since_fy', default='2016-03-31')
    ap.add_argument('--only-missing', action='store_true')
    ap.add_argument('--dry-run', dest='dry', action='store_true')
    a = ap.parse_args()
    if a.syms_file:
        syms = [s.strip().upper() for s in json.load(open(a.syms_file)) if s.strip()]
    else:
        syms = [s.strip().upper() for s in a.syms.split(',') if s.strip()]
    session = prime_nse_session()
    print(f"[INFO] {len(syms)} symbols  since_fy={a.since_fy}  only_missing={a.only_missing}  dry={a.dry}")
    agg = {}
    for i, sym in enumerate(syms, 1):
        try:
            _s, status, st = process_symbol(session, sym, a.since_fy, a.only_missing, a.dry)
        except Exception as e:
            status, st = 'ERR', {}
            print(f"  [{i}/{len(syms)}] {sym:14} ERR {str(e)[:90]}")
        agg[status] = agg.get(status, 0) + 1
        if status in ('OK', 'DRY', 'NO_NEW', 'NO_ROWS'):
            print(f"  [{i}/{len(syms)}] {sym:14} {status} pl+{st.get('pl', 0)} {st.get('periods', [])}")
        time.sleep(0.3)
    print("-" * 50)
    print(f"  {agg}")


if __name__ == '__main__':
    main()
