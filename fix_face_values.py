"""
fix_face_values.py
==================
Systemic fix for the NMDC bug class: compute_metrics defaults face value to Rs 10
when the bundle snapshot lacks it, corrupting every price-derived ratio by 10x/5x/2x
for stocks whose real face is 1/2/5 (NMDC showed mcap 7,655 cr vs real 77,904 cr).

Authoritative sources, fetched in bulk (no per-stock API hammering):
  1. NSE EQUITY_L.csv - symbol -> face value for every NSE-listed stock
  2. BSE ListofScripData - scrip code -> face value for BSE-only stocks

For every active stock with a bundle:
  - snapshot.face_value missing  -> write the source value
  - snapshot.face_value != source -> overwrite (source wins), tagged
Stocks whose effective face CHANGED from the implicit 10 are listed for metrics
recompute (printed + _logs/face_changed_syms.txt).

Run:  py -3.11 fix_face_values.py            # dry-run report
      py -3.11 fix_face_values.py --apply
"""
import csv
import io
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
NSE_H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124', 'Referer': 'https://www.nseindia.com/'}
BSE_H = {'User-Agent': 'Mozilla/5.0 Chrome/120', 'Accept': 'application/json',
         'Origin': 'https://www.bseindia.com', 'Referer': 'https://www.bseindia.com/'}


def nse_faces():
    r = requests.get('https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv', headers=NSE_H, timeout=60)
    r.raise_for_status()
    rd = csv.DictReader(io.StringIO(r.text))
    out = {}
    for row in rd:
        row = {k.strip(): (v or '').strip() for k, v in row.items()}
        sym, fv = row.get('SYMBOL'), row.get('FACE VALUE')
        try:
            if sym and fv:
                out[sym] = float(fv)
        except ValueError:
            continue
    return out


def bse_faces():
    """scrip_code -> face value from the full BSE scrip list."""
    try:
        r = requests.get('https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity&status=Active',
                         headers=BSE_H, timeout=90)
        arr = r.json()
        out = {}
        for x in arr:
            code = str(x.get('SCRIP_CD') or x.get('Scrip_Code') or '').strip()
            fv = x.get('FACE_VALUE') or x.get('FaceValue') or x.get('Face_Value')
            try:
                if code and fv:
                    out[code] = float(fv)
            except (ValueError, TypeError):
                continue
        return out
    except Exception as e:
        print(f'  [warn] BSE list failed: {str(e)[:60]} (NSE-only pass)')
        return {}


def main():
    nf = nse_faces()
    bf = bse_faces()
    print(f'[face] sources: NSE {len(nf)} symbols, BSE {len(bf)} scrips')

    stocks, off = [], 0
    while True:
        d = requests.get(f'{URL}/rest/v1/stock_master?select=symbol,bse_scrip_code&is_active=eq.true'
                         f'&offset={off}&limit=1000', headers=H, timeout=30).json()
        stocks += d
        if len(d) < 1000:
            break
        off += 1000
    print(f'[face] {len(stocks)} active stocks')

    changed, fixed_missing, mismatched, nosrc = [], 0, 0, 0

    def work(s):
        nonlocal fixed_missing, mismatched, nosrc
        sym = s['symbol']
        src = nf.get(sym) or bf.get(str(s.get('bse_scrip_code') or ''))
        if not src:
            nosrc += 1
            return
        try:
            r = requests.get(f'{URL}/storage/v1/object/public/fundamentals-v2/{sym}.json',
                             params={'cb': 'face1'}, timeout=25)
            if r.status_code != 200:
                return
            b = r.json()
        except Exception:
            return
        snap = b.get('snapshot')
        cur = None
        if isinstance(snap, list) and snap:
            cur = snap[0].get('face_value')
        elif isinstance(snap, dict):
            cur = snap.get('face_value')
        if cur is not None and abs(float(cur) - src) < 0.001:
            return  # already correct
        effective_before = cur if cur is not None else 10.0
        if cur is None:
            fixed_missing += 1
        else:
            mismatched += 1
            print(f'  MISMATCH {sym:12s} snapshot {cur} vs source {src}')
        if abs(effective_before - src) > 0.001:
            changed.append(sym)
        if not APPLY:
            return
        if isinstance(snap, list) and snap:
            snap[0]['face_value'] = src
        elif isinstance(snap, dict):
            snap['face_value'] = src
        else:
            b['snapshot'] = [{'face_value': src}]
        mm = b.get('_meta') or {}
        mm['face_fix'] = 'EQUITY_L/BSE 2026-06-11'
        b['_meta'] = mm
        requests.put(f'{URL}/storage/v1/object/fundamentals-v2/{sym}.json', headers=STORE_H,
                     data=json.dumps(b), timeout=40)

    with ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(work, stocks))

    print(f'[face] missing->set: {fixed_missing} | mismatched->corrected: {mismatched} | no source: {nosrc}')
    print(f'[face] metrics recompute needed for {len(changed)} stocks (face != implicit 10)')
    open(r'e:/Stocks sena/_logs/face_changed_syms.txt', 'w').write(','.join(changed))


if __name__ == '__main__':
    main()
