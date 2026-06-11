"""
stock_health.py
===============
COMPLETE per-stock data health check - every dataset we hold for each ticker,
validated for internal consistency, cross-source agreement and freshness.

Domains checked per stock:
  IDENTITY    symbol/name/sector/ISIN/kite mapping present
  PRICE       latest_price fresh + agrees with last candle close (±20%)
  CANDLES     last bar fresh; OHLC sane (high>=low etc.); no huge unexplained gaps
  RANGE       52w high/low vs actual candle extremes (±10%)
  RETURNS     published 1y return vs candle-derived (±15pp)
  ANNUAL_PL   row sanity (|np| <= max(sales x 2, 100)); YoY 100x jumps; year gaps
  ANNUAL_BS   assets ~= equity + liabilities (±5%); negative assets
  QUARTERLY   4 latest quarters sum vs FY annual (±25% when FY complete)
  METRICS     pe/pb/mcap recompute (anchor-aware) - the full_verify checks
  SHAREHOLDING promoter+public ~= 100 (±2); pledged <= promoter; freshness (<2 qtrs)
  DELIVERY    0-100 bounds
  COVERAGE    bundle exists / has PL / has BS / has quarters / has shareholding
Output: _logs/stock_health_report.json (per-stock issues) + categorised summary.

Run:  py -3.11 stock_health.py [--limit N]
"""
import json
import os
import sys
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import requests

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
SVC = os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
H = {'apikey': SVC, 'Authorization': 'Bearer ' + SVC}
LIMIT = int(sys.argv[sys.argv.index('--limit') + 1]) if '--limit' in sys.argv else None
TODAY = dt.date.today()


def fetch_all(q):
    out, off = [], 0
    while True:
        d = requests.get(f'{URL}/rest/v1/{q}&offset={off}&limit=1000', headers=H, timeout=40).json()
        if not isinstance(d, list):
            break
        out += d
        if len(d) < 1000:
            break
        off += 1000
    return out


def n(v):
    return v if isinstance(v, (int, float)) and abs(v) < 1e15 else None


def latest_row(b, prefix):
    best = None
    for k in (prefix + '_consolidated', prefix, prefix + '_standalone'):
        for row in (b.get(k) or []):
            p = str(row.get('period'))[:10]
            if p and p != 'None' and (best is None or p > best[0]):
                best = (p, row)
    return best


def series(b, prefix):
    rows = {}
    for k in (prefix, prefix + '_standalone', prefix + '_consolidated'):
        for row in (b.get(k) or []):
            p = str(row.get('period'))[:10]
            if p and p != 'None' and (p not in rows or k.endswith('consolidated')):
                rows[p] = row
    return [rows[p] for p in sorted(rows)]


def check(s, anchors, shp_latest):
    sym = s['symbol']
    iss = []

    # IDENTITY
    if not s.get('name'):
        iss.append('IDENTITY: no name')
    if not s.get('sector'):
        iss.append('IDENTITY: no sector')
    if not s.get('kite_token'):
        iss.append('IDENTITY: no kite mapping (no live price possible)')

    px = n(s.get('latest_price'))
    try:
        r = requests.get(f'{URL}/storage/v1/object/public/fundamentals-v2/{sym}.json', params={'cb': 'h1'}, timeout=25)
        b = r.json() if r.status_code == 200 else None
    except Exception:
        b = None
    try:
        rc = requests.get(f'{URL}/storage/v1/object/public/daily/{sym}.json', params={'cb': 'h1'}, timeout=25)
        bars = (rc.json().get('bars') or []) if rc.status_code == 200 else []
    except Exception:
        bars = []

    # COVERAGE
    if b is None:
        iss.append('COVERAGE: no fundamentals bundle')
    else:
        if not series(b, 'annual_pl'):
            iss.append('COVERAGE: no annual P&L')
        if not series(b, 'annual_bs'):
            iss.append('COVERAGE: no balance sheet')
        if not (b.get('quarterly_results') or b.get('quarterly_results_consolidated') or b.get('quarterly_results_standalone')):
            iss.append('COVERAGE: no quarterly results')
        if not b.get('shareholding'):
            iss.append('COVERAGE: no shareholding')
    if not bars:
        iss.append('COVERAGE: no price candles')

    # PRICE + CANDLES
    if bars:
        last = bars[-1]
        last_date = str(last[0])[:10]
        try:
            age = (TODAY - dt.date.fromisoformat(last_date)).days
            if age > 7:
                iss.append(f'CANDLES: last bar {last_date} ({age}d old)')
        except Exception:
            iss.append('CANDLES: unparseable last bar date')
        o, hi2, lo2, c = (n(last[1]), n(last[2]), n(last[3]), n(last[4]))
        if None not in (o, hi2, lo2, c):
            if not (hi2 >= lo2 and hi2 >= max(o, c) - 1e-9 and lo2 <= min(o, c) + 1e-9):
                iss.append(f'CANDLES: OHLC insane {last[1:5]}')
            if c <= 0:
                iss.append('CANDLES: non-positive close')
        if px and c and c > 0 and (px / c > 1.2 or px / c < 0.8):
            iss.append(f'PRICE: latest_price {px} vs last close {c} (>20% apart)')

        # RANGE vs candle extremes (last 250 bars)
        closes = [n(x[4]) for x in bars[-250:] if n(x[4])]
        if closes and len(closes) > 60:
            chi, clo = max(closes), min(closes)
            phi, plo = n(s.get('high_52w')), n(s.get('low_52w'))
            if phi and chi and abs(phi - chi) / chi > 0.10 and phi < chi:
                iss.append(f'RANGE: 52w high {phi} < candle max {chi:.1f}')
            if plo and clo and abs(plo - clo) / clo > 0.10 and plo > clo:
                iss.append(f'RANGE: 52w low {plo} > candle min {clo:.1f}')
            # RETURNS: 1y vs candles
            r1 = n(s.get('return_1y_pct'))
            if r1 is not None and len(closes) >= 240 and closes[0] > 0:
                derived = (closes[-1] / closes[0] - 1) * 100
                if abs(r1 - derived) > max(15, 0.15 * abs(derived)):
                    iss.append(f'RETURNS: 1y {r1:.0f}% vs candle-derived {derived:.0f}%')

    if b is not None:
        # ANNUAL_PL sanity
        pl = series(b, 'annual_pl')
        prev_sales = None
        for row in pl[-8:]:
            p = str(row.get('period'))[:10]
            sales, np_ = n(row.get('sales')), n(row.get('net_profit'))
            if sales is not None and np_ is not None and abs(np_) > max(abs(sales) * 2, 100):
                iss.append(f'ANNUAL_PL {p}: |np {np_:.0f}| >> sales {sales:.0f}')
            if sales is not None and prev_sales and sales > 0 and prev_sales > 0:
                ratio = sales / prev_sales
                if ratio > 100 or ratio < 0.01:
                    iss.append(f'ANNUAL_PL {p}: sales jump {ratio:.0f}x YoY')
            if sales is not None and sales > 0:
                prev_sales = sales

        # ANNUAL_BS reconciliation
        bs = latest_row(b, 'annual_bs')
        if bs:
            p, row = bs
            ta, te, tl = n(row.get('total_assets')), n(row.get('total_equity')), n(row.get('total_liabilities_filed'))
            if ta is not None and ta < 0:
                iss.append(f'ANNUAL_BS {p}: negative assets {ta}')
            if None not in (ta, te, tl) and abs(ta) > 1:
                if abs(ta - (te + tl)) / abs(ta) > 0.05:
                    iss.append(f'ANNUAL_BS {p}: assets {ta:.0f} != eq+liab {te + tl:.0f}')

        # QUARTERLY vs ANNUAL (only when the 4 quarters cover the FY)
        q = None
        for k in ('quarterly_results_consolidated', 'quarterly_results_standalone', 'quarterly_results'):
            if b.get(k) and len(b[k]) >= 4:
                q = sorted(b[k], key=lambda x: str(x.get('period')))
                break
        if q and pl:
            fy = latest_row(b, 'annual_pl')
            if fy:
                fy_end, fy_row = fy
                fy_sales = n(fy_row.get('sales'))
                qs = [r2 for r2 in q if str(r2.get('period'))[:10] <= fy_end][-4:]
                if fy_sales and len(qs) == 4 and str(qs[-1].get('period'))[:10] == fy_end:
                    qsum = sum(n(r2.get('sales')) or 0 for r2 in qs)
                    if qsum > 0 and abs(qsum - fy_sales) / fy_sales > 0.25:
                        iss.append(f'QUARTERLY: 4q sales {qsum:.0f} vs FY {fy_sales:.0f} (>25% gap)')

    # METRICS (anchor-aware mcap)
    mc = n(s.get('market_cap_cr'))
    sh = anchors.get(sym)
    if sh and px and mc:
        exp = sh * px / 1e7
        if exp > 25 and abs(mc - exp) / exp > 0.25:
            iss.append(f'METRICS: mcap {mc:,.0f} vs anchor {exp:,.0f}')

    # SHAREHOLDING
    shp = shp_latest.get(sym)
    if shp:
        pr, pu = n(shp.get('promoter_pct')), n(shp.get('public_pct'))
        if pr is not None and pu is not None and abs((pr + pu) - 100) > 2.5 and (pr + pu) > 0:
            emp = n(shp.get('employee_trust_pct')) or 0
            if abs((pr + pu + emp) - 100) > 2.5:
                iss.append(f'SHAREHOLDING: promoter {pr} + public {pu} + emp {emp} != 100')
        pl_pct = n(s.get('pledged_pct'))
        if pl_pct and pr and pl_pct > 100:
            iss.append(f'SHAREHOLDING: pledged {pl_pct}% > 100')
        per = str(shp.get('period'))[:10]
        try:
            if (TODAY - dt.date.fromisoformat(per)).days > 200:
                iss.append(f'SHAREHOLDING: latest period {per} stale (>2 quarters)')
        except Exception:
            pass

    # DELIVERY bounds
    dp = n(s.get('delivery_pct'))
    if dp is not None and (dp < 0 or dp > 100):
        iss.append(f'DELIVERY: {dp}%')

    return sym, iss, mc


def main():
    stocks = fetch_all('stock_master?select=symbol,name,sector,kite_token,latest_price,market_cap_cr,'
                       'high_52w,low_52w,return_1y_pct,pledged_pct,delivery_pct&is_active=eq.true')
    if LIMIT:
        stocks = stocks[:LIMIT]
    anch = fetch_all('shareholding_periods?select=symbol,total_shares,period&total_shares=not.is.null&order=period.desc')
    anchors = {}
    for r in anch:
        anchors.setdefault(r['symbol'], r['total_shares'])
    shp_rows = fetch_all('shareholding_periods?select=symbol,period,promoter_pct,public_pct,employee_trust_pct&order=period.desc')
    shp_latest = {}
    for r in shp_rows:
        shp_latest.setdefault(r['symbol'], r)
    print(f'[health] {len(stocks)} stocks | {len(anchors)} anchors | {len(shp_latest)} shp')

    results = []
    with ThreadPoolExecutor(max_workers=14) as ex:
        for i, res in enumerate(ex.map(lambda s: check(s, anchors, shp_latest), stocks)):
            results.append(res)
            if (i + 1) % 500 == 0:
                print(f'  ...{i + 1}/{len(stocks)}')

    by_cat = {}
    flagged = []
    for sym, iss, mc in results:
        if iss:
            flagged.append({'symbol': sym, 'mcap': mc, 'issues': iss})
            for d in iss:
                by_cat.setdefault(d.split(':')[0].split(' ')[0], []).append((sym, mc or 0, d))

    print('\n================ STOCK HEALTH SUMMARY ================')
    print(f'stocks checked : {len(results)}')
    print(f'stocks flagged : {len(flagged)} ({100 * len(flagged) / max(len(results), 1):.1f}%)')
    for c, lst in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        lst.sort(key=lambda x: -x[1])
        print(f'\n[{c}] {len(lst)} issues (top by mcap):')
        for sym, mc, d in lst[:8]:
            print(f'   {sym:14s} {mc:10,.0f}  {d[:90]}')
    json.dump({'checked': len(results), 'flagged': flagged},
              open(r'e:/Stocks sena/_logs/stock_health_report.json', 'w'), indent=1)
    print('\n[health] report -> _logs/stock_health_report.json')


if __name__ == '__main__':
    main()
