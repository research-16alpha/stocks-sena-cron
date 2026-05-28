"""
build_identifier_master.py
==========================
Builds a unified exchange-identifier master for the whole Indian equity
universe and resolves every stock_master row to its BSE scrip code + ISIN +
NSE symbol. This is the foundation for primary-source scraping (BSE results
XBRL is keyed by scrip code, which we did not store before).

SOURCES
  - BSE  : api.bseindia.com ListofScripData (status='' → Active+Delisted+Suspended,
           ~10.7k rows). Fields: SCRIP_CD, scrip_id, Issuer_Name, Scrip_Name,
           ISIN_NUMBER, Status, FACE_VALUE, Mktcap, INDUSTRY.
  - NSE  : nsearchives EQUITY_L.csv (listed EQ/BE, ~2.4k) → SYMBOL, NAME, ISIN.
           NSE has no clean public delisted feed; BSE's delisted set covers the
           union because virtually all NSE names are dual-listed (join on ISIN).

OUTPUT (committed next to the crons so the cloud cron is self-contained)
  - _identifier_master.json   : ISIN-keyed crosswalk for the full universe.
  - _symbol_identifiers.json   : our stock_master.symbol → {bse_scrip, isin,
                                  nse_symbol, bse_name, status, method}.

MATCHING (strict, to avoid the Welspun-India/Welspun-Corp class of collision)
  Conservative normalizer: strip only trailing Ltd/Limited/The + punctuation;
  KEEP distinctive tokens (India/Corp/Industries). Resolution priority:
    1. our symbol == NSE symbol            → ISIN → BSE  (highest confidence)
    2. our name ≈ NSE name (unique)        → ISIN → BSE
    3. our name ≈ BSE Issuer_Name (UNIQUE) → bse_scrip + isin
  Ambiguous names (map to >1 ISIN) are left unresolved rather than guessed.

USAGE
  py -3.11 build_identifier_master.py            # build masters + resolve, write JSON
  py -3.11 build_identifier_master.py --populate # also PATCH stock_master.bse_scrip_code/isin
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from io import StringIO

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER_FILE = os.path.join(HERE, '_identifier_master.json')
SYMIDS_FILE = os.path.join(HERE, '_symbol_identifiers.json')

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not KEY:
    try:
        with open('e:/Stocks sena/.supabase-service-key') as f:
            KEY = f.read().strip()
    except FileNotFoundError:
        KEY = None

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
BSE_MASTER = 'https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w'
NSE_EQUITY_L = 'https://archives.nseindia.com/content/equities/EQUITY_L.csv'

# Conservative: only strip trailing Ltd/Limited/The + punctuation. Keep
# India/Corp/Industries so "Welspun India" != "Welspun Corp".
_TAIL = re.compile(r'\s*(the\s+)?$', re.I)
_SUFFIX = re.compile(r'\b(ltd|limited)\b\.?', re.I)
_PUNCT = re.compile(r'[^a-z0-9 ]')


def norm(name: str) -> str:
    s = (name or '').lower()
    s = _SUFFIX.sub(' ', s)
    s = _PUNCT.sub(' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r'^the\s+', '', s)
    return s


def bse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({'User-Agent': UA, 'Referer': 'https://www.bseindia.com/',
                      'Origin': 'https://www.bseindia.com',
                      'Accept': 'application/json, text/plain, */*'})
    try:
        s.get('https://www.bseindia.com/', timeout=20)
    except Exception:
        pass
    return s


def fetch_bse_full(s: requests.Session) -> list:
    r = s.get(BSE_MASTER, params={'Group': '', 'Scripcode': '', 'industry': '',
                                  'segment': 'Equity', 'status': ''}, timeout=90)
    r.raise_for_status()
    return r.json()


def fetch_nse_listed() -> list:
    r = requests.get(NSE_EQUITY_L, headers={'User-Agent': UA}, timeout=30)
    r.raise_for_status()
    out = []
    for row in csv.DictReader(StringIO(r.text)):
        out.append({
            'symbol': (row.get('SYMBOL') or '').strip(),
            'name': (row.get('NAME OF COMPANY') or '').strip(),
            'isin': (row.get(' ISIN NUMBER') or row.get('ISIN NUMBER') or '').strip(),
            'series': (row.get(' SERIES') or row.get('SERIES') or '').strip(),
        })
    return out


def build_master(bse_rows: list, nse_rows: list) -> dict:
    """ISIN-keyed crosswalk. Rows without ISIN are kept under a synthetic key."""
    master = {}

    def blank():
        return {'isin': None, 'bse_scrip': None, 'bse_scrip_id': None,
                'bse_name': None, 'bse_status': None, 'face_value': None,
                'mktcap': None, 'industry': None, 'nse_symbol': None, 'nse_name': None}

    for b in bse_rows:
        isin = (b.get('ISIN_NUMBER') or '').strip().upper()
        scrip = str(b.get('SCRIP_CD') or '').strip()
        key = isin or f'BSE:{scrip}'
        m = master.setdefault(key, blank())
        m['isin'] = isin or m['isin']
        # Prefer Active row if duplicate ISIN across statuses
        if m['bse_scrip'] is None or (b.get('Status') == 'Active'):
            m['bse_scrip'] = scrip
            m['bse_scrip_id'] = b.get('scrip_id')
            m['bse_name'] = b.get('Issuer_Name') or b.get('Scrip_Name')
            m['bse_status'] = b.get('Status')
            m['face_value'] = b.get('FACE_VALUE')
            m['mktcap'] = b.get('Mktcap')
            m['industry'] = b.get('INDUSTRY')

    for n in nse_rows:
        isin = (n.get('isin') or '').strip().upper()
        key = isin if isin in master else (isin or f'NSE:{n["symbol"]}')
        m = master.setdefault(key, blank())
        m['isin'] = m['isin'] or isin
        m['nse_symbol'] = n['symbol']
        m['nse_name'] = n['name']
    return master


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--populate', action='store_true',
                    help='Also PATCH stock_master.bse_scrip_code + isin for resolved rows.')
    args = ap.parse_args()

    print('[1/4] Fetching BSE full master (Active+Delisted+Suspended)...')
    s = bse_session()
    bse_rows = fetch_bse_full(s)
    print(f'      BSE rows: {len(bse_rows)}')

    print('[2/4] Fetching NSE listed (EQUITY_L)...')
    nse_rows = fetch_nse_listed()
    print(f'      NSE rows: {len(nse_rows)}')

    print('[3/4] Building ISIN-keyed master...')
    master = build_master(bse_rows, nse_rows)
    with_isin = sum(1 for m in master.values() if m['isin'])
    with_both = sum(1 for m in master.values() if m['bse_scrip'] and m['nse_symbol'])
    print(f'      master entries: {len(master)}  with_isin: {with_isin}  bse+nse linked: {with_both}')
    with open(MASTER_FILE, 'w', encoding='utf-8') as f:
        json.dump(master, f)
    print(f'      wrote {MASTER_FILE}')

    # Build resolution indices
    nse_by_symbol = {n['symbol'].upper(): n for n in nse_rows if n['symbol']}
    nse_name = {}
    for n in nse_rows:
        nse_name.setdefault(norm(n['name']), []).append(n)
    bse_active = [b for b in bse_rows if b.get('Status') == 'Active']
    bse_name = {}
    for b in bse_active:
        bse_name.setdefault(norm(b.get('Issuer_Name') or b.get('Scrip_Name') or ''), []).append(b)
    isin_to_master = {m['isin']: m for m in master.values() if m['isin']}

    print('[4/4] Resolving stock_master universe...')
    H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}
    rows = []
    off = 0
    while True:
        r = requests.get(f'{URL}/rest/v1/stock_master?select=symbol,name,exchange,is_active'
                         f'&limit=1000&offset={off}', headers=H, timeout=30)
        b = r.json()
        if not b:
            break
        rows += b
        off += 1000
        if len(b) < 1000:
            break
    print(f'      stock_master rows: {len(rows)}')

    symids = {}
    methods = {}
    for r in rows:
        sym = r['symbol']
        nm = norm(r.get('name') or '')
        bse_scrip = isin = nse_symbol = bse_name_out = status = None
        method = None

        # 1. our symbol == NSE symbol
        hit = nse_by_symbol.get(sym.upper())
        if hit and hit.get('isin'):
            isin = hit['isin'].upper()
            nse_symbol = hit['symbol']
            method = 'nse_symbol'
        # 2. unique NSE name match
        if not isin and nse_name.get(nm) and len(nse_name[nm]) == 1 and nse_name[nm][0].get('isin'):
            hit = nse_name[nm][0]
            isin = hit['isin'].upper()
            nse_symbol = hit['symbol']
            method = 'nse_name'
        # ISIN → BSE via master
        if isin and isin in isin_to_master:
            m = isin_to_master[isin]
            bse_scrip = m['bse_scrip']
            bse_name_out = m['bse_name']
            status = m['bse_status']
            nse_symbol = nse_symbol or m['nse_symbol']
        # 3. unique BSE Issuer_Name match (no ISIN path)
        if not bse_scrip and bse_name.get(nm) and len(bse_name[nm]) == 1:
            b = bse_name[nm][0]
            bse_scrip = str(b.get('SCRIP_CD') or '')
            isin = isin or (b.get('ISIN_NUMBER') or '').strip().upper() or None
            bse_name_out = b.get('Issuer_Name')
            status = b.get('Status')
            method = method or 'bse_name'

        if bse_scrip or isin or nse_symbol:
            symids[sym] = {'bse_scrip': bse_scrip or None, 'isin': isin or None,
                           'nse_symbol': nse_symbol or None, 'bse_name': bse_name_out,
                           'status': status, 'method': method}
            methods[method or 'partial'] = methods.get(method or 'partial', 0) + 1

    with open(SYMIDS_FILE, 'w', encoding='utf-8') as f:
        json.dump(symids, f, indent=0)
    resolved_bse = sum(1 for v in symids.values() if v['bse_scrip'])
    print(f'      resolved {len(symids)}/{len(rows)} ; with BSE scrip: {resolved_bse}')
    print(f'      methods: {methods}')
    print(f'      wrote {SYMIDS_FILE}')

    if args.populate:
        print('[populate] PATCHing stock_master.bse_scrip_code + isin...')
        import urllib.parse
        ok = 0
        for sym, v in symids.items():
            payload = {}
            if v['bse_scrip']:
                payload['bse_scrip_code'] = v['bse_scrip']
            if v['isin']:
                payload['isin'] = v['isin']
            if not payload:
                continue
            u = f"{URL}/rest/v1/stock_master?symbol=eq.{urllib.parse.quote(sym)}"
            rr = requests.patch(u, headers={**H, 'Content-Type': 'application/json',
                                            'Prefer': 'return=minimal'},
                                data=json.dumps(payload), timeout=20)
            if rr.status_code in (200, 204):
                ok += 1
        print(f'[populate] patched {ok} rows')


if __name__ == '__main__':
    main()
