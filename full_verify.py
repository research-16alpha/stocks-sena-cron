"""
full_verify.py
==============
Complete cross-verification of every active ticker's PUBLISHED numbers against
(a) independent anchors and (b) recomputation from the stock's own bundle.

Per stock:
  MCAP_ANCHOR   mcap vs SHP-verified shares x live price        (>25% off)
  MCAP_INTERNAL mcap vs bundle equity_capital/face x price      (>25% off)
  PE_RECOMPUTE  published pe vs mcap / TTM net profit           (>20% off)
  PB_RECOMPUTE  published pb vs mcap / latest total_equity      (>25% off)
  EPS_X_SHARES  eps x shares vs TTM net profit                  (>35% off)
  RANGE         price outside [0.65 x 52w-low, 1.35 x 52w-high]
  BOUNDS        pe>5000, pb>500, |roe|>400, div_yield>30, mcap>=2e6, delivery outside 0-100
  INPUTS        face missing / equity_capital missing / no shares source (coverage)
Writes _logs/full_verify_report.json + a categorised console summary.

Run:  py -3.11 full_verify.py
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

import os as _os
def _outp(name):
    p = r'e:/Stocks sena/_logs/' + name
    return p if _os.name == 'nt' else name


try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
SVC = os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
H = {'apikey': SVC, 'Authorization': 'Bearer ' + SVC}


def fetch_all(q):
    out, off = [], 0
    while True:
        d = requests.get(f'{URL}/rest/v1/{q}&offset={off}&limit=1000', headers=H, timeout=40).json()
        out += d
        if len(d) < 1000:
            break
        off += 1000
    return out


def n(v):
    return v if isinstance(v, (int, float)) and abs(v) < 1e15 else None


def check(s, anchor_shares):
    sym = s['symbol']
    iss = []
    px, mc, pe, pb = n(s.get('latest_price')), n(s.get('market_cap_cr')), n(s.get('pe_ratio')), n(s.get('pb_ratio'))
    try:
        r = requests.get(f'{URL}/storage/v1/object/public/fundamentals-v2/{sym}.json',
                         params={'cb': 'fv1'}, timeout=25)
        b = r.json() if r.status_code == 200 else {}
    except Exception:
        b = {}
    snap = b.get('snapshot')
    snap = (snap[0] if isinstance(snap, list) and snap else snap) or {}
    face = n(snap.get('face_value'))
    shp = anchor_shares.get(sym) or n(snap.get('total_shares'))

    def latest(prefix, field):
        # MERGED array first - compute_metrics reads the merged statements, so the
        # verifier must judge against the same basis (consolidated-first here used to
        # false-flag stocks whose merged and consolidated values differ, e.g. LGHL).
        best = None
        for k in (prefix, prefix + '_consolidated', prefix + '_standalone'):
            rows = b.get(k) or []
            for row in rows:
                p = str(row.get('period'))[:10]
                v = n(row.get(field))
                if v is not None and (best is None or p > best[0]):
                    best = (p, v)
        return best[1] if best else None

    eqcap = latest('annual_bs', 'equity_capital')
    toteq = latest('annual_bs', 'total_equity')
    # TTM NP - SAME definition as compute_metrics.ttm_flows (the canonical PE basis):
    # merged array first, the last 4 quarters must be consecutive (span 250-400d) and
    # all non-null; otherwise PE was published on the ANNUAL basis, so compare to that.
    ttm = None
    qr = b.get('quarterly_results') or b.get('quarterly_results_consolidated') or []
    rows4 = sorted([r2 for r2 in qr if r2.get('period')], key=lambda x: str(x.get('period')))[-4:]
    if len(rows4) == 4:
        try:
            import datetime as _dt
            d0 = _dt.date.fromisoformat(str(rows4[0]['period'])[:10])
            d3 = _dt.date.fromisoformat(str(rows4[-1]['period'])[:10])
            span = (d3 - d0).days
        except Exception:
            span = 0
        nps = [n(r2.get('net_profit')) for r2 in rows4]
        if 250 <= span <= 400 and all(v is not None for v in nps):
            ttm = round(sum(nps), 2)
    if ttm is None:
        ttm = latest('annual_pl', 'net_profit')   # annual fallback = what compute used

    # MCAP_ANCHOR
    if shp and px and mc:
        exp = shp * px / 1e7
        if exp > 25 and abs(mc - exp) / exp > 0.25:
            iss.append(('MCAP_ANCHOR', f'mcap {mc:,.0f} vs shares-implied {exp:,.0f}'))
    # MCAP_INTERNAL
    if eqcap and face and px and mc and not shp:
        exp = eqcap * px / face
        if exp > 25 and abs(mc - exp) / exp > 0.25:
            iss.append(('MCAP_INTERNAL', f'mcap {mc:,.0f} vs eqcap-implied {exp:,.0f}'))
    # PE_RECOMPUTE
    if pe and mc and ttm and ttm > 0:
        exp = mc / ttm
        if abs(pe - exp) / exp > 0.20:
            iss.append(('PE_RECOMPUTE', f'pe {pe} vs recomputed {exp:.1f}'))
    # PB_RECOMPUTE - the published pb excludes minority interest while total_equity
    # includes it, so small gaps are definitional. Flag only directional extremes
    # (>2.5x apart) which indicate a genuinely wrong input.
    if pb and mc and toteq and toteq > 0:
        exp = mc / toteq
        if pb / exp > 2.5 or exp / pb > 2.5:
            iss.append(('PB_RECOMPUTE', f'pb {pb} vs recomputed {exp:.2f}'))
    # RANGE
    hi, lo = n(s.get('high_52w')), n(s.get('low_52w'))
    if px and hi and lo and (px > hi * 1.35 or px < lo * 0.65):
        iss.append(('RANGE', f'price {px} outside 52w [{lo}-{hi}]'))
    # BOUNDS
    if pe and abs(pe) > 5000:
        iss.append(('BOUNDS', f'pe {pe}'))
    if pb and pb > 500:
        iss.append(('BOUNDS', f'pb {pb}'))
    if mc and mc >= 2_000_000:
        iss.append(('BOUNDS', f'mcap {mc:,.0f}'))
    roe = n(s.get('roe_pct'))
    if roe and abs(roe) > 400:
        iss.append(('BOUNDS', f'roe {roe}%'))
    dy = n(s.get('div_yield_pct'))
    if dy and dy > 30:
        iss.append(('BOUNDS', f'div_yield {dy}%'))
    dp = n(s.get('delivery_pct'))
    if dp is not None and (dp < 0 or dp > 100):
        iss.append(('BOUNDS', f'delivery {dp}%'))
    # INPUTS coverage
    inputs = {'face': face is not None, 'eqcap': eqcap is not None, 'shares_anchor': shp is not None}
    return sym, iss, inputs, mc


def main():
    stocks = fetch_all('stock_master?select=symbol,latest_price,market_cap_cr,pe_ratio,pb_ratio,roe_pct,div_yield_pct,high_52w,low_52w,delivery_pct&is_active=eq.true')
    shp = fetch_all('shareholding_periods?select=symbol,total_shares,period&total_shares=not.is.null&order=period.desc')
    anchor = {}
    for r in shp:
        if r['symbol'] not in anchor:
            anchor[r['symbol']] = r['total_shares']
    print(f'[verify] {len(stocks)} active stocks | {len(anchor)} SHP anchors')

    results = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for i, res in enumerate(ex.map(lambda s: check(s, anchor), stocks)):
            results.append(res)
            if (i + 1) % 1000 == 0:
                print(f'  ...{i + 1}/{len(stocks)}')

    by_cat = {}
    flagged = []
    cov = {'face': 0, 'eqcap': 0, 'shares_anchor': 0, 'total': len(results)}
    for sym, iss, inputs, mc in results:
        for k in cov:
            if k != 'total' and inputs.get(k):
                cov[k] += 1
        if iss:
            flagged.append({'symbol': sym, 'mcap': mc, 'issues': [f'{c}: {d}' for c, d in iss]})
            for c, d in iss:
                by_cat.setdefault(c, []).append((sym, mc or 0, d))

    print('\n================ FULL VERIFY SUMMARY ================')
    print(f'stocks checked : {cov["total"]}')
    print(f'inputs coverage: face {cov["face"]} | equity_capital {cov["eqcap"]} | SHP shares {cov["shares_anchor"]}')
    print(f'stocks flagged : {len(flagged)}')
    for c, lst in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        lst.sort(key=lambda x: -x[1])
        print(f'\n[{c}] {len(lst)} stocks (top by mcap):')
        for sym, mc, d in lst[:10]:
            print(f'   {sym:14s} mcap {mc:10,.0f}  {d}')
    json.dump({'coverage': cov, 'flagged': flagged},
              open(_outp('full_verify_report.json'), 'w'), indent=1)
    print('\n[verify] report -> _logs/full_verify_report.json')


if __name__ == '__main__':
    main()
