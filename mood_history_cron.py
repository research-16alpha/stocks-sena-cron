"""
mood_history_cron.py
====================
DAILY cron - computes the Market Mood Index ONCE per trading day using the
same logic as src/lib/marketMood.ts, snapshots to market_mood_history.

Runs at 7 PM IST after all upstream data has settled (daily_ohlcv, returns,
macro, breadth, PCR, fii_dii).

Why this exists: the app's mood widget needs "vs yesterday" delta (e.g. "▲ +5").
Computing yesterday on the fly would need fetching 30+ days of inputs every
load. Instead: precompute daily, store in market_mood_history, app just reads
last 2 rows.

Inputs (all real, all from our pipeline):
  - NIFTY closes        → daily Storage: daily/_NSEI.json
  - VIX closes (1y)     → Yahoo Chart API
  - Gold closes (3mo)   → Yahoo Chart API
  - Breadth (today)     → public.breadth_daily latest row
  - PCR (today)         → public.nifty_pcr_history latest row
  - FII flows (90d)     → public.fii_dii_flows last 95 rows

Output: one row per date in public.market_mood_history.
"""

import os
import sys
import math
import json
from datetime import datetime, timezone, timedelta

import requests
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
IST = timezone(timedelta(hours=5, minutes=30))


# ─── Helpers (mirror src/lib/marketMood.ts) ──────────────────────────────────

def clamp100(n):
    if n is None or not math.isfinite(n): return 50
    return max(0.0, min(100.0, float(n)))

def linear_map(value, lo, hi):
    if hi == lo: return 50.0
    return clamp100(((value - lo) / (hi - lo)) * 100.0)

def percentile(value, history):
    if not history: return 50.0
    below = sum(1 for v in history if v < value)
    return clamp100((below / len(history)) * 100.0)

def sma(values, n):
    if len(values) < n: return None
    return sum(values[-n:]) / n

def stdev(values):
    if not values: return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(var)


# ─── Data fetchers ───────────────────────────────────────────────────────────

def fetch_yahoo_closes(symbol, range_="1y"):
    url = f"{YAHOO_BASE}/{symbol}"
    try:
        r = requests.get(url, params={"interval": "1d", "range": range_}, headers=HEADERS, timeout=15)
        if r.status_code != 200: return None, None
        payload = r.json()
        result = (payload.get("chart", {}).get("result") or [None])[0]
        if not result: return None, None
        closes = [c for c in ((result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []) if c is not None]
        live = result.get("meta", {}).get("regularMarketPrice")
        return closes, live
    except Exception as e:
        print(f"[mood] yahoo {symbol}: {e}", file=sys.stderr)
        return None, None


def fetch_nifty_closes_from_storage():
    url = sb.storage.from_("daily").get_public_url("_NSEI.json")
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200: return None
        return [b[4] for b in (r.json().get("bars") or []) if b[4] is not None]
    except Exception as e:
        print(f"[mood] storage _NSEI: {e}", file=sys.stderr)
        return None


def fetch_latest_breadth():
    res = sb.table("breadth_daily").select("*").order("date", desc=True).limit(1).execute()
    rows = res.data or []
    return rows[0] if rows else None


def fetch_latest_pcr():
    res = sb.table("nifty_pcr_history").select("pcr").order("captured_at", desc=True).limit(1).execute()
    rows = res.data or []
    return float(rows[0]["pcr"]) if rows else None


def fetch_fii_history(rows=120):
    """Returns list of (date_iso, net_value) tuples, oldest -> newest."""
    res = (
        sb.table("fii_dii_flows")
        .select("date,net_value_cr,category")
        .eq("category", "FII/FPI")
        .order("date", desc=True)
        .limit(rows)
        .execute()
    )
    by_date = {}
    for r in (res.data or []):
        if r["net_value_cr"] is None: continue
        by_date[r["date"]] = by_date.get(r["date"], 0) + float(r["net_value_cr"])
    return [(d, by_date[d]) for d in sorted(by_date.keys())]


# ─── Signal compute (mirrors marketMood.ts) ──────────────────────────────────

def compute_trend(nifty_closes):
    if not nifty_closes or len(nifty_closes) < 125: return None
    last125 = nifty_closes[-125:]
    mean = sum(last125) / 125
    sd = stdev(last125)
    if sd == 0: return 50.0
    z = (nifty_closes[-1] - mean) / sd
    return linear_map(z, -2, 2)


def compute_breadth_dma(breadth_row):
    if not breadth_row or breadth_row.get("pct_above_50dma") is None: return None
    return clamp100(float(breadth_row["pct_above_50dma"]))


def compute_breadth_hl(breadth_row):
    if not breadth_row: return None
    h = int(breadth_row.get("highs_52w") or 0)
    l = int(breadth_row.get("lows_52w") or 0)
    denom = h + l
    if denom == 0: return 50.0
    return clamp100((h / denom) * 100.0)


def compute_vix(vix_live, vix_history):
    if vix_live is None or not vix_history or len(vix_history) < 60: return None
    p = percentile(vix_live, vix_history)
    return clamp100(100 - p)


def compute_pcr(pcr):
    if pcr is None: return None
    return linear_map(pcr, 1.4, 0.7)


def compute_fii(fii_history):
    """
    fii_history: list of (date_iso, net_value) tuples, oldest -> newest.
    Requires 95 CONSECUTIVE-ish trading days (within ~140 calendar days)
    for the 5d-rolling vs 90d-percentile compute to be statistically valid.
    Returns None if data is insufficient or has gaps - better than wrong score.
    """
    if not fii_history or len(fii_history) < 95: return None
    # Gap check - last 95 rows should span no more than 140 calendar days
    last95 = fii_history[-95:]
    try:
        first_dt = datetime.strptime(last95[0][0], "%Y-%m-%d")
        last_dt = datetime.strptime(last95[-1][0], "%Y-%m-%d")
        span_days = (last_dt - first_dt).days
        if span_days > 140:
            print(f"[mood] fii gap detected: 95 rows span {span_days} cal days, expected <= 140")
            return None
    except Exception:
        return None
    values = [v for _, v in last95]
    last5sum = sum(values[-5:])
    hist = values[:-5]
    windows = []
    for i in range(4, len(hist)):
        windows.append(sum(hist[i - 4:i + 1]))
    return percentile(last5sum, windows)


def compute_safe_haven(nifty_closes, gold_closes):
    def ret_n(c, n):
        if len(c) < n + 1: return None
        if c[-1 - n] == 0: return None
        return c[-1] / c[-1 - n] - 1
    rn = ret_n(nifty_closes or [], 30)
    rg = ret_n(gold_closes or [], 30)
    if rn is None or rg is None: return None
    return linear_map(rn - rg, -0.10, 0.10)


def band_label(score):
    if score < 25:   return ("extreme_fear", "Extreme Fear")
    if score < 45:   return ("fear", "Fear")
    if score <= 55:  return ("neutral", "Neutral")
    if score <= 75:  return ("greed", "Greed")
    return ("extreme_greed", "Extreme Greed")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    # Pull all inputs in parallel-ish (sequential, but each is fast)
    nifty_storage = fetch_nifty_closes_from_storage()
    nifty_yahoo, nifty_live = fetch_yahoo_closes("^NSEI", "1y")
    vix_closes, vix_live = fetch_yahoo_closes("^INDIAVIX", "1y")
    gold_closes, _ = fetch_yahoo_closes("GC=F", "3mo")
    fii_hist = fetch_fii_history(120)
    breadth = fetch_latest_breadth()
    pcr = fetch_latest_pcr()

    # Prefer storage NIFTY (5y, our own) if long enough, else fall back to Yahoo
    if nifty_storage and len(nifty_storage) >= 125:
        nifty_for_trend = nifty_storage
        nifty_for_haven = nifty_storage
    else:
        nifty_for_trend = nifty_yahoo or []
        nifty_for_haven = nifty_yahoo or []

    signals = {
        "trend":       compute_trend(nifty_for_trend),
        "breadth_dma": compute_breadth_dma(breadth),
        "breadth_hl":  compute_breadth_hl(breadth),
        "volatility":  compute_vix(vix_live, vix_closes or []),
        "pcr":         compute_pcr(pcr),
        "fii_flow":    compute_fii(fii_hist),
        "safe_haven":  compute_safe_haven(nifty_for_haven, gold_closes or []),
    }

    available = [v for v in signals.values() if v is not None]
    if not available:
        print("[mood] no signals available, aborting")
        sys.exit(1)

    score = round(sum(available) / len(available), 2)
    band_key, _ = band_label(score)

    today = datetime.now(IST).date().isoformat()
    row = {
        "date": today,
        "score": score,
        "band": band_key,
        "signal_scores": {k: (round(v, 2) if v is not None else None) for k, v in signals.items()},
        "available_count": len(available),
        "total_count": len(signals),
    }
    sb.table("market_mood_history").upsert(row, on_conflict="date").execute()
    print(f"[mood] {today} score={score} band={band_key} available={len(available)}/7")
    for k, v in signals.items():
        print(f"  {k:12s} = {v if v is not None else 'MISSING'}")


if __name__ == "__main__":
    main()
