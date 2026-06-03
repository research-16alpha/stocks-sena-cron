"""
backfill_auditor_changes.py  [--apply]   (default DRY-RUN)
=========================================================
Populates `auditor_changes` from the corporate_announcements we already scrape
(~3,335 rows whose headline mentions "auditor"). Classifies each as resignation /
appointment / re-appointment / removal, captures the STATUTORY-auditor events
(the material ones), extracts the auditor firm name where parseable, and maps to:
  symbol, old_auditor, new_auditor, effective_date, reason, filing_url

Idempotent: dedups on (symbol, effective_date, reason). DRY-RUN by default.
"""
import os, sys, json, re
import requests

URL = "https://tbeadvvkqyrhtendttrg.supabase.co"
KEY = os.environ.get("SUPABASE_SERVICE_KEY") or open(r"e:/Stocks sena/.supabase-service-key").read().strip()
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
APPLY = "--apply" in sys.argv

# firm name after "of"/"as" — e.g. "Appointment of M/s ABC & Co, Chartered Accountants as Statutory Auditor"
FIRM_RE = re.compile(r"(?:M/s\.?\s*)?([A-Z][A-Za-z&.,'\- ]{3,60}?(?:&\s*Co|& Associates|LLP|Chartered Accountants|Associates|& Affiliates))",)


def classify(h):
    hl = h.lower()
    # skip routine non-statutory auditors (internal / cost / secretarial are less material)
    kind = ("statutory" if "statutory" in hl else
            "secretarial" if "secretarial" in hl else
            "internal" if "internal" in hl else
            "cost" if "cost audit" in hl else "statutory")
    if "resignation" in hl or "resign" in hl or "cessation" in hl:
        action = "resignation"
    elif "removal" in hl or "removed" in hl:
        action = "removal"
    elif "re-appoint" in hl or "reappoint" in hl:
        action = "reappointment"
    elif "appoint" in hl:
        action = "appointment"
    elif "casual vacancy" in hl:
        action = "casual_vacancy"
    else:
        action = None
    return action, kind


def firm(h, detail):
    for txt in (h, detail or ""):
        m = FIRM_RE.search(txt)
        if m:
            return m.group(1).strip(" .,")
    return None


def page(q):
    out, off = [], 0
    while True:
        r = requests.get(f"{URL}/rest/v1/corporate_announcements?select=symbol,headline,detail,filed_at,pdf_url&{q}",
                         headers={**H, "Range": f"{off}-{off+999}"}, timeout=90)
        b = r.json() if r.status_code == 200 else []
        if not b:
            break
        out += b
        if len(b) < 1000:
            break
        off += 1000
    return out


def main():
    raw = page("headline=ilike.*auditor*&order=filed_at.desc")
    print(f"[auditor] {len(raw)} auditor announcements")
    rows, seen = [], set()
    from collections import Counter
    kinds, actions = Counter(), Counter()
    for a in raw:
        sym = (a.get("symbol") or "").strip()
        hl = a.get("headline") or ""
        if not sym or not hl:
            continue
        action, kind = classify(hl)
        if not action:
            continue
        kinds[kind] += 1
        actions[action] += 1
        # only ship statutory auditor events (the material ones)
        if kind != "statutory":
            continue
        eff = (a.get("filed_at") or "")[:10]
        if not eff:
            continue
        fn = firm(hl, a.get("detail"))
        reason = f"{action.replace('_',' ').title()} of Statutory Auditor"
        key = (sym, eff, action)
        if key in seen:
            continue
        seen.add(key)
        outgoing = action in ("resignation", "removal")
        row = {"symbol": sym, "effective_date": eff, "reason": reason, "filing_url": a.get("pdf_url"),
               "old_auditor": fn if outgoing else None,
               "new_auditor": None if outgoing else fn}
        rows.append(row)
    print(f"[auditor] kinds={dict(kinds)}  actions={dict(actions)}")
    print(f"[auditor] statutory events to insert: {len(rows)}  (distinct symbols: {len(set(r['symbol'] for r in rows))})")
    for r in rows[:12]:
        print(f"   {r['symbol']:12} {r['effective_date']}  {r['reason']:42} {r.get('new_auditor') or r.get('old_auditor') or ''}")
    if APPLY and rows:
        ok = 0
        for i in range(0, len(rows), 500):
            ch = rows[i:i + 500]
            resp = requests.post(f"{URL}/rest/v1/auditor_changes", headers={**H, "Prefer": "return=minimal"},
                                 data=json.dumps(ch), timeout=90)
            if resp.status_code in (200, 201, 204):
                ok += len(ch)
            else:
                print(f"   [WARN] {resp.status_code}: {resp.text[:160]}")
        print(f"[auditor] inserted {ok}")
    elif rows:
        print("[auditor] DRY-RUN — re-run with --apply to insert.")


if __name__ == "__main__":
    main()
