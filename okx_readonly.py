"""本地 OKX 只读查询。凭据从 api.rtf 读取，不输出或复制密钥。"""
import argparse
import base64
import datetime
import hashlib
import hmac
import json
from pathlib import Path
import re
import subprocess
import ssl
import sys
import urllib.error
import urllib.request
import urllib.parse

ROOT = Path(__file__).resolve().parent
HOSTS = {'global': 'www.okx.com', 'us': 'us.okx.com', 'eu': 'eea.okx.com'}
PATHS = {'config': '/api/v5/account/config', 'balance': '/api/v5/account/balance',
         'funding': '/api/v5/asset/balances', 'positions': '/api/v5/account/positions',
         'orders': '/api/v5/trade/orders-pending',
         'bills': '/api/v5/account/bills-archive',
         'fills': '/api/v5/trade/fills-history',
         'history_orders': '/api/v5/trade/orders-history-archive',
         'history_positions': '/api/v5/account/positions-history',
         'algo_orders': '/api/v5/trade/orders-algo-pending',
         'deposits': '/api/v5/asset/deposit-history',
         'withdrawals': '/api/v5/asset/withdrawal-history',
         'funding_bills': '/api/v5/asset/bills-history',
         'valuation': '/api/v5/asset/asset-valuation',
         'savings': '/api/v5/finance/savings/balance',
         'staking': '/api/v5/finance/staking-defi/orders-active',
         'bill_types': '/api/v5/account/subtypes',
         'fee_rates': '/api/v5/account/trade-fee',
         'instruments': '/api/v5/public/instruments',
         'mark_candles': '/api/v5/market/history-mark-price-candles',
         'spot_candles': '/api/v5/market/history-candles',
         'time': '/api/v5/public/time'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, site, credentials=None):
        if credentials is None:
            result = subprocess.run(['textutil', '-convert', 'txt', '-stdout', str(ROOT / 'api.rtf')],
                                    capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError('无法读取 api.rtf')
            fields = {}
            for line in result.stdout.splitlines():
                pair = re.split(r'[:：=]', line, maxsplit=1)
                if len(pair) == 2:
                    fields[pair[0].strip().lower()] = pair[1].strip().strip('"“”' + "'")
            credentials = {'key': fields.get('apikey', ''), 'secret': fields.get('secretkey', ''),
                           'passphrase': fields.get('passphrase', '')}
        self.key = credentials.get('key', '')
        self.secret = credentials.get('secret', '')
        self.passphrase = credentials.get('passphrase', '')
        if not all([self.key, self.secret, self.passphrase]):
            raise RuntimeError('缺少 API Key、Secret Key 或 Passphrase')
        self.host = HOSTS[site]
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def get(self, name, params=None):
        path = PATHS[name]
        if params:
            allowed = {'instType', 'instId', 'instFamily', 'after', 'before', 'begin', 'end', 'limit', 'ordType', 'type', 'ccy', 'bar'}
            if set(params) - allowed:
                raise RuntimeError('不支持的查询参数')
            path += '?' + urllib.parse.urlencode(params)
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        signature = base64.b64encode(hmac.new(self.secret.encode(), (timestamp + 'GET' + path).encode(), hashlib.sha256).digest()).decode()
        headers = {
            'OK-ACCESS-KEY': self.key, 'OK-ACCESS-SIGN': signature,
            'OK-ACCESS-TIMESTAMP': timestamp, 'OK-ACCESS-PASSPHRASE': self.passphrase,
            'User-Agent': 'Local-OKX-Readonly/1.0'}
        if name in {'instruments', 'time', 'mark_candles', 'spot_candles'}:
            headers = {'User-Agent': 'Local-OKX-Readonly/1.0'}
        request = urllib.request.Request('https://' + self.host + path, method='GET', headers=headers)
        try:
            with self.opener.open(request, timeout=20) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read())
            except (ValueError, UnicodeError):
                raise RuntimeError(f'HTTP {error.code}，请检查网络或账户站点') from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError('连接失败，请检查网络、代理和账户所属站点') from None
        if str(payload.get('code')) != '0':
            message = str(payload.get('msg', '请求失败'))
            for credential in (self.key, self.secret, self.passphrase):
                message = message.replace(credential, '[已隐藏]')
            raise RuntimeError(f"OKX 错误 {payload.get('code')}: {message}")
        return payload.get('data', [])

    def verify(self):
        data = self.get('config')
        if not data:
            raise RuntimeError('账户配置为空，无法验证权限')
        permissions = {p.strip().lower() for p in str(data[0].get('perm', '')).split(',') if p.strip()}
        if permissions not in ({'read'}, {'read_only'}):
            raise RuntimeError('服务端未确认仅有 Read 权限，请在 OKX 检查密钥权限')
        return data[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check', 'balance', 'funding', 'positions', 'orders'], nargs='?', default='check')
    parser.add_argument('--site', choices=HOSTS, default='global')
    args = parser.parse_args()
    try:
        client = Client(args.site)
        client.verify()
        if args.command == 'check':
            balance = client.get('balance')
            funding = client.get('funding')
            positions = client.get('positions')
            orders = client.get('orders')
            print(json.dumps({'连接': '成功', '实际权限': 'Read（只读）',
                              '交易账户币种数': len(balance[0].get('details', [])) if balance else 0,
                              '资金账户币种数': len(funding), '持仓条目数': len(positions),
                              '未完成普通订单数': len(orders)}, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(client.get(args.command), ensure_ascii=False, indent=2))
    except (RuntimeError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
