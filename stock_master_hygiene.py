"""
stock_master_hygiene.py
=======================
Sprint 4 cleanup. Adds `is_active` flag to stock_master and marks dead
listings inactive so they stop polluting search / screener / heatmaps.

Inactive = stock has ZERO presence in any of:
  - fundamentals-v2 bucket (no XBRL bundle)
  - daily bucket (no OHLCV chart)
  - latest_price (no Yahoo or Kite quote)

These are typically delisted (Allahabad Bank ALBK → merged 2020), renamed
(ABIRLANUVO demerged 2017), or BSE microcaps Kite doesn't track.

Run after Kite LTP gap-fill so the latest_price column is maximally populated.
"""
import os
import sys
import json
import urllib.request

SUPABASE_URL = 'https://tbeadvvkqyrhtendttrg.supabase.co'
with open(r'e:\Stocks sena\.supabase-service-key') as f:
    KEY = f.read().strip()
HEADERS = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

# Manual alias map — renamed/demerged stocks. Old symbol → new symbol.
# These get marked inactive AND we record the alias for future search.
ALIAS_MAP = {
    'ALBK':         'INDIANB',         # Allahabad Bank merged into Indian Bank (2020)
    'ABIRLANUVO':   'ABFRL',           # Aditya Birla Nuvo demerged (2017)
    'CORPBANK':     'UNIONBANK',       # Corporation Bank merged (2020)
    'OBC':          'PNB',             # Oriental Bank of Commerce merged with PNB (2020)
    'UNITEDBNK':    'PNB',             # United Bank of India merged with PNB (2020)
    'SYNDIBANK':    'CANBK',           # Syndicate Bank merged with Canara (2020)
    'ANDHRABANK':   'UNIONBANK',       # Andhra Bank merged (2020)
    'VIJAYABANK':   'BANKBARODA',      # Vijaya Bank merged with BoB (2019)
    'DENABANK':     'BANKBARODA',      # Dena Bank merged with BoB (2019)
    'AMARAJABAT':   'ARE&M',           # Amara Raja Energy & Mobility
    'TATAMTRDVR':   'TATAMOTORS',      # DVR merged into TATAMOTORS (2024)
    'PVR':          'PVRINOX',         # PVR-Inox merger (2023)
    'INOXLEISUR':   'PVRINOX',         # Same merger
    'LTI':          'LTIM',            # L&T Infotech + Mindtree → LTIM (2022)
    'MINDTREE':     'LTIM',
    'MCDOWELL-N':   'UNITDSPR',        # United Spirits rename
    'MGLAB':        'MERCURYLAB',
    'MINDAIND':     'UNOMINDA',        # Minda Industries rename
    'AVENUE':       'DMART',           # Avenue Supermarts (DMART)
    'ADANITRANS':   'ADANIENSOL',      # Adani Energy Solutions
    'GMRINFRA':     'GMRP&UI',         # GMR rebrand (2022)
    'L&TFH':        'LTF',             # L&T Finance Holdings → LTF rename
    'BPCL-BO':      'BPCL',
    'JSWENERGY-BO': 'JSWENERGY',
    'IDFC':         'IDFCFIRSTB',      # IDFC merger
    'CAPF':         'CAPITALAU',
    'ABGSHIP':      None,              # ABG Shipyard — bankrupt, delisted 2018
    'ABGHEAVY':     None,              # ABG Heavy — suspended
    'AFTEK':        None,              # delisted
    'ACROPETAL':    None,              # delisted
    'ALBK':         'INDIANB',
}


def list_bucket(bucket: str) -> set:
    out = set()
    offset = 0
    headers = {**HEADERS, 'Content-Type': 'application/json'}
    while True:
        req = urllib.request.Request(
            f'{SUPABASE_URL}/storage/v1/object/list/{bucket}',
            data=json.dumps({'prefix': '', 'limit': 1000, 'offset': offset}).encode(),
            headers=headers, method='POST',
        )
        r = urllib.request.urlopen(req, timeout=30)
        batch = json.loads(r.read())
        if not batch: break
        for f in batch:
            if f['name'].endswith('.json'):
                out.add(f['name'][:-5])
        if len(batch) < 1000: break
        offset += 1000
    return out


def fetch_master() -> list:
    syms = []
    offset = 0
    while True:
        url = f'{SUPABASE_URL}/rest/v1/stock_master?select=symbol,name,isin,latest_price,market_cap_cr&limit=1000&offset={offset}'
        r = urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=20)
        batch = json.loads(r.read())
        if not batch: break
        syms.extend(batch)
        if len(batch) < 1000: break
        offset += 1000
    return syms


def ensure_columns():
    """Add is_active + alias_for columns via Supabase Management API if missing."""
    # Try a SELECT for is_active — if it errors, column doesn't exist
    url = f'{SUPABASE_URL}/rest/v1/stock_master?select=is_active&limit=1'
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=10)
        print('[schema] is_active column exists')
        return True
    except urllib.error.HTTPError as e:
        if e.code == 400:
            print('[schema] is_active column missing — please add via Supabase SQL editor:')
            print('  ALTER TABLE stock_master ADD COLUMN is_active boolean DEFAULT true NOT NULL;')
            print('  ALTER TABLE stock_master ADD COLUMN alias_for text;')
            print('  CREATE INDEX idx_stock_master_active ON stock_master(is_active) WHERE is_active = true;')
            return False
    return True


def patch_row(sym: str, body: dict) -> bool:
    if not body: return False
    url = f'{SUPABASE_URL}/rest/v1/stock_master?symbol=eq.{sym}'
    headers = {**HEADERS, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}
    payload = json.dumps(body).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method='PATCH')
        r = urllib.request.urlopen(req, timeout=15)
        return r.status in (200, 204)
    except Exception:
        return False


def main():
    if not ensure_columns():
        print('\n[abort] Add columns then re-run.')
        return

    print('[load] stock_master...')
    master = fetch_master()
    print(f'  {len(master)} stocks')

    print('[load] fundamentals-v2 bucket...')
    fund = list_bucket('fundamentals-v2')
    print(f'  {len(fund)} bundles')

    print('[load] daily bucket...')
    daily = list_bucket('daily')
    print(f'  {len(daily)} OHLCV files')

    # Identify inactive: NO presence anywhere
    inactive = []
    alias_updates = []
    for s in master:
        sym = s['symbol']
        has_fund = sym in fund
        has_ohlcv = sym in daily
        has_price = s.get('latest_price') is not None
        # Alias?
        alias_target = ALIAS_MAP.get(sym)
        if alias_target is not None:
            # Has a replacement — mark alias_for + inactive
            alias_updates.append({'symbol': sym, 'alias_for': alias_target, 'is_active': False})
        elif sym in ALIAS_MAP and ALIAS_MAP[sym] is None:
            # In map with no replacement = delisted
            inactive.append(sym)
        elif not has_fund and not has_ohlcv and not has_price:
            # Triple-empty — dead listing
            inactive.append(sym)

    print(f'\n[summary]')
    print(f'  Active: {len(master) - len(inactive) - len(alias_updates)}')
    print(f'  Inactive (no data anywhere): {len(inactive)}')
    print(f'  Inactive (alias to renamed): {len(alias_updates)}')

    # Sample
    print(f'\nSample inactive (no data):')
    for s in inactive[:10]:
        print(f'  {s}')
    print(f'\nSample alias updates:')
    for a in alias_updates[:10]:
        print(f'  {a["symbol"]} → {a["alias_for"]}')

    # Apply
    print()
    print(f'[apply] PATCHing {len(inactive)} inactive + {len(alias_updates)} alias rows...')
    from concurrent.futures import ThreadPoolExecutor
    ok = 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        # Inactive: set is_active=false
        for n in ex.map(lambda s: patch_row(s, {'is_active': False}), inactive):
            ok += n
        # Aliases: set alias_for + is_active=false
        for a in alias_updates:
            sym = a['symbol']
            if patch_row(sym, {'alias_for': a['alias_for'], 'is_active': False}):
                ok += 1

    total = len(inactive) + len(alias_updates)
    print(f'[done] {ok}/{total} rows updated')


if __name__ == '__main__':
    main()
