"""
bse_results_delta_cron.py
=========================
DAILY ongoing-freshness catcher. Replaces the broken delta path
(fetch_new_filings -> apply_new_filings), which fetched announcement PDFs and
applied 0 because PDFs aren't parseable.

This instead asks BSE which companies FILED RESULTS in the recent window and
pulls their STRUCTURED XBRL (the same primary source as the backfill crons), so
new quarters + annuals actually land. BSE captures ~all filers (dual-listed), and
NSE-primary rows are protected by the quarterly cron's fill guard, so this one
delta keeps the whole market current.

SOURCE
  Corp_FinanceResult_ng?SCRIP_CD=&FlagDur=<1|2|3>&HFQ=&ISUBGROUP_CODE=
    FlagDur 1=Today, 2=Last Week (default, safe overlap), 3=Last 15 Days.
  -> rows of {Scrip_cd, quarter_code, XMLName, Consol_XMLName} for every filer.
  Resolve Scrip_cd -> our symbol via stock_master.bse_scrip_code, then reuse:
    bse_quarterly_cron.process_symbol  (quarters)
    bse_annual_cron.process_symbol     (annual P&L+BS+CF, from the M* rows)

The workflow runs compute_metrics --ttm afterwards to roll the new data into
ROE/ROCE/book value/promoter% and latest_* periods.

USAGE
  py -3.11 bse_results_delta_cron.py --flagdur 2 --dry-run
  py -3.11 bse_results_delta_cron.py --flagdur 2 --workers 8
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bse_quarterly_cron import session, BSE_API
import bse_quarterly_cron as bq
import bse_annual_cron as ba
from fix_bank_quarterly_sales import ensure_backup_bucket, URL, HEADERS

WORKERS = 8


def recent_filers(flagdur):
    """{scrip_code: True} for every company that filed results in the window."""
    s = session()
    scrips = set()
    try:
        r = s.get(BSE_API, params={'SCRIP_CD': '', 'FlagDur': flagdur,
                                   'HFQ': '', 'ISUBGROUP_CODE': ''}, timeout=40)
        for row in (r.json() or {}).get('Table') or []:
            sc = row.get('Scrip_cd')
            if sc:
                scrips.add(str(sc))
    except Exception as e:
        print(f'[ERR] recent_filers: {e}', file=sys.stderr)
    return scrips


def scrip_to_symbol():
    """Reverse map from stock_master.bse_scrip_code -> symbol (active)."""
    out = {}
    off = 0
    while True:
        r = requests.get(f'{URL}/rest/v1/stock_master?select=symbol,bse_scrip_code'
                         f'&bse_scrip_code=not.is.null&limit=1000&offset={off}',
                         headers=HEADERS, timeout=30)
        b = r.json()
        if not b:
            break
        for row in b:
            sc = str(row.get('bse_scrip_code') or '')
            if sc and sc not in out:
                out[sc] = row['symbol']
        off += 1000
        if len(b) < 1000:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--flagdur', default='2', choices=['1', '2', '3'],
                    help='1=today, 2=last week (default), 3=last 15 days')
    ap.add_argument('--since', default='2025-01-01', help='quarters: only write period >= this')
    ap.add_argument('--since-fy', dest='since_fy', default='2024-03-31',
                    help='annual: only write FY-end >= this (recent FYs only on the delta)')
    ap.add_argument('--workers', type=int, default=WORKERS)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    scrips = recent_filers(args.flagdur)
    print(f'[INFO] recent filers (FlagDur={args.flagdur}): {len(scrips)} scrips')
    # H2: a "last week" window returning ZERO filers means the BSE source is
    # down/throttled — fail loud instead of exiting 0 green-but-empty, so the
    # monitor + freshness-assert catch it rather than silently ingesting nothing.
    if not scrips and args.flagdur in ('2', '3') and not args.dry_run:
        print('[ERR] 0 recent filers from BSE for a multi-day window — source outage, failing loud',
              file=sys.stderr)
        sys.exit(1)
    s2sym = scrip_to_symbol()
    pairs = [(s2sym[sc], sc) for sc in scrips if sc in s2sym]
    if args.limit:
        pairs = pairs[:args.limit]
    print(f'[INFO] resolved to {len(pairs)} of our symbols  workers={args.workers}  dry={args.dry_run}')

    if not args.dry_run and not ensure_backup_bucket():
        print('[ERR] backup bucket', file=sys.stderr)
        sys.exit(1)

    q_ok = a_ok = q_per = a_per = err = 0
    t0 = time.time()

    def work(pair):
        sym, scrip = pair
        res = {'sym': sym}
        try:
            _, qs, qstats = bq.process_symbol(sym, scrip, args.since, ['6'], args.dry_run)
            res['q'] = (qs, qstats)
        except Exception as e:
            res['q'] = (f'EXC:{type(e).__name__}', {})
        try:
            _, as_, astats = ba.process_symbol(sym, scrip, args.since_fy, ['6'], args.dry_run)
            res['a'] = (as_, astats)
        except Exception as e:
            res['a'] = (f'EXC:{type(e).__name__}', {})
        return res

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, p) for p in pairs]
        done = 0
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            qs, qstats = r['q']
            as_, astats = r['a']
            if qs in ('OK', 'DRY'):
                q_ok += 1
                q_per += qstats.get('merged', 0)
            elif qs.startswith('EXC'):
                err += 1
            if as_ in ('OK', 'DRY'):
                a_ok += 1
                a_per += astats.get('pl', 0)
            elif as_.startswith('EXC'):
                err += 1
            if q_ok <= 25 and qs in ('OK', 'DRY') and qstats.get('merged'):
                print(f"  {r['sym']:<14} q+{qstats.get('merged',0)} a+{astats.get('pl',0)}  "
                      f"{qstats.get('periods', [])[-2:]}")

    print('\n' + '-' * 60)
    print(f'  filers resolved   : {len(pairs)}')
    print(f'  quarters updated  : {q_ok} bundles (+{q_per} periods)')
    print(f'  annuals  updated  : {a_ok} bundles (+{a_per} periods)')
    print(f'  errors            : {err}')
    print(f'  elapsed           : {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
