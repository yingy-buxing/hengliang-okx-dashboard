"""为当前采集窗口补齐历史日标记价和现货价格，以及额外资产快照。"""
from okx_readonly import Client, PATHS
import collect_analysis
from collect_analysis import save
import json
import time
from datetime import datetime, timezone


def main(client=None, destination=None):
    DEST = destination if destination is not None else collect_analysis.DEST
    collect_analysis.DEST = DEST
    c = client if client is not None else Client('global')
    c.verify()
    meta = json.loads((DEST / 'metadata.json').read_text())
    for endpoint, params in [('valuation', {'ccy': 'USDT'}), ('savings', {}), ('staking', {})]:
        try:
            rows = c.get(endpoint, params)
            save(endpoint + '.json', rows)
            meta['sources'][endpoint] = {'path': PATHS[endpoint], 'params': params, 'rows': len(rows),
                'pages': 1, 'scope': 'supplementary_snapshot', 'complete': True,
                'collected_at': datetime.now(timezone.utc).isoformat()}
        except RuntimeError as error:
            meta['errors'][endpoint] = str(error)
    insts = sorted({r['instId'] for r in json.loads((DEST / 'fills_SWAP.json').read_text())}
                   | {r['instId'] for r in json.loads((DEST / 'positions.json').read_text())})
    pairs = [(i, 'mark_candles') for i in insts]
    pairs += [(i + '-USDT', 'spot_candles') for i in ['BTC', 'ETH', 'SOL']]
    for inst, endpoint in pairs:
        rows, requests = [], []
        after = meta['end_ms']
        for _ in range(3):
            params = {'instId': inst, 'bar': '1D', 'after': str(after), 'limit': '100'}
            batch = c.get(endpoint, params)
            requests.append(params)
            if not batch:
                break
            rows.extend(batch)
            if min(int(r[0]) for r in batch) < meta['begin_ms'] - 86400000:
                break
            after = min(int(r[0]) for r in batch) - 1
            time.sleep(0.2)
        rows = list({r[0]: r for r in rows}.values())
        label = 'candles_' + inst
        save(label + '.json', rows)
        meta['sources'][label] = {'path': PATHS[endpoint], 'requests': requests,
            'rows': len(rows), 'scope': 'daily_utc8',
            'complete': bool(rows) and min(int(r[0]) for r in rows) <= meta['begin_ms']}
        print(label, len(rows), flush=True)
        time.sleep(0.2)
    save('metadata.json', meta)


if __name__ == '__main__':
    main()
