"""Independent research only; no changes to scanner or execution rules."""
from pathlib import Path
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parent
KEY=['market','code']

def features(d, keys):
    d=d.sort_values(keys+['date']).copy()
    g=d.groupby(keys,sort=False)['close']
    for n in (5,10,20,60):
        d[f'ma{n}']=g.transform(lambda x:x.rolling(n,min_periods=n).mean())
    for n in (5,20):
        d[f'ma{n}_slope']=d[f'ma{n}']/d.groupby(keys)[f'ma{n}'].shift(5)-1
    for n in (1,5,20): d[f'ret{n}']=g.pct_change(n,fill_method=None)
    d['vol20']=d.groupby(keys)['ret1'].transform(lambda x:x.rolling(20).std())
    d['drawdown20']=d.close/g.transform(lambda x:x.rolling(20).max())-1
    a,b,c,e=[d[f'ma{n}'] for n in (5,10,20,60)]
    cross=(a>b)&(d.groupby(keys).ma5.shift(1)<=d.groupby(keys).ma10.shift(1))
    age=d.groupby(keys).cumcount(); last=age.where(cross).groupby([d[k] for k in keys]).ffill()
    d['ma_cross_age']=age-last
    for name,cond,valid in [('MA1',a>b,b.notna()),('MA2',(a>b)&(b>c),c.notna()),('MA3',(a>b)&(b>c)&(c>e),e.notna()),('MA4',(d.ma5_slope>0)&(d.ma20_slope>0),d.ma20_slope.notna()),('MA5',(a>b)&(d.ma_cross_age<=5)&(d.ma20_slope>=-.005),d.ma20_slope.notna()),('MA6',(d.close>c)&(c>e),e.notna())]:
        d[name]=cond.astype(float).where(valid)
    d['trend']=np.select([c.isna(),d.MA2.eq(1),d.MA5.eq(1),(a<b)&(b<c)],['unknown','bull','turning','bear'],default='mixed')
    d['ma_spread_pct']=100*(a/c-1); d['close_above_ma20_pct']=100*(d.close/c-1)
    return d

def run(root=ROOT):
    root=Path(root); docs=root/'docs'; docs.mkdir(exist_ok=True)
    p=pd.read_csv(root/'data/daily.csv',dtype={'code':str}); p.date=pd.to_datetime(p.date)
    p=p.drop_duplicates(KEY+['date'],keep='last'); p['close']=pd.to_numeric(p.close,errors='coerce'); p=p[p.close>0]
    f=features(p[KEY+['date','close']],KEY)
    # Equal-weight proxy uses only consecutive market sessions, not stale stock quotes.
    cal=p[['market','date']].drop_duplicates().sort_values(['market','date']); cal['prev']=cal.groupby('market').date.shift()
    f['prev_stock']=f.groupby(KEY).date.shift(); f=f.merge(cal,on=['market','date'],how='left')
    valid=f[f.prev_stock.eq(f.prev)&f.ret1.notna()].copy(); valid['up']=valid.ret1.gt(0)
    m=valid.groupby(['market','date']).agg(proxy_ret=('ret1','mean'),breadth=('up','mean'),stocks=('up','size')).reset_index()
    m=cal[['market','date']].merge(m,on=['market','date'],how='left'); m['close']=(1+m.proxy_ret.fillna(0)).groupby(m.market).cumprod()*100
    m['benchmark']='equal_weight_proxy'
    official=root/'data/factors/market_index.csv'
    if official.exists():
        o=pd.read_csv(official); o.date=pd.to_datetime(o.date); o.close=pd.to_numeric(o.close,errors='raise')
        if o.duplicated(['market','date']).any() or (o.close<=0).any(): raise ValueError('Invalid market_index.csv')
        for market in m.market.unique():
            mask=m.market.eq(market); z=m.loc[mask,['date']].merge(o[o.market.eq(market)][['date','close']],on='date',how='left')
            if z.close.notna().all(): m.loc[mask,'close']=z.close.to_numpy(); m.loc[mask,'benchmark']='official_price_index'
    m=features(m,['market']); m['regime']=np.select([m.ma60.isna()|m.vol20.isna(),(m.ret1.abs()>=.025)|(m.vol20>=.025),(m.MA3==1)&(m.ret20>=.05),(m.close>m.ma20)&(m.ma20_slope>0),(m.close<m.ma20)&(m.ma20_slope<0)],['unknown','extreme','strong_bull','bull','bear'],default='range')
    m.to_csv(docs/'v256a_market_daily.csv',index=False,encoding='utf-8-sig')
    t=pd.read_csv(docs/'v255_hf1_trades.csv',dtype={'code':str}); total=len(t)
    for col in ['signal_date','entry_date','exit_date']: t[col]=pd.to_datetime(t[col],errors='coerce')
    # Decision cutoff: exact signal close or previous MARKET session before entry.
    t=t.merge(cal.rename(columns={'date':'entry_date','prev':'cutoff'}),on=['market','entry_date'],how='left',validate='many_to_one')
    cols=['ma5','ma10','ma20','ma60','ma5_slope','ma20_slope','ma_cross_age','ma_spread_pct','close_above_ma20_pct','trend','ret5','ret20']+[f'MA{i}' for i in range(1,7)]
    for date,prefix in [('signal_date','signal'),('cutoff','pre')]:
        z=f[KEY+['date']+cols].rename(columns={'date':date,**{c:prefix+'_'+c for c in cols}})
        t=t.merge(z,on=KEY+[date],how='left',validate='many_to_one')
        z=m[['market','date','regime','benchmark','ret5','ret20','breadth','close']].rename(columns={'date':date,**{c:prefix+'_market_'+c for c in ['regime','benchmark','ret5','ret20','breadth','close']}})
        t=t.merge(z,on=['market',date],how='left',validate='many_to_one')
        for n in (5,20): t[f'{prefix}_rs{n}']=100*(t[f'{prefix}_ret{n}']-t[f'{prefix}_market_ret{n}'])
    t=t.merge(m[['market','date','close','benchmark']].rename(columns={'date':'exit_date','close':'exit_market_close','benchmark':'exit_benchmark'}),on=['market','exit_date'],how='left',validate='many_to_one')
    t['benchmark_return_pct']=100*(t.exit_market_close/t.pre_market_close-1).where(t.exit_benchmark.eq(t.pre_market_benchmark))
    t['excess_net_pct']=t.net_return_pct-t.benchmark_return_pct
    assert len(t)==total
    t.to_csv(docs/'v256a_event_trends.csv',index=False,encoding='utf-8-sig')
    out=[]
    for cohort,mask in [('matured',t.matured.eq(1)),('fixed10',t.fixed10.eq(1)&t.horizon.le(10)),('nonoverlap_matured',t.matured.eq(1)&t.nonoverlap.eq(1))]:
      base=t[mask&t.net_return_pct.notna()]
      for scope,sub in [('ALL',base),('liquid',base[base.liquidity_pass.eq(1)]),('liquid_waiting',base[base.liquidity_pass.eq(1)&base.stage.eq('等待啟動')])]:
       for timing in ['signal','pre']:
        for label,group in [('baseline',sub.assign(label='MA0')), *[(f'MA{i}',sub.assign(label=sub[f'{timing}_MA{i}'].map({0:'no',1:'yes'}).fillna('unknown'))) for i in range(1,7)],('trend',sub.assign(label=sub[f'{timing}_trend'].fillna('unknown'))),('regime',sub.assign(label=sub[f'{timing}_market_regime'].fillna('unknown'))),('matrix',sub.assign(label=sub[f'{timing}_market_regime'].fillna('unknown')+'|'+sub[f'{timing}_trend'].fillna('unknown')))]:
         for (market,h,k),x in group.groupby(['market','horizon','label']):
          r=x.net_return_pct; loss=r[r<0]; win=r[r>0]; dates=x.signal_date.nunique()
          out.append(dict(cohort=cohort,scope=scope,timing=timing,factor=label,group=k,market=market,horizon=h,samples=len(x),signal_days=dates,mean_net_pct=r.mean(),win_rate_pct=100*r.gt(0).mean(),stop_pct=100*x.status.eq('stop').mean(),extreme_pct=100*r.le(-20).mean(),payoff_ratio=win.mean()/(-loss.mean()) if len(loss) and len(win) else np.nan,benchmark_samples=x.excess_net_pct.count(),mean_excess_pct=x.excess_net_pct.mean(),reportable=int(len(x)>=30 and dates>=5)))
    pd.DataFrame(out).to_csv(docs/'v256a_performance.csv',index=False,encoding='utf-8-sig')
    # Signal-level trigger rates: never condition on future entry features.
    s=t[t.horizon.eq(1)].copy(); s['entered']=s.entry_date.notna(); s['failed']=s.status.eq('failed_before_entry'); s['resolved']=s.entered|s.failed
    a=s.groupby(['market','signal_trend','signal_market_regime'],dropna=False).agg(events=('code','size'),entered=('entered','sum'),failed=('failed','sum'),resolved=('resolved','sum')).reset_index(); a['trigger_resolved_pct']=100*a.entered/a.resolved.replace(0,np.nan); a.to_csv(docs/'v256a_signal_audit.csv',index=False,encoding='utf-8-sig')
    print(f'V2.5.6A: scenarios={total}; signal_ma20={t.signal_ma20.notna().sum()}; pre_ma20={t.pre_ma20.notna().sum()}; groups={len(out)}; market_days={len(m)}; benchmark={m.groupby("market").benchmark.first().to_dict()}')
if __name__=='__main__': run()
