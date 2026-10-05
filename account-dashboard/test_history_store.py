import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
import history_store as store

class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=store.ROOT;store.ROOT=Path(self.tmp.name)/'db';self.account=store.key('global','123456')
    def tearDown(self):store.ROOT=self.root;self.tmp.cleanup()
    def api(self,begin,end,rows,complete=True):
        folder=Path(self.tmp.name)/'raw';folder.mkdir(exist_ok=True)
        (folder/'metadata.json').write_text(json.dumps({'begin_ms':begin,'end_ms':end,'sources':{k:{'pages':1,'complete':complete} for k in ['bills','fills_SWAP']}}))
        (folder/'bills.json').write_text(json.dumps(rows));(folder/'fills_SWAP.json').write_text('[]');store.api_ingest(self.account,folder)
    def file(self,uid='123456'):
        out=io.StringIO();w=csv.writer(out);w.writerow(['用户ID: '+uid+' UTC+8'])
        w.writerow(['id','关联订单id','时间','账单类型','交易品种','交易类型','数量','交易单位','成交价','收益','手续费','手续费单位','仓位余额变动','仓位余额','交易账户余额变动','交易账户余额','交易账户余额单位'])
        for bid,time,pnl in [('older','2026-01-01 12:00:00','7'),('export-other-id','2026-04-01 12:00:00','999')]:w.writerow([bid,'1',time,'永续合约','ETH-USDT-SWAP','卖出','1','张','2',pnl,'-1','USDT','0','0',pnl,'100','USDT'])
        return [('history.csv',out.getvalue().encode())]
    def test_api_window_priority_repeat_and_persistence(self):
        begin=store.stamp('2026-03-01 00:00:00');end=store.stamp('2026-05-01 00:00:00')
        self.api(begin,end,[{'billId':'api-id','ts':str(store.stamp('2026-04-01 12:00:00')),'instId':'ETH-USDT-SWAP','instType':'SWAP','ccy':'USDT','pnl':'3','fee':'-1','type':'2'}])
        store.import_files(self.account,'123456',self.file());again=store.import_files(self.account,'123456',self.file())
        self.assertTrue(again[0]['重复文件']);self.assertEqual(len(store.effective(self.account,'bills')),2)
        result=store.report(self.account);self.assertEqual(result['summary']['合约期间已实现净盈亏USDT'],8)
        self.assertEqual(sum(r['平仓毛盈亏']+r['手续费']+r['资金费']+r['其他调整'] for r in result['ledger']),8)
        self.assertEqual(len(result['ledger']),2)
        self.assertTrue(result['periodCoverage']['bills'])
        self.assertNotIn('raw',result['ledger'][0]);self.assertNotIn('billId',result['ledger'][0])
        store.save_cache(self.account,result);self.assertEqual(store.cached(self.account)['summary'],result['summary'])
        self.api(store.stamp('2026-04-01 00:00:00'),store.stamp('2026-06-01 00:00:00'),[])
        self.assertEqual(len(store.effective(self.account,'bills')),2)
        self.assertIsNone(store.cached(store.key('global','789')))
    def test_preview_does_not_import_and_feedback_reconciles(self):
        self.api(store.stamp('2026-03-01 00:00:00'),store.stamp('2026-05-01 00:00:00'),[])
        preview=store.import_files(self.account,'123456',self.file(),preview=True)
        self.assertEqual(preview[0]['有效新增'],1);self.assertEqual(preview[0]['API覆盖'],1)
        self.assertEqual(store.describe(self.account)['storedRecords'],0)
        actual=store.import_files(self.account,'123456',self.file())
        self.assertEqual(preview,actual)
        again=store.import_files(self.account,'123456',self.file(),preview=True)[0]
        self.assertEqual(again['重复记录'],2)
        self.assertEqual(again['读取记录'],again['有效新增']+again['重复记录']+again['API覆盖'])

    def test_identity_mismatch_and_atomic_batch(self):
        with self.assertRaises(RuntimeError):store.import_files(self.account,'123456',self.file()+self.file('789'))
        self.assertEqual(store.describe(self.account)['storedRecords'],0)
    def test_gap_uses_sync_time_not_last_trade_and_failed_sync_does_not_advance(self):
        last=store.stamp('2026-01-01 00:00:00');now=store.stamp('2026-05-01 00:00:00')
        self.api(store.three_months_before(last),last,[])
        self.assertTrue(store.describe(self.account,now)['gaps'])
        self.api(store.three_months_before(now),now,[],False)
        self.assertEqual(store.describe(self.account)['lastSync'],store.date(last))
        self.api(store.three_months_before(now),now,[])
        self.assertTrue(store.describe(self.account)['gaps'])
        self.assertEqual(store.describe(self.account)['lastSync'],store.date(now))
    def test_monthly_reopen_has_no_gap(self):
        last=store.stamp('2026-01-01 00:00:00');now=store.stamp('2026-02-01 00:00:00')
        self.api(store.three_months_before(last),last,[])
        self.assertFalse(store.describe(self.account,now)['gaps'])

    def recent(self,complete=True):
        return {'range':{'start':'2026-03-01','end':'2026-05-01'},'riskAvailable':False,'summary':{},'coverage':[{'数据集':k,'完整':complete} for k in ['bills','fills_SWAP']],'errors':{} if complete else {'bills':'测试失败'}}
    def test_failed_first_sync_keeps_unknown_and_does_not_hide_files(self):
        store.import_files(self.account,'123456',self.file())
        self.api(store.stamp('2026-03-01 00:00:00'),store.stamp('2026-05-01 00:00:00'),[],False)
        self.assertEqual(len(store.effective(self.account,'bills')),2)
        # A file-backed result would already be cached by the import endpoint.
        result=store.finish_sync(self.account,Path(self.tmp.name)/'raw',self.recent(False))
        self.assertIsNone(result['summary']['合约期间已实现净盈亏USDT'])
        self.assertEqual(result['sync']['state'],'partial')
    def test_failed_second_page_preserves_previous_records_and_cache(self):
        begin=store.stamp('2026-03-01 00:00:00');end=store.stamp('2026-05-01 00:00:00')
        row={'billId':'original','ts':str(begin+1000),'instId':'ETH-USDT-SWAP','instType':'SWAP','ccy':'USDT','pnl':'12','fee':'-2','type':'2'}
        self.api(begin,end,[row]);result=store.finish_sync(self.account,Path(self.tmp.name)/'raw',self.recent())
        self.assertEqual(result['summary']['合约期间已实现净盈亏USDT'],10)
        self.api(begin,end+1000,[dict(row,pnl='999'),dict(row,billId='partial-new')],False)
        result=store.finish_sync(self.account,Path(self.tmp.name)/'raw',self.recent(False))
        self.assertEqual(result['summary']['合约期间已实现净盈亏USDT'],10)
        self.assertEqual(len(store.effective(self.account,'bills')),1)
        self.assertEqual(store.effective(self.account,'bills')[0]['pnl'],'12')
        self.assertEqual(result['sync']['statisticsAt'],store.date(end))
        self.assertEqual(store.cached(self.account)['sync']['state'],'partial')
    def test_bill_success_but_fill_failure_does_not_publish_half_core(self):
        self.api(1000,2000,[],False);folder=Path(self.tmp.name)/'raw'
        meta=json.loads((folder/'metadata.json').read_text());meta['sources']['bills']['complete']=True
        (folder/'metadata.json').write_text(json.dumps(meta));(folder/'bills.json').write_text(json.dumps([{'billId':'partial','ts':'1500'}]))
        result=store.finish_sync(self.account,folder,self.recent(False))
        self.assertEqual(store.effective(self.account,'bills'),[])
        self.assertIsNone(result['summary']['合约期间已实现净盈亏USDT'])
        self.assertIsNone(store.describe(self.account)['lastSync'])
    def test_complete_empty_account_is_zero_not_unknown(self):
        self.api(1000,2000,[]);result=store.finish_sync(self.account,Path(self.tmp.name)/'raw',self.recent())
        self.assertEqual(result['summary']['合约期间已实现净盈亏USDT'],0)
        self.assertEqual(result['sync']['state'],'complete')
    def test_legacy_incomplete_window_does_not_hide_file(self):
        store.import_files(self.account,'123456',self.file())
        with store.connect() as db:db.execute('INSERT INTO windows VALUES(?,?,?,?,?)',(self.account,'bills',0,store.stamp('2027-01-01 00:00:00'),0))
        self.assertEqual(len(store.effective(self.account,'bills')),2)

    def test_reimport_enriches_legacy_fields_without_duplicate_counts(self):
        files=self.file();store.import_files(self.account,'123456',files)
        with store.connect() as db:
            row=db.execute("SELECT identity,payload FROM records WHERE account=? AND dataset='bills' LIMIT 1",(self.account,)).fetchone()
            value=json.loads(row[1]);value.pop('raw');value.pop('sz')
            db.execute("UPDATE records SET payload=? WHERE account=? AND identity=?",(json.dumps(value),self.account,row[0]))
        again=store.import_files(self.account,'123456',files)
        self.assertTrue(again[0]['重复文件']);self.assertEqual(store.describe(self.account)['storedRecords'],2)
        self.assertTrue(all('raw' in r and 'sz' in r for r in store.effective(self.account,'bills')))
    def test_original_filename_records_declared_export_coverage(self):
        _,raw=self.file()[0];store.import_files(self.account,'123456',[('交易账单_2026-01-01~2026-05-01.csv',raw)])
        with store.connect() as db:row=db.execute('SELECT begin,end FROM file_windows WHERE account=?',(self.account,)).fetchone()
        self.assertEqual(row,(store.stamp('2026-01-01 00:00:00'),store.stamp('2026-05-02 00:00:00')))

if __name__=='__main__':unittest.main()
