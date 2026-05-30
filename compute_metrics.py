"""
compute_metrics.py
==================
Compute derived per-stock metrics from the fundamentals-v2 bundle's own
statements and write them into (a) the bundle snapshot and (b) stock_master
quality columns. Fixes the "ROE / book value / ROCE / scorecard blank" problem:
the inputs (equity, net_profit, borrowings, shareholding) already exist in the
bundles — they just were never computed for most stocks.

Metrics (consolidated preferred, else standalone):
  book_value  = total_equity * face_value / equity_capital           (per share)
  roe_pct     = net_profit / total_equity * 100
  roce_pct    = EBIT / (total_equity + borrowings) * 100   (EBIT = pbt + finance_costs)   [skip for banks]
  debt_equity = borrowings / total_equity                                                  [skip for banks]
  pb_ratio    = price / book_value
  ev_ebitda   = (mcap + borrowings - cash) / EBITDA        (only if mcap sane & ebitda>0)
  rev_5y_cagr / profit_5y_cagr  from annual_pl sales / net_profit (>=4y apart)
  piotroski_score (0-9)  from 2y of P&L+BS+CF
  promoter_pct / fii_pct / dii_pct / pledged_pct  from shareholding[latest]
  latest_annual_period / latest_quarter_period
  market_cap_cr recompute = equity_capital * price / face_value (sanity-gated)

VALIDATION: --validate <syms> prints computed vs existing snapshot values so we
never write a wrong number. Banks: ROE/book value computed; ROCE/D-E skipped.

Usage:
  py -3.11 compute_metrics.py --validate RELIANCE,TCS,HDFCBANK,HAWKINCOOK,MUKTAARTS
  py -3.11 compute_metrics.py --syms RELIANCE --dry-run
  py -3.11 compute_metrics.py                      # full universe
"""
import argparse
import json
import os
import sys
import time
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fix_bank_quarterly_sales import (
    list_bundles, ensure_backup_bucket, backup_bundle, upload_bundle,
    URL, HEADERS, BUCKET,
)

WORKERS = 8


def num(v):
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def latest(d, *keys):
    for k in keys:
        a = d.get(k)
        if a:
            return a[-1]
    return {}


def series(d, *keys):
    for k in keys:
        a = d.get(k)
        if a:
            return a
    return []


def g(row, *names):
    for n in names:
        v = row.get(n)
        if v is not None:
            return num(v)
    return None


def is_bank_bundle(d):
    for k in ('annual_bs', 'annual_pl', 'annual_bs_consolidated', 'annual_pl_consolidated'):
        for r in (d.get(k) or []):
            if r.get('_is_bank') or r.get('interest_earned') is not None:
                return True
    s = (d.get('snapshot') or [{}])
    return False


def sane(v, lo, hi):
    return v if (v is not None and lo <= v <= hi) else None


def pick_equity(bs):
    """Robust shareholders' equity. Some bundles have a corrupted `total_equity`
    (e.g. RELIANCE = 0.0002) while `equity_attributable_to_owners` is correct.
    Reject any candidate that's absurdly tiny vs paid-up capital. Banks store
    equity under bank_capital + bank_reserves_surplus."""
    ec = g(bs, 'equity_capital') or 0
    floor = abs(ec) * 0.01
    for v in (g(bs, 'equity_attributable_to_owners'), g(bs, 'total_equity')):
        if v is not None and abs(v) >= floor:
            return v
    # capital + reserves (incl. bank schema)
    cap = g(bs, 'equity_capital') or g(bs, 'bank_capital')
    rs = g(bs, 'reserves') or g(bs, 'bank_reserves_surplus')
    if rs is not None:
        return (cap or 0) + rs
    return None


def _qend(p):
    try:
        return date.fromisoformat(p)
    except (TypeError, ValueError):
        return None


def ttm_flows(d):
    """Trailing-twelve-months flow items = sum of the last 4 single-quarter rows.
    Returns a dict of summed flows (+ basis periods), or None if we don't have 4
    clean consecutive recent quarters. Balance-sheet items are never summed — TTM
    is a flow concept, so equity/borrowings still come from the latest annual BS.

    Guard: the 4 quarter-ends must span ~9 months (3 gaps × ~3mo); a wider span
    means a missing quarter (e.g. the Mar-2025 holes) and we fall back to annual
    rather than emit a wrong TTM."""
    qr = d.get('quarterly_results') or d.get('quarterly_results_consolidated') or []
    rows = [r for r in qr if r.get('period') and _qend(r['period'])]
    rows.sort(key=lambda r: r['period'])
    if len(rows) < 4:
        return None
    last4 = rows[-4:]
    span = (_qend(last4[-1]['period']) - _qend(last4[0]['period'])).days
    if not (250 <= span <= 400):           # 3 quarters apart ≈ 273d; reject gaps
        return None

    def s(*keys):
        vals = []
        for r in last4:
            v = g(r, *keys)
            if v is None:
                return None                 # need all 4 quarters for a clean sum
            vals.append(v)
        return round(sum(vals), 2)

    np_ttm = s('net_profit', 'pat_ordinary')
    if np_ttm is None:
        return None                          # net profit is the must-have
    return {
        'net_profit': np_ttm,
        'sales': s('sales', 'total_income', 'revenue_from_operations'),
        'pbt': s('pbt', 'pbt_ordinary'),
        'finance_costs': s('finance_costs', 'interest'),
        'operating_profit': s('operating_profit'),
        'depreciation': s('depreciation'),
        'period': last4[-1]['period'],
        'periods': [r['period'] for r in last4],
    }


def compute(d, use_ttm=False):
    """Return (metrics_dict, snapshot_patch_dict).

    use_ttm: when True and 4 clean recent quarters exist, ROE/ROCE/EV-EBITDA use
    trailing-twelve-month flows (net profit, PBT, finance costs, operating profit)
    instead of the latest full-year P&L — so the ratios track the latest quarter.
    Balance-sheet inputs (equity, borrowings) stay annual either way. Falls back to
    annual per-field when a quarter lacks the tag, and entirely when <4 quarters."""
    bank = is_bank_bundle(d)
    # Prefer the merged alias (consolidated-preferred PER PERIOD, kept fresh by the
    # parser + bse_annual_cron). A basis-specific array can be stale relative to the
    # alias — e.g. a company that stopped filing consolidated leaves a 2-year-old
    # annual_bs_consolidated that would otherwise shadow the current standalone year.
    bs = latest(d, 'annual_bs', 'annual_bs_consolidated', 'annual_bs_standalone')
    pl = latest(d, 'annual_pl', 'annual_pl_consolidated', 'annual_pl_standalone')
    pl_series = series(d, 'annual_pl', 'annual_pl_consolidated', 'annual_pl_standalone')
    bs_series = series(d, 'annual_bs', 'annual_bs_consolidated', 'annual_bs_standalone')
    cf = latest(d, 'annual_cf', 'annual_cf_consolidated', 'annual_cf_standalone')
    snap = (d.get('snapshot') or [{}])[0] if isinstance(d.get('snapshot'), list) and d.get('snapshot') else (d.get('snapshot') or {})
    price = g(snap, 'current_price', 'price')
    face = g(snap, 'face_value') or 10.0

    # H9: a price is only valid for VALUATION ratios (pb / ev_ebitda / market_cap)
    # if it is (a) reasonably fresh and (b) sits over reasonably fresh fundamentals.
    # Otherwise we'd emit fake "live" ratios — e.g. a 2026 price over a 2006 book
    # value (WYETH pb=12.47). Stale/illiquid names (Yahoo NO_QUOTE freezes price)
    # get pb/ev/mcap left null rather than wrong. ROE/ROCE/book_value don't use
    # price and still compute. val_price=None disables only the price-derived set.
    _fd = snap.get('fetched_date') or snap.get('fetched_at') or snap.get('date')
    _latest_ann = (pl or {}).get('period')
    price_fresh = False
    if _fd:
        try:
            from datetime import date as _date
            price_fresh = (_date.today() - _date.fromisoformat(str(_fd)[:10])).days <= 7
        except Exception:
            price_fresh = False
    ann_fresh = bool(_latest_ann and str(_latest_ann)[:10] >= '2024-03-31')
    val_price = price if (price_fresh and ann_fresh) else None

    total_equity = pick_equity(bs)
    equity_capital = g(bs, 'equity_capital')
    borrowings = g(bs, 'borrowings')
    if borrowings is None:
        bc, bnc = g(bs, 'borrowings_current'), g(bs, 'borrowings_noncurrent')
        borrowings = (bc or 0) + (bnc or 0) if (bc is not None or bnc is not None) else None
    cash = (g(bs, 'cash_equivalents') or 0) + (g(bs, 'bank_balance') or 0)
    net_profit = g(pl, 'net_profit', 'pat_ordinary')
    pbt = g(pl, 'pbt', 'pbt_ordinary')
    fin = g(pl, 'finance_costs', 'interest')
    dep = g(pl, 'depreciation') or g(cf, 'depreciation_addback')
    op = g(pl, 'operating_profit')
    sales = g(pl, 'sales', 'revenue_from_operations')

    # TTM override: swap full-year flows for the sum of the last 4 quarters.
    metrics_basis = 'annual'
    ttm = ttm_flows(d) if use_ttm else None
    if ttm:
        metrics_basis = 'ttm'
        net_profit = ttm['net_profit']
        if ttm['pbt'] is not None:
            pbt = ttm['pbt']
        if ttm['finance_costs'] is not None:
            fin = ttm['finance_costs']
        if ttm['operating_profit'] is not None:
            op = ttm['operating_profit']
        if ttm['sales'] is not None:
            sales = ttm['sales']
        if ttm['depreciation'] is not None:
            dep = ttm['depreciation']

    m = {}
    snap_patch = {}

    # Book value per share (gate: |bvps| < 1e6)
    bvps = None
    if total_equity is not None and equity_capital and equity_capital != 0 and face:
        cand = round(total_equity * face / equity_capital, 2)
        if abs(cand) < 1_000_000:
            bvps = cand
            snap_patch['book_value'] = bvps

    # ROE  (gate -100..200)
    if net_profit is not None and total_equity and total_equity > 0:
        roe = sane(round(net_profit / total_equity * 100, 2), -100, 200)
        if roe is not None:
            m['roe_pct'] = roe
            snap_patch['roe'] = roe

    # P/B (gate 0..5000) — only on a fresh price over fresh fundamentals (H9)
    if val_price and bvps and bvps > 0:
        pb = sane(round(val_price / bvps, 2), 0, 5000)
        if pb is not None:
            snap_patch['pb'] = pb
            m['pb_ratio'] = pb

    if not bank:
        # ROCE (gate -100..300)
        ebit = None
        if pbt is not None and fin is not None:
            ebit = pbt + fin
        elif op is not None:
            ebit = op
        if ebit is not None and total_equity is not None and borrowings is not None:
            ce = total_equity + borrowings
            if ce > 0:
                roce = sane(round(ebit / ce * 100, 2), -100, 300)
                if roce is not None:
                    m['roce_pct'] = roce
                    snap_patch['roce'] = roce
        # Debt/equity (gate 0..50)
        if borrowings is not None and total_equity and total_equity > 0:
            de = sane(round(borrowings / total_equity, 2), 0, 50)
            if de is not None:
                m['debt_equity'] = de
        # EV/EBITDA — only when the price (hence mcap) is fresh over fresh fundamentals (H9)
        ebitda = (op + dep) if (op is not None and dep is not None) else None
        mcap = g(snap, 'market_cap_cr') if val_price else None
        if (ebitda and ebitda > 0 and mcap and 1 < mcap < 2_000_000 and borrowings is not None):
            ev = mcap + borrowings - cash
            evb = sane(round(ev / ebitda, 2), 0, 200)
            if evb is not None:
                m['ev_ebitda'] = evb

    # CAGRs (5-point, 4y gap; gate -60..200)
    def cagr(key):
        vals = [g(r, key) for r in pl_series if g(r, key) is not None and g(r, key) > 0]
        if len(vals) >= 5:
            first, last = vals[-5], vals[-1]
            if first > 0:
                return sane(round(((last / first) ** (1 / 4) - 1) * 100, 2), -60, 200)
        return None
    rc = cagr('sales')
    pc = cagr('net_profit')
    if rc is not None:
        m['rev_5y_cagr'] = rc
    if pc is not None:
        m['profit_5y_cagr'] = pc

    # Piotroski (needs >=2 years of pl+bs+cf)
    pio = piotroski(pl_series, bs_series, series(d, 'annual_cf', 'annual_cf_consolidated', 'annual_cf_standalone'))
    if pio is not None:
        m['piotroski_score'] = pio

    # Shareholding rollup
    sh = d.get('shareholding')
    shr = sh[-1] if isinstance(sh, list) and sh else (sh if isinstance(sh, dict) else {})
    for col, key in (('promoter_pct', 'promoters'), ('fii_pct', 'fii'), ('dii_pct', 'dii'), ('pledged_pct', 'pledged')):
        v = g(shr, key)
        if v is not None:
            m[col] = round(v, 2)

    # Latest periods
    ap = series(d, 'annual_pl', 'annual_pl_consolidated', 'annual_pl_standalone')
    qr = d.get('quarterly_results') or []
    if ap:
        m['latest_annual_period'] = ap[-1].get('period')
        m['pl_years_count'] = len(ap)
    if qr:
        m['latest_quarter_period'] = qr[-1].get('period')
        m['quarters_count'] = len(qr)   # N2: recompute in lockstep so it can't drift

    # market cap recompute — only from a fresh price (H9; avoids reviving a stale
    # mcap for illiquid/delisted names whose Yahoo quote is frozen)
    if equity_capital and face and val_price:
        mc = round(equity_capital * val_price / face, 2)
        if 1 < mc < 2_000_000:
            m['market_cap_cr'] = mc

    if use_ttm:
        m['_basis'] = metrics_basis              # ignored by patch_stock_master (not a SM_COL)
        snap_patch['metrics_basis'] = metrics_basis
    return m, snap_patch


def piotroski(pl, bs, cf):
    """0-9 score using latest vs prior year. None if insufficient data."""
    if len(pl) < 2 or len(bs) < 2:
        return None
    p1, p0 = pl[-1], pl[-2]
    b1, b0 = bs[-1], bs[-2]
    c1 = cf[-1] if cf else {}
    ni1 = g(p1, 'net_profit'); ni0 = g(p0, 'net_profit')
    ta1 = g(b1, 'total_assets'); ta0 = g(b0, 'total_assets')
    cfo1 = g(c1, 'cfo')
    if None in (ni1, ta1) or not ta1:
        return None
    score = 0
    roa1 = ni1 / ta1
    # 1 ROA>0
    if roa1 > 0: score += 1
    # 2 CFO>0
    if cfo1 is not None and cfo1 > 0: score += 1
    # 3 dROA>0
    if ni0 is not None and ta0 and ta0 > 0:
        if roa1 > (ni0 / ta0): score += 1
    # 4 accruals: CFO>NI
    if cfo1 is not None and ni1 is not None and cfo1 > ni1: score += 1
    # 5 dLeverage<0 (LT debt/assets down)
    d1 = g(b1, 'borrowings_noncurrent'); d0 = g(b0, 'borrowings_noncurrent')
    if d1 is not None and d0 is not None and ta1 and ta0 and ta0 > 0:
        if (d1 / ta1) < (d0 / ta0): score += 1
    # 6 dCurrentRatio>0
    ca1, cl1 = g(b1, 'current_assets'), g(b1, 'current_liabilities')
    ca0, cl0 = g(b0, 'current_assets'), g(b0, 'current_liabilities')
    if all(x is not None for x in (ca1, cl1, ca0, cl0)) and cl1 and cl0 and cl1 > 0 and cl0 > 0:
        if (ca1 / cl1) > (ca0 / cl0): score += 1
    # 7 no new shares
    ec1, ec0 = g(b1, 'equity_capital'), g(b0, 'equity_capital')
    if ec1 is not None and ec0 is not None and ec1 <= ec0 + 0.01: score += 1
    # 8 dGrossMargin>0
    s1, s0 = g(p1, 'sales'), g(p0, 'sales')
    op1, op0 = g(p1, 'operating_profit'), g(p0, 'operating_profit')
    if all(x is not None for x in (s1, s0, op1, op0)) and s1 and s0 and s1 > 0 and s0 > 0:
        if (op1 / s1) > (op0 / s0): score += 1
    # 9 dAssetTurnover>0
    if s1 is not None and s0 is not None and ta1 and ta0 and ta0 > 0:
        if (s1 / ta1) > (s0 / ta0): score += 1
    return score


def download_bundle(sym):
    r = requests.get(f'{URL}/storage/v1/object/public/{BUCKET}/{sym}.json', timeout=30)
    return r.json() if r.status_code == 200 else None


SM_COLS = ['roe_pct', 'roce_pct', 'debt_equity', 'pb_ratio', 'ev_ebitda', 'rev_5y_cagr',
           'profit_5y_cagr', 'piotroski_score', 'promoter_pct', 'fii_pct', 'dii_pct',
           'pledged_pct', 'latest_annual_period', 'latest_quarter_period', 'market_cap_cr',
           'quarters_count', 'pl_years_count']  # N2: keep counts in sync with periods


def patch_stock_master(sym, m):
    payload = {k: m[k] for k in SM_COLS if k in m}
    if not payload:
        return True
    payload['quality_updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    import urllib.parse
    url = f"{URL}/rest/v1/stock_master?symbol=eq.{urllib.parse.quote(sym)}"
    r = requests.patch(url, headers={**HEADERS, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'},
                       data=json.dumps(payload), timeout=20)
    return r.status_code in (200, 204)


def process(sym, dry, use_ttm=False):
    d = download_bundle(sym)
    if not d:
        return (sym, 'NO_BUNDLE', 0)
    raw = json.dumps(d, default=str, separators=(',', ':')).encode('utf-8')
    m, snap_patch = compute(d, use_ttm=use_ttm)
    if not m and not snap_patch:
        return (sym, 'NO_METRICS', 0)
    ttm = (m.get('_basis') == 'ttm')

    # Fill-don't-overwrite: only set a snapshot field if the existing value is
    # missing or broken. Preserve sane existing (Screener) values — EXCEPT when a
    # TTM recompute is authoritative-current for roe/roce/pb, which then overwrite.
    snap = d.get('snapshot')
    s0 = snap[0] if isinstance(snap, list) and snap else (snap if isinstance(snap, dict) else None)
    changed = False

    def should_set(field, existing):
        if field == 'metrics_basis':
            return True                                        # always reflect this run's basis
        if ttm and field in ('roe', 'roce', 'pb', 'book_value'):
            return True   # TTM = authoritative recompute from primary annual; the
                          # roe/roce/pb/book_value set refreshes together (sanity-gated)
        if field == 'book_value':
            return existing in (None, 0, 0.0)                 # 0 = broken
        if field == 'pb':
            return existing is None or existing > 5000         # garbage P/B
        return existing is None                                # roe/roce: fill blanks only

    if s0 is not None:
        for f, v in snap_patch.items():
            if should_set(f, s0.get(f)):
                s0[f] = v
                changed = True
        if changed:
            s0['_metrics_recomputed'] = True
    elif snap_patch:
        d['snapshot'] = [{**snap_patch, '_metrics_recomputed': True}]
        changed = True
    if dry:
        return (sym, 'DRY', len(m) + len(snap_patch))
    if changed:
        backup_bundle(sym, raw)
        upload_bundle(sym, d)
    patch_stock_master(sym, m)
    return (sym, 'OK', len(m) + len(snap_patch))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--validate', type=str, default='')
    ap.add_argument('--compare', type=str, default='',
                    help='Print annual-vs-TTM ROE/ROCE side by side for these symbols (no writes).')
    ap.add_argument('--ttm', action='store_true',
                    help='Compute ROE/ROCE/EV-EBITDA from trailing-twelve-month (last 4 quarters) flows.')
    ap.add_argument('--syms', type=str, default='')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, default=WORKERS)
    args = ap.parse_args()

    if args.compare:
        for sym in [s.strip().upper() for s in args.compare.split(',') if s.strip()]:
            d = download_bundle(sym)
            if not d:
                print(f'{sym}: NO BUNDLE'); continue
            ma, _ = compute(d, use_ttm=False)
            mt, _ = compute(d, use_ttm=True)
            tf = ttm_flows(d)
            print(f'\n=== {sym} (bank={is_bank_bundle(d)})  basis={mt.get("_basis")} ===')
            print(f'  ROE   annual={ma.get("roe_pct")}   TTM={mt.get("roe_pct")}')
            print(f'  ROCE  annual={ma.get("roce_pct")}   TTM={mt.get("roce_pct")}')
            print(f'  EV/EBITDA annual={ma.get("ev_ebitda")}   TTM={mt.get("ev_ebitda")}')
            if tf:
                print(f'  TTM quarters: {tf["periods"]}  net_profit_ttm={tf["net_profit"]}  sales_ttm={tf["sales"]}')
            else:
                print('  (no clean 4-quarter TTM — falls back to annual)')
        return

    if args.validate:
        for sym in [s.strip().upper() for s in args.validate.split(',')]:
            d = download_bundle(sym)
            if not d:
                print(f'{sym}: NO BUNDLE'); continue
            m, sp = compute(d)
            snap = (d.get('snapshot') or [{}])[0] if isinstance(d.get('snapshot'), list) and d.get('snapshot') else {}
            print(f'\n=== {sym} (bank={is_bank_bundle(d)}) ===')
            print(f'  book_value : computed {sp.get("book_value")}   existing {snap.get("book_value")}')
            print(f'  roe        : computed {sp.get("roe")}   existing {snap.get("roe")}')
            print(f'  roce       : computed {sp.get("roce")}   existing {snap.get("roce")}')
            print(f'  pb         : computed {sp.get("pb")}   existing {snap.get("pb")}')
            print(f'  debt_equity: {m.get("debt_equity")}  ev_ebitda: {m.get("ev_ebitda")}')
            print(f'  rev_cagr5y : {m.get("rev_5y_cagr")}  profit_cagr5y: {m.get("profit_5y_cagr")}  piotroski: {m.get("piotroski_score")}')
            print(f'  promoter%  : {m.get("promoter_pct")}  fii%: {m.get("fii_pct")}  dii%: {m.get("dii_pct")}')
            print(f'  mcap recompute: {m.get("market_cap_cr")}')
        return

    if args.syms:
        syms = [s.strip().upper() for s in args.syms.split(',') if s.strip()]
    else:
        print('[INFO] listing bundles...')
        syms = list_bundles()
        # M5: skip the 688 orphan BSE_<scrip> bundles — they don't map to a
        # stock_master symbol (so are never displayed) and just waste processing.
        n_before = len(syms)
        syms = [s for s in syms if not s.startswith('BSE_')]
        if n_before != len(syms):
            print(f'[INFO] skipped {n_before - len(syms)} orphan BSE_ bundles')
    if args.limit:
        syms = syms[:args.limit]
    print(f'[INFO] {len(syms)} symbols')
    if not args.dry_run and not ensure_backup_bucket():
        print('[ERR] backup bucket', file=sys.stderr); sys.exit(1)

    ok = nm = nb = err = fields = 0
    t0 = time.time()

    def safe(s):
        try:
            return process(s, args.dry_run, use_ttm=args.ttm)
        except Exception as e:
            return (s, f'EXC:{e}', 0)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(safe, s): s for s in syms}
        for i, fut in enumerate(as_completed(futs), 1):
            sym, status, n = fut.result()
            if status in ('OK', 'DRY'):
                ok += 1; fields += n
            elif status == 'NO_METRICS':
                nm += 1
            elif status == 'NO_BUNDLE':
                nb += 1
            else:
                err += 1
                if err <= 20:
                    print(f'  {sym}: {status}', file=sys.stderr)
            if i % 500 == 0:
                print(f'  [{i}/{len(syms)}] ok={ok} fields={fields} rate={i/(time.time()-t0):.0f}/s')

    print('\n' + '-' * 60)
    print(f'  computed/patched : {ok}')
    print(f'  no metrics       : {nm}')
    print(f'  no bundle        : {nb}')
    print(f'  errors           : {err}')
    print(f'  elapsed          : {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
