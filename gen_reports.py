# -*- coding: utf-8 -*-
"""Batch report generator: fetch data -> build_html -> render PDF (shared browser)
-> upload to Supabase Storage `reports/{safe}.pdf`. Skips banks (need bank template)
and stocks with <4 years of annual data.
Usage:  python gen_reports.py                 # full universe (non-bank, has fundamentals)
        python gen_reports.py RELIANCE,TCS    # specific symbols (test)
"""
import json, urllib.request, urllib.parse, sys, re, time
from playwright.sync_api import sync_playwright
import report_lib

import os
B=os.environ.get('SUPABASE_URL','https://tbeadvvkqyrhtendttrg.supabase.co')
SVC=os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
H={'apikey':SVC,'Authorization':'Bearer '+SVC}
def safe(sym): return re.sub(r'[^A-Za-z0-9]+','-',sym)

def get(path,t=40):
    return json.loads(urllib.request.urlopen(urllib.request.Request(B+path,headers=H),timeout=t).read())

def fetch_data(sym):
    sm=get(f'/rest/v1/stock_master?select=*&symbol=eq.{urllib.parse.quote(sym)}')
    if not sm: return None
    sm=sm[0]
    try:
        b=get(f'/storage/v1/object/public/fundamentals-v2/{urllib.parse.quote(sym)}.json')
    except Exception:
        return None
    cons_pl=b.get('annual_pl_consolidated') or []
    basis='cons' if len(cons_pl)>=4 else 'std'
    def pick(prefix,cap):
        cons=b.get(prefix+'_consolidated') or []; std=b.get(prefix+'_standalone') or b.get(prefix) or []
        use=cons if (basis=='cons' and len(cons)>=4) else std
        return use[-cap:]
    return {'sm':sm,
        'annual_pl':pick('annual_pl',10),'annual_bs':pick('annual_bs',10),'annual_cf':pick('annual_cf',10),
        'quarterly':pick('quarterly_results',8),'shareholding':(b.get('shareholding') or [])[-8:],'segments':[]}

def upload(sym,pdf):
    key=safe(sym)+'.pdf'
    req=urllib.request.Request(f'{B}/storage/v1/object/reports/{key}',data=pdf,
        headers={**H,'Content-Type':'application/pdf','x-upsert':'true'},method='POST')
    r=urllib.request.urlopen(req,timeout=40); return r.status

def universe():
    syms=[]; off=0
    while True:
        r=get(f'/rest/v1/stock_master?select=symbol&is_active=eq.true&is_bank=not.is.true&latest_quarter_period=not.is.null&order=market_cap_cr.desc.nullslast&limit=1000&offset={off}')
        if not r: break
        syms+=[x['symbol'] for x in r]
        if len(r)<1000: break
        off+=1000
    return syms

def main():
    if len(sys.argv)>1:
        syms=[s.strip().upper() for s in sys.argv[1].split(',') if s.strip()]
    else:
        syms=universe()
    print(f'[gen_reports] {len(syms)} symbols', flush=True)
    ok=skip=err=0; t0=time.time()
    with sync_playwright() as p:
        br=p.chromium.launch(); pg=br.new_page(); pg.set_viewport_size({'width':794,'height':1123})
        for i,sym in enumerate(syms):
            try:
                d=fetch_data(sym)
                if not d or len(d['annual_pl'])<4: skip+=1; continue
                html=report_lib.build_html(d)
                pg.set_content(html,wait_until='networkidle'); pg.wait_for_timeout(700)
                pdf=pg.pdf(format='A4',print_background=True)
                upload(sym,pdf); ok+=1
            except Exception as e:
                err+=1; print(f'  ERR {sym}: {str(e)[:70]}',flush=True)
            if (i+1)%50==0:
                print(f'  {i+1}/{len(syms)} ok={ok} skip={skip} err={err} · {(i+1)/(time.time()-t0):.1f}/s',flush=True)
        br.close()
    print(f'[gen_reports] DONE ok={ok} skip={skip} err={err} · {time.time()-t0:.0f}s',flush=True)

if __name__=='__main__':
    main()
