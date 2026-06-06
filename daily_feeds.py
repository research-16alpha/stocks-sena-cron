"""
daily_feeds.py
=============
DAILY incremental refresh of the NSE-sourced feeds we backfilled:
  insider trades · credit ratings · SAST/promoter · concall transcripts · deals.

Strategy (robust, idempotent, no DB constraints needed):
  1. fetch only the last FEED_DAYS days (one small window),
  2. pull the natural keys already in the DB for that window,
  3. insert ONLY the rows not already present.

Reuses the exact fetch + map + classify logic from the backfill scripts (single
source of truth), so daily output matches the backfill.

Run daily after market close (~6-7 PM IST):  py daily_feeds.py
Env: SUPABASE_SERVICE_KEY (else reads e:/Stocks sena/.supabase-service-key); FEED_DAYS (default 12).
"""
import os, sys, json
import requests
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_insider as INS
import backfill_nse_announcements as ANN
import backfill_concall_nse as CON
import capture_deals_today as DEALS
import backfill_auditor_changes as AUD
import pledge_backfill as PL

URL = os.environ.get("SUPABASE_URL", "https://tbeadvvkqyrhtendttrg.supabase.co").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY") or open(r"e:/Stocks sena/.supabase-service-key").read().strip()
SB_H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
DAYS = int(os.environ.get("FEED_DAYS", "12"))


def existing_keys(table, select, date_field, keyfn):
    since = (date.today() - timedelta(days=DAYS + 5)).isoformat()
    try:
        r = requests.get(f"{URL}/rest/v1/{table}?select={select}&{date_field}=gte.{since}",
                         headers={**SB_H, "Range": "0-199999"}, timeout=90)
        return set(keyfn(x) for x in (r.json() if r.status_code == 200 else []))
    except Exception as e:
        print(f"[WARN] existing_keys {table}: {e}")
        return set()


def insert_new(table, rows):
    ok = 0
    for i in range(0, len(rows), 500):
        ch = rows[i:i + 500]
        r = requests.post(f"{URL}/rest/v1/{table}", headers={**SB_H, "Prefer": "return=minimal"},
                          data=json.dumps(ch), timeout=90)
        if r.status_code in (200, 201, 204):
            ok += len(ch)
        else:
            print(f"[WARN] {table} {r.status_code}: {r.text[:160]}")
    return ok


def window():
    to = date.today().strftime("%d-%m-%Y")
    fr = (date.today() - timedelta(days=DAYS)).strftime("%d-%m-%Y")
    return fr, to


def run_insider():
    s = INS.new_session()
    fr, to = window()
    raw = INS.fetch_window(s, fr, to) or []
    rows = []
    seen = set()
    for r in raw:
        m = INS.map_row(r)
        if not m or not m["trade_date"]:
            continue
        k = (m["symbol"], m["insider_name"], m["trade_date"], m["trade_type"], m["quantity"])
        if k in seen:
            continue
        seen.add(k); rows.append(m)
    ex = existing_keys("insider_trades", "symbol,insider_name,trade_date,trade_type,quantity", "trade_date",
                       lambda x: (x["symbol"], x.get("insider_name"), x["trade_date"], x["trade_type"], x.get("quantity")))
    new = [r for r in rows if (r["symbol"], r["insider_name"], r["trade_date"], r["trade_type"], r["quantity"]) not in ex]
    print(f"[insider]  window={len(rows)} new={len(new)} inserted={insert_new('insider_trades', new)}")


def run_announcements():
    s = ANN.new_session()
    fr, to = window()
    raw = ANN.fetch_window(s, fr, to) or []
    ratings, promos = {}, {}
    for x in raw:
        sym = (x.get("symbol") or "").strip()
        text = ((x.get("attchmntText") or "") + " " + (x.get("desc") or "")).strip()
        if not sym or not text:
            continue
        dtv = ANN.pdt(x.get("an_dt") or x.get("sort_date") or x.get("dt"))
        if not dtv:
            continue
        cr = ANN.classify_rating(sym, text, dtv, None)
        if cr:
            ratings[(cr["symbol"], cr["agency"], cr["rating_date"], cr["action_type"], cr["new_rating"])] = cr
        pr = ANN.classify_promo(sym, x.get("sm_name"), text, dtv, None)
        if pr:
            promos[(pr["symbol"], pr["action_type"], pr["filing_date"][:10])] = pr
    # credit ratings
    exr = existing_keys("credit_ratings", "symbol,agency,rating_date,action_type,new_rating", "rating_date",
                        lambda x: (x["symbol"], x["agency"], x["rating_date"], x["action_type"], x.get("new_rating")))
    nr = [v for k, v in ratings.items() if k not in exr]
    print(f"[credit]   window={len(ratings)} new={len(nr)} inserted={insert_new('credit_ratings', nr)}")
    # promoter / SAST
    exp = existing_keys("promoter_actions", "symbol,action_type,filing_date", "filing_date",
                        lambda x: (x["symbol"], x["action_type"], (x.get("filing_date") or "")[:10]))
    np = [v for k, v in promos.items() if k not in exp]
    print(f"[sast]     window={len(promos)} new={len(np)} inserted={insert_new('promoter_actions', np)}")


def run_concall():
    s = CON.new_session()
    fr, to = window()
    raw = CON.fetch_window(s, fr, to) or []
    by = {}
    for x in raw:
        sym = (x.get("symbol") or "").strip()
        text = ((x.get("desc") or "") + " " + (x.get("attchmntText") or "")).strip()
        if not sym or not CON.is_transcript(text):
            continue
        dtv = CON.pdt(x.get("an_dt") or x.get("sort_date"))
        if not dtv:
            continue
        q, pe = CON.infer_quarter(dtv.date())
        att = x.get("attchmntFile")
        if not (att and att.startswith("http") and att.lower().endswith(".pdf")):
            continue  # link directly to the transcript PDF only
        by[(sym, q)] = {"symbol": sym, "quarter": q, "period_end": pe, "filed_at": dtv.isoformat(),
                        "source": "NSE", "source_url": att,
                        "title": (x.get("desc") or "Earnings call transcript")[:200], "has_text": False}
    # concall_transcripts has a unique (symbol, quarter, source) constraint -> upsert (idempotent).
    rows = list(by.values())
    ok = 0
    for i in range(0, len(rows), 500):
        ch = rows[i:i + 500]
        r = requests.post(f"{URL}/rest/v1/concall_transcripts?on_conflict=symbol,quarter,source",
                          headers={**SB_H, "Prefer": "resolution=merge-duplicates,return=minimal"},
                          data=json.dumps(ch), timeout=90)
        if r.status_code in (200, 201, 204):
            ok += len(ch)
        else:
            print(f"[WARN] concall {r.status_code}: {r.text[:150]}")
    print(f"[concall]  window={len(rows)} upserted={ok}")


def run_auditor():
    """A8 — new statutory auditor changes from the announcements we already scrape."""
    since = (date.today() - timedelta(days=DAYS)).isoformat()
    r = requests.get(f"{URL}/rest/v1/corporate_announcements?select=symbol,headline,detail,filed_at,pdf_url"
                     f"&headline=ilike.*auditor*&filed_at=gte.{since}",
                     headers={**SB_H, "Range": "0-9999"}, timeout=90)
    rows, seen = [], set()
    for a in (r.json() if r.status_code == 200 else []):
        sym, hl = (a.get("symbol") or "").strip(), (a.get("headline") or "")
        if not sym or not hl:
            continue
        action, kind = AUD.classify(hl)
        if not action or kind != "statutory":
            continue
        eff = (a.get("filed_at") or "")[:10]
        if not eff or (sym, eff, action) in seen:
            continue
        seen.add((sym, eff, action))
        outgoing = action in ("resignation", "removal")
        fn = AUD.firm(hl, a.get("detail"))
        rows.append({"symbol": sym, "effective_date": eff,
                     "reason": f"{action.replace('_', ' ').title()} of Statutory Auditor",
                     "filing_url": a.get("pdf_url"),
                     "old_auditor": fn if outgoing else None,
                     "new_auditor": None if outgoing else fn})
    ex = existing_keys("auditor_changes", "symbol,effective_date,reason", "effective_date",
                       lambda x: (x["symbol"], x["effective_date"], x.get("reason")))
    new = [r for r in rows if (r["symbol"], r["effective_date"], r["reason"]) not in ex]
    print(f"[auditor]  window={len(rows)} new={len(new)} inserted={insert_new('auditor_changes', new)}")


def run_pledge():
    """A1 — recompute promoter pledge % for stocks whose shareholding pattern was just filed."""
    since = (date.today() - timedelta(days=DAYS)).isoformat()
    r = requests.get(f"{URL}/rest/v1/shareholding_periods?select=symbol,period,xbrl_url,promoter_pct"
                     f"&xbrl_url=not.is.null&filing_date=gte.{since}&order=period.desc",
                     headers={**SB_H, "Range": "0-9999"}, timeout=90)
    recs, seen = [], set()
    for x in (r.json() if r.status_code == 200 else []):
        if x["symbol"] not in seen:
            seen.add(x["symbol"]); recs.append(x)
    if not recs:
        print("[pledge]   no new shareholding filings"); return
    PL.load_shares()
    ok = 0
    for rec in recs:
        res = PL.parse_pledge(rec)
        if not res or res[3] is None:
            continue
        resp = requests.patch(f"{URL}/rest/v1/stock_master?symbol=eq.{res[0]}",
                              headers={**SB_H, "Prefer": "return=minimal"},
                              data=f'{{"pledged_pct": {res[3]}}}', timeout=60)
        if resp.status_code in (200, 204):
            ok += 1
    print(f"[pledge]   new shareholding filings={len(recs)} updated={ok}")


def main():
    print(f"[daily_feeds] last {DAYS} days")
    for name, fn in [("insider", run_insider), ("announcements", run_announcements),
                     ("concall", run_concall), ("deals", DEALS.main),
                     ("auditor", run_auditor), ("pledge", run_pledge)]:
        try:
            fn()
        except Exception as e:
            print(f"[ERR] {name}: {e}")
    print("[daily_feeds] done")


if __name__ == "__main__":
    main()
