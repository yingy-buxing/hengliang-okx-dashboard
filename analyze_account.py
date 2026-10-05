"""从本地只读快照重建账户指标；不读取密钥，不连接外网。"""
from pathlib import Path
from decimal import Decimal as D
from datetime import datetime, timedelta, timezone
from collections import defaultdict, Counter
import json
import math
import statistics

ROOT = Path(__file__).resolve().parent
DATA = ROOT / '.okx-analysis'
TZ = timezone(timedelta(hours=8))
ZERO = D(0)
DAY = 86400000


def load(name):
    return json.loads((DATA / (name + '.json')).read_text())


def decimal(value):
    return D(str(value or 0))


def date(ts, fmt='%Y-%m-%d %H:%M:%S'):
    return datetime.fromtimestamp(int(ts) / 1000, TZ).strftime(fmt)


def number(value):
    return float(value) if value is not None else None


def run():
    meta = load('metadata')
    bills = sorted(load('bills'), key=lambda r: (int(r['ts']), int(r['billId'])))
    fills = sorted(load('fills_SWAP'), key=lambda r: (int(r['fillTime']), int(r['billId'])))
    positions = load('positions')
    history = load('history_positions')
    balance = load('balance')[0]
    trade_bills = {r['billId']: r for r in bills if r['type'] == '2' and r['instType'] == 'SWAP'}
    assert set(trade_bills) == {r['billId'] for r in fills}, '合约流水与成交未完全匹配'
    instruments = {r['instId']: load('instrument_' + r['instId'])[0] for r in fills}
    assert all(r['ctType'] == 'linear' and r['settleCcy'] == 'USDT' for r in instruments.values())
    assert all(r['posSide'] == 'net' for r in fills)
    assert all(r['feeCcy'] == 'USDT' for r in fills)
    contract = {k: decimal(v['ctVal']) * decimal(v.get('ctMult') or 1) for k, v in instruments.items()}
    current_q = defaultdict(lambda: ZERO)
    for r in positions:
        current_q[(r['instId'], r['mgnMode'])] += decimal(r['pos'])
    delta_q = defaultdict(lambda: ZERO)
    for r in fills:
        key = (r['instId'], trade_bills[r['billId']]['mgnMode'])
        delta_q[key] += decimal(r['fillSz']) * (1 if r['side'] == 'buy' else -1)
    initial_q = {k: current_q[k] - v for k, v in delta_q.items()}
    states, closed = {}, []
    initial_entries = {}
    for key, qty in initial_q.items():
        avg = ZERO
        if qty:
            candidates = sorted([r for r in history if (r['instId'], r['mgnMode']) == key], key=lambda r:int(r['uTime']))
            assert candidates, '缺少期初持仓均价'
            first = candidates[0]
            avg = decimal(first['openAvgPx'])
            assert decimal(first['closeTotalPos']) == abs(qty), '期初持仓量无法核对'
            initial_entries[str(key)] = {'quantity':str(qty), 'average':str(avg), 'source':'first historical close record'}
        states[key] = {'q': qty, 'avg': avg, 'episode': None}
        if qty:
            states[key]['episode'] = {'inst':key[0], 'mode':key[1], 'direction':'多' if qty>0 else '空',
                'start':meta['begin_ms'], 'gross':ZERO,'fee':ZERO,'funding':ZERO,'fill_count':0,
                'notional':ZERO,'left_censored':True}

    cash = {r['ccy']: decimal(r['cashBal']) for r in balance['details']}
    for r in bills:
        cash[r['ccy']] = cash.get(r['ccy'], ZERO) - decimal(r['balChg'])
    initial_cash = dict(cash)
    isolated = defaultdict(lambda:ZERO)
    all_keys = set(initial_q)
    events = sorted(bills, key=lambda r:(int(r['ts']), int(r['billId'])))
    fills_by_id = {r['billId']:r for r in fills}
    fill_errors, ledger_errors = [], []
    by_symbol = defaultdict(lambda:{'gross':ZERO,'fees':ZERO,'funding':ZERO,'fills':0,'orders':set(),'notional':ZERO,'taker':0})
    monthly = defaultdict(lambda:{'gross':ZERO,'fees':ZERO,'funding':ZERO,'fills':0})
    hourly = Counter()
    def apply(r):
        ccy = r['ccy']
        cash[ccy] = cash.get(ccy,ZERO) + decimal(r['balChg'])
        # 同一时间多行的 bal 可能是整批最终值；单行比对保留为诊断，不用于重建。
        key = (r['instId'], r['mgnMode'])
        if r['mgnMode']=='isolated':
            isolated[key] += decimal(r['posBalChg'])
        if r['instType']!='SWAP':
            return
        state = states.setdefault(key, {'q':ZERO,'avg':ZERO,'episode':None})
        if r['type']=='8':
            value=decimal(r['pnl'])
            by_symbol[key[0]]['funding']+=value
            monthly[date(r['ts'],'%Y-%m')]['funding']+=value
            if state['episode'] is not None:state['episode']['funding']+=value
            return
        if r['type']!='2':
            return
        fill = fills_by_id[r['billId']]
        qty = decimal(fill['fillSz']) * (1 if fill['side']=='buy' else -1)
        px = decimal(fill['fillPx'])
        fee = decimal(fill['fee'])
        realized=decimal(fill['fillPnl'])
        notional=abs(qty)*contract[key[0]]*px
        by=by_symbol[key[0]]
        by['gross']+=realized;by['fees']+=fee;by['fills']+=1;by['orders'].add(fill['ordId']);by['notional']+=notional
        by['taker']+=int(fill['execType']=='T')
        month=monthly[date(fill['fillTime'],'%Y-%m')]
        month['gross']+=realized;month['fees']+=fee;month['fills']+=1
        hourly[int(date(fill['fillTime'],'%H'))]+=1
        prev=state['q']
        if not prev or prev*qty>0:
            if not prev:
                state['episode']={'inst':key[0],'mode':key[1],'direction':'多' if qty>0 else '空',
                    'start':int(fill['fillTime']),'gross':ZERO,'fee':ZERO,'funding':ZERO,'fill_count':0,'notional':ZERO,'left_censored':False}
            state['avg']=(abs(prev)*state['avg']+abs(qty)*px)/(abs(prev)+abs(qty))
            state['q']=prev+qty
            e=state['episode'];e['fee']+=fee;e['fill_count']+=1;e['notional']+=notional
            assert abs(realized)<D('0.00000001'), '开仓成交出现非零实现盈亏'
        else:
            close_qty=min(abs(prev),abs(qty))
            expected=(px-state['avg'])*close_qty*contract[key[0]]*(1 if prev>0 else -1)
            fill_errors.append(abs(expected-realized))
            e=state['episode'];fraction=close_qty/abs(qty)
            e['gross']+=realized;e['fee']+=fee*fraction;e['fill_count']+=1;e['notional']+=notional*fraction
            next_q=prev+qty
            if not next_q or next_q*prev<0:
                e['end']=int(fill['fillTime']);e['net']=e['gross']+e['fee']+e['funding']
                e['hours']=None if e['left_censored'] else (e['end']-e['start'])/3600000
                closed.append(dict(e))
                state['episode']=None;state['avg']=ZERO
            if next_q*prev<0:
                state['avg']=px
                state['episode']={'inst':key[0],'mode':key[1],'direction':'多' if next_q>0 else '空',
                    'start':int(fill['fillTime']),'gross':ZERO,'fee':fee*(1-fraction),'funding':ZERO,
                    'fill_count':1,'notional':notional*(1-fraction),'left_censored':False}
            state['q']=next_q

    # 北京时间日 K 的开始时间 + 24 小时是该日收盘边界；排除未收盘 K。
    candles={}
    for path in DATA.glob('candles_*.json'):
        inst=path.stem[len('candles_'):]
        candles[inst]={int(r[0])+DAY:decimal(r[4]) for r in json.loads(path.read_text()) if r[-1]=='1'}
    first_boundary=(meta['begin_ms']+8*3600000)//DAY*DAY-8*3600000+DAY
    last_boundary=(meta['end_ms']+8*3600000)//DAY*DAY-8*3600000
    daily=[];cursor=0
    for boundary in range(first_boundary,last_boundary+1,DAY):
        transfers=[]
        while cursor<len(events) and int(events[cursor]['ts'])<boundary:
            r=events[cursor]
            if r['type']=='1':transfers.append(r)
            apply(r);cursor+=1
        cash_value=ZERO
        for ccy,quantity in cash.items():
            if not quantity:continue
            price=D(1) if ccy=='USDT' else candles.get(ccy+'-USDT',{}).get(boundary)
            assert price is not None, f'缺少 {ccy} 历史价格'
            cash_value+=quantity*price
        floating=ZERO;notional=ZERO
        for key,state in states.items():
            if not state['q']:continue
            price=candles[key[0]].get(boundary)
            assert price is not None, f'缺少 {key[0]} 历史标记价'
            floating+=state['q']*contract[key[0]]*(price-state['avg'])
            notional+=abs(state['q'])*contract[key[0]]*price
        collateral=sum(isolated.values())
        equity=cash_value+collateral+floating
        flow=ZERO
        for r in transfers:
            price=D(1) if r['ccy']=='USDT' else candles[r['ccy']+'-USDT'][boundary]
            flow+=decimal(r['balChg'])*price
        daily.append({'日期':date(boundary-DAY,'%Y-%m-%d'),'收盘时间':date(boundary),
            '现金资产':number(cash_value),'逐仓保证金':number(collateral),'浮动盈亏':number(floating),
            '重建权益':number(equity),'外部划转估值':number(flow),'持仓名义价值':number(notional)})
    # 消化截至采集时的当天事件；当前日不参与夏普计算。
    while cursor<len(events):apply(events[cursor]);cursor+=1
    for ccy,value in cash.items():
        expected=next((decimal(r['cashBal']) for r in balance['details'] if r['ccy']==ccy),ZERO)
        assert abs(value-expected)<D('0.000000001'), '现金余额无法闭合'
    assert all(abs(s['q']-current_q[k])<D('0.000000001') for k,s in states.items()), '持仓数量无法闭合'
    assert max(fill_errors,default=ZERO)<D('0.00001'), '成交实现盈亏与成本均价不符'
    assert abs(sum(isolated.values()))<D('0.000001'), '逐仓保证金未释放'
    for r in positions:
        s=states[(r['instId'],r['mgnMode'])]
        assert abs(s['avg']-decimal(r['avgPx']))<D('0.00001'), '当前开仓均价无法闭合'
    returns=[];index=1.0;peak=index
    for i,row in enumerate(daily):
        if i:
            prev=daily[i-1]['重建权益']
            ret=(row['重建权益']-row['外部划转估值'])/prev-1
            returns.append(ret);row['日收益率']=ret;index*=1+ret
        else:row['日收益率']=None
        peak=max(peak,index);row['净值指数']=index;row['回撤']=index/peak-1
    sharpe=(statistics.mean(returns)/statistics.stdev(returns)*math.sqrt(365)) if len(returns)>1 and statistics.stdev(returns)>0 else None
    sortino=(statistics.mean(returns)/math.sqrt(sum(min(r,0)**2 for r in returns)/len(returns))*math.sqrt(365)) if returns and any(r<0 for r in returns) else None
    full=[r for r in closed if not r['left_censored']]
    wins=[r['net'] for r in full if r['net']>0];losses=[r['net'] for r in full if r['net']<0]
    gross=sum(decimal(r['fillPnl']) for r in fills)
    fees=sum(decimal(r['fee']) for r in fills)
    funding=sum(decimal(r['pnl']) for r in bills if r['instType']=='SWAP' and r['type']=='8')
    summary={'采集时间':date(meta['end_ms']),'起始时间':date(meta['begin_ms']),
        '当前账户权益美元':number(decimal(balance['totalEq'])),
        '合约平仓毛盈亏USDT':number(gross),'合约手续费USDT':number(fees),
        '合约资金费USDT':number(funding),'合约期间已实现净盈亏USDT':number(gross+fees+funding),
        '完整闭合交易轮次':len(full),'跨期闭合轮次':len(closed)-len(full),
        '完整轮次胜率':len(wins)/len(full) if full else None,
        '完整轮次盈利因子':number(sum(wins)/-sum(losses)) if losses else None,
        '平均盈利USDT':number(sum(wins)/len(wins)) if wins else None,
        '平均亏损USDT':number(sum(losses)/len(losses)) if losses else None,
        '完整轮次平均净盈亏USDT':number(sum(r['net'] for r in full)/len(full)) if full else None,
        '持仓时长中位数小时':statistics.median(r['hours'] for r in full) if full else None,
        '合约成交笔数':len(fills),'合约成交订单数':len({r['ordId'] for r in fills}),
        '合约成交名义金额USDT':number(sum(r['notional'] for r in by_symbol.values())),
        'Taker成交占比':sum(r['execType']=='T' for r in fills)/len(fills),
        '重建日收益样本数':len(returns),'重建年化夏普':sharpe,'重建年化索提诺':sortino,
        '重建日收盘最大回撤':min(r['回撤'] for r in daily),'重建期间累计收益率':index-1,
        '当前仓位浮盈USDT':number(sum(decimal(r['upl']) for r in positions)),
        '当前合约名义价值美元':number(sum(decimal(r['notionalUsd']) for r in positions)),
        '当前有效敞口倍数':number(sum(decimal(r['notionalUsd']) for r in positions)/decimal(balance['totalEq'])),
        '普通挂单数':len(load('orders')),
        '策略挂单数':sum(len(load('algo_'+kind)) for kind in ['conditional','oco','trigger','move_order_stop']),
        '最早合约成交时间':date(min(int(r['fillTime']) for r in fills))}
    symbol_rows=[]
    for inst,r in by_symbol.items():
        rounds=[e for e in full if e['inst']==inst]
        symbol_rows.append({'合约':inst,'平仓毛盈亏':number(r['gross']),'手续费':number(r['fees']),
            '资金费':number(r['funding']),'期间净盈亏':number(r['gross']+r['fees']+r['funding']),
            '成交笔数':r['fills'],'成交订单数':len(r['orders']),'名义成交额':number(r['notional']),
            '完整轮次数':len(rounds),'完整轮次胜率':sum(e['net']>0 for e in rounds)/len(rounds) if rounds else None})
    symbol_rows.sort(key=lambda r:r['期间净盈亏'],reverse=True)
    month_rows=[{'月份':k,'平仓毛盈亏':number(v['gross']),'手续费':number(v['fees']),
        '资金费':number(v['funding']),'净盈亏':number(v['gross']+v['fees']+v['funding']),'成交笔数':v['fills']} for k,v in sorted(monthly.items())]
    round_rows=[{'合约':r['inst'],'方向':r['direction'],'模式':r['mode'],'开仓时间':date(r['start']) if not r['left_censored'] else '期初已持有',
        '平仓时间':date(r['end']),'持仓小时':r['hours'],'毛盈亏':number(r['gross']),'手续费':number(r['fee']),
        '资金费':number(r['funding']),'净盈亏':number(r['net']),'跨期':r['left_censored']} for r in closed]
    groups=[]
    for dim in ['direction','mode']:
        for value in sorted(set(r[dim] for r in full)):
            rows=[r for r in full if r[dim]==value]
            groups.append({'维度':'方向' if dim=='direction' else '模式','分组':value,'轮次数':len(rows),
                '胜率':sum(r['net']>0 for r in rows)/len(rows),'净盈亏':number(sum(r['net'] for r in rows)),
                '平均净盈亏':number(sum(r['net'] for r in rows)/len(rows)),
                '持仓时长中位数小时':statistics.median(r['hours'] for r in rows)})
    validations={'合约成交与账单完全匹配':True,'现金余额重建闭合':True,'当前仓位数量和均价闭合':True,
        '逐仓保证金闭合':True,'单笔实现盈亏最大复核误差USDT':number(max(fill_errors,default=ZERO)),
        '期初现金余额':{k:str(v) for k,v in initial_cash.items()},'期初持仓':initial_entries,
        '历史持仓条目数':len(history),'重建闭合轮次':len(closed),'完整轮次':len(full),
        '日净值起止':[daily[0]['收盘时间'],daily[-1]['收盘时间']],
        '样本内无强平记录':not any(r['type'] in {'3','4','5','6'} for r in history)}
    result={'summary':summary,'daily':daily,'symbols':symbol_rows,'months':month_rows,'rounds':round_rows,'groups':groups,
        'hourly':[{'北京时间小时':h,'成交笔数':hourly[h]} for h in range(24)],'validations':validations,
        'caveats':[
            '范围是 API 可获取的最近三个月，并非账户成立以来的完整历史。',
            '期间合约净盈亏按发生时间统计平仓盈亏、手续费和资金费，包含跨期仓位在本期实现的盈亏；不是账户收益率。',
            '胜率和盈利因子采用从空仓到空仓的完整交易轮次，剔除期初已持有的 1 轮及当前未平仓轮次。',
            '日净值是重建估算：现金资产 + 逐仓保证金 + 按北京时间日收盘标记价计价的浮盈，使用当前合约面值和线性合约成本均价。',
            '期初 ETH 合约数量通过当前仓位减去本期净成交量得到，成本均价从首笔历史平仓记录核对；该仓位在本期前已开仓。',
            '净值以 USDT 计价，USDT 按 1 处理；不是 OKX 官方历史美元净值。现货资产按日收盘价估值。',
            '交易账户内外划转按当日收盘价估值并从日收益剔除；两笔 SOL 划转的日内价格和时间权重未精确计入。',
            '夏普以日简单收益、无风险利率 0、样本标准差和 365 天年化；排除今天未收盘的数据，短样本和自相关会影响稳定性。',
            '回撤仅衡量日收盘净值，无法识别日内最深回撤；当前浮盈不计入已结束日期的夏普。',
            '历史持仓 cTime 可能复用，持仓时长使用成交重建轮次，未直接用历史 cTime 相减。',
            '无链上充值提现记录不等于没有法币或 P2P 出入金；这些渠道未由当前接口核验。',
            '不同币种、方向和保证金模式的比较样本有限，不能据此证明某种策略长期有效。']}
    path=DATA/'analysis.json';path.write_text(json.dumps(result,ensure_ascii=False,indent=2));path.chmod(0o600)
    # 保留可被共享报告运行时检查的数据来源和派生步骤，不嵌入账户认证或地址。
    query_rows={'summary':[summary],'daily':daily,'symbols':symbol_rows,'months':month_rows,'rounds':round_rows,
        'groups':groups,'hourly':result['hourly'], 'coverage':[{'数据集':k,'条目数':v['rows'],'页数':v.get('pages',len(v.get('requests',[]))),
            '覆盖状态':v.get('status','快照'),'采集完整':v.get('complete',False)} for k,v in meta['sources'].items()],
        'positions':[{k:r.get(k) for k in ['instId','mgnMode','pos','avgPx','markPx','upl','lever','liqPx','notionalUsd']} for r in positions],
        'assets':[{'币种':r['ccy'],'现金余额':number(decimal(r['cashBal'])),'权益':number(decimal(r['eq'])),'美元权益':number(decimal(r['eqUsd']))} for r in balance['details']]}
    query_rows['fills']=[{'时间':date(r['fillTime']),'合约':r['instId'],'买卖':r['side'],'数量张':number(decimal(r['fillSz'])),
        '成交价':number(decimal(r['fillPx'])),'平仓毛盈亏USDT':number(decimal(r['fillPnl'])),
        '手续费USDT':number(decimal(r['fee'])),'流动性':r['execType']} for r in fills]
    query_rows['flows']=[{'时间':date(r['ts']),'币种':r['ccy'],'类型':r['type'],'子类型':r['subType'],
        '余额变化':number(decimal(r['balChg'])),'手续费':number(decimal(r['fee'])),'价格':number(decimal(r['px']))}
        for r in bills if r['instType']!='SWAP']
    snapshot={'surface':'report','title':'账户收益主要来自 ETH，其他合约合计拖累表现','generatedAt':meta['finished_at'],
        'buildStatus':'creating','status':'reviewed','filters':[],'report':{'asOf':date(meta['end_ms'],'%Y-%m-%d'),'title':'账户收益主要来自 ETH，其他合约合计拖累表现'},'queries':{}}
    for name,rows in query_rows.items():
        snapshot['queries'][name]={'rows':rows,'source':{'label':'OKX 实盘只读 API 与本地复核计算','files':['.okx-analysis/analysis.json','.okx-analysis/metadata.json','analyze_account.py'],
            'filters':[f"北京时间 {date(meta['begin_ms'])} 至 {date(meta['end_ms'])}",'当前快照和已结束日收益分别统计'],
            'caveats':result['caveats'],'evidenceFlow':[{'title':'读取只读账户记录','detail':json.dumps(meta['sources'],ensure_ascii=False)},
                {'title':'复核并计算','detail':'执行 analyze_account.py；按 billId 关联合约成交与账单，Decimal 复核费用和盈亏，重建空仓到空仓轮次及日权益。'}],
            'metricDefinitions':[{'label':'日净值与风险指标','definition':'现金 + 逐仓保证金 + 标记价浮盈；剔除划转估值；日收益采用简单收益，无风险收益率设为 0。','componentIds':['equity-trend','drawdown-trend','risk-summary']}]
            },'methods':[{'language':'python','code':'from analyze_account import run\nresult = run()'}]}
    (DATA/'reviewed.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps(validations,ensure_ascii=False,indent=2))
    return result


if __name__=='__main__':run()
