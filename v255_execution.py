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
            out.append(r);audit[f'h{h}_{status}']+=1
            # Use fully matured entry cohorts, including early stops, to avoid censoring bias.
            entry_i=next((i for i,x in enumerate(series[k][pos+1:pos+21]) if x['high']>=t),None)
            mature=entry_i is not None and len(series[k])-pos-2-entry_i>=h
            if net is not None and mature:
                for g in ['ALL','type_'+e['type']]+(['liquid'] if liquid else [])+(['liquid_waiting'] if liquid and stage=='等待啟動' else []):groups[h,g].append(net)
    report=[]
    for (h,g),v in sorted(groups.items()):
        n=len(v);report.append(dict(horizon=h,group=g,samples=n,mean_net_pct=round(sum(v)/n,4),win_rate_pct=round(100*sum(x>0 for x in v)/n,2),reportable=int(n>=20)))
    write(ROOT/'docs/v255_trades.csv',out,['signal_date','market','code','type','stage','low_base_score','liquidity_pass','horizon','status','entry','exit','held','net_return_pct'])
    write(ROOT/'docs/v255_performance.csv',report,['horizon','group','samples','mean_net_pct','win_rate_pct','reportable'])
    write(ROOT/'docs/v255_audit.csv',[dict(check=k,value=v) for k,v in sorted(audit.items())],['check','value'])
    print('V2.5.5 execution:',len(events),'events;',len(out),'scenario rows;',dict(audit))
if __name__=='__main__':run()
