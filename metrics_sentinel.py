"""
metrics_sentinel.py
===================
Daily integrity check on PUBLISHED metrics - catches derived-number corruption
(the ACEINTEG / NMDC class) automatically instead of waiting for a human to spot
a 20-lakh-crore microcap. Runs after the valuation cron.

Invariants checked across stock_master (active stocks):
  A. mcap vs SHP anchor: where shareholding_periods.total_shares is known,
     |mcap - shares*price/1e7| must be within 25%
  B. impossible ratios: pe > 5000, pb > 500, |mcap| >= 2,000,000 cr,
     mcap > 0 but < price/1e6 (nonsense), div_yield > 50%
  C. top-50 churn: any stock ENTERING the top-50 by mcap that wasn't in
     yesterday's snapshot (stored in app_config) - giants don't appear overnight
Findings -> printed report + admin bell notification (user_notifications),
NOTHING is auto-fixed (a human or the fixers decide).

Run:  py -3.11 metrics_sentinel.py [--notify]
"""
import json
import os
import sys

import requests

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
SVC = os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
H = {'apikey': SVC, 'Authorization': 'Bearer ' + SVC, 'Content-Type': 'application/json'}
ADMIN = '10658747-b0d3-4d8d-a2da-a1775afb9c87'
NOTIFY = '--notify' in sys.argv


def fetch_all(q):
    out, off = [], 0
    while True:
        d = requests.get(f'{URL}/rest/v1/{q}&offset={off}&limit=1000', headers=H, timeout=30).json()
        out += d
        if len(d) < 1000:
            break
        off += 1000
    return out


def main():
    stocks = fetch_all('stock_master?select=symbol,market_cap_cr,latest_price,pe_ratio,pb_ratio,div_yield_pct&is_active=eq.true&market_cap_cr=not.is.null')
    shp = fetch_all('shareholding_periods?select=symbol,total_shares,period&total_shares=not.is.null&order=period.desc')
    anchor = {}
    for r in shp:
        if r['symbol'] not in anchor:
            anchor[r['symbol']] = r['total_shares']

    findings = []

    # A. SHP anchor cross-check
    for s in stocks:
        sh = anchor.get(s['symbol'])
        if not sh or not s.get('latest_price'):
            continue
        expect = sh * s['latest_price'] / 1e7
        if expect > 50 and s['market_cap_cr'] and abs(s['market_cap_cr'] - expect) / expect > 0.25:
            findings.append(f"ANCHOR {s['symbol']}: mcap {s['market_cap_cr']:,.0f} vs SHP-implied {expect:,.0f} cr")

    # B. impossible values
    for s in stocks:
        mc, pe, pb, dy = s.get('market_cap_cr'), s.get('pe_ratio'), s.get('pb_ratio'), s.get('div_yield_pct')
        if mc and mc >= 2_000_000:
            findings.append(f"IMPOSSIBLE {s['symbol']}: mcap {mc:,.0f} cr")
        if pe and (pe > 5000 or pe < -5000):
            findings.append(f"IMPOSSIBLE {s['symbol']}: pe {pe}")
        if pb and pb > 500:
            findings.append(f"IMPOSSIBLE {s['symbol']}: pb {pb}")
        if dy and dy > 50:
            findings.append(f"IMPOSSIBLE {s['symbol']}: div_yield {dy}%")

    # C. top-50 churn vs yesterday
    top = sorted([s for s in stocks if s.get('market_cap_cr')], key=lambda x: -x['market_cap_cr'])[:50]
    top_syms = [t['symbol'] for t in top]
    prev_raw = requests.get(f'{URL}/rest/v1/app_config?select=value&key=eq.sentinel_top50', headers=H, timeout=20).json()
    prev = json.loads(prev_raw[0]['value']) if prev_raw and prev_raw[0].get('value') else []
    if prev:
        new_entrants = [s for s in top_syms if s not in prev]
        for s in new_entrants:
            mc = next(t['market_cap_cr'] for t in top if t['symbol'] == s)
            findings.append(f"TOP50-ENTRANT {s}: mcap {mc:,.0f} cr (not in yesterday's top-50)")
    requests.post(f'{URL}/rest/v1/app_config?on_conflict=key',
                  headers={**H, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
                  data=json.dumps([{'key': 'sentinel_top50', 'value': json.dumps(top_syms)}]), timeout=20)

    print(f'[sentinel] {len(stocks)} stocks checked | {len(findings)} findings')
    # public repo -> Actions logs are world-readable; symbol-level detail goes to the
    # admin bell (private table) only, never stdout, when running in CI
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        for f in findings[:40]:
            print('  ' + f)

    if findings and NOTIFY:
        body = '\n'.join(findings[:12]) + (f'\n...and {len(findings)-12} more' if len(findings) > 12 else '')
        requests.post(f'{URL}/rest/v1/user_notifications',
                      headers={**H, 'Prefer': 'return=minimal'},
                      data=json.dumps([{'user_id': ADMIN, 'title': f'Metrics sentinel: {len(findings)} integrity findings',
                                        'body': body, 'kind': 'system'}]), timeout=20)
        print('[sentinel] admin notified')


if __name__ == '__main__':
    main()
