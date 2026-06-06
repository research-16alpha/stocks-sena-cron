"""
sector_index_cron.py
====================
Builds our OWN market-cap-weighted sector index LEVEL series (for the charts) from the
constituents' daily closes - no exchange index is used. For each sector:

  level[t] = 100 * Σ_i w_i * (close_i[t] / close_i[t0]) / Σ_i w_i

a fixed-basket cap-weighted price index (w_i = current mcap share; basket = the stocks
present on the first day of the window, so it doesn't jump when a new stock lists).

Uses the top-50 constituents by mcap per sector (they dominate a cap-weighted index;
keeps the run light) over ~520 trading days. Writes one JSON per sector to the public
`daily` bucket at sector/{slug}.json so the website charts it without recomputing.

Run:  py -3.11 sector_index_cron.py --limit 3    # test a few sectors
      py -3.11 sector_index_cron.py              # all sectors
"""
import argparse, json, os, re, datetime
from concurrent.futures import ThreadPoolExecutor

import requests
from supabase import create_client

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
KEY = os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
sb = create_client(URL, KEY)
PUB = f'{URL}/storage/v1/object/public'

WINDOW = 520
TOPN = 50


def slugify(name):
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', (name or '').lower().replace('&', ' and '))).strip('-')


def load_universe():
    out, off = [], 0
    while True:
        d = (sb.table('stock_master').select('symbol,sector,market_cap_cr')
             .eq('is_active', True).not_.is_('sector', 'null').not_.is_('market_cap_cr', 'null')
             .lt('market_cap_cr', 2500000).range(off, off + 999).execute().data) or []
        out += d
        if len(d) < 1000:
            break
        off += 1000
    return out


def load_closes(sym):
    try:
        r = requests.get(f"{PUB}/daily/{sym}.json", timeout=20)
        bars = (r.json() or {}).get('bars') if r.status_code == 200 else None
    except Exception:
        bars = None
    if not bars:
        return {}
    out = {}
    for b in bars:
        try:
            d = str(b[0])[:10]; c = float(b[4])
            if c > 0:
                out[d] = c
        except Exception:
            continue
    return out


def build_sector(sector, members):
    members = sorted(members, key=lambda m: -(m['market_cap_cr'] or 0))[:TOPN]
    closes = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for m, c in zip(members, ex.map(lambda mm: load_closes(mm['symbol']), members)):
            if len(c) >= 30:
                closes[m['symbol']] = c
    if len(closes) < 3:
        return None
    weights = {m['symbol']: (m['market_cap_cr'] or 0) for m in members if m['symbol'] in closes}

    master = sorted(set().union(*[set(c.keys()) for c in closes.values()]))[-WINDOW:]
    # forward-fill each stock onto the master calendar
    ff = {}
    for sym, c in closes.items():
        dates = sorted(c.keys()); series, last, j = [], None, 0
        for d in master:
            while j < len(dates) and dates[j] <= d:
                last = c[dates[j]]; j += 1
            series.append(last)
        ff[sym] = series
    incl = [s for s in closes if ff[s][0] is not None]
    if len(incl) < 3:
        return None

    # Chain mcap-weighted DAILY returns, each clamped to ±25%. Unadjusted closes carry
    # split/bonus jumps (a 1:10 split looks like -90% in a day) and penny tickers spike;
    # clamping the per-day return stops any single artifact from blowing up the index,
    # which a raw price-relative basket can't survive (e.g. a fake +1000% sector).
    CLAMP = 0.25
    level = 100.0
    pts = []
    for k, d in enumerate(master):
        if k == 0:
            pts.append([d, 100.0]); continue
        num = den = 0.0
        for s in incl:
            v, p = ff[s][k], ff[s][k - 1]
            if v is None or p is None or p <= 0:
                continue
            ret = max(-CLAMP, min(CLAMP, v / p - 1))
            num += weights[s] * ret; den += weights[s]
        if den > 0:
            level *= (1 + num / den)
        pts.append([d, round(level, 2)])
    if len(pts) < 10:
        return None
    return {'sector': sector, 'slug': slugify(sector), 'n': len(incl),
            'points': pts, 'updated': datetime.datetime.utcnow().isoformat() + 'Z'}


def upload(slug, payload):
    data = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    path = f'sector/{slug}.json'
    try:
        sb.storage.from_('daily').upload(file=data, path=path,
                                         file_options={'content-type': 'application/json', 'upsert': 'true'})
        return True
    except Exception:
        try:
            sb.storage.from_('daily').remove([path])
        except Exception:
            pass
        try:
            sb.storage.from_('daily').upload(file=data, path=path,
                                             file_options={'content-type': 'application/json'})
            return True
        except Exception as e:
            print(f'  upload err {slug}: {str(e)[:60]}', flush=True)
            return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    uni = load_universe()
    by = {}
    for s in uni:
        if (s['market_cap_cr'] or 0) > 0:
            by.setdefault(s['sector'], []).append(s)
    sectors = [(k, v) for k, v in by.items() if len(v) >= 3]
    sectors.sort(key=lambda kv: -sum(m['market_cap_cr'] or 0 for m in kv[1]))
    if args.limit:
        sectors = sectors[:args.limit]
    print(f'[sector-index] {len(sectors)} sectors', flush=True)

    ok = skip = 0
    sparks = {}  # slug -> last ~40 levels, for the home/list mini-charts (one combined file)
    for i, (sector, members) in enumerate(sectors, 1):
        res = build_sector(sector, members)
        if not res:
            skip += 1; print(f'  [{i}] {sector[:30]:30} SKIP (insufficient history)', flush=True); continue
        last = res['points'][-1][1]; first = res['points'][0][1]
        chg = (last / first - 1) * 100
        print(f"  [{i}] {sector[:30]:30} {res['n']:3} stocks  {len(res['points'])}pts  "
              f"level {last:.0f}  ({'+' if chg >= 0 else ''}{chg:.0f}% over window)", flush=True)
        sparks[res['slug']] = [p[1] for p in res['points'][-40:]]
        if not args.dry_run:
            if upload(res['slug'], res):
                ok += 1
    if not args.dry_run and sparks:
        upload('_sparks', sparks)  # daily/sector/_sparks.json = { slug: [levels...] }
    print(f'[sector-index] {"DRY " if args.dry_run else ""}done: {ok} written, {skip} skipped', flush=True)


if __name__ == '__main__':
    main()
