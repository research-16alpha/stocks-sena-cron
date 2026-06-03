"""
backfill_nse_announcements.py
=============================
One pass over NSE corporate-announcements (whole market, monthly windows over
YEARS years) -> classifies into TWO tables:
  - credit_ratings   (agency rating actions)
  - promoter_actions (promoter sale/purchase, pledge, SAST Reg 29)

NSE announcements carry the NSE `symbol` directly (no BSE scrip crosswalk needed).

Run:  py backfill_nse_announcements.py --dry --years 3
      py backfill_nse_announcements.py --years 3
"""
import os, sys, json, re, time, argparse
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

AGENCIES = [("crisil", "CRISIL"), ("icra", "ICRA"), ("careedge", "CARE"), ("care ratings", "CARE"),
            ("care edge", "CARE"), ("india ratings", "India Ratings"), ("ind-ra", "India Ratings"),
            ("brickwork", "Brickwork"), ("acuite", "Acuite"), ("infomerics", "Infomerics"), ("fitch", "Fitch")]
RATING_RE = re.compile(r"\b(?:CRISIL\s|ICRA\s?|CARE\s|IND\s|BWR\s|ACUITE\s|\[ICRA\])?"
                       r"(A{1,3}[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|C|D|A1\+?|A2\+?|A3\+?|A4\+?)\b")
OUTLOOK_RE = re.compile(r"\b(stable|positive|negative|developing|watch)\b", re.I)

# Rating-agency listed companies — their own PRs self-match "rating"+agency; skip as subjects.
AGENCY_SYMBOLS = {"CARERATING", "CRISIL", "ICRA", "INFOMERICS"}

# promoter_actions patterns (regex, action_type, severity). Generic "acquisition of
# shares" was too noisy; SAST Reg-29 disclosures are substantial-acquisition filings
# -> mapped to 'purchase' (the enum has no reg29 value).
PROMO = [
    (r"sale of (equity )?shares.*by (promoter|insider)|promoter.+sale of shares|disposal of shares by promoter", "sale", 4),
    (r"pledge.+(creat|increas)|creation of pledge|invocation of pledge", "pledge_increase", 3),
    (r"release of pledge|pledge.+revok|pledge.+decreas|revocation of pledge", "pledge_decrease", 2),
    (r"reg(ulation)?\.?\s*29\(?[12]\)?\s*(of\s*)?(sebi|sast)|substantial acquisition", "purchase", 2),
]


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


def classify_rating(sym, text, dtv, url):
    if sym in AGENCY_SYMBOLS:
        return None
    low = text.lower()
    agency = next((name for k, name in AGENCIES if k in low), None)
    if not agency or not ("rating" in low or "rated" in low):
        return None
    if "downgrad" in low or "revised downward" in low:
        act = "downgrade"
    elif "upgrad" in low or "revised upward" in low:
        act = "upgrade"
    elif "withdraw" in low:
        act = "withdrawal"
    elif "assigned" in low or "first-time" in low or "first time" in low:
        act = "first_rating"
    else:
        act = "reaffirmed"
    m = RATING_RE.search(text)
    out = OUTLOOK_RE.search(text)
    return {
        "symbol": sym, "agency": agency, "old_rating": None,
        "new_rating": (m.group(1)[:30] if m else None), "action_type": act,
        "outlook": (out.group(1).capitalize() if out else None),
        "rating_date": dtv.date().isoformat(), "rationale": text[:500] or None,
    }


def classify_promo(sym, company, text, dtv, url):
    low = text.lower()
    for rgx, act, sev in PROMO:
        if re.search(rgx, low):
            return {
                "symbol": sym, "company_name": company or sym, "action_type": act,
                "action_description": text[:500], "severity": sev,
                "filing_date": dtv.isoformat(), "source_url": url, "raw_data": None,
            }
    return None


def windows(years):
    out, end = [], date.today()
    cur = end - timedelta(days=int(365.25 * years))
    while cur < end:
        nxt = min(cur + timedelta(days=30), end)
        out.append((cur.strftime("%d-%m-%Y"), nxt.strftime("%d-%m-%Y"))); cur = nxt + timedelta(days=1)
    return out


def post(table, rows):
    ok = 0
    for i in range(0, len(rows), 500):
        ch = rows[i:i + 500]
        r = requests.post(f"{URL}/rest/v1/{table}", headers={**SB_H, "Prefer": "return=minimal"},
                          data=json.dumps(ch), timeout=60)
        if r.status_code in (200, 201, 204):
            ok += len(ch)
        else:
            print(f"[WARN] {table} {r.status_code}: {r.text[:160]}"); sys.stdout.flush()
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=3)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    wins = windows(a.years)
    print(f"[ann] {len(wins)} monthly windows over {a.years}y")
    s = new_session()
    raw = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(fetch_window, s, w[0], w[1]): w for w in wins}
        for fut in as_completed(futs):
            w = futs[fut]; d = fut.result()
            print(f"  {w[0]}..{w[1]} -> {('FAIL' if d is None else len(d))}")
            if d:
                raw.extend(d)

    ratings, promos = {}, {}
    for x in raw:
        sym = (x.get("symbol") or "").strip()
        if not sym:
            continue
        text = ((x.get("attchmntText") or "") + " " + (x.get("desc") or "")).strip()
        if not text:
            continue
        dtv = pdt(x.get("an_dt") or x.get("sort_date") or x.get("dt"))
        if not dtv:
            continue
        att = x.get("attchmntFile")
        url = att if (att and att.startswith("http")) else None
        cr = classify_rating(sym, text, dtv, url)
        if cr:  # keep all agency-named rating filings (grade often only in the PDF -> new_rating may be null)
            ratings[(cr["symbol"], cr["agency"], cr["rating_date"], cr["new_rating"])] = cr
        pr = classify_promo(sym, x.get("sm_name"), text, dtv, url)
        if pr:
            promos[(pr["symbol"], pr["action_type"], pr["action_description"][:120])] = pr
    R, P = list(ratings.values()), list(promos.values())
    print(f"[ann] raw={len(raw)} -> credit_ratings={len(R)} promoter_actions={len(P)}")

    if a.dry:
        print("=== rating samples ==="); [print(json.dumps({k: v for k, v in r.items() if k != 'rationale'}, ensure_ascii=True)) for r in R[:4]]
        print("=== promoter samples ==="); [print(p["symbol"], p["action_type"], "|", p["action_description"][:60]) for p in P[:4]]
        from collections import Counter
        print("rating actions:", dict(Counter(r["action_type"] for r in R)))
        print("promo actions:", dict(Counter(p["action_type"] for p in P)))
        return

    requests.delete(f"{URL}/rest/v1/credit_ratings?id=not.is.null", headers=SB_H, timeout=60)
    print(f"[ann] credit_ratings inserted={post('credit_ratings', R)}")
    requests.delete(f"{URL}/rest/v1/promoter_actions?id=not.is.null", headers=SB_H, timeout=60)
    print(f"[ann] promoter_actions inserted={post('promoter_actions', P)}")


if __name__ == "__main__":
    main()
