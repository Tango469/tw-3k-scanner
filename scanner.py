"""Taiwan 3K scanner V2.3.4. Stdlib only; official TWSE/TPEx data."""
import argparse
import csv
import datetime as dt
import html
import http.client
import json
import re
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'data' / 'daily.csv'
DOC = ROOT / 'docs'
FIELDS = ['date', 'market', 'code', 'name', 'open', 'high', 'low', 'close', 'volume']
LATEST = {
    'TWSE': 'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
    'TPEX': 'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes',
}


def number(value):
    try:
        return float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def field(row, *names):
    for name in names:
        if name in row and str(row[name]).strip():
            return row[name]
    return None


def normalized_date(value):
    s = re.sub(r'[^0-9]', '', str(value or ''))
    if len(s) == 8 and int(s[:4]) > 1900:
        return f'{s[:4]}-{s[4:6]}-{s[6:8]}'
    if len(s) == 7:
        return f'{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}'
    if len(s) == 6:
        return f'{int(s[:2]) + 1911:04d}-{s[2:4]}-{s[4:6]}'
    raise ValueError(f'Unrecognized date {value!r}')


def fetch_json(url, attempts=4):
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json',
            'Accept-Encoding': 'identity', 'Connection': 'close',
        })
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.loads(response.read().decode('utf-8-sig'))
        except (http.client.IncompleteRead, http.client.RemoteDisconnected,
                urllib.error.URLError, TimeoutError, socket.timeout,
                ConnectionError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f'Attempt {attempt}/{attempts} failed: {url}: {exc}', file=sys.stderr)
            if attempt == attempts:
                raise
            time.sleep(min(2 * attempt, 8))


def make_row(day, market, code, name, opening, high, low, close, volume):
    code = str(code or '').strip()
    if not re.fullmatch(r'\d{4}', code) or code.startswith('0'):
        return None  # Exclude ETFs, warrants and non-common-stock codes.
    values = [number(v) for v in (opening, high, low, close, volume)]
    if any(v is None for v in values):
        return None
    o, h, l, c, v = values
    if min(o, h, l, c) <= 0 or v < 0 or h < max(o, c, l) or l > min(o, c, h):
        return None
    return dict(date=day, market=market, code=code,
                name=str(name or code).strip(), open=o, high=h, low=l, close=c, volume=v)


def latest_market(market):
    payload = fetch_json(LATEST[market])
    if not isinstance(payload, list):
        raise ValueError('Unexpected latest API format')
    result = []
    for item in payload:
        try:
            day = normalized_date(field(item, 'Date', '日期'))
        except ValueError:
            continue
        row = make_row(
            day, market, field(item, 'Code', 'SecuritiesCompanyCode', 'SecuritiesCode', '代號'),
            field(item, 'CompanyName', 'SecuritiesCompanyName', 'SecuritiesName',
                  'Name', 'StockName', '股票名稱', '證券名稱', '公司名稱'),
            field(item, 'OpeningPrice', 'Open', 'Opening', '開盤價'),
            field(item, 'HighestPrice', 'High', 'Highest', '最高價'),
            field(item, 'LowestPrice', 'Low', 'Lowest', '最低價'),
            field(item, 'ClosingPrice', 'Close', 'Closing', '收盤價'),
            field(item, 'TradeVolume', 'TradingShares', 'TradeShares', '成交股數'))
        if row:
            result.append(row)
    if len(result) < 100:
        raise ValueError(f'{market} latest only {len(result)} usable stocks; schema may have changed')
    return result


def history_table(payload, market, day):
    """Parse official historical tables by normalized field names, never table positions."""
    if not isinstance(payload, dict):
        raise ValueError(f'{market}: unexpected payload type')
    tables = payload.get('tables')
    if not isinstance(tables, list):
        raise ValueError(f'{market}: missing tables; keys={list(payload)}')
    aliases = {
        'code': ('證券代號', '代號'),
        'name': ('證券名稱', '名稱'),
        'open': ('開盤價', '開盤'),
        'high': ('最高價', '最高'),
        'low': ('最低價', '最低'),
        'close': ('收盤價', '收盤'),
        'volume': ('成交股數',),
    }
    candidates = []
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get('fields'), list):
            continue
        names = [re.sub(r'<[^>]*>', '', str(x)).replace('　', '').strip() for x in table['fields']]
        indices = {}
        for key, choices in aliases.items():
            indices[key] = next((names.index(alias) for alias in choices if alias in names), None)
        if any(v is None for v in indices.values()):
            continue
        records = table.get('data', [])
        if not isinstance(records, list):
            continue
        parsed = []
        for cells in records:
            if not isinstance(cells, list) or len(cells) <= max(indices.values()):
                continue
            row = make_row(day.isoformat(), market, *(
                cells[indices[k]] for k in ('code', 'name', 'open', 'high', 'low', 'close', 'volume')
            ))
            if row:
                parsed.append(row)
        candidates.append((len(parsed), parsed, names))
    if not candidates:
        raise ValueError(f'{market}: no recognized stock table; tables={len(tables)}')
    count, rows, names = max(candidates, key=lambda x: x[0])
    if count < 100:
        raise ValueError(f'{market}: parsed only {count} stock rows; headers={names}')
    return rows, 'ok'


def history_twse(day):
    query = urllib.parse.urlencode({'response': 'json', 'date': day.strftime('%Y%m%d'), 'type': 'ALLBUT0999'})
    payload = fetch_json('https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?' + query, attempts=3)
    status = str(payload.get('stat', '')) if isinstance(payload, dict) else ''
    if status != 'OK':
        if any(x in status for x in ('沒有符合條件', '查無資料', '很抱歉', '休市')):
            return [], 'no-session'
        raise ValueError(f'TWSE status {status!r}')
    return history_table(payload, 'TWSE', day)


def history_tpex(day):
    roc = f'{day.year - 1911:03d}/{day.month:02d}/{day.day:02d}'
    query = urllib.parse.urlencode({'l': 'zh-tw', 'd': roc, 'se': 'EW'})
    payload = fetch_json('https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?' + query, attempts=3)
    if isinstance(payload, dict) and str(payload.get('stat', '')).lower() in ('休市', '查無資料'):
        return [], 'no-session'
    return history_table(payload, 'TPEX', day)


def refresh_twse_current_session(indexed, today, errors):
    """Prefer official TWSE dated historical endpoint when OpenAPI lags.

    Only check on/after 16:00 Taiwan time, on weekdays; never replace valid
    latest records with an empty or unverified historical response.
    """
    now = dt.datetime.now(ZoneInfo('Asia/Taipei'))
    if today.weekday() >= 5 or now.hour < 16:
        print('TWSE freshness: current-session check deferred (weekend / before 16:00)')
        return
    day_str = today.isoformat()
    known_today = sum(1 for key in indexed if key[0] == day_str and key[1] == 'TWSE')
    if known_today >= 100:
        print(f'TWSE freshness: current session {day_str} already present ({known_today} stocks)')
        return
    try:
        records, status = history_twse(today)
        if status != 'ok' or len(records) < 100:
            print(f'TWSE freshness: {day_str} not yet available (status={status})')
            return
        for row in records:
            indexed[(row['date'], row['market'], row['code'])] = row
        print(f'TWSE freshness: recovered {day_str} from dated MI_INDEX, {len(records)} stocks')
    except Exception as exc:
        msg = f'TWSE current-session fallback {day_str}: {exc}'
        errors.append(msg)
        print(msg, file=sys.stderr)


def read_rows():
    if not DB.exists():
        return []
    with DB.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def save_rows(rows):
    DB.parent.mkdir(parents=True, exist_ok=True)
    with DB.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def scan(rows):
    groups = defaultdict(list)
    for row in rows:
        try:
            q = {**row, **{k: float(row[k]) for k in ('open', 'high', 'low', 'close', 'volume')}}
            groups[(row['market'], row['code'])].append(q)
        except (ValueError, KeyError):
            continue
    signals = []
    for (market, code), values in groups.items():
        series = sorted(values, key=lambda x: x['date'])
        for i in range(2, len(series)):
            left, middle, right = series[i-2:i+1]
            if not (middle['low'] < left['low'] and middle['low'] < right['low']):
                continue
            kind = 'A' if middle['high'] < left['high'] and middle['high'] < right['high'] else 'B'
            recent = series[i-19:i+1] if i >= 19 else []
            prior = series[i-5:i] if i >= 5 else []
            ma20 = sum(x['close'] for x in recent)/20 if recent else None
            vol5 = sum(x['volume'] for x in prior)/5 if prior else None
            ratio = right['volume']/vol5 if vol5 else None
            closepos = ((right['close']-right['low'])/(right['high']-right['low'])
                        if right['high'] > right['low'] else 0.5)
            score = (20 if kind == 'A' else 15) + round(closepos * 15)
            if ma20 is not None and right['close'] >= ma20:
                score += 15
            if ratio is not None:
                score += 15 if ratio >= 1.3 else (8 if ratio >= 1 else 0)
            if recent:
                low20 = min(x['low'] for x in recent)
                high20 = max(x['high'] for x in recent)
                position = (right['close']-low20)/(high20-low20) if high20 > low20 else 0.5
                score += 20 if position <= 0.5 else (10 if position <= 0.75 else 0)
            if right['close'] > left['high']:
                score += 15
            signals.append(dict(date=right['date'], market=market, code=code,
                                name=right['name'], type=kind, score=min(100, score),
                                close=right['close'], trigger=round(max(left['high'], right['high']), 2),
                                fail=round(middle['low'], 2),
                                volume_ratio=round(ratio, 2) if ratio is not None else '',
                                ma20=round(ma20, 2) if ma20 is not None else '',
                                history_days=i+1,
                                volume=right['volume'],
                                turnover=round(right['close'] * right['volume'], 2),
                                left_date=left['date'], left_high=left['high'], left_low=left['low'],
                                middle_date=middle['date'], middle_high=middle['high'], middle_low=middle['low'],
                                right_date=right['date'], right_high=right['high'], right_low=right['low']))
    return signals, groups


def backtest(signals, groups):
    stats = defaultdict(list)
    for signal in signals:
        series = sorted(groups[(signal['market'], signal['code'])], key=lambda r: r['date'])
        index = next((i for i, row in enumerate(series) if row['date'] == signal['date']), None)
        if index is None:
            continue
        for horizon in (5, 10, 20):
            if index + horizon < len(series):
                ret = (series[index+horizon]['close']/signal['close'] - 1)*100
                stats[(signal['type'], horizon)].append(ret)
    return [dict(type=t, days=h, samples=len(vals),
                 avg_return_pct=round(sum(vals)/len(vals), 2),
                 positive_pct=round(100*sum(x > 0 for x in vals)/len(vals), 1))
            for (t, h), vals in sorted(stats.items())]


def csv_report(path, records, cols):
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=cols, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(records)


def publish(rows, signals, stats, errors, coverage, min_volume=100000, min_turnover=2000000):
    """Publish market-specific latest sessions; incomplete and illiquid signals separately."""
    DOC.mkdir(parents=True, exist_ok=True)
    latest_by_market = {
        m: max((r['date'] for r in rows if r['market'] == m), default='')
        for m in LATEST
    }
    latest = max(latest_by_market.values(), default='')
    market_signals = [s for s in signals if s['date'] == latest_by_market.get(s['market'])]
    qualified, watch = [], []
    audit = []
    for market in LATEST:
        selected = [s for s in market_signals if s['market'] == market]
        audit.append(dict(market=market, latest_date=latest_by_market[market],
                          raw_signals=len(selected), history_short=sum(s['history_days'] < 20 for s in selected),
                          low_liquidity=sum(s['history_days'] >= 20 and
                                            (s['volume'] < min_volume or s['turnover'] < min_turnover)
                                            for s in selected),
                          qualified=sum(s['history_days'] >= 20 and s['volume'] >= min_volume and
                                        s['turnover'] >= min_turnover for s in selected),
                          a_signals=sum(s['type'] == 'A' for s in selected),
                          b_signals=sum(s['type'] == 'B' for s in selected)))
    for signal in market_signals:
        if signal['history_days'] < 20:
            signal['status'] = '歷史不足20日'
            watch.append(signal)
        elif signal['volume'] < min_volume or signal['turnover'] < min_turnover:
            signal['status'] = '流動性不足'
            watch.append(signal)
        else:
            signal['status'] = '符合初篩'
            qualified.append(signal)
    ordering = lambda s: (-s['score'], s['market'], s['code'])
    qualified.sort(key=ordering)
    watch.sort(key=ordering)
    cols = ['date','market','code','name','type','score','close','trigger','fail',
            'volume_ratio','ma20','history_days','volume','turnover','status']
    csv_report(DOC/'latest.csv', qualified, cols)
    csv_report(DOC/'watch.csv', watch, cols)
    audit_cols = ['market','latest_date','raw_signals','history_short','low_liquidity',
                  'qualified','a_signals','b_signals']
    csv_report(DOC/'audit.csv', audit, audit_cols)
    ohlc_cols = ['left_date','left_high','left_low','middle_date','middle_high',
                 'middle_low','right_date','right_high','right_low']
    csv_report(DOC/'signal_validation.csv', market_signals, cols + ohlc_cols)
    csv_report(DOC/'signals.csv', signals, cols)
    csv_report(DOC/'backtest.csv', stats,
               ['type','days','samples','avg_return_pct','positive_pct'])
    headers = ''.join(f'<th>{html.escape(c)}</th>' for c in cols)
    body = ''.join('<tr>'+''.join(f'<td>{html.escape(str(sig.get(c,"")))}</td>' for c in cols)+'</tr>'
                   for sig in qualified[:150])
    coverage_text = ', '.join(f'{m}: {n} 日' for m,n in coverage.items())
    dates_text = ', '.join(f'{m}: {latest_by_market[m] or "無資料"}' for m in LATEST)
    warnings = '; '.join(errors[:12]) if errors else '無'
    if len(errors) > 12:
        warnings += f'；另有 {len(errors)-12} 項'
    page = f'''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>台股三K盤後掃描 V2.3</title><style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:auto;padding:20px;background:#f7f9fc;color:#16243a}}.box{{background:white;border-radius:12px;padding:16px;margin:14px 0}}.scroll{{overflow:auto}}table{{border-collapse:collapse;min-width:1100px;width:100%}}td,th{{padding:9px;border-bottom:1px solid #e1e5ea;text-align:left;font-size:13px}}th{{background:#e9eff6}}</style><h1>台股三 K 盤後掃描 V2.3.4</h1><div class="box"><b>各市場最新日期：{html.escape(dates_text)}</b><p>合格初篩 {len(qualified)} 檔｜觀察 {len(watch)} 檔｜覆蓋：{html.escape(coverage_text)}</p><p>初篩門檻：每檔至少20筆歷史日線、當日成交量 ≥ {min_volume:,} 股、以收盤價估算成交額 ≥ NT$ {min_turnover:,.0f}；不是買進建議。</p><p><a href="latest.csv">合格初篩</a> · <a href="watch.csv">資料不足／低流動性觀察</a> · <a href="audit.csv">篩選稽核</a> · <a href="signal_validation.csv">三K驗證</a> · <a href="signals.csv">歷史訊號</a> · <a href="backtest.csv">回測統計</a></p><p>下載警告：{html.escape(warnings)}</p><p>A：中間K高低點均低於兩側；B：中間K低點低於兩側但不符合A。</p></div><div class="box scroll"><table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table></div><p>trigger 是突破觀察價，fail 是型態失效參考價。回測不含交易成本、流動性限制或實際成交模擬。</p></html>'''
    (DOC/'index.html').write_text(page, encoding='utf-8')
    print(f'Selection audit={audit}')
    print(f'Latest by market={latest_by_market}; rows={len(rows)}; qualified={len(qualified)}; watch={len(watch)}; coverage={coverage}; errors={len(errors)}')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--backfill-days', type=int, default=120,
                        help='Target historical trading sessions per market')
    parser.add_argument('--batch-size', type=int, default=100,
                        help='Maximum missing historical dates to attempt per market per run')
    parser.add_argument('--max-backfill-seconds', type=int, default=540,
                        help='Total historical backfill time budget in seconds (default 9 minutes)')
    parser.add_argument('--request-pause', type=float, default=0.8,
                        help='Delay between historical API requests in seconds')
    parser.add_argument('--skip-latest', action='store_true')
    parser.add_argument('--min-volume', type=float, default=100000, help='Minimum daily traded shares')
    parser.add_argument('--min-turnover', type=float, default=2000000, help='Estimated NT$ traded value')
    args = parser.parse_args(argv)
    old = read_rows()
    indexed = {(r['date'],r['market'],r['code']):r for r in old}
    errors = []
    today = dt.datetime.now(ZoneInfo('Asia/Taipei')).date()
    if not args.skip_latest:
        for market in LATEST:
            try:
                records = latest_market(market)
                last = max(r['date'] for r in records)
                if abs((today - dt.date.fromisoformat(last)).days) > 5:
                    raise ValueError(f'stale/future date: {last}')
                for row in records:
                    indexed[(row['date'], row['market'], row['code'])] = row
                print(f'{market}: latest {len(records)} rows, date={last}')
            except Exception as exc:
                errors.append(f'{market} latest: {exc}')
                print(errors[-1], file=sys.stderr)
    if not args.skip_latest:
        refresh_twse_current_session(indexed, today, errors)
    # Backfill newest missing weekdays first, retaining only confirmed successful
    # dates. Failed requests are retried on later runs, not marked as done.
    state_path = ROOT/'data'/'backfill_state.json'
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {}
    backfill_started = time.monotonic()
    deadline = backfill_started + max(0, args.max_backfill_seconds)
    for market in LATEST:
        known = {k[0] for k in indexed if k[1] == market}
        market_state = state.get(market, {}) if isinstance(state.get(market), dict) else {}
        no_sessions = set(market_state.get('no_sessions', []))
        failures = dict(market_state.get('failures', {}))
        run_number = int(market_state.get('run_number', 0)) + 1
        # Existing V2 state lists are discarded: they marked 0-row failures done.
        end = today - dt.timedelta(days=1)
        start = end - dt.timedelta(days=max(240, int(args.backfill_days * 2.1)))
        dates = [start + dt.timedelta(days=i) for i in range((end-start).days + 1)]
        dates.reverse()  # newest missing dates first, then progressively older
        processed = 0
        successes = 0
        stop_reason = 'date range exhausted'
        for day in dates:
            if len(known) >= args.backfill_days:
                stop_reason = 'target reached'
                break
            if processed >= max(0, args.batch_size):
                stop_reason = 'batch limit reached'
                break
            if time.monotonic() >= deadline:
                stop_reason = 'time budget reached'
                break
            key = day.isoformat()
            if day.weekday() >= 5 or key in known or key in no_sessions:
                continue
            # Retry persistent malformed/empty historical responses every 7 runs,
            # instead of consuming the 12-request budget on every run.
            if failures.get(key, 0) >= 2 and run_number % 7 != 0:
                continue
            processed += 1
            try:
                records, status = history_twse(day) if market == 'TWSE' else history_tpex(day)
                if status == 'no-session':
                    no_sessions.add(key)
                    print(f'{market} history {key}: confirmed no session')
                    continue
                if len(records) < 100:
                    raise ValueError(f'only {len(records)} valid rows')
                for row in records:
                    indexed[(row['date'], row['market'], row['code'])] = row
                known.add(key)
                failures.pop(key, None)
                successes += 1
                print(f'{market} history {key}: {len(records)} rows')
            except Exception as exc:
                message = f'{market} history {key}: {exc}'
                errors.append(message)
                failures[key] = int(failures.get(key, 0)) + 1
                print(message, file=sys.stderr)
            if time.monotonic() < deadline:
                time.sleep(max(0.0, args.request_pause))
        if len(known) >= args.backfill_days:
            stop_reason = 'target reached'
        state[market] = {'no_sessions': sorted(no_sessions), 'failures': failures,
                         'run_number': run_number}
        print(f'{market} history batch: attempted={processed}; added_sessions={successes}; coverage={len(known)}/{args.backfill_days}; stop={stop_reason}')
    print(f'Backfill elapsed={time.monotonic() - backfill_started:.1f}s; budget={args.max_backfill_seconds}s')
    rows = sorted(indexed.values(), key=lambda r: (r['date'],r['market'],r['code']))
    if not rows:
        raise SystemExit('No valid data; refusing to publish')
    save_rows(rows)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    signals, groups = scan(rows)
    coverage = {m: len({r['date'] for r in rows if r['market'] == m}) for m in LATEST}
    publish(rows, signals, backtest(signals, groups), errors, coverage, args.min_volume, args.min_turnover)
    if errors:
        print('WARNING: partial data failures; see report', file=sys.stderr)
    if all(len({r['date'] for r in rows if r['market'] == m}) < 3 for m in LATEST):
        print('WARNING: insufficient history for any 3K signals', file=sys.stderr)

if __name__ == '__main__':
    main()
