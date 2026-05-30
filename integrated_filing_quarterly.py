"""
integrated_filing_quarterly.py
==============================
PRIMARY-SOURCE backfill + cron for recent BANK quarterly results.

WHY
---
In 2025 SEBI/NSE moved listed companies to the "Integrated Filing (Financials)"
format. The legacy `corporates-financial-results` XBRL feed (BANKING_*.xml) froze
around Q3 FY25 (Dec-2024). Everything after that was gap-filled from the Screener
*medallion* secondary source, which carries net_profit but NOT bank revenue
(Interest Earned) — so recent bank quarters show sales/revenue = NULL in the app.

The live data still exists on NSE under the new feed:
    GET https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=<SYM>
returns rows whose `xbrl` points at
    .../corporate/xbrl/INTEGRATED_FILING_BANKING_<...>_WEB.xml
one standalone + one consolidated file per quarter. Each carries InterestEarned,
OtherIncome, Income (=top line), InterestExpended, OperatingProfit..., PBT, tax,
net profit and EPS. This script downloads those, parses the *quarter* context
(matched to the filing's qe_Date), and writes the primary-source numbers into the
fundamentals-v2 bundle — overwriting the medallion gap rows.

Bank revenue convention (matches existing XBRL rows in the bundle):
    sales = Income  (== InterestEarned + OtherIncome)
The merged `quarterly_results` array uses the CONSOLIDATED basis when a consolidated
filing exists, else standalone (same as the historical rows).

SAFETY
------
  - Backs up every bundle to fundamentals-v2-backups/<date>/ before re-upload.
  - Only touches bank/financial bundles (is_bank_bundle).
  - Only writes periods the integrated filing actually covers; other rows untouched.
  - Idempotent: re-running overwrites the same periods with identical values.

Usage:
  py -3.11 integrated_filing_quarterly.py --syms HDFCBANK            # one bank
  py -3.11 integrated_filing_quarterly.py --syms HDFCBANK --dry-run  # parse, no upload
  py -3.11 integrated_filing_quarterly.py                            # all banks (backfill)
  py -3.11 integrated_filing_quarterly.py --since 2025-01-01         # only quarters >= date
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_new_filings import prime_nse_session
from bse_local_parser import parse_xbrl_text, num, to_crores
from fix_bank_quarterly_sales import (
    list_bundles, is_bank_bundle, ensure_backup_bucket, backup_bundle,
    upload_bundle, URL, HEADERS, BUCKET,
)

NSE_INT_FILING = 'https://www.nseindia.com/api/integrated-filing-results'
NSE_FIN_RESULTS = 'https://www.nseindia.com/api/corporates-financial-results'
WORKERS = 3            # NSE is rate-sensitive
SLEEP_BETWEEN = 0.4    # jitter between symbols
XBRL_SLEEP = 0.25      # between XBRL downloads within a symbol
SOURCE_TAG = 'nse_integrated_filing'
BANK_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_bank_universe.json')

NATURE_RE = re.compile(r'NatureOfReportStandaloneConsolidated[^>]*>\s*([A-Za-z]+)\s*<', re.IGNORECASE)


def detect_basis(raw_text: str) -> str:
    """'consolidated' | 'standalone' from the NatureOfReport tag; default standalone."""
    m = NATURE_RE.search(raw_text)
    if m:
        v = m.group(1).strip().lower()
        if v.startswith('cons'):
            return 'consolidated'
        if v.startswith('stand'):
            return 'standalone'
    return 'standalone'


def pick_quarter_ctx(contexts: dict, target_end: str):
    """Non-dimensional ~3-month period context whose END == target_end.
    If target_end is unknown (old endpoint has no qe_Date), pick the context with
    the LATEST end date — the current quarter always ends after any year-ago
    comparative quarter in the same filing."""
    exact = None
    fallback = None
    fb_end = ''
    for cid, c in contexts.items():
        if c.get('type') != 'period' or c.get('dim'):
            continue
        try:
            s = date.fromisoformat(c['start'])
            e = date.fromisoformat(c['end'])
        except Exception:
            continue
        days = (e - s).days
        if not (80 <= days <= 100):
            continue
        if target_end and c['end'] == target_end:
            exact = cid
        if c['end'] > fb_end:
            fb_end = c['end']
            fallback = cid
    return exact or fallback


def qe_to_iso(qe: str):
    """'31-MAR-2026' -> '2026-03-31'. None-safe."""
    if not qe:
        return None
    for fmt in ('%d-%b-%Y', '%d-%B-%Y', '%d-%b-%y'):
        try:
            from datetime import datetime
            return datetime.strptime(qe.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def g(facts, tag, ctx):
    if not ctx:
        return None
    v = (facts.get(tag) or {}).get(ctx)
    return to_crores(num(v)) if v is not None else None


def first(facts, ctx, *tags):
    for t in tags:
        v = g(facts, t, ctx)
        if v is not None:
            return v
    return None


def map_quarter(facts: dict, ctx: str, ftype: str) -> dict:
    """Map one quarter context to our quarterly_results row, across schemas:
    BANKING (scheduled banks), NBFC_INDAS (NBFCs/HFCs), and generic INTEGRATED_FILING.
    All three expose the `Income` tag as total income (top line)."""
    ie = g(facts, 'InterestEarned', ctx)
    oi = g(facts, 'OtherIncome', ctx)
    income = g(facts, 'Income', ctx)
    revops = g(facts, 'RevenueFromOperations', ctx)
    # Insurance (IRDA schema): no Income/RevenueFromOperations/InterestEarned — the
    # top line is OperatingIncome (premiums + investment income), profit is
    # ProfitLossAfterTax. Added so insurers (NIACL/GICRE/life cos) aren't dropped.
    opinc = g(facts, 'OperatingIncome', ctx)
    gross_prem = g(facts, 'GrossPremiumsWritten', ctx)
    # sales = total income. Prefer Income; else RevenueFromOperations; else ie+oi;
    # else insurance OperatingIncome (else gross premium as a last resort).
    if income is not None:
        sales = income
    elif revops is not None:
        sales = revops + (oi or 0)
    elif ie is not None or oi is not None:
        sales = (ie or 0) + (oi or 0)
    elif gross_prem:
        # Insurance top line = gross premium written (OperatingIncome is a small
        # net/residual figure). Only when it's a real >0 quarter value — some
        # insurers file premium only YTD, leaving 0 in the quarter context; we
        # don't ship a misleading 0, net_profit still captures the row.
        sales = gross_prem
    elif opinc:
        sales = opinc
    else:
        sales = None

    pbt = first(facts, ctx,
                'ProfitLossFromOrdinaryActivitiesBeforeTax', 'ProfitBeforeTax',
                'ProfitBeforeExceptionalItemsAndTax',
                'ProfitOrLossBeforeTax', 'ProfitOrLossBeforeExtraordinaryItems')  # insurance
    np_ = first(facts, ctx,
                'ProfitLossFromOrdinaryActivitiesAfterTax', 'ProfitLossForPeriod',
                'ProfitLossForThePeriod', 'ProfitLossForPeriodFromContinuingOperations',
                'ProfitOrLossAttributableToOwnersOfParent',
                'ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates',
                'ProfitLossAfterTax')  # insurance (IRDA)
    eps = first(facts, ctx,
                'BasicEarningsPerShareAfterExtraordinaryItems',
                'BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations',
                'BasicEarningsPerShareBeforeExtraordinaryItems',
                'BasicEarningsLossPerShareFromContinuingOperations',
                'BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized',
                'BasicAndDilutedEPSBeforeExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized')  # insurance
    deps = first(facts, ctx,
                 'DilutedEarningsPerShareAfterExtraordinaryItems',
                 'DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations',
                 'DilutedEarningsPerShareBeforeExtraordinaryItems')
    row = {
        'sales': round(sales, 2) if sales is not None else None,
        'total_income': round(sales, 2) if sales is not None else None,
        'revenue_from_operations': revops,
        'interest_earned': ie,
        'other_income': oi,
        'interest_expended': g(facts, 'InterestExpended', ctx),
        'finance_costs': g(facts, 'FinanceCosts', ctx),
        'operating_profit': first(facts, ctx, 'OperatingProfitBeforeProvisionAndContingencies'),
        'expenses': first(facts, ctx, 'ExpenditureExcludingProvisionsAndContingencies',
                          'Expenses', 'TotalExpenses'),
        'provisions_contingencies': g(facts, 'ProvisionsOtherThanTaxAndContingencies', ctx),
        'pbt': pbt,
        'tax_expense': g(facts, 'TaxExpense', ctx),
        'net_profit': np_,
        'eps': eps,
        'basic_eps': eps,
        'diluted_eps': deps,
        'gross_npa_pct': g(facts, 'PercentageOfGrossNpa', ctx),
        'net_npa_pct': g(facts, 'PercentageOfNpa', ctx),
        '_bank': ftype == 'bank',
        '_filing_type': ftype,
    }
    return {k: v for k, v in row.items() if v is not None}


def filing_type(xbrl_url: str) -> str:
    """Classify a financial-result XBRL by filename, across both naming schemes:
      new feed:   INTEGRATED_FILING_BANKING_*, INTEGRATED_FILING_NBFC_INDAS_*,
                  INTEGRATED_FILING_* (generic), INTEGRATED_FILING_GOVERNANCE_* (skip)
      old archive: BANKING_*, NBFC_INDAS_*, INDAS_* (generic)
    Returns 'bank' | 'nbfc' | 'generic' | '' (not a P&L financial filing)."""
    fn = xbrl_url.upper().rsplit('/', 1)[-1]
    if 'GOVERNANCE' in fn:
        return ''               # shareholding/governance — not P&L
    if 'BANKING' in fn:
        return 'bank'
    if 'NBFC' in fn:
        return 'nbfc'
    if 'INTEGRATED_FILING' in fn or 'INDAS' in fn:
        return 'generic'
    return ''


def fetch_filings(sym: str, session, historical: bool = False) -> list:
    """Return [(period_iso, xbrl_url, audited, ftype)] for a symbol's financial filings.

    Always pulls the new integrated-filing feed (>= ~Mar-2025). When `historical`,
    also pulls the legacy corporates-financial-results archive (~2023-2024, old
    BANKING_*/NBFC_INDAS_*/INDAS_* XBRL). qe_Date is only present on the new feed;
    old rows pass period_iso=None and rely on pick_quarter_ctx's latest-end logic."""
    out = []
    seen = set()
    # New feed
    try:
        r = session.get(NSE_INT_FILING, params={'index': 'equities', 'symbol': sym}, timeout=25)
        if r.status_code == 200:
            for rec in (r.json() or {}).get('data') or []:
                x = rec.get('xbrl') or ''
                ft = filing_type(x)
                if ft and x not in seen:
                    seen.add(x)
                    out.append((qe_to_iso(rec.get('qe_Date')), x, rec.get('audited'), ft))
    except Exception:
        pass
    # Legacy archive (historical backfill only)
    if historical:
        try:
            r = session.get(NSE_FIN_RESULTS,
                            params={'index': 'equities', 'symbol': sym, 'period': 'Quarterly'},
                            timeout=25)
            if r.status_code == 200:
                for rec in (r.json() or []):
                    x = rec.get('xbrl') or ''
                    if not x or x.endswith('/-'):
                        continue
                    ft = filing_type(x)
                    if ft and x not in seen:
                        seen.add(x)
                        out.append((None, x, rec.get('audited'), ft))
        except Exception:
            pass
    return out


def download_bundle(sym: str) -> dict:
    r = requests.get(f'{URL}/storage/v1/object/public/{BUCKET}/{sym}.json', timeout=30)
    return r.json() if r.status_code == 200 else None


def upsert_period(arr: list, period: str, fields: dict, audited):
    """Merge fields into the row for `period` (create if absent). Returns 'added'/'updated'."""
    fields = dict(fields)
    fields['period'] = period
    fields['_source'] = SOURCE_TAG
    fields['_audited'] = (audited or '').lower().startswith('aud') if audited else None
    # Drop stale medallion markers
    for i, row in enumerate(arr):
        if row.get('period') == period:
            merged = dict(row)
            merged.update(fields)
            merged.pop('_sales_unavailable_reason', None)
            arr[i] = merged
            return 'updated'
    arr.append(fields)
    return 'added'


def process_symbol(sym: str, session, since_iso: str, dry: bool, historical: bool = False):
    filings = fetch_filings(sym, session, historical=historical)
    if not filings:
        return (sym, 'NO_FILINGS', {})

    # Restrict to one schema per symbol so a stray generic disclosure never
    # pollutes a bank/NBFC. Priority: bank > nbfc > generic.
    types = {f[3] for f in filings}
    dominant = 'bank' if 'bank' in types else ('nbfc' if 'nbfc' in types else 'generic')
    filings = [f for f in filings if f[3] == dominant]

    # period_iso -> {basis: (row_fields, audited)}
    by_period = {}
    for period_iso, url, audited, ftype in filings:
        try:
            txt = session.get(url, timeout=30).text
        except Exception:
            continue
        time.sleep(XBRL_SLEEP)
        facts, ctxs = parse_xbrl_text(txt)
        ctx = pick_quarter_ctx(ctxs, period_iso)
        if not ctx:
            continue
        eff_period = ctxs[ctx]['end']  # authoritative quarter-end from the context
        if since_iso and eff_period < since_iso:
            continue
        basis = detect_basis(txt)
        row = map_quarter(facts, ctx, ftype)
        if not row.get('sales') and not row.get('net_profit'):
            continue
        # If two filings of the same basis+period exist (revisions), keep the later
        # one encountered (NSE returns newest first, so don't overwrite an existing).
        by_period.setdefault(eff_period, {})
        by_period[eff_period].setdefault(basis, (row, audited))

    if not by_period:
        return (sym, 'NO_QUARTERS', {})

    bundle = download_bundle(sym)
    if not bundle:
        return (sym, 'NO_BUNDLE', {})
    raw_bytes = json.dumps(bundle, default=str, separators=(',', ':')).encode('utf-8')

    std = bundle.setdefault('quarterly_results_standalone', [])
    con = bundle.setdefault('quarterly_results_consolidated', [])
    merged = bundle.setdefault('quarterly_results', [])

    stats = {'std': 0, 'con': 0, 'merged': 0, 'periods': sorted(by_period)}
    for period, bases in by_period.items():
        if 'standalone' in bases:
            row, aud = bases['standalone']
            upsert_period(std, period, row, aud)
            stats['std'] += 1
        if 'consolidated' in bases:
            row, aud = bases['consolidated']
            upsert_period(con, period, row, aud)
            stats['con'] += 1
        # merged: prefer consolidated, else standalone
        pick = bases.get('consolidated') or bases.get('standalone')
        if pick:
            row, aud = pick
            upsert_period(merged, period, row, aud)
            stats['merged'] += 1

    for arr in (std, con, merged):
        arr.sort(key=lambda r: r.get('period') or '')
    bundle.setdefault('provenance', {})['last_integrated_filing_quarterly'] = time.strftime(
        '%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    if dry:
        return (sym, 'DRY', stats)
    if not backup_bundle(sym, raw_bytes):
        return (sym, 'BACKUP_FAIL', stats)
    if upload_bundle(sym, bundle):
        return (sym, 'OK', stats)
    return (sym, 'UPLOAD_FAIL', stats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', type=str, default='')
    ap.add_argument('--since', type=str, default='2025-01-01',
                    help='Only write quarters with period >= this ISO date (default 2025-01-01)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, default=WORKERS)
    ap.add_argument('--use-cache', action='store_true',
                    help='Read the bank universe from _bank_universe.json instead of '
                         'rescanning all bundles (for the cron). Full run rebuilds it.')
    ap.add_argument('--historical', action='store_true',
                    help='Also pull the legacy corporates-financial-results archive '
                         '(~2023-2024) to fill pre-2025 quarters. Pair with --since 2022-01-01.')
    ap.add_argument('--all', action='store_true', dest='all_syms',
                    help='Process the FULL fundamentals-v2 universe (banks + non-banks), '
                         'skipping bank detection. Upgrades non-bank quarters from medallion '
                         'to primary XBRL. Heavy run (~4.5k symbols) — use as a one-shot.')
    args = ap.parse_args()

    use_cache = args.use_cache and not args.syms and os.path.exists(BANK_CACHE)
    if args.syms:
        candidates = [s.strip().upper() for s in args.syms.split(',') if s.strip()]
    elif use_cache:
        with open(BANK_CACHE) as f:
            candidates = json.load(f)
        print(f'[INFO] Loaded {len(candidates)} banks from cache {BANK_CACHE}')
    else:
        print('[INFO] Listing fundamentals-v2 bundles ...')
        candidates = list_bundles()
        candidates = [s for s in candidates
                      if not (s.startswith('BSE') and s[3:].isdigit())]
    if args.limit:
        candidates = candidates[:args.limit]
    print(f'[INFO] {len(candidates)} candidate symbols (banks auto-detected)')

    if not args.dry_run:
        if not ensure_backup_bucket():
            print('[ERR] backup bucket unavailable', file=sys.stderr)
            sys.exit(1)

    session = prime_nse_session()

    # When scanning the full universe, filter to banks first (cheap bundle check).
    # --all skips this filter and processes every symbol (banks + non-banks).
    if not args.syms and not use_cache and not args.all_syms:
        print('[INFO] Detecting bank bundles ...')
        banks = []

        def is_bank(sym):
            try:
                b = download_bundle(sym)
                return sym if (b and is_bank_bundle(b)) else None
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=8) as ex:
            for i, fut in enumerate(as_completed({ex.submit(is_bank, s): s for s in candidates}), 1):
                r = fut.result()
                if r:
                    banks.append(r)
                if i % 500 == 0:
                    print(f'  scanned {i}/{len(candidates)} found {len(banks)} banks')
        candidates = sorted(banks)
        # Cache the detected universe so the cron can skip the full rescan.
        try:
            with open(BANK_CACHE, 'w') as f:
                json.dump(candidates, f)
            print(f'[INFO] Cached {len(candidates)} banks -> {BANK_CACHE}')
        except OSError:
            pass
        print(f'[INFO] {len(candidates)} bank bundles to process')

    ok = no_f = no_q = no_b = err = 0
    t0 = time.time()

    def safe(sym):
        try:
            res = process_symbol(sym, session, args.since, args.dry_run, args.historical)
            time.sleep(SLEEP_BETWEEN)
            return res
        except Exception as e:
            return (sym, f'EXC:{e}', {})

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(safe, s): s for s in candidates}
        for i, fut in enumerate(as_completed(futs), 1):
            sym, status, stats = fut.result()
            if status in ('OK', 'DRY'):
                ok += 1
                ps = stats.get('periods', [])
                print(f'  [{i}/{len(candidates)}] {sym:<14} {status}  '
                      f'std+{stats.get("std",0)} con+{stats.get("con",0)} '
                      f'merged+{stats.get("merged",0)}  {ps}')
            elif status == 'NO_FILINGS':
                no_f += 1
            elif status == 'NO_QUARTERS':
                no_q += 1
            elif status == 'NO_BUNDLE':
                no_b += 1
            else:
                err += 1
                print(f'  [{i}/{len(candidates)}] {sym:<14} {status}', file=sys.stderr)

    print('\n' + '-' * 60)
    print(f'  processed/updated : {ok}')
    print(f'  no banking filings: {no_f}')
    print(f'  no quarters parsed: {no_q}')
    print(f'  no bundle         : {no_b}')
    print(f'  errors            : {err}')
    print(f'  elapsed           : {time.time()-t0:.1f}s')


if __name__ == '__main__':
    main()
