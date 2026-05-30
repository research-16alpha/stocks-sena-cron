"""
fix_bank_quarterly_sales.py
===========================
Post-merge transform for bank/financial bundles in `fundamentals-v2`.

BACKGROUND
----------
The most recent quarterly rows in bank bundles (e.g. HDFCBANK 2025-06-30 ..) come
from the Screener.in *medallion* dataset (`_source == 'screener_medallion'`). Those
rows have `interest`, `other_income`, `net_profit`, `eps`, `expenses`, `pbt` populated
but `sales`/`interest_earned`/`bank_total_income` == null. The earlier BSE/MCA XBRL
parser fix (bank revenue = interest_earned + other_income) never reached them.

FINDING (cross-checked against live data, all classes — see audit below)
------------------------------------------------------------------------
For every bundle that actually has the medallion sales-gap, medallion's `interest`
field == INTEREST **EXPENDED** (a finance cost), NOT interest earned / top line.

Proof (source-boundary continuity + magnitude):
  * Scheduled banks (HDFCBANK, SBIN, PSU banks, KOTAK, RBL, YES — 15 bundles):
    in every XBRL quarter carrying both fields, `interest` is byte-identical to
    `interest_expended` and ~half of `interest_earned` (HDFCBANK 2024-12-31:
    interest=46914 == expended=46914; earned=85040). The medallion series
    continues the expended series across the boundary (BANKBARODA XBRL 2024-12-31
    expended=20143 -> medallion 2025-03-31 interest=20273). It never jumps to the
    earned magnitude.
  * NBFCs/HFCs with no self-disambiguating XBRL breakdown (SUNDARMFIN, MANAPPURAM,
    IREDA, AADHARHFC, INDIASHLTR, CAPTRUST, TFCILTD — 7 bundles): same boundary
    test confirms expended. MANAPPURAM medallion 2024-12-31 interest=925 ->
    XBRL 2025-03-31 interest=925-ish (cost), earned=2339, sales=2360. Deriving
    interest+other_income would be ~4x too low.
  * Broking/distribution "EARN-class" finance co's (ALMONDZ, VERTEX, PRUDENT, ...):
    these match interest==interest_earned ONLY because both are tiny vs their real
    revenue (fee/brokerage). Their `sales` (RevenueFromOperations) is already
    populated and they have ZERO medallion sales-gap rows. Deriving would corrupt
    them (PRUDENT derived=7.5 vs actual sales=249). So they are never touched.

CONSEQUENCE
-----------
Bank/financial quarterly REVENUE cannot be derived from medallion `interest` (it's a
cost line, not top-line). `sales = interest + other_income` would badly understate
revenue and corrupt the data. Therefore this transform does NOT populate `sales` on
ANY medallion financial quarter. It leaves `sales` null and verifies `net_profit`
(the number users care most about) is present. A marker `_sales_unavailable_reason`
is stamped on affected rows so the UI/audits know the gap is intentional, not a
parser miss.

If a future medallion field for interest *earned* / total income appears, revisit.

SAFETY
------
  - Backs up every bundle to `fundamentals-v2-backups` BEFORE any re-upload.
  - Only touches bank/financial stocks (detect via `_is_bank` flag or
    interest_earned / net_interest_income presence in the bundle).
  - Never writes a wrong `sales`. Conservative by construction.
  - --audit-only just prints the cross-check, touches nothing.

Usage:
  py -3.11 fix_bank_quarterly_sales.py --audit-only --syms HDFCBANK,ICICIBANK
  py -3.11 fix_bank_quarterly_sales.py --syms HDFCBANK,ICICIBANK   # test (still uploads)
  py -3.11 fix_bank_quarterly_sales.py                            # full batch
  py -3.11 fix_bank_quarterly_sales.py --dry-run                  # scan, no upload
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not KEY:
    with open('e:/Stocks sena/.supabase-service-key', 'r') as f:
        KEY = f.read().strip()
URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')

BUCKET = 'fundamentals-v2'
BACKUP_BUCKET = 'fundamentals-v2-backups'
WORKERS = 8

HEADERS = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

QUARTERLY_KEYS = [
    'quarterly_results',
    'quarterly_results_standalone',
    'quarterly_results_consolidated',
]

# Marker stamped on medallion bank quarters whose `sales` we intentionally leave null.
UNAVAIL_REASON = 'bank_quarterly_revenue_not_in_medallion'


# ----------------------------------------------------------------------------- IO
def list_bundles():
    files, offset = [], 0
    while True:
        r = requests.post(
            f'{URL}/storage/v1/object/list/{BUCKET}',
            headers={**HEADERS, 'Content-Type': 'application/json'},
            json={'prefix': '', 'limit': 1000, 'offset': offset,
                  'sortBy': {'column': 'name', 'order': 'asc'}},
            timeout=30,
        )
        if r.status_code != 200:
            print(f'[ERR] list failed: {r.status_code} {r.text}', file=sys.stderr)
            break
        batch = r.json()
        if not batch:
            break
        files.extend([b['name'][:-5] for b in batch if b.get('name', '').endswith('.json')])
        if len(batch) < 1000:
            break
        offset += 1000
    return files


def fetch_bundle(sym):
    url = f'{URL}/storage/v1/object/public/{BUCKET}/{sym}.json'
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f'fetch {sym}: {r.status_code}')
    return r.json()


def ensure_backup_bucket():
    r = requests.get(f'{URL}/storage/v1/bucket/{BACKUP_BUCKET}', headers=HEADERS, timeout=15)
    if r.status_code == 200:
        return True
    r = requests.post(
        f'{URL}/storage/v1/bucket',
        headers={**HEADERS, 'Content-Type': 'application/json'},
        json={'id': BACKUP_BUCKET, 'name': BACKUP_BUCKET, 'public': False},
        timeout=15,
    )
    return r.status_code in (200, 409)


# M11: one backup folder per PROCESS (date + HHMMSS), set once at import. Within a
# run all symbols share it; separate runs (e.g. the quarterly cron then the annual
# cron on the same day) get DIFFERENT folders, so the annual step no longer
# overwrites the quarterly step's pre-run backup at the same daily path.
_BACKUP_RUN_STAMP = time.strftime('%Y%m%d_%H%M%S', time.gmtime())


def backup_bundle(sym, raw_bytes):
    """Store a frozen copy in fundamentals-v2-backups/<date_HHMMSS>/<sym>.json before overwrite."""
    path = f'{BACKUP_BUCKET}/{_BACKUP_RUN_STAMP}/{sym}.json'
    h = {**HEADERS, 'Content-Type': 'application/json', 'x-upsert': 'true'}
    r = requests.post(f'{URL}/storage/v1/object/{path}', headers=h, data=raw_bytes, timeout=30)
    if r.status_code in (200, 201):
        return True
    r = requests.put(f'{URL}/storage/v1/object/{path}', headers=h, data=raw_bytes, timeout=30)
    return r.status_code in (200, 201)


def upload_bundle(sym, bundle):
    payload = json.dumps(bundle, default=str, separators=(',', ':')).encode('utf-8')
    h = {**HEADERS, 'Content-Type': 'application/json', 'x-upsert': 'true'}
    upload_url = f'{URL}/storage/v1/object/{BUCKET}/{sym}.json'
    r = requests.post(upload_url, headers=h, data=payload, timeout=30)
    if r.status_code in (200, 201):
        return True
    r = requests.put(upload_url, headers=h, data=payload, timeout=30)
    return r.status_code in (200, 201)


# ------------------------------------------------------------------------- logic
def all_quarter_rows(bundle):
    for k in QUARTERLY_KEYS:
        for r in (bundle.get(k) or []):
            yield k, r


def is_bank_bundle(bundle):
    """Bank/financial detection: _is_bank flag, or interest_earned / net_interest_income
    present anywhere in the bundle's rows."""
    for _, r in all_quarter_rows(bundle):
        if r.get('interest_earned') is not None or r.get('net_interest_income') is not None:
            return True
    for k in ('annual_pl', 'annual_bs', 'annual_pl_standalone', 'annual_pl_consolidated',
              'annual_bs_standalone', 'annual_bs_consolidated'):
        for r in (bundle.get(k) or []):
            if r.get('_is_bank') or r.get('interest_earned') is not None \
               or r.get('net_interest_income') is not None:
                return True
    return False


def audit_interest_meaning(bundle):
    """Within one bundle, classify XBRL quarters that carry `interest` AND
    (interest_earned or interest_expended): does interest match expended or earned?
    Returns (n_expended, n_earned, n_ambiguous)."""
    n_exp = n_earn = n_amb = 0

    def close(a, b):
        return a is not None and b is not None and abs(a - b) <= max(1.0, abs(b) * 0.01)

    for _, r in all_quarter_rows(bundle):
        i = r.get('interest')
        if i is None:
            continue
        ie, ix = r.get('interest_earned'), r.get('interest_expended')
        if close(i, ix) and not close(i, ie):
            n_exp += 1
        elif close(i, ie) and not close(i, ix):
            n_earn += 1
        else:
            n_amb += 1
    return n_exp, n_earn, n_amb


def transform(bundle):
    """Apply the conservative fix. Returns (changed_bool, stats).

    Action: for bank medallion quarters with sales==null, we DO NOT derive sales
    (medallion `interest` is a cost, not top-line). We stamp a marker so the gap is
    explicit, and we record net_profit presence.
    """
    changed = False
    med_bank_q = 0
    np_present = 0
    np_missing = []
    stamped = 0

    for _, r in all_quarter_rows(bundle):
        if r.get('_source') != 'screener_medallion':
            continue
        # bank quarter = has interest but no top-line revenue tags
        looks_bank_q = (r.get('interest') is not None
                        and r.get('sales') is None
                        and r.get('interest_earned') is None
                        and r.get('bank_total_income') is None)
        if not looks_bank_q:
            continue
        med_bank_q += 1
        if r.get('net_profit') is not None:
            np_present += 1
        else:
            np_missing.append(r.get('period'))
        # Leave sales null on purpose; stamp marker if not already present.
        if r.get('_sales_unavailable_reason') != UNAVAIL_REASON:
            r['_sales_unavailable_reason'] = UNAVAIL_REASON
            stamped += 1
            changed = True

    return changed, {
        'medallion_bank_quarters': med_bank_q,
        'net_profit_present': np_present,
        'net_profit_missing_periods': np_missing,
        'rows_stamped': stamped,
    }


def before_after_report(sym, bundle):
    print(f'\n----- {sym}: medallion bank quarters (before fix) -----')
    print(f'{"period":<12}{"sales":>10}{"interest":>12}{"int_earned":>12}{"other_inc":>12}{"net_profit":>12}{"eps":>8}')
    for _, r in all_quarter_rows(bundle):
        if r.get('_source') != 'screener_medallion' or r.get('interest') is None:
            continue
        if r.get('sales') is not None or r.get('interest_earned') is not None:
            continue

        def g(k):
            v = r.get(k)
            return '' if v is None else (f'{v:.0f}' if isinstance(v, (int, float)) else str(v))
        print(f'{r.get("period",""):<12}{g("sales"):>10}{g("interest"):>12}'
              f'{g("interest_earned"):>12}{g("other_income"):>12}{g("net_profit"):>12}{g("eps"):>8}')


# -------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', type=str, default='')
    ap.add_argument('--audit-only', action='store_true',
                    help='Print interest-meaning cross-check only; touch nothing.')
    ap.add_argument('--dry-run', action='store_true', help='Scan + report, no upload.')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    if args.syms:
        syms = [s.strip().upper() for s in args.syms.split(',') if s.strip()]
    else:
        print('[INFO] Listing fundamentals-v2 ...')
        syms = list_bundles()
    if args.limit:
        syms = syms[:args.limit]
    print(f'[INFO] Candidate symbols: {len(syms)}')

    if not args.audit_only and not args.dry_run:
        if not ensure_backup_bucket():
            print('[ERR] could not ensure backup bucket', file=sys.stderr)
            sys.exit(1)

    # Aggregate audit across all banks for the global interest-meaning verdict.
    agg_exp = agg_earn = agg_amb = 0
    banks = 0
    updated = 0
    np_total = 0
    np_missing_all = []
    errors = []

    def process(sym):
        raw = requests.get(f'{URL}/storage/v1/object/public/{BUCKET}/{sym}.json', timeout=30)
        if raw.status_code != 200:
            return ('ERR', sym, f'fetch {raw.status_code}', None, None, None)
        raw_bytes = raw.content
        bundle = json.loads(raw_bytes)
        if not is_bank_bundle(bundle):
            return ('SKIP', sym, None, None, None, None)
        a = audit_interest_meaning(bundle)
        if args.audit_only:
            return ('AUDIT', sym, None, a, None, raw_bytes)
        changed, stats = transform(bundle)
        if args.dry_run:
            return ('DRY', sym, None, a, stats, raw_bytes)
        # back up first, then upload only if changed
        if not backup_bundle(sym, raw_bytes):
            return ('ERR', sym, 'backup failed', a, stats, raw_bytes)
        if changed:
            if not upload_bundle(sym, bundle):
                return ('ERR', sym, 'upload failed', a, stats, raw_bytes)
            return ('UPDATED', sym, None, a, stats, raw_bytes)
        return ('NOCHANGE', sym, None, a, stats, raw_bytes)

    # Test symbols: print before/after detail.
    detail_syms = set(s.strip().upper() for s in args.syms.split(',')) if args.syms else \
        {'HDFCBANK', 'ICICIBANK'}

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(process, s): s for s in syms}
        for i, fut in enumerate(as_completed(futs), 1):
            status, sym, msg, a, stats, raw_bytes = fut.result()
            if status == 'SKIP':
                continue
            banks += 1
            if a:
                agg_exp += a[0]; agg_earn += a[1]; agg_amb += a[2]
            if stats:
                np_total += stats['net_profit_present']
                np_missing_all += [(sym, p) for p in stats['net_profit_missing_periods']]
            if status == 'UPDATED':
                updated += 1
            if status == 'ERR':
                errors.append((sym, msg))
            if sym in detail_syms and raw_bytes is not None:
                before_after_report(sym, json.loads(raw_bytes))
                if a:
                    print(f'      interest==expended:{a[0]}  interest==earned:{a[1]}  ambiguous:{a[2]}')
                if stats:
                    print(f'      medallion_bank_quarters={stats["medallion_bank_quarters"]} '
                          f'net_profit_present={stats["net_profit_present"]} '
                          f'rows_stamped={stats["rows_stamped"]}')
                    print('      AFTER: sales left NULL by design (medallion interest = expended, '
                          'a cost; cannot derive revenue). net_profit retained.')
            if i % 200 == 0:
                print(f'  [{i}/{len(syms)}] banks={banks} updated={updated}')

    print('\n' + '=' * 64)
    print('GLOBAL INTEREST-MEANING CROSS-CHECK (XBRL quarters w/ both fields)')
    print(f'  interest == interest_EXPENDED : {agg_exp}')
    print(f'  interest == interest_EARNED   : {agg_earn}')
    print(f'  ambiguous (no breakdown)      : {agg_amb}')
    verdict = 'EXPENDED (a cost)' if agg_exp > agg_earn else \
              ('EARNED (top line)' if agg_earn > agg_exp else 'INCONCLUSIVE')
    print(f'  VERDICT: medallion `interest` = {verdict}')
    print('=' * 64)
    print(f'  bank bundles scanned : {banks}')
    print(f'  bundles updated      : {updated}')
    print(f'  medallion bank-quarter net_profit present : {np_total}')
    if np_missing_all:
        print(f'  net_profit MISSING on {len(np_missing_all)} medallion bank quarters:')
        for s, p in np_missing_all[:20]:
            print(f'     {s} {p}')
    if errors:
        print(f'  errors: {len(errors)}')
        for s, m in errors[:10]:
            print(f'     {s}: {m}')


if __name__ == '__main__':
    main()
