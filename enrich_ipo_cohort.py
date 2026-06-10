"""
enrich_ipo_cohort.py
====================
Enriches the listed-IPO cohort (ipo_calendar status=Listed, matched to stock_master) so
new listings get metrics, not just prices:
  1. sector + ISIN from BSE ComHeadernew (IndustryNew = our sector taxonomy) where missing
  2. empty-but-valid fundamentals-v2 bundle seeded where missing (same shape as
     prep_new_fundamentals.py) so the BSE quarterly/annual crons have a target to fill
  3. the symbol's BSE scrip added to _symbol_identifiers.json so those crons FIND it

Market cap / P/E arrive automatically once the first filings (or DRHP backfill) provide
share counts - compute_metrics handles that downstream.

Run:  py -3.11 enrich_ipo_cohort.py [--dry-run]
"""
import json
import os
import sys
import time

import requests

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
SVC = os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
H = {'apikey': SVC, 'Authorization': 'Bearer ' + SVC, 'Content-Type': 'application/json'}
STORE_H = {**H, 'x-upsert': 'true'}
DRY = '--dry-run' in sys.argv
BSE_H = {'User-Agent': 'Mozilla/5.0 Chrome/120', 'Accept': 'application/json',
         'Origin': 'https://www.bseindia.com', 'Referer': 'https://www.bseindia.com/'}
SYMIDS_FILE = os.path.join(HERE, '_symbol_identifiers.json')

EMPTY_BUNDLE_KEYS = [
    'annual_pl', 'annual_pl_standalone', 'annual_pl_consolidated',
    'annual_bs', 'annual_bs_standalone', 'annual_bs_consolidated',
    'annual_cf', 'annual_cf_standalone', 'annual_cf_consolidated',
    'annual_ratios', 'annual_ratios_filed',
    'quarterly_results', 'quarterly_results_standalone', 'quarterly_results_consolidated',
    'segments_annual', 'shareholding',
]


def main():
    cal = requests.get(f'{URL}/rest/v1/ipo_calendar?select=matched_symbol&status=eq.Listed&matched_symbol=not.is.null',
                       headers=H, timeout=25).json()
    syms = sorted({x['matched_symbol'] for x in cal})
    inq = ','.join(f'"{s}"' for s in syms)
    sm = requests.get(f'{URL}/rest/v1/stock_master?select=symbol,sector,isin,bse_scrip_code&symbol=in.({inq})',
                      headers=H, timeout=25).json()
    print(f'[enrich] cohort {len(sm)} symbols')

    # 1) sector + isin from ComHeader where missing
    for r in sm:
        if r.get('sector') or not r.get('bse_scrip_code'):
            continue
        try:
            d = requests.get(f"https://api.bseindia.com/BseIndiaAPI/api/ComHeadernew/w?quotetype=EQ&scripcode={r['bse_scrip_code']}&seriesid=",
                             headers=BSE_H, timeout=20).json()
        except Exception as e:
            print(f"  [sector] {r['symbol']}: fetch err {str(e)[:50]}")
            continue
        patch = {}
        if d.get('IndustryNew'):
            patch['sector'] = d['IndustryNew']
        if d.get('ISIN') and not r.get('isin'):
            patch['isin'] = d['ISIN']
        if patch:
            print(f"  [sector] {r['symbol']}: {patch}")
            if not DRY:
                requests.patch(f"{URL}/rest/v1/stock_master?symbol=eq.{r['symbol']}",
                               headers={**H, 'Prefer': 'return=minimal'}, data=json.dumps(patch), timeout=20)
        time.sleep(0.3)

    # 2) seed missing bundles + 3) identifiers
    try:
        symids = json.load(open(SYMIDS_FILE))
    except Exception:
        symids = {}
    seeded = ids_added = 0
    for r in sm:
        sym = r['symbol']
        try:
            chk = requests.get(f'{URL}/storage/v1/object/public/fundamentals-v2/{sym}.json', timeout=15)
            has = chk.status_code == 200
        except Exception:
            has = False
        if not has:
            bundle = {'symbol': sym, **{k: [] for k in EMPTY_BUNDLE_KEYS},
                      '_meta': {'source': 'enrich_ipo_cohort seed',
                                'seeded_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}}
            print(f'  [seed] {sym}: empty bundle')
            if not DRY:
                requests.put(f'{URL}/storage/v1/object/fundamentals-v2/{sym}.json', headers=STORE_H,
                             data=json.dumps(bundle), timeout=30)
            seeded += 1
        scrip = str(r.get('bse_scrip_code') or '').strip()
        if scrip and sym not in symids:
            symids[sym] = {'bse_scrip_code': scrip}
            ids_added += 1
    if ids_added and not DRY:
        json.dump(symids, open(SYMIDS_FILE, 'w'), indent=1)
    print(f'[enrich] done: {seeded} bundles seeded, {ids_added} identifiers added')


if __name__ == '__main__':
    main()
