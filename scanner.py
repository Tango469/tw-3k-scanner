"""Taiwan 3K scanner V2. Stdlib only; official TWSE/TPEx data."""
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


def history_twse(day):
    """TWSE all-stock historical daily quotes. Match headers, not table numbers."""
    query = urllib.parse.urlencode({'response': 'json', 'date': day.strftime('%Y%m%d'), 'type': 'ALLBUT0999'})
    url = 'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?' + query
    payload = fetch_json(url, attempts=3)
    if not isinstance(payload, dict):
        raise ValueError(f'TWSE unexpected response type {type(payload).__name__}')
    status = str(payload.get('stat', ''))
    if status != 'OK':
        if any(x in status for x in ('沒有符合條件', '查無資料', '很抱歉', '休市')):
            return [], 'no-session'
        raise ValueError(f'TWSE status {status!r}')
    for key, headers in payload.items():
        if not re.fullmatch(r'fields\d+', key) or not isinstance(headers, list):
            continue
        names = [re.sub(r'<[^>]+>', '', str(h)).strip() for h in headers]
        idx = {v: i for i, v in enumerate(names)}
        required = ('證券代號', '證券名稱', '成交股數', '開盤價', '最高價', '最低價', '收盤價')
        if not all(k in idx for k in required):
            continue
        out = []
        for cells in payload.get(key.replace('fields', 'data'), []):
            if not isinstance(cells, list) or len(cells) <= max(idx.values()):
                continue
            # Correct mapping: open/high/low/close BEFORE volume.
            row = make_row(day.isoformat(), 'TWSE', cells[idx['證券代號']],
                           cells[idx['證券名稱']], cells[idx['開盤價']],
                           cells[idx['最高價']], cells[idx['最低價']],
                           cells[idx['收盤價']], cells[idx['成交股數']])
            if row:
                out.append(row)
        if len(out) >= 100:
            return out, 'ok'
        raise ValueError(f'TWSE quote table found but parsed only {len(out)} rows; headers={names!r}')
    raise ValueError('TWSE status OK but no recognized all-stock quote table; keys=' + ','.join(payload.keys()))


def history_tpex(day):
    """TPEx official historical daily quotes; fail loudly on schema changes."""
    roc = f'{day.year - 1911:03d}/{day.month:02d}/{day.day:02d}'
    query = urllib.parse.urlencode({'l': 'zh-tw', 'd': roc, 'se': 'EW'})
    url = 'https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?' + query
    payload = fetch_json(url, attempts=3)
    if not isinstance(payload, dict):
        raise ValueError(f'TPEx unexpected response type {type(payload).__name__}')
    rows = payload.get('aaData')
    if rows is None:
        raise ValueError('TPEx missing aaData; keys=' + ','.join(payload.keys()))
    if not isinstance(rows, list):
        raise ValueError('TPEx aaData is not a list')
    if not rows:
        # Only accept a zero-row result as a no-session if explicitly identified.
        # A broken/retired endpoint often returns an empty list for ALL dates.
        raise ValueError('TPEx empty aaData; cannot distinguish holiday from endpoint failure')
    out = []
    for cells in rows:
        if not isinstance(cells, list) or len(cells) < 9:
            continue
        row = make_row(day.isoformat(), 'TPEX', cells[0], cells[1],
                       cells[4], cells[5], cells[6], cells[2], cells[8])
        if row:
            out.append(row)
    if len(out) < 100:
        raise ValueError(f'TPEx parsed only {len(out)} of {len(rows)} rows; sample={str(rows[0])[:250]}')
    return out, 'ok'


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
                                history_days=i+1))
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


def publish(rows, signals, stats, errors, coverage):
    DOC.mkdir(parents=True, exist_ok=True)
    dates = sorted({r['date'] for r in rows})
    latest = dates[-1] if dates else '無資料'
    current = sorted((s for s in signals if s['date'] == latest),
                     key=lambda s: (-s['score'], s['market'], s['code']))
    cols = ['date','market','code','name','type','score','close','trigger','fail',
            'volume_ratio','ma20','history_days']
    csv_report(DOC/'latest.csv', current, cols)
    csv_report(DOC/'signals.csv', signals, cols)
    csv_report(DOC/'backtest.csv', stats,
               ['type','days','samples','avg_return_pct','positive_pct'])
    headers = ''.join(f'<th>{html.escape(c)}</th>' for c in cols)
    body = ''.join('<tr>'+''.join(f'<td>{html.escape(str(s.get(c,"")))}</td>' for c in cols)+'</tr>'
                   for s in current[:150])
    market_counts = ', '.join(f'{m}: {n} 日' for m, n in coverage.items())
    warnings = '; '.join(errors) if errors else '無'
    page = f'''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>台股三K盤後掃描 V2</title><style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:auto;padding:20px;background:#f7f9fc;color:#16243a}}.box{{background:white;border-radius:12px;padding:16px;margin:14px 0}}.scroll{{overflow:auto}}table{{border-collapse:collapse;min-width:950px;width:100%}}td,th{{padding:9px;border-bottom:1px solid #e1e5ea;text-align:left;font-size:13px}}th{{background:#e9eff6}}</style><h1>台股三 K 盤後掃描 V2</h1><div class="box"><b>資料最新日期：{html.escape(latest)}</b><p>候選 {len(current)} 檔｜累積交易日 {len(dates)} 天｜各市場覆蓋：{html.escape(market_counts)}</p><p>歷史回補逐批進行；未滿 20 日的個股，其 MA20 與量能評分可能不完整。</p><p><a href="latest.csv">今日排名</a> · <a href="signals.csv">歷史訊號</a> · <a href="backtest.csv">回測統計</a></p><p>資料下載警告：{html.escape(warnings)}</p><p>A：中間 K 高低點均低於兩側；B：中間 K 低點低於兩側但不符合 A。評分為研究用途，非經驗證勝率。</p></div><div class="box scroll"><table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table></div><p>trigger 為左右 K 高點較高者；fail 為中間 K 低點。回測為未含成本的未來收盤價報酬，不代表可成交策略績效。</p></html>'''
    (DOC/'index.html').write_text(page, encoding='utf-8')
    print(f'Latest={latest}; rows={len(rows)}; candidates={len(current)}; coverage={coverage}; errors={errors}')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--backfill-days', type=int, default=120,
                        help='Target historical trading sessions per market')
    parser.add_argument('--batch-size', type=int, default=12,
                        help='Historical calendar dates to attempt per run (safe for GitHub Actions)')
    parser.add_argument('--skip-latest', action='store_true')
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
    # Backfill oldest missing weekdays first, retaining only confirmed successful
    # dates. Failed requests are retried on later runs, not marked as done.
    state_path = ROOT/'data'/'backfill_state.json'
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {}
    for market in LATEST:
        known = {k[0] for k in indexed if k[1] == market}
        no_sessions = set(state.get(market, {}).get('no_sessions', [])) if isinstance(state.get(market), dict) else set()
        # Existing V2 state lists are discarded: they marked 0-row failures done.
        end = today - dt.timedelta(days=1)
        start = end - dt.timedelta(days=max(200, int(args.backfill_days * 1.8)))
        dates = [start + dt.timedelta(days=i) for i in range((end-start).days + 1)]
        dates.reverse()  # newest missing dates first, then progressively older
        processed = 0
        successes = 0
        for day in dates:
            if len(known) >= args.backfill_days or processed >= max(0, args.batch_size):
                break
            key = day.isoformat()
            if day.weekday() >= 5 or key in known or key in no_sessions:
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
                successes += 1
                print(f'{market} history {key}: {len(records)} rows')
            except Exception as exc:
                message = f'{market} history {key}: {exc}'
                errors.append(message)
                print(message, file=sys.stderr)
            time.sleep(0.5)
        state[market] = {'no_sessions': sorted(no_sessions)}
        print(f'{market} history batch: attempted={processed}; added_sessions={successes}; coverage={len(known)}')
    rows = sorted(indexed.values(), key=lambda r: (r['date'],r['market'],r['code']))
    if not rows:
        raise SystemExit('No valid data; refusing to publish')
    save_rows(rows)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    signals, groups = scan(rows)
    coverage = {m: len({r['date'] for r in rows if r['market'] == m}) for m in LATEST}
    publish(rows, signals, backtest(signals, groups), errors, coverage)
    if errors:
        print('WARNING: partial data failures; see report', file=sys.stderr)
    if all(len({r['date'] for r in rows if r['market'] == m}) < 3 for m in LATEST):
        print('WARNING: insufficient history for any 3K signals', file=sys.stderr)

if __name__ == '__main__':
    main()
