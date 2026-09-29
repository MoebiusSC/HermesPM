"""Read-only public API adapter, bounded paging and conservative fee verification."""
import json, math, threading, time, urllib.parse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
DATA='https://data-api.polymarket.com'
CLOB='https://clob.polymarket.com'
GAMMA='https://gamma-api.polymarket.com'
_cache={};_lock=threading.Lock()


def get(host,path,params=None):
    url=host+path+('?' + urllib.parse.urlencode(params or {},doseq=True) if params else '')
    req=urllib.request.Request(url,headers={'User-Agent':'HermesPM/2.0','Accept':'application/json'})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req,timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code not in (429,500,502,503,504) or attempt: raise
            time.sleep(min(2,float(e.headers.get('Retry-After','1'))))
        except (TimeoutError,OSError):
            if attempt: raise
            time.sleep(.3)


def listing(value):
    if isinstance(value,list): return value
    if isinstance(value,dict) and isinstance(value.get('data'),list): return value['data']
    raise ValueError('unexpected API schema')


def paged(path,params,size=100,pages=10):
    rows=[]
    for page in range(pages):
        part=listing(get(DATA,path,{**params,'limit':size,'offset':page*size}))
        rows.extend(part)
        if len(part)<size: return rows,True
    return rows,False


def positions(address):
    rows,complete=paged('/positions',{'user':address,'sizeThreshold':0},500,10)
    if not complete: raise ValueError('positions pagination cap; baseline unavailable')
    return {str(p['asset']):float(p['size']) for p in rows if float(p.get('size') or 0)>0}


def trades(address,cursor,now):
    # Overlap accommodates delayed indexing; older items deduplicated by engine.
    rows,complete=paged('/trades',{'user':address,'takerOnly':'false','start':max(0,cursor-300),'end':now},100,10)
    if not complete: raise ValueError('trade pagination cap; cursor retained, entries blocked')
    legs={}
    for t in rows:
        tx=t.get('transactionHash')
        if tx:legs.setdefault(tx,set()).add(str(t.get('asset')))
    for t in rows:
        if len(legs.get(t.get('transactionHash'),set()))>1:
            t['_complex_reason']='transacción con varios activos; copia combinada no soportada'
    return sorted(rows,key=lambda t:(int(t.get('timestamp') or 0),str(t.get('transactionHash','')),str(t.get('asset',''))))



def complex_activity(address,cursor,now):
    rows,complete=paged('/activity',{'user':address,'type':'CONVERSION,SPLIT,MERGE','start':max(0,cursor-300),'end':now,'sortBy':'TIMESTAMP','sortDirection':'ASC'},100,10)
    if not complete:raise ValueError('activity pagination cap; complex strategy check incomplete')
    return [r for r in rows if r.get('type') in ('CONVERSION','SPLIT','MERGE')]


def market_risk(m,condition):
    events=m.get('events') or []
    # Use the canonical parent event ID across all outcome markets.
    event_id=events[0].get('id') if len(events)==1 else None
    event_slug=events[0].get('slug') if len(events)==1 else None
    neg=bool(m.get('negRisk'))
    event=('event:'+str(event_id)) if event_id is not None else (str(event_slug) if event_slug else condition)
    augmented=bool(m.get('negRiskAugmented')) or any(bool(x.get('negRiskAugmented')) for x in events)
    reason='mercado neg-risk aumentado; estructura no soportada' if augmented else ''
    if neg and not (event_id is not None or event_slug):reason='neg-risk sin evento padre verificable'
    return dict(event=event,neg_risk=neg,complex=bool(reason),risk_reason=reason)

def discovery():
    def batch(offset): return listing(get(DATA,'/v1/leaderboard',{'category':'OVERALL','timePeriod':'MONTH','orderBy':'PNL','limit':50,'offset':offset}))
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches=list(pool.map(batch,[0,50,100,150]))
    unique={str(w['proxyWallet']).lower():w for rows in batches for w in rows}
    return list(unique.values())


def evaluate(w):
    address=str(w['proxyWallet']).lower()
    closed=listing(get(DATA,'/closed-positions',{'user':address,'limit':50,'offset':0,'sortBy':'TIMESTAMP','sortDirection':'DESC'}))
    values=[float(x.get('realizedPnl') or 0) for x in closed]
    stamps=[int(x.get('timestamp') or 0) for x in closed if x.get('timestamp')]
    profits=sum(max(v,0) for v in values); losses=-sum(min(v,0) for v in values)
    concentration=max([0]+values)/profits if profits else 1.
    span=(max(stamps)-min(stamps))/86400 if len(stamps)>1 else 0
    weeks={int(x.get('timestamp',0))//604800 for x in closed if float(x.get('realizedPnl') or 0)>0}
    eligible=len(values)>=30 and span>=30 and sum(values)>0 and concentration<.5 and len(weeks)>=3
    score=round(min(len(values)/50,1)*20+min(span/90,1)*20+(1-concentration)*20+min(len(weeks)/8,1)*20+20*profits/(profits+losses+1),1)
    return dict(address=address,label=str(w.get('userName') or address[:10])[:60],pnl=float(w.get('pnl') or 0),volume=float(w.get('vol') or 0),score=score,sample_count=len(values),span_days=span,concentration=concentration,eligible=int(eligible),reason='muestra parcial de 50 cierres; elegible para experimento' if eligible else 'evidencia insuficiente o beneficio concentrado',updated=int(time.time()))


def metadata(condition):
    with _lock:
        hit=_cache.get(condition)
        if hit and time.time()-hit[0]<300:return hit[1]
    rows=listing(get(GAMMA,'/markets',{'condition_ids':condition}))
    match=next((m for m in rows if m.get('conditionId')==condition),None)
    if not match: raise ValueError('market metadata unavailable')
    with _lock:_cache[condition]=(time.time(),match)
    return match


def read_list(value):
    return json.loads(value) if isinstance(value,str) else (value or [])


def quote(asset,condition):
    m=metadata(condition)
    schedule=m.get('feeSchedule') or {}
    rate=0. if m.get('feesEnabled') is False else None
    exponent=1.
    if m.get('feesEnabled') is True and 'rate' in schedule and 'exponent' in schedule:
        r=float(schedule['rate']);e=float(schedule['exponent'])
        if 0<=r<=1 and 0<=e<=5:rate=r;exponent=e
    tokens=read_list(m.get('clobTokenIds'));prices=read_list(m.get('outcomePrices'))
    settlement=None
    # Closed alone does not imply final resolution. Require explicit resolved status.
    if m.get('closed') is True and str(m.get('umaResolutionStatus','')).lower()=='resolved' and asset in tokens and len(tokens)==len(prices):
        payouts=[float(p) for p in prices]
        if all(p in (0.,.5,1.) for p in payouts) and abs(sum(payouts)-1)<1e-6:settlement=payouts[tokens.index(asset)]
    result=dict(ts=int(time.time()),asks=[],bids=[],fee_rate=rate,fee_exponent=exponent,settlement=settlement,scheduled_end=m.get('endDate'),resolution_status=str(m.get('umaResolutionStatus') or ''),market_closed=bool(m.get('closed')),**market_risk(m,condition))
    if settlement is not None:return result
    book=get(CLOB,'/book',{'token_id':asset})
    for key in ('asks','bids'):
        result[key]=[[float(l['price']),float(l['size'])] for l in book.get(key,[]) if math.isfinite(float(l['price'])) and math.isfinite(float(l['size']))]
    result['min_order_size']=float(book.get('min_order_size') or m.get('orderMinSize') or 0)
    raw=book.get('timestamp')
    if raw:
        stamp=float(raw);stamp=stamp/1000 if stamp>1e12 else stamp
        if abs(time.time()-stamp)>120:raise ValueError('stale orderbook timestamp')
    # Conservative fallback only if endpoint explicitly says zero fee. Never guess positive schedules.
    if rate is None:
        f=get(CLOB,'/fee-rate',{'token_id':asset})
        if f.get('base_fee') is not None and float(f['base_fee'])==0:result['fee_rate']=0.
    result['ts']=int(time.time())
    return result
