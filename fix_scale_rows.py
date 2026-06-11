"""
fix_scale_rows.py
=================
Systemic repair for the ACEINTEG bug class: a single annual row stored at the wrong
scale (raw rupees/thousands/lakhs instead of crores) inside an otherwise-sane series.

Detection (proof-based, no guessing):
  For every row in annual_bs*/annual_pl*/annual_cf*, compare each anchor field
  (equity_capital, total_equity, total_assets, sales/revenue, net_profit, expenses)
  to the MEDIAN of the same field across the stock's other rows (same array, plus the
  sibling basis arrays). A row is corrupt only if >= 2 anchor fields independently
  agree on the SAME clean power-of-ten factor K (1e3..1e8, within 3x tolerance).
  Fix = divide (or multiply, for the legacy /1e7 class) every numeric in that row by K.

Rows with no reference (single-row arrays, whole-series-wrong stocks like ZMILGFIN)
are NOT touched - they are listed for re-parse instead.

Run:  py -3.11 fix_scale_rows.py            # dry-run, writes _logs/scale_fix_plan.json
      py -3.11 fix_scale_rows.py --apply    # fix bundles + recompute metrics list
"""
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
SVC = os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
H = {'apikey': SVC, 'Authorization': 'Bearer ' + SVC}
STORE_H = {**H, 'x-upsert': 'true', 'Content-Type': 'application/json'}
APPLY = '--apply' in sys.argv
ONLY = [a for a in sys.argv[1:] if not a.startswith('-')]

ARRAYS = ['annual_bs', 'annual_bs_standalone', 'annual_bs_consolidated',
          'annual_pl', 'annual_pl_standalone', 'annual_pl_consolidated',
          'annual_cf', 'annual_cf_standalone', 'annual_cf_consolidated']
ANCHORS = ['equity_capital', 'total_equity', 'total_assets', 'sales',
           'revenue_from_operations', 'net_profit', 'expenses', 'total_liabilities_filed',
           'cash_from_operations', 'borrowings']
# alias fields mirror one underlying value - they corroborate as ONE vote, not two
ANCHOR_GROUP = {'sales': 'rev', 'revenue_from_operations': 'rev',
                'equity_capital': 'eqcap', 'total_equity': 'toteq', 'total_assets': 'assets',
                'net_profit': 'np', 'expenses': 'exp', 'total_liabilities_filed': 'liab',
                'cash_from_operations': 'cfo', 'borrowings': 'borrow'}
FACTORS = [10 ** k for k in range(3, 9)]


def sibling_arrays(key):
    base = key.rsplit('_standalone', 1)[0].rsplit('_consolidated', 1)[0]
    return [base, base + '_standalone', base + '_consolidated']


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


# Plausibility = min(country-level ceiling, per-stock mcap-relative ceiling).
# Country ceilings (Rs cr): HDFC Bank assets ~40L cr, Reliance sales ~9.6L cr,
# Vodafone Idea equity capital ~1.1L cr. Mcap-relative: a 5-cr microcap cannot have
# a 6.96-lakh-cr balance sheet (TRANOCE) even though that passes the country bound.
# Generous multiples so distressed-but-real cases never false-flag.
ABS_MAX = {'equity_capital': 2e5, 'total_equity': 1.2e6, 'total_assets': 5e6,
           'sales': 1.2e6, 'revenue_from_operations': 1.2e6, 'net_profit': 1.2e5,
           'expenses': 1.2e6, 'total_liabilities_filed': 5e6,
           'cash_from_operations': 2e5, 'borrowings': 1.5e6}
MCAP_MULT = {'equity_capital': 1000, 'total_equity': 1000, 'total_assets': 1000,
             'total_liabilities_filed': 1000, 'borrowings': 1000,
             'sales': 300, 'revenue_from_operations': 300, 'expenses': 300,
             'net_profit': 60, 'cash_from_operations': 60}
MCAP_FLOOR = {'net_profit': 200, 'cash_from_operations': 200,
              'sales': 500, 'revenue_from_operations': 500, 'expenses': 500}


def plausible(f, v, mcap=None):
    bound = ABS_MAX.get(f, 5e6)
    if mcap and mcap > 0:
        rel = max(MCAP_FLOOR.get(f, 1500), mcap * MCAP_MULT.get(f, 1000))
        bound = min(bound, rel)
    return abs(v) <= bound


def detect_row_factor(row, refs, mcap=None):
    """Returns (K, bad_fields, auto). A field is fixable only when BOTH hold:
      1. its value is off its PLAUSIBLE reference median by a clean power of ten, and
      2. direction is arbitrated by absolute bounds - the row value is implausible
         while plausible references exist (row corrupt), or the row value is tiny
         (<1/1000 of a plausible reference median; the legacy divide-bug class).
    References that violate absolute bounds are DISCARDED, never matched against."""
    votes = {}
    field_k = {}
    for f in ANCHORS:
        v = row.get(f)
        if not isinstance(v, (int, float)) or v == 0:
            continue
        rv = [abs(x) for x in refs.get(f, [])
              if isinstance(x, (int, float)) and x != 0 and plausible(f, x, mcap)]
        if len(rv) < 2:
            continue
        m = median(rv)
        if not m:
            continue
        ratio = abs(v) / m
        for K in FACTORS:
            too_big = (K / 3 <= ratio <= K * 3) and not plausible(f, v, mcap)
            too_small = (K / 3 <= (1 / ratio) <= K * 3)
            if too_big:
                votes.setdefault(K, set()).add(ANCHOR_GROUP[f])
                field_k[f] = K
            elif too_small:
                votes.setdefault(1.0 / K, set()).add(ANCHOR_GROUP[f])
                field_k[f] = 1.0 / K
    if not votes:
        return None, [], False
    K, groups = max(votes.items(), key=lambda kv: len(kv[1]))
    bad = [f for f, k in field_k.items() if k == K]
    if not bad:
        return None, [], False
    if K > 1 and len(groups) >= 2:
        return K, bad, True       # too-big + implausible + corroborated = provable
    if K > 1:
        return K, bad, False      # too-big + implausible, single field group: review
    # too-small (divide-bug class): direction can't use bounds, so demand an extreme
    # gap AND corroboration before auto-fixing; single-field goes to review.
    if K <= 1 / 10000 and len(groups) >= 2:
        return K, bad, True
    return K, bad, False


def scan_stock(sym, mcap=None):
    try:
        r = requests.get(f'{URL}/storage/v1/object/public/fundamentals-v2/{sym}.json', timeout=25)
        if r.status_code != 200:
            return None
        b = r.json()
    except Exception:
        return None
    fixes = []
    for key in ARRAYS:
        rows = b.get(key) or []
        if len(rows) < 1:
            continue
        for i, row in enumerate(rows):
            if row.get('source_format') == 'legacy_csv' or row.get('_scale_fixed'):
                continue  # legacy_csv rows have their own deterministic repairer
            period = str(row.get('period'))[:10]
            # reference pool: other rows of this array + same/adjacent periods in siblings
            refs = {}
            for f in ANCHORS:
                vals = [r2.get(f) for j, r2 in enumerate(rows) if j != i]
                for sk in sibling_arrays(key):
                    if sk == key:
                        continue
                    for r2 in (b.get(sk) or []):
                        if str(r2.get('period'))[:10] != period:  # other-basis other-years too
                            vals.append(r2.get(f))
                        elif abs((r2.get(f) or 0)) > 0:
                            vals.append(r2.get(f))
                refs[f] = [v for v in vals if isinstance(v, (int, float))]
            K, bad, auto = detect_row_factor(row, refs, mcap)
            if K and (K >= 1000 or K <= 1 / 1000) and bad:
                groups = len({ANCHOR_GROUP[f] for f in bad})
                # >=4 independent anchors off by the same K = the whole row is at the
                # wrong scale (ACEINTEG class) -> rescale every numeric. Fewer = only
                # the provably-bad fields (KALIND class: sales wrong, np fine).
                fixes.append({'key': key, 'period': period, 'factor': K,
                              'fields': bad, 'whole_row': groups >= 4, 'auto': auto})
    if not fixes:
        return None
    return {'symbol': sym, 'fixes': fixes, 'bundle': b if APPLY else None}


def main():
    if ONLY:
        syms = ONLY
    else:
        syms, off = [], 0
        while True:
            d = requests.get(f'{URL}/rest/v1/stock_master?select=symbol&is_active=eq.true'
                             f'&offset={off}&limit=1000', headers=H, timeout=30).json()
            syms += [x['symbol'] for x in d]
            if len(d) < 1000:
                break
            off += 1000
    print(f'[scale-fix] scanning {len(syms)} stocks ({"APPLY" if APPLY else "dry-run"})')

    mcaps, off = {}, 0
    while True:
        d = requests.get(f'{URL}/rest/v1/stock_master?select=symbol,market_cap_cr'
                         f'&offset={off}&limit=1000', headers=H, timeout=30).json()
        mcaps.update({x['symbol']: x.get('market_cap_cr') for x in d})
        if len(d) < 1000:
            break
        off += 1000

    found = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for i, res in enumerate(ex.map(lambda s2: scan_stock(s2, mcaps.get(s2)), syms)):
            if res:
                found.append(res)
            if (i + 1) % 500 == 0:
                print(f'  ...{i + 1}/{len(syms)} ({len(found)} flagged)')

    print(f'[scale-fix] {len(found)} stocks with provable wrong-scale rows')
    plan = []
    for f in found:
        for fx in f['fixes']:
            plan.append({'symbol': f['symbol'], **fx})
            tag = 'AUTO  ' if fx['auto'] else 'REVIEW'
            print(f"  {tag} {f['symbol']:14s} {fx['key']:28s} {fx['period']}  factor {fx['factor']:.0e}  {','.join(fx['fields'])[:50]}")

    json.dump(plan, open(r'e:/Stocks sena/_logs/scale_fix_plan.json', 'w'), indent=1)
    n_auto = sum(1 for p in plan if p['auto'])
    print(f'[scale-fix] plan: {n_auto} auto rows, {len(plan) - n_auto} review rows')
    if not APPLY:
        print(f'[scale-fix] dry-run -> _logs/scale_fix_plan.json')
        return

    fixed_syms = []
    for f in found:
        if not any(fx['auto'] for fx in f['fixes']):
            continue
        b = f['bundle']
        for fx in f['fixes']:
            if not fx['auto']:
                continue
            for row in (b.get(fx['key']) or []):
                if str(row.get('period'))[:10] == fx['period']:
                    K = fx['factor']
                    targets = (list(row.keys()) if fx.get('whole_row') else fx['fields'])
                    for fk in targets:
                        v = row.get(fk)
                        if isinstance(v, (int, float)):
                            row[fk] = round(v / K, 6)
                    row['_scale_fixed'] = (f"div {K:.0e} 2026-06-11 "
                                           f"({'whole row' if fx.get('whole_row') else ','.join(fx['fields'])})")
        mm = b.get('_meta') or {}
        mm['scale_row_fix'] = '2026-06-11 fix_scale_rows'
        b['_meta'] = mm
        r = requests.put(f'{URL}/storage/v1/object/fundamentals-v2/{f["symbol"]}.json',
                         headers=STORE_H, data=json.dumps(b), timeout=40)
        if r.status_code == 200:
            fixed_syms.append(f['symbol'])
    print(f'[scale-fix] applied to {len(fixed_syms)} bundles')
    open(r'e:/Stocks sena/_logs/scale_fixed_syms.txt', 'w').write(','.join(fixed_syms))


if __name__ == '__main__':
    main()
