"""
index_series_cron.py
====================
Stores daily OHLCV series for the headline indices (NIFTY 50, BANK NIFTY, SENSEX) from
ZERODHA KITE into  daily/index/<SLUG>.json  — the single source of truth read by the
market strip and investor-perf, so nothing fetches NIFTY live from a third party.

Same shape as the per-stock candle files. Token from the app_config vault (the daily-rotated
one the price crons use). Stdlib only (urllib), no pip deps.

Run:  py index_series_cron.py            # ~2000 days (≈5.5y)
      py index_series_cron.py --days 4000
"""
import argparse, datetime, json, os, urllib.request

SB_URL = os.environ.get("SUPABASE_URL", "https://tbeadvvkqyrhtendttrg.supabase.co").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY") or open(r"e:/Stocks sena/.supabase-service-key").read().strip()
HDR = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
# slug, Kite tradingsymbol/name, instrument_token
INDICES = [("NIFTY", "NIFTY 50", 256265), ("BANKNIFTY", "NIFTY BANK", 260105), ("SENSEX", "SENSEX", 265)]


def http_json(url, headers=None, data=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8"))


def kite_creds():
    rows = http_json(f"{SB_URL}/rest/v1/app_config?select=key,value&key=in.(kite_api_key,kite_access_token)", HDR)
    cfg = {x["key"]: x["value"] for x in rows}
    return cfg.get("kite_api_key"), cfg.get("kite_access_token")


def put_storage(path, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{SB_URL}/storage/v1/object/{path}", data=body, method="POST",
                                 headers={**HDR, "Content-Type": "application/json", "x-upsert": "true"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2000)
    a = ap.parse_args()
    ak, at = kite_creds()
    if not (ak and at):
        print("[index] no Kite token in vault — abort (token rotates daily; nothing written)")
        return
    kh = {"X-Kite-Version": "3", "Authorization": f"token {ak}:{at}"}
    to = datetime.date.today().isoformat()
    frm = (datetime.date.today() - datetime.timedelta(days=a.days)).isoformat()
    print(f"[index] {len(INDICES)} indices · {frm} -> {to}", flush=True)
    for slug, name, token in INDICES:
        url = f"https://api.kite.trade/instruments/historical/{token}/day?from={frm}&to={to}"
        try:
            candles = (http_json(url, kh).get("data") or {}).get("candles") or []
        except Exception as e:
            print(f"  {slug:10} fetch ERROR: {e}", flush=True)
            continue
        bars = [[str(c[0])[:10], c[1], c[2], c[3], c[4], c[5]] for c in candles]
        if len(bars) < 50:
            print(f"  {slug:10} only {len(bars)} bars — skip (token stale?)", flush=True)
            continue
        payload = {"symbol": name, "interval": "1d", "from": bars[0][0], "to": bars[-1][0],
                   "_ticker_used": f"KITE:{name}", "bars": bars}
        put_storage(f"daily/index/{slug}.json", payload)
        print(f"  {slug:10} {len(bars):5} bars  {bars[0][0]} -> {bars[-1][0]}  last {bars[-1][4]}", flush=True)
    print("[index] done.", flush=True)


if __name__ == "__main__":
    main()
