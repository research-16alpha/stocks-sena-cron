"""
sector_backfill.py
=================
Classify the long-tail stocks that have no sector (NSE only publishes sectors for
its ~755 indexed names; the other ~3,400 tradable stocks were blank).

Source = BSE, in OUR taxonomy:
  - bulk ListofScripData  -> ISIN -> BSE scrip code (one call, ~4,855 scrips)
  - per-scrip ComHeader   -> IndustryNew ("Information Technology", "Financial
                             Services", "Oil Gas & Consumable Fuels"...) == the
                             same scheme stock_master.sector already uses
Banks (is_bank) are written as 'Banking' to match the existing convention.

Run:  python sector_backfill.py --limit 30 --dry-run   # smoke test
      python sector_backfill.py                         # full
"""
import argparse
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from supabase import create_client

SVC = os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
sb = create_client(URL, SVC)
BSE_H = {'User-Agent': 'Mozilla/5.0 Chrome/120', 'Accept': 'application/json',
         'Origin': 'https://www.bseindia.com', 'Referer': 'https://www.bseindia.com/'}
_lock = threading.Lock()


# Normalize every sector label to the 22 canonical buckets (see reference_sector_classification).
# Without this, BSE's no-comma "Oil Gas..." or any variant re-fragments the sector universe.
SECTOR_CANON = {
    'FMCG': 'Fast Moving Consumer Goods', 'Liquor': 'Fast Moving Consumer Goods',
    'Auto': 'Automobile and Auto Components', 'Auto Anc': 'Automobile and Auto Components',
    'IT': 'Information Technology', 'Pharma': 'Healthcare',
    'Finance': 'Financial Services', 'Insurance': 'Financial Services', 'Fintech': 'Financial Services',
    'Metals': 'Metals & Mining',
    'Oil Gas & Consumable Fuels': 'Oil, Gas & Consumable Fuels', 'Energy': 'Oil, Gas & Consumable Fuels',
    'Telecom': 'Telecommunication',
    'Cement': 'Construction Materials', 'Building Materials': 'Construction Materials',
    'Entertainment': 'Media, Entertainment & Publication',
    'Media Entertainment & Publication': 'Media, Entertainment & Publication',
    'Utilities': 'Power', 'Industrials': 'Capital Goods', 'Defence': 'Capital Goods',
    'Electronics': 'Consumer Durables', 'Paints': 'Consumer Durables', 'Jewellery': 'Consumer Durables',
    'Infra': 'Construction', 'Logistics': 'Services',
    'Retail': 'Consumer Services', 'Internet': 'Consumer Services', 'Travel': 'Consumer Services',
    'Aviation': 'Consumer Services', 'Hospitality': 'Consumer Services', 'QSR': 'Consumer Services',
}


def canon_sector(s):
    return SECTOR_CANON.get(s.strip(), s.strip()) if s else s


def com_industry(scrip):
    try:
        j = requests.get('https://api.bseindia.com/BseIndiaAPI/api/ComHeader/w', headers=BSE_H,
                         params={'quotetype': 'EQ', 'scripcode': str(scrip), 'seriesid': ''}, timeout=12).json()
        sec = (j.get('IndustryNew') or j.get('Sector') or '').strip() or None
        return canon_sector(sec), (j.get('Industry') or '').strip() or None
    except Exception:
        return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, default=14)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    # ISIN -> BSE scrip from the bulk list
    d = requests.get('https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w'
                     '?Group=&Scripcode=&industry=&segment=Equity&status=Active', headers=BSE_H, timeout=30).json()
    isin2scrip = {}
    for r in d:
        iz = (r.get('ISIN_NUMBER') or '').strip()
        sc = str(r.get('SCRIP_CD') or '').strip()
        if iz and sc:
            isin2scrip.setdefault(iz, sc)
    print(f'[INFO] BSE bulk: {len(isin2scrip)} ISIN->scrip', flush=True)

    # unclassified tradable stocks with an ISIN
    rows, off = [], 0
    while True:
        b = sb.table('stock_master').select('symbol,isin,is_bank').eq('is_active', True) \
            .not_.is_('latest_price', 'null').is_('sector', 'null').not_.is_('isin', 'null') \
            .range(off, off + 999).execute().data or []
        rows += b
        if len(b) < 1000:
            break
        off += 1000
    rows = [r for r in rows if r['isin'] in isin2scrip]
    if args.limit:
        rows = rows[:args.limit]
    print(f'[INFO] {len(rows)} unclassified tradable stocks resolvable to a BSE scrip', flush=True)

    def work(r):
        sec, ind = com_industry(isin2scrip[r['isin']])
        time.sleep(0.05)
        if not sec:
            return (r['symbol'], None, None)
        if r.get('is_bank'):
            sec = 'Banking'
        return (r['symbol'], sec, ind)

    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, res in enumerate(ex.map(work, rows), 1):
            results.append(res)
            if i % 300 == 0:
                print(f'  {i}/{len(rows)} · {i/(time.time()-t0):.1f}/s', flush=True)

    got = [r for r in results if r[1]]
    from collections import Counter
    print(f'[INFO] classified {len(got)}/{len(rows)}')
    print('  sector spread:', Counter(r[1] for r in got).most_common(12))
    print('  sample:', got[:8])

    if args.dry_run:
        print('\nDRY RUN — nothing written.')
        return

    ok = 0
    for sym, sec, ind in got:
        patch = {'sector': sec}
        if ind:
            patch['industry'] = ind
        try:
            sb.table('stock_master').update(patch).eq('symbol', sym).execute(); ok += 1
        except Exception as e:
            print(f'  upd err {sym}: {str(e)[:40]}')
    print(f'[OK] wrote sector for {ok}/{len(rows)} stocks')


if __name__ == '__main__':
    main()
