"""
kite_alias_backfill.py
======================
Second-pass Kite OHLCV backfill for stocks that didn't match by tradingsymbol
in the main backfill. Matches via ISIN as fallback, then alternative trading
symbol variants (renames/demergers).

Run after kite_backfill_ohlcv.py. Idempotent — only processes stocks still
missing from the `daily` bucket.
"""
import os
import sys
import json
import time
import urllib.request
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

try:
    from kiteconnect import KiteConnect
except ImportError:
    print('[ERR] pip install kiteconnect', file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kite_backfill_ohlcv import (
    load_credentials, ensure_kite_session, fetch_historical,
    upload_to_supabase, fetch_supabase_symbols, SUPABASE_URL, BUCKET,
)

SUPABASE_KEY_FILE = r'e:\Stocks sena\.supabase-service-key'

# Manual symbol aliases — for renames/demergers Kite has under different name
MANUAL_ALIAS_MAP: Dict[str, List[str]] = {
    'TATAMOTORS':   ['TATAMOTORS', 'TATAMOTORSDV'],
    'TATAMTRDVR':   ['TATAMTRDVR', 'TATAMOTORSDV'],
    'PVR':          ['PVRINOX'],
    'INOXLEISUR':   ['PVRINOX'],
    'LTIM':         ['LTIM', 'LTI', 'MINDTREE'],
    'MINDTREE':     ['LTIM'],
    'LTI':          ['LTIM'],
    'AMARAJABAT':   ['ARE&M', 'AMARARAJA'],
    'ADANITRANS':   ['ADANIENSOL', 'ADANIENERGY'],
    'MCDOWELL-N':   ['UNITDSPR', 'MCDOWELL'],
    'MGLAB':        ['MERCURYLAB'],
    'MINDAIND':     ['UNOMINDA'],
    'AVENUE':       ['DMART'],
    'BAJAJ-AUTO':   ['BAJAJ-AUTO', 'BAJAJAUTO'],
    'M&M':          ['M&M', 'MAHINDRA'],
    'M&MFIN':       ['M&MFIN', 'MMFIN'],
    'L&TFH':        ['LTF', 'L&TFH'],
}


def fetch_master_with_isin():
    """Get full stock_master with ISIN field."""
    with open(SUPABASE_KEY_FILE) as f: key = f.read().strip()
    headers = {'apikey': key, 'Authorization': f'Bearer {key}'}
    syms = {}
    offset = 0
    while True:
        url = f'{SUPABASE_URL}/rest/v1/stock_master?select=symbol,name,isin&limit=1000&offset={offset}'
        r = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20)
        batch = json.loads(r.read())
        if not batch: break
        for s in batch:
            syms[s['symbol']] = s
        if len(batch) < 1000: break
        offset += 1000
    return syms


def fetch_done_ohlcv() -> set:
    """Symbols that already have OHLCV bundle in daily bucket."""
    with open(SUPABASE_KEY_FILE) as f: key = f.read().strip()
    headers = {'apikey': key, 'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    done = set()
    offset = 0
    while True:
        req = urllib.request.Request(
            f'{SUPABASE_URL}/storage/v1/object/list/{BUCKET}',
            data=json.dumps({'prefix': '', 'limit': 1000, 'offset': offset}).encode(),
            headers=headers, method='POST',
        )
        r = urllib.request.urlopen(req, timeout=30)
        batch = json.loads(r.read())
        if not batch: break
        for f in batch:
            if f['name'].endswith('.json'):
                done.add(f['name'][:-5])
        if len(batch) < 1000: break
        offset += 1000
    return done


def build_full_kite_index(kite: KiteConnect) -> Tuple[Dict[str, Tuple[int, str]], Dict[str, Tuple[int, str]]]:
    """Returns (by_symbol, by_isin) — two parallel maps."""
    nse = kite.instruments('NSE')
    bse = kite.instruments('BSE')
    by_sym: Dict[str, Tuple[int, str]] = {}
    by_isin: Dict[str, Tuple[int, str]] = {}
    for exch_name, lst in (('NSE', nse), ('BSE', bse)):
        for inst in lst:
            if inst.get('segment') not in ('NSE', 'BSE') or inst.get('instrument_type') != 'EQ':
                continue
            sym = inst.get('tradingsymbol')
            token = inst.get('instrument_token')
            isin = inst.get('isin')
            if sym and token and sym not in by_sym:
                by_sym[sym] = (token, exch_name)
            if isin and token and isin not in by_isin:
                by_isin[isin] = (token, exch_name)
            scrip = inst.get('exchange_token')
            if exch_name == 'BSE' and scrip:
                by_sym.setdefault(f'BSE{scrip}', (token, 'BSE'))
                by_sym.setdefault(f'BSE_{scrip}', (token, 'BSE'))
    return by_sym, by_isin


def resolve_symbol(sym: str, master: dict, by_sym: dict, by_isin: dict) -> Optional[Tuple[int, str, str]]:
    """Try to find Kite token via: alias map, ISIN, then bare symbol. Returns (token, exchange, resolved_via)."""
    # 1. Direct symbol match
    if sym in by_sym:
        return (*by_sym[sym], 'direct')
    # 2. Manual alias
    for alt in MANUAL_ALIAS_MAP.get(sym, []):
        if alt in by_sym:
            return (*by_sym[alt], f'alias:{alt}')
    # 3. ISIN
    isin = master.get(sym, {}).get('isin')
    if isin and isin in by_isin:
        return (*by_isin[isin], f'isin:{isin}')
    return None


def process_one(args):
    sym, token, exch, kite, resolved_via = args
    bars = fetch_historical(kite, token)
    if not bars:
        return (sym, 'NO_DATA', 0, resolved_via)
    bundle = {
        'symbol': sym,
        'interval': '1d',
        'from': bars[0][0],
        'to': bars[-1][0],
        'bars': bars,
        'source': f'kite_{exch.lower()}',
        'instrument_token': token,
        'resolved_via': resolved_via,
    }
    ok = upload_to_supabase(sym, bundle)
    return (sym, 'OK' if ok else 'UPLOAD_ERR', len(bars), resolved_via)


def main():
    creds = load_credentials()
    kite = ensure_kite_session(creds)

    print('[load] stock_master...')
    master = fetch_master_with_isin()
    print(f'  {len(master)} stocks')

    print('[load] already-done OHLCV...')
    done = fetch_done_ohlcv()
    print(f'  {len(done)} already have OHLCV')

    print('[load] Kite instruments...')
    by_sym, by_isin = build_full_kite_index(kite)
    print(f'  {len(by_sym)} by symbol, {len(by_isin)} by ISIN')

    # Find stocks missing OHLCV
    missing = [s for s in master if s not in done]
    print(f'\n[scan] {len(missing)} stocks missing OHLCV — trying ISIN/alias resolution')

    # Resolve
    resolved = []
    still_missing = []
    for sym in missing:
        m = resolve_symbol(sym, master, by_sym, by_isin)
        if m:
            token, exch, via = m
            resolved.append((sym, token, exch, kite, via))
        else:
            still_missing.append(sym)

    print(f'[resolve] resolved={len(resolved)} still_missing={len(still_missing)}')
    via_counter = {}
    for r in resolved:
        v = r[4].split(':')[0]
        via_counter[v] = via_counter.get(v, 0) + 1
    print(f'  via: {via_counter}')

    if still_missing[:10]:
        print(f'  sample still missing: {still_missing[:10]}')

    if not resolved:
        print('[done] nothing to fetch')
        return

    # Fetch + upload
    print(f'\n[fetch] {len(resolved)} stocks...')
    t0 = time.time()
    ok = no_data = err = 0
    WORKERS = 3
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, fut in enumerate(as_completed([ex.submit(process_one, r) for r in resolved]), 1):
            try:
                sym, status, bars, via = fut.result()
            except Exception:
                continue
            if status == 'OK': ok += 1
            elif status == 'NO_DATA': no_data += 1
            else: err += 1
            if i % 50 == 0 or i <= 5:
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed > 0 else 0
                eta = (len(resolved) - i) / rate if rate > 0 else 0
                print(f'  {i}/{len(resolved)}  ok={ok}  no_data={no_data}  err={err}  '
                      f'rate={rate:.1f}/s  ETA={eta:.0f}s')

    print(f'\n[done] ok={ok} no_data={no_data} err={err}  elapsed={time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
