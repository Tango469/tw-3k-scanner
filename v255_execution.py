"""Additive execution research; daily OHLC, no guaranteed fills."""
import csv
from pathlib import Path
from collections import defaultdict, Counter
ROOT=Path(__file__).resolve().parent
H=(1,3,5,10,20)
BUY_FEE=.001425
SELL_FEE=.001425
TAX=.003

def read(p):
    with p.open(encoding='utf-8-sig',newline='') as f: return list(csv.DictReader(f))
def write(p,rows,fields):
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
def simulate(bars,trigger,fail,h):
    for i,b in enumerate(bars[:20]):
        if b['high']>=trigger and b['low']<=fail:
            return 'ambiguous_entry',None,None,None
        if b['low']<=fail:return 'failed_before_entry',None,None,None
        if b['high']>=trigger:
            entry=max(trigger,b['open'])
            # Taiwan cash-equity T+1 assumption; no same-day exit.
            for j,c in enumerate(bars[i+1:i+h+1],1):
                if c['low']<=fail:
                    return 'stop',entry,min(fail,c['open']),j
                if j==h:return 'time_exit',entry,c['close'],j
            return 'pending_exit',entry,None,None
    return ('expired' if len(bars)>=20 else 'pending_entry'),None,None,None

def run(rows=None):
    rows=rows if rows is not None else read(ROOT/'data/daily.csv')
    events=read(ROOT/'docs/trigger_tracking.csv')
    snapshots=read(ROOT/'data/factors/signal_snapshots.csv')
    snap={(r['date'],r['market'],r['code'],r['type']):r for r in snapshots}
    series=defaultdict(list)
    for r in rows:
        series[r['market'],r['code']].append({**r,**{k:float(r[k]) for k in ('open','high','low','close')}})
    for s in series.values():s.sort(key=lambda r:r['date'])
    idx={k:{r['date']:i for i,r in enumerate(s)} for k,s in series.items()}
    out=[]; audit=Counter(); groups=defaultdict(list)
    for e in events:
        k=e['market'],e['code']; d=e['signal_date']; t=float(e['trigger']); f=float(e['fail']); ss=snap.get((d,*k,e['type']),{})
        pos=idx.get(k,{}).get(d)
        if pos is None or not 0<f<t: audit['invalid_event']+=1;continue
        b=series[k][pos]; liquid=float(b['volume'])>=100000 and float(b['volume'])*b['close']>=2000000 and pos>=19
        stage=ss.get('low_base_stage','missing_snapshot'); score=ss.get('low_base_score','')
        for h in H:
            status,entry,exit,held=simulate(series[k][pos+1:pos+41],t,f,h)
            net=100*(exit*(1-SELL_FEE-TAX)/(entry*(1+BUY_FEE))-1) if exit is not None else None
            r=dict(signal_date=d,market=k[0],code=k[1],type=e['type'],stage=stage,low_base_score=score,liquidity_pass=int(liquid),horizon=h,status=status,entry=entry,exit=exit,held=held,net_return_pct=round(net,4) if net is not None else '')
            future=series[k][pos+1:pos+41]
            ei=next((i for i,x in enumerate(future[:20]) if x['high']>=t),None) if entry is not None else None
            mature=ei is not None and len(future)-ei-1>=h
            ed=future[ei]['date'] if ei is not None else ''
            xd=future[ei+held]['date'] if held is not None else ''
            # Exclude entry and exit bars from excursions: intraday sequence unknown.
            window=future[ei+1:ei+held] if held is not None else []
            r.update(entry_date=ed,exit_date=xd,trigger=t,fail=f,matured=int(mature),
                fixed10=int(ei is not None and len(future)-ei-1>=10),
                initial_risk_pct=round(100*(1-f/entry),4) if entry else '',
                gap_pct=round(100*(entry/t-1),4) if entry else '',
                mfe_interior_pct=round(100*(max(x['high'] for x in window)/entry-1),4) if window else '',
                mae_interior_pct=round(100*(min(x['low'] for x in window)/entry-1),4) if window else '',
                exit_gap_pct=round(100*(exit/f-1),4) if status=='stop' else '',
                extreme_loss=int(net is not None and net<=-20))
            out.append(r);audit[f'h{h}_{status}']+=1
            # Use fully matured entry cohorts, including early stops, to avoid censoring bias.
            entry_i=next((i for i,x in enumerate(series[k][pos+1:pos+21]) if x['high']>=t),None)
            mature=entry_i is not None and len(series[k])-pos-2-entry_i>=h
            if net is not None and mature:
                for g in ['ALL','type_'+e['type']]+(['liquid'] if liquid else [])+(['liquid_waiting'] if liquid and stage=='等待啟動' else []):groups[h,g].append(net)
    report=[]
    for (h,g),v in sorted(groups.items()):
        n=len(v);report.append(dict(horizon=h,group=g,samples=n,mean_net_pct=round(sum(v)/n,4),win_rate_pct=round(100*sum(x>0 for x in v)/n,2),reportable=int(n>=20)))
    write(ROOT/'docs/v255_trades.csv',out,['signal_date','market','code','type','stage','low_base_score','liquidity_pass','horizon','status','entry','exit','held','net_return_pct','entry_date','exit_date','trigger','fail','matured','fixed10','initial_risk_pct','gap_pct','mfe_interior_pct','mae_interior_pct','exit_gap_pct','extreme_loss'])
    write(ROOT/'docs/v255_performance.csv',report,['horizon','group','samples','mean_net_pct','win_rate_pct','reportable'])
    write(ROOT/'docs/v255_audit.csv',[dict(check=k,value=v) for k,v in sorted(audit.items())],['check','value'])
    diagnostics(out)
    print('V2.5.5 execution:',len(events),'events;',len(out),'scenario rows;',dict(audit))

def diagnostics(out):
    # Non-overlapping positions per horizon; do not reuse a stock on exit day.
    for h in H:
        busy={}
        candidates=sorted((r for r in out if r['horizon']==h and r['entry_date']),key=lambda r:(r['entry_date'],r['signal_date'],r['market'],r['code'],r['type']))
        for r in candidates:
            k=r['market'],r['code']
            r['nonoverlap']=int(r['entry_date']>busy.get(k,''))
            if r['nonoverlap']:busy[k]=r['exit_date'] or '9999-12-31'
    g=defaultdict(list); pending=Counter()
    for r in out:
        h=r['horizon']; v=r['net_return_pct']
        if r['status']=='pending_exit':pending[h]+=1
        if v=='':continue
        cohorts=['closed']+(['matured'] if r['matured'] else [])+(['fixed10'] if r['fixed10'] and h<=10 else [])+(['nonoverlap_matured'] if r['matured'] and r.get('nonoverlap') else [])
        risk=r['initial_risk_pct']; gap=r['gap_pct']
        labels=['ALL','risk_'+('0_3' if risk<3 else '3_5' if risk<5 else '5_8' if risk<8 else '8_plus'),'gap_'+('0' if gap==0 else '0_2' if gap<2 else '2_5' if gap<5 else '5_plus')]
        if r['liquidity_pass']:labels+=['liquid_waiting' if r['stage']=='等待啟動' else 'liquid_other']
        for c in cohorts:
            for label in labels:g[h,c,label].append(float(v))
    report=[]
    for (h,c,label),v in sorted(g.items()):
        report.append(dict(horizon=h,cohort=c,group=label,samples=len(v),mean_net_pct=round(sum(v)/len(v),4),win_rate_pct=round(100*sum(x>0 for x in v)/len(v),2),reportable=int(len(v)>=20)))
    write(ROOT/'docs/v255_hf1_cohorts.csv',report,['horizon','cohort','group','samples','mean_net_pct','win_rate_pct','reportable'])
    extreme=[r for r in out if r['extreme_loss']]
    fields=list(out[0]) if out else ['horizon','status']; fields=list(dict.fromkeys(fields+['nonoverlap']))
    write(ROOT/'docs/v255_hf1_extreme.csv',extreme,fields)
    write(ROOT/'docs/v255_hf1_trades.csv',out,fields)
    print('V2.5.5 Hotfix 1:', 'cohort_groups=',len(report),'extreme_scenarios=',len(extreme),'pending_exit=',dict(pending))

if __name__=='__main__':run()
