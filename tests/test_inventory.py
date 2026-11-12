import asyncio
import json
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from workbench.store import Store,crypt,now
from workbench.engine import Engine
from test_complete_flow import ProviderFixture,session,wait_until


class StockProvider(ProviderFixture):
    def __init__(self,email='owner@example.com',blocked=False,uncertain=False):
        super().__init__()
        self.email=email
        self.blocked=blocked
        self.uncertain=uncertain

    async def lookup(self,code):
        return {'cdk_code':code,'status':'used' if self.redeems and not self.uncertain else 'unused',
                'used':bool(self.redeems and not self.uncertain),'account_email':self.email,
                'product':'gpt','plan':'plus',
                'used_at':'2026-11-12T10:20:30+08:00' if self.redeems and not self.uncertain else ''}

    async def result(self,token):
        if self.uncertain:
            return {'status':'processing'}
        return await super().result(token)

    async def call(self,kind,body):
        if kind=='preflight':
            return {'email':self.email,'current_plan':'plus' if self.blocked else 'free',
                    'subscription_has_active':self.blocked,'preflight_token':'TEST-PREFLIGHT'}
        return await super().call(kind,body)


class InventoryTest(unittest.TestCase):
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
        return self.store.inventory_import_accounts([{'email':email,'password':'TEST-PASSWORD'}])['ids'][0]

    def cards(self,sku='plus',codes=None):
        self.store.inventory_import_cards(sku,codes or ['STOCK-PLUS-TEST'])

    def provider(self,provider):
        async def get_provider(_):return provider
        self.engine.provider=get_provider

    def begin(self,key):
        quote=self.engine.inventory_command('quote',{'ids':[key],'sku':'plus'})
        return self.engine.inventory_command('start',{'quote':quote['quote']})['tasks'][0]

    def test_account_copy_is_explicit_read_only_and_independent_of_recharge_or_session(self):
        key=self.store.inventory_import_accounts([{'email':'copy@example.com','password':'COPY-PASS-特殊%--',
            'totp':'COPY-TOTP-TEST','mail_url':'https://example.com/code?key=PRIVATE-MAIL'}])['ids'][0]
        self.cards()
        for completed in (False,True):
            if completed:self.store.update(key,last_successful_recharge={'status':'completed','sku':'plus'})
            before=self.path.read_bytes()
            snapshot=json.dumps(self.engine.inventory_snapshot(),ensure_ascii=False)
            for secret in ('COPY-PASS-特殊%--','COPY-TOTP-TEST','PRIVATE-MAIL'):
                self.assertNotIn(secret,snapshot)
            result=self.engine.inventory_command('copy-account',{'id':key})
            self.assertEqual(result,{'text':'邮箱账号：copy@example.com\nGPT密码：COPY-PASS-特殊%--\n2FA密钥：COPY-TOTP-TEST\n在线获取2FA网站：https://2fa.vip/index.html'})
            self.assertNotIn('PRIVATE-MAIL',result['text'])
            self.assertEqual(before,self.path.read_bytes())
            self.assertEqual(self.engine.sessions,{})
            self.assertEqual(self.store.charge_tasks,{})
            self.assertTrue(all(card['status']=='unused' for card in self.store.cards.values()))

    def test_account_copy_missing_fields_and_invalid_account(self):
        key=self.store.inventory_import_accounts([{'email':'email-only@example.com'}])['ids'][0]
        text=self.engine.inventory_command('copy-account',{'id':key})['text']
        self.assertIn('GPT密码：未配置\n2FA密钥：未配置',text)
        for key in (None,[],{},'missing'):
            with self.subTest(key=key),self.assertRaises(ValueError):
                self.engine.inventory_command('copy-account',{'id':key})

    def test_account_deduplication_and_validation_precede_any_mutation(self):
        result=self.store.inventory_import_accounts([{'email':'owner@example.com'}, {'email':'OWNER@example.com'}])
        self.assertEqual((result['imported'],result['skipped']),(1,1))
        self.assertEqual(self.store.cards,{})
        with self.assertRaises(ValueError):
            self.store.inventory_import_accounts([{'email':'other@example.com'},{'email':'bad'}])
        self.assertEqual(len(self.store.rows),1)

    def test_five_independent_stock_categories_and_cross_category_duplicates(self):
        for sku in ('plus','plus_half','pro100','pro200','pro500'):
            self.cards(sku,['CARD-'+sku])
        self.assertEqual(len(self.store.cards),5)
        with self.assertRaises(ValueError):
            self.cards('pro100',['NEW-CARD','CARD-plus'])
        self.assertEqual(len(self.store.cards),5)
        self.assertEqual(self.store.inventory_import_cards('plus',['CARD-plus'])['skipped'],1)
        self.assertEqual(self.store.rows,{})

    def test_quote_is_read_only_and_exact_plus_assignment_is_atomic(self):
        key=self.account()
        self.cards('pro100',['PRO-ONLY-CARD'])
        self.cards('plus',['PLUS-ONLY-CARD'])
        assignment=self.store.inventory_quote([key],'plus')
        self.assertEqual(self.store.charge_tasks,{})
        self.assertTrue(all(card['status']=='unused' for card in self.store.cards.values()))
        tasks=self.store.inventory_reserve(assignment)
        self.assertEqual(len(tasks),1)
        self.assertEqual(self.store.cards[assignment[0]['card_id']]['status'],'reserved')
        self.assertEqual(next(card for card in self.store.cards.values() if card['sku']=='pro100')['status'],'unused')

    def test_stale_quote_cannot_reallocate_or_double_reserve(self):
        first=self.account()
        second=self.account('second@example.com')
        self.cards()
        a=self.engine.inventory_command('quote',{'ids':[first],'sku':'plus'})
        b=self.engine.inventory_command('quote',{'ids':[second],'sku':'plus'})
        self.engine.command('pause',[])
        self.engine.inventory_command('start',{'quote':a['quote']})
        with self.assertRaises(ValueError):
            self.engine.inventory_command('start',{'quote':b['quote']})
        with self.assertRaises(ValueError):
            self.engine.inventory_command('start',{'quote':a['quote']})
        self.assertEqual(len(self.store.charge_tasks),1)
        self.assertTrue(all(card['status']=='reserved' for card in self.store.cards.values()))

    def test_stock_shortage_and_unconfirmed_pro_do_not_reserve_anything(self):
        keys=[self.account(),self.account('other@example.com')]
        self.cards()
        with self.assertRaises(ValueError):self.store.inventory_quote(keys,'plus')
        self.cards('pro500',['PRO500-TEST-CARD'])
        with self.assertRaises(ValueError):self.store.inventory_quote([keys[0]],'pro500')
        self.assertEqual(self.store.charge_tasks,{})

    def test_success_updates_stock_and_account_and_session_does_not_rewrite_history(self):
        key=self.account()
        self.cards()
        provider=StockProvider()
        self.provider(provider)
        self.engine.sessions[key]=session()
        task_id=self.begin(key)
        wait_until(lambda:self.engine.jobs[key].done())
        self.assertEqual(provider.redeems,1)
        self.assertEqual(self.store.charge_tasks[task_id]['status'],'completed')
        self.assertNotIn(key,self.engine.sessions)
        self.assertNotIn(key,self.store.load_session_credentials())
        self.assertEqual(self.store.get(key)['last_recharge']['sku'],'plus')
        self.assertEqual(self.store.get(key)['current_plan'],'plus')
        self.assertEqual(next(iter(self.store.cards.values()))['status'],'used')
        self.assertEqual(self.store.inventory_public()['cards'][0]['status'],'used')
        self.engine.sessions[key]=session()
        self.engine.inventory_command('session',{'ids':[key]})
        wait_until(lambda:self.engine.jobs[key].done())
        self.assertEqual(self.store.charge_tasks[task_id]['status'],'completed')
        self.assertEqual(provider.redeems,1)
        self.engine.inventory_command('edit-account',{'id':key,'password':'UPDATED-PASSWORD','eligibility_months':'1'})
        self.assertEqual(self.store.get(key)['eligibility_months'],1)
        self.assertEqual(self.store.charge_tasks[task_id]['status'],'completed')

    def test_mismatched_success_is_automatically_attributed_to_preflight_account(self):
        key=self.account()
        self.cards()
        task_id=self.store.inventory_reserve(self.store.inventory_quote([key],'plus'))[0]
        self.store.update(key,status='pending',submitted=True,message='合成提交')
        self.engine.remember_session(key,session())
        self.engine._inventory_call(self.engine.apply_lookup(key,{
            'status':'used','used':True,'account_email':'other@example.com'}))
        task=self.store.charge_tasks[task_id]
        card=self.store.inventory_public()['cards'][0]
        self.assertEqual(task['status'],'completed')
        self.assertFalse(task.get('attribution_pending',False))
        self.assertEqual(task['result_email'],'owner@example.com')
        self.assertEqual(task['provider_result_email'],'other@example.com')
        self.assertEqual(card['status'],'used')
        self.assertTrue(card['deletable'])
        account=self.store.get(key)
        self.assertEqual(task['status'],'completed')
        self.assertEqual(account['current_plan'],'plus')
        self.assertTrue(self.store.inventory_public()['accounts'][0]['has_recharged'])
        self.assertEqual(card['status'],'used')
        self.assertTrue(card['deletable'])
        self.assertNotIn(key,self.engine.sessions)
        self.assertNotIn(key,self.store.load_session_credentials())
        with self.assertRaises(ValueError):
            self.engine.inventory_command('task',{'id':task_id,'action':'confirm-success'})

    def test_used_card_without_local_submission_is_not_attributed_to_current_account(self):
        key=self.account()
        self.cards()
        task_id=self.store.inventory_reserve(self.store.inventory_quote([key],'plus'))[0]
        self.store.update(key,status='pending',submitted=False,message='仅本地预留，未提交')
        self.engine._inventory_call(self.engine.apply_lookup(key,{
            'status':'used','used':True,'account_email':'other@example.com'}))
        account=self.store.get(key)
        task=self.store.charge_tasks[task_id]
        cards=self.store.inventory_public()['cards']
        self.assertEqual(task['status'],'blocked')
        self.assertFalse(task.get('attribution_pending',False))
        self.assertFalse(account.get('last_successful_recharge'))
        self.assertFalse(self.store.inventory_public()['accounts'][0]['has_recharged'])
        self.assertEqual(next(card for card in cards if card['task_id']==task_id)['status'],'used')

    def test_legacy_attribution_pending_task_is_auto_resolved_on_store_open(self):
        key=self.account()
        self.cards()
        task_id=self.store.inventory_reserve(self.store.inventory_quote([key],'plus'))[0]
        self.store.update(key,status='blocked',submitted=True,result_state='completed',
                          attribution_pending=True,message='卡密已用于其他邮箱，请人工核对')
        self.engine.close()
        self.store.db.close()
        self.store=Store(self.path)
        self.engine=Engine(self.store)
        task=self.store.charge_tasks[task_id]
        card=self.store.inventory_public()['cards'][0]
        self.assertEqual(task['status'],'completed')
        self.assertFalse(task.get('attribution_pending',False))
        self.assertEqual(task['result_email'],'owner@example.com')
        self.assertEqual(card['status'],'used')
        self.assertTrue(card['deletable'])
        self.assertEqual(card['status'],'used')

    def test_deleted_account_legacy_success_is_auto_resolved_in_history(self):
        key=self.account()
        self.cards()
        task_id=self.store.inventory_reserve(self.store.inventory_quote([key],'plus'))[0]
        self.store.update(key,status='blocked',submitted=True,result_state='completed',
                          attribution_pending=True,message='邮箱不一致，待核实')
        self.store.inventory_delete('accounts',[key])
        self.engine.close()
        self.store.db.close()
        self.store=Store(self.path)
        self.engine=Engine(self.store)
        task=self.store.charge_tasks[task_id]
        self.assertEqual(task['status'],'completed')
        self.assertEqual(task['result_email'],'owner@example.com')
        self.assertNotIn(key,self.store.rows)
        self.assertEqual(next(iter(self.store.cards.values()))['status'],'used')

    def test_blocked_preflight_releases_confirmed_unused_card_and_preserves_reason(self):
        key=self.account()
        self.cards()
        provider=StockProvider(blocked=True)
        self.provider(provider)
        self.engine.sessions[key]=session()
        task_id=self.begin(key)
        wait_until(lambda:self.engine.jobs[key].done())
        self.assertEqual(provider.redeems,0)
        task=self.store.charge_tasks[task_id]
        self.assertEqual(task['status'],'error')
        self.assertIn('已有',task['message'])
        self.assertEqual(task['precheck']['current_plan'],'plus')
        self.assertEqual(next(iter(self.store.cards.values()))['status'],'unused')

    def test_uncertain_submission_remains_reserved_and_only_queries(self):
        key=self.account()
        self.cards()
        provider=StockProvider(uncertain=True)
        self.provider(provider)
        self.engine.sessions[key]=session()
        task_id=self.begin(key)
        wait_until(lambda:provider.redeems==1 and self.store.get(key)['submitted'])
        wait_until(lambda:self.engine.jobs[key].done())
        self.assertEqual(self.store.charge_tasks[task_id]['status'],'pending')
        self.assertEqual(next(iter(self.store.cards.values()))['status'],'reserved')
        with self.assertRaises(ValueError):self.engine.inventory_command('quote',{'ids':[key],'sku':'plus'})
        self.assertEqual(provider.redeems,1)

    def test_session_only_has_no_card_and_does_not_contact_provider(self):
        key=self.account()
        self.engine.sessions[key]=session()
        async def forbidden(_):raise AssertionError('Provider must not be contacted')
        self.engine.provider=forbidden
        self.engine.inventory_command('session',{'ids':[key]})
        wait_until(lambda:self.engine.jobs[key].done())
        self.assertTrue(self.engine.inventory_snapshot()['accounts'][0]['session_valid'])
        self.assertEqual(self.store.charge_tasks,{})
        public=json.dumps(self.engine.inventory_snapshot())
        for secret in ('TEST-PASSWORD','TEST-ACCESS-ONLY','TEST-COOKIE-ONLY'):
            self.assertNotIn(secret,public)
            self.assertNotIn(secret.encode(),self.path.read_bytes())

    def test_restart_releases_only_unsubmitted_reservations(self):
        key=self.account()
        self.cards()
        for intermediate in ('queued','ready'):
            assignment=self.store.inventory_quote([key],'plus')
            task=self.store.inventory_reserve(assignment)[0]
            self.store.update(key,status=intermediate)
            self.store.db.close()
            self.store=Store(self.path)
            self.engine.store=self.store
            self.assertEqual(self.store.charge_tasks[task]['status'],'stopped')
            self.assertEqual(next(iter(self.store.cards.values()))['status'],'unused')
        task=self.store.inventory_reserve(self.store.inventory_quote([key],'plus'))[0]
        self.store.update(key,status='pending',submitted=True,message='TEST SUBMITTED')
        self.store.db.close()
        self.store=Store(self.path)
        self.engine.store=self.store
        self.assertEqual(self.store.charge_tasks[task]['status'],'pending')
        self.assertEqual(next(iter(self.store.cards.values()))['status'],'reserved')

    def test_independent_card_lookup_does_not_create_accounts_or_tasks(self):
        self.cards()
        provider=StockProvider()
        provider.redeems=1
        self.provider(provider)
        key=next(iter(self.store.cards))
        self.engine.inventory_command('check-cards',{'ids':[key]})
        wait_until(lambda:self.engine.inventory_checks[key].done())
        self.assertEqual(self.store.cards[key]['status'],'used')
        self.assertEqual(self.store.rows,{})
        self.assertEqual(self.store.charge_tasks,{})

    def test_card_lookup_preserves_safe_upstream_facts_for_explicit_detail(self):
        self.cards()
        provider=StockProvider()
        provider.redeems=1
        self.provider(provider)
        key=next(iter(self.store.cards))
        self.engine.inventory_command('check-cards',{'ids':[key]})
        wait_until(lambda:self.engine.inventory_checks[key].done())
        detail=self.engine.inventory_command('card-detail',{'id':key})
        self.assertEqual(detail['provider_account_email'],'ow••••er@example.com')
        self.assertEqual(detail['provider_product'],'gpt')
        self.assertEqual(detail['provider_plan'],'plus')
        self.assertEqual(detail['provider_used_at'],'2026-11-12T10:20:30+08:00')
        public=json.dumps(self.engine.inventory_snapshot(),ensure_ascii=False)
        self.assertNotIn('owner@example.com',public)
        self.assertNotIn(b'owner@example.com',self.path.read_bytes())

    def test_deleting_account_keeps_recharge_identity(self):
        key=self.account()
        self.cards()
        self.provider(StockProvider())
        self.engine.sessions[key]=session()
        task_id=self.begin(key)
        wait_until(lambda:self.engine.jobs[key].done())
        self.engine.inventory_command('delete',{'kind':'accounts','ids':[key]})
        self.assertNotIn(key,self.store.rows)
        self.assertEqual(self.store.charge_tasks[task_id]['email'],'owner@example.com')
        self.assertEqual(next(iter(self.store.cards.values()))['status'],'used')

    def test_account_numbers_resequence_after_delete_and_persist(self):
        ids=self.store.inventory_import_accounts([
            {'email':f'resequence{index}@example.com'} for index in range(3)])['ids']
        self.store.inventory_delete('accounts',[ids[1]])
        self.assertEqual([row['sequence'] for row in self.store.inventory_public()['accounts']],[1,2])

        self.store.db.close()
        self.store=Store(self.path)
        self.engine.store=self.store
        self.assertEqual([row['sequence'] for row in self.store.inventory_public()['accounts']],[1,2])
        added=self.account('resequence-new@example.com')
        self.assertEqual(self.store.get(added)['sequence'],3)

    def test_visible_account_numbers_restart_at_one_for_each_pool(self):
        normal=self.store.inventory_import_accounts([
            {'email':'visible-normal@example.com'},
            {'email':'visible-risk@example.com'}])['ids']
        self.store.quarantine_account(normal[1],'manual')
        public=self.store.inventory_public()
        self.assertEqual([row['display_sequence'] for row in public['accounts'] if not row['risk'].get('active')],[1])
        self.assertEqual([row['display_sequence'] for row in public['accounts'] if row['risk'].get('active')],[1])

    def test_visible_card_numbers_restart_at_one_for_each_sku(self):
        self.store.inventory_import_cards('plus',['VISIBLE-PLUS-1','VISIBLE-PLUS-2'])
        self.store.inventory_import_cards('plus_half',['VISIBLE-HALF-1'])
        public=self.store.inventory_public()
        self.assertEqual([row['display_sequence'] for row in public['cards'] if row['sku']=='plus'],[1,2])
        self.assertEqual([row['display_sequence'] for row in public['cards'] if row['sku']=='plus_half'],[1])

    def test_visible_card_numbers_resequence_and_ignore_archived_cards(self):
        self.store.inventory_import_cards('plus',[f'RESEQUENCE-CARD-{index}' for index in range(4)])
        cards=sorted(self.store.cards.values(),key=lambda card:card['sequence'])
        used=dict(cards[1],status='used')
        with self.store.db:
            self.store._inventory_write('cards',used)
        self.store.cards[used['id']]=used

        self.store.inventory_delete('cards',[cards[1]['id'],cards[2]['id']])
        visible=self.store.inventory_public()['cards']
        self.assertEqual([card['sequence'] for card in visible],[1,2])
        self.assertTrue(self.store.cards[cards[1]['id']]['archived'])
        self.assertEqual(self.store.cards[cards[1]['id']]['sequence'],2)

        self.store.db.close()
        self.store=Store(self.path)
        self.engine.store=self.store
        self.assertEqual([card['sequence'] for card in self.store.inventory_public()['cards']],[1,2])
        self.store.inventory_import_cards('plus',['RESEQUENCE-CARD-NEW'])
        self.assertEqual(max(card['sequence'] for card in self.store.inventory_public()['cards']),3)

    def test_used_card_cannot_return_to_stock_after_inconsistent_lookup(self):
        self.cards()
        key=next(iter(self.store.cards))
        self.store.inventory_card_result(key,{'status':'used','used':True})
        self.store.inventory_card_result(key,{'status':'unused','used':False})
        self.assertEqual(self.store.cards[key]['status'],'used')
        self.assertTrue(self.store.cards[key]['check_error'])

    def test_submitted_terminal_error_keeps_account_locked_until_resolved(self):
        key=self.account()
        self.cards(codes=['CARD-FIRST','CARD-SECOND'])
        self.engine.command('pause',[])
        self.begin(key)
        self.store.update(key,submitted=True,status='blocked',message='Submitted result requires review')
        with self.assertRaises(ValueError):self.store.inventory_quote([key],'plus')
        with self.assertRaises(ValueError):self.store.inventory_session_slot(key)
        with self.assertRaises(ValueError):self.store.inventory_delete('accounts',[key])


class LegacyMigrationTest(unittest.TestCase):
    def test_old_bound_rows_become_deduplicated_accounts_stock_and_history(self):
        with TemporaryDirectory() as temporary:
            path=Path(temporary)/'legacy.sqlite3'
            rows=[]
            for index,status in enumerate(('completed','pending')):
                rows.append(dict(id=str(index)*32,email='owner@example.com',secret=crypt('LEGACY-CARD-'+str(index)),
                    password=crypt('PASSWORD'),totp=crypt(''),mail_url=crypt(''),card='LEGACY',
                    created=now(),updated=now(),status=status,submitted=True,can_retry=False,
                    message='legacy',history=[{'time':now(),'status':status,'message':'legacy history'}],plan='Plus',current_plan='free'))
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE records(id TEXT PRIMARY KEY,document TEXT NOT NULL)')
                db.executemany('INSERT INTO records VALUES (?,?)',[(row['id'],json.dumps(row)) for row in rows])
            db.close()
            store=Store(path)
            try:
                self.assertEqual(len(store.rows),1)
                self.assertEqual(len(store.cards),2)
                self.assertEqual(len(store.charge_tasks),2)
                self.assertEqual({card['status'] for card in store.cards.values()},{'used','reserved'})
                self.assertEqual(next(iter(store.rows.values()))['last_recharge']['status'],'completed')
                self.assertNotIn('PASSWORD',json.dumps(store.inventory_public()))
            finally:store.db.close()
            store=Store(path)
            self.assertEqual(len(store.cards),2)
            self.assertEqual(len(store.charge_tasks),2)
            store.db.close()


if __name__=='__main__':unittest.main(verbosity=2)
