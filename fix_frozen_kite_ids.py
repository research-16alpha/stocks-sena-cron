"""
fix_frozen_kite_ids.py  [--apply]   (default DRY-RUN)
====================================================
Self-heals Kite identifiers that Kite's quote() API doesn't recognise, so the
live movers cron can never silently FREEZE a stock again.

Background: some active stocks store an id Kite can't quote — overwhelmingly NSE
T2T names that need a "-BE" suffix (e.g. NSE:RUBYMILLS is invalid; the real
symbol is NSE:RUBYMILLS-BE), or BSE-only names. The live quote cron can't fetch
them, so their price / pct / turnover / rvol freeze at the last good value and
pollute the movers board (RUBYMILLS once showed +18.66% from days earlier while
it was actually -1.1%).

This finds every frozen id, resolves it to the correct exchange:tradingsymbol
(validated by a LIVE quote dated today), and patches kite_exchange /
kite_tradingsymbol / kite_token in stock_master. Names with no live match
(suspended/delisted) are only reported, never changed.

Reusable: call heal(kite=<existing client>) from another cron (intraday_movers
calls it once at startup) to auto-heal daily, or run standalone with --apply.
"""
import os, sys, json, datetime
import requests
from kiteconnect import KiteConnect

URL = os.environ.get("SUPABASE_URL", "https://tbeadvvkqyrhtendttrg.supabase.co").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY") or open(r"e:/Stocks sena/.supabase-service-key").read().strip()
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _today_iso():
    return datetime.datetime.now(IST).date().isoformat()


def get_kite():
    cfg = {r["key"]: r.get("value") for r in
           requests.get(URL + "/rest/v1/app_config?select=key,value", headers=H, timeout=30).json()}
    k = KiteConnect(api_key=cfg["kite_api_key"]); k.set_access_token(cfg["kite_access_token"])
    return k


def _all_active():
    rows, off = [], 0
    while True:
        b = requests.get(URL + f"/rest/v1/stock_master?select=symbol,kite_exchange,kite_tradingsymbol,kite_token"
                               f"&is_active=eq.true&kite_tradingsymbol=not.is.null&order=symbol&limit=1000&offset={off}",
                         headers=H, timeout=60).json()
        rows += b; off += 1000
        if len(b) < 1000:
            return rows


def _find_frozen(k, rows):
    idmap = {f"{r['kite_exchange']}:{r['kite_tradingsymbol']}": r for r in rows}
    ids = list(idmap); got = set()
    for i in range(0, len(ids), 400):
        for _ in range(3):
            try:
                got |= set(k.quote(ids[i:i + 400]).keys()); break
            except Exception:
                pass
    return [idmap[x] for x in ids if x not in got]


def _resolve(k, sym, nse, bse, today):
    """Return (exchange, tradingsymbol, token) for a live id dated today, else None."""
    cands = []
    if sym + "-BE" in nse:
        cands.append(("NSE", sym + "-BE", nse[sym + "-BE"]["instrument_token"]))
    if sym in bse:
        cands.append(("BSE", sym, bse[sym]["instrument_token"]))
    for ex, ts, tok in cands:
        qid = f"{ex}:{ts}"
        try:
            d = k.quote([qid])[qid]
            if str(d.get("last_trade_time", ""))[:10] == today:
                return ex, ts, tok
        except Exception:
            pass
    return None


def heal(kite=None, apply=True, verbose=True):
    """Detect + fix frozen Kite identifiers. Returns list of (symbol, old_id, new_id).
    Reuses an existing kite client if given (so callers don't re-authenticate)."""
    k = kite or get_kite()
    today = _today_iso()
    frozen = _find_frozen(k, _all_active())
    if not frozen:
        if verbose:
            print("[heal] no frozen Kite ids", flush=True)
        return []
    nse = {i["tradingsymbol"]: i for i in k.instruments("NSE")}
    bse = {i["tradingsymbol"]: i for i in k.instruments("BSE")}
    fixes, dead = [], []
    for r in sorted(frozen, key=lambda x: x["symbol"]):
        res = _resolve(k, r["kite_tradingsymbol"], nse, bse, today)
        if not res:
            dead.append(r["symbol"]); continue
        ex, ts, tok = res
        old = f"{r['kite_exchange']}:{r['kite_tradingsymbol']}"
        if apply:
            requests.patch(URL + f"/rest/v1/stock_master?symbol=eq.{r['symbol']}",
                           headers={**H, "Prefer": "return=minimal"},
                           data=json.dumps({"kite_exchange": ex, "kite_tradingsymbol": ts, "kite_token": tok}),
                           timeout=30)
        fixes.append((r["symbol"], old, f"{ex}:{ts}"))
    if verbose:
        print(f"[heal] frozen={len(frozen)} fixed={len(fixes)} "
              f"unresolved(suspended/delisted)={len(dead)} mode={'APPLY' if apply else 'DRY-RUN'}", flush=True)
        for sym, old, new in fixes:
            print(f"   {sym:13} {old} -> {new}", flush=True)
        if dead:
            print("   no live match:", ", ".join(dead), flush=True)
    return fixes


if __name__ == "__main__":
    heal(apply="--apply" in sys.argv)
