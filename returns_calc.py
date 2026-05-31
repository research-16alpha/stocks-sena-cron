"""
returns_calc.py
===============
Pure, dependency-free return / 52-week math shared by EVERY price cron
(returns_cron.py, kite_daily_update.py, kite_backfill_daily.py, the one-off
backfill runner). No network, no env, no SDK — just the function.

Why date-based (and why this lives in one place):
  Illiquid scrips skip trading days, and some go dormant for years then resume.
  The old "N bars ago" lookback (closes[-1-63]) reached back YEARS for such names
  — ARIHANT reported a +2051% 3-month return because "63 bars ago" landed on a
  2021 penny print. Anchoring each period to a CALENDAR date fixes it. Keeping the
  math in one module stops the three copies from drifting apart again.
"""

from datetime import datetime, timedelta

# (period_field, calendar_days_back, max_gap_days)
#   max_gap_days = how far the nearest available bar may sit from the target date
#   before we return None ("didn't trade near then") instead of a fake number.
PERIODS = [
    ("return_1m_pct",   30,  15),
    ("return_3m_pct",   91,  35),
    ("return_6m_pct",  182,  50),
    ("return_1y_pct",  365,  75),
    ("return_5y_pct", 1825, 150),
]


def _parse_date(d) -> "datetime | None":
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d")
    except Exception:
        return None


def compute_returns(bars: list) -> "dict | None":
    """bars = [[date, open, high, low, close, volume], ...] (any order, gaps ok).

    Returns latest_price, price_change_pct, high_52w, low_52w and
    return_1m/3m/6m/1y/5y_pct. Callers may keep only the keys they want
    (e.g. the manual daily job keeps its LTP latest_price and takes only the
    returns/52w keys).
    """
    if not bars or len(bars) < 2:
        return None
    rows = []
    for b in bars:
        d = _parse_date(b[0])
        if d is not None and b[4] is not None:
            rows.append((d, b))
    if len(rows) < 2:
        return None
    rows.sort(key=lambda r: r[0])

    today_date, today_bar = rows[-1]
    today = today_bar[4]
    out: dict = {"latest_price": round(today, 2)}

    for field, days_back, max_gap in PERIODS:
        target = today_date - timedelta(days=days_back)
        best_d, best_bar = min(rows, key=lambda r: abs((r[0] - target).days))
        gap = abs((best_d - target).days)
        past = best_bar[4]
        if gap <= max_gap and best_d < today_date and past and past > 0:
            out[field] = round((today / past - 1) * 100, 2)
        else:
            out[field] = None

    # 52-week high/low from the trailing 365 CALENDAR days (date-based window).
    cutoff = today_date - timedelta(days=365)
    yr = [b for (d, b) in rows if d >= cutoff]
    highs = [b[2] for b in yr if b[2] is not None]
    lows = [b[3] for b in yr if b[3] is not None]
    if highs:
        out["high_52w"] = round(max(highs), 2)
    if lows:
        out["low_52w"] = round(min(lows), 2)

    prev = rows[-2][1][4]
    if prev and prev > 0:
        out["price_change_pct"] = round((today / prev - 1) * 100, 2)
    return out
