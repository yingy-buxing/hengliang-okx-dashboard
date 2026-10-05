"""使用独立钥匙串测试条目验证保存与恢复，不使用真实账户凭据或 OKX 网络。"""
import http.cookiejar
import base64
import json
import secrets
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
import server
import tempfile
from pathlib import Path
import keychain_store as store


class SavedAccountTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_tmp=tempfile.TemporaryDirectory()
        cls.original_root=server.history_store.ROOT
        server.history_store.ROOT=Path(cls.data_tmp.name)/'data'
        cls.original_service=store.SERVICE
        store.SERVICE += ('.test.'+secrets.token_hex(8)).encode()
        cls.original_client=server.Client;cls.original_analyze=server.analyze;cls.original_port=server.PORT
        class FakeClient(server.Client):
            def __init__(self,site,credentials):
                self.key=credentials['key'];self.secret=credentials['secret'];self.passphrase=credentials['passphrase'];self.host=server.HOSTS[site]
            def get(self,name,params=None):
                if self.key=='invalid':raise RuntimeError('测试凭据未通过验证')
                if name=='config':return [{'perm':'read_only','uid':'123456'}]
                if name=='balance':return [{'totalEq':'100','details':[]}]
                return []
        server.Client=FakeClient
        server.analyze=lambda s:s.update(status='ready',message='测试完成')
        cls.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        server.PORT=cls.http.server_port
        cls.base='http://127.0.0.1:'+str(server.PORT)
        threading.Thread(target=cls.http.serve_forever,daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        server.history_store.ROOT=cls.original_root;cls.data_tmp.cleanup()
        store.delete();store.SERVICE=cls.original_service
        cls.http.shutdown();cls.http.server_close()
        server.Client=cls.original_client;server.analyze=cls.original_analyze;server.PORT=cls.original_port
    def setUp(self):
        store.delete()
        self.browser=self.new_browser()
        self.value={k:secrets.token_urlsafe(20) for k in ['key','secret','passphrase']}
        self.value.update(site='global',remember=True)
    def tearDown(self):store.delete()
    def new_browser(self):return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def state(self,browser=None):
        with (browser or self.browser).open(self.base+'/api/state') as r:return json.load(r)
    def post(self,path,body,browser=None,token=None):
        browser=browser or self.browser
        token=token if token is not None else self.state(browser)['csrf']
        req=urllib.request.Request(self.base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-CSRF-Token':token,'Origin':self.base})
        with browser.open(req) as r:return json.load(r)
    def test_save_restore_and_forget(self):
        self.post('/api/connect',self.value)
        self.assertTrue(self.state()['savedAccount'])
        result=self.state()
        for k in ['key','secret','passphrase']:self.assertNotIn(self.value[k],json.dumps(result))
        self.post('/api/disconnect',{})
        self.assertFalse(self.state()['connected']);self.assertTrue(store.exists())
        other=self.new_browser();self.assertFalse(self.state(other)['connected'])
        self.post('/api/restore',{},other)
        self.assertTrue(self.state(other)['connected'])
        self.post('/api/forget',{},other)
        self.assertFalse(self.state(other)['connected']);self.assertFalse(store.exists())
    def test_no_opt_in_does_not_save(self):
        self.value['remember']=False;self.post('/api/connect',self.value)
        self.assertFalse(store.exists())
    def test_invalid_credentials_do_not_overwrite_saved(self):
        store.save({k:v for k,v in self.value.items() if k!='remember'})
        previous=store.load()
        self.value['key']='invalid'
        with self.assertRaises(urllib.error.HTTPError):self.post('/api/connect',self.value)
        self.assertEqual(store.load(),previous)
    def test_restore_missing_account_is_controlled_error(self):
        with self.assertRaises(urllib.error.HTTPError) as error:self.post('/api/restore',{})
        self.assertEqual(error.exception.code,400)
        self.assertFalse(self.state()['connected'])
    def test_import_then_reconnect_loads_local_history(self):
        self.post('/api/connect',self.value)
        from test_history_store import HistoryTest
        raw=HistoryTest().file()[0][1]
        entries={'files':[{'name':'history.csv','data':base64.b64encode(raw).decode()}]}
        result=self.post('/api/import',entries)
        self.assertEqual(len(result['files']),1)
        self.assertEqual(self.state()['analysis']['summary']['合约期间已实现净盈亏USDT'],1004)
        self.post('/api/disconnect',{})
        self.post('/api/restore',{})
        self.assertEqual(self.state()['analysis']['summary']['合约期间已实现净盈亏USDT'],1004)
        self.assertTrue(self.state()['local']['storedRecords'])
    def test_backup_http_auth_preview_restore_and_integrity(self):
        self.post('/api/connect',self.value)
        account=server.history_store.key('global','123456')
        with server.history_store.connect() as db:
            db.execute('INSERT OR IGNORE INTO accounts(account) VALUES(?)',(account,))
            server.history_store.insert(db,account,'bills','backup-fixture',1000,{'billId':'backup-fixture','ts':'1000'},'api')
        backup=self.post('/api/backup',{})
        self.assertEqual(backup['format'],'hengliang-account-backup')
        for key in ['key','secret','passphrase']:self.assertNotIn(self.value[key],json.dumps(backup))
        self.assertEqual(self.post('/api/backup-preview',{'backup':backup})['preview']['可新增'],0)
        self.assertTrue(self.post('/api/backup-restore',{'backup':backup})['ok'])
        backup['sha256']='invalid'
        with self.assertRaises(urllib.error.HTTPError) as error:self.post('/api/backup-preview',{'backup':backup})
        self.assertEqual(error.exception.code,400)
        self.post('/api/disconnect',{})
        with self.assertRaises(urllib.error.HTTPError) as error:self.post('/api/backup',{})
        self.assertEqual(error.exception.code,401)

    def test_import_preview_http_does_not_write_records(self):
        self.post('/api/connect',self.value)
        from test_history_store import HistoryTest
        raw=HistoryTest().file()[0][1]
        before=self.state()['local']['storedRecords']
        result=self.post('/api/import-preview',{'files':[{'name':'history.csv','data':base64.b64encode(raw).decode()}]})
        self.assertEqual(result['files'][0]['读取记录'],2)
        self.assertEqual(self.state()['local']['storedRecords'],before)

    def test_snapshot_failure_is_distinct_from_empty(self):
        class FailingClient:
            def get(self,name):
                if name=='balance':return [{'totalEq':'100','details':[]}]
                if name in ['funding','orders']:raise RuntimeError('测试网络失败')
                return []
        result=server.snapshot(FailingClient())
        self.assertIsNone(result['orderCount'])
        self.assertEqual(result['blocks']['funding']['status'],'error')
        self.assertEqual(len(result['warnings']),2)
        class EmptyClient(FailingClient):
            def get(self,name):
                if name in ['funding','orders']:return []
                return super().get(name)
        result=server.snapshot(EmptyClient())
        self.assertEqual(result['orderCount'],0)
        self.assertEqual(result['blocks']['funding']['status'],'ready')
        self.assertEqual(result['warnings'],[])
    def test_invalid_csrf_cannot_delete(self):
        store.save({k:v for k,v in self.value.items() if k!='remember'})
        with self.assertRaises(urllib.error.HTTPError) as error:self.post('/api/forget',{},token='invalid')
        self.assertEqual(error.exception.code,403);self.assertTrue(store.exists())


if __name__=='__main__':unittest.main(verbosity=2)
