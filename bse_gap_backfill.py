"""
bse_gap_backfill.py
===================
Heals INTERNAL quarter holes that the --stale path can't see. The stale cron only
checks whether a stock's LATEST quarter is behind; a stock can be fully current at
the tail yet be missing a quarter in the MIDDLE (e.g. BSE was down the day it
filed Q2-2024). Those old internal holes never get re-pulled, because:
  - --stale skips the stock (its latest period is current), and
  - the daily delta's FlagDur=6 window only reaches back ~1 year.

This scans every active stock's quarterly_results for gaps in the expected
Jun/Sep/Dec/Mar sequence BETWEEN its earliest and latest present quarter, then
re-pulls FULL history (FlagDur 6+7) for the stocks with holes and merges
fill-don't-clobber. Quarters BSE genuinely doesn't have (pre-IPO, etc.) are left
absent — harmless.

USAGE
  py -3.11 bse_gap_backfill.py --dry-run            # just report the gap set
  py -3.11 bse_gap_backfill.py --workers 10          # detect + backfill
  py -3.11 bse_gap_backfill.py --min-hole 2025-01-01 # only heal holes >= a date
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bse_quarterly_cron as bq
from fix_bank_quarterly_sales import ensure_backup_bucket, URL, HEADERS

SYMIDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_symbol_identifiers.json')


def quarter_ends(start_iso, end_iso):
    """All Jun30/Sep30/Dec31/Mar31 ends in [start, end] inclusive."""
    qs = []
    y0 = int(start_iso[:4]) - 1
    y1 = int(end_iso[:4]) + 1
    for y in range(y0, y1 + 1):
        for md in ('06-30', '09-30', '12-31', '03-31'):
            d = f'{y}-{md}'
            if start_iso <= d <= end_iso:
                qs.append(d)
    return sorted(qs)


def find_holes(d, min_hole):
    """Internal missing quarters between earliest and latest present quarter."""
    q = sorted(set(r.get('period') for r in (d.get('quarterly_results') or []) if r.get('period')))
    if len(q) < 2:
        return []
    expected = quarter_ends(q[0], q[-1])
    present = set(q)
    holes = [e for e in expected if e not in present]
    if min_hole:
        holes = [h for h in holes if h >= min_hole]
    return holes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=10)
    ap.add_argument('--min-hole', dest='min_hole', default='2023-01-01',
                    help='only heal holes on/after this date (older history is sparse anyway)')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    symids = json.load(open(SYMIDS_FILE))

    # active universe
    rows = []
    off = 0
    while True:
        r = requests.get(f'{URL}/rest/v1/stock_master?select=symbol&is_active=eq.true'
                         f'&limit=1000&offset={off}', headers=HEADERS, timeout=30)
        b = r.json()
        if not b:
            break
        rows += b
        off += 1000
        if len(b) < 1000:
            break
    syms = [r['symbol'] for r in rows]
    print(f'[INFO] scanning {len(syms)} active stocks for internal quarter holes '
          f'(>= {args.min_hole})...')

    # detect holes (parallel bundle reads)
    def scan(sym):
        try:
            d = requests.get(f'{URL}/storage/v1/object/public/{bq.BUCKET}/{sym}.json', timeout=20).json()
        except Exception:
            return None
        holes = find_holes(d, args.min_hole)
        return (sym, holes) if holes else None

    hole_stocks = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for f in as_completed([ex.submit(scan, s) for s in syms]):
            r = f.result()
            if r:
                hole_stocks.append(r)
    print(f'[INFO] {len(hole_stocks)} stocks have internal holes')

    # keep only those with a resolved BSE scrip
    targets = [(s, symids[s]['bse_scrip'], holes) for s, holes in hole_stocks
               if s in symids and symids[s].get('bse_scrip')]
    if args.limit:
        targets = targets[:args.limit]
    print(f'[INFO] {len(targets)} have a BSE scrip → backfillable  (workers={args.workers})')

    if args.dry_run:
        for s, sc, holes in sorted(targets, key=lambda x: -len(x[2]))[:30]:
            print(f'  {s:<14} {len(holes)} holes  {holes[:6]}')
        print(f'\n[dry-run] would re-pull full history for {len(targets)} stocks')
        return

    if not ensure_backup_bucket():
        print('[ERR] backup bucket', file=sys.stderr)
        sys.exit(1)

    ok = filled = noq = err = 0
    t0 = time.time()

    def heal(t):
        sym, scrip, holes = t
        since = min(holes)
        try:
            # FlagDur 6+7 = full history; since = earliest hole so we reach it
            _, status, stats = bq.process_symbol(sym, scrip, since, ['6', '7'], False)
            return (sym, status, stats, len(holes))
        except Exception as e:
            return (sym, f'EXC:{type(e).__name__}', {}, len(holes))

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(heal, t) for t in targets]
        for i, f in enumerate(as_completed(futs), 1):
            sym, status, stats, nh = f.result()
            if status == 'OK':
                ok += 1
                filled += stats.get('merged', 0)
                if ok <= 25 or ok % 50 == 0:
                    print(f'  [{i}/{len(targets)}] {sym:<14} healed (had {nh} holes) +{stats.get("merged",0)}q')
            elif status in ('NO_QUARTERS', 'NO_ROWS'):
                noq += 1
            elif status.startswith('EXC'):
                err += 1
            if i % 100 == 0:
                print(f'  ...[{i}/{len(targets)}] healed={ok} +{filled}q elapsed={time.time()-t0:.0f}s')

    print('\n' + '-' * 60)
    print(f'  stocks healed     : {ok} (+{filled} periods merged)')
    print(f'  no quarters/rows  : {noq}')
    print(f'  errors            : {err}')
    print(f'  elapsed           : {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
