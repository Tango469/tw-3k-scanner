"""V2.5.2 point-in-time factor collection. No changes to V2.4 trading rules.

TWSE T86 daily institutional flow; TPEx official OpenAPI when date can be
verified; equal-weighted market breadth from scanner's historical OHLCV.
Only writes observations with conservative after-close availability timestamps.
"""
import csv
import datetime as dt
import json
import re
import time
import urllib.request
from collections import defaultdict, Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PATH = ROOT/'data'/'factors'/'factor_observations.csv'
FIELDS = ['market','code','factor','value','observation_date','available_at','source']
TWSE_URL='https://www.twse.com.tw/rwd/zh/fund/T86?date={}&selectType=ALLBUT0999&response=json'
TPEX_URL='https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading'

def get_json(url):
    req=urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0','Accept':'application/json','Accept-Encoding':'identity'})
    last=None
    for i in range(3):
        try:
            with urllib.request.urlopen(req,timeout=35) as f:
                return json.loads(f.read().decode('utf-8-sig'))
        except Exception as e:
            last=e
            time.sleep(i+1)
    raise RuntimeError(str(last))

def num(x):
    try: return float(str(x).replace(',','').replace('＋','+').strip())
    except (ValueError,TypeError): return None

def norm_date(s):
    s=str(s or '').strip().replace('/','-')
    parts=s.split('-')
    if len(parts)==3 and parts[0].isdigit():
        y=int(parts[0]); y=y+1911 if y<1911 else y
        return f'{y:04d}-{int(parts[1]):02d}-{int(parts[2]):02d}'
    raw=re.sub(r'\D','',s)
    if len(raw)==7: return f'{int(raw[:3])+1911:04d}-{raw[3:5]}-{raw[5:7]}'
    if len(raw)==8: return f'{raw[:4]}-{raw[4:6]}-{raw[6:8]}'
    return ''

def available(day):
    return day+'T19:00:00+08:00' # conservative; no same-day intraday use

def observation(market,code,factor,value,day,source):
    return {'market':market,'code':code,'factor':factor,'value':str(round(value,8)),
            'observation_date':day,'available_at':available(day),'source':source}

def parse_twse(payload, day):
    if payload.get('stat')!='OK': return []
    fields=payload.get('fields',[])
    data=payload.get('data',[])
    if not fields or not data: return []
    def index(terms):
        for i,f in enumerate(fields):
            n=str(f).replace(' ','').replace('（','(').replace('）',')')
            if all(t in n for t in terms): return i
        return None
    code_idx=index(['證券代號'])
    foreign_idx=index(['外陸資','買賣超','不含外資自營商'])
    trust_idx=index(['投信','買賣超'])
    dealer_idx=index(['自營商','買賣超','自行買賣'])
    hedge_idx=index(['自營商','買賣超','避險'])
    if None in (code_idx,foreign_idx,trust_idx,dealer_idx,hedge_idx):
        raise ValueError('TWSE T86 column names changed; refuse to mislabel institutional data: '+repr(fields))
    result=[]
    for r in data:
        code=str(r[code_idx]).strip()
        if not re.fullmatch(r'[1-9]\d{3}',code): continue
        values=[num(r[i]) for i in (foreign_idx,trust_idx,dealer_idx,hedge_idx)]
        if any(v is None for v in values): continue
        foreign,trust,dealer,hedge=values
        for factor,v in [('foreign_net_shares',foreign),('trust_net_shares',trust),('dealer_net_shares',dealer+hedge)]:
            result.append(observation('TWSE',code,factor,v,day,'TWSE T86'))
    return result

def parse_tpex(payload,day):
    # TPEx OpenAPI may publish a latest-only snapshot; require explicit row date.
    if not isinstance(payload,list): raise ValueError('TPEx format changed')
    out=[]
    rejected=Counter()
    samples={}
    for r in payload:
        if not isinstance(r,dict):
            rejected["non_dict"]+=1;continue
        def field(*names):
            for n in names:
                if n in r and str(r[n]).strip(): return r[n]
            return None
        d=norm_date(field('Date','日期','資料日期','交易日期','date'))
        if d!=day:
            rejected["date_mismatch"]+=1;samples.setdefault("date_mismatch",repr(field("Date","日期","資料日期","交易日期","date")));continue
        code=str(field('SecuritiesCompanyCode','證券代號','股票代號','代號','Code') or '').strip()
        if not re.fullmatch(r'[1-9]\d{3}',code):
            rejected['invalid_code']+=1;samples.setdefault('invalid_code',repr(code));continue
        # TPEx uses human-readable English column labels with spaces and punctuation.
        # Normalize exact known aliases only; never guess from Buy/Sell columns.
        def canonical(name):
            return re.sub(r'[^a-z0-9\u4e00-\u9fff]', '', str(name).lower())
        columns={canonical(k):v for k,v in r.items()}
        def net(*aliases):
            for alias in aliases:
                key=canonical(alias)
                if key in columns and str(columns[key]).strip():
                    return columns[key]
            return None
        vals=[
            net('Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference',
                'ForeignInvestorsIncludeMainlandAreaInvestors-TotalDifference',
                'ForeignInvestorsNetBuySell', '外資及陸資買賣超股數', '外資買賣超股數'),
            net('SecuritiesInvestmentTrustCompanies-Difference',
                'SecuritiesInvestmentTrustCompanies-TotalDifference',
                'InvestmentTrustNetBuySell', '投信買賣超股數'),
            net('Dealers-Difference', 'Dealers-TotalDifference',
                'DealerNetBuySell', '自營商買賣超股數'),
        ]
        if any(v is None for v in vals):
            rejected['missing_field']+=1;samples.setdefault('missing_field',repr(vals));continue
        nums=[num(v) for v in vals]
        if any(v is None for v in nums):
            rejected['invalid_number']+=1;samples.setdefault('invalid_number',repr(vals));continue
        rejected['accepted']+=1
        # Official TPEx fields are reported as shares; preserve signed difference.
        for f,v in zip(('foreign_net_shares','trust_net_shares','dealer_net_shares'),nums):
            out.append(observation('TPEX',code,f,v,day,'TPEx OpenAPI'))
    print(f'V2.5.3 TPEx parse audit: {dict(rejected)}; samples={samples}')
    return out

def market_factors(rows, day):
    grouped=defaultdict(dict)
    for r in rows:
        market=r.get('market'); code=str(r.get('code',''))
        if market not in ('TWSE','TPEX') or not re.fullmatch(r'[1-9]\d{3}',code):continue
        d=r.get('date'); close=num(r.get('close'))
        if d and d<=day and close and close>0: grouped[(market,code)][d]=close
    result=[]
    for market in ('TWSE','TPEX'):
        one=[]; five=[]; advancing=0; declining=0
        for (m,code),series in grouped.items():
            if m!=market or day not in series:continue
            dates=sorted(series)
            if len(dates)<2:continue
            if dates[-1]!=day:continue
            c=series[day]; p=series[dates[-2]]
            r1=(c/p-1)*100
            one.append(r1)
            advancing+=r1>0; declining+=r1<0
            if len(dates)>=6:
                five.append((c/series[dates[-6]]-1)*100)
        if len(one)>=20:
            result.append(observation(market,'ALL','market_return_1d_pct',sum(one)/len(one),day,'scanner OHLCV equal-weight'))
            result.append(observation(market,'ALL','market_advance_ratio',advancing/(advancing+declining),day,'scanner OHLCV breadth')) if advancing+declining else None
        if len(five)>=20:
            result.append(observation(market,'ALL','market_return_5d_pct',sum(five)/len(five),day,'scanner OHLCV equal-weight'))
        print(f'V2.5.2 {market} breadth: one_day={len(one)}, five_day={len(five)}')
    return result

def load_existing():
    if not PATH.exists():return []
    with PATH.open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))

def collect(rows):
    days={m:sorted({r['date'] for r in rows if r.get('market')==m}) for m in ('TWSE','TPEX')}
    if not all(days.values()):
        print('V2.5.2: no valid market sessions, skipping');return
    latest=min(days['TWSE'][-1],days['TPEX'][-1])
    # No retroactive market lookahead. On first run, backfill TWSE T86 last 5 sessions.
    observations=market_factors(rows,latest)
    historical={r.get('observation_date') for r in load_existing() if r.get('factor')=='foreign_net_shares' and r.get('market')=='TWSE'}
    fetch_days=[d for d in days['TWSE'] if d<=latest][-5:]
    for d in fetch_days:
        if d in historical:continue
        try:
            found=parse_twse(get_json(TWSE_URL.format(d.replace('-',''))),d)
            observations.extend(found)
            print(f'V2.5.2 TWSE T86 {d}: {len(found)//3} stocks')
        except Exception as e:
            print(f'V2.5.2 WARN TWSE T86 {d}: {e}')
    try:
        payload=get_json(TPEX_URL)
        found=parse_tpex(payload,latest)
        if not found and isinstance(payload,list) and payload:
            sample=payload[0]
            print(f'V2.5.3 TPEx schema diagnostic: rows={len(payload)}; fields={list(sample) if isinstance(sample,dict) else type(sample).__name__}; date_sample={[(k,v) for k,v in sample.items() if any(t in k.lower() for t in ("date","日期","交易日"))][:3] if isinstance(sample,dict) else []}')
        observations.extend(found)
        print(f'V2.5.2 TPEx institution {latest}: {len(found)//3} stocks (date-verified)')
    except Exception as e:
        print(f'V2.5.2 WARN TPEx institution: {e}')
    existing=load_existing()
    index={(r.get('market'),r.get('code'),r.get('factor'),r.get('observation_date')):r for r in existing if r.get('market') and r.get('factor')}
    for r in observations:
        index[(r['market'],r['code'],r['factor'],r['observation_date'])]=r
    PATH.parent.mkdir(parents=True,exist_ok=True)
    with PATH.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction='ignore');w.writeheader();w.writerows(sorted(index.values(),key=lambda r:(r.get('observation_date',''),r.get('market',''),r.get('code',''),r.get('factor',''))))
    print(f'V2.5.2 observations: added_or_updated={len(observations)}, cumulative={len(index)}; asof={latest}')
    return len(observations)
