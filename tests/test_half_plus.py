"""Synthetic half-price Plus flows, stock isolation, and specification edits."""
import asyncio
import copy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from workbench.engine import Engine
from workbench.provider import plus_qualification,blocking_reason
from workbench.store import Store,crypt
from test_provider_contract import transport
from test_complete_flow import session,wait_until


class HalfPlusTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.path=Path(self.temp.name)/'records.sqlite3'
        self.store=Store(self.path)
        self.engine=Engine(self.store)

    def tearDown(self):
        self.engine.close()
        self.store.db.close()
        self.temp.cleanup()

    def account(self,email='owner@example.com'):
        key=self.store.inventory_import_accounts([{'email':email}])['ids'][0]
        self.engine.sessions[key]=session(email)
        return key

    def card(self,sku,code=None):
        code=code or 'SYNTHETIC-'+sku.upper()
        self.store.inventory_import_cards(sku,[code])
        return next(card['id'] for card in self.store.cards.values() if crypt(card['secret'],True)==code)

    def install(self,key,plan):
        redeemed=[False]
        email=self.store.get(key)['email']
        def lookup(body):
            return {'cdk_code':body['code'],'status':'used' if redeemed[0] else 'unused',
                'used':redeemed[0],'can_resubmit':not redeemed[0],'account_email':email}
        def preflight(body):
            self.assertEqual(body['redemption_token'],'SYNTHETIC-QUALIFICATION')
            self.assertEqual(json.loads(body['credential']['session'])['user']['email'],email)
            return {'email':email,'currentPlan':'free','subscription_has_active':False,
                    'preflight_token':'SYNTHETIC-CONFIRM'}
        def redeem(body):
            self.assertEqual(body['preflight_token'],'SYNTHETIC-CONFIRM')
            redeemed[0]=True
            return {'status':'queued'}
        provider=transport({'lookup':lookup,'preview':{'product':'gpt','plan':plan,
            'redemption_token':'SYNTHETIC-QUALIFICATION'},'preflight':preflight,'redeem':redeem,
            'result':{'status':'completed','account_email':email},
            'result-by-code':{'status':'completed','account_email':email}})
        if not hasattr(self,'providers'):
            self.providers={}
            async def get_provider(account_id):return self.providers[account_id]
            self.engine.provider=get_provider
        self.providers[key]=provider
        return provider

    def begin(self,key,sku):
        quote=self.engine.inventory_command('quote',{'ids':[key],'sku':sku})
        return self.engine.inventory_command('start',{'quote':quote['quote']})['tasks'][0]

    def edit(self,key,sku,expected_sku='plus'):
        return self.engine.inventory_command('edit-card-spec',{'id':key,'sku':sku,'expected_sku':expected_sku})

    def test_explicit_half_qualification_and_bidirectional_mismatch(self):
        self.assertEqual(plus_qualification({'product':' GPT ','plan':' PLUS_HALF ','redemption_token':'TEST-TOKEN'},'plus_half'),('plus_half','TEST-TOKEN'))
        for expected,actual in (('plus','plus_half'),('plus_half','plus')):
            with self.subTest(expected=expected),self.assertRaisesRegex(ValueError,'卡密类型不符'):
                plus_qualification({'product':'gpt','plan':actual,'redemption_token':'TEST-TOKEN'},expected)
        for plan in ('halfplus','plus_half_fake','half','not-plus','pro'):
            with self.subTest(plan=plan),self.assertRaises(ValueError):
                plus_qualification({'product':'gpt','plan':plan,'redemption_token':'TEST-TOKEN'},'plus_half')
        self.assertEqual(plus_qualification({'product':'gpt','plan':'plus_half','plan_type':'ignored-legacy-field',
                                             'redemption_token':'TEST-TOKEN'},'plus_half'),
                         ('plus_half','TEST-TOKEN'))

    def test_half_obeys_existing_subscription_and_purchase_blocks(self):
        for account in ({'current_plan':'plus'},{'current_plan':'pro'},{'can_purchase':False},
                        {'subscription_recovery_required':True}):
            self.assertTrue(blocking_reason(account,'plus_half',''))
        self.assertFalse(blocking_reason({'current_plan':'free','subscription_has_active':False},'plus_half',''))

    def test_half_success_uses_only_half_stock_and_preserves_product_in_history(self):
        key=self.account()
        regular=self.card('plus')
        half=self.card('plus_half')
        provider=self.install(key,'plus_half')
        task_id=self.begin(key,'plus_half')
        wait_until(lambda:self.engine.jobs[key].done())
        task=self.store.charge_tasks[task_id]
        self.assertEqual((task['status'],task['sku'],task['card_id']),('completed','plus_half',half))
        self.assertEqual(task['precheck']['target_plan'],'半价 Plus')
        self.assertEqual(task['precheck']['card_product'],'plus_half')
        self.assertEqual(self.store.get(key)['last_successful_recharge']['sku'],'plus_half')
        self.assertEqual(self.store.cards[regular]['status'],'unused')
        self.assertEqual(self.store.cards[half]['status'],'used')
        self.assertEqual(sum(call['action']=='redeem' for call in provider.synthetic_transport.calls),1)

    def test_both_inventory_mismatches_stop_before_account_credentials_and_release_card(self):
        for index,(selected,actual) in enumerate((('plus','plus_half'),('plus_half','plus'))):
            key=self.account(f'mismatch{index}@example.com')
            card=self.card(selected)
            provider=self.install(key,actual)
            task_id=self.begin(key,selected)
            wait_until(lambda:self.engine.jobs[key].done())
            task=self.store.charge_tasks[task_id]
            self.assertEqual(task['status'],'error')
            self.assertFalse(task['submitted'])
            self.assertIn('卡密类型不符',task['message'])
            self.assertEqual([call['action'] for call in provider.synthetic_transport.calls],['lookup','preview'])
            self.assertEqual(self.store.cards[card]['status'],'unused')
            self.assertEqual(task['sku'],selected)
            self.assertIn(key,self.engine.sessions)

    def test_failed_regular_classification_can_be_corrected_without_rewriting_history(self):
        key=self.account()
        card=self.card('plus')
        self.install(key,'plus_half')
        failed=self.begin(key,'plus')
        wait_until(lambda:self.engine.jobs[key].done())
        previous=copy.deepcopy(self.store.charge_tasks[failed])
        secret=self.store.cards[card]['secret']
        self.edit(card,'plus_half')
        self.assertEqual(self.store.charge_tasks[failed],previous)
        self.assertEqual(self.store.cards[card]['secret'],secret)
        self.assertEqual(self.store.get(key)['last_recharge']['sku'],'plus')
        task_id=self.begin(key,'plus_half')
        wait_until(lambda:self.engine.jobs[key].done())
        self.assertEqual(self.store.charge_tasks[task_id]['status'],'completed')
        self.assertEqual(self.store.charge_tasks[failed],previous)
        self.assertEqual(self.store.cards[card]['last_spec_change']['from'],'plus')

    def test_parallel_products_never_cross_allocate(self):
        keys=[self.account('regular@example.com'),self.account('half@example.com')]
        cards=[self.card('plus'),self.card('plus_half')]
        self.engine.command('limit',[],2)
        self.engine.command('pause',[])
        task_ids=[]
        for key,sku in zip(keys,('plus','plus_half')):
            self.install(key,sku)
            task_ids.append(self.begin(key,sku))
        self.engine.command('resume',[])
        wait_until(lambda:all(self.engine.jobs[key].done() for key in keys))
        for task_id,card,sku in zip(task_ids,cards,('plus','plus_half')):
            task=self.store.charge_tasks[task_id]
            self.assertEqual((task['status'],task['card_id'],task['sku']),('completed',card,sku))

    def test_no_substitution_when_one_product_has_no_stock(self):
        key=self.account()
        self.card('plus')
        with self.assertRaisesRegex(ValueError,'卡密不足'):
            self.engine.inventory_command('quote',{'ids':[key],'sku':'plus_half'})
        self.assertEqual(self.store.charge_tasks,{})

    def test_spec_edit_invalidates_old_quote_and_rejects_stale_editor(self):
        key=self.account()
        card=self.card('plus')
        quote=self.engine.inventory_command('quote',{'ids':[key],'sku':'plus'})
        self.edit(card,'plus_half')
        with self.assertRaisesRegex(ValueError,'状态发生变化'):
            self.engine.inventory_command('start',{'quote':quote['quote']})
        with self.assertRaisesRegex(ValueError,'规格已变化'):
            self.edit(card,'pro100','plus')
        self.assertEqual(self.store.cards[card]['sku'],'plus_half')
        self.assertEqual(self.store.charge_tasks,{})

    def test_edit_blocks_used_archived_reserved_and_unresolved_even_if_status_is_stale(self):
        key=self.account()
        card=self.card('plus')
        for status in ('used','reserved','exception'):
            self.store.cards[card]['status']=status
            with self.subTest(status=status),self.assertRaises(ValueError):self.edit(card,'plus_half')
        self.store.cards[card].update(status='unused',archived=True)
        with self.assertRaises(ValueError):self.edit(card,'plus_half')
        self.store.cards[card].pop('archived')
        task_id=self.store.inventory_reserve(self.store.inventory_quote([key],'plus'))[0]
        self.store.cards[card]['status']='unused'
        for status,submitted in (('queued',False),('error',True),('completed',True)):
            self.store.charge_tasks[task_id].update(status=status,submitted=submitted)
            with self.subTest(status=status),self.assertRaises(ValueError):self.edit(card,'plus_half')
        self.assertEqual(self.store.cards[card]['sku'],'plus')

    def test_edit_blocks_ongoing_stock_check_and_snapshot_explains_it(self):
        card=self.card('plus')
        async def checking():
            self.engine.inventory_checks={card:asyncio.create_task(asyncio.sleep(600))}
        self.engine._inventory_call(checking())
        with self.assertRaisesRegex(ValueError,'正在核对'):self.edit(card,'plus_half')
        row=self.engine.inventory_snapshot()['cards'][0]
        self.assertFalse(row['spec_editable'])
        self.assertIn('正在核对',row['spec_edit_block_reason'])

    def test_manual_edit_persists_and_deduplication_remains_global(self):
        card=self.card('plus')
        self.edit(card,'plus_half')
        with self.assertRaises(ValueError):self.store.inventory_import_cards('plus',['SYNTHETIC-PLUS'])
        self.assertEqual(self.store.inventory_import_cards('plus_half',['SYNTHETIC-PLUS'])['skipped'],1)
        self.engine.close();self.store.db.close()
        self.store=Store(self.path);self.engine=Engine(self.store)
        self.assertEqual(self.store.cards[card]['sku'],'plus_half')
        self.assertEqual(self.store.cards[card]['last_spec_change']['to'],'plus_half')
        self.assertNotIn(b'SYNTHETIC-PLUS',self.path.read_bytes())


if __name__=='__main__':unittest.main(verbosity=2)
