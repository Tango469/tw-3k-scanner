import csv, json, re, sys, time, urllib.request
import http.client, socket, urllib.error
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
from collections import defaultdict

ROOT=Path(__file__).parent; DB=ROOT/'data'/'daily.csv'; DOC=ROOT/'docs'
FIELDS=['date','market','code','name','open','high','low','close','volume']
URLS={
 'TWSE':'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
 'TPEX':'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes'
}
def num(v):
 try: return float(str(v).replace(',','').strip())
 except (TypeError,ValueError): return None
def get(row,*keys):
 for k in keys:
  if k in row and str(row[k]).strip():return row[k]
 return None
def norm_date(raw):
 s=re.sub(r'[^0-9]','',str(raw or ''))
 if len(s)==8 and int(s[:4])>1900: return f'{s[:4]}-{s[4:6]}-{s[6:8]}'
 if len(s)==7: return f'{int(s[:3])+1911:04d}-{s[3:5]}-{s[5:7]}'
 if len(s)==6: return f'{int(s[:2])+1911:04d}-{s[2:4]}-{s[4:6]}'
 raise ValueError(f'Unrecognized date: {raw}')
def download(market):
 # Re-create the connection for every attempt: an incomplete HTTP response
 # must never be reused. Retry transient network errors and truncated JSON.
 payload=None
 for attempt in range(1,5):
  req=urllib.request.Request(URLS[market],headers={
   'User-Agent':'Mozilla/5.0', 'Accept':'application/json',
   'Accept-Encoding':'identity', 'Connection':'close',
   'Cache-Control':'no-cache'})
  try:
   with urllib.request.urlopen(req,timeout=90) as response:
    raw=response.read()
   payload=json.loads(raw.decode('utf-8-sig'))
   break
  except (http.client.IncompleteRead, http.client.RemoteDisconnected,
          urllib.error.URLError, TimeoutError, socket.timeout,
          ConnectionError, json.JSONDecodeError, UnicodeDecodeError) as exc:
   print(f'{market}: download attempt {attempt}/4 failed: {type(exc).__name__}: {exc}',
         file=sys.stderr,flush=True)
   if attempt==4:
    raise RuntimeError(f'{market} download failed after 4 attempts: {exc}') from exc
   time.sleep(min(3*attempt,9))
 if not isinstance(payload,list) or not payload:raise ValueError(f'{market} returned empty/non-list payload')
 out=[]
 for r in payload:
  code=str(get(r,'Code','SecuritiesCompanyCode','SecuritiesCode','代號') or '').strip()
  if not re.fullmatch(r'\d{4}',code):continue
  try: d=norm_date(get(r,'Date','日期'))
  except ValueError:continue
  o=num(get(r,'OpeningPrice','Open','Opening','開盤價'))
  h=num(get(r,'HighestPrice','High','Highest','最高價'))
  l=num(get(r,'LowestPrice','Low','Lowest','最低價'))
  c=num(get(r,'ClosingPrice','Close','Closing','收盤價'))
  v=num(get(r,'TradeVolume','TradingShares','TradeShares','成交股數'))
  if None in (o,h,l,c,v) or min(o,h,l,c)<=0 or h<max(o,c,l) or l>min(o,c,h):continue
  out.append(dict(date=d,market=market,code=code,name=str(get(r,'Name','SecuritiesCompanyName','SecuritiesName','證券名稱') or code).strip(),open=o,high=h,low=l,close=c,volume=v))
 if len(out)<100:raise ValueError(f'{market}: only {len(out)} usable stocks; check API schema')
 return out

def load():
 if not DB.exists():return []
 with DB.open(encoding='utf-8',newline='') as f:return list(csv.DictReader(f))
def save(rows):
 DB.parent.mkdir(exist_ok=True)
 with DB.open('w',encoding='utf-8',newline='') as f:
  w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
def scan(rows):
 by=defaultdict(list)
 for r in rows:
  try:
   q={**r,**{k:float(r[k]) for k in ('open','high','low','close','volume')}}
   by[(r['market'],r['code'])].append(q)
  except (ValueError,KeyError):continue
 signals=[]
 for (market,code),items in by.items():
  a=sorted(items,key=lambda x:x['date'])
  for i in range(2,len(a)):
   left,mid,right=a[i-2:i+1]
   if not(mid['low']<left['low'] and mid['low']<right['low']):continue
   typ='A' if mid['high']<left['high'] and mid['high']<right['high'] else 'B'
   recent=a[max(0,i-19):i+1]; prev=a[max(0,i-5):i]
   ma20=sum(x['close'] for x in recent)/len(recent) if len(recent)==20 else None
   vol5=sum(x['volume'] for x in prev)/5 if len(prev)==5 else None
   ratio=right['volume']/vol5 if vol5 else None
   closepos=(right['close']-right['low'])/(right['high']-right['low']) if right['high']>right['low'] else 0.5
   # transparent provisional scoring, not calibrated to win rate
   score=(20 if typ=='A' else 15)+round(closepos*15)
   if ma20 is not None:score+=15 if right['close']>=ma20 else 0
   if ratio is not None:score+=15 if ratio>=1.3 else (8 if ratio>=1 else 0)
   if len(recent)==20:
    low20=min(x['low'] for x in recent); high20=max(x['high'] for x in recent)
    location=(right['close']-low20)/(high20-low20) if high20>low20 else 0.5
    score+=20 if location<=0.5 else (10 if location<=0.75 else 0)
   if right['close']>left['high']:score+=15
   sig=dict(date=right['date'],market=market,code=code,name=right['name'],type=typ,score=min(100,score),close=right['close'],trigger=round(max(left['high'],right['high']),2),fail=round(mid['low'],2),volume_ratio=round(ratio,2) if ratio else '',ma20=round(ma20,2) if ma20 else '',history_days=i+1)
   signals.append(sig)
 return signals,by

def backtest(signals,by):
 stats=defaultdict(list)
 for s in signals:
  a=sorted(by[(s['market'],s['code'])],key=lambda r:r['date'])
  idx=next((i for i,r in enumerate(a) if r['date']==s['date']),None)
  if idx is None:continue
  for horizon in (5,10,20):
   if idx+horizon>=len(a):continue
   # close-to-close forward returns, NOT executable strategy performance
   ret=(a[idx+horizon]['close']/s['close']-1)*100
   stats[(s['type'],horizon)].append(ret)
 return [{'type':t,'days':h,'samples':len(v),'avg_return_pct':round(sum(v)/len(v),2),'positive_pct':round(100*sum(x>0 for x in v)/len(v),1)} for (t,h),v in sorted(stats.items())]

def write_csv(path,records,cols):
 with path.open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore');w.writeheader();w.writerows(records)
def publish(rows,signals,stats,errors):
 DOC.mkdir(exist_ok=True)
 dates=sorted({r['date'] for r in rows}); last=dates[-1] if dates else '無資料'
 current=sorted([s for s in signals if s['date']==last],key=lambda s:(-s['score'],s['market'],s['code']))
 cols=['date','market','code','name','type','score','close','trigger','fail','volume_ratio','ma20','history_days']
 write_csv(DOC/'latest.csv',current,cols);write_csv(DOC/'signals.csv',signals,cols)
 write_csv(DOC/'backtest.csv',stats,['type','days','samples','avg_return_pct','positive_pct'])
 import html
 def esc(x):return html.escape(str(x))
 head=''.join(f'<th>{esc(k)}</th>' for k in cols)
 body=''.join('<tr>'+''.join(f'<td>{esc(s.get(k,""))}</td>' for k in cols)+'</tr>' for s in current[:100])
 warning='; '.join(errors) if errors else '無'
 status='暖機中：未滿20個交易日，均線/量能評分不完整' if len(dates)<20 else '具備20日歷史資料（仍非完整兩年回測）'
 page=f'''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>台股三K盤後掃描</title><style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:auto;padding:20px;background:#f7f9fc;color:#16243a}}.box{{background:white;border-radius:12px;padding:16px;margin:14px 0}}.scroll{{overflow:auto}}table{{border-collapse:collapse;min-width:900px;width:100%}}td,th{{padding:9px;border-bottom:1px solid #e1e5ea;text-align:left;font-size:13px}}th{{background:#e9eff6}}a{{color:#175baf}}</style><h1>台股三 K 盤後掃描器</h1><div class="box"><b>資料最新日期：{esc(last)}</b><p>候選 {len(current)} 檔｜累積交易日 {len(dates)} 天｜{esc(status)}</p><p>下載：<a href="latest.csv">今日排名 CSV</a> · <a href="signals.csv">歷史訊號</a> · <a href="backtest.csv">回測統計</a></p><p>資料來源：TWSE / TPEx 官方公開 API。下載異常：{esc(warning)}</p><p>型態 A：中間最高與最低均低於兩側；型態 B：中間最低低於兩側，但不符合 A。分數為未校準的研究指標，非買賣建議。</p></div><div class="box scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div><p>突破觀察價 trigger = 左側與右側最高價的較高者；失效參考 fail = 中間最低價。價格未含交易成本、滑價與除權息調整。</p></html>'''
 (DOC/'index.html').write_text(page,encoding='utf-8')
 print(f'Latest={last}; rows={len(rows)}; candidates={len(current)}; sessions={len(dates)}; errors={errors}')

def main():
 old=load(); allrows={(r['date'],r['market'],r['code']):r for r in old}; errors=[]; added=0
 today=datetime.now(ZoneInfo('Asia/Taipei')).date()
 for market in URLS:
  try:
   records=download(market)
   latest=max(r['date'] for r in records)
   lag=(today-date.fromisoformat(latest)).days
   if lag>5 or lag<0:raise ValueError(f'stale/future source date {latest}')
   for r in records:allrows[(r['date'],r['market'],r['code'])]=r;added+=1
   print(f'{market}: {len(records)} records, date={latest}')
  except Exception as e:errors.append(f'{market}: {e}');print(errors[-1],file=sys.stderr)
 if not allrows:raise SystemExit('No valid market data; refusing to publish fake results')
 rows=sorted(allrows.values(),key=lambda r:(r['date'],r['market'],r['code']))
 save(rows); signals,by=scan(rows); publish(rows,signals,backtest(signals,by),errors)
 if errors:print('WARNING: one or more market sources failed',file=sys.stderr)
if __name__=='__main__':main()
