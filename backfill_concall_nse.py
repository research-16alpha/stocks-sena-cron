"""
backfill_concall_nse.py
=======================
Concall transcript metadata from NSE corporate-announcements (whole market,
monthly windows). NSE carries ~900 transcript filings / 60 days WITH the PDF
link — far richer than the BSE source. Link-out only (we store metadata + URL,
no PDF download).

Run:  py backfill_concall_nse.py --dry --years 3
      py backfill_concall_nse.py --years 3
"""
import os, sys, json, time, argparse
import requests
from datetime import date, timedelta, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

URL = os.environ.get("SUPABASE_URL", "https://tbeadvvkqyrhtendttrg.supabase.co").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY") or open(r"e:/Stocks sena/.supabase-service-key").read().strip()
SB_H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
API = "https://www.nseindia.com/api/corporate-announcements?index=equities&from_date={f}&to_date={t}"

# Real transcripts say "transcript". Exclude ONLY pre-call intimations / notices.
# Do NOT exclude "audio recording"/"recording of": companies routinely file the transcript
# as a single "Audio recording AND transcript of the earnings call" PDF, and a pure audio
# filing (no "transcript" in the text) is already dropped by the "transcript" check below.
EXCLUDE = ("intimation", "schedule of", "notice of", "will be held",
           "prior intimation", "newspaper", "link for the", "link of")


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate"})
    try:
        s.get("https://www.nseindia.com/", timeout=15); s.get(PAGE, timeout=15)
    except Exception:
        pass
    return s


def fetch_window(s, f, t, tries=4):
    h = {"Referer": PAGE, "Accept": "application/json, */*", "X-Requested-With": "XMLHttpRequest"}
    for i in range(tries):
        try:
            r = s.get(API.format(f=f, t=t), headers=h, timeout=60)
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                j = r.json()
                return j if isinstance(j, list) else (j.get("data") or [])
            s.get(PAGE, timeout=15); time.sleep(1.5 * (i + 1))
        except Exception:
            time.sleep(1.5 * (i + 1))
    return None


def pdt(s):
    if not s:
        return None
    s = str(s).split(".")[0].strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def infer_quarter(d):
    """Filing date -> (quarter_str, period_end_iso). Indian FY = Apr-Mar; transcripts
       are filed ~1-2 months after the quarter ends."""
    m, y = d.month, d.year
    if m in (4, 5, 6):      q, pe = "Q4", date(y, 3, 31); fy = y
    elif m in (7, 8, 9):    q, pe = "Q1", date(y, 6, 30); fy = y + 1
    elif m in (10, 11, 12): q, pe = "Q2", date(y, 9, 30); fy = y + 1
    else:                   q, pe = "Q3", date(y - 1, 12, 31); fy = y
    return f"{q}FY{str(fy)[2:]}", pe.isoformat()


def is_transcript(text):
    t = text.lower()
    if "transcript" not in t:
        return False
    return not any(x in t for x in EXCLUDE)


def windows(years):
    out, end = [], date.today()
    cur = end - timedelta(days=int(365.25 * years))
    while cur < end:
        nxt = min(cur + timedelta(days=30), end)
        out.append((cur.strftime("%d-%m-%Y"), nxt.strftime("%d-%m-%Y"))); cur = nxt + timedelta(days=1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=3)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    wins = windows(a.years)
    print(f"[concall-nse] {len(wins)} windows over {a.years}y")
    s = new_session()
    raw = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(fetch_window, s, w[0], w[1]): w for w in wins}
        for fut in as_completed(futs):
            w = futs[fut]; d = fut.result()
            print(f"  {w[0]}..{w[1]} -> {('FAIL' if d is None else len(d))}")
            if d:
                raw.extend(d)

    by_key = {}
    for x in raw:
        sym = (x.get("symbol") or "").strip()
        text = ((x.get("desc") or "") + " " + (x.get("attchmntText") or "")).strip()
        if not sym or not is_transcript(text):
            continue
        dtv = pdt(x.get("an_dt") or x.get("sort_date"))
        if not dtv:
            continue
        q, pe = infer_quarter(dtv.date())
        att = x.get("attchmntFile")
        # Link DIRECTLY to the transcript PDF only — skip any filing without a PDF attachment.
        if not (att and att.startswith("http") and att.lower().endswith(".pdf")):
            continue
        row = {
            "symbol": sym, "quarter": q, "period_end": pe,
            "filed_at": dtv.isoformat(), "source": "NSE",
            "source_url": att,
            "title": (x.get("desc") or "Earnings call transcript")[:200],
            "has_text": False,
        }
        # latest filing per (symbol, quarter) wins
        k = (sym, q)
        if k not in by_key or row["filed_at"] > by_key[k]["filed_at"]:
            by_key[k] = row
    rows = list(by_key.values())
    print(f"[concall-nse] raw={len(raw)} -> transcripts(unique sym+qtr)={len(rows)}")

    if a.dry:
        for r in rows[:5]:
            print(" ", r["symbol"], r["quarter"], r["period_end"], "|", (r["source_url"] or "")[:55])
        return
    # No unique (symbol,quarter) constraint -> truncate + insert fresh (NSE set is
    # far more comprehensive than the existing rows, all link-only metadata anyway).
    requests.delete(f"{URL}/rest/v1/concall_transcripts?symbol=not.is.null", headers=SB_H, timeout=60)
    ok = 0
    for i in range(0, len(rows), 500):
        ch = rows[i:i + 500]
        r = requests.post(f"{URL}/rest/v1/concall_transcripts",
                          headers={**SB_H, "Prefer": "return=minimal"}, data=json.dumps(ch), timeout=60)
        if r.status_code in (200, 201, 204):
            ok += len(ch)
        else:
            print(f"[WARN] {r.status_code}: {r.text[:200]}"); sys.stdout.flush()
    print(f"[concall-nse] inserted={ok}")


if __name__ == "__main__":
    main()
