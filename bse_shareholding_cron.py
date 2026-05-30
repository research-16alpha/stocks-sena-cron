"""
bse_shareholding_cron.py
========================
Primary-source SHAREHOLDING PATTERN (promoter / FII / DII / public / govt %) from
BSE for stocks NSE's shareholding feed doesn't carry. Third leg of the NSE->BSE
coverage extension (after quarters + annuals). ~1,766 active BSE-only/NSE-gap
stocks had ZERO shareholding data; this fills promoter_pct + fii/dii/public.

SOURCE (reverse-engineered from BSE's shp-latest Angular app, 2026-05-30)
  1. Quarter list + XBRL attachments:
     api.bseindia.com/BseIndiaAPI/api/Corp_Shareholding_ng/w
       ?scripcode=<code>&flag=<any qtrid>&indtype=
     -> {"Table":[{sQtrName, nqtrid, EndDate, IsXBRL, XBRLAttachment, ...}]}
     (flag must be non-empty; the current qtrid comes from Corp_shpSec_shpqtrinfo_ng
      .table1[0].fld_quarterid. Any valid qtrid returns the FULL quarter history.)
  2. Each XBRLAttachment ("/XBRLFILES/SHPXBRLDataXML/<code>_..._SP.html") is a SEBI
     iXBRL in BSE's `in-bse-shp` taxonomy. The category % we need is the fact
     `in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares` keyed by context:
       ShareholdingOfPromoterAndPromoterGroup_ContextI -> promoters
       PublicShareholding_ContextI                     -> public
       InstitutionsForeign_ContextI                    -> fii  (FPI Cat I+II+others)
       InstitutionsDomestic_ContextI                   -> dii  (MF+insurance+banks+...)
       Governments_ContextI                            -> government
     Validated on RELIANCE: promoter 50.00 / public 50.00 / FII 18.67 / DII 20.55.

MERGE
  Writes rows {period(YYYY-MM-DD), promoters, public, fii, dii, government,
  _source:'bse_shp'} into bundle.shareholding (dedup by period, sorted). Then
  compute_metrics rolls promoter_pct/fii_pct/dii_pct from shareholding[-1].

USAGE
  py -3.11 bse_shareholding_cron.py --syms RELIANCE,HAWKINCOOK --quarters 4 --dry-run
  py -3.11 bse_shareholding_cron.py --missing-promoter --quarters 4 --workers 8
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
from fix_bank_quarterly_sales import (
    ensure_backup_bucket, backup_bundle, upload_bundle, URL, HEADERS, BUCKET,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SYMIDS_FILE = os.path.join(HERE, '_symbol_identifiers.json')
SOURCE_TAG = 'bse_shp'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
SHP_LIST = 'https://api.bseindia.com/BseIndiaAPI/api/Corp_Shareholding_ng/w'
SHP_QTRINFO = 'https://api.bseindia.com/BseIndiaAPI/api/Corp_shpSec_shpqtrinfo_ng/w'
BSE_FILE_BASE = 'https://www.bseindia.com'
WORKERS = 8

# in-bse-shp context -> our field
CTX_MAP = {
    'ShareholdingOfPromoterAndPromoterGroup_ContextI': 'promoters',
    # NOTE: PublicShareholding = ALL non-promoter (= FII + DII + retail), so it's
    # 100% for promoter-less companies. The app's "public" means RETAIL / non-
    # institutional public (the residual), so promoters+fii+dii+govt+public ≈ 100.
    # Map "public" to NonInstitutions; keep PublicShareholding only as a total.
    'NonInstitutions_ContextI': 'public',
    'PublicShareholding_ContextI': 'public_total',
    'InstitutionsForeign_ContextI': 'fii',
    'InstitutionsDomestic_ContextI': 'dii',
    'Governments_ContextI': 'government',
}
PCT_TAG = 'ShareholdingAsAPercentageOfTotalNumberOfShares'
# nonFraction fact: name + contextRef (order varies) -> value
_FACT_RE = re.compile(
    r'<(?:ix:)?nonFraction[^>]*?name=["\']in-bse-shp:' + PCT_TAG +
    r'["\'][^>]*?contextRef=["\']([^"\']+)["\'][^>]*?>([^<]*)<', re.I)
_FACT_RE2 = re.compile(  # contextRef before name
    r'<(?:ix:)?nonFraction[^>]*?contextRef=["\']([^"\']+)["\'][^>]*?name=["\']in-bse-shp:' + PCT_TAG +
    r'["\'][^>]*?>([^<]*)<', re.I)

_local = threading.local()


def session():
    s = getattr(_local, 's', None)
    if s is None:
        s = requests.Session()
        s.headers.update({'User-Agent': UA, 'Referer': 'https://www.bseindia.com/',
                          'Origin': 'https://www.bseindia.com',
                          'Accept': 'application/json, text/plain, */*'})
        try:
            s.get('https://www.bseindia.com/', timeout=20)
        except Exception:
            pass
        _local.s = s
    return s


def latest_qtrid(scrip):
    s = session()
    try:
        r = s.get(SHP_QTRINFO, params={'scripcode': scrip}, timeout=20)
        t1 = (r.json() or {}).get('table1') or []
        if t1:
            return str(t1[0].get('fld_quarterid') or '').split('.')[0]
    except Exception:
        pass
    return None


def fetch_quarters(scrip):
    """[(period_iso, xbrl_url)] newest first, only IsXBRL rows."""
    qid = latest_qtrid(scrip)
    if not qid:
        return []
    s = session()
    try:
        r = s.get(SHP_LIST, params={'scripcode': scrip, 'flag': qid, 'indtype': ''}, timeout=25)
        rows = (r.json() or {}).get('Table') or []
    except Exception:
        return []
    out = []
    for row in rows:
        if str(row.get('IsXBRL')) != '1':
            continue
        att = row.get('XBRLAttachment') or ''
        end = (row.get('EndDate') or row.get('DisplayDT') or '')[:10]
        if not att or not end:
            continue
        url = att if att.startswith('http') else BSE_FILE_BASE + att
        out.append((end, url))
    # newest first (EndDate desc)
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def parse_shp(text):
    """Extract category %s from a BSE in-bse-shp iXBRL doc."""
    vals = {}
    for rx in (_FACT_RE, _FACT_RE2):
        for ctx, val in rx.findall(text):
            field = CTX_MAP.get(ctx)
            if not field or field in vals:
                continue
            try:
                vals[field] = round(float(val.strip().replace(',', '')), 2)
            except (ValueError, TypeError):
                pass
    # SEBI SHP omits the promoter context entirely when promoter holding is 0
    # (professionally-managed cos, e.g. VCU = 100% public). Treat absent-promoter
    # + present-public as a genuine 0%, not missing data.
    if 'promoters' not in vals and vals.get('public') is not None:
        vals['promoters'] = 0.0
    return vals


def process_symbol(sym, scrip, n_quarters, dry):
    quarters = fetch_quarters(scrip)
    if not quarters:
        return (sym, 'NO_SHP', {})
    s = session()
    rows = []
    for period, url in quarters[:n_quarters]:
        try:
            txt = s.get(url, timeout=40).text
        except Exception:
            continue
        time.sleep(0.03)
        vals = parse_shp(txt)
        if not vals.get('promoters') and not vals.get('public'):
            continue
        rows.append({'period': period, '_source': SOURCE_TAG, **vals})
    if not rows:
        return (sym, 'NO_PARSE', {})

    r = requests.get(f'{URL}/storage/v1/object/public/{BUCKET}/{sym}.json', timeout=30)
    if r.status_code != 200:
        return (sym, 'NO_BUNDLE', {})
    bundle = r.json()
    raw = json.dumps(bundle, default=str, separators=(',', ':')).encode('utf-8')

    sh = bundle.get('shareholding')
    if not isinstance(sh, list):
        sh = []
    by_period = {x.get('period'): x for x in sh if isinstance(x, dict)}
    added = 0
    for row in rows:
        p = row['period']
        if p in by_period and by_period[p].get('_source') == SOURCE_TAG:
            by_period[p].update(row)
        elif p not in by_period:
            by_period[p] = row
            added += 1
        else:
            by_period[p].update(row)   # upgrade a non-bse_shp/legacy row
    merged = [by_period[p] for p in sorted(by_period) if p]
    bundle['shareholding'] = merged
    bundle.setdefault('provenance', {})['last_bse_shp'] = time.strftime(
        '%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    stats = {'rows': len(rows), 'added': added,
             'latest': rows[0]['period'], 'promoter': rows[0].get('promoters')}
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
            base = [s.strip().upper() for s in json.load(f) if s.strip()]
    elif args.syms:
        base = [s.strip().upper() for s in args.syms.split(',') if s.strip()]
    else:
        rows = []
        off = 0
        while True:
            r = requests.get(f'{URL}/rest/v1/stock_master?select=symbol,promoter_pct'
                             f'&is_active=eq.true&limit=1000&offset={off}', headers=HEADERS, timeout=30)
            b = r.json()
            if not b:
                break
            rows += b
            off += 1000
            if len(b) < 1000:
                break
        if args.missing_promoter:
            base = [r['symbol'] for r in rows if r.get('promoter_pct') is None]
        else:
            base = [r['symbol'] for r in rows]
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', type=str, default='')
    ap.add_argument('--syms-file', dest='syms_file', type=str, default='')
    ap.add_argument('--missing-promoter', action='store_true',
                    help='target only active stocks with NULL promoter_pct')
    ap.add_argument('--all', dest='all_syms', action='store_true',
                    help='target the FULL active universe — upgrades EVERY stock to '
                         'primary BSE SHP, including ones currently on secondary (screener) data')
    ap.add_argument('--quarters', type=int, default=4, help='how many recent SHP quarters to pull')
    ap.add_argument('--workers', type=int, default=WORKERS)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    # Default to FULL universe (primary for everyone) unless explicitly --missing-promoter.
    if not args.syms and not args.syms_file and not args.missing_promoter:
        args.all_syms = True

    if not os.path.exists(SYMIDS_FILE):
        print(f'[ERR] {SYMIDS_FILE} missing — run build_identifier_master.py first', file=sys.stderr)
        sys.exit(1)
    symids = json.load(open(SYMIDS_FILE))

    targets = load_targets(args, symids)
    pairs = [(s, symids[s]['bse_scrip']) for s in targets
             if s in symids and symids[s].get('bse_scrip')]
    if args.limit:
        pairs = pairs[:args.limit]
    print(f'[INFO] targets={len(targets)}  with BSE scrip={len(pairs)}  '
          f'quarters={args.quarters}  workers={args.workers}  dry={args.dry_run}')

    if not args.dry_run and not ensure_backup_bucket():
        print('[ERR] backup bucket', file=sys.stderr)
        sys.exit(1)

    ok = added = noshp = noparse = nob = err = 0
    t0 = time.time()

    def safe(p):
        s, sc = p
        try:
            return process_symbol(s, sc, args.quarters, args.dry_run)
        except Exception as e:
            return (s, f'EXC:{type(e).__name__}:{e}', {})

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(safe, p): p for p in pairs}
        for i, fut in enumerate(as_completed(futs), 1):
            sym, status, stats = fut.result()
            if status in ('OK', 'DRY'):
                ok += 1
                added += stats.get('added', 0)
                if ok <= 25 or ok % 100 == 0:
                    print(f'  [{i}/{len(pairs)}] {sym:<14} {status} +{stats.get("added",0)}q '
                          f'latest={stats.get("latest")} promoter={stats.get("promoter")}')
            elif status == 'NO_SHP':
                noshp += 1
            elif status == 'NO_PARSE':
                noparse += 1
            elif status == 'NO_BUNDLE':
                nob += 1
            else:
                err += 1
                if err <= 20:
                    print(f'  {sym}: {status}', file=sys.stderr)
            if i % 200 == 0:
                print(f'  ...[{i}/{len(pairs)}] ok={ok} added_rows={added} elapsed={time.time()-t0:.0f}s')

    print('\n' + '-' * 60)
    print(f'  updated bundles   : {ok} (+{added} shp periods)')
    print(f'  no SHP filings    : {noshp}')
    print(f'  no parse          : {noparse}')
    print(f'  no bundle         : {nob}')
    print(f'  errors            : {err}')
    print(f'  elapsed           : {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
