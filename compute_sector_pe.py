"""
compute_sector_pe.py
====================
Monthly cap-weighted P/E history per sector, written to storage
daily/sector_pe_history.json for the website's sector pages.

Method (labelled in the UI):
  - constituents: each sector's 25 largest stocks by current market cap
  - for each month-end t (last ~42 months):
      mcap_i(t) = shares_now_i x adjusted_close_i(t)   [Kite/Yahoo series are split-adjusted]
      ttm_np_i(t) = sum of the 4 most recent quarterly net profits with period <= t
      sector_pe(t) = SUM mcap_i(t) / SUM ttm_np_i(t)   [aggregate incl. loss-makers]
  - a month is published only if covered stocks represent >= 60% of the sector's
    current top-25 mcap AND aggregate TTM earnings are positive; else null (no guessing).

Run:  py -3.11 compute_sector_pe.py [--apply]
"""
import datetime as dt
import json
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
MONTHS = 42
TOP_N = 25


def month_ends(n):
    out, d = [], dt.date.today().replace(day=1) - dt.timedelta(days=1)
    for _ in range(n):
        out.append(d)
        d = d.replace(day=1) - dt.timedelta(days=1)
    return out[::-1]


ME = month_ends(MONTHS)


def load_stock(s):
    """(symbol, shares_cr, {month_end: close}, [(period, ttm-capable np)]) or None"""
    sym = s['symbol']
    try:
        d = requests.get(f'{URL}/storage/v1/object/public/daily/{sym}.json', timeout=25).json()
        bars = d.get('bars') or []
        if len(bars) < 250:
            return None
        closes = {}
        bi = 0
        # bars sorted by date; walk once collecting last close <= each month end
        dates = [b[0] for b in bars]
        for me in ME:
            mes = me.isoformat()
            while bi < len(bars) - 1 and dates[bi + 1] <= mes:
                bi += 1
            if dates[bi] <= mes:
                closes[mes] = bars[bi][4]
        b = requests.get(f'{URL}/storage/v1/object/public/fundamentals-v2/{sym}.json', timeout=25).json()
        q = None
        for k in ('quarterly_results_consolidated', 'quarterly_results_standalone', 'quarterly_results'):
            if b.get(k) and len(b[k]) >= 4:
                q = b[k]
                break
        if not q:
            return None
        mcap_now = s['market_cap_cr']
        # quarter sanity gate: |NP| > 50% of current mcap is either a parse/scale error
        # (MAXESTATES 253000 'cr') or a one-time accounting gain (RAYMOND's ~Rs 5,328 cr
        # demerger gain on a Rs 3,700 cr company). Either way it poisons the sector
        # aggregate for 4 quarters - excluded, and disclosed in the method note.
        nps = sorted([(str(r.get('period'))[:10], r.get('net_profit')) for r in q
                      if r.get('period') and r.get('net_profit') is not None
                      and abs(r.get('net_profit')) <= 0.5 * mcap_now])
        if len(nps) < 4:
            return None
        shares_cr = (mcap_now / s['latest_price']) if s.get('latest_price') else None
        if not shares_cr:
            return None
        return (sym, shares_cr, closes, nps, s['market_cap_cr'])
    except Exception:
        return None


def ttm_at(nps, mes):
    past = [v for p, v in nps if p <= mes]
    if len(past) < 4:
        return None
    return sum(past[-4:])


def main():
    rows = requests.get(f'{URL}/rest/v1/stock_master?select=symbol,sector,market_cap_cr,latest_price'
                        f'&is_active=eq.true&market_cap_cr=not.is.null&sector=not.is.null'
                        f'&market_cap_cr=lt.2500000&order=market_cap_cr.desc&limit=3000', headers=H, timeout=30).json()
    by_sec = {}
    for r in rows:
        by_sec.setdefault(r['sector'], []).append(r)
    out = {}
    for sec, stocks in sorted(by_sec.items()):
        top = stocks[:TOP_N]
        if len(top) < 5:
            continue
        with ThreadPoolExecutor(max_workers=8) as ex:
            loaded = [x for x in ex.map(load_stock, top) if x]
        if len(loaded) < 5:
            print(f'{sec:38s} skipped (only {len(loaded)} loadable)')
            continue
        total_top_mcap = sum(x[4] for x in loaded)
        series = []
        for me in ME:
            mes = me.isoformat()
            mc = earn = cov = 0.0
            n = 0
            for sym, shares, closes, nps, mcap_now in loaded:
                c = closes.get(mes)
                t = ttm_at(nps, mes)
                if c is None or t is None:
                    continue
                mc += shares * c
                earn += t
                cov += mcap_now
                n += 1
            if cov >= 0.6 * total_top_mcap and earn > 0:
                series.append({'date': mes, 'pe': round(mc / earn, 2), 'n': n})
            else:
                series.append({'date': mes, 'pe': None, 'n': n})
        valid = [p['pe'] for p in series if p['pe'] is not None]
        print(f'{sec:38s} {len(loaded):2d} stocks | {len(valid):2d}/{MONTHS} months | '
              f"now {valid[-1] if valid else '-'} | range {min(valid) if valid else '-'}-{max(valid) if valid else '-'}")
        out[sec] = series

    doc = {'computed_at': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
           'method': (f'cap-weighted aggregate P/E of each sector\'s top {TOP_N} stocks; monthly; '
                      'TTM earnings from quarterly filings; quarters with |net profit| above 50% of '
                      'market cap (one-time accounting gains / data errors) excluded from aggregates'),
           'sectors': out}
    if APPLY:
        r = requests.put(f'{URL}/storage/v1/object/daily/sector_pe_history.json', headers=STORE_H,
                         data=json.dumps(doc, separators=(',', ':')), timeout=60)
        print(f'[sector-pe] wrote {len(out)} sectors (HTTP {r.status_code})')
    else:
        json.dump(doc, open(r'e:/Stocks sena/_logs/sector_pe_preview.json', 'w'), indent=1)
        print(f'[sector-pe] dry-run: {len(out)} sectors -> _logs/sector_pe_preview.json')


if __name__ == '__main__':
    main()
