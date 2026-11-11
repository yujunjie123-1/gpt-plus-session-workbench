"""Quarantine on explicit payment errors, eligibility, and no premature stock release."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from workbench.account_policy import payment_risk_code,ProviderRiskError
from workbench.engine import Engine
from workbench.store import Store
from workbench.provider import Provider
from test_inventory import StockProvider
from test_complete_flow import session,wait_until


class PaymentFailureProvider(StockProvider):
    def __init__(self,stage='lookup',code='card_declined',released=True,email='owner@example.com'):
        super().__init__(email)
        self.stage,self.code,self.released=stage,code,released

    async def call(self,kind,body):
        if kind=='preflight' and self.stage=='preflight':
            return {'email':self.email,'error_code':self.code,'message':'PRIVATE-REMOTE-RESPONSE-NEVER-SAVE'}
        if kind=='redeem':
            self.redeems+=1
            return {'error':{'code':self.code}} if self.stage=='redeem' else {}
        return await super().call(kind,body)

    async def lookup(self,code):
        if self.redeems:
            return {'cdk_code':code,'status':'failed','account_email':self.email,
                    'can_resubmit':self.released,'error_code':self.code,
                    'error_message':'PRIVATE-REMOTE-RESPONSE-NEVER-SAVE'}
        return {'cdk_code':code,'status':'unused'}

    async def result(self, token):
        return {'status':'failed','account_email':self.email,
                'can_resubmit':self.released,'error_code':self.code}


class RiskPoolTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.path=Path(self.temp.name)/'records.sqlite3'
        self.store=Store(self.path)
        self.engine=Engine(self.store)
        self.key=self.store.inventory_import_accounts([{'email':'owner@example.com','password':'TEST-PASSWORD'}])['ids'][0]
        self.store.inventory_import_cards('plus',['RISK-TEST-CARD-1','RISK-TEST-CARD-2'])
        self.engine.sessions[self.key]=session()

    def tearDown(self):
        self.engine.close();self.store.db.close();self.temp.cleanup()

    def begin(self,provider):
        async def get_provider(_):return provider
        self.engine.provider=get_provider
        quote=self.engine.inventory_command('quote',{'ids':[self.key],'sku':'plus'})
        task=self.engine.inventory_command('start',{'quote':quote['quote']})['tasks'][0]
        wait_until(lambda:self.engine.jobs[self.key].done())
        return self.store.charge_tasks[task]

    def test_only_explicit_decline_or_approve_error_is_classified(self):
        for text in ['card_declined','Your card has been declined.','银行卡拒付','银行卡被拒绝']:
            self.assertEqual(payment_risk_code({'error':{'message':text}}),'card_declined')
        for text in ['approve_not_allowed','approval not permitted','approve未允许','approve 未被允许']:
            self.assertEqual(payment_risk_code({'failure_reason':text}),'approve_not_allowed')
        for payload in [{'message':'request not allowed'},{'error':'network timeout'},{'error':'cdk expired'},
                        {'approved':False},{'can_purchase':False},{'message':'already subscribed'},
                        {'email':'card_declined@example.com'},{'accessToken':'approve_not_allowed'},
                        {'status':'failed','message':'insufficient funds'}]:
            self.assertIsNone(payment_risk_code(payload))

    def test_preflight_failure_quarantines_and_releases_only_unsubmitted_unused_stock(self):
        task=self.begin(PaymentFailureProvider(stage='preflight',code='approve_not_allowed'))
        risk=self.store.get(self.key)['risk']
        self.assertTrue(risk['active']);self.assertEqual(risk['reason'],'approve 未允许')
        self.assertEqual(task['status'],'error');self.assertEqual(task['risk']['code'],'approve_not_allowed')
        self.assertTrue(all(card['status']=='unused' for card in self.store.cards.values()))
        self.assertFalse(task['submitted'])
        self.assertNotIn('PRIVATE-REMOTE-RESPONSE-NEVER-SAVE',json.dumps(self.engine.inventory_snapshot()))
        self.assertNotIn(b'PRIVATE-REMOTE-RESPONSE-NEVER-SAVE',self.path.read_bytes())

    def test_redeem_and_lookup_failures_quarantine_and_do_not_submit_twice(self):
        for stage in ('redeem','lookup'):
            with self.subTest(stage=stage):
                if self.store.get(self.key).get('risk',{}).get('active'):
                    self.engine.inventory_command('risk-pool',{'ids':[self.key],'active':False})
                self.engine.sessions[self.key]=session()
                provider=PaymentFailureProvider(stage=stage)
                task=self.begin(provider)
                self.assertEqual(task['status'],'failed');self.assertFalse(task['submitted'])
                self.assertEqual(provider.redeems,1)
                self.assertIn('银行卡拒付',task['message']);self.assertTrue(self.store.get(self.key)['risk']['active'])
                self.assertTrue(all(card['status']=='unused' for card in self.store.cards.values()))
                with self.assertRaises(ValueError):self.store.inventory_quote([self.key],'plus')

    def test_unconfirmed_submission_stays_locked_after_manual_restore(self):
        task=self.begin(PaymentFailureProvider(released=False))
        self.assertEqual(task['status'],'pending');self.assertTrue(task['submitted'])
        self.assertEqual(self.store.cards[task['card_id']]['status'],'reserved')
        self.engine.inventory_command('risk-pool',{'ids':[self.key],'active':False})
        self.assertFalse(self.store.get(self.key)['risk']['active'])
        self.assertTrue(self.store.get(self.key)['submitted'])
        with self.assertRaises(ValueError):self.store.inventory_quote([self.key],'plus')
        self.assertEqual(self.store.cards[task['card_id']]['status'],'reserved')

    def test_ordinary_failure_and_another_accounts_response_do_not_quarantine(self):
        task=self.begin(PaymentFailureProvider(code='ordinary_error'))
        self.assertEqual(task['status'],'failed')
        self.assertFalse(self.store.get(self.key).get('risk',{}).get('active'))
        self.engine.sessions[self.key]=session()
        self.begin(PaymentFailureProvider(stage='preflight',email='different@example.com'))
        self.assertFalse(self.store.get(self.key).get('risk',{}).get('active'))

    def test_stale_quotes_cannot_reserve_quarantined_or_previously_charged_accounts(self):
        quote=self.engine.inventory_command('quote',{'ids':[self.key],'sku':'plus'})
        self.engine.inventory_command('risk-pool',{'ids':[self.key],'active':True})
        with self.assertRaises(ValueError):self.engine.inventory_command('start',{'quote':quote['quote']})
        self.assertEqual(self.store.charge_tasks,{})
        self.engine.inventory_command('risk-pool',{'ids':[self.key],'active':False})
        quote=self.engine.inventory_command('quote',{'ids':[self.key],'sku':'plus'})
        self.store.update(self.key,last_successful_recharge={'status':'completed','sku':'plus'})
        with self.assertRaises(ValueError):self.engine.inventory_command('start',{'quote':quote['quote']})
        self.assertTrue(all(card['status']=='unused' for card in self.store.cards.values()))
        public=self.engine.inventory_snapshot()['accounts'][0]
        self.assertFalse(public['recharge_eligible']);self.assertTrue(public['has_recharged'])

    def test_success_history_survives_account_deletion_and_reimport(self):
        self.begin(StockProvider())
        self.engine.inventory_command('delete',{'kind':'accounts','ids':[self.key]})
        again=self.store.inventory_import_accounts([{'email':'OWNER@example.com'}])['ids'][0]
        self.assertTrue(self.store.has_successful_recharge(self.store.get(again)))
        with self.assertRaises(ValueError):self.store.inventory_quote([again],'plus')
        with self.assertRaises(ValueError):self.engine.command('preflight',[again])

    def test_quarantine_and_manual_restore_survive_reopen_without_resetting_age(self):
        self.store.quarantine_account(self.key,'card_declined')
        before=self.store.get(self.key)['risk']
        self.store.quarantine_account(self.key,'card_declined')
        self.assertEqual(self.store.get(self.key)['risk'],before)
        reopened=Store(self.path)
        try:
            self.assertEqual(reopened.get(self.key)['risk'],before)
            reopened.restore_account(self.key)
        finally:reopened.db.close()
        reopened=Store(self.path)
        try:
            risk=reopened.get(self.key)['risk']
            self.assertFalse(risk['active']);self.assertEqual(risk['since'],before['since'])
            self.assertEqual([event['action'] for event in risk['history']],['quarantine','restore'])
            self.assertTrue(reopened.inventory_public()['accounts'][0]['recharge_eligible'])
        finally:reopened.db.close()

    def test_provider_errors_keep_only_safe_reason_and_do_not_classify_lookup_errors(self):
        import json
        def fetch(url, body, headers, timeout):
            status = 422 if url.endswith(('/preflight','/redeem')) else 400
            return status, {}, json.dumps({'ok':False,'error':{
                'code':'CARD_DECLINED','message':'card_declined PRIVATE-PAYLOAD'}}).encode()
        provider=Provider('synthetic-device-id-123456', password='test', fetch_impl=fetch)
        for action in ('preflight','redeem'):
            with self.assertRaises(ProviderRiskError) as result:
                asyncio.run(provider.call(action,{}))
            self.assertNotIn('PRIVATE-PAYLOAD',str(result.exception))
        with self.assertRaises(RuntimeError) as result:asyncio.run(provider.call('lookup',{}))
        self.assertNotIsInstance(result.exception,ProviderRiskError)

    def test_quarantined_account_can_check_session_without_allocating_stock(self):
        self.store.quarantine_account(self.key,'card_declined')
        async def forbidden(_):raise AssertionError('Must not contact the redemption provider')
        self.engine.provider=forbidden
        self.engine.inventory_command('session',{'ids':[self.key]})
        wait_until(lambda:self.engine.jobs[self.key].done())
        public=self.engine.inventory_snapshot()['accounts'][0]
        self.assertTrue(public['session_valid']);self.assertTrue(public['risk']['active'])
        self.assertFalse(public['recharge_eligible']);self.assertEqual(self.store.charge_tasks,{})


if __name__=='__main__':unittest.main(verbosity=2)
