"""以合并交易账单重建交易账户日权益；历史记录、API 覆盖与行情分别核对。"""
from collections import defaultdict
from decimal import Decimal
from datetime import datetime, timedelta
import json
import math
import statistics
import time
import history_store as store

DAY=86400000
ZERO=Decimal(0)
D=store.dec


def prepare(account,directory):
    meta=json.loads((directory/'metadata.json').read_text())
    assert all(meta['sources'].get(k,{}).get('complete') for k in ['bills','fills_SWAP']), '本次核心 API 数据不完整，保留已保存净值'
    bills=store.effective(account,'bills')
    assert bills, '没有可重建的交易账单'
    begin=min(int(b['ts']) for b in bills)
    with store.connect() as db:
        windows=db.execute("SELECT begin,end FROM file_windows WHERE account=? AND dataset='bills' UNION SELECT begin,end FROM windows WHERE account=? AND dataset='bills' AND complete=1",(account,account)).fetchall()
    covering=[a for a,b in windows if a<=begin<=b]
    assert covering, '历史文件缺少导出起止日期，请重新导入原始交易账单'
    begin=min(covering);cursor=begin
    for a,b in sorted(windows):
        if a>cursor+1000:break
        if b>=cursor:cursor=max(cursor,b)
    assert cursor>=meta['end_ms']-1000, '历史文件与 API 覆盖之间存在账单缺口，未连接缺口两端的净值'
    assert not any(b.get('instType') in {'FUTURES','OPTION'} or (b.get('instType')=='MARGIN' and b.get('label')!='穿仓代偿') for b in bills), '历史中存在尚不支持的杠杆、交割或期权记录'
    assert max(int(b['ts']) for b in bills)<=meta['end_ms'], '导入账单晚于当前 API 截止时间，需核对文件日期'
    histories=store.effective(account,'import_history_positions')
    snapshots=store.effective(account,'import_positions')
    positions=json.loads((directory/'positions.json').read_text())
    assert all(p.get('instType')=='SWAP' and p.get('posSide')=='net' for p in positions), '历史净值仅支持净持仓模式的永续合约'
    contracts={};intervals=[]
    for r in histories+snapshots:
        inst=r['交易产品']
        if not inst.endswith('-USDT-SWAP'):continue
        val=D(r.get('合约面值'))*D(r.get('合约乘数') or '1')
        if inst in contracts:assert contracts[inst]==val, '历史合约面值发生变化，需进一步复核'
        contracts[inst]=val
        mode={'全仓':'cross','逐仓':'isolated','cross':'cross','isolated':'isolated'}.get(r['保证金模式'])
        start=store.stamp(r['仓位创建时间']);end=store.stamp(r.get('快照时间') or r['仓位更新时间'])
        intervals.append((inst,start,end,mode))
    for path in directory.glob('instrument_*.json'):
        rows=json.loads(path.read_text())
        if not rows:continue
        r=rows[0]
        if r.get('ctType')=='linear' and r.get('settleCcy')=='USDT':
            val=D(r['ctVal'])*D(r.get('ctMult') or '1')
            if r['instId'] in contracts:assert contracts[r['instId']]==val, '当前与历史合约面值不一致'
            contracts[r['instId']]=val
    events=[]
    for b in bills:
        r=dict(b);r['ts']=int(r['ts']);r['trade']=False
        if r.get('instType')=='SWAP' and r.get('type')=='2':
            inst=r['instId'];assert inst.endswith('-USDT-SWAP') and contracts.get(inst,0)>0, '缺少历史线性合约面值'
            label=r.get('label','');side={'buy':1,'sell':-1}.get(r.get('side'))
            if side is None:
                sub=str(r.get('subType',''))
                side=1 if label in {'买入','强平买入'} or sub in {'1','3','6','101','102','105','106'} else -1 if label in {'卖出','强平卖出'} or sub in {'2','4','5','100','103','104','107'} else None
            assert side is not None, '历史成交缺少可确认的买卖方向'
            r['q']=D(r.get('sz'))*side;r['px']=D(r.get('px'));assert r['q'] and r['px']>0, '历史账单缺少成交数量或价格，请重新导入原始交易账单'
            mode=r.get('mgnMode')
            if mode not in {'cross','isolated'}:
                candidates={m for i,a,z,m in intervals if i==inst and a<=r['ts']<=z and m}
                if len(candidates)==1:mode=candidates.pop()
                elif D(r.get('posBalChg'))!=0:mode='isolated'
                elif label.startswith('强平') and all(a>r['ts'] for i,a,z,m in intervals if i==inst):mode='initial'
                else:raise AssertionError('历史成交保证金模式无法唯一确认')
            r.update(trade=True,key=(inst,mode))
        elif r.get('instType')=='SWAP' and D(r.get('posBalChg')):
            inst=r['instId'];r['key']=(inst,r.get('mgnMode') or 'isolated')
        events.append(r)
    events.sort(key=lambda r:(r['ts'],int(r['billId'])))
    return {'begin':begin,'end':meta['end_ms'],'events':events,'contracts':contracts,'positions':positions,
            'balance':json.loads((directory/'balance.json').read_text())[0], 'sourceRows':len(bills)}


def fetch_prices(client,directory,model):
    """公开日行情按实际历史跨度分页，落本机缓存；不把账户信息发给行情接口。"""
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS daily_prices(site TEXT,inst TEXT,ts INTEGER,payload TEXT,PRIMARY KEY(site,inst,ts))')
    insts={r['key'][0] for r in model['events'] if r.get('key')}|{r['instId'] for r in model['positions']}
    currencies={r['ccy'] for r in model['events']}|{r['ccy'] for r in model['balance']['details']}
    pairs=[(i,'mark_candles') for i in sorted(insts)]+[(c+'-USDT','spot_candles') for c in sorted(currencies-{'USDT'})]
    result={};failures=[]
    for inst,endpoint in pairs:
        with store.connect() as db:
            stored=db.execute('SELECT payload FROM daily_prices WHERE site=? AND inst=?',(client.host,inst)).fetchall()
        rows={int(json.loads(r[0])[0]):json.loads(r[0]) for r in stored}
        after=model['end'];requests=0
        while after>=model['begin']-DAY and requests<200:
            # 已确认的历史 K 线无需每次重取。最近未缓存部分先补，再向更早分页。
            day=(after+8*3600000)//DAY*DAY-8*3600000
            if day in rows and day-DAY in rows:
                after=day-DAY;continue
            try:batch=client.get(endpoint,{'instId':inst,'bar':'1D','after':str(after),'limit':'100'})
            except RuntimeError:
                failures.append(inst);break
            requests+=1
            if not batch:break
            oldest=min(int(r[0]) for r in batch)
            if oldest>=after:break
            confirmed=[r for r in batch if r[-1]=='1']
            with store.connect() as db:
                for row in confirmed:
                    rows[int(row[0])]=row
                    db.execute('INSERT OR REPLACE INTO daily_prices VALUES(?,?,?,?)',(client.host,inst,int(row[0]),json.dumps(row)))
            after=oldest-1;time.sleep(.2)
        result[inst]={ts+DAY:D(row[4]) for ts,row in rows.items()}
        print('candles_'+inst,'历史行情复核',flush=True)
    return result,failures


def reconstruct(model,prices):
    events=model['events'];contracts=model['contracts'];positions=model['positions']
    assert all(D(r.get('liab'))==0 for r in model['balance']['details']), '当前存在币种负债，暂无法可靠重建历史交易账户净值'
    current={};states={};cash={r['ccy']:D(r['cashBal']) for r in model['balance']['details']};collateral=defaultdict(lambda:ZERO)
    for p in positions:current[(p['instId'],p['mgnMode'])]={'q':D(p['pos']),'avg':D(p['avgPx'])}
    assert not any(p['mgnMode']=='isolated' for p in positions), '当前逐仓保证金未取得可靠余额，暂无法重建全历史'
    states={k:dict(v) for k,v in current.items()}
    # 从当前持仓逆推期初数量与成本；平仓实现盈亏提供期初成本约束。
    for r in reversed(events):
        cash[r['ccy']]=cash.get(r['ccy'],ZERO)-D(r.get('balChg'))
        if r.get('key'):collateral[r['key']]-=D(r.get('posBalChg'))
        if not r['trade']:continue
        s=states.setdefault(r['key'],{'q':ZERO,'avg':ZERO});after=s['q'];before=after-r['q'];px=r['px']
        if before and before*r['q']<0:
            close=min(abs(before),abs(r['q']));s['avg']=px-D(r.get('pnl'))/(close*contracts[r['key'][0]]*(1 if before>0 else -1))
        elif before:
            s['avg']=(abs(after)*s['avg']-abs(r['q'])*px)/abs(before)
        else:s['avg']=ZERO
        s['q']=before
        assert not before or s['avg']>0, '期初持仓成本无法复核'
    initial={k:dict(v) for k,v in states.items()};out=[];cursor=0;max_error=ZERO;balance_checks=0;missing=[];balance_bad=[];last_good=None
    rounds=[];episodes={};last_closed={};round_issues=[]
    def episode(key,stamp,q,censored=False):
        return {'key':key,'start':stamp,'direction':'多' if q>0 else '空','gross':ZERO,'fees':ZERO,'funding':ZERO,'adjustment':ZERO,'censored':censored}
    for key,v in initial.items():
        if v['q']:episodes[key]=episode(key,model['begin'],v['q'],True)
    def close_episode(key,stamp):
        e=episodes.pop(key);e['end']=stamp;rounds.append(e);last_closed[key]=e
    def apply_batch(batch):
        nonlocal max_error,balance_checks,last_good
        for r in batch:
            cash[r['ccy']]=cash.get(r['ccy'],ZERO)+D(r.get('balChg'))
            if r.get('key'):collateral[r['key']]+=D(r.get('posBalChg'))
            if not r['trade']:
                inst=r.get('instId');funding=D(r.get('pnl')) if r.get('type')=='8' else ZERO
                fee=D(r.get('fee'));adjustment=D(r.get('adjustment'))
                if inst and (funding or fee or adjustment):
                    candidates=[e for k,e in episodes.items() if k[0]==inst]
                    if not candidates:candidates=[e for k,e in last_closed.items() if k[0]==inst and e['end']==r['ts']]
                    if len(candidates)==1:
                        candidates[0]['funding']+=funding;candidates[0]['fees']+=fee;candidates[0]['adjustment']+=adjustment
                    elif candidates:round_issues.append('账单费用无法唯一归属交易轮次')
                continue
            s=states[r['key']];q=s['q'];delta=r['q'];px=r['px']
            if q*delta<0:
                expected=(px-s['avg'])*min(abs(q),abs(delta))*contracts[r['key'][0]]*(1 if q>0 else -1)
                max_error=max(max_error,abs(expected-D(r.get('pnl'))))
            else:assert abs(D(r.get('pnl')))<Decimal('.00001'), '开仓账单出现非零平仓盈亏'
            nxt=q+delta
            key=r['key'];fee=D(r.get('fee'));adjustment=D(r.get('adjustment'))
            if not q:
                episodes[key]=episode(key,r['ts'],delta)
                episodes[key]['fees']+=fee;episodes[key]['adjustment']+=adjustment
            elif q*delta>0:
                episodes[key]['fees']+=fee;episodes[key]['adjustment']+=adjustment
            else:
                fraction=min(abs(q),abs(delta))/abs(delta);e=episodes[key]
                e['gross']+=D(r.get('pnl'));e['fees']+=fee*fraction;e['adjustment']+=adjustment*fraction
                if not nxt or q*nxt<0:close_episode(key,r['ts'])
                if q*nxt<0:
                    episodes[key]=episode(key,r['ts'],nxt)
                    episodes[key]['fees']+=fee*(1-fraction);episodes[key]['adjustment']+=adjustment*(1-fraction)
            if not q or q*delta>0:s['avg']=(abs(q)*s['avg']+abs(delta)*px)/(abs(q)+abs(delta))
            elif q*nxt<0:s['avg']=px
            elif not nxt:s['avg']=ZERO
            s['q']=nxt
        # 同毫秒整批结算余额：最终值必须出现在该批提供的余额中。
        for ccy in {r['ccy'] for r in batch}:
            reported=[D(r['bal']) for r in batch if r['ccy']==ccy and r.get('bal') not in {None,''}]
            if reported:
                if min(abs(cash[ccy]-v) for v in reported)>=Decimal('.000001'):balance_bad.append(batch[0]['ts'])
                else:balance_checks+=1;last_good=batch[0]['ts']
    def advance(boundary):
        nonlocal cursor
        flows=[]
        while cursor<len(events) and events[cursor]['ts']<boundary:
            stamp=events[cursor]['ts'];batch=[]
            while cursor<len(events) and events[cursor]['ts']==stamp:batch.append(events[cursor]);cursor+=1
            flows.extend(r for r in batch if r.get('type')=='1');apply_batch(batch)
        return flows
    first=(model['begin']+8*3600000)//DAY*DAY-8*3600000+DAY
    last=(model['end']+8*3600000)//DAY*DAY-8*3600000
    for boundary in range(first,last+1,DAY):
        flows=advance(boundary);value=ZERO;floating=ZERO;flow=ZERO;weighted_flow=ZERO;reason=[]
        for ccy,q in cash.items():
            if abs(q)<Decimal('.0000000001'):continue
            px=Decimal(1) if ccy=='USDT' else prices.get(ccy+'-USDT',{}).get(boundary)
            if px is None:reason.append('缺少 '+ccy+' 现货收盘价')
            else:value+=q*px
        for (inst,mode),s in states.items():
            if not s['q']:continue
            px=prices.get(inst,{}).get(boundary)
            if px is None:reason.append('缺少 '+inst+' 标记价')
            else:floating+=s['q']*contracts[inst]*(px-s['avg'])
        for r in flows:
            px=Decimal(1) if r['ccy']=='USDT' else prices.get(r['ccy']+'-USDT',{}).get(boundary)
            if px is None:reason.append('缺少划转币种估值')
            else:
                cash_flow=D(r['balChg'])*px;flow+=cash_flow
                weighted_flow+=cash_flow*Decimal(boundary-r['ts'])/Decimal(DAY)
        date=store.date(boundary-DAY)[:10]
        if reason:missing.append({'日期':date,'原因':'；'.join(sorted(set(reason)))})
        equity=value+floating+sum(collateral.values())
        out.append({'日期':date,'重建权益':None if reason else float(equity),'外部划转估值':None if reason else float(flow),'资金流加权估值':None if reason else float(weighted_flow),'日收益率':None,'净值指数':None,'回撤':None})
    advance(model['end']+1)
    assert max_error<Decimal('.00001'), '历史平仓盈亏与数量、价格、成本不符'
    for k,s in states.items():
        end=current.get(k,{'q':ZERO,'avg':ZERO});assert abs(s['q']-end['q'])<Decimal('.000000001'), '当前持仓数量无法闭合'
        if end['q']:assert abs(s['avg']-end['avg'])<Decimal('.00001'), '当前开仓均价无法闭合'
    assert abs(sum(collateral.values()))<Decimal('.000001'), '逐仓保证金余额无法闭合'
    assert balance_checks, '缺少独立余额校验字段，请重新导入原始交易账单'
    if balance_bad:
        assert last_good is not None and last_good>max(balance_bad), '交易账户余额连续性不符，可能缺少历史流水'
        cutoff=max(balance_bad)
        for row in out:
            if store.stamp(row['日期']+' 00:00:00')<=cutoff:
                row['重建权益']=None;row['外部划转估值']=None
                missing.append({'日期':row['日期'],'原因':'余额连续性未通过，期初权益无法确认'})
    # 零权益、负权益或缺价后开启新段；跨段不得拼接收益率、指数或回撤。
    segments=[];segment=[];index=peak=1.0
    for row in out:
        v=row['重建权益']
        if v is None or v<=0:
            if segment:segments.append(segment)
            segment=[];index=peak=1.0;continue
        if segment:
            capital=segment[-1]['重建权益']+row['资金流加权估值']
            ret=(v-segment[-1]['重建权益']-row['外部划转估值'])/capital if capital>0 else None
            if ret is None or ret<=-1:
                segments.append(segment);segment=[];index=peak=1.0
            else:row['日收益率']=ret;index*=1+ret
        row['净值指数']=index;peak=max(peak,index);row['回撤']=index/peak-1;segment.append(row)
    if segment:segments.append(segment)
    selected=segments[-1] if segments and segments[-1][-1] is out[-1] else []
    returns=[r['日收益率'] for r in selected if r['日收益率'] is not None]
    # 只在最近连续有效段显示风险图；权益图仍保留更早日值及缺口。
    selected_dates={r['日期'] for r in selected}
    for row in out:
        if row['日期'] not in selected_dates:row.update(日收益率=None,净值指数=None,回撤=None)
    grouped={}
    for gap in missing:
        grouped.setdefault(gap['日期'],set()).add(gap['原因'])
    missing=[{'日期':date,'原因':'；'.join(sorted(reasons))} for date,reasons in sorted(grouped.items())]
    sharpe=statistics.mean(returns)/statistics.stdev(returns)*math.sqrt(365) if len(returns)>1 and statistics.stdev(returns)>0 else None
    round_rows=[{'合约':e['key'][0],'方向':e['direction'],'模式':e['key'][1],
                 '开仓时间':store.date(e['start']),'平仓时间':store.date(e['end']),
                 '持仓小时':(e['end']-e['start'])/3600000,'毛盈亏':float(e['gross']),
                 '手续费':float(e['fees']),'资金费':float(e['funding']),'其他调整':float(e['adjustment']),
                 '净盈亏':float(e['gross']+e['fees']+e['funding']+e['adjustment']),'跨期':e['censored']} for e in rounds]
    return {'rounds':round_rows,'roundRange':{'start':store.date(model['begin']),'end':store.date(model['end'])},'roundAvailable':not round_issues,
            'returnMethod':'daily-modified-dietz-v1','daily':out,'riskAvailable':len(returns)>1,'riskRange':{'start':selected[0]['日期'],'end':selected[-1]['日期']} if selected else None,
            'summary':{'重建年化夏普':sharpe,'重建日收益样本数':len(returns),'重建日收盘最大回撤':min((r['回撤'] for r in selected),default=None),'重建年化索提诺':statistics.mean(returns)/math.sqrt(sum(min(r,0)**2 for r in returns)/len(returns))*math.sqrt(365) if returns and any(r<0 for r in returns) else None,'重建期间累计收益率':selected[-1]['净值指数']-1 if selected else None},
            'equityRange':{'start':out[0]['日期'],'end':out[-1]['日期']} if out else None,
            'equityHistory':{'state':'partial' if missing else 'complete','missing':missing,'segments':len(segments),'message':'全历史日权益已按合并账单重建。'+('存在未通过复核的日期，曲线按缺口断开。' if missing else '')+('风险指标仅计算最近连续正权益区间。' if len(segments)>1 else '')},
            'validations':{'历史余额校验批次':balance_checks,'未通过余额校验批次':len(balance_bad),'平仓盈亏最大误差':float(max_error),'当前持仓数量和成本闭合':True,'期初持仓数量':{str(k):str(v['q']) for k,v in initial.items()}}}


def extend(account,directory,client,recent):
    result=dict(recent);result['roundRange']=recent.get('range');result['roundAvailable']=bool(recent.get('riskAvailable'))
    try:
        model=prepare(account,directory)
        prices,failed=fetch_prices(client,directory,model)
        full=reconstruct(model,prices)
        result.update({k:v for k,v in full.items() if k not in {'summary','validations'}})
        result['summary']={**recent.get('summary',{}),**full['summary']}
        result['historyValidations']=full['validations']
        result['riskReason']='最近连续区间不足两个日收益样本' if not result['riskAvailable'] else ''
        result['caveats']=list(recent.get('caveats',[]))+['日权益含历史强平成交与账单余额调整。账户资金划转不计为投资收益。日收益采用按划转实际时间加权的 Modified Dietz 估算，未取得每笔划转时的账户估值，不是精确时间加权收益；大额资金流附近误差可能较大。', '历史余额按同毫秒整批账单核对，持仓数量、开仓成本与当前 API 持仓核对。', '交易轮次从合并账单逐笔复核；期初持仓与所选日期前已开仓的轮次不计入胜率。']
    except (AssertionError,KeyError,ValueError,ArithmeticError,OSError) as error:
        reason=str(error) if isinstance(error,AssertionError) else '历史重建所需字段或数据尚未齐全'
        result['equityHistory']={'state':'unavailable','message':'全历史净值未通过复核：'+reason+'。图表继续显示已复核的近期数据。','missing':[]}
    return result
