"""V2.5 independent point-in-time multi-factor research. Stdlib only.

Optional factor data are USER-PROVIDED, never silently inferred. A factor observation
is eligible only when available_at <= signal_date 23:59:59 Asia/Taipei.
For intraday strategies replace this cutoff with actual decision timestamp.
"""
import csv
import datetime as dt
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INPUT = ROOT / 'data' / 'factors'
OUTPUT = ROOT / 'docs'
FACTORS = ['institutional_net_5d_ratio', 'market_return_5d_pct', 'sector_return_5d_pct',
           'us_proxy_return_pct', 'revenue_yoy_pct', 'news_catalyst']


def load_csv(path):
    if not path.exists():
        return []
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def write_csv(path, records, cols):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        w.writerows(records)


def timestamp(value):
    if not value:
        return None
    try:
        value = value.strip().replace('Z', '+00:00')
        if len(value) == 10:
            value += 'T23:59:59+08:00'
        d = dt.datetime.fromisoformat(value)
        if d.tzinfo is None:
            raise ValueError('timezone required')
        return d.astimezone(dt.timezone(dt.timedelta(hours=8)))
    except (ValueError, TypeError):
        return None


def numeric(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def run():
    # Persist today's complete signal features before evaluating events. Historical
    # values are NEVER reconstructed from the current latest-only low_base file.
    current = load_csv(OUTPUT/'low_base.csv')
    snapshot_path = INPUT/'signal_snapshots.csv'
    existing_snapshots = load_csv(snapshot_path)
    snapshot_cols = ['date','market','code','type','low_base_score','low_base_stage',
                     'price_position_120','range_20_pct','volume_ratio_5_20',
                     'prior_5_20_volume_ratio','breakout_gap_pct']
    snapshot_map = {(r.get('date'),r.get('market'),r.get('code'),r.get('type')): r
                    for r in existing_snapshots}
    for r in current:
        key=(r.get('date'),r.get('market'),r.get('code'),r.get('type'))
        if all(key):
            snapshot_map[key] = {k:r.get(k,'') for k in snapshot_cols}
    if current:
        write_csv(snapshot_path, [snapshot_map[k] for k in sorted(snapshot_map)], snapshot_cols)
        print(f'V2.5 snapshots: current={len(current)}; cumulative={len(snapshot_map)}')
    # Support the two human-readable templates without silently assigning a
    # publication time: each populated row MUST include available_at.
    manual = load_csv(INPUT/'factor_observations.csv')
    for r in load_csv(INPUT/'institutional.csv'):
        if not r.get('available_at') or not r.get('date') or not r.get('code'):
            continue
        market=r.get('market','').strip()
        if market not in ('TWSE','TPEX'):
            continue
        vals=[numeric(r.get(k)) for k in ('foreign_net_buy','investment_trust_net_buy','dealer_net_buy')]
        volume=numeric(r.get('volume_5d'))
        if any(v is None for v in vals) or volume is None or volume<=0:
            continue
        manual.append({'market':market,'code':r['code'].strip(),
                       'factor':'institutional_net_5d_ratio','value':str(sum(vals)/volume),
                       'observation_date':r['date'],'available_at':r['available_at'],
                       'source':'institutional.csv'})
    for r in load_csv(INPUT/'market_context.csv'):
        if not r.get('available_at') or not r.get('date'):
            continue
        market=r.get('market','').strip()
        if market not in ('TWSE','TPEX','ALL'):
            continue
        for col, factor in [('market_return_5d','market_return_5d_pct'),
                            ('industry_return_5d','sector_return_5d_pct'),
                            ('us_return_1d','us_proxy_return_pct')]:
            if numeric(r.get(col)) is None:
                continue
            # Industry and US proxy values require a stock code for precise matching.
            code=(r.get('code') or '').strip() if factor != 'market_return_5d_pct' else 'ALL'
            if not code:
                continue
            manual.append({'market':market,'code':code,'factor':factor,
                           'value':r[col],'observation_date':r['date'],
                           'available_at':r['available_at'],'source':'market_context.csv'})
    events = load_csv(OUTPUT/'trigger_tracking.csv')
    factors = manual
    # Explicit event snapshots from previous signal days, not latest-only low_base.csv.
    snapshots = load_csv(INPUT/'signal_snapshots.csv')
    index = defaultdict(list)
    for row in factors:
        at = timestamp(row.get('available_at'))
        obs = timestamp(row.get('observation_date'))
        if at is None or obs is None or at < obs:
            continue
        key = (row.get('market', '').strip(), row.get('code', '').strip(), row.get('factor', '').strip())
        if key[2] not in FACTORS:
            continue
        index[key].append((at, obs, row.get('value', ''), row.get('source', '')))
    for key in index:
        index[key].sort(key=lambda t: (t[0], t[1]))
    snapshot_index = {(r.get('date'), r.get('market'), r.get('code'), r.get('type')): r for r in snapshots}
    results = []
    for event in events:
        r = dict(event)
        signal_day = r.get('signal_date', '')
        cutoff = timestamp(signal_day)
        snapshot = snapshot_index.get((signal_day, r.get('market'), r.get('code'), r.get('type')), {})
        for k in ('low_base_score', 'low_base_stage', 'price_position_120', 'range_20_pct',
                  'volume_ratio_5_20', 'prior_5_20_volume_ratio', 'breakout_gap_pct'):
            r[k] = snapshot.get(k, '')
        for factor in FACTORS:
            r[factor] = ''
            r[factor + '_available_at'] = ''
            r[factor + '_source'] = ''
            if cutoff is None:
                continue
            # Market-wide factors use market/code='ALL'; sector proxy is stock-specific.
            keys = [(r.get('market', ''), r.get('code', ''), factor),
                    (r.get('market', ''), 'ALL', factor), ('ALL', 'ALL', factor)]
            for key in keys:
                eligible = [v for v in index.get(key, []) if v[0] <= cutoff]
                if eligible:
                    at, obs, val, source = eligible[-1]
                    r[factor] = val
                    r[factor + '_available_at'] = at.isoformat()
                    r[factor + '_source'] = source
                    break
        r['factor_count'] = sum(bool(r[k]) for k in FACTORS)
        results.append(r)
    base_cols = list(events[0]) if events else ['signal_date', 'market', 'code', 'name', 'type', 'status', 'ret_1', 'ret_3']
    extra = ['low_base_score', 'low_base_stage', 'price_position_120', 'range_20_pct',
             'volume_ratio_5_20', 'prior_5_20_volume_ratio', 'breakout_gap_pct', 'factor_count']
    extra += [col for k in FACTORS for col in (k, k+'_available_at', k+'_source')]
    write_csv(OUTPUT/'v25_event_factors.csv', results, base_cols + extra)
    # Exploratory factor contrast: only completed returns, never treat missing as zero.
    stats = []
    for factor in FACTORS:
        for horizon in (1, 3, 5, 10, 20):
            ret = 'ret_'+str(horizon)
            for group in ('positive_factor', 'nonpositive_factor'):
                selected = []
                for r in results:
                    f = numeric(r[factor]); v = numeric(r.get(ret))
                    if f is None or v is None:
                        continue
                    if (f > 0) == (group == 'positive_factor'):
                        selected.append(v)
                if selected:
                    stats.append(dict(factor=factor, horizon=horizon, group=group,
                                      samples=len(selected), positive_return_rate_pct=round(100*sum(x>0 for x in selected)/len(selected), 2),
                                      mean_return_pct=round(100*sum(selected)/len(selected), 3)))
    write_csv(OUTPUT/'v25_factor_summary.csv', stats,
              ['factor','horizon','group','samples','positive_return_rate_pct','mean_return_pct'])
    print(f'V2.5 research: events={len(results)}; observations={len(factors)}; '
          f'snapshots={len(snapshots)}; matched_factors={sum(r["factor_count"] for r in results)}; groups={len(stats)}')
    if not factors:
        print('V2.5: No eligible dated factor data; factor fields intentionally empty. Templates require available_at and valid values.')
    if not snapshots:
        print('V2.5: No historical signal_snapshots.csv; historical low-base fields intentionally empty.')


if __name__ == '__main__':
    run()
