import copy,json,tempfile,unittest
from pathlib import Path
import history_store as store

class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=store.ROOT;store.ROOT=Path(self.tmp.name)/'source';self.account=store.key('global','fixture-only')
        with store.connect() as db:
            db.execute('INSERT INTO accounts VALUES(?,?,?,?,?)',(self.account,10000,1000,'[]',json.dumps({'summary':{'fixture':1},'daily':[]})))
            store.insert(db,self.account,'bills','a',5000,{'billId':'a','ts':'5000'},'api')
            store.insert(db,self.account,'bills','f',1500,{'billId':'f','ts':'1500'},'file')
            db.execute('INSERT INTO windows VALUES(?,?,?,?,?)',(self.account,'bills',4000,10000,1))
            db.execute('INSERT INTO file_windows VALUES(?,?,?,?,?)',(self.account,'batch','bills',1000,4000))
            db.execute('INSERT INTO imports VALUES(?,?,?,?,?)',(self.account,'batch','交易账单',1,1500))
            db.execute('INSERT INTO import_details VALUES(?,?,?)',(self.account,'batch',json.dumps({'有效新增':1})))
    def tearDown(self):store.ROOT=self.old;self.tmp.cleanup()
    def test_round_trip_preserves_history_coverage_cache_and_imports(self):
        original=store.make_backup(self.account);store.ROOT=Path(self.tmp.name)/'restored'
        self.assertEqual(store.backup_preview(self.account,original)['可新增'],2)
        store.restore_backup(self.account,original);restored=store.make_backup(self.account)
        self.assertEqual(original['payload'],restored['payload'])
        self.assertEqual(store.backup_preview(self.account,original)['可新增'],0)
        store.restore_backup(self.account,original);self.assertEqual(store.describe(self.account)['storedRecords'],2)
    def test_corruption_account_mismatch_and_shape_rejected_without_changes(self):
        original=store.make_backup(self.account);bad=copy.deepcopy(original);bad['payload']['tables']['records'][0][2]=99
        with self.assertRaises(RuntimeError):store.restore_backup(self.account,bad)
        with self.assertRaises(RuntimeError):store.restore_backup('different-account',original)
        bad=copy.deepcopy(original);bad['payload']['tables']['records'][0].append('unexpected');bad['sha256']=store.backup_digest(bad['payload'])
        with self.assertRaises(RuntimeError):store.restore_backup(self.account,bad)
        self.assertEqual(store.make_backup(self.account)['payload'],original['payload'])
    def test_existing_record_not_overwritten_by_backup(self):
        original=store.make_backup(self.account)
        with store.connect() as db:store.insert(db,self.account,'bills','a',5000,{'billId':'a','ts':'5000','note':'newer'},'api')
        store.restore_backup(self.account,original)
        self.assertEqual(next(r for r in store.effective(self.account,'bills') if r['billId']=='a')['note'],'newer')
    def test_merge_coverage_not_sum_overlapping_windows(self):
        result=store.coverage_ranges([('bills',1000,3000,'API'),('bills',2000,4000,'API'),('bills',9000,10000,'API')])
        self.assertEqual(len(result),2)

if __name__=='__main__':unittest.main()
