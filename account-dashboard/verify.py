"""本地服务的端到端复核；不会打印凭据或账户金额。"""
import http.cookiejar
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from okx_readonly import Client
BASE = 'http://127.0.0.1:4180'


def browser():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

def get(opener):
    with opener.open(BASE + '/api/state') as response: return json.load(response)

def post(opener, path, body, token, origin=BASE):
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', 'X-CSRF-Token': token, 'Origin': origin})
    with opener.open(request, timeout=120) as response: return json.load(response)

def rejected(opener, token, origin):
    try: post(opener, '/api/disconnect', {}, token, origin)
    except urllib.error.HTTPError as error: assert error.code == 403;return
    raise AssertionError('无效来源或令牌未被阻止')


if __name__ == '__main__':
    one, two = browser(), browser()
    s = get(one);other = get(two)
    assert s['csrf'] != other['csrf']
    rejected(one, 'invalid-token', BASE)
    rejected(one, s['csrf'], 'https://example.invalid')
    print('独立会话、跨站请求与无效令牌检查通过。', flush=True)
    client = Client('global')
    try:
        post(one, '/api/connect', {'key':client.key,'secret':client.secret,'passphrase':client.passphrase,'site':'global'}, s['csrf'])
        s = get(one);assert s['connected'];assert not get(two)['connected']
        assert all(value not in json.dumps(s) for value in [client.key,client.secret,client.passphrase])
        assert not {'key','secret','passphrase'}.intersection(s)
        print('真实只读接入、快照与账户隔离通过；等待历史分析。', flush=True)
        for i in range(300):
            time.sleep(3);s = get(one)
            if s['status'] != 'running':break
            if i % 20 == 19: print('采集与复核进行中。', flush=True)
        assert s['status'] == 'ready', s.get('message')
        a = s['analysis'];assert a['fills'];assert a['coverage']
        print('历史分析完成；风险指标可用：', a['riskAvailable'], '成交条目数：', len(a['fills']), flush=True)
        post(one, '/api/refresh', {}, s['csrf'])
        assert get(one)['snapshot'] and not get(two)['connected']
        print('刷新快照通过。', flush=True)
        if not a['riskAvailable']: print('净值未通过复核，已正确保留不可用状态。原因：', a['riskReason'], flush=True)
    finally:
        post(one, '/api/disconnect', {}, s['csrf']);assert not get(one)['connected']
        assert not get(two)['connected']
        print('断开与会话清理通过。', flush=True)
