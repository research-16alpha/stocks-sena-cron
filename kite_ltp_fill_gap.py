"""
kite_ltp_fill_gap.py
====================
Fills LIVE PRICE + market cap for stocks Yahoo can't reach.

Yahoo coverage caps at ~56% of stock_master (Yahoo doesn't index many BSE
microcaps + dead listings). Kite has 100% of NSE+BSE.

Strategy:
  1. List all stock_master symbols with null latest_price
  2. Match to Kite instruments via symbol / ISIN / BSE scrip
  3. Batch-fetch quotes via kite.ltp() (250 instruments per call)
  4. Compute market cap using last_price × shares_outstanding from bundle
  5. PATCH stock_master with latest_price + market_cap_cr

Run weekly (or whenever you want gap-fill). Free with Kite paid API access.
Credentials in e:/Stocks sena/.kite-credentials (local, never pushed).
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error
from typing import Dict, List, Optional, Tuple

try:
    from kiteconnect import KiteConnect
except ImportError:
    print('[ERR] pip install kiteconnect', file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kite_backfill_ohlcv import load_credentials, ensure_kite_session

CRED_FILE = r'e:\Stocks sena\.kite-credentials'
SUPABASE_KEY_FILE = r'e:\Stocks sena\.supabase-service-key'
SUPABASE_URL = 'https://tbeadvvkqyrhtendttrg.supabase.co'

with open(SUPABASE_KEY_FILE) as f:
    SB_KEY = f.read().strip()
SB_HEADERS = {'apikey': SB_KEY, 'Authorization': f'Bearer {SB_KEY}'}

LTP_BATCH = 250  # Kite caps quote/ltp at 500, we use 250 to be safe


def fetch_master() -> List[Dict]:
    syms = []
    offset = 0
    while True:
        url = f'{SUPABASE_URL}/rest/v1/stock_master?select=symbol,name,isin,latest_price,market_cap_cr&limit=1000&offset={offset}'
        r = urllib.request.urlopen(urllib.request.Request(url, headers=SB_HEADERS), timeout=20)
        batch = json.loads(r.read())
        if not batch: break
        syms.extend(batch)
        if len(batch) < 1000: break
        offset += 1000
    return syms


def build_kite_index(kite: KiteConnect) -> Tuple[Dict[str, Tuple[int, str, str]], Dict[str, Tuple[int, str, str]]]:
    """Returns (by_symbol, by_isin) — each maps to (token, exchange_trading_pair, tradingsymbol)
    Exchange_trading_pair is 'NSE:RELIANCE' or 'BSE:FGP' format for kite.ltp() calls."""
    nse = kite.instruments('NSE')
    bse = kite.instruments('BSE')
    by_sym: Dict[str, Tuple[int, str, str]] = {}
    by_isin: Dict[str, Tuple[int, str, str]] = {}
    for exch_name, lst in (('NSE', nse), ('BSE', bse)):
        for inst in lst:
            if inst.get('segment') not in ('NSE', 'BSE') or inst.get('instrument_type') != 'EQ':
                continue
            sym = inst.get('tradingsymbol')
            token = inst.get('instrument_token')
            isin = inst.get('isin')
            scrip = inst.get('exchange_token')
            if not (sym and token): continue
            pair = f'{exch_name}:{sym}'
            if sym not in by_sym:
                by_sym[sym] = (token, pair, sym)
            if isin and isin not in by_isin:
                by_isin[isin] = (token, pair, sym)
            if exch_name == 'BSE' and scrip:
                by_sym.setdefault(f'BSE{scrip}', (token, pair, sym))
                by_sym.setdefault(f'BSE_{scrip}', (token, pair, sym))
    return by_sym, by_isin


def fetch_bundle_shares(sym: str) -> Optional[float]:
    """Read shares-outstanding (in crore) from the bundle's latest BS equity_capital / face_value."""
    try:
        r = urllib.request.urlopen(
            f'{SUPABASE_URL}/storage/v1/object/public/fundamentals-v2/{sym}.json',
            timeout=10,
        )
        b = json.loads(r.read())
        bs = b.get('annual_bs') or b.get('annual_bs_consolidated') or b.get('annual_bs_standalone') or []
        if not bs: return None
        latest = bs[-1]
        eq_cap = latest.get('equity_capital')  # in crore
        face_val = (b.get('snapshot') or [{}])[0].get('face_value') or 10.0
        if eq_cap and eq_cap > 0 and face_val:
            return eq_cap / face_val  # shares in crore (since eq_cap is in cr already)
        return None
    except Exception:
        return None


def patch_stock_master(updates: List[Dict]) -> int:
    """PATCH each row individually (PostgREST doesn't support bulk PATCH by symbol)."""
    from concurrent.futures import ThreadPoolExecutor

    def patch_one(row: Dict) -> int:
        sym = row.pop('symbol')
        body = {k: v for k, v in row.items() if v is not None}
        if not body: return 0
        url = f'{SUPABASE_URL}/rest/v1/stock_master?symbol=eq.{sym}'
        headers = {**SB_HEADERS, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}
        payload = json.dumps(body).encode('utf-8')
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method='PATCH')
            r = urllib.request.urlopen(req, timeout=15)
            return 1 if r.status in (200, 204) else 0
        except Exception:
            return 0

    pushed = 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        for n in ex.map(patch_one, updates, chunksize=5):
            pushed += n
    return pushed


def main():
    creds = load_credentials()
    kite = ensure_kite_session(creds)

    print('[load] stock_master...')
    master = fetch_master()
    print(f'  {len(master)} stocks')

    # Pick stocks with NULL latest_price — those are the gap we're filling
    gap = [s for s in master if not s.get('latest_price')]
    print(f'  {len(gap)} stocks need price fill')

    if not gap:
        print('[done] No gap to fill')
        return

    print('[load] Kite instruments...')
    by_sym, by_isin = build_kite_index(kite)

    # Build symbol→Kite pair mapping for the gap
    pairs_to_fetch: List[Tuple[str, str, int, str]] = []  # (our_sym, kite_pair, token, kite_tradingsymbol)
    no_match = []
    for s in gap:
        sym = s['symbol']
        isin = s.get('isin')
        match = by_sym.get(sym) or (by_isin.get(isin) if isin else None)
        if match:
            token, pair, kite_sym = match
            pairs_to_fetch.append((sym, pair, token, kite_sym))
        else:
            no_match.append(sym)

    print(f'  matched={len(pairs_to_fetch)} no_match={len(no_match)}')
    if no_match[:5]:
        print(f'  sample no_match: {no_match[:5]}')

    if not pairs_to_fetch:
        print('[done] Nothing to fetch')
        return

    # Batch fetch quotes (kite.ltp returns {'NSE:RELIANCE': {'instrument_token':..., 'last_price':...}})
    print(f'[fetch] {len(pairs_to_fetch)} quotes in batches of {LTP_BATCH}...')
    quote_results: Dict[str, float] = {}  # our_sym → last_price
    t0 = time.time()
    for i in range(0, len(pairs_to_fetch), LTP_BATCH):
        batch = pairs_to_fetch[i:i + LTP_BATCH]
        pairs = [p[1] for p in batch]
        try:
            quotes = kite.ltp(pairs)
        except Exception as e:
            print(f'  [WARN] batch {i // LTP_BATCH}: {e}', file=sys.stderr)
            continue
        for our_sym, pair, token, kite_sym in batch:
            q = quotes.get(pair)
            if q and q.get('last_price'):
                quote_results[our_sym] = q['last_price']
        print(f'  {i + len(batch)}/{len(pairs_to_fetch)}  got={len(quote_results)}')
        time.sleep(0.35)  # rate limit (3 req/sec)

    print(f'[fetch] {len(quote_results)} quotes captured in {time.time()-t0:.0f}s')

    if not quote_results:
        print('[done] No quotes returned')
        return

    # Optionally compute market cap via bundle shares
    print('[mcap] computing market caps via bundle equity_capital/face_value...')
    updates = []
    no_mcap = 0
    for sym, price in quote_results.items():
        update = {'symbol': sym, 'latest_price': round(price, 2)}
        shares_cr = fetch_bundle_shares(sym)
        if shares_cr and shares_cr > 0:
            mcap_cr = round(price * shares_cr, 2)
            update['market_cap_cr'] = mcap_cr
        else:
            no_mcap += 1
        updates.append(update)
    print(f'  price-only={no_mcap}  price+mcap={len(updates) - no_mcap}')

    # Push to stock_master
    print(f'[patch] updating stock_master for {len(updates)} stocks...')
    pushed = patch_stock_master(updates)
    print(f'[done] pushed {pushed}/{len(updates)} rows in {time.time()-t0:.0f}s total')


if __name__ == '__main__':
    main()
