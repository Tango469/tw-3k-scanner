"""Independent risk experiments, daily OHLC, conservative stop-first, T+1."""
from pathlib import Path
import pandas as pd
import numpy as np
from collections import defaultdict
ROOT=Path(__file__).resolve().parent
H=(1,3,5,10,20)
POLICIES={'BASE':{}, **{f'RISK{x}':{'risk':x/100} for x in (2,3,4,5,7)}, **{f'GAP{x}':{'gap':x/100} for x in (1,2,3)}, **{f'TP{x}R':{'target':x} for x in (1,1.5,2,3)}, 'BREAKEVEN':{'trail':'be'}, 'TRAIL1R':{'trail':'trail'}, 'CLOSE_CONFIRM':{'confirm':True}, 'NO_EXTREME':{'exclude':('extreme','unknown')}, 'NO_BEAR_EXTREME':{'exclude':('bear','extreme','unknown')}}
POLICIES.update({f'{family}{i}':dict(model=family, **({'exclude':('bear','extreme','unknown')} if family=='A' else {}), **({'risk':.03} if i in (2,4) else {'risk':.04} if i==3 else {}), **({'target':1} if i==4 else {})) for family in ('A','B') for i in range(1,5)})
FREEZE='2026-11-18'
ZERO={'failed_before_entry','expired','risk_reject','gap_reject','regime_reject','model_reject'}

def entry_plan(bars,t,f,p,regimes):
    for i,(d,o,hi,lo,c) in enumerate(bars[:20]):
        if hi>=t and lo<=f:return 'ambiguous_entry',None,None
        if lo<=f:return 'failed_before_entry',None,None
        if (c>=t if p.get('confirm') else hi>=t):
            if p.get('confirm'):
                i+=1
                if i>=len(bars):return 'pending_entry',None,None
                d,o,hi,lo,c=bars[i]; price=o
                if price<=f:return 'failed_before_entry',None,None
            else:price=max(t,o)
            if 1-f/price>p.get('risk',float('inf'))+1e-10:return 'risk_reject',None,None
            if price/t-1>p.get('gap',float('inf'))+1e-10:return 'gap_reject',None,None
            if p.get('exclude') and regimes.get(d,'unknown')=='unknown':return 'data_missing',None,None
            if p.get('exclude') and regimes.get(d) in p['exclude']:return 'regime_reject',None,None
            return 'entered',i,price
    return ('expired' if len(bars)>=20 else 'pending_entry'),None,None

def exit_plan(bars,i,entry,f,h,p):
    risk=entry-f; stop=f; target=entry+p.get('target',float('inf'))*risk; peak=entry
    for j,(d,o,hi,lo,c) in enumerate(bars[i+1:i+h+1],1):
        if o<=stop:return 'stop',o,j
        if o>=target:return 'target',o,j
        if lo<=stop:return 'stop',stop,j # stop first if both touched
        if hi>=target:return 'target',target,j
        if j==h:return 'time_exit',c,j
        peak=max(peak,hi)
        if peak>=entry+risk and p.get('trail'):
            stop=max(stop,entry if p['trail']=='be' else peak-risk)
    return 'pending_exit',None,None

def run(root=ROOT):
    root=Path(root); docs=root/'docs'
    cols=['market','code','date','open','high','low','close']; d=pd.read_csv(root/'data/daily.csv',usecols=cols,dtype={'code':str})
    d=d.drop_duplicates(['market','code','date'],keep='last').sort_values(['market','code','date'])
    series={k:list(x[['date','open','high','low','close']].itertuples(index=False,name=None)) for k,x in d.groupby(['market','code'])}
    idx={k:{b[0]:i for i,b in enumerate(v)} for k,v in series.items()}
    trend=pd.read_csv(docs/'v256a_event_trends.csv',dtype={'code':str}); e=trend[trend.horizon.eq(1)].copy()
    m=pd.read_csv(docs/'v256a_market_daily.csv').sort_values(['market','date']);m['previous']=m.groupby('market').regime.shift(); regimes={k:dict(zip(x.date,x.previous)) for k,x in m.groupby('market')}
    out=[]
    for n,r in enumerate(e.itertuples(index=False)):
        k=(r.market,r.code); pos=idx.get(k,{}).get(r.signal_date)
        if pos is None or not 0<r.fail<r.trigger:continue
        bars=series[k][pos+1:pos+42]; t=r.trigger; f=r.fail
        for policy,p in POLICIES.items():
            model=p.get('model')
            if model and (pd.isna(r.liquidity_pass) or (model=='A' and pd.isna(r.signal_MA2)) or (model=='B' and (pd.isna(r.stage) or pd.isna(r.signal_MA5)))):
                status,i,entry='data_missing',None,None
            elif model and not (r.liquidity_pass==1 and (r.signal_MA2==1 if model=='A' else r.stage=='等待啟動' and r.signal_MA5==1)):
                status,i,entry='model_reject',None,None
            else:status,i,entry=entry_plan(bars,t,f,p,regimes.get(r.market,{}))
            for h in H:
                mature=i is not None and len(bars)-i-1>=h
                s,price,j=exit_plan(bars,i,entry,f,h,p) if i is not None else (status,None,None)
                net=100*(price*(1-.001425-.003)/(entry*(1+.001425))-1) if price is not None else np.nan
                out.append((n,r.market,r.code,r.signal_date,policy,h,s,bars[i][0] if i is not None else '',bars[i+j][0] if j is not None else '',entry,price,net,mature,len(bars)>=21+h,r.liquidity_pass,r.stage,r.signal_MA2,r.signal_MA5,r.signal_market_regime))
    a=pd.DataFrame(out,columns='event_id market code signal_date policy horizon status entry_date exit_date entry exit net_return_pct matured common_window liquid stage ma2 ma5 regime'.split())
    # Keep the strict fixed window, and separately expose resolved paired cohorts.
    a['fixed_window']=a.common_window
    a['resolved']=a.net_return_pct.notna() | a.status.isin(ZERO)
    a['contribution_pct']=a.net_return_pct.where(~a.status.isin(ZERO),0.)
    a['sample_period']=np.where(a.signal_date.gt(FREEZE),'forward','development')
    keys=['event_id','horizon']
    a['common_window']=a.groupby(keys).resolved.transform('all') & a.groupby(keys).policy.transform('size').eq(len(POLICIES))
    paired_report(a,docs)

    a['nonoverlap']=False
    busy={}
    accepted=[]
    for r in a[a.entry_date.ne('')].sort_values(['entry_date','signal_date','event_id']).itertuples():
        k=(r.policy,r.horizon,r.market,r.code)
        if r.entry_date>busy.get(k,''):
            accepted.append(r.Index);busy[k]=r.exit_date or '9999-12-31'
    a.loc[accepted,'nonoverlap']=True
    a.to_csv(docs/'v256b_trades.csv.gz',index=False,compression={'method':'gzip','mtime':0})
    report=[]
    for cohort,base in [('matured',a[a.matured]),('common_window',a[a.common_window]),('nonoverlap_matured',a[a.matured&a.nonoverlap])]:
      for scope,x in [('ALL',base),('liquid',base[base.liquid.eq(1)]),('right_side',base[base.liquid.eq(1)&base.ma2.eq(1)]),('low_turn',base[base.liquid.eq(1)&base.stage.eq('等待啟動')&base.ma5.eq(1)])]:
       for (market,policy,h),g in x.groupby(['market','policy','horizon']):
        v=g.net_return_pct.dropna(); win=v[v>0]; loss=v[v<0]; days=g.loc[g.net_return_pct.notna(),'signal_date'].nunique()
        report.append(dict(cohort=cohort,scope=scope,market=market,policy=policy,horizon=h,events=len(g),samples=len(v),signal_days=days,mean_net_pct=v.mean(),win_pct=100*(v>0).mean(),stop_pct=100*g.status.eq('stop').sum()/len(v) if len(v) else np.nan,payoff=win.mean()/(-loss.mean()) if len(win) and len(loss) else np.nan,extreme_pct=100*(v<=-20).mean(),net_per_signal_pct=g.contribution_pct.mean() if cohort=='common_window' else np.nan,reportable=int(len(v)>=30 and days>=5)))
    pd.DataFrame(report).to_csv(docs/'v256b_performance.csv',index=False)
    a.groupby(['market','policy','horizon','status']).size().rename('count').reset_index().to_csv(docs/'v256b_audit.csv',index=False)
    print(f'V2.5.6B HF1: events={len(e)}; policies={len(POLICIES)}; scenarios={len(a)}; groups={len(report)}; common_window={a.common_window.sum()}')
def paired_report(a,docs):
    keys=['event_id','horizon']
    base=a[a.policy.eq('BASE')][keys+['resolved','contribution_pct','fixed_window','net_return_pct']].rename(columns={'resolved':'base_resolved','contribution_pct':'base_pct','fixed_window':'base_fixed','net_return_pct':'base_trade_return'})
    rows=[]
    for policy,x in a.groupby('policy',sort=False):
        x=x.merge(base,on=keys,validate='one_to_one')
        for cohort,mask in [('paired_resolved',x.resolved & x.base_resolved),('paired_fixed',x.resolved & x.base_resolved & x.fixed_window & x.base_fixed),('all_policy_resolved',x.common_window)]:
            for (market,h,period),g in x[mask].groupby(['market','horizon','sample_period']):
                delta=g.contribution_pct-g.base_pct
                traded=g.net_return_pct.notna(); days=g.signal_date.nunique()
                rows.append(dict(policy=policy,cohort=cohort,market=market,horizon=h,sample_period=period,signals=len(g),signal_days=days,trades=int(traded.sum()),retention_pct=100*traded.mean(),base_trades=int(g.base_trade_return.notna().sum()),no_trade=int(g.status.isin(ZERO).sum()),mean_per_signal_pct=g.contribution_pct.mean(),base_per_signal_pct=g.base_pct.mean(),delta_pct=delta.mean(),mean_trade_pct=g.net_return_pct.mean(),reportable=int(len(g)>=30 and days>=5 and traded.sum()>=30)))
    pd.DataFrame(rows).to_csv(docs/'v256b_hf1_paired.csv',index=False)
    a.groupby(['sample_period','market','policy','horizon','status']).size().rename('count').reset_index().to_csv(docs/'v256b_hf1_status.csv',index=False)

if __name__=='__main__':run()
