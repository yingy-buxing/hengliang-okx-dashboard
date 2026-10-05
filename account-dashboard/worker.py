"""每次分析独立进程与临时目录；认证只从标准输入进入。"""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import signal
from collections import defaultdict
from decimal import Decimal
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from okx_readonly import Client
import collect_analysis
import collect_prices
import analyze_account
import history_store
import equity_history


def emit(value):
    sys.__stdout__.write(json.dumps(value, ensure_ascii=False) + '\n')
    sys.__stdout__.flush()


def summarize(directory):
    def read(name):
        p = directory / (name + '.json')
        return json.loads(p.read_text()) if p.exists() else []
    meta = read('metadata')
    fills = []
    for kind in ['SPOT', 'MARGIN', 'SWAP', 'FUTURES', 'OPTION']:
        for r in read('fills_' + kind):
            fills.append({'时间': analyze_account.date(r['fillTime']), '产品': kind, '合约': r['instId'],
                          '买卖': '买入' if r['side'] == 'buy' else '卖出', '数量': float(r['fillSz']),
                          '成交价': float(r['fillPx']), '平仓盈亏': float(r.get('fillPnl') or 0),
                          '手续费': float(r.get('fee') or 0), '费用币种': r.get('feeCcy'),
                          '流动性': r.get('execType', '')})
    fills.sort(key=lambda r: r['时间'], reverse=True)
    groups = defaultdict(lambda: {'合约': '', '平仓毛盈亏': Decimal(0), '手续费': Decimal(0), '资金费': Decimal(0), '成交笔数': 0})
    # USDT 费用才能与 USDT 合约盈亏相加；其他币种单独显示在成交明细。
    for r in read('fills_SWAP'):
        if r.get('feeCcy') != 'USDT' or not r['instId'].endswith('-USDT-SWAP'):
            continue
        g = groups[r['instId']];g['合约'] = r['instId'];g['成交笔数'] += 1
        g['平仓毛盈亏'] += Decimal(r.get('fillPnl') or '0');g['手续费'] += Decimal(r.get('fee') or '0')
    for r in read('bills'):
        if r.get('instType') == 'SWAP' and r.get('type') == '8' and r.get('ccy') == 'USDT':
            g = groups[r['instId']];g['合约'] = r['instId'];g['资金费'] += Decimal(r.get('pnl') or '0')
    symbols = []
    for g in groups.values():
        g['期间净盈亏'] = g['平仓毛盈亏'] + g['手续费'] + g['资金费']
        symbols.append({k: float(v) if isinstance(v, Decimal) else v for k, v in g.items()})
    symbols.sort(key=lambda r: r['期间净盈亏'], reverse=True)
    core = ['bills', 'fills_SWAP']
    complete = all(meta['sources'].get(k, {}).get('complete') for k in core)
    result = {'fills': fills, 'symbols': symbols, 'daily': [], 'rounds': [], 'months': [], 'groups': [],
              'riskAvailable': False, 'riskReason': '日净值尚未通过重建复核。',
              'range': {'start': analyze_account.date(meta['begin_ms']), 'end': analyze_account.date(meta['end_ms'])},
              'coverage': [{'数据集': k, '条目数': v['rows'], '完整': v.get('complete', False)} for k, v in meta['sources'].items()],
              'errors': meta['errors'], 'summary': {'合约成交笔数': len(read('fills_SWAP')),
                 '合约平仓毛盈亏USDT': sum(r['平仓毛盈亏'] for r in symbols) if complete else None,
                 '合约手续费USDT': sum(r['手续费'] for r in symbols) if complete else None,
                 '合约资金费USDT': sum(r['资金费'] for r in symbols) if complete else None,
                 '合约期间已实现净盈亏USDT': sum(r['期间净盈亏'] for r in symbols) if complete else None},
              'caveats': ['历史范围为最近三个日历月，实际覆盖情况见数据质量。',
                          '期间盈亏卡片仅统计 USDT 线性永续合约，其他产品与费用币种见成交明细。',
                          '平仓毛盈亏 + 手续费 + 资金费 = 已实现净盈亏；未平仓浮盈单独展示。',
                          '交易账户净值不包含资金账户与理财历史。USDT 按 1 计价。',
                          '无法取得期初持仓成本、历史价格或流水未闭合时，夏普和回撤不显示。']}
    try:
        assert complete, '合约成交或流水没有完整获取'
        assert not any(read('fills_' + k) for k in ['MARGIN', 'FUTURES', 'OPTION']), '暂不支持混合保证金、交割合约或期权的历史净值重建'
        assert all(p.get('instType') == 'SWAP' and p.get('posSide') == 'net' for p in read('positions')), '净值重建仅支持净持仓模式的永续合约'
        assert read('fills_SWAP'), '没有合约成交样本，不能计算本版本的历史风险指标'
        assert not any(r.get('type') in ['3', '4', '5', '6'] for r in read('history_positions')), '存在强平或减仓记录，暂不支持可靠重建'
        analyze_account.DATA = directory
        with contextlib.redirect_stdout(io.StringIO()):
            analysis = analyze_account.run()
        assert all(r['重建权益'] > 0 for r in analysis['daily']), '存在非正权益，日收益率不可用'
        result.update({k: analysis[k] for k in ['summary', 'daily', 'symbols', 'rounds', 'months', 'groups']})
        result.update(riskAvailable=True, riskReason='', validations=analysis['validations'])
        result['caveats'] += ['日净值来自流水和日收盘标记价重建，已剔除交易账户划转；USDT 划转按流水金额、非 USDT 划转按日收盘价格估值，收益分母按资金流发生时间加权。',
                             '夏普：平均日收益率 / 日收益样本标准差 × √365，无风险收益率为 0；不包含今天未收盘数据。',
                             '回撤按日收盘净值指数计算，无法反映日内最深回撤。',
                             '胜率以从空仓到空仓的完整轮次统计，剔除期初持仓和当前未平仓轮次。']
    except (AssertionError, KeyError, IndexError, ValueError, ZeroDivisionError, OSError, ArithmeticError) as error:
        # 不把原始异常或账户字段输出给页面。
        result['riskReason'] = str(error) if isinstance(error, AssertionError) and str(error) else '该账户的数据或持仓类型未满足日净值重建条件。'
    return result


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    credentials = json.loads(sys.stdin.readline())
    account=credentials.pop('_account')
    client = Client(credentials.pop('site', 'global'), credentials)
    class Progress(io.TextIOBase):
        def write(self, text):
            if text.strip():
                first=text.strip().split()[0]
                stage='账单' if first in {'bills','funding_bills'} else '成交' if first.startswith('fills_') else '历史行情' if first.startswith(('mark_','spot_','candles_','instrument_')) else '账户及辅助记录'
                emit({'progress': '正在获取'+stage+'；完成后核对净值。'})
            return len(text)
    try:
        with tempfile.TemporaryDirectory(prefix='okx-dashboard-') as tmp:
            directory = Path(tmp)
            with contextlib.redirect_stdout(Progress()):
                collect_analysis.main(client, directory)
                try:
                    collect_prices.main(client, directory)
                except RuntimeError:
                    meta = json.loads((directory / 'metadata.json').read_text())
                    meta['errors']['historical_prices'] = '历史价格未完整取得'
                    collect_analysis.save('metadata.json', meta)
            emit({'progress': '正在重建净值并核对成交、流水和持仓。'})
            recent=summarize(directory)
            emit({'progress':'正在合并本机历史，补齐全年行情并逐批复核余额。'})
            with contextlib.redirect_stdout(Progress()):
                result=history_store.finish_sync(account,directory,recent,lambda value:equity_history.extend(account,directory,client,value))
            emit({'result': result})
    except Exception:
        emit({'error': '分析未完成，请检查网络后重试；已获取的快照仍可查看。'})
