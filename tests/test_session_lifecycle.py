"""All credentials, cards and upstream responses in this module are synthetic."""
import asyncio
import base64
import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from workbench.store import Store
from workbench.engine import Engine
from workbench.provider import session_rejected
from workbench.session_lifecycle import local_facts,token_expiry,iso,SessionNeedsAttention,SessionRejectedError
from workbench.edge_session import EdgePageLostError
from test_complete_flow import session,wait_until
from test_inventory import StockProvider


def jwt(exp):
    def part(value):return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return part({'alg':'RS256'})+'.'+part({'exp':exp})+'.SYNTHETIC_SIGNATURE'


class LifetimeRulesTest(unittest.TestCase):
    def test_token_deadline_shortens_session_deadline_and_is_not_server_validation(self):
        stamp=time.time()
        value={**session(),'expires':iso(stamp+3600),'accessToken':jwt(stamp+300)}
        facts=local_facts(value,'owner@example.com',stamp)
        self.assertEqual(facts['state'],'expiring')
        self.assertEqual(facts['effective_expires_at'],iso(stamp+300))
        self.assertEqual(local_facts(value,'owner@example.com',stamp+301)['state'],'expired')
        value['accessToken']=jwt(stamp+7200)
        self.assertEqual(local_facts(value,'owner@example.com',stamp)['effective_expires_at'],iso(stamp+3600))

    def test_opaque_malformed_and_unsigned_tokens_have_no_invented_expiry(self):
        for token in ('opaque','a.b.c','',jwt(True),jwt('123'),jwt(float('inf'))):
            with self.subTest(token=token):self.assertIsNone(token_expiry(token))
        value=session()
        facts=local_facts(value,'owner@example.com')
        self.assertEqual(facts['state'],'usable')
        self.assertIsNone(facts['access_token_expires_at'])
        self.assertFalse(facts['token_expiry_known'])
        for changed in ({'sessionToken':''},{'accessToken':''},{'expires':'2099-01-01T00:00:00'},{'expires':None}):
            self.assertEqual(local_facts({**value,**changed},'owner@example.com')['state'],'invalid')

    def test_only_explicit_target_session_rejection_invalidates(self):
        self.assertTrue(session_rejected({'error':{'code':'access_token_expired'}}))
        self.assertTrue(session_rejected({'data':{'session_valid':False}}))
        for payload in ({'status':401},{'status':403},{'code':'permission_denied'},{'error':'network failed'},{'valid':False}):
            self.assertFalse(session_rejected(payload))


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.path=Path(self.temp.name).resolve()/'records.sqlite3'
        self.store=Store(self.path)
        self.engine=Engine(self.store)
        self.clock=[time.time()]
        self.engine.session_clock=lambda:self.clock[0]
        self.calls=[]
        async def login(key,**kwargs):
            self.calls.append(key)
            self.engine.remember_session(key,session(self.store.get(key)['email']))
            self.store.update(key,status='ready')
        self.engine.edge_login=login

    def tearDown(self):
        self.engine.close()
        self.store.db.close()
        self.temp.cleanup()

    def account(self,name='owner',password='TEST-PASSWORD'):
        return self.store.inventory_import_accounts([{'email':name+'@example.com','password':password}])['ids'][0]

    def execute(self,coro):
        return asyncio.run_coroutine_threadsafe(coro,self.engine.loop).result(8)

    def scan(self,force=False):return self.execute(self.engine.refresh_eligible_sessions(force))

    def settle(self):wait_until(lambda:all(job.done() for job in self.engine.jobs.values()))

    def test_eligibility_uses_success_history_and_risk_not_free_plan_or_copy_action(self):
        free=self.account('free');plus=self.account('plus')
        self.store.update(plus,current_plan='plus')
        used=self.account('used');risk=self.account('risk');pending=self.account('pending')
        self.store.update(used,current_plan='free',last_successful_recharge={'status':'completed'})
        self.store.quarantine_account(risk,'manual')
        self.store.update(pending,submitted=True)
        self.engine.inventory_command('copy-account',{'id':free})
        result=self.scan()
        self.settle()
        self.assertEqual((result['queued'],result['excluded']),(2,3))
        self.assertEqual(set(self.calls),{free,plus})
        self.assertEqual(self.scan()['queued'],0)

    def test_missing_expired_expiring_and_explicitly_invalid_are_refreshed(self):
        keys=[self.account('state'+str(index)) for index in range(5)]
        for key,delta in zip(keys[1:4],(-1,599,1200)):
            self.engine.sessions[key]={**session(self.store.get(key)['email']),'accessToken':jwt(self.clock[0]+delta)}
        self.engine.invalidate_session(keys[4])
        result=self.scan();self.settle()
        self.assertEqual(result['queued'],4)
        self.assertNotIn(keys[3],self.calls)
        self.assertEqual(len(self.calls),4)

    def test_session_credential_survives_restart_only_when_unexpired(self):
        key=self.account()
        self.engine.remember_session(key,session())
        info=self.engine.session_info(key)
        self.assertEqual(info['state'],'usable')
        self.assertEqual(info['verification'],'not_checked')
        self.assertIsNotNone(info['acquired_at'])
        self.assertNotIn(b'TEST-ACCESS-ONLY',self.path.read_bytes())
        self.assertNotIn(b'TEST-COOKIE-ONLY',self.path.read_bytes())
        self.engine.close();self.store.db.close()
        self.store=Store(self.path);self.engine=Engine(self.store)
        info=self.engine.session_info(key)
        self.assertEqual(info['state'],'usable')
        self.assertTrue(info['present'])
        self.assertTrue(self.engine.inventory_snapshot()['accounts'][0]['session_valid'])
        self.assertIsNotNone(info['acquired_at'])

    def test_delete_account_removes_only_its_persisted_session(self):
        first=self.account('first')
        second=self.account('second')
        first_session=session('first@example.com')
        second_session=session('second@example.com')
        self.engine.remember_session(first,first_session)
        self.engine.remember_session(second,second_session)
        self.engine.session_checks[first]={'fingerprint':'first','time':'synthetic'}
        self.engine.session_checks[second]={'fingerprint':'second','time':'synthetic'}
        self.engine.tickets[first]={'token':'first-ticket'}
        self.engine.tickets[second]={'token':'second-ticket'}
        self.engine.inventory_command('delete',{'kind':'accounts','ids':[first]})
        self.assertNotIn(first,self.store.load_session_credentials())
        self.assertNotIn(first,self.engine.sessions)
        self.assertNotIn(first,self.engine.session_checks)
        self.assertNotIn(first,self.engine.tickets)
        self.assertEqual(self.store.load_session_credentials()[second],second_session)
        self.assertEqual(self.engine.sessions[second],second_session)
        self.assertEqual(self.engine.session_checks[second]['fingerprint'],'second')
        self.assertEqual(self.engine.tickets[second]['token'],'second-ticket')
        self.assertNotIn(first,self.store.rows)
        self.assertIn(second,self.store.rows)

    def test_completed_charge_commits_session_removal_with_recharge_history(self):
        key=self.account()
        other=self.account('other')
        self.store.inventory_import_cards('plus',['SYNTHETIC-COMPLETION-CARD'])
        self.engine.remember_session(key,session())
        self.engine.remember_session(other,session('other@example.com'))
        assignment=self.store.inventory_quote([key],'plus')
        self.store.inventory_reserve(assignment)
        self.store.update(key,status='pending',submitted=True,message='synthetic submitted')

        self.execute(self.engine.apply_lookup(key,{
            'status':'completed','used':True,'account_email':'owner@example.com'}))
        self.assertNotIn(key,self.engine.sessions)
        self.assertNotIn(key,self.store.load_session_credentials())
        self.assertTrue(self.store.get(key)['last_successful_recharge'])
        self.assertIn(other,self.engine.sessions)
        self.assertIn(other,self.store.load_session_credentials())

        # Simulate a stale credential left by an earlier build; startup must not restore it.
        self.store.save_session_credential(key,session())

        self.engine.close();self.store.db.close()
        self.store=Store(self.path);self.engine=Engine(self.store)
        self.assertNotIn(key,self.engine.sessions)
        self.assertNotIn(key,self.store.load_session_credentials())
        self.assertIn(other,self.engine.sessions)
        self.assertTrue(self.store.get(key)['last_successful_recharge'])

    def test_default_off_and_toggle_persistence_cancels_automatic_queue(self):
        key=self.account()
        self.assertFalse(self.engine.session_auto_enabled)
        self.engine.command('pause',[])
        result=self.engine.inventory_command('session-auto',{'enabled':True})
        self.assertEqual(result['queued'],1)
        self.assertEqual(self.store.db.execute("SELECT value FROM inventory_meta WHERE key='session-auto-refresh'").fetchone(),('true',))
        self.engine.inventory_command('session-auto',{'enabled':False})
        self.settle();self.engine.command('resume',[])
        self.assertEqual(self.calls,[])
        self.assertFalse(self.engine.session_auto_enabled)
        with self.assertRaises(ValueError):self.engine.inventory_command('session-auto',{'enabled':'true'})

    def test_paused_queue_rechecks_after_risk_move_or_success_before_opening(self):
        risk=self.account('risk-later');used=self.account('used-later')
        self.engine.command('pause',[])
        self.assertEqual(self.scan()['queued'],2)
        self.engine.inventory_command('risk-pool',{'ids':[risk],'active':True})
        self.store.update(used,last_successful_recharge={'status':'completed'})
        self.engine.command('resume',[]);self.settle()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.scan()['queued'],0)

    def test_concurrency_shared_with_session_jobs_and_duplicate_scans_do_not_duplicate(self):
        keys=[self.account('parallel'+str(index)) for index in range(6)]
        self.engine.command('session-limit',[],2)
        counts={'active':0,'peak':0}
        async def login(key,**kwargs):
            self.calls.append(key);counts['active']+=1
            counts['peak']=max(counts['peak'],counts['active'])
            await asyncio.sleep(.12)
            self.engine.remember_session(key,session(self.store.get(key)['email']))
            self.store.update(key,status='ready');counts['active']-=1
        self.engine.edge_login=login
        self.assertEqual(self.scan()['queued'],6)
        self.assertEqual(self.scan()['queued'],0)
        self.settle()
        self.assertEqual(counts['peak'],2)
        self.assertEqual(set(self.calls),set(keys))
        self.assertEqual(len(self.calls),6)

    def test_failures_backoff_three_attempts_then_require_explicit_manual_retry(self):
        key=self.account()
        async def failing(key,**kwargs):
            self.calls.append(key)
            raise RuntimeError('SECRET-REMOTE-ERROR-MUST-NOT-BE-SAVED')
        self.engine.edge_login=failing
        for delay in (61,301,10000):
            self.assertEqual(self.scan()['queued'],1);self.settle()
            self.assertEqual(self.scan()['queued'],0)
            self.clock[0]+=delay
        meta=self.store.get(key)['session_meta']
        self.assertEqual(meta['attempts'],3)
        self.assertTrue(meta['manual_required'])
        self.assertIsNone(meta['next_retry_at'])
        self.assertEqual(self.scan()['queued'],0)
        self.assertNotIn('SECRET-REMOTE',json.dumps(self.store.get(key)))
        async def login(key,**kwargs):
            self.engine.remember_session(key,session())
            self.store.update(key,status='ready')
        self.engine.edge_login=login
        self.assertEqual(self.engine.inventory_command('refresh-sessions',{})['queued'],1);self.settle()
        self.assertFalse(self.store.get(key)['session_meta']['manual_required'])

    def test_missing_credentials_and_human_attention_do_not_loop(self):
        missing=self.account('missing','');challenge=self.account('challenge')
        async def blocked(key,**kwargs):
            self.calls.append(key)
            raise SessionNeedsAttention('synthetic challenge')
        self.engine.edge_login=blocked
        result=self.scan();self.settle()
        self.assertEqual((result['queued'],result['manual']),(1,1))
        self.clock[0]+=10000
        self.assertEqual(self.scan()['queued'],0)
        self.assertEqual(self.calls,[challenge])
        self.assertTrue(self.store.get(missing)['session_meta']['manual_required'])

    def test_page_loss_requires_manual_refresh_without_reopening_after_backoff(self):
        key=self.account('page-lost')
        async def page_lost(key,**kwargs):
            self.calls.append(key)
            raise EdgePageLostError('synthetic page heartbeat timeout')
        self.engine.edge_login=page_lost
        self.assertEqual(self.scan()['queued'],1)
        self.settle()
        self.assertEqual(self.calls,[key])
        self.assertTrue(self.store.get(key)['session_meta']['manual_required'])
        self.clock[0]+=10000
        self.assertEqual(self.scan()['queued'],0)
        self.assertEqual(self.calls,[key])

    def test_active_refresh_is_cancelled_when_account_becomes_ineligible(self):
        key=self.account()
        async def waiting(key,**kwargs):
            self.calls.append(key)
            await asyncio.sleep(100)
        self.engine.edge_login=waiting
        self.scan();wait_until(lambda:bool(self.calls))
        self.store.quarantine_account(key,'manual')
        self.scan();self.settle()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.scan()['queued'],0)

    def test_active_refresh_stops_after_success(self):
        key=self.account()
        async def waiting(key,**kwargs):
            self.calls.append(key)
            await asyncio.sleep(100)
        self.engine.edge_login=waiting
        self.scan();wait_until(lambda:bool(self.calls))
        self.store.update(key,last_successful_recharge={'status':'completed'})
        self.scan();self.settle()
        self.assertEqual(self.scan()['queued'],0)
        self.assertEqual(len(self.calls),1)

    def test_disabling_active_maintenance_does_not_require_manual_repair_when_reenabled(self):
        key=self.account()
        async def waiting(key,**kwargs):
            self.calls.append(key)
            await asyncio.sleep(100)
        self.engine.edge_login=waiting
        self.engine.inventory_command('session-auto',{'enabled':True})
        wait_until(lambda:bool(self.calls))
        self.engine.inventory_command('session-auto',{'enabled':False});self.settle()
        self.assertFalse(self.store.get(key)['session_meta']['manual_required'])
        async def login(key,**kwargs):
            self.engine.remember_session(key,session())
            self.store.update(key,status='ready')
        self.engine.edge_login=login
        self.assertEqual(self.engine.inventory_command('session-auto',{'enabled':True})['queued'],1)
        self.settle()
        self.assertEqual(self.engine.session_info(key)['state'],'usable')

    def test_background_monitor_notices_deadline_without_ui_polling(self):
        key=self.account()
        self.engine.remember_session(key,{**session(),'accessToken':jwt(self.clock[0]+1200)})
        self.engine.session_auto_enabled=True
        self.clock[0]+=700
        with patch('workbench.session_lifecycle.SCAN_INTERVAL',.01):
            self.engine.maintenance_task.cancel()
            async def restart():
                await asyncio.gather(self.engine.maintenance_task,return_exceptions=True)
                self.engine.maintenance_task=asyncio.create_task(self.engine._session_maintenance())
            self.execute(restart())
            wait_until(lambda:bool(self.calls));self.settle()
        self.assertEqual(self.calls,[key])

    def test_manual_session_jobs_share_refresh_concurrency_slots(self):
        first=self.account('manual');second=self.account('automatic')
        self.engine.command('session-limit',[],1)
        counters={'active':0,'peak':0}
        async def login(key,**kwargs):
            self.calls.append(key);counters['active']+=1
            counters['peak']=max(counters['peak'],counters['active'])
            await asyncio.sleep(.12)
            self.engine.remember_session(key,session(self.store.get(key)['email']))
            self.store.update(key,status='ready');counters['active']-=1
        self.engine.edge_login=login
        self.engine.inventory_command('session',{'ids':[first]})
        self.scan();self.settle()
        self.assertEqual(counters['peak'],1)
        self.assertEqual(self.calls,[first,second])

    def test_native_helper_challenge_stops_managed_refresh_without_reopening(self):
        key=self.account()
        self.engine.edge_login=Engine.edge_login.__get__(self.engine)
        with patch('workbench.edge_session.open_ordinary_edge') as browser:
            self.scan();wait_until(lambda:bool(self.engine.edge_jobs()))
            job=self.engine.edge_jobs()[0]
            self.engine.edge_step({'record_id':key,'request_id':job['request_id'],'kind':'progress','stage':'challenge'})
            self.settle()
            self.assertTrue(self.store.get(key)['session_meta']['manual_required'])
            self.assertEqual(self.scan()['queued'],0)
            self.assertEqual(browser.call_count,1)

    def configure_recharge(self,key,provider):
        self.store.inventory_import_cards('plus',['SYNTHETIC-LIFETIME-CARD'])
        async def get_provider(_):return provider
        self.engine.provider=get_provider

    def reserve_recharge(self,key):
        assignments=self.store.inventory_quote([key],'plus')
        self.store.inventory_reserve(assignments)

    def test_recharge_refreshes_expired_access_token_despite_future_session_expires(self):
        key=self.account();provider=StockProvider()
        self.configure_recharge(key,provider)
        self.engine.sessions[key]={**session(),'accessToken':jwt(self.clock[0]-5)}
        quote=self.engine.inventory_command('quote',{'ids':[key],'sku':'plus'})
        self.engine.inventory_command('start',{'quote':quote['quote']});self.settle()
        self.assertEqual(self.calls,[key])
        self.assertEqual(provider.redeems,1)
        self.assertEqual(self.scan()['queued'],0)

    def test_explicit_rejection_refreshes_once_but_network_failure_does_not_invalidate(self):
        key=self.account()
        class Rejected(StockProvider):
            def __init__(self):super().__init__();self.preflights=0
            async def call(self,kind,body):
                if kind=='preflight':
                    self.preflights+=1
                    if self.preflights==1:raise SessionRejectedError()
                return await super().call(kind,body)
        provider=Rejected();self.configure_recharge(key,provider);self.reserve_recharge(key)
        self.engine.remember_session(key,session())
        self.execute(self.engine.preflight(key))
        self.assertEqual((provider.preflights,len(self.calls)),(2,1))
        self.assertEqual(self.engine.session_info(key)['verification'],'provider_accepted')
        self.execute(self.engine.redeem(key))
        self.assertEqual(provider.redeems,1)

    def test_expiry_between_preflight_and_submit_requires_new_preflight(self):
        key=self.account();provider=StockProvider()
        self.configure_recharge(key,provider);self.reserve_recharge(key)
        self.engine.remember_session(key,{**session(),'accessToken':jwt(self.clock[0]+1200)})
        self.execute(self.engine.preflight(key))
        self.clock[0]+=1300
        self.execute(self.engine.redeem(key))
        self.assertEqual(len(self.calls),1)
        self.assertEqual(provider.redeems,1)

    def test_network_error_does_not_invalidate_or_reopen_login_and_repeat_rejection_is_bounded(self):
        key=self.account()
        class NetworkError(StockProvider):
            async def call(self,kind,body):
                if kind=='preflight':raise RuntimeError('synthetic network failure')
                return await super().call(kind,body)
        self.configure_recharge(key,NetworkError())
        self.engine.remember_session(key,session())
        with self.assertRaises(RuntimeError):self.execute(self.engine.preflight(key))
        self.assertEqual(self.calls,[])
        self.assertEqual(self.engine.session_info(key)['state'],'usable')
        class AlwaysRejected(StockProvider):
            async def call(self,kind,body):
                if kind=='preflight':raise SessionRejectedError()
                return await super().call(kind,body)
        async def provider(_):return AlwaysRejected()
        self.engine.provider=provider
        with self.assertRaises(SessionRejectedError):self.execute(self.engine.preflight(key))
        self.assertEqual(self.calls,[key])
        self.assertEqual(self.engine.session_info(key)['state'],'invalid')
        self.assertFalse(self.store.charge_tasks)


if __name__=='__main__':unittest.main(verbosity=2)
