"""仅监听本机的账户分析服务。启动：python3 account-dashboard/server.py"""
import json
import base64
import secrets
from datetime import datetime, timezone, timedelta
import subprocess
import sys
import threading
import time
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.parse import urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from okx_readonly import Client, HOSTS
import keychain_store
import history_store
KEYCHAIN_LOCK = threading.RLock()

PUBLIC = Path(__file__).parent / 'public'
SESSIONS = {}
LOCK = threading.RLock()
JOBS = threading.BoundedSemaphore(2)
PORT = 4180
TTL = 1800


def release(session):
    process = session.get('process')
    if process and process.poll() is None:
        process.terminate()
    session.clear()


def snapshot(c):
    balance = c.get('balance')
    if not balance:
        raise RuntimeError('没有取得账户余额')
    b = balance[0]
    def num(value): return float(value or 0)
    assets = [{'币种': r['ccy'], '权益': num(r.get('eq')), '现金余额': num(r.get('cashBal')),
               '美元权益': num(r.get('eqUsd')), '可用余额': num(r.get('availBal'))} for r in b.get('details', [])]
    positions = [{k: r.get(k) for k in ['instId', 'instType', 'posSide', 'mgnMode', 'pos', 'avgPx', 'markPx', 'upl', 'lever', 'liqPx', 'notionalUsd', 'margin', 'ccy']} for r in c.get('positions') if num(r.get('pos')) != 0]
    extras, warnings,blocks = {}, [], {name:{'status':'ready'} for name in ['assets','positions']}
    for name in ['funding', 'orders']:
        try:
            extras[name] = c.get(name);blocks[name]={'status':'ready'}
        except RuntimeError:
            extras[name] = [];label={'funding':'资金账户','orders':'普通挂单'}[name]
            message=label+'暂未取得，请点击“刷新快照”重试。'
            warnings.append(message);blocks[name]={'status':'error','message':message}
    funding = [{'币种': r['ccy'], '余额': num(r.get('bal')), '可用余额': num(r.get('availBal'))} for r in extras['funding']]
    exposure = sum(abs(num(r.get('notionalUsd'))) for r in positions)
    return {'equity': num(b.get('totalEq')), 'assets': assets, 'positions': positions, 'funding': funding,
            'orderCount': len(extras['orders']) if blocks['orders']['status']=='ready' else None, 'exposure': exposure,
            'floating': sum(num(r.get('upl')) for r in positions if r.get('ccy') == 'USDT' or '-USDT-' in r.get('instId', '')), 'updated': datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S'), 'warnings': warnings, 'blocks':blocks}


def analyze(s):
    if not JOBS.acquire(blocking=False):
        s['status'] = 'error';s['message'] = '已有两项分析运行，请稍后重试。';return
    try:
        c = s['client']
        p = subprocess.Popen([sys.executable, str(Path(__file__).parent / 'worker.py')], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        with LOCK:
            if not s.get('client'): p.terminate();return
            s['process'] = p
        p.stdin.write(json.dumps({'key': c.key, 'secret': c.secret, 'passphrase': c.passphrase, 'site': s['site'], '_account': s['account']}) + '\n')
        p.stdin.close()
        timer = threading.Timer(1200, p.terminate);timer.start()
        try:
            for line in p.stdout:
                event = json.loads(line)
                with LOCK:
                    if not s.get('client'): break
                    if 'progress' in event: s['message'] = event['progress']
                    if 'result' in event: s['analysis'] = event['result'];s['status'] = 'ready';s['message'] = event['result'].get('sync',{}).get('message','分析完成')
                    if 'error' in event: s['status'] = 'error';s['message'] = event['error']
            p.wait()
        finally: timer.cancel()
        with LOCK:
            if s.get('status') == 'running': s['status'] = 'error';s['message'] = '分析超时或中断，请重试。'
    except Exception:
        with LOCK:
            if s.get('client'): s['status'] = 'error';s['message'] = '分析暂时未完成，请重试。'
    finally:
        JOBS.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def response(self, code, payload, content_type='application/json; charset=utf-8', cookie=None):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if content_type.startswith('application/json') else payload
        self.send_response(code)
        self.send_header('Content-Type', content_type);self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store');self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie: self.send_header('Set-Cookie', cookie)
        self.end_headers()
        try: self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError): pass
    def valid_host(self): return self.headers.get('Host') in {f'127.0.0.1:{PORT}', f'localhost:{PORT}'}
    def session(self, create=False, touch=False):
        cookie = SimpleCookie()
        try: cookie.load(self.headers.get('Cookie', ''))
        except Exception: pass
        sid = cookie.get('okx_session');sid = sid.value if sid else ''
        with LOCK:
            s = SESSIONS.get(sid)
            if s and time.time() - s['touched'] > TTL: release(s);del SESSIONS[sid];s = None
            if not s and create:
                if len(SESSIONS) >= 32: return '', None
                sid = secrets.token_urlsafe(32);s = {'csrf': secrets.token_urlsafe(32), 'touched': time.time(), 'status': 'idle', 'attempts': []};SESSIONS[sid] = s
            if s and touch: s['touched'] = time.time()
            return sid, s
    def do_GET(self):
        if not self.valid_host(): return self.response(403, {'error': '请求来源不允许'})
        path = urlsplit(self.path).path
        if path == '/api/state':
            sid, s = self.session(True)
            if s is None: return self.response(429, {'error': '会话数量达到上限'})
            with LOCK:
                payload = {k: s.get(k) for k in ['status', 'message', 'snapshot', 'analysis', 'csrf', 'site']}
                payload['connected'] = bool(s.get('client'))
                payload['credentialStore'] = keychain_store.storage_info()
                payload['local'] = history_store.describe(s['account'], int(time.time()*1000)) if s.get('account') else None
            try:
                with KEYCHAIN_LOCK: payload['savedAccount'] = keychain_store.exists()
                payload['keychainAvailable'] = True
            except RuntimeError:
                payload['savedAccount'] = False
                payload['keychainAvailable'] = False
            return self.response(200, payload, cookie=f'okx_session={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={TTL}')
        files = {'/period.js': ('period.js', 'text/javascript; charset=utf-8'), '/vendor/lightweight-charts.js': ('vendor/lightweight-charts.js', 'text/javascript; charset=utf-8'), '/': ('index.html', 'text/html; charset=utf-8'), '/numbers.js': ('numbers.js', 'text/javascript; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8'), '/favicon.svg': ('favicon.svg', 'image/svg+xml')}
        if path not in files: return self.response(404, {'error': '页面不存在'})
        file, mime = files[path]
        return self.response(200, (PUBLIC / file).read_bytes(), mime)
    def do_POST(self):
        if not self.valid_host(): return self.response(403, {'error': '请求来源不允许'})
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers['Host']: return self.response(403, {'error': '跨站请求被拒绝'})
        sid, s = self.session(touch=True)
        if not s or not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), s.get('csrf', '')): return self.response(403, {'error': '会话已过期，请刷新页面'})
        path = urlsplit(self.path).path
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 <= length <= (32*1024*1024 if path in ['/api/import','/api/import-preview','/api/backup-preview','/api/backup-restore'] else 8192): raise ValueError()
            body = json.loads(self.rfile.read(length) or '{}')
            if not isinstance(body, dict): raise ValueError()
        except Exception: return self.response(400, {'error': '请求格式无效'})
        path = urlsplit(self.path).path
        if path == '/api/disconnect':
            with LOCK:
                release(s);SESSIONS.pop(sid, None)
            return self.response(200, {'ok': True}, cookie='okx_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
        if path == '/api/forget':
            try:
                with KEYCHAIN_LOCK: keychain_store.delete()
            except RuntimeError as error: return self.response(400, {'error': str(error)})
            with LOCK:
                release(s);SESSIONS.pop(sid, None)
            return self.response(200, {'ok': True}, cookie='okx_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
        if path in ['/api/connect', '/api/restore']:
            with LOCK:
                if s.get('client') or s.get('connecting'): return self.response(409, {'error': '请先断开当前账户'})
                s['attempts'] = [t for t in s['attempts'] if time.time() - t < 60]
                if len(s['attempts']) >= 5: return self.response(429, {'error': '尝试过于频繁，请一分钟后再试'})
                s['attempts'].append(time.time());s['connecting'] = True
            try:
                if path == '/api/restore':
                    with KEYCHAIN_LOCK: body = keychain_store.load()
                site = body.get('site', 'global')
                if site not in HOSTS: raise RuntimeError('请选择账户所属站点')
                credentials = {k: body.get(k, '').strip() if isinstance(body.get(k), str) else '' for k in ['key', 'secret', 'passphrase']}
                if any(not v or len(v) > 512 for v in credentials.values()): raise RuntimeError('请填写完整的 API Key、Secret Key 和 Passphrase')
                c = Client(site, credentials);config=c.verify();data = snapshot(c)
                uid=str(config.get('uid') or '')
                account=history_store.key(site,uid)
                previous=history_store.cached(account)
                if previous and 'ledger' not in previous:previous=history_store.report(account,previous)
                if path == '/api/connect' and body.get('remember') is True:
                    with KEYCHAIN_LOCK: keychain_store.save(dict(credentials, site=site))
                with LOCK:
                    if sid not in SESSIONS: return self.response(409, {'error': '会话已断开'})
                    s.update(client=c, site=site, uid=uid, account=account, snapshot=data, analysis=previous, status='running', message='正在同步 API 近期记录，本机历史已保留。')
                threading.Thread(target=analyze, args=(s,), daemon=True).start()
                return self.response(200, {'ok': True})
            except (RuntimeError, ValueError, OSError) as error: return self.response(400, {'error': str(error)})
            finally:
                with LOCK: s.pop('connecting', None)
        if path in ['/api/backup','/api/backup-preview','/api/backup-restore']:
            with LOCK:
                if not s.get('client'):return self.response(401, {'error':'请先连接对应账户，以确认历史归属'})
                if s.get('status')=='running' or s.get('importing') or s.get('refreshing'):return self.response(409, {'error':'请等待当前同步或快照更新完成'})
                account=s['account'];s['importing']=True
            try:
                if path=='/api/backup':return self.response(200,history_store.make_backup(account))
                if path=='/api/backup-preview':return self.response(200,{'preview':history_store.backup_preview(account,body.get('backup'))})
                feedback=history_store.restore_backup(account,body.get('backup'))
                result=history_store.report(account);history_store.save_cache(account,result)
                with LOCK:
                    if s.get('account')==account:
                        s.update(analysis=result,status='running',message='备份已合并，正在复核历史净值。')
                        threading.Thread(target=analyze,args=(s,),daemon=True).start()
                return self.response(200,{'ok':True,'restored':feedback})
            except RuntimeError as error:return self.response(400,{'error':str(error)})
            except Exception:return self.response(400,{'error':'备份内容无效，未完成恢复'})
            finally:
                with LOCK:s.pop('importing',None)
        if path in ['/api/import','/api/import-preview']:
            with LOCK:
                if not s.get('client'): return self.response(401, {'error': '请先接入账户'})
                if s.get('status') == 'running' or s.get('importing'): return self.response(409, {'error': '请等待当前同步完成后导入'})
                s['importing'] = True
                account,uid=s['account'],s['uid']
            try:
                entries=body.get('files')
                if not isinstance(entries,list) or not 1 <= len(entries) <= 12: raise RuntimeError('请选择 1 至 12 个历史文件')
                files=[(str(e['name']),base64.b64decode(e['data'],validate=True)) for e in entries]
                feedback=history_store.import_files(account,uid,files,preview=path=='/api/import-preview')
                if path=='/api/import-preview':return self.response(200,{'files':feedback})
                result=history_store.report(account)
                history_store.save_cache(account,result)
                with LOCK:
                    if s.get('account') == account:
                        s['analysis']=result;s['status']='running';s['message']='导入完成，正在重建合并历史净值。'
                        threading.Thread(target=analyze,args=(s,),daemon=True).start()
                return self.response(200, {'ok':True,'files':feedback})
            except RuntimeError as error: return self.response(400, {'error':str(error)})
            except Exception: return self.response(400, {'error':'文件内容无法识别，未完成导入；请使用原始 OKX ZIP 或 CSV'})
            finally:
                with LOCK: s.pop('importing',None)
        if path in ['/api/refresh', '/api/analyze']:
            with LOCK:
                c = s.get('client')
                if not c: return self.response(401, {'error': '请先接入账户'})
                if s.get('status') == 'running' or s.get('refreshing') or s.get('importing'): return self.response(409, {'error': '当前任务还在进行中'})
                s['refreshing'] = True
            try:
                data = snapshot(c)
                with LOCK:
                    if not s.get('client'): return self.response(409, {'error': '会话已断开'})
                    s['snapshot'] = data
                    if path == '/api/analyze': s.update(status='running', message='正在重新采集最近三个月数据。')
                if path == '/api/analyze': threading.Thread(target=analyze, args=(s,), daemon=True).start()
                return self.response(200, {'ok': True})
            except RuntimeError as error: return self.response(400, {'error': str(error)})
            finally:
                with LOCK: s.pop('refreshing', None)
        return self.response(404, {'error': '接口不存在'})


def cleanup():
    while True:
        time.sleep(30)
        with LOCK:
            for sid in list(SESSIONS):
                if time.time() - SESSIONS[sid]['touched'] > TTL: release(SESSIONS.pop(sid))


if __name__ == '__main__':
    threading.Thread(target=cleanup, daemon=True).start()
    print(f'账户看板：http://127.0.0.1:{PORT}（仅本机）', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
