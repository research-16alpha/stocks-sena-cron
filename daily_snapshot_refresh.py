"""
daily_snapshot_refresh.py
=========================
Refreshes the `snapshot` field in every fundamentals-v2 bundle daily.

Snapshot fields refreshed (from Yahoo Finance quote API):
  - current_price       (regularMarketPrice)
  - pe                  (trailingPE)
  - book_value          (bookValue)
  - div_yield           (trailingAnnualDividendYield * 100)
  - market_cap          (marketCap / 1e7 → crore)
  - 52_week_high        (fiftyTwoWeekHigh)
  - 52_week_low         (fiftyTwoWeekLow)
  - prev_close          (regularMarketPreviousClose)
  - day_change_pct      ((current - prev) / prev * 100)
  - face_value          (kept from existing if Yahoo doesn't expose)
  - roe, roce           (kept from prior — computed from fundamentals, not market data)

Source: Yahoo Finance quote API (batched up to 100 symbols per request).
        Same endpoint our daily OHLCV cron uses successfully.

Output: Updates snapshot[0] in each fundamentals-v2/<sym>.json and re-uploads.
        Adds 'snapshot_refreshed_at' timestamp.

Safety: ONLY mutates the snapshot field. All annual/quarterly/etc data untouched.
        Resume-capable: skips symbols whose snapshot was refreshed today.

Designed for GitHub Actions: completes in ~10-15 min for 4,500 stocks.

Usage:
  python daily_snapshot_refresh.py                # refresh all stocks with bundles
  python daily_snapshot_refresh.py --syms RELIANCE
  python daily_snapshot_refresh.py --force        # ignore today-skip
"""
import argparse
import json
import os
import sys
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not KEY:
    try:
        with open('e:/Stocks sena/.supabase-service-key', 'r') as f:
            KEY = f.read().strip()
    except FileNotFoundError:
        print('[ERR] SUPABASE_SERVICE_KEY env var or local file required', file=sys.stderr)
        sys.exit(1)
URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

BUCKET = 'fundamentals-v2'
YAHOO_CHART = 'https://query1.finance.yahoo.com/v8/finance/chart'
YAHOO_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
}

IST = timezone(timedelta(hours=5, minutes=30))
TODAY_IST = datetime.now(IST).strftime('%Y-%m-%d')

WORKERS = 6
SLEEP_BETWEEN = 0.3   # seconds between Yahoo chart calls per worker


def fetch_v2_symbols() -> list:
    syms = []
    offset = 0
    while True:
        r = requests.post(
            f'{URL}/storage/v1/object/list/{BUCKET}',
            headers={**H, 'Content-Type': 'application/json'},
            json={'prefix': '', 'limit': 1000, 'offset': offset,
                  'sortBy': {'column': 'name', 'order': 'asc'}}, timeout=30,
        )
        b = r.json()
        if not b: break
        for o in b:
            n = o.get('name', '')
            if n.endswith('.json'):
                syms.append(n[:-5])
        if len(b) < 1000: break
        offset += 1000
    return syms


def _fetch_quote_single(yahoo_sym: str) -> dict:
    """Call Yahoo v8 chart API for one ticker form. Returns {} on failure."""
    try:
        r = requests.get(f'{YAHOO_CHART}/{yahoo_sym}',
                         params={'interval': '1d', 'range': '1d'},
                         headers=YAHOO_HEADERS, timeout=15)
        if r.status_code != 200:
            return {}
        meta = (r.json().get('chart', {}).get('result') or [{}])[0].get('meta', {})
        if not meta:
            return {}
        current = meta.get('regularMarketPrice')
        if current is None:
            return {}
        prev = meta.get('chartPreviousClose') or meta.get('previousClose')
        day_change = None
        day_change_pct = None
        if current is not None and prev is not None and prev > 0:
            day_change = current - prev
            day_change_pct = (day_change / prev) * 100
        return {
            'current_price': current,
            'prev_close': prev,
            'day_change': day_change,
            'day_change_pct': day_change_pct,
            'week52_high': meta.get('fiftyTwoWeekHigh'),
            'week52_low': meta.get('fiftyTwoWeekLow'),
            'long_name': meta.get('longName'),
            'currency': meta.get('currency'),
            '_ticker_used': yahoo_sym,
        }
    except Exception:
        return {}


def fetch_quote_v8(symbol: str) -> dict:
    """Get current price with fallback chain:
      1. .NS suffix (NSE-listed, default)
      2. .BO suffix (BSE-listed by symbol, e.g. BLUESTARCO.BO)
      3. Numeric scrip code + .BO (BSE-only stocks named like BSE500013 → 500013.BO)
      4. Bare symbol (indices, FX)
    """
    # 1. NSE
    q = _fetch_quote_single(f'{symbol}.NS')
    if q: return q
    # 2. BSE by symbol
    q = _fetch_quote_single(f'{symbol}.BO')
    if q: return q
    # 3. BSE by numeric scrip code (for symbols like BSE500013, BSE_501242)
    import re
    m = re.match(r'^BSE_?(\d+)$', symbol)
    if m:
        q = _fetch_quote_single(f'{m.group(1)}.BO')
        if q: return q
    # 4. Bare symbol (indices, FX)
    q = _fetch_quote_single(symbol)
    return q


def compute_derived(price: float, bundle: dict) -> dict:
    """Compute PE, PB, market cap from fundamentals data."""
    out = {'pe': None, 'pb': None, 'market_cap_cr': None,
           'book_value': None, 'eps_ttm': None}
    if not price or price <= 0:
        return out

    # EPS TTM from quarterly (sum of last 4 quarters)
    qrs = (bundle.get('quarterly_results') or [])[-4:]
    if len(qrs) >= 4:
        try:
            eps_ttm = sum((r.get('eps') or 0) for r in qrs)
            if eps_ttm and eps_ttm > 0:
                out['eps_ttm'] = round(eps_ttm, 2)
                out['pe'] = round(price / eps_ttm, 2)
        except Exception:
            pass
    # Fallback to latest annual EPS
    if out['pe'] is None:
        pl = bundle.get('annual_pl') or []
        if pl:
            eps = (pl[-1] or {}).get('eps') or (pl[-1] or {}).get('basic_eps')
            if eps and eps > 0:
                out['eps_ttm'] = eps
                out['pe'] = round(price / eps, 2)

    # Book value per share from latest BS
    bs = bundle.get('annual_bs') or []
    if bs:
        latest_bs = bs[-1] or {}
        equity_cap = latest_bs.get('equity_capital')  # crore
        reserves = latest_bs.get('reserves')
        total_eq = latest_bs.get('total_equity')
        # Face value from prior snapshot if any
        face_val = ((bundle.get('snapshot') or [{}])[0]).get('face_value')
        if not face_val:
            # Common defaults
            face_val = 10.0
        if equity_cap and equity_cap > 0 and face_val:
            num_shares_cr = equity_cap / face_val   # shares in crore (since equity_cap is in cr)
            total_equity_cr = total_eq or ((equity_cap or 0) + (reserves or 0))
            if num_shares_cr > 0:
                bvps = (total_equity_cr * 1e7) / (num_shares_cr * 1e7)  # = total_equity_cr / num_shares_cr
                out['book_value'] = round(bvps, 2)
                if bvps > 0:
                    out['pb'] = round(price / bvps, 2)
                # Market cap = price (rupees) × num_shares (cr count × 1e7) → divided by 1e7 to get cr
                mcap_cr = (price * num_shares_cr * 1e7) / 1e7
                out['market_cap_cr'] = round(mcap_cr, 2)

    return out


def merge_snapshot(bundle: dict, fresh: dict, derived: dict) -> dict:
    """Replace snapshot[0] with fresh price (Yahoo) + computed ratios (from local fundamentals)."""
    existing = (bundle.get('snapshot') or [{}])[0] if bundle.get('snapshot') else {}
    merged = {
        'symbol': bundle.get('symbol'),
        # Fresh from Yahoo (price only)
        'current_price': fresh.get('current_price'),
        'prev_close': fresh.get('prev_close'),
        'day_change': fresh.get('day_change'),
        'day_change_pct': fresh.get('day_change_pct'),
        'week52_high': fresh.get('week52_high'),
        'week52_low': fresh.get('week52_low'),
        'currency': fresh.get('currency') or 'INR',
        # Computed from local fundamentals
        'pe': derived.get('pe'),
        'pb': derived.get('pb'),
        'book_value': derived.get('book_value'),
        'eps_ttm': derived.get('eps_ttm'),
        'market_cap_cr': derived.get('market_cap_cr'),
        # Kept from existing (Screener historical values)
        'roe': existing.get('roe'),
        'roce': existing.get('roce'),
        'div_yield': existing.get('div_yield'),
        'face_value': existing.get('face_value'),
        'long_name': fresh.get('long_name') or existing.get('long_name'),
        # Stamp
        '_source': 'yahoo_v8_chart+computed',
        'fetched_date': TODAY_IST,
    }
    bundle['snapshot'] = [merged]
    bundle.setdefault('provenance', {})['snapshot_refreshed_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    return bundle


def download_bundle(sym: str) -> dict:
    url = f'{URL}/storage/v1/object/public/{BUCKET}/{sym}.json'
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            return None
        return r.json()
    except (requests.exceptions.JSONDecodeError, ValueError, requests.exceptions.RequestException):
        return None
    except Exception:
        return None


def safe_process_symbol(sym: str, force: bool) -> tuple:
    """Wrap process_symbol to never raise — bad responses can't kill the pool."""
    try:
        return process_symbol(sym, force)
    except Exception as e:
        return (sym, 'EXCEPTION', str(e)[:200])


def upload_bundle(sym: str, bundle: dict) -> bool:
    payload = json.dumps(bundle, default=str, separators=(',', ':')).encode('utf-8')
    headers = {**H, 'Content-Type': 'application/json', 'x-upsert': 'true'}
    url = f'{URL}/storage/v1/object/{BUCKET}/{sym}.json'
    r = requests.post(url, headers=headers, data=payload, timeout=30)
    if r.status_code in (200, 201):
        return True
    r = requests.put(url, headers=headers, data=payload, timeout=30)
    return r.status_code in (200, 201)


def process_symbol(sym: str, force: bool) -> tuple:
    b = download_bundle(sym)
    if not b:
        return (sym, 'NO_BUNDLE', None)
    if not force:
        last = (b.get('provenance') or {}).get('snapshot_refreshed_at', '')
        if last.startswith(TODAY_IST):
            return (sym, 'SKIPPED_RECENT', None)
    fresh = fetch_quote_v8(sym)
    if not fresh or fresh.get('current_price') is None:
        return (sym, 'NO_QUOTE', None)
    derived = compute_derived(fresh['current_price'], b)
    merged = merge_snapshot(b, fresh, derived)
    if upload_bundle(sym, merged):
        return (sym, 'OK', fresh.get('current_price'))
    return (sym, 'UPLOAD_ERR', None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', type=str, default='')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    if args.syms:
        syms = [s.strip().upper() for s in args.syms.split(',') if s.strip()]
    else:
        print('[INFO] Listing fundamentals-v2 bundles...')
        syms = fetch_v2_symbols()
    if args.limit:
        syms = syms[:args.limit]
    # BSE-prefixed symbols (BSE500013, BSE_501242) are processed via the
    # numeric-scrip-code fallback in fetch_quote_v8() — 500013.BO etc.
    print(f'[INFO] Processing {len(syms)} symbols (incl. BSE_<scrip> via numeric .BO fallback)')

    t0 = time.time()
    ok = no_quote = no_bundle = upload_err = skipped = 0

    exception_count = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(safe_process_symbol, s, args.force): s for s in syms}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                sym, status, price = fut.result()
            except Exception:
                exception_count += 1
                continue
            if status == 'OK': ok += 1
            elif status == 'NO_QUOTE': no_quote += 1
            elif status == 'NO_BUNDLE': no_bundle += 1
            elif status == 'UPLOAD_ERR': upload_err += 1
            elif status == 'SKIPPED_RECENT': skipped += 1
            elif status == 'EXCEPTION': exception_count += 1

            if i % 200 == 0:
                rate = i / (time.time() - t0)
                eta = (len(syms) - i) / rate if rate > 0 else 0
                print(f'  [{i}/{len(syms)}] ok={ok} no_quote={no_quote} '
                      f'no_bundle={no_bundle} upload_err={upload_err} skipped={skipped}  '
                      f'rate={rate:.1f}/s  eta={eta:.0f}s')

    print()
    print('-' * 60)
    print(f'  Total            : {len(syms)}')
    print(f'  Snapshots updated: {ok}')
    print(f'  No quote (Yahoo) : {no_quote}')
    print(f'  No bundle        : {no_bundle}')
    print(f'  Upload errors    : {upload_err}')
    print(f'  Skipped recent   : {skipped}')
    print(f'  Exceptions       : {exception_count}')
    print(f'  Elapsed          : {time.time()-t0:.1f}s')

    # Sprint 2: push price/mcap/PE back to stock_master table so search,
    # screener, sector heatmap all pick up the fresh data. Without this the
    # snapshot lives only in bundle JSONs and the app's table queries stay stale.
    print()
    print('=' * 60)
    print('[INFO] Syncing snapshots to stock_master table...')
    try:
        import sync_snapshot_to_stock_master  # type: ignore
        sync_snapshot_to_stock_master.main()
    except ImportError:
        print('[WARN] sync_snapshot_to_stock_master.py not found in path — skipping sync')
    except Exception as e:
        print(f'[ERR] sync failed: {e}', file=sys.stderr)


if __name__ == '__main__':
    main()
