"""V2.5.4 additive, point-in-time institutional factor research.

Does not alter V2.4.1 signal generation, trigger/fail, or prior reports.
Research anchor: BEFORE trigger session begins (previously published data only).
Returns in trigger_tracking are descriptive forward returns from trigger-day close,
not executable trigger-price returns. No claim of predictive validity.
"""
import csv
import datetime as dt
import math
from collections import defaultdict, Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data' / 'factors'
DOC = ROOT / 'docs'
TZ = dt.timezone(dt.timedelta(hours=8))
RAW_FACTORS = ('foreign_net_shares', 'trust_net_shares', 'dealer_net_shares')
METRICS = ('foreign_net_1d_shares', 'trust_net_1d_shares', 'dealer_net_1d_shares',
           'institutional_net_1d_shares', 'institutional_net_5d_shares',
           'institutional_net_5d_volume_ratio', 'foreign_consecutive_buy_days',
           'trust_consecutive_buy_days', 'market_advance_ratio', 'market_return_5d_pct')
HORIZONS = (1, 3, 5, 10, 20)
MIN_SAMPLES = 20


def read_csv(path):
    if not path.exists():
        return []
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def write_csv(path, records, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(records)


def finite(v):
    try:
        n = float(str(v).replace(',', '').strip())
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def parse_at(v):
    try:
        t = dt.datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        return t.astimezone(TZ) if t.tzinfo else None
    except (ValueError, TypeError):
        return None


def parse_day(v):
    try:
        return dt.date.fromisoformat(str(v))
    except (ValueError, TypeError):
        return None


def build(rows, observations):
    """Return stock/date features; require 5 actual consecutive market sessions.

    A five-session factor is unavailable if any session has missing flows or
    volume; no zero-fill, no implicit extrapolation from one TPEx session.
    """
    market_sessions = defaultdict(set)
    volume = {}
    for r in rows:
        m, c, day = r.get('market'), str(r.get('code', '')).strip(), r.get('date')
        v = finite(r.get('volume'))
        if m in ('TWSE', 'TPEX') and parse_day(day):
            market_sessions[m].add(day)
            if c.isdigit() and len(c) == 4 and v is not None and v > 0:
                volume[(m, c, day)] = v
    sessions = {m: sorted(ds) for m, ds in market_sessions.items()}
    raw = defaultdict(dict)
    common = defaultdict(dict)
    counters = Counter()
    for r in observations:
        m, c, f = r.get('market'), str(r.get('code', '')).strip(), r.get('factor')
        day = r.get('observation_date')
        val, at = finite(r.get('value')), parse_at(r.get('available_at'))
        observed = parse_day(day)
        if not (m in sessions and observed and val is not None and at and at.date() >= observed):
            counters['invalid_observation'] += 1
            continue
        entry = (val, at)
        if c == 'ALL' and f in ('market_advance_ratio', 'market_return_5d_pct'):
            common[(m, day)][f] = entry
        elif len(c) == 4 and c.isdigit() and f in RAW_FACTORS:
            raw[(m, c, day)][f] = entry
    features = {}
    for (m, c, day), factors in raw.items():
        feat = {}
        # All raw daily fields must exist and have availability timestamps.
        if all(f in factors for f in RAW_FACTORS):
            foreign, trust, dealer = (factors[f][0] for f in RAW_FACTORS)
            feat.update(foreign_net_1d_shares=foreign, trust_net_1d_shares=trust,
                        dealer_net_1d_shares=dealer,
                        institutional_net_1d_shares=foreign + trust + dealer)
            dates = sessions.get(m, [])
            try:
                idx = dates.index(day)
            except ValueError:
                idx = -1
            if idx >= 4:
                last5 = dates[idx-4:idx+1]
                flow_days = [raw.get((m, c, d), {}) for d in last5]
                vols = [volume.get((m, c, d)) for d in last5]
                if all(all(f in d for f in RAW_FACTORS) for d in flow_days):
                    five = sum(sum(d[f][0] for f in RAW_FACTORS) for d in flow_days)
                    feat['institutional_net_5d_shares'] = five
                    if all(v is not None for v in vols) and sum(vols) > 0:
                        feat['institutional_net_5d_volume_ratio'] = five / sum(vols)
                    else:
                        counters['five_day_missing_volume'] += 1
                    for f, name in [('foreign_net_shares', 'foreign_consecutive_buy_days'),
                                    ('trust_net_shares', 'trust_consecutive_buy_days')]:
                        count = 0
                        for d in reversed(flow_days):
                            if d[f][0] > 0:
                                count += 1
                            else:
                                break
                        feat[name] = count
                else:
                    counters['five_day_missing_flows'] += 1
            else:
                counters['five_day_insufficient_sessions'] += 1
        else:
            counters['missing_daily_institutional_components'] += 1
        if feat:
            # Point-in-time for 5-day fields is latest availability of five sessions.
            # Individual factors are exposed only after their actual component data.
            daily_at = max(factors[f][1] for f in RAW_FACTORS if f in factors)
            feat_at = {k: daily_at for k in feat}
            if 'institutional_net_5d_shares' in feat:
                last5 = sessions[m][sessions[m].index(day)-4:sessions[m].index(day)+1]
                five_at = max(raw[(m,c,d)][f][1] for d in last5 for f in RAW_FACTORS)
                for key in ('institutional_net_5d_shares', 'institutional_net_5d_volume_ratio',
                            'foreign_consecutive_buy_days', 'trust_consecutive_buy_days'):
                    if key in feat:
                        feat_at[key] = five_at
            features[(m, c, day)] = {k: (v, feat_at[k]) for k, v in feat.items()}
    return features, common, counters


def run(rows=None):
    if rows is None:
        # Standalone mode for local verification.
        rows = read_csv(ROOT / 'data' / 'daily.csv')
    observations = read_csv(DATA / 'factor_observations.csv')
    events = read_csv(DOC / 'trigger_tracking.csv')
    features, common, audit = build(rows, observations)
    by_stock = defaultdict(list)
    for (m,c,d), factors in features.items():
        by_stock[(m,c)].append((d, factors))
    for items in by_stock.values():
        items.sort(key=lambda x: x[0])
    by_market = defaultdict(list)
    for (m,d), factors in common.items():
        by_market[m].append((d, factors))
    for items in by_market.values():
        items.sort(key=lambda x: x[0])

    event_out = []
    for r in events:
        event = dict(r)
        trigger_day = parse_day(r.get('event_date'))
        valid = r.get('status') == '已觸及Trigger_待驗證' and trigger_day is not None
        # Strictly before trigger-day 00:00 Taiwan time: after-close prior day is valid.
        cutoff = dt.datetime.combine(trigger_day, dt.time.min, TZ) if valid else None
        event['v254_decision'] = 'pre_trigger_session' if valid else 'not_eligible'
        event['v254_cutoff'] = cutoff.isoformat() if cutoff else ''
        event['v254_factor_count'] = 0
        for name in METRICS:
            event[name] = ''
            event[name+'_available_at'] = ''
        if cutoff:
            m, c = r.get('market'), str(r.get('code', '')).strip()
            # Select latest available observation per metric, not simply latest date.
            for name in METRICS:
                options = by_market.get(m, []) if name.startswith('market_') else by_stock.get((m,c), [])
                candidates = [(d, val, at) for d, fs in options for k, (val, at) in fs.items()
                              if k == name and at < cutoff and parse_day(d) < trigger_day]
                if candidates:
                    d, val, at = max(candidates, key=lambda x: (x[0], x[2]))
                    event[name] = val
                    event[name+'_available_at'] = at.isoformat()
                    event['v254_factor_count'] += 1
        event_out.append(event)
    base = list(events[0]) if events else ['signal_date','market','code','type','status','event_date','ret_1']
    extras = ['v254_decision','v254_cutoff','v254_factor_count'] + [x for name in METRICS for x in (name,name+'_available_at')]
    write_csv(DOC/'v254_event_factors.csv', event_out, base+extras)

    performance = []
    for name in METRICS:
        for horizon in HORIZONS:
            for group in ('positive', 'nonpositive'):
                chosen = []
                for e in event_out:
                    if e['v254_decision'] != 'pre_trigger_session':
                        continue
                    factor, ret = finite(e.get(name)), finite(e.get('ret_'+str(horizon)))
                    if factor is None or ret is None or (factor > 0) != (group == 'positive'):
                        continue
                    chosen.append((e,ret))
                n = len(chosen)
                performance.append({'factor':name,'horizon':horizon,'group':group,
                    'samples':n,'min_samples':MIN_SAMPLES,'reportable':int(n >= MIN_SAMPLES),
                    'win_rate_pct':round(100*sum(v>0 for _,v in chosen)/n,2) if n >= MIN_SAMPLES else '',
                    'mean_return_pct':round(sum(v for _,v in chosen)/n,3) if n >= MIN_SAMPLES else '',
                    'note':'descriptive_only; pre_trigger_data; trigger_close_return; correlated_signals_possible'
                           if n >= MIN_SAMPLES else 'insufficient_samples'})
    write_csv(DOC/'v254_factor_performance.csv',performance,
              ['factor','horizon','group','samples','min_samples','reportable','win_rate_pct','mean_return_pct','note'])
    triggered = [e for e in event_out if e['v254_decision'] == 'pre_trigger_session']
    checks = [('events',len(events)),('unambiguous_triggered',len(triggered)),
              ('with_any_factor',sum(e['v254_factor_count']>0 for e in triggered)),
              ('with_institutional_1d',sum(finite(e['institutional_net_1d_shares']) is not None for e in triggered)),
              ('with_institutional_5d',sum(finite(e['institutional_net_5d_shares']) is not None for e in triggered)),
              ('reportable_groups',sum(p['reportable'] for p in performance)),
              ('feature_stock_dates',len(features))]
    for h in HORIZONS:
        completed = [e for e in triggered if finite(e.get('ret_'+str(h))) is not None]
        checks.extend([(f'h{h}_completed',len(completed)),
                       (f'h{h}_completed_and_any_factor',sum(e['v254_factor_count']>0 for e in completed)),
                       (f'h{h}_completed_and_5d_institutional',sum(finite(e['institutional_net_5d_shares']) is not None for e in completed))])
    checks.extend(('build_'+k,v) for k,v in sorted(audit.items()))
    write_csv(DOC/'v254_quality_audit.csv', [{'check':k,'value':v} for k,v in checks], ['check','value'])
    print('V2.5.4 research: events={}; triggered={}; any_factor={}; inst_1d={}; inst_5d={}; reportable_groups={}; feature_stock_dates={}'.format(
        len(events),len(triggered),checks[2][1],checks[3][1],checks[4][1],checks[5][1],len(features)))
    print('V2.5.4 funnel: '+', '.join(f'h{h}='+str(next(v for k,v in checks if k==f'h{h}_completed_and_any_factor')) for h in HORIZONS))
    if checks[4][1] == 0:
        print('V2.5.4 NOTE: no eligible five-session institutional history at trigger cutoff; do not infer factor effectiveness.')

if __name__ == '__main__':
    run()
