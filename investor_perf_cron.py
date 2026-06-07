"""
investor_perf_cron.py
=====================
Precompute the price-history-dependent parts of the Super Investors pages that are too
heavy to do per request: the NET-WORTH TREND (disclosed portfolio value per quarter) and
TRAILING RETURNS vs NIFTY (1M / 3M / 1Y of the current basket).

Inputs (all already stored, no Kite calls):
  - per-investor holdings + per-quarter shares  -> get_investor_portfolio / get_investor_matrix RPCs
  - per-stock 21y daily closes                  -> public storage  daily/<SYMBOL>.json  (bars [d,o,h,l,c,v])
  - NIFTY 50 daily closes                        -> Yahoo ^NSEI (same source the OHLCV cron already uses)

Output: one small JSON per investor at  daily/investors/<slug>.json  (public bucket), e.g.
  { slug, as_of, networth:[{q,v}], ret:{m1,m3,y1}, nifty:{m1,m3,y1} }   (v in cr, ret in %)

Run:  py investor_perf_cron.py --dry     # compute + print, no upload
      py investor_perf_cron.py           # compute + upload
"""
import argparse, bisect, datetime, json, os, sys, urllib.parse, urllib.request

SB_URL = os.environ.get("SUPABASE_URL", "https://tbeadvvkqyrhtendttrg.supabase.co").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY") or open(r"e:/Stocks sena/.supabase-service-key").read().strip()
HDR = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
HORIZONS = {"m1": 30, "m3": 91, "y1": 365}


def http_json(url, headers=None, timeout=40):
    req = urllib.request.Request(url, headers={**(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def rpc(name, params):
    return http_json(f"{SB_URL}/rest/v1/rpc/{name}?{urllib.parse.urlencode(params)}", HDR)


def stock_series(sym):
    """(sorted dates[], closes[]) from daily/<sym>.json, or ([],[]) if missing."""
    try:
        j = http_json(f"{SB_URL}/storage/v1/object/public/daily/{urllib.parse.quote(sym)}.json")
        bars = j.get("bars") or []
        d, c = [], []
        for b in bars:
            if b and b[0] and b[4] is not None:
                d.append(str(b[0])[:10]); c.append(float(b[4]))
        return d, c
    except Exception:
        return [], []


NIFTY_TOKEN = 256265  # Kite instrument_token for the NIFTY 50 index


def _kite_creds():
    """Kite api_key + access_token from the app_config vault (same row the price crons use)."""
    try:
        rows = http_json(f"{SB_URL}/rest/v1/app_config?select=key,value&key=in.(kite_api_key,kite_access_token)", HDR)
        cfg = {x["key"]: x["value"] for x in rows}
        if cfg.get("kite_api_key") and cfg.get("kite_access_token"):
            return cfg["kite_api_key"], cfg["kite_access_token"]
    except Exception:
        pass
    return None


def nifty_kite():
    """NIFTY 50 daily closes from Kite (primary). None if no/expired token or empty."""
    creds = _kite_creds()
    if not creds:
        return None
    ak, at = creds
    to = datetime.date.today().isoformat()
    frm = (datetime.date.today() - datetime.timedelta(days=1100)).isoformat()
    url = f"https://api.kite.trade/instruments/historical/{NIFTY_TOKEN}/day?from={frm}&to={to}"
    try:
        req = urllib.request.Request(url, headers={"X-Kite-Version": "3", "Authorization": f"token {ak}:{at}"})
        with urllib.request.urlopen(req, timeout=40) as r:
            candles = (json.loads(r.read()).get("data") or {}).get("candles") or []
        d = [str(c[0])[:10] for c in candles]
        c = [float(c[4]) for c in candles]
        return (d, c) if len(d) > 50 else None
    except Exception:
        return None


def nifty_yahoo():
    """NIFTY 50 daily closes from Yahoo ^NSEI (no-auth fallback)."""
    j = http_json("https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1d&range=3y", UA)
    res = j["chart"]["result"][0]
    ts, cl = res["timestamp"], res["indicators"]["quote"][0]["close"]
    d, c = [], []
    for t, x in zip(ts, cl):
        if x is None:
            continue
        d.append(datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")); c.append(float(x))
    return d, c


def nifty_stored():
    """Our own Kite-sourced NIFTY series at daily/index/NIFTY.json (PRIMARY - no live call, one source of truth)."""
    try:
        j = http_json(f"{SB_URL}/storage/v1/object/public/daily/index/NIFTY.json")
        bars = j.get("bars") or []
        d = [str(b[0])[:10] for b in bars]
        c = [float(b[4]) for b in bars]
        return (d, c) if len(d) > 50 else None
    except Exception:
        return None


def nifty_series():
    """Read our stored Kite series first; live Kite if the store is missing; Yahoo only as last resort."""
    s = nifty_stored()
    if s:
        print("[perf] NIFTY <- daily/index/NIFTY.json (our Kite store)", flush=True)
        return s
    k = nifty_kite()
    if k:
        print("[perf] NIFTY <- Kite live (store missing)", flush=True)
        return k
    print("[perf] NIFTY <- Yahoo (last resort)", flush=True)
    return nifty_yahoo()


def close_at(dates, closes, target):
    """close on or before target ('YYYY-MM-DD'); None if no earlier bar."""
    i = bisect.bisect_right(dates, target) - 1
    return closes[i] if i >= 0 else None


def put_storage(path, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{SB_URL}/storage/v1/object/{path}", data=body, method="POST",
                                 headers={**HDR, "Content-Type": "application/json", "x-upsert": "true"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--only", help="single slug for testing")
    a = ap.parse_args()

    investors = http_json(f"{SB_URL}/rest/v1/superstar_investors?select=slug,display_name&order=sort_order", HDR)
    if a.only:
        investors = [i for i in investors if i["slug"] == a.only]
    ndates, ncloses = nifty_series()
    today = ndates[-1]
    print(f"[perf] {len(investors)} investors · NIFTY through {today} ({len(ndates)} bars)", flush=True)

    cache = {}
    def series(sym):
        if sym not in cache:
            cache[sym] = stock_series(sym)
        return cache[sym]

    for inv in investors:
        slug = inv["slug"]
        matrix = rpc("get_investor_matrix", {"p_slug": slug})
        port = rpc("get_investor_portfolio", {"p_slug": slug})

        # net-worth per quarter = sum(shares held that quarter x close at quarter-end)
        byq = {}
        for row in matrix:
            sh = row.get("shares"); q = row["period"]
            if not sh:
                continue
            d, c = series(row["symbol"])
            px = close_at(d, c, q)
            if px:
                byq[q] = byq.get(q, 0.0) + sh * px
        networth = [{"q": q, "v": round(byq[q] / 1e7, 1)} for q in sorted(byq)]

        # trailing returns of the CURRENT basket, plus NIFTY over the same horizons
        ret, nifty = {}, {}
        for k, days in HORIZONS.items():
            tgt = (datetime.date.fromisoformat(today) - datetime.timedelta(days=days)).isoformat()
            vnow = vthen = 0.0
            for h in port:
                sh = h.get("shares")
                if not sh:
                    continue
                d, c = series(h["symbol"])
                pn, pt = close_at(d, c, today), close_at(d, c, tgt)
                if pn and pt:
                    vnow += sh * pn; vthen += sh * pt
            ret[k] = round((vnow / vthen - 1) * 100, 1) if vthen > 0 else None
            nn, nt = close_at(ndates, ncloses, today), close_at(ndates, ncloses, tgt)
            nifty[k] = round((nn / nt - 1) * 100, 1) if (nn and nt) else None

        payload = {"slug": slug, "as_of": today, "networth": networth, "ret": ret, "nifty": nifty}
        nw_last = networth[-1]["v"] if networth else None
        print(f"  {slug:26} networth_pts={len(networth):2} latest={nw_last} cr · "
              f"ret(1y)={ret['y1']}% vs NIFTY {nifty['y1']}%", flush=True)
        if not a.dry:
            put_storage(f"daily/investors/{slug}.json", payload)

    print("[perf] done." + (" (dry - nothing written)" if a.dry else ""), flush=True)


if __name__ == "__main__":
    main()
