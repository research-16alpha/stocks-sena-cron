"""
pledge_backfill.py  [--apply] [--full]   (default DRY-RUN, sample only)
======================================================================
Populates promoter PLEDGE % from the shareholding (SHP) XBRL we already have
URLs for. For each stock's LATEST shareholding period:
  pledged_shares = max(NumberOfSharesEncumberedUnderPledged) in the SHP XBRL
  promoter_shares = promoter_pct/100 * total_shares  (from shareholding_periods)
  pledge_pct = pledged_shares / promoter_shares * 100   (capped 100)

Writes stock_master.pledged_pct. DRY-RUN by default on a small validation sample
(stocks with known pledge events); pass --full to run the whole universe.
"""
import os, sys, re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

URL = "https://tbeadvvkqyrhtendttrg.supabase.co"
KEY = os.environ.get("SUPABASE_SERVICE_KEY") or open(r"e:/Stocks sena/.supabase-service-key").read().strip()
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
APPLY = "--apply" in sys.argv
FULL = "--full" in sys.argv
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121 Safari/537.36"}
PLEDGE_RE = re.compile(r"NumberOfSharesEncumberedUnderPledged[^>]*>\s*(\d+)", re.I)


def latest_shp():
    """symbol -> {period, xbrl_url, promoter_pct, total_shares}, the most recent SHP per symbol."""
    best, off = {}, 0
    while True:
        r = requests.get(f"{URL}/rest/v1/shareholding_periods?select=symbol,period,xbrl_url,promoter_pct,total_shares"
                         f"&xbrl_url=not.is.null&order=period.desc", headers={**H, "Range": f"{off}-{off+999}"}, timeout=90)
        b = r.json() if r.status_code == 200 else []
        if not b:
            break
        for x in b:
            s = x["symbol"]
            if s not in best:                         # first seen = latest (ordered desc)
                best[s] = x
        if len(b) < 1000:
            break
        off += 1000
    return best


SHARES = {}  # symbol -> est total shares (market_cap_cr*1e7 / price)


def load_shares():
    off = 0
    while True:
        r = requests.get(f"{URL}/rest/v1/stock_master?select=symbol,market_cap_cr,latest_price",
                         headers={**H, "Range": f"{off}-{off+999}"}, timeout=60)
        b = r.json() if r.status_code == 200 else []
        if not b:
            break
        for x in b:
            mc, px = x.get("market_cap_cr"), x.get("latest_price")
            if mc and px and px > 0:
                SHARES[x["symbol"]] = mc * 1e7 / px
        if len(b) < 1000:
            break
        off += 1000


def parse_pledge(rec):
    sym = rec["symbol"]
    try:
        t = requests.get(rec["xbrl_url"], headers=UA, timeout=25).text
    except Exception:
        return None
    pledged = max((int(m) for m in PLEDGE_RE.findall(t)), default=0)
    prom_pct, tot = rec.get("promoter_pct"), SHARES.get(sym)
    if not prom_pct or not tot:
        prom_shares = None
    else:
        prom_shares = prom_pct / 100.0 * tot
    if pledged == 0:
        pct = 0.0
    elif prom_shares and prom_shares > 0:
        pct = round(min(100.0, pledged / prom_shares * 100), 2)
    else:
        pct = None
    return (sym, rec["period"], pledged, pct)


def main():
    load_shares()
    shp = latest_shp()
    if FULL:
        targets = list(shp.values())
    else:
        r = requests.get(f"{URL}/rest/v1/promoter_actions?select=symbol&action_type=in.(pledge_increase,pledge_decrease)&limit=80",
                         headers={**H, "Range": "0-79"}, timeout=60)
        samp = {x["symbol"] for x in r.json()}
        targets = [shp[s] for s in samp if s in shp]
    print(f"[pledge] {'FULL ' if FULL else 'SAMPLE '}{len(targets)} stocks  mode={'APPLY' if APPLY else 'DRY-RUN'}")
    results, pledged_cos = [], []
    with ThreadPoolExecutor(max_workers=12) as ex:
        done = 0
        for f in as_completed([ex.submit(parse_pledge, rec) for rec in targets]):
            r = f.result()
            done += 1
            if FULL and done % 500 == 0:
                print(f"   ...{done}/{len(targets)}")
            if not r:
                continue
            results.append(r)
            if r[3] and r[3] > 0:
                pledged_cos.append(r)
    print(f"[pledge] parsed {len(results)}   with pledge>0: {len(pledged_cos)}")
    for sym, per, sh, pct in sorted(pledged_cos, key=lambda x: -(x[3] or 0))[:25]:
        print(f"   {sym:12} {per}  pledged_shares={sh:>12,}  pledge%={pct}")
    if APPLY:
        ok = 0
        for sym, per, sh, pct in results:
            if pct is None:
                continue
            resp = requests.patch(f"{URL}/rest/v1/stock_master?symbol=eq.{sym}",
                                  headers={**H, "Prefer": "return=minimal"},
                                  data=f'{{"pledged_pct": {pct}}}', timeout=60)
            if resp.status_code in (200, 204):
                ok += 1
        print(f"[pledge] updated stock_master.pledged_pct for {ok} stocks")
    elif results:
        print("[pledge] DRY-RUN — add --apply (and --full for all stocks) to write.")


if __name__ == "__main__":
    main()
