"""按 OKX 账户隔离的本机历史库；API 窗口内以 API 为准，文件仅补充窗口外历史。"""
import calendar
from contextlib import contextmanager
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / '.account-data'
TZ = timezone(timedelta(hours=8))


def key(site, uid):
    if not uid: raise RuntimeError('无法确认账户身份，不能保存或合并历史数据')
    return hashlib.sha256((site + ':' + uid).encode()).hexdigest()


@contextmanager
def connect():
    ROOT.mkdir(mode=0o700, exist_ok=True);ROOT.chmod(0o700)
    path=ROOT/'history.sqlite3'
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600);os.close(fd);path.chmod(0o600)
    db=sqlite3.connect(path,timeout=30)
    db.executescript('''
    CREATE TABLE IF NOT EXISTS accounts(account TEXT PRIMARY KEY,last_sync INTEGER,latest_begin INTEGER,gaps TEXT NOT NULL DEFAULT '[]',cache TEXT);
    CREATE TABLE IF NOT EXISTS records(account TEXT,dataset TEXT,identity TEXT,ts INTEGER,source TEXT,payload TEXT,PRIMARY KEY(account,dataset,identity,source));
    CREATE INDEX IF NOT EXISTS records_time ON records(account,dataset,ts);
    CREATE TABLE IF NOT EXISTS windows(account TEXT,dataset TEXT,begin INTEGER,end INTEGER,complete INTEGER,PRIMARY KEY(account,dataset,begin,end));
    CREATE TABLE IF NOT EXISTS file_windows(account TEXT,hash TEXT,dataset TEXT,begin INTEGER,end INTEGER,PRIMARY KEY(account,hash,dataset));
    CREATE TABLE IF NOT EXISTS import_details(account TEXT,hash TEXT,details TEXT,PRIMARY KEY(account,hash));
    CREATE TABLE IF NOT EXISTS imports(account TEXT,hash TEXT,label TEXT,rows INTEGER,created INTEGER,PRIMARY KEY(account,hash));
    ''')
    try:
        with db: yield db
    finally: db.close()


def three_months_before(ms):
    dt=datetime.fromtimestamp(ms/1000,timezone.utc);year,month=divmod(dt.year*12+dt.month-1-3,12);month+=1
    return int(dt.replace(year=year,month=month,day=min(dt.day,calendar.monthrange(year,month)[1])).timestamp()*1000)


def stamp(value):
    try:return int(datetime.strptime(value,'%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ).timestamp()*1000)
    except ValueError:raise RuntimeError('文件时间格式无法识别，请使用 UTC+8 导出的原始 CSV') from None


def date(ms):return datetime.fromtimestamp(ms/1000,TZ).strftime('%Y-%m-%d %H:%M:%S') if ms else None

def dec(value):
    result=Decimal(str(value or '0').replace(',',''))
    if not result.is_finite():raise RuntimeError('文件包含无效数值，未导入记录')
    return result

def insert(db,account,dataset,identity,ts,payload,source):
    db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?)',(account,dataset,str(identity),int(ts),source,json.dumps(payload,ensure_ascii=False)))


def describe(account,now_ms=None):
    with connect() as db:
        row=db.execute('SELECT last_sync,latest_begin,gaps FROM accounts WHERE account=?',(account,)).fetchone()
        imports=db.execute('SELECT i.label,i.rows,d.details,i.created FROM imports i LEFT JOIN import_details d ON d.account=i.account AND d.hash=i.hash WHERE i.account=? ORDER BY i.created DESC',(account,)).fetchall()
        coverage=db.execute("SELECT dataset,begin,end,'API' FROM windows WHERE account=? AND complete=1 UNION ALL SELECT dataset,begin,end,'历史文件' FROM file_windows WHERE account=?",(account,account)).fetchall()
        count=db.execute('SELECT COUNT(*) FROM records WHERE account=?',(account,)).fetchone()[0]
    last=row[0] if row else None;gaps=json.loads(row[2]) if row else []
    if last and now_ms and three_months_before(now_ms)>last+1000:
        gaps += [{'start':last,'end':three_months_before(now_ms)}]
    unique={(g['start'],g['end']) for g in gaps}
    return {'lastSync':date(last),'storedRecords':count,'imports':[{'文件类型':r[0],'记录数':r[1],'导入时间':date(r[3]),**(json.loads(r[2]) if r[2] else {})} for r in imports],
            'coverage':coverage_ranges(coverage),
            'gaps':[{'start':date(a),'end':date(b)} for a,b in sorted(unique)],
            'message':'API 可查询窗口与上次同步之间可能存在记录缺口；本次仅同步近期数据。' if unique else ''}


def coverage_ranges(rows):
    grouped=defaultdict(list)
    for dataset,a,b,source in rows:grouped[(dataset,source)].append((a,b))
    result=[]
    for (dataset,source),intervals in sorted(grouped.items()):
        merged=[]
        for a,b in sorted(intervals):
            if merged and a<=merged[-1][1]+1000:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
            else:merged.append((a,b))
        result.extend({'数据集':dataset,'来源':source,'开始':date(a),'结束':date(b)} for a,b in merged)
    return result


def cached(account):
    with connect() as db:row=db.execute('SELECT cache FROM accounts WHERE account=?',(account,)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def api_ingest(account,directory):
    meta=json.loads((directory/'metadata.json').read_text());begin,end=meta['begin_ms'],meta['end_ms']
    complete=all(meta['sources'].get(k,{}).get('complete') for k in ['bills','fills_SWAP'])
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO accounts(account) VALUES(?)',(account,))
        last,gaps=db.execute('SELECT last_sync,gaps FROM accounts WHERE account=?',(account,)).fetchone();gaps=json.loads(gaps)
        for path in directory.glob('*.json'):
            dataset=path.stem
            if dataset in {'bills','funding_bills','deposits','withdrawals','history_positions'} or dataset.startswith(('fills_','orders_')):
                source=meta['sources'].get(dataset,{})
                if not source.get('complete') or (dataset in {'bills','fills_SWAP'} and not complete):continue
                rows=json.loads(path.read_text())
                for r in rows:
                    ts=int(r.get('fillTime') or r.get('uTime') or r.get('ts') or 0)
                    identity=(r.get('instId','')+'|'+str(r.get('tradeId') or r.get('billId'))) if dataset.startswith('fills_') else str(r.get('billId') or r.get('ordId') or r.get('depId') or r.get('wdId') or (str(r.get('posId'))+'|'+str(r.get('uTime'))))
                    insert(db,account,dataset,identity,ts,r,'api')
                if source.get('pages',0)>0:
                    # 只有完整获取的数据集才能建立 API 覆盖窗口。
                    db.execute('INSERT OR REPLACE INTO windows VALUES(?,?,?,?,?)',(account,dataset,begin,end,int(source.get('complete',False))))
        if complete:

            if last and begin>last+1000:gaps.append({'start':last,'end':begin})
            db.execute('UPDATE accounts SET last_sync=?,latest_begin=?,gaps=? WHERE account=?',(end,begin,json.dumps(gaps),account))
    return complete


def effective(account,dataset):
    with connect() as db:
        rows=db.execute('SELECT identity,ts,source,payload FROM records WHERE account=? AND dataset=?',(account,dataset)).fetchall()
        windows=db.execute('SELECT begin,end FROM windows WHERE account=? AND dataset=? AND complete=1',(account,dataset)).fetchall()
    api={identity for identity,_,source,_ in rows if source=='api'}
    return [dict(json.loads(payload),_source=source) for identity,ts,source,payload in rows
            if source=='api' or (identity not in api and not any(a<=ts<=b for a,b in windows))]


def parse_files(files,uid):
    parsed=[]
    for filename,data in files:
        if len(data)>8*1024*1024:raise RuntimeError('单个文件不能超过 8 MB')
        if filename.lower().endswith('.zip'):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    infos=[r for r in z.infolist() if not r.is_dir() and not r.filename.startswith('__MACOSX/')]
                    if len(infos)>24 or sum(r.file_size for r in infos)>30*1024*1024:raise RuntimeError('压缩包展开内容超过限制')
                    contents=[(r.filename,z.read(r)) for r in infos if r.filename.lower().endswith('.csv')]
                    if not contents:raise RuntimeError('压缩包内没有 CSV 文件，请重新选择 CSV 导出格式')
            except (zipfile.BadZipFile,RuntimeError) as e:
                if isinstance(e,RuntimeError):raise
                raise RuntimeError('无法读取 ZIP 文件') from None
        elif filename.lower().endswith('.csv'):contents=[(filename,data)]
        else:raise RuntimeError('请选择 OKX 导出的 ZIP 或 CSV 文件')
        for name,raw in contents:
            try:text=raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                try:text=raw.decode('gb18030')
                except UnicodeDecodeError:raise RuntimeError('CSV 编码无法识别') from None
            rows=[[v.strip().lstrip('\ufeff') for v in r] for r in csv.reader(io.StringIO(text)) if any(r)]
            if len(rows)<2:raise RuntimeError('CSV 文件没有可识别的表头')
            # 手机导出元数据里的用户 ID 用于防止导入其他账户；不信任压缩包文件名中的编码。
            m=re.search(r'(?:用户\s*ID|User\s*ID)\s*[:：]\s*(\d+)', ' '.join(sum(rows[:1],[])),re.I)
            if not m or m.group(1)!=uid:raise RuntimeError('文件账户身份与当前连接账户不一致，或缺少用户 ID；未导入任何记录')
            metadata=' '.join(rows[0])
            zone=re.search(r'UTC\s*([+-]\d+)',metadata,re.I)
            if zone and zone.group(1) not in {'+8','+08'}:raise RuntimeError('请使用 UTC+8 时区重新导出文件')
            idx=next((i for i,r in enumerate(rows[:20]) if any(v in {'时间','仓位创建时间','委托ID','交易ID','委托 ID'} for v in r)),None)
            if idx is None:raise RuntimeError('文件表头不属于支持的 OKX 手机导出格式')
            headers=rows[idx]
            data_rows=[]
            for r in rows[idx+1:]:
                if len(r)!=len(headers):raise RuntimeError('CSV 行列数不一致，未导入文件')
                data_rows.append(dict(zip(headers,r)))
            digest=hashlib.sha256(raw).hexdigest()
            dates=re.findall(r'\d{4}-\d{2}-\d{2}',metadata) or re.findall(r'\d{4}-\d{2}-\d{2}',filename) or re.findall(r'\d{4}-\d{2}-\d{2}',name)
            window=(stamp(dates[0]+' 00:00:00'),stamp(dates[1]+' 00:00:00')+86400000) if len(dates)>=2 else None
            parsed.append((digest,headers,data_rows,window))
    return parsed


def import_files(account,uid,files,preview=False):
    parsed=parse_files(files,uid);prepared=[]
    # 全部先解析与校验，通过后在同一事务中提交，避免半批导入。
    for digest,h,rows,window in parsed:
        records=[]
        if '关联订单id' in h:
            dataset,label='bills','交易账单'
            for r in rows:
                inst=r['交易品种'];side=r['交易类型'];kind=r['账单类型'];funding='资金费' in side
                adjustment=any(x in side for x in ['惩罚费','穿仓代偿'])
                for col in ['收益','手续费','交易账户余额变动','仓位余额变动']:dec(r[col])
                value={'billId':r['id'],'ordId':r['关联订单id'],'ts':str(stamp(r['时间'])),
                       'instId':inst,'instType':{'永续合约':'SWAP','币币':'SPOT','杠杆':'MARGIN'}.get(kind,''),
                       'type':'8' if funding else '1' if kind=='划转' and side in ['转入','转出'] else '2' if side in ['买入','卖出'] or '强平' in side and not adjustment else 'other',
                       'ccy':r['交易账户余额单位'],'pnl':r['收益'],'fee':r['手续费'],'balChg':r['交易账户余额变动'],
                       'posBalChg':r['仓位余额变动'],'posBal':r['仓位余额'],'bal':r['交易账户余额'],'sz':r['数量'],'px':r['成交价'],'feeCcy':r['手续费单位'],'raw':r,'label':side or kind,'adjustment':str(dec(r['交易账户余额变动'])+dec(r['仓位余额变动'])) if adjustment else '0'}
                records.append((r['id'],int(value['ts']),value))
        elif '交易ID' in h:
            dataset,label='fills','逐笔成交'
            for r in rows:
                for col in ['数量','成交价格','手续费']:dec(r[col])
                kind={'永续合约':'SWAP','币币':'SPOT','杠杆':'MARGIN','交割合约':'FUTURES','期权':'OPTION'}.get(r['交易类型'])
                if not kind:raise RuntimeError('成交文件包含尚未支持的交易类型')
                value={'tradeId':r['交易ID'],'ordId':r['委托ID'],'instId':r['交易品种'],'instType':kind,
                       'fillTime':str(stamp(r['交易时间'])),'fillSz':r['数量'],'fillPx':r['成交价格'],
                       'fee':r['手续费'],'feeCcy':r['手续费单位'],'execType':'T' if r['流动性方向']=='吃单' else 'M' if r['流动性方向']=='挂单' else '',
                       'side':'','fillPnl':None}
                records.append((r['交易品种']+'|'+r['交易ID'],int(value['fillTime']),value))
        elif '委托ID' in h:
            dataset,label='orders','历史委托'
            for r in rows:
                kind={'永续合约':'SWAP','币币':'SPOT','杠杆':'MARGIN','交割合约':'FUTURES','期权':'OPTION'}.get(r['交易类型'])
                if not kind:raise RuntimeError('委托文件包含尚未支持的交易类型')
                value={'ordId':r['委托ID'],'instId':r['交易品种'],'instType':kind,'uTime':str(stamp(r['委托时间'])),
                       'side':'buy' if '买' in r['方向'] or r['方向'] in ['开多','平空'] else 'sell' if '卖' in r['方向'] or r['方向'] in ['开空','平多'] else '', 'label':r['方向'],'raw':r}
                records.append((r['委托ID'],int(value['uTime']),value))
        elif '快照时间' in h:
            dataset,label='import_positions','仓位快照'
            for r in rows:records.append((r['仓位ID']+'|'+r['快照时间'],stamp(r['快照时间']),r))
        elif '仓位更新时间' in h:
            dataset,label='import_history_positions','持仓历史'
            for r in rows:records.append((hashlib.sha256(json.dumps(r,sort_keys=True).encode()).hexdigest(),stamp(r['仓位更新时间']),r))
        elif '转账后余额' in h:
            dataset,label='funding_bills','资金账单'
            for r in rows:
                dec(r['数量']);records.append((r['id'],stamp(r['时间']),r))
        else:raise RuntimeError('不支持的 CSV 字段组合')
        prepared.append((digest,dataset,label,records,window))
    feedback=[]
    with connect() as db:
        if not preview:db.execute('INSERT OR IGNORE INTO accounts(account) VALUES(?)',(account,))
        seen=set();seen_records=set()
        for digest,dataset,label,records,window in prepared:
            duplicate=bool(db.execute('SELECT 1 FROM imports WHERE account=? AND hash=?',(account,digest)).fetchone()) or digest in seen
            seen.add(digest);duplicates=covered=added=0
            for identity,ts,value in records:
                actual=dataset+'_'+value['instType'] if dataset in {'fills','orders'} else dataset
                exists=db.execute("SELECT 1 FROM records WHERE account=? AND dataset=? AND identity=? AND source='file'",(account,actual,str(identity))).fetchone()
                api=db.execute("SELECT 1 FROM records WHERE account=? AND dataset=? AND identity=? AND source='api'",(account,actual,str(identity))).fetchone()
                in_window=db.execute('SELECT 1 FROM windows WHERE account=? AND dataset=? AND complete=1 AND begin<=? AND end>=?',(account,actual,ts,ts)).fetchone()
                record_key=(actual,str(identity))
                if duplicate or exists or record_key in seen_records:duplicates+=1
                elif api or in_window:covered+=1
                else:added+=1
                seen_records.add(record_key)
                if not preview:insert(db,account,actual,identity,ts,value,'file')
            begin,end=window if window else (min((r[1] for r in records),default=0),max((r[1] for r in records),default=0))
            details={'有效新增':added,'重复记录':duplicates,'API覆盖':covered,'覆盖开始':date(begin),'覆盖结束':date(end)}
            if not preview and not duplicate:
                if window and dataset=='fills':
                    for kind in {r[2]['instType'] for r in records}:db.execute('INSERT OR REPLACE INTO file_windows VALUES(?,?,?,?,?)',(account,digest,'fills_'+kind,*window))
                if window and dataset=='bills':db.execute('INSERT OR REPLACE INTO file_windows VALUES(?,?,?,?,?)',(account,digest,dataset,*window))
                db.execute('INSERT OR IGNORE INTO imports VALUES(?,?,?,?,?)',(account,digest,label,len(records),int(datetime.now().timestamp()*1000)))
                db.execute('INSERT OR REPLACE INTO import_details VALUES(?,?,?)',(account,digest,json.dumps(details,ensure_ascii=False)))
            feedback.append({'类型':label,'读取记录':len(records),'重复文件':duplicate,**details})
    return feedback


def report(account,recent=None):
    recent=recent or cached(account) or {};result=dict(recent)
    bills=effective(account,'bills');fills=[r for k in ['SPOT','MARGIN','SWAP','FUTURES','OPTION'] for r in effective(account,'fills_'+k)]
    orders={r.get('ordId'):r for k in ['SPOT','MARGIN','SWAP','FUTURES','OPTION'] for r in effective(account,'orders_'+k)}
    pnl_lookup=defaultdict(list)
    for b in bills:
        if b.get('ordId'):pnl_lookup[(b['ordId'],b.get('instId',''),int(b['ts'])//1000)].append(b)
    display=[]
    for f in fills:
        order=orders.get(f.get('ordId'),{});side=f.get('side') or order.get('side','')
        candidates=pnl_lookup.get((f.get('ordId'),f['instId'],int(f['fillTime'])//1000),[])
        pnl=f.get('fillPnl')
        if pnl is None and len(candidates)==1:pnl=candidates[0].get('pnl')
        display.append({'时间':date(int(f['fillTime'])),'产品':f.get('instType') or ('SWAP' if f['instId'].endswith('-SWAP') else 'SPOT'),
                        '合约':f['instId'],'买卖':{'buy':'买入','sell':'卖出'}.get(side,order.get('label') or '未提供'),
                        '数量':float(dec(f['fillSz'])),'成交价':float(dec(f['fillPx'])),'原始数量':str(f['fillSz']),'原始成交价':str(f['fillPx']),'原始手续费':str(f.get('fee') or '0'),'平仓盈亏':float(dec(pnl)) if pnl is not None else None,
                        '手续费':float(dec(f.get('fee'))),'费用币种':f.get('feeCcy'),'流动性':f.get('execType'),
                        '来源':'API' if f['_source']=='api' else '历史文件'})
    display.sort(key=lambda r:r['时间'],reverse=True)
    # 仅传递计算所需字段，日期筛选不得依赖已经聚合的月份结果。
    result['ledger']=[{'时间':date(int(b['ts'])),'合约':b['instId'],
                       '平仓毛盈亏':float(dec(b.get('pnl'))) if b.get('type')!='8' else 0,
                       '资金费':float(dec(b.get('pnl'))) if b.get('type')=='8' else 0,
                       '手续费':float(dec(b.get('fee'))),'其他调整':float(dec(b.get('adjustment')))}
                      for b in bills if b.get('instType')=='SWAP' and b.get('ccy')=='USDT' and b.get('instId','').endswith('-USDT-SWAP')]
    with connect() as db:
        result['periodCoverage']={kind:[{'start':date(a),'end':date(z)} for a,z in db.execute(
            'SELECT begin,end FROM windows WHERE account=? AND dataset=? AND complete=1 UNION SELECT begin,end FROM file_windows WHERE account=? AND dataset=?',
            (account,kind,account,kind)).fetchall()] for kind in ['bills','fills_SWAP']}
    groups=defaultdict(lambda:defaultdict(lambda:Decimal(0)));months=defaultdict(lambda:defaultdict(lambda:Decimal(0)))
    for b in bills:
        if b.get('instType')!='SWAP' or b.get('ccy')!='USDT' or not b.get('instId','').endswith('-USDT-SWAP'):continue
        g=groups[b['instId']];m=months[date(int(b['ts']))[:7]]
        for target in [g,m]:
            target['资金费' if b.get('type')=='8' else '平仓毛盈亏']+=dec(b.get('pnl'))
            target['手续费']+=dec(b.get('fee'));target['其他调整']+=dec(b.get('adjustment'))
    for f in fills:
        if f.get('feeCcy')=='USDT' and f['instId'].endswith('-USDT-SWAP'):
            groups[f['instId']]['成交笔数']+=1;months[date(int(f['fillTime']))[:7]]['成交笔数']+=1
    def rows(mapping,name,net):
        out=[]
        for key,g in mapping.items():
            g[net]=g['平仓毛盈亏']+g['手续费']+g['资金费']+g['其他调整']
            out.append({name:key,**{k:float(v) for k,v in g.items()}})
        return out
    symbols=rows(groups,'合约','期间净盈亏');symbols.sort(key=lambda r:r['期间净盈亏'],reverse=True)
    monthly=rows(months,'月份','净盈亏');monthly.sort(key=lambda r:r['月份'])
    summary=dict(result.get('summary') or {})
    for col,key_name in [('平仓毛盈亏','合约平仓毛盈亏USDT'),('手续费','合约手续费USDT'),('资金费','合约资金费USDT'),('期间净盈亏','合约期间已实现净盈亏USDT')]:summary[key_name]=sum(r.get(col,0) for r in symbols)
    if not bills and any(not r.get('完整') for r in result.get('coverage',[]) if r.get('数据集') in ['bills','fills_SWAP']):
        for name in ['合约平仓毛盈亏USDT','合约手续费USDT','合约资金费USDT','合约期间已实现净盈亏USDT']:summary[name]=None
    summary['合约其他调整USDT']=sum(r.get('其他调整',0) for r in symbols)
    swap=[f for f in fills if f['instId'].endswith('-SWAP')];summary['合约成交笔数']=len(swap)
    summary['Taker成交占比']=sum(f.get('execType')=='T' for f in swap)/len(swap) if swap else None
    timestamps=[int(r.get('ts') or 0) for r in bills]+[int(r['fillTime']) for r in fills]
    result.update(summary=summary,fills=display,symbols=symbols,months=monthly,local=describe(account),riskRange=recent.get('riskRange') if 'riskRange' in recent else recent.get('range'))
    if timestamps:result['range']={'start':date(min(timestamps)),'end':date(max(timestamps))}
    result['caveats']=list(result.get('caveats') or [])+['累计交易与账单来自本机保存的记录。API 查询区间内采用 API，历史文件只补充区间外的数据。',
        '净值重建尝试使用本机合并历史；夏普与回撤仅在连续、正权益的有效区间计算。轮次胜率仅在已复核轮次覆盖的所选区间计算。',
        '历史账单里的强平惩罚费、穿仓代偿等余额调整单独计入“其他调整”；未把本金划转计入合约盈利。']
    result['caveats']=list(dict.fromkeys(c for c in result['caveats'] if not c.startswith(('历史范围为最近三个日历月','平仓毛盈亏 + 手续费 + 资金费 =','夏普、回撤和完整轮次胜率仍采用'))))
    if any(not r.get('完整') for r in result.get('coverage',[]) if r.get('数据集') in ['bills','fills_SWAP']):
        result['caveats'].append('最新 API 账单或成交未完整获取，累计结果仅代表已保存的记录，不代表完整账户收益。')
    return result


def save_cache(account,result):
    with connect() as db:db.execute('UPDATE accounts SET cache=? WHERE account=?',(json.dumps(result,ensure_ascii=False,allow_nan=False),account))


def finish_sync(account,directory,recent,augment=None):
    """完整核心数据才更新统计；失败结果只更新本次尝试状态。"""
    meta=json.loads((directory/'metadata.json').read_text())
    complete=api_ingest(account,directory)
    previous=cached(account)
    failed=sorted(set(meta.get('errors',{})) | {name for name,source in meta['sources'].items() if not source.get('complete')})
    if complete:
        if augment is not None:recent=augment(recent)
        result=report(account,recent)
        result['statisticsAvailable']=True
        message='近期账单与合约成交同步完成。' if not failed else '账单与合约成交已同步，部分辅助数据未完整取得；请查看数据获取状态。'
    else:
        if previous:
            result=dict(previous)
            message='本次账单或合约成交未完整获取，继续展示上次保存的统计；请点击“同步 API”重试。'
        else:
            result=dict(recent)
            result.update(summary={name:None for name in ['合约平仓毛盈亏USDT','合约手续费USDT','合约资金费USDT','合约期间已实现净盈亏USDT','合约其他调整USDT','合约成交笔数','Taker成交占比']},
                          fills=[],symbols=[],months=[],daily=[],rounds=[],groups=[],riskAvailable=False,statisticsAvailable=False)
            message='账单或合约成交未完整获取，尚无可靠累计统计；收益保持未取得状态，请重试。'
        result['coverage']=recent.get('coverage',[])
        result['errors']=recent.get('errors',{})
    result['local']=describe(account)
    result['sync']={'state':'complete' if complete and not failed else 'partial', 'message':message,
                    'attemptAt':date(meta['end_ms']),'statisticsAt':result['local']['lastSync'],'failed':failed}
    save_cache(account,result)
    return result

# 备份仅包含当前账户历史与计算缓存，不包含 API 凭据或其他账户。
BACKUP_TABLES={'records':5,'windows':4,'file_windows':4,'imports':4,'import_details':2}

def backup_digest(payload):
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def make_backup(account):
    with connect() as db:
        db.execute('BEGIN')
        meta=db.execute('SELECT last_sync,latest_begin,gaps,cache FROM accounts WHERE account=?',(account,)).fetchone()
        if not meta:raise RuntimeError('当前账户没有可备份的历史')
        payload={'account':account,'meta':list(meta),'tables':{name:[list(r) for r in db.execute('SELECT * FROM '+name+' WHERE account=?',(account,)).fetchall()] for name in BACKUP_TABLES}}
        for name,rows in payload['tables'].items():payload['tables'][name]=[r[1:] for r in rows]
    return {'format':'hengliang-account-backup','version':1,'created':date(int(datetime.now().timestamp()*1000)),'payload':payload,'sha256':backup_digest(payload)}

def validate_backup(account,backup):
    try:
        if not isinstance(backup,dict) or backup.get('format')!='hengliang-account-backup' or backup.get('version')!=1:raise ValueError()
        p=backup['payload']
        if p['account']!=account:raise RuntimeError('备份不属于当前已连接账户，不能恢复')
        if backup_digest(p)!=backup['sha256']:raise RuntimeError('备份校验失败，文件可能已损坏')
        if len(p['meta'])!=4 or set(p['tables'])!=set(BACKUP_TABLES):raise ValueError()
        for n in p['meta'][:2]:
            if n is not None and (type(n)!=int or not 0<=n<10**15):raise ValueError()
        gaps=json.loads(p['meta'][2]);cache=json.loads(p['meta'][3]) if p['meta'][3] else None
        if not isinstance(gaps,list) or cache is not None and not isinstance(cache,dict):raise ValueError()
        for gap in gaps:
            if not isinstance(gap,dict) or any(type(gap.get(k))!=int for k in ['start','end']) or not 0<=gap['start']<=gap['end']<10**15:raise ValueError()
        for name,size in BACKUP_TABLES.items():
            rows=p['tables'][name]
            if not isinstance(rows,list) or len(rows)>200000:raise ValueError()
            for row in rows:
                if not isinstance(row,list) or len(row)!=size or any(isinstance(v,(dict,list)) for v in row):raise ValueError()
                if name=='records':
                    dataset,identity,ts,source,value=row
                    if not isinstance(dataset,str) or not isinstance(identity,str) or type(ts)!=int or not 0<=ts<10**15 or source not in ['api','file'] or not isinstance(json.loads(value),dict):raise ValueError()
                if name in ['windows','file_windows']:
                    start,end=(row[1],row[2]) if name=='windows' else (row[2],row[3])
                    if type(start)!=int or type(end)!=int or not 0<=start<=end<10**15:raise ValueError()
                    if name=='windows' and row[3] not in [0,1]:raise ValueError()
                if name=='imports' and (type(row[2])!=int or row[2]<0 or type(row[3])!=int):raise ValueError()
                if name=='import_details' and not isinstance(json.loads(row[1]),dict):raise ValueError()
        return p
    except RuntimeError:raise
    except (ValueError,TypeError,KeyError,OverflowError):raise RuntimeError('备份结构不受支持或内容无效，未恢复记录') from None

def backup_preview(account,backup):
    p=validate_backup(account,backup)
    with connect() as db:
        existing={tuple(r) for r in db.execute('SELECT dataset,identity,source FROM records WHERE account=?',(account,))}
    incoming={(r[0],r[1],r[3]) for r in p['tables']['records']}
    return {'记录数':len(p['tables']['records']),'可新增':len(incoming-existing),'已存在':len(incoming&existing),'上次同步':date(p['meta'][0]),'覆盖窗口':len(p['tables']['windows'])+len(p['tables']['file_windows']),'校验':'通过 · 与当前账户一致'}

def restore_backup(account,backup):
    p=validate_backup(account,backup);feedback=backup_preview(account,backup)
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO accounts(account) VALUES(?)',(account,))
        for name,size in BACKUP_TABLES.items():
            # 不覆盖已有记录，重复恢复幂等；历史文件在 API 窗口内仍不参与有效分析。
            db.executemany('INSERT OR IGNORE INTO '+name+' VALUES('+','.join(['?']*(size+1))+')',[(account,*row) for row in p['tables'][name]])
        old=db.execute('SELECT last_sync,latest_begin,gaps,cache FROM accounts WHERE account=?',(account,)).fetchone()
        newer=(p['meta'][0] or 0)>(old[0] or 0)
        gaps=json.loads(old[2])+json.loads(p['meta'][2]);gaps=list({(g['start'],g['end']):g for g in gaps}.values())
        db.execute('UPDATE accounts SET last_sync=?,latest_begin=?,gaps=?,cache=? WHERE account=?',((p['meta'][0] if newer else old[0]),p['meta'][1] if newer else old[1],json.dumps(gaps),old[3] or p['meta'][3],account))
    return feedback
