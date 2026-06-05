# -*- coding: utf-8 -*-  (auto-generated from build_report3.py)
import json, re, base64, os
LOGO_PATH=os.environ.get('LOGO_PATH', r'e:/Stocks sena/web/public/logo.png')
LOGO='data:image/png;base64,'+base64.b64encode(open(LOGO_PATH,'rb').read()).decode()
NAVY='#4F46E5'; NAVYD='#3730A3'; GOLD='#CA8A04'; SOFT='#EEF2FF'; CREAM='#EEF2FF'; INK='#0F1419'; INK2='#1F2937'; INK3='#4B5563'; INK4='#9CA3AF'; BG2='#FAFBFC'; BG3='#F1F3F5'; RULE='#E5E7EB'; UP='#10B981'; DN='#EF4444'

def build_html(d):
    sm = d['sm']
    APL, QTR, ABSL, ACF, SHP = d['annual_pl'], d['quarterly'], d['annual_bs'], d['annual_cf'], d['shareholding']
    BSI = {r['period']: r for r in ABSL}; CFI = {r['period']: r for r in ACF}
    
    def nz(r,*k):
        for x in k:
            if r and r.get(x) is not None:
                try: return float(r[x])
                except: pass
        return None
    def ig(x):
        x=str(int(x));
        if len(x)<=3: return x
        h,t=x[:-3],x[-3:]; return re.sub(r'(\d)(?=(\d\d)+$)',r'\1,',h)+','+t
    def inr(n,dec=0):
        if n is None: return '–'
        neg=n<0; n=abs(n); g=ig(int(round(n))) if dec==0 else ig(int(n))
        if dec: g+='.'+f'{n-int(n):.{dec}f}'[2:]
        return ('('+g+')') if neg else g
    def f(n,dec=1): return '–' if n is None else f'{n:.{dec}f}'
    def gpc(c,p): return None if (c is None or p in (None,0)) else (c/p-1)*100
    def cagr(e,s,y): return None if (not s or s<=0 or not e or e<=0 or y<=0) else ((e/s)**(1/y)-1)*100
    def G(v,dec=1):
        if v is None: return '<span class="m">–</span>'
        return f'<span class="{"u" if v>=0 else "d"}">{"+" if v>=0 else ""}{v:.{dec}f}%</span>'
    
    rev=lambda r: nz(r,'revenue_from_operations','sales','total_income')
    np_=lambda r: nz(r,'net_profit')
    def cogs(r): return sum(x for x in [nz(r,'cost_of_materials'),nz(r,'purchases_stock_in_trade'),nz(r,'changes_in_inventories')] if x is not None) or None
    def ebitda(r): return nz(r,'ebitda','operating_profit')
    def ebit(r):
        e=ebitda(r); dp=nz(r,'depreciation'); return (e-dp) if (e is not None and dp is not None) else None
    def opm(r):
        o=nz(r,'opm_pct'); rv=rev(r); e=ebitda(r)
        return o if o is not None else ((e/rv*100) if (e is not None and rv) else None)
    def npm(r):
        rv=rev(r); n=np_(r); return (n/rv*100) if (n is not None and rv) else None
    def gpm(r):
        rv=rev(r); c=cogs(r); return ((rv-c)/rv*100) if (rv and c is not None) else None
    def bv(p,*k): return nz(BSI.get(p),*k)
    def roe_y(r):
        eq=bv(r['period'],'total_equity'); n=np_(r); return (n/eq*100) if (eq and n is not None) else None
    def roce(r):
        eq=bv(r['period'],'total_equity'); bo=bv(r['period'],'borrowings'); e=ebit(r); ce=(eq or 0)+(bo or 0)
        return (e/ce*100) if (e is not None and ce>0) else None
    def roa(r):
        ta=bv(r['period'],'total_assets'); n=np_(r); return (n/ta*100) if (ta and n is not None) else None
    def de(r):
        eq=bv(r['period'],'total_equity'); bo=bv(r['period'],'borrowings'); return (bo/eq) if (eq and bo is not None) else None
    def aturn(r):
        ta=bv(r['period'],'total_assets'); rv=rev(r); return (rv/ta) if (ta and rv) else None
    def icov(r):
        e=ebit(r); fin=nz(r,'finance_costs','interest'); return (e/fin) if (e is not None and fin) else None
    def days(r,key):
        val=bv(r['period'],key); rv=rev(r); return (val/rv*365) if (val is not None and rv) else None
    def curr_ratio(r):
        ca=bv(r['period'],'current_assets'); cl=bv(r['period'],'current_liabilities'); return (ca/cl) if (ca and cl) else None
    def ccc(r):
        dd=days(r,'trade_receivables'); idd=days(r,'inventories'); pd=days(r,'trade_payables')
        return (dd+idd-(pd or 0)) if (dd is not None and idd is not None) else None
    def bvps(r):
        eq=bv(r['period'],'total_equity'); ec=bv(r['period'],'equity_capital'); return (eq*10/ec) if (eq and ec) else None
    def fcf(r):
        c=nz(CFI.get(r['period']),'cfo'); cx=nz(CFI.get(r['period']),'capex'); return (c-cx) if (c is not None and cx is not None) else None
    
    YRS=APL; ncol=len(YRS); yhead=''.join(f'<th>FY{r["period"][2:4]}</th>' for r in YRS)
    def row(label,vals,fmt='inr',cls='',dec=1):
        c=''
        for v in vals:
            if fmt=='inr': c+=f'<td>{inr(v)}</td>'
            elif fmt=='g': c+=f'<td>{G(v,dec)}</td>'
            elif fmt=='pct': c+=f'<td>{f(v,dec)}{"" if v is None else "%"}</td>'
            else: c+=f'<td>{f(v,dec)}</td>'
        return f'<tr class="{cls}"><td class="lbl">{label}</td>{c}</tr>'
    def tbl(hl,head,body): return f'<table><tr><th class="lbl">{hl}</th>{head}</tr>{body}</table>'
    SEC=lambda n,t,note='': f'<div class="sec"><span class="no">{n}</span>{t}{(" <em>"+note+"</em>") if note else ""}</div>'
    
    # ---- series & aggregates ----
    revL=[rev(r) for r in YRS]; npL=[np_(r) for r in YRS]; opmL=[opm(r) for r in YRS]; npmL=[npm(r) for r in YRS]
    rc=cagr(revL[-1],revL[0],ncol-1); pc=cagr(npL[-1],npL[0],ncol-1); ebc=cagr(ebitda(YRS[-1]),ebitda(YRS[0]),ncol-1)
    yoy_rev=gpc(revL[-1],revL[-2]); yoy_np=gpc(npL[-1],npL[-2])
    opm_avg=sum(x for x in opmL if x is not None)/len([x for x in opmL if x is not None])
    L=YRS[-1]; fy1='FY'+YRS[0]['period'][2:4]; fyN='FY'+L['period'][2:4]
    cfoL=nz(CFI.get(L['period']),'cfo'); capexL=nz(CFI.get(L['period']),'capex'); fcfL=fcf(L)
    fcf_pos=sum(1 for r in YRS if (fcf(r) or -1)>0)
    sh=SHP[-1] if SHP else {}
    roeL=sm.get('roe_pct'); roceL=sm.get('roce_pct'); deL=de(L); icovL=icov(L); cccL=ccc(L)
    
    # ---- Altman Z + Piotroski ----
    def altman():
        ta=bv(L['period'],'total_assets'); ca=bv(L['period'],'current_assets'); cl=bv(L['period'],'current_liabilities')
        re_=bv(L['period'],'reserves'); eb=ebit(L); mc=sm.get('market_cap_cr'); teq=bv(L['period'],'total_equity'); sales=rev(L)
        if not ta: return None,None
        tl=ta-(teq or 0)
        X1=(ca-cl)/ta if (ca and cl) else 0; X2=(re_/ta) if re_ else 0; X3=(eb/ta) if eb else 0
        X4=(mc/tl) if (mc and tl) else 0; X5=(sales/ta) if sales else 0
        Z=1.2*X1+1.4*X2+3.3*X3+0.6*X4+1.0*X5
        zone='Safe zone' if Z>2.99 else ('Grey zone' if Z>=1.81 else 'Distress zone')
        return Z,zone
    altZ,altZone=altman()
    P=YRS[-2] if len(YRS)>1 else None
    def piotroski():
        pts=[];
        def add(name,ok): pts.append((name,bool(ok)))
        add('Net profit positive', (np_(L) or 0)>0)
        add('Operating cash flow positive', (cfoL or 0)>0)
        add('Return on assets rising', P and roa(L) is not None and roa(P) is not None and roa(L)>roa(P))
        add('CFO exceeds net profit', cfoL is not None and np_(L) is not None and cfoL>np_(L))
        add('Leverage reduced YoY', P and bv(L['period'],'borrowings_noncurrent') is not None and bv(P['period'],'borrowings_noncurrent') is not None and (bv(L['period'],'borrowings_noncurrent')/ (bv(L['period'],'total_assets') or 1))<(bv(P['period'],'borrowings_noncurrent')/(bv(P['period'],'total_assets') or 1)))
        add('Current ratio improved', P and curr_ratio(L) is not None and curr_ratio(P) is not None and curr_ratio(L)>curr_ratio(P))
        add('No equity dilution', P and bv(L['period'],'equity_capital') is not None and bv(P['period'],'equity_capital') is not None and bv(L['period'],'equity_capital')<=bv(P['period'],'equity_capital'))
        add('Gross margin improved', P and gpm(L) is not None and gpm(P) is not None and gpm(L)>gpm(P))
        add('Asset turnover improved', P and aturn(L) is not None and aturn(P) is not None and aturn(L)>aturn(P))
        return pts
    piot=piotroski(); piot_score=sum(1 for _,ok in piot if ok)
    
    # ---- charts (inline SVG) ----
    def barchart(vals1,vals2,labels,w=706,h=150):
        pad=22; bw=(w-2*pad)/len(labels); mx=max([abs(v) for v in vals1+vals2 if v is not None] or [1])
        def y(v): return (h-26) - (abs(v)/mx)*(h-44)
        bars=''
        for i,(a,b,lb) in enumerate(zip(vals1,vals2,labels)):
            x0=pad+i*bw
            if a is not None: bars+=f'<rect x="{x0+bw*0.16:.1f}" y="{y(a):.1f}" width="{bw*0.32:.1f}" height="{(h-26)-y(a):.1f}" fill="{NAVY}"/>'
            if b is not None: bars+=f'<rect x="{x0+bw*0.52:.1f}" y="{y(b):.1f}" width="{bw*0.32:.1f}" height="{(h-26)-y(b):.1f}" fill="{GOLD}"/>'
            bars+=f'<text x="{x0+bw*0.5:.1f}" y="{h-9}" text-anchor="middle" font-size="16" fill="#9CA3AF">{lb}</text>'
        return f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}">{bars}<line x1="{pad}" y1="{h-26}" x2="{w-pad}" y2="{h-26}" stroke="{RULE}"/></svg>'
    def linechart(s1,s2,labels,w=706,h=130):
        pad=22; mx=max([v for v in s1+s2 if v is not None] or [1]); mn=min([v for v in s1+s2 if v is not None] or [0]); rng=(mx-mn) or 1
        def pt(i,v): return (pad+i*((w-2*pad)/(len(labels)-1)), (h-22)-((v-mn)/rng)*(h-40))
        def path(s,col):
            pts=[pt(i,v) for i,v in enumerate(s) if v is not None]
            d='M'+' L'.join(f'{x:.1f},{y:.1f}' for x,y in pts)
            dots=''.join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="{col}"/>' for x,y in pts)
            return f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2"/>{dots}'
        labs=''.join(f'<text x="{pt(i,mn)[0]:.1f}" y="{h-7}" text-anchor="middle" font-size="16" fill="#9CA3AF">{lb}</text>' for i,lb in enumerate(labels))
        return f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}">{path(s1,NAVY)}{path(s2,GOLD)}{labs}</svg>'
    
    ylab=[f'FY{r["period"][2:4]}' for r in YRS]
    chart_rp=barchart(revL,npL,ylab); chart_mar=linechart(opmL,npmL,ylab)
    
    # ---- KEY OBSERVATIONS (deterministic) ----
    obs=[]
    obs.append(f"Revenue compounded at <b>{f(rc)}%</b> a year over {fy1}–{fyN}, reaching <b>₹{inr(revL[-1])} Cr</b> in {fyN} ({G(yoy_rev)} YoY).")
    if npL[0] is not None and npL[0]<0 and npL[-1] is not None and npL[-1]>0:
        obs.append(f"Net profit turned around from a loss of ₹{inr(abs(npL[0]))} Cr in {fy1} to a profit of <b>₹{inr(npL[-1])} Cr</b> in {fyN}; net margin moved from {f(npmL[0])}% to <b>{f(npmL[-1])}%</b>.")
    elif pc is not None:
        obs.append(f"Net profit grew {'faster' if pc>(rc or 0) else 'slower'} than revenue at <b>{f(pc)}%</b> CAGR; net margin moved from {f(npmL[0])}% to <b>{f(npmL[-1])}%</b>.")
    else:
        obs.append(f"Net profit was <b>₹{inr(npL[-1])} Cr</b> in {fyN} ({G(yoy_np)} YoY); net margin {f(npmL[-1])}%.")
    trend='expanded' if (opmL[-1] or 0)>(opmL[0] or 0) else ('compressed' if (opmL[-1] or 0)<(opmL[0] or 0) else 'held steady')
    obs.append(f"Operating margin was <b>{f(opmL[-1])}%</b> in {fyN}, {trend} from {f(opmL[0])}% in {fy1}.")
    obs.append(f"Return on equity is <b>{f(roeL)}%</b> and return on capital employed <b>{f(roceL)}%</b>; the DuPont breakdown is shown in §08.")
    obs.append(f"Leverage is {'conservative' if (deL or 0)<0.5 else 'moderate' if (deL or 0)<1 else 'elevated'} at <b>{f(deL,2)}×</b> debt/equity with interest cover of {f(icovL,1)}×.")
    obs.append(f"Operating cash flow of ₹{inr(cfoL)} Cr funded capex of ₹{inr(capexL)} Cr, leaving free cash flow of <b>₹{inr(fcfL)} Cr</b> (FCF positive in {fcf_pos} of {ncol} years).")
    if cccL is not None: obs.append(f"The cash conversion cycle is <b>{f(cccL,0)} days</b>.")
    obs.append(f"Promoters hold <b>{f(nz(sh,'promoters'),2)}%</b>{' with '+f(sm.get('pledged_pct'),2)+'% pledged' if sm.get('pledged_pct') else ' with no disclosed pledge'}; FIIs {f(nz(sh,'fii'),2)}%, DIIs {f(nz(sh,'dii'),2)}%.")
    obs.append(f"The stock returned {G(sm.get('return_1y_pct'))} over 1 year and {G(sm.get('return_5y_pct'))} over 5 years, and trades at <b>{f(sm.get('pe_ratio'))}×</b> earnings and {f(sm.get('pb_ratio'),2)}× book.")
    obs_html=''.join(f'<li>{o}</li>' for o in obs)
    
    # ---- tables ----
    summ=(row('Revenue',revL)+row('EBITDA',[ebitda(r) for r in YRS])+row('EBITDA margin',opmL,'pct')
     +row('Net profit',npL,cls='hi')+row('Net margin',npmL,'pct')+row('EPS (₹)',[nz(r,'eps') for r in YRS],'num')
     +row('Book value (₹)',[bvps(r) for r in YRS],'num',dec=0)+row('ROE',[roe_y(r) for r in YRS],'pct')
     +row('ROCE',[roce(r) for r in YRS],'pct')+row('Debt / Equity',[de(r) for r in YRS],'num',dec=2))
    ist=(row('Revenue from operations',revL)+row('Cost of materials / COGS',[cogs(r) for r in YRS])
     +row('Gross profit',[(rev(r)-cogs(r)) if (rev(r) is not None and cogs(r) is not None) else None for r in YRS],cls='sub')
     +row('Gross margin',[gpm(r) for r in YRS],'pct')+row('Employee cost',[nz(r,'employee_benefit_expense') for r in YRS])
     +row('Other expenses',[nz(r,'other_expenses') for r in YRS])+row('EBITDA',[ebitda(r) for r in YRS],cls='sub')
     +row('EBITDA margin',opmL,'pct')+row('Depreciation',[nz(r,'depreciation') for r in YRS])
     +row('EBIT',[ebit(r) for r in YRS],cls='sub')+row('Finance costs',[nz(r,'finance_costs','interest') for r in YRS])
     +row('Other income',[nz(r,'other_income') for r in YRS])+row('Profit before tax',[nz(r,'pbt') for r in YRS],cls='sub')
     +row('Tax',[nz(r,'tax_expense') for r in YRS])+row('Tax rate',[nz(r,'tax_pct') for r in YRS],'pct')
     +row('Net profit',npL,cls='hi')+row('EPS (₹)',[nz(r,'eps') for r in YRS],'num'))
    # common-size (% of revenue)
    def cs(fn): return [ (fn(r)/rev(r)*100) if (fn(r) is not None and rev(r)) else None for r in YRS]
    cstab=(row('COGS',cs(cogs),'pct')+row('Gross profit',[gpm(r) for r in YRS],'pct',cls='sub')
     +row('Employee cost',cs(lambda r:nz(r,'employee_benefit_expense')),'pct')+row('Other expenses',cs(lambda r:nz(r,'other_expenses')),'pct')
     +row('EBITDA',opmL,'pct',cls='sub')+row('Depreciation',cs(lambda r:nz(r,'depreciation')),'pct')
     +row('Finance costs',cs(lambda r:nz(r,'finance_costs','interest')),'pct')+row('PBT',cs(lambda r:nz(r,'pbt')),'pct',cls='sub')
     +row('Net profit',npmL,'pct',cls='hi'))
    qs=QTR[-8:]; qh=''.join(f'<th>{r["period"][:7]}</th>' for r in qs)
    def qg(i,fn,lag):
        gi=len(QTR)-8+i; return gpc(fn(QTR[gi]),fn(QTR[gi-lag])) if gi-lag>=0 else None
    qtab=(row('Revenue',[rev(r) for r in qs])+row('  QoQ',[qg(i,rev,1) for i in range(len(qs))],'g')+row('  YoY',[qg(i,rev,4) for i in range(len(qs))],'g')
     +row('Operating profit',[(rev(r)-nz(r,'expenses')) if (rev(r) is not None and nz(r,'expenses') is not None) else None for r in qs],cls='sub')
     +row('PBT',[nz(r,'pbt') for r in qs])+row('Net profit',[np_(r) for r in qs],cls='hi')
     +row('  QoQ',[qg(i,np_,1) for i in range(len(qs))],'g')+row('  YoY',[qg(i,np_,4) for i in range(len(qs))],'g')
     +row('Net margin',[npm(r) for r in qs],'pct')+row('EPS (₹)',[nz(r,'eps') for r in qs],'num',dec=2))
    bst=(row('Equity capital',[bv(r['period'],'equity_capital') for r in YRS])+row('Reserves',[bv(r['period'],'reserves') for r in YRS])
     +row('Net worth',[bv(r['period'],'total_equity') for r in YRS],cls='sub')+row('Borrowings',[bv(r['period'],'borrowings') for r in YRS])
     +row('Total liabilities',[bv(r['period'],'total_assets') for r in YRS],cls='sub')+row('Fixed assets',[bv(r['period'],'fixed_assets','property_plant_equipment') for r in YRS])
     +row('Investments',[bv(r['period'],'investments') for r in YRS])+row('Inventories',[bv(r['period'],'inventories') for r in YRS])
     +row('Trade receivables',[bv(r['period'],'trade_receivables') for r in YRS])+row('Cash & equivalents',[bv(r['period'],'cash_equivalents') for r in YRS])
     +row('Total assets',[bv(r['period'],'total_assets') for r in YRS],cls='sub'))
    cft=(row('Cash from operations',[nz(CFI.get(r['period']),'cfo') for r in YRS],cls='sub')+row('Cash from investing',[nz(CFI.get(r['period']),'cfi') for r in YRS])
     +row('Cash from financing',[nz(CFI.get(r['period']),'cff') for r in YRS])+row('Capex',[nz(CFI.get(r['period']),'capex') for r in YRS])
     +row('Free cash flow',[fcf(r) for r in YRS],cls='hi')+row('Dividend paid',[nz(CFI.get(r['period']),'dividend_paid') for r in YRS]))
    wctab=(row('Debtor days',[days(r,'trade_receivables') for r in YRS],'num',dec=0)+row('Inventory days',[days(r,'inventories') for r in YRS],'num',dec=0)
     +row('Payable days',[days(r,'trade_payables') for r in YRS],'num',dec=0)+row('Cash conversion cycle',[ccc(r) for r in YRS],'num',dec=0,cls='sub')
     +row('Current ratio',[curr_ratio(r) for r in YRS],'num',dec=2)+row('Asset turnover (x)',[aturn(r) for r in YRS],'num',dec=2))
    rat=('<tr class="grp"><td colspan="'+str(ncol+1)+'">Profitability (%)</td></tr>'
     +row('Gross margin',[gpm(r) for r in YRS],'pct')+row('Operating margin',opmL,'pct')+row('Net margin',npmL,'pct')
     +row('Return on equity',[roe_y(r) for r in YRS],'pct')+row('Return on capital employed',[roce(r) for r in YRS],'pct')+row('Return on assets',[roa(r) for r in YRS],'pct')
     +'<tr class="grp"><td colspan="'+str(ncol+1)+'">Growth — YoY (%)</td></tr>'
     +row('Revenue',[gpc(revL[i],revL[i-1]) if i>0 else None for i in range(ncol)],'g')+row('EBITDA',[gpc(ebitda(YRS[i]),ebitda(YRS[i-1])) if i>0 else None for i in range(ncol)],'g')+row('Net profit',[gpc(npL[i],npL[i-1]) if i>0 else None for i in range(ncol)],'g')
     +'<tr class="grp"><td colspan="'+str(ncol+1)+'">Leverage & per share</td></tr>'
     +row('Debt / Equity',[de(r) for r in YRS],'num',dec=2)+row('Interest coverage (x)',[icov(r) for r in YRS],'num',dec=1)
     +row('EPS (₹)',[nz(r,'eps') for r in YRS],'num')+row('Book value (₹)',[bvps(r) for r in YRS],'num',dec=0))
    shh=''.join(f'<th>{r["period"][:7]}</th>' for r in SHP[-8:])
    shtab=(row('Promoters',[nz(r,'promoters') for r in SHP[-8:]],'pct',dec=2)+row('FIIs',[nz(r,'fii') for r in SHP[-8:]],'pct',dec=2)
     +row('DIIs',[nz(r,'dii') for r in SHP[-8:]],'pct',dec=2)+row('Public',[nz(r,'public') for r in SHP[-8:]],'pct',dec=2))
    
    # DuPont
    du_npm=npm(L); du_at=aturn(L); du_lev=(bv(L['period'],'total_assets')/bv(L['period'],'total_equity')) if (bv(L['period'],'total_assets') and bv(L['period'],'total_equity')) else None
    du_roe=(du_npm/100*du_at*du_lev*100) if (du_npm is not None and du_at and du_lev) else None
    
    # Quality scorecard html
    piot_html=''.join(f'<div class="pf"><span class="{"ok" if ok else "no"}">{"✓" if ok else "✕"}</span>{name}</div>' for name,ok in piot)
    # Valuation
    peg=(sm.get('pe_ratio')/pc) if (sm.get('pe_ratio') and pc) else None
    ey=(1/sm.get('pe_ratio')*100) if sm.get('pe_ratio') else None
    payout=(nz(CFI.get(L['period']),'dividend_paid')/np_(L)*100) if (nz(CFI.get(L['period']),'dividend_paid') and np_(L)) else None
    h52=sm.get('high_52w'); l52=sm.get('low_52w'); cmp_=sm.get('latest_price')
    frm_high=((cmp_/h52-1)*100) if (cmp_ and h52) else None
    def vkv(l,v): return f'<div class="vk"><span>{l}</span><b>{v}</b></div>'
    val_html=(vkv('P / E',f(sm.get('pe_ratio'))+'×')+vkv('P / B',f(sm.get('pb_ratio'),2)+'×')+vkv('EV / EBITDA',f(sm.get('ev_ebitda'))+'×')
     +vkv('PEG (PE÷gr)',f(peg,2))+vkv('Earnings yield',f(ey)+'%')+vkv('Dividend yield',f(sm.get('div_yield_pct'),2)+'%')
     +vkv('Dividend payout',f(payout)+'%')+vkv('From 52-wk high',G(frm_high)))
    ret_html=(vkv('1 month',G(sm.get('return_1m_pct')))+vkv('3 months',G(sm.get('return_3m_pct')))+vkv('6 months',G(sm.get('return_6m_pct')))
     +vkv('1 year',G(sm.get('return_1y_pct')))+vkv('5 years (CAGR)',G(sm.get('return_5y_pct'))))
    
    def kvb(l,v): return f'<div class="kv"><span>{l}</span><b>{v}</b></div>'
    snap=(kvb('CMP (₹)',inr(cmp_,2))+kvb('Mkt cap (₹ Cr)',inr(sm.get('market_cap_cr')))+kvb('52-wk H/L',f'{inr(h52,0)} / {inr(l52,0)}')
     +kvb('P/E · P/B',f'{f(sm.get("pe_ratio"))} · {f(sm.get("pb_ratio"),2)}')+kvb('ROE · ROCE',f'{f(roeL)}% · {f(roceL)}%')
     +kvb('D/E',f(deL,2))+kvb('Div yield',f(sm.get('div_yield_pct'),2)+'%')+kvb('Promoter · Pledge',f'{f(nz(sh,"promoters"),1)}% · {f(sm.get("pledged_pct") or 0,1)}%'))
    
    SHIELD=f'<svg viewBox="0 0 22 24" width="17" height="19" style="vertical-align:-3px;margin-right:6px"><path d="M11 1 L20 4 V12 C20 18 11 23 11 23 C11 23 2 18 2 12 V4 Z" fill="none" stroke="{GOLD}" stroke-width="1.6"/><path d="M7 11 L11 7 L15 11 M11 7 V16" stroke="{GOLD}" stroke-width="1.4" fill="none"/></svg>'
    
    HTML=f'''<!doctype html><html><head><meta charset="utf-8">
    <link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Spectral:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500;600&display=swap" rel="stylesheet"><style>
    @page{{size:A4;margin:0}}*{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Inter',system-ui,sans-serif;color:{INK};font-size:10px;line-height:1.45;-webkit-font-smoothing:antialiased}}
    .pg{{width:210mm;min-height:297mm;padding:0 0 13mm;position:relative;background:#fff}}
    .mast{{display:flex;justify-content:space-between;align-items:center;padding:11px 14mm 10px;border-bottom:1px solid {RULE}}}
    .mast .logo{{height:30px;width:auto;display:block}}
    .mast .r{{text-align:right}} .mast .r b{{display:block;color:{NAVY};font-size:11px;font-weight:700;letter-spacing:.2px}} .mast .r small{{font-size:8px;letter-spacing:1.5px;color:{INK4};text-transform:uppercase}}
    .rhead{{display:flex;align-items:center;gap:8px;padding:8px 14mm 7px;border-bottom:1px solid {RULE}}}
    .rhead img{{height:16px}} .rhead .nm{{font-size:9px;font-weight:700;color:{INK2}}} .rhead .lb{{margin-left:auto;font-size:8px;letter-spacing:1.5px;color:{INK4};text-transform:uppercase}}
    .title{{padding:13px 14mm 0;display:flex;justify-content:space-between;align-items:flex-end}}
    .title h1{{font-size:26px;color:{INK};font-weight:800;letter-spacing:-.4px;line-height:1.05}} .title .sub{{font-size:10px;color:{INK3};margin-top:5px}} .title .sub b{{color:{NAVY};font-weight:600}}
    .title .tk{{text-align:right}} .title .tk b{{font-family:'JetBrains Mono';font-size:18px;color:{INK};font-weight:700}} .title .tk small{{display:block;font-size:9px;margin-top:1px}}
    .snap{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:{RULE};border:1px solid {RULE};margin:11px 14mm 0;border-radius:10px;overflow:hidden}}
    .kv{{background:#fff;padding:7px 11px}}
    .kv span{{display:block;font-size:7.4px;color:{INK4};text-transform:uppercase;letter-spacing:.4px;font-weight:600}} .kv b{{font-family:'JetBrains Mono';font-size:12.5px;font-weight:600;color:{INK};display:block;margin-top:2px}}
    .sec{{margin:11px 14mm 6px;font-size:14.5px;font-weight:700;color:{INK};display:flex;align-items:center;gap:9px}}
    .sec .no{{font-family:'JetBrains Mono';background:{NAVY};color:#fff;font-size:9.5px;font-weight:700;padding:2px 7px;border-radius:6px}}
    .sec em{{font-style:normal;font-weight:500;font-size:9px;color:{INK4};margin-left:auto}}
    .obs{{margin:0 14mm;background:{SOFT};border:1px solid #dfe3fb;border-radius:10px;padding:11px 15px 11px 30px}}
    .obs li{{font-size:9.8px;color:{INK2};margin:3px 0;line-height:1.55}} .obs li b{{color:{NAVY};font-weight:700}}
    .charts{{display:flex;gap:13px;margin:0 14mm}}
    .cbox{{flex:1;border:1px solid {RULE};border-radius:10px;padding:10px 12px 5px;background:{BG2}}}
    .cbox .ct{{font-size:9.5px;font-weight:700;color:{INK};margin-bottom:3px}}
    .cbox .lg{{font-size:8.2px;color:{INK3}}} .cbox .lg i{{display:inline-block;width:8px;height:8px;border-radius:2px;margin:0 4px -1px 8px}}
    table{{width:calc(100% - 28mm);margin:0 14mm;border-collapse:collapse;font-variant-numeric:tabular-nums}}
    th{{background:{NAVY};color:#fff;padding:5px 7px;text-align:right;font-weight:600;font-size:8.8px}} th.lbl{{text-align:left}}
    td{{font-family:'JetBrains Mono';font-size:9px;padding:2.8px 7px;text-align:right;border-bottom:1px solid #eef0f3;color:{INK2}}} td.lbl{{font-family:'Inter';font-size:9.4px;font-weight:500;text-align:left;color:{INK2}}}
    tr.sub td{{font-weight:700;color:{INK};background:{BG3}}}
    tr.hi td{{font-weight:700;color:{NAVY};background:{SOFT}}}
    tr.grp td{{background:{NAVYD};color:#fff;font-weight:700;text-transform:uppercase;letter-spacing:.5px;font-size:8px;text-align:left;padding:4px 7px}}
    td .u{{color:{UP};font-weight:600}} td .d{{color:{DN};font-weight:600}} td .m{{color:{INK4}}}
    .cagr{{margin:7px 14mm 0;font-size:9.3px;color:{INK3}}} .cagr b{{color:{NAVY};font-weight:700}}
    .dupont{{margin:0 14mm;display:flex;gap:8px}} .dpc{{flex:1;border:1px solid {RULE};border-radius:10px;padding:9px;text-align:center;background:{BG2}}}
    .dpc small{{display:block;font-size:7.6px;color:{INK4};text-transform:uppercase;font-weight:600;letter-spacing:.3px}} .dpc b{{font-family:'JetBrains Mono';font-size:16px;color:{INK}}} .dpx{{align-self:center;font-size:15px;color:{NAVY};font-weight:700}}
    .qual{{display:flex;gap:11px;margin:0 14mm}}
    .qbox{{flex:1;border:1px solid {RULE};border-radius:10px;padding:10px 13px}} .qbox h4{{font-size:9.5px;color:{INK};margin-bottom:6px;font-weight:700}}
    .score{{font-family:'JetBrains Mono';font-size:27px;font-weight:700;color:{NAVY}}} .score small{{font-size:12px;color:{INK4};font-weight:400}}
    .pf{{font-size:8.8px;color:{INK2};margin:2px 0}} .pf span{{display:inline-block;width:13px;font-weight:800}} .pf .ok{{color:{UP}}} .pf .no{{color:{DN}}}
    .zone{{display:inline-block;padding:3px 10px;border-radius:6px;font-size:9px;font-weight:700;margin-top:4px}}
    .vgrid{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:{RULE};margin:0 14mm;border:1px solid {RULE};border-radius:10px;overflow:hidden}}
    .vk{{background:#fff;padding:7px 11px}} .vk span{{display:block;font-size:7.4px;color:{INK4};text-transform:uppercase;letter-spacing:.3px;font-weight:600}} .vk b{{font-family:'JetBrains Mono';font-size:11.5px;font-weight:600;color:{INK};display:block;margin-top:2px}}
    .foot{{position:absolute;bottom:0;left:0;right:0;display:flex;justify-content:space-between;padding:7px 14mm;border-top:1px solid {RULE};color:{INK4};font-size:7.6px}} .foot b{{color:{NAVY}}}
    .disc{{margin:10px 14mm 0;font-size:8px;color:{INK4};line-height:1.6;border-top:1px solid {RULE};padding-top:8px}} .disc b{{color:{INK3}}}
    </style></head><body>
    
    <div class="pg">
      <div class="mast"><img class="logo" src="{LOGO}" alt="Stocks Sena"/>
        <div class="r"><b>Equity Research</b><small>Company Financial Report · 05 Jun 2026</small></div></div>
      <div class="title"><div><h1>{sm['name']}</h1><div class="sub"><b>NSE: {sm['symbol']}</b> &nbsp;·&nbsp; {sm.get('sector') or '–'} &nbsp;·&nbsp; Consolidated, ₹ Crore unless stated</div></div>
        <div class="tk"><b>₹{inr(cmp_,2)}</b><br>{G(sm.get('return_1y_pct'))} 1Y</div></div>
      <div class="snap">{snap}</div>
      {SEC('01','Key Observations','computed from filed results — factual, not advice')}
      <ul class="obs">{obs_html}</ul>
      {SEC('02','Revenue, Profit &amp; Margins','8-year trend')}
      <div class="charts">
        <div class="cbox"><div class="ct">Revenue &amp; Net profit (₹ Cr)</div>{chart_rp}<div class="lg"><i style="background:{NAVY}"></i>Revenue<i style="background:{GOLD}"></i>Net profit</div></div>
        <div class="cbox"><div class="ct">Operating &amp; Net margin (%)</div>{chart_mar}<div class="lg"><i style="background:{NAVY}"></i>Operating margin<i style="background:{GOLD}"></i>Net margin</div></div>
      </div>
      {SEC('03','Financial Summary')}
      {tbl('₹ Crore',yhead,summ)}
      <div class="cagr">{ncol-1}-year CAGR &nbsp;·&nbsp; Revenue <b>{f(rc)}%</b> &nbsp;·&nbsp; EBITDA <b>{f(ebc)}%</b> &nbsp;·&nbsp; Net profit <b>{f(pc)}%</b>.</div>
      <div class="foot"><div><b>STOCKS SENA</b> &nbsp;·&nbsp; Equity Research</div><div>{sm['symbol']} &nbsp;·&nbsp; Page 1 of 4</div></div>
    </div>
    
    <div class="pg">
      <div class="rhead"><img src="{LOGO}"/><span class="nm">{sm['name']} · NSE: {sm['symbol']}</span><span class="lb">Equity Research</span></div>
      {SEC('04','Income Statement')}
      {tbl('₹ Crore',yhead,ist)}
      {SEC('05','Common-size Income Statement','each line as % of revenue')}
      {tbl('% of revenue',yhead,cstab)}
      {SEC('06','Quarterly Results','last 8 quarters, ₹ Cr')}
      {tbl('₹ Crore',qh,qtab)}
      <div class="foot"><div><b>STOCKS SENA</b> &nbsp;·&nbsp; Equity Research</div><div>{sm['symbol']} &nbsp;·&nbsp; Page 2 of 4</div></div>
    </div>
    
    <div class="pg">
      <div class="rhead"><img src="{LOGO}"/><span class="nm">{sm['name']} · NSE: {sm['symbol']}</span><span class="lb">Equity Research</span></div>
      {SEC('07','Balance Sheet')}
      {tbl('₹ Crore',yhead,bst)}
      {SEC('08','Cash Flow')}
      {tbl('₹ Crore',yhead,cft)}
      {SEC('09','Working Capital &amp; Efficiency')}
      {tbl('Metric',yhead,wctab)}
      {SEC('10','Ratio Analysis')}
      {tbl('Ratio',yhead,rat)}
      <div class="foot"><div><b>STOCKS SENA</b> &nbsp;·&nbsp; Equity Research</div><div>{sm['symbol']} &nbsp;·&nbsp; Page 3 of 4</div></div>
    </div>
    
    <div class="pg">
      <div class="rhead"><img src="{LOGO}"/><span class="nm">{sm['name']} · NSE: {sm['symbol']}</span><span class="lb">Equity Research</span></div>
      {SEC('11','DuPont — Return on Equity ('+fyN+')')}
      <div class="dupont"><div class="dpc"><small>Net margin</small><b>{f(du_npm)}%</b></div><div class="dpx">×</div>
        <div class="dpc"><small>Asset turnover</small><b>{f(du_at,2)}×</b></div><div class="dpx">×</div>
        <div class="dpc"><small>Equity multiplier</small><b>{f(du_lev,2)}×</b></div><div class="dpx">=</div>
        <div class="dpc" style="border-color:{GOLD};background:{CREAM}"><small>Return on equity</small><b>{f(du_roe)}%</b></div></div>
      {SEC('12','Quality Scorecard')}
      <div class="qual">
        <div class="qbox"><h4>Piotroski F-Score</h4><div class="score">{piot_score}<small>/9</small></div>{piot_html}</div>
        <div class="qbox"><h4>Altman Z-Score</h4><div class="score">{f(altZ,2)}</div>
          <span class="zone" style="background:{('#dcf5e6;color:#157a3a' if altZ and altZ>2.99 else '#fdeecf;color:#a07a14' if altZ and altZ>=1.81 else '#fbe0dd;color:#c0392b')}">{altZone or '–'}</span>
          <div style="font-size:7.4px;color:#7a8290;margin-top:5px;line-height:1.5">Bankruptcy-risk model. &gt;2.99 safe · 1.81–2.99 grey · &lt;1.81 distress. Computed from working capital, retained earnings, EBIT, market cap and sales over total assets.</div></div>
        <div class="qbox"><h4>Snapshot flags</h4>
          <div class="pf"><span class="{'ok' if (deL or 0)<0.6 else 'no'}">{'✓' if (deL or 0)<0.6 else '!'}</span>Debt/equity {f(deL,2)}×</div>
          <div class="pf"><span class="{'ok' if (sm.get('pledged_pct') or 0)==0 else 'no'}">{'✓' if (sm.get('pledged_pct') or 0)==0 else '!'}</span>Promoter pledge {f(sm.get('pledged_pct') or 0,1)}%</div>
          <div class="pf"><span class="{'ok' if fcfL and fcfL>0 else 'no'}">{'✓' if fcfL and fcfL>0 else '!'}</span>Free cash flow positive</div>
          <div class="pf"><span class="{'ok' if (icovL or 0)>3 else 'no'}">{'✓' if (icovL or 0)>3 else '!'}</span>Interest cover {f(icovL,1)}×</div>
          <div class="pf"><span class="{'ok' if (npmL[-1] or 0)>(npmL[0] or 0) else 'no'}">{'✓' if (npmL[-1] or 0)>(npmL[0] or 0) else '!'}</span>Net margin trend</div></div>
      </div>
      {SEC('13','Valuation &amp; Returns')}
      <div class="vgrid">{val_html}</div>
      <div class="vgrid" style="margin-top:7px;grid-template-columns:repeat(5,1fr)">{ret_html}</div>
      {SEC('14','Shareholding Pattern','last 8 quarters, %')}
      {tbl('Holder',shh,shtab)}
      <div class="disc">All figures consolidated unless stated, in ₹ Crore, sourced from the company's filed results with NSE/BSE and computed by Stocks Sena. Growth rates, margins, CAGRs, ratios, Piotroski F-Score and Altman Z-Score are calculated from reported figures and are factual indicators, not forecasts. This document is a financial summary for <b>educational purposes only</b>; it is not investment advice, a recommendation, or an offer to buy or sell any security. Read the company's official filings and consult a SEBI-registered investment adviser before any decision.</div>
      <div class="foot"><div><b>STOCKS SENA</b> &nbsp;·&nbsp; stockssena.com &nbsp;·&nbsp; Market Seekho · Sena Mein Aao</div><div>{sm['symbol']} &nbsp;·&nbsp; Page 4 of 4</div></div>
    </div>
    </body></html>'''
    return HTML
