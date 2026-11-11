"""Synthetic direct Session recharge and explicit card-detail coverage."""
import json
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from workbench.engine import Engine
from workbench.provider import PartnerAPIError, PartnerConfigurationError
from workbench.store import Store,crypt
from test_complete_flow import session,wait_until


class DirectProvider:
    def __init__(self):
        self.redeems=0;self.preflight_email='direct@example.com'
        self.preview_product='gpt';self.preview_plan='plus_half';self.calls=[]
        self.current_plan='free';self.subscription_has_active=False
    async def lookup(self,code):
        return {'cdk_code':code,'status':'unused','used':False,'can_resubmit':True}
    async def call(self,operation,body):
        self.calls.append((operation,body))
        if operation=='preview':
            return {'product':self.preview_product,'plan':self.preview_plan,'redemption_token':'SYNTHETIC-REDEMPTION'}
        if operation=='preflight':
            value=json.loads(body['credential']['session'])
            self.session_text=value['sessionToken']
            return {'email':self.preflight_email,'current_plan':self.current_plan,
                    'subscription_has_active':self.subscription_has_active,'preflight_token':'SYNTHETIC-PREFLIGHT'}
        raise AssertionError(operation)
    async def redeem(self,redemption_token,preflight_token,request_id):
        self.redeems+=1
        return {'status':'queued'}
    async def result(self,token):
        return {'status':'completed','account_email':'direct@example.com'}
    async def close(self):pass


class LookupErrorProvider(DirectProvider):
    async def lookup(self,code):
        raise PartnerAPIError('AUTHENTICATION_FAILED','synthetic authorization failure',status=401)


class MismatchedResultProvider(DirectProvider):
    async def result(self,token):
        return {'status':'completed','account_email':'other@example.com'}


class MissingEmailResultProvider(DirectProvider):
    async def result(self,token):
        return {'status':'completed'}


class RejectedDirectProvider(DirectProvider):
    def __init__(self):
        super().__init__();self.lookups=0
    async def lookup(self,code):
        self.lookups+=1
        if self.lookups==1:
            return {'cdk_code':code,'status':'unused','used':False,'can_resubmit':True}
        return {'cdk_code':code,'status':'unused','used':False,'can_resubmit':True}
    async def redeem(self,redemption_token,preflight_token,request_id):
        self.redeems+=1
        raise PartnerAPIError('INVALID_REQUEST','synthetic rejected request',status=422)


class RejectedUsedCardDirectProvider(RejectedDirectProvider):
    async def lookup(self,code):
        self.lookups+=1
        if self.lookups==1:
            return {'cdk_code':code,'status':'unused','used':False,'can_resubmit':True}
        return {'cdk_code':code,'status':'used','used':True,'can_resubmit':False,
                'account_email':'other@example.com'}


class RateLimitedDirectProvider(RejectedDirectProvider):
    async def redeem(self,redemption_token,preflight_token,request_id):
        self.redeems+=1
        raise PartnerAPIError('RATE_LIMITED','synthetic rate limit',status=429,
                              retryable=True,retry_after=5)


class SessionDirectTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.path=Path(self.temp.name)/'records.sqlite3'
        self.store=Store(self.path)
        self.engine=Engine(self.store)

    def tearDown(self):
        self.engine.close();self.store.db.close();self.temp.cleanup()

    def provider(self,value):
        async def get_provider(_):return value
        self.engine.provider=get_provider

    def test_card_secret_is_returned_only_by_explicit_detail(self):
        code='SYNTHETIC-CARD-DETAIL-1234'
        self.store.inventory_import_cards('plus',[code])
        key=next(iter(self.store.cards))
        state=self.store.inventory_public()
        self.assertNotIn(code,json.dumps(state))
        self.assertNotIn('secret',state['cards'][0])
        detail=self.engine.inventory_command('card-detail',{'id':key})
        self.assertEqual(detail['code'],code)
        self.assertNotIn('secret',detail)

    def test_lookup_auth_error_keeps_local_stock_and_has_safe_reason(self):
        self.store.inventory_import_cards('plus',['SYNTHETIC-CARD-LOOKUP-ERROR'])
        key=next(iter(self.store.cards))
        self.provider(LookupErrorProvider())
        self.engine.inventory_command('check-cards',{'ids':[key]})
        wait_until(lambda:self.engine.inventory_checks[key].done())
        card=self.store.cards[key]
        self.assertEqual(card['status'],'unused')
        self.assertEqual(card['check_error_code'],'AUTHENTICATION_FAILED')
        self.assertNotIn('synthetic authorization failure',card['check_error'])

    def test_missing_partner_password_has_a_distinct_configuration_error(self):
        from unittest.mock import patch
        import os
        self.store.inventory_import_cards('plus',['SYNTHETIC-CARD-MISSING-CONFIG'])
        key=next(iter(self.store.cards))
        with patch.dict(os.environ,{'CDK_PARTNER_PASSWORD':''}):
            self.engine.inventory_command('check-cards',{'ids':[key]})
            wait_until(lambda:self.engine.inventory_checks[key].done())
        card=self.store.cards[key]
        self.assertEqual(card['status'],'unused')
        self.assertEqual(card['check_error_code'],'PARTNER_CONFIGURATION_MISSING')
        self.assertIn('CDK_PARTNER_PASSWORD',card['check_error'])

    def test_direct_preview_reports_missing_partner_password_before_any_upstream_call(self):
        from unittest.mock import patch
        import os
        value=session('direct@example.com')
        payload={'code':'SYNTHETIC-DIRECT-NO-PASSWORD',
                 'session':json.dumps(value,separators=(',',':'))}
        with patch.dict(os.environ,{'CDK_PARTNER_PASSWORD':''}):
            with self.assertRaisesRegex(PartnerConfigurationError,'CDK_PARTNER_PASSWORD'):
                self.engine.session_direct_prepare(payload)

    def test_direct_recharge_is_attributed_and_never_persists_session(self):
        provider=DirectProvider();self.provider(provider)
        value=session('direct@example.com')
        value['sessionToken']='SYNTHETIC-DIRECT-SESSION-SECRET'
        preview=self.engine.session_direct_prepare({
            'code':'SYNTHETIC-DIRECT-CARD-1234',
            'session':json.dumps(value,separators=(',',':'))})
        self.assertEqual(preview['summary']['target_plan'],'gpt / plus_half')
        self.assertEqual(preview['summary']['product'],'gpt')
        self.assertEqual(preview['summary']['plan'],'plus_half')
        self.assertNotIn('sku',provider.calls[0][1])
        result=self.engine.session_direct_start({'quote':preview['quote']})
        task_id=result['task']['id']
        wait_until(lambda:self.store.charge_tasks[task_id].get('remote_terminal') is True)
        task=self.store.charge_tasks[task_id]
        self.assertEqual(task['status'],'completed')
        self.assertEqual((task['provider_product'],task['provider_plan']),('gpt','plus_half'))
        self.assertEqual(provider.redeems,1)
        self.assertEqual(self.store.rows,{})
        self.assertEqual(len(self.store.cards),0)
        self.assertNotIn('SYNTHETIC-DIRECT-SESSION-SECRET',json.dumps(self.store.charge_tasks))
        self.assertNotIn('SYNTHETIC-DIRECT-CARD-1234',json.dumps(self.engine.inventory_snapshot()))
        persisted=self.path.read_bytes()
        self.assertNotIn(b'SYNTHETIC-DIRECT-SESSION-SECRET',persisted)
        self.assertNotIn(b'SYNTHETIC-DIRECT-CARD-1234',persisted)
        self.assertNotIn('direct_card_secret',json.dumps(self.engine.inventory_snapshot()))

    def test_direct_recharge_uses_any_upstream_product_without_local_sku(self):
        provider=DirectProvider();provider.preview_product='ios';provider.preview_plan='5x';self.provider(provider)
        value=session('direct@example.com')
        preview=self.engine.session_direct_prepare({'code':'SYNTHETIC-IOS-5X',
            'session':json.dumps(value,separators=(',',':'))})
        self.assertEqual((preview['summary']['product'],preview['summary']['plan']),('ios','5x'))
        self.assertEqual(provider.calls[0][0],'preview')
        self.assertNotIn('sku',provider.calls[0][1])

    def test_direct_preview_accepts_upstream_product_and_plan_values_without_sku_mapping(self):
        provider=DirectProvider();self.provider(provider)
        value=session('direct@example.com')
        variants=[('gpt','plus'),('gpt','plus_half'),('ios','ios5x'),
                  ('card','5x'),('card','20x'),('card','50x'),('points','points')]
        for index,(product,plan) in enumerate(variants):
            provider.preview_product=product;provider.preview_plan=plan;provider.calls.clear()
            result=self.engine.session_direct_prepare({'code':f'SYNTHETIC-CARD-TYPE-{index}',
                'session':json.dumps(value,separators=(',',':'))})
            self.assertEqual((result['summary']['product'],result['summary']['plan']),(product,plan))
            requests=dict(provider.calls)
            self.assertNotIn('sku',requests['preview'])
            self.assertNotIn('sku',requests['preflight'])
        self.assertEqual(self.store.charge_tasks,{})

    def test_plus_subscription_conflict_check_is_scoped_to_gpt_products(self):
        provider=DirectProvider();provider.current_plan='plus';provider.subscription_has_active=True
        value=session('direct@example.com')
        provider.preview_product='ios';provider.preview_plan='plus';self.provider(provider)
        result=self.engine.session_direct_prepare({'code':'SYNTHETIC-IOS-PLUS-PLAN',
            'session':json.dumps(value,separators=(',',':'))})
        self.assertEqual(result['summary']['target_plan'],'ios / plus')
        provider.preview_product='GPT';provider.preview_plan='PLUS'
        with self.assertRaisesRegex(ValueError,'已有同档或更高套餐'):
            self.engine.session_direct_prepare({'code':'SYNTHETIC-GPT-PLUS-PLAN',
                'session':json.dumps(value,separators=(',',':'))})

    def test_duplicate_direct_card_is_rejected_before_second_submission(self):
        provider=DirectProvider();self.provider(provider)
        value=session('direct@example.com')
        payload={'code':'SYNTHETIC-DIRECT-DUPLICATE-1234','sku':'plus_half',
                 'session':json.dumps(value,separators=(',',':'))}
        preview=self.engine.session_direct_prepare(payload)
        self.engine.session_direct_start({'quote':preview['quote']})
        wait_until(lambda:provider.redeems==1)
        with self.assertRaisesRegex(ValueError,'已有独立充值记录'):
            self.engine.session_direct_prepare(payload)
        self.assertEqual(provider.redeems,1)

    def test_preflight_email_mismatch_blocks_redemption(self):
        provider=DirectProvider();provider.preflight_email='other@example.com';self.provider(provider)
        value=session('direct@example.com')
        with self.assertRaisesRegex(ValueError,'邮箱与 Session 邮箱不一致'):
            self.engine.session_direct_prepare({'code':'SYNTHETIC-DIRECT-PREFLIGHT-MISMATCH',
                'sku':'plus_half','session':json.dumps(value,separators=(',',':'))})
        self.assertEqual(provider.redeems,0)
        self.assertEqual(self.store.rows,{})
        self.assertEqual(self.store.charge_tasks,{})

    def test_completed_direct_redemption_is_attributed_to_preflight_session_email(self):
        provider=MismatchedResultProvider();self.provider(provider)
        value=session('direct@example.com')
        preview=self.engine.session_direct_prepare({'code':'SYNTHETIC-DIRECT-WRONG-OWNER',
            'sku':'plus_half','session':json.dumps(value,separators=(',',':'))})
        task_id=self.engine.session_direct_start({'quote':preview['quote']})['task']['id']
        wait_until(lambda:self.store.charge_tasks[task_id].get('remote_terminal') is True)
        task=self.store.charge_tasks[task_id]
        self.assertEqual(task['status'],'completed')
        self.assertEqual(task['provider_result_email'],'other@example.com')
        self.assertEqual(task['result_email'],'direct@example.com')
        self.assertIn('自动记账',task['message'])
        self.assertEqual(self.store.rows,{})
        self.assertEqual(provider.redeems,1)

    def test_completed_direct_redemption_without_result_email_is_attributed_without_lookup(self):
        provider=MissingEmailResultProvider();self.provider(provider)
        value=session('direct@example.com')
        preview=self.engine.session_direct_prepare({'code':'SYNTHETIC-DIRECT-MISSING-EMAIL',
            'session':json.dumps(value,separators=(',',':'))})
        task_id=self.engine.session_direct_start({'quote':preview['quote']})['task']['id']
        wait_until(lambda:self.store.charge_tasks[task_id].get('remote_terminal') is True)
        task=self.store.charge_tasks[task_id]
        self.assertEqual(task['status'],'completed')
        self.assertEqual(task['result_email'],'direct@example.com')
        self.assertEqual(task['provider_result_email'],'上游未提供')
        self.assertEqual(task['message'],'上游已确认兑换成功，已按本次 Session 预检邮箱自动记账；上游未提供结果邮箱')

    def test_definite_direct_rejection_releases_only_after_card_lookup_allows_retry(self):
        provider=RejectedDirectProvider();self.provider(provider)
        value=session('direct@example.com')
        preview=self.engine.session_direct_prepare({'code':'SYNTHETIC-DIRECT-REJECTED-RETRY',
            'session':json.dumps(value,separators=(',',':'))})
        task_id=self.engine.session_direct_start({'quote':preview['quote']})['task']['id']
        wait_until(lambda:self.store.charge_tasks[task_id].get('remote_terminal') is True)
        task=self.store.charge_tasks[task_id]
        self.assertEqual(task['status'],'failed')
        self.assertTrue(task['can_retry'])
        self.assertEqual(provider.redeems,1)
        self.assertEqual(provider.lookups,2)
        self.assertFalse(task.get('submission_rejected',False))
        self.assertEqual(task['direct_card_secret'],'')

    def test_definite_direct_rejection_does_not_attribute_a_used_card(self):
        provider=RejectedUsedCardDirectProvider();self.provider(provider)
        value=session('direct@example.com')
        preview=self.engine.session_direct_prepare({'code':'SYNTHETIC-DIRECT-REJECTED-USED',
            'session':json.dumps(value,separators=(',',':'))})
        task_id=self.engine.session_direct_start({'quote':preview['quote']})['task']['id']
        wait_until(lambda:self.store.charge_tasks[task_id].get('remote_terminal') is True)
        task=self.store.charge_tasks[task_id]
        self.assertEqual(task['status'],'blocked')
        self.assertEqual(task['result_state'],'blocked')
        self.assertEqual(task['result_email'],'未确认')
        self.assertEqual(task['provider_result_email'],'other@example.com')
        self.assertNotIn('direct@example.com',task['result_email'])
        self.assertEqual(provider.redeems,1)

    def test_direct_rate_limit_waits_before_checking_original_card(self):
        provider=RateLimitedDirectProvider();self.provider(provider)
        value=session('direct@example.com')
        preview=self.engine.session_direct_prepare({'code':'SYNTHETIC-DIRECT-RATE-LIMIT',
            'session':json.dumps(value,separators=(',',':'))})
        task_id=self.engine.session_direct_start({'quote':preview['quote']})['task']['id']
        wait_until(lambda:task_id in self.engine.direct_jobs and self.engine.direct_jobs[task_id].done())
        task=self.store.charge_tasks[task_id]
        self.assertEqual(task['status'],'pending')
        self.assertTrue(task.get('submission_rejected'))
        self.assertEqual(provider.redeems,1)
        self.assertEqual(provider.lookups,1)
        self.assertGreaterEqual(self.engine.direct_poll_after[task_id]-time.monotonic(),4.9)

    def test_legacy_direct_success_waiting_for_email_is_auto_resolved_on_store_open(self):
        task={'id':'synthetic-legacy-direct','sequence':1,'source':'session_extract',
              'account_id':None,'email':'direct@example.com','card_id':None,'card':'SYNTHETIC-USED',
              'card_digest':'synthetic-digest','sku':'plus_half','status':'blocked','submitted':True,
              'can_retry':False,'created':'2026-11-09T00:00:00+08:00','updated':'2026-11-09T00:00:00+08:00',
              'message':'上游确认卡密已使用，但未能确认与 Session 邮箱一致；未计为该邮箱充值成功',
              'history':[],'result_state':'blocked','provider_result_email':'上游未提供',
              'remote_terminal':True}
        self.store.inventory_direct_task_create(task)
        self.engine.close()
        self.store.db.close()
        self.store=Store(self.path)
        self.engine=Engine(self.store)
        saved=self.store.charge_tasks[task['id']]
        self.assertEqual(saved['status'],'completed')
        self.assertEqual(saved['result_email'],'direct@example.com')
        self.assertEqual(saved['provider_result_email'],'上游未提供')
        self.assertEqual(saved['partner_redemption_token'],'')
        self.assertEqual(saved['direct_card_secret'],'')

    def test_unused_without_can_resubmit_is_not_released(self):
        self.store.inventory_import_cards('plus',['SYNTHETIC-CARD-NO-RETRY'])
        key=next(iter(self.store.cards))
        self.store.inventory_card_result(key,{'status':'unused','used':False,'can_resubmit':False})
        self.assertEqual(self.store.cards[key]['status'],'exception')


if __name__=='__main__':unittest.main()
