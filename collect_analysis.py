"""采集最近三个月可用的只读账户数据，私密文件不含认证信息。"""
from okx_readonly import Client, ROOT, PATHS
from datetime import datetime, timezone
from pathlib import Path
import calendar
import json
import os
import time

DEST = ROOT / '.okx-analysis'


def save(name, value):
    path = DEST / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    path.chmod(0o600)


def main(client=None, destination=None):
    global DEST
    if destination is not None:
        DEST = Path(destination)
    os.umask(0o077)
    DEST.mkdir(mode=0o700, exist_ok=True)
    DEST.chmod(0o700)
    c = client if client is not None else Client('global')
    c.verify()
    now_ms = int(c.get('time')[0]['ts'])
    now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
    month_index = now.year * 12 + now.month - 1 - 3
    year, month = divmod(month_index, 12)
    month += 1
    start = now.replace(year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1]))
    begin_ms = int(start.timestamp() * 1000)
    meta = {'begin_ms': begin_ms, 'end_ms': now_ms, 'timezone': 'Asia/Shanghai', 'site': c.host,
            'collected_at': now.isoformat(), 'sources': {}, 'errors': {}}

    def single(label, endpoint, params=None, keep=None):
        try:
            data = c.get(endpoint, params)
            if keep:
                data = [{k: r.get(k) for k in keep if k in r} for r in data]
            save(label + '.json', data)
            meta['sources'][label] = {'path': PATHS[endpoint], 'params': params or {}, 'rows': len(data), 'pages': 1, 'complete': True, 'scope': 'snapshot'}
            print(label, len(data), '完成', flush=True)
            return data
        except RuntimeError as e:
            meta['errors'][label] = str(e)
            print(label, str(e), flush=True)
            return []

    def paged(label, endpoint, params, cursor_field, timestamp_field='ts', timestamp_cursor=False, keep=None):
        records, seen, requests = [], set(), []
        status, cursor = 'unknown', None
        for _ in range(1000):
            query = dict(params, limit='100')
            if cursor is not None:
                query['before' if endpoint in {'deposits', 'withdrawals'} else 'after'] = str(cursor)
            try:
                batch = c.get(endpoint, query)
            except RuntimeError as e:
                meta['errors'][label] = str(e)
                status = 'error'
                break
            requests.append(query)
            added = 0
            for r in batch:
                identifier = (str(r.get(cursor_field)), str(r.get('instId', '')), str(r.get('posId', '')))
                if endpoint == 'history_positions':
                    identifier = (str(r.get('posId')), str(r.get('uTime')))
                if identifier in seen:
                    continue
                seen.add(identifier)
                timestamp = int(r.get(timestamp_field) or 0)
                if begin_ms <= timestamp <= now_ms:
                    records.append(r if keep is None else {k: r.get(k) for k in keep if k in r})
                added += 1
            if not batch or len(batch) < 100:
                status = 'exhausted'
                break
            timestamps = [int(r.get(timestamp_field) or 0) for r in batch]
            if min(timestamps) < begin_ms:
                status = 'window_reached'
                break
            next_cursor = min(timestamps) - 1 if timestamp_cursor else batch[-1].get(cursor_field)
            if next_cursor is None or str(next_cursor) == str(cursor) or not added:
                status = 'cursor_stalled'
                break
            cursor = next_cursor
            time.sleep(0.25)
        else:
            status = 'page_limit'
        save(label + '.json', records)
        meta['sources'][label] = {'path': PATHS[endpoint], 'params': params, 'rows': len(records),
                                 'pages': len(requests), 'requests': requests, 'status': status,
                                 'complete': status in {'exhausted', 'window_reached'}, 'scope': 'last_three_months',
                                 'first_ts': min((int(r.get(timestamp_field) or 0) for r in records), default=None),
                                 'last_ts': max((int(r.get(timestamp_field) or 0) for r in records), default=None)}
        print(label, len(records), len(requests), status, flush=True)

    single('balance', 'balance')
    single('funding', 'funding')
    single('positions', 'positions')
    single('orders', 'orders')
    single('bill_types', 'bill_types')
    for kind in ['conditional', 'oco', 'trigger', 'move_order_stop']:
        single('algo_' + kind, 'algo_orders', {'ordType': kind})
    window = {'begin': str(begin_ms), 'end': str(now_ms)}
    paged('bills', 'bills', window, 'billId')
    for kind in ['SPOT', 'MARGIN', 'SWAP', 'FUTURES', 'OPTION']:
        paged('fills_' + kind, 'fills', dict(window, instType=kind), 'billId', 'fillTime')
        paged('orders_' + kind, 'history_orders', dict(window, instType=kind), 'ordId', 'uTime')
    paged('history_positions', 'history_positions', {}, 'uTime', 'uTime', timestamp_cursor=True)
    safe_deposit = ['ccy', 'amt', 'state', 'ts', 'depId', 'type', 'actualDepBlkConfirm']
    safe_withdrawal = ['ccy', 'amt', 'fee', 'feeCcy', 'state', 'ts', 'wdId', 'type']
    paged('deposits', 'deposits', {'after': str(begin_ms), 'before': str(now_ms)}, 'depId', timestamp_cursor=True, keep=safe_deposit)
    paged('withdrawals', 'withdrawals', {'after': str(begin_ms), 'before': str(now_ms)}, 'wdId', timestamp_cursor=True, keep=safe_withdrawal)
    paged('funding_bills', 'funding_bills', {}, 'billId')
    instruments = set(r['instId'] for r in json.loads((DEST / 'positions.json').read_text()))
    for file in DEST.glob('fills_*.json'):
        instruments.update(r['instId'] for r in json.loads(file.read_text()))
    for inst_id in sorted(instruments):
        kind = 'SWAP' if inst_id.endswith('-SWAP') else 'SPOT'
        single('instrument_' + inst_id, 'instruments', {'instType': kind, 'instId': inst_id})
    for kind in ['SPOT', 'SWAP']:
        single('fee_' + kind, 'fee_rates', {'instType': kind})
    meta['finished_at'] = datetime.now(timezone.utc).isoformat()
    save('metadata.json', meta)
    print('采集结束，数据仅保存在本机私密目录', flush=True)


if __name__ == '__main__':
    main()
