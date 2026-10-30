"""V2.5.3 point-in-time research, additive only; never changes trade signals.

Research decision is NEXT SESSION after signal day, using observations available
by 23:59:59 Asia/Taipei on signal day. No same-day intraday inference.
"""
import csv
import datetime as dt
import math
from collections import Counter, defaultdict
from pathlib import Path

ROOT=Path(__file__).resolve().parent
DOC=ROOT/'docs'
DATA=ROOT/'data'/'factors'
TZ=dt.timezone(dt.timedelta(hours=8))
FACTOR_NAMES=('institutional_net_5d_ratio','market_advance_ratio','market_return_5d_pct','sector_return_5d_pct','us_proxy_return_pct','revenue_yoy_pct','news_catalyst')


def read(path):
    if not path.exists(): return []
    with path.open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def write(path,rows,columns):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore');w.writeheader();w.writerows(rows)


def number(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError):return None


def instant(v):
    if not v:return None
    try:
        t=dt.datetime.fromisoformat(v.strip().replace('Z','+00:00'))
        return t.astimezone(TZ) if t.tzinfo else None
    except (ValueError,TypeError):return None


def cutoff(day):
    try:return dt.datetime.combine(dt.date.fromisoformat(day),dt.time(23,59,59),TZ)
    except (TypeError,ValueError):return None


def run():
    events=read(DOC/'trigger_tracking.csv')
    raw=read(DATA/'factor_observations.csv')
    snaps=read(DATA/'signal_snapshots.csv')
    # Use only dated observations, with explicit, valid, timezone-aware availability.
    index=defaultdict(list)
    invalid=Counter()
    for r in raw:
        market=r.get('market','').strip();code=r.get('code','').strip();factor=r.get('factor','').strip()
        day=r.get('observation_date','').strip();at=instant(r.get('available_at',''))
        try: observation_day=dt.date.fromisoformat(day)
        except ValueError: invalid['bad_observation_date']+=1;continue
        if not at:invalid['missing_or_invalid_available_at']+=1;continue
        if at.date()<observation_day:invalid['available_before_observation']+=1;continue
        if number(r.get('value')) is None:invalid['non_numeric']+=1;continue
        if not market or not code or not factor:invalid['missing_key']+=1;continue
        index[(market,code,factor)].append((at,observation_day,number(r['value']),r.get('source','')))
    for values in index.values():values.sort(key=lambda x:(x[0],x[1]))
    snap_index={(r.get('date'),r.get('market'),r.get('code'),r.get('type')):r for r in snaps}
    output=[];reason=Counter();eligible_events=0
    for event in events:
        r=dict(event);day=r.get('signal_date','');end=cutoff(day)
        snap=snap_index.get((day,r.get('market'),r.get('code'),r.get('type')), {})
        r['research_decision']='next_session_after_signal'
        r['research_cutoff']=end.isoformat() if end else ''
        r['snapshot_found']=int(bool(snap))
        r['low_base_score_at_signal']=snap.get('low_base_score','')
        r['low_base_stage_at_signal']=snap.get('low_base_stage','')
        count=0
        for factor in FACTOR_NAMES:
            r[factor]='';r[factor+'_available_at']='';r[factor+'_observation_date']=''
            if not end:continue
            for key in ((r.get('market'),r.get('code'),factor),(r.get('market'),'ALL',factor),('ALL','ALL',factor)):
                matches=[x for x in index.get(key,()) if x[0]<=end and x[1]<=end.date()]
                if matches:
                    at,obs,val,source=matches[-1]
                    r[factor]=val;r[factor+'_available_at']=at.isoformat();r[factor+'_observation_date']=obs.isoformat()
                    count+=1;break
        r['factor_count']=count
        triggered=r.get('status')=='已觸及Trigger_待驗證' and bool(r.get('event_date'))
        # Some statuses describe ambiguity; do not infer intraday ordering.
        r['valid_trigger_event']=int(triggered)
        r['exclusion_reason']='' if triggered else 'not_unambiguously_triggered'
        if triggered:eligible_events+=1
        else:reason['not_unambiguously_triggered']+=1
        output.append(r)
    fields=list(events[0]) if events else ['signal_date','market','code','type','status','event_date','ret_1','ret_3','ret_5']
    extra=['research_decision','research_cutoff','snapshot_found','low_base_score_at_signal','low_base_stage_at_signal','valid_trigger_event','exclusion_reason','factor_count']
    extra += [x for f in FACTOR_NAMES for x in (f,f+'_available_at',f+'_observation_date')]
    write(DOC/'v253_event_research.csv',output,fields+extra)
    # Group comparisons are descriptive, never predictive evidence by themselves.
    groups=[];minimum=20
    for factor in FACTOR_NAMES:
        for horizon in (1,3,5,10,20):
            col=f'ret_{horizon}'
            for sign in ('positive','nonpositive'):
                selected=[];unique=set()
                for r in output:
                    if not r['valid_trigger_event']:continue
                    f=number(r[factor]);ret=number(r.get(col))
                    if f is None or ret is None:continue
                    if (f>0)!=(sign=='positive'):continue
                    selected.append(ret);unique.add((r.get('signal_date'),r.get('market'),r.get('code'),r.get('type')))
                n=len(selected)
                # trigger_tracking ret_N values are already percentages (e.g. -1.16 means -1.16%).
                groups.append({'factor':factor,'horizon':horizon,'group':sign,'samples':n,
                               'distinct_signals':len(unique),'min_samples':minimum,
                               'reportable':int(n>=minimum),
                               'win_rate_pct':round(100*sum(x>0 for x in selected)/n,2) if n>=minimum else '',
                               'mean_return_pct':round(sum(selected)/n,3) if n>=minimum else '',
                               'note':'descriptive_only; signal-day known factors; next-session decision' if n>=minimum else 'insufficient_sample'})
    write(DOC/'v253_factor_performance.csv',groups,['factor','horizon','group','samples','distinct_signals','min_samples','reportable','win_rate_pct','mean_return_pct','note'])
    # Diagnostic funnel: count eligible events at each step without weakening
    # the 20-observation reporting threshold or inventing forward returns.
    funnel=[]
    for horizon in (1,3,5,10,20):
        ret_key=f'ret_{horizon}'
        triggered_rows=[r for r in output if r['valid_trigger_event']]
        matured=[r for r in triggered_rows if number(r.get(ret_key)) is not None]
        factor_ready=[r for r in matured if r['factor_count']>0]
        funnel.extend([
            {'check':f'h{horizon}_triggered','value':len(triggered_rows)},
            {'check':f'h{horizon}_completed_return','value':len(matured)},
            {'check':f'h{horizon}_completed_and_any_factor','value':len(factor_ready)},
        ])
        for factor in FACTOR_NAMES:
            matched=[r for r in matured if number(r.get(factor)) is not None]
            funnel.append({'check':f'h{horizon}_{factor}_completed','value':len(matched)})
    market_counts=Counter((r.get('market'),r.get('factor')) for r in raw)
    checks=[{'check':'events','value':len(events)},{'check':'raw_observations','value':len(raw)},
            {'check':'valid_factor_records','value':sum(map(len,index.values()))},
            {'check':'event_factor_matches','value':sum(r['factor_count'] for r in output)},
            {'check':'snapshot_matches','value':sum(r['snapshot_found'] for r in output)},
            {'check':'unambiguous_trigger_events','value':eligible_events},
            {'check':'reportable_groups','value':sum(x['reportable'] for x in groups)},
            {'check':'tpex_institutional_raw_rows','value':sum(n for (m,f),n in market_counts.items() if m=='TPEX' and f in ('foreign_net_shares','trust_net_shares','dealer_net_shares'))}]
    checks.extend(funnel)
    checks.extend({'check':'invalid_'+k,'value':v} for k,v in sorted(invalid.items()))
    write(DOC/'v253_quality_audit.csv',checks,['check','value'])
    print('V2.5.3 research: events={}; factor_matches={}; snapshots={}; triggered={}; reportable_groups={}; TPEx_institutional_rows={}'.format(len(events),sum(r['factor_count'] for r in output),sum(r['snapshot_found'] for r in output),eligible_events,sum(x['reportable'] for x in groups),checks[7]['value']))
    print('V2.5.3 patch funnel: '+', '.join(f'h{h}='+str(next(x['value'] for x in funnel if x['check']==f'h{h}_completed_and_any_factor')) for h in (1,3,5,10,20)))
    if not checks[7]['value']:print('V2.5.3 WARN: TPEx institutional coverage missing; do not compare TWSE and TPEx institutional factors.')

if __name__=='__main__':run()
