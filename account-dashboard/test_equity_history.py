import copy
import json
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch
import equity_history as equity
import history_store as store

class EquityTest(unittest.TestCase):
    def event(self,day,**values):
        row={'billId':str(day),'ts':store.stamp(f'2026-01-{day:02} 12:00:00'),'type':'other','ccy':'USDT','balChg':'0','trade':False,'posBalChg':'0'};row.update(values);return row
    def model(self,events,balance,positions=None):
        return {'begin':store.stamp('2026-01-01 00:00:00'),'end':store.stamp('2026-01-04 00:00:00'),'events':events,'contracts':{'TEST-USDT-SWAP':Decimal(1)},'positions':positions or [],'balance':{'details':[{'ccy':'USDT','cashBal':str(balance)}]}}
    def prices(self,values):return {'TEST-USDT-SWAP':{store.stamp(f'2026-01-{day+1:02} 00:00:00'):Decimal(str(value)) for day,value in values.items()}}
    def test_cash_position_and_transfer_independent_expected_values(self):
        key=('TEST-USDT-SWAP','cross')
        events=[self.event(1,trade=True,key=key,q=Decimal(1),px=Decimal(100),pnl='0',balChg='-1',bal='99'),self.event(2,trade=True,key=key,q=Decimal(-1),px=Decimal(120),pnl='20',balChg='19',bal='118'),self.event(3,type='1',balChg='50',bal='168')]
        out=equity.reconstruct(self.model(events,168),self.prices({1:110}))
        self.assertEqual([r['重建权益'] for r in out['daily']],[109,118,168])
        self.assertAlmostEqual(out['daily'][1]['日收益率'],118/109-1)
        self.assertEqual(out['daily'][2]['日收益率'],0)
        self.assertTrue(out['riskAvailable']);self.assertEqual(out['validations']['历史余额校验批次'],3)
    def test_full_rounds_allocate_fees_funding_and_reversal(self):
        key=('TEST-USDT-SWAP','cross')
        events=[self.event(1,trade=True,key=key,q=Decimal(1),px=Decimal(100),pnl='0',fee='-1',balChg='-1',bal='99'),
                self.event(2,type='8',instId=key[0],pnl='-2',balChg='-2',bal='97'),
                self.event(2,trade=True,key=key,q=Decimal(-2),px=Decimal(120),pnl='20',fee='-2',balChg='18',bal='115'),
                self.event(3,trade=True,key=key,q=Decimal(1),px=Decimal(110),pnl='10',fee='-1',balChg='9',bal='124')]
        out=equity.reconstruct(self.model(events,124),self.prices({1:110,2:120}))
        self.assertTrue(out['roundAvailable']);self.assertEqual(len(out['rounds']),2)
        self.assertEqual([r['净盈亏'] for r in out['rounds']],[16,8])
        self.assertEqual([r['手续费'] for r in out['rounds']],[-2,-2])
        self.assertEqual(out['rounds'][0]['资金费'],-2)
        self.assertFalse(any(r['跨期'] for r in out['rounds']))

    def test_redeposit_after_tiny_balance_weights_new_capital(self):
        events=[self.event(1,balChg='0',bal='0.0005'),self.event(2,type='1',balChg='10',bal='10.0005'),self.event(2,balChg='0.1595',bal='10.16')]
        result=equity.reconstruct(self.model(events,'10.16'),{})
        row=result['daily'][1]
        self.assertEqual(row['资金流加权估值'],5)
        self.assertAlmostEqual(row['日收益率'],.1595/5.0005)
        self.assertLess(row['日收益率'],.04)

    def test_missing_ledger_event_fails_independent_balance_check(self):
        events=[self.event(1,balChg='-1',bal='99'),self.event(3,balChg='50',bal='168')]
        out=equity.reconstruct(self.model(events,168),{})
        self.assertIsNone(out['daily'][0]['重建权益']);self.assertEqual(out['daily'][2]['重建权益'],168);self.assertEqual(out['equityHistory']['state'],'partial')
    def test_initial_isolated_short_and_liquidation(self):
        key=('TEST-USDT-SWAP','isolated')
        events=[self.event(2,trade=True,key=key,q=Decimal(1),px=Decimal(120),pnl='-20',balChg='-20',bal='70',posBalChg='-10')]
        out=equity.reconstruct(self.model(events,70),self.prices({1:110}))
        self.assertEqual([r['重建权益'] for r in out['daily']],[90,70,70])
        self.assertEqual(out['validations']['平仓盈亏最大误差'],0)
        self.assertTrue(out['rounds'][0]['跨期'])
    def test_missing_prices_are_null_and_risk_does_not_cross_gap(self):
        key=('TEST-USDT-SWAP','cross')
        events=[self.event(1,trade=True,key=key,q=Decimal(1),px=Decimal(100),pnl='0',balChg='0',bal='100')]
        positions=[{'instId':key[0],'mgnMode':key[1],'pos':'1','avgPx':'100'}]
        out=equity.reconstruct(self.model(events,100,positions),self.prices({1:110,3:120}))
        self.assertIsNone(out['daily'][1]['重建权益']);self.assertIsNone(out['daily'][2]['日收益率']);self.assertFalse(out['riskAvailable'])
        self.assertEqual(out['equityHistory']['state'],'partial');self.assertEqual(out['equityHistory']['segments'],2)
    def test_zero_equity_and_restart_do_not_create_infinite_returns(self):
        events=[self.event(1,balChg='-100',bal='0'),self.event(2,type='1',balChg='100',bal='100')]
        out=equity.reconstruct(self.model(events,100),{})
        self.assertEqual(out['daily'][0]['重建权益'],0);self.assertIsNone(out['daily'][1]['日收益率']);self.assertEqual(out['daily'][2]['日收益率'],0)
    def test_missing_latest_balance_anchor_rejects_full_result(self):
        events=[self.event(1,balChg='0',bal='100')]
        with self.assertRaisesRegex(AssertionError,'校验字段'):equity.reconstruct(self.model(events,200),{})
    def test_unconfirmed_tail_prices_do_not_enter_cache(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(store,'ROOT',Path(tmp)/'db'),patch.object(equity.time,'sleep'):
            end=store.stamp('2026-01-04 12:00:00')
            class Fake:
                host='test';calls=0
                def get(self,*args):
                    self.calls+=1
                    return [[str(end-1000),'1','1','1','99','0'],[str(end-2*equity.DAY),'1','1','1','7','1']] if self.calls==1 else []
            client=Fake();model={'begin':end-3*equity.DAY,'end':end,'events':[{'key':('TEST-USDT-SWAP','cross'),'ccy':'USDT'}],'positions':[],'balance':{'details':[]}}
            values,_=equity.fetch_prices(client,Path(tmp),model)
            self.assertNotIn(end-1000+equity.DAY,values['TEST-USDT-SWAP'])
            self.assertEqual(values['TEST-USDT-SWAP'][end-equity.DAY],Decimal(7))
    def test_public_history_paginates_past_three_pages_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(store,'ROOT',Path(tmp)/'db'),patch.object(equity.time,'sleep'):
            end=store.stamp('2026-01-04 00:00:00');begin=end-400*equity.DAY
            rows=[[str(end-i*equity.DAY),'1','1','1','1','1'] for i in range(1,451)]
            class Fake:
                host='official-example';calls=0
                def get(self,endpoint,params):
                    self.calls+=1;return [r for r in rows if int(r[0])<int(params['after'])][:100]
            client=Fake();model={'begin':begin,'end':end,'events':[{'key':('TEST-USDT-SWAP','cross'),'ccy':'USDT'}],'positions':[],'balance':{'details':[]}}
            result,failed=equity.fetch_prices(client,Path(tmp),model)
            self.assertGreaterEqual(client.calls,4);self.assertFalse(failed);self.assertIn(end-350*equity.DAY,result['TEST-USDT-SWAP'])
            client.calls=0;equity.fetch_prices(client,Path(tmp),model);self.assertLessEqual(client.calls,1)

if __name__=='__main__':unittest.main()
