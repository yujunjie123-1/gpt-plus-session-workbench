import asyncio
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.store import Store, crypt
from workbench.engine import Engine, check_identity, session_cookie
from workbench.provider import lookup_state, blocking_reason
from workbench.auth import extract_mail_code, fetch_mail_code, totp_code
from app import make_server, safe_csv


class MockProvider:
    def __init__(self, email='owner@example.com'):
        self.row = dict(cdk_code='TEST-PLUS-CARD', status='unused', can_resubmit=True)
        self.email = email
        self.redeems = 0
        self.timeout = False

    async def lookup(self, code):
        return dict(self.row, cdk_code=code)

    async def call(self, action, body):
        if action == 'preview':
            return dict(product='gpt', plan='plus', redemption_token='secret-redemption')
        if action == 'preflight':
            return dict(email=self.email, current_plan='free', subscription_has_active=False, preflight_token='secret-ticket')
        if action == 'redeem':
            self.redeems += 1
            if self.timeout:
                raise TimeoutError()
            self.row.update(status='used', used=True, account_email=self.email)
            return {}

    async def result(self, token):
        return {'status': 'processing'} if self.timeout else {'status': 'completed', 'account_email': self.email}

    async def result_by_code(self, code):
        return {'status': 'processing'} if self.timeout else {'status': 'completed', 'account_email': self.email}


class RulesTest(unittest.TestCase):
    def test_totp_rfc_vector(self):
        self.assertEqual(totp_code('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',timestamp=59,digits=8),'94287082')
        with self.assertRaises(ValueError): totp_code('not-a-valid-secret')

    def test_mail_code_extracts_only_six_digit_values(self):
        self.assertEqual(extract_mail_code({'success':True,'data':{'verification_code':' 123456 '}}),'123456')
        self.assertEqual(extract_mail_code('Your OpenAI code is 654321'),'654321')
        self.assertEqual(extract_mail_code({'code':0,'message':'ok'}),'')

    def test_mail_fetch_rejects_private_hosts_redirects_and_malformed_url(self):
        with self.assertRaises(RuntimeError): fetch_mail_code('http://127.0.0.1/api?key=SECRET')
        with self.assertRaises(RuntimeError): fetch_mail_code('https://127.0.0.1/api?key=SECRET')
        with self.assertRaises(RuntimeError): fetch_mail_code('https://user:SECRET@example.com/api')

    def test_success_requires_both_fields(self):
        self.assertEqual(lookup_state({'status':'used','used':True}), 'completed')
        for row in [{'status':'used'}, {'status':'used','used':'true'}, {'status':'plus_paid'}, {'status':'review'}]:
            self.assertNotEqual(lookup_state(row), 'completed')

    def test_cookie_chunks_and_domain(self):
        name='__Secure-next-auth.session-token'
        self.assertEqual(session_cookie([dict(name=name+'.1',value='B',domain='.chatgpt.com'),dict(name=name+'.0',value='A',domain='chatgpt.com')]),'AB')
        self.assertEqual(session_cookie([dict(name=name+'.1',value='B',domain='.chatgpt.com')]),'')
        self.assertEqual(session_cookie([dict(name=name,value='secret',domain='.evil.test')]),'')

    def test_identity_expiry_and_existing_plan(self):
        with self.assertRaises(ValueError):
            check_identity({'user':{'email':'other@example.com'},'accessToken':'x'}, 'owner@example.com')
        with self.assertRaises(ValueError):
            check_identity({'user':{'email':'owner@example.com'},'accessToken':'x','expires':'2020-01-01T00:00:00Z'}, 'owner@example.com')
        self.assertTrue(blocking_reason({'current_plan':'plus'}, 'plus', ''))
        self.assertTrue(blocking_reason({'subscription_is_delinquent':True}, 'plus', 'card_attach'))

    def test_export_escapes_formulas(self):
        self.assertEqual(safe_csv('=1+1'), "'=1+1")
        self.assertEqual(safe_csv('  @SUM(A1)'), "'  @SUM(A1)")


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'db.sqlite3')
        self.key=self.store.add_many([dict(email='owner@example.com',code='TEST-PLUS-CARD',password='sample-password',totp='GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',mail_url='https://mail.example.com/api?key=PRIVATE')])[0]
        self.engine=Engine(self.store)
        self.provider=MockProvider()
        self.engine.sessions[self.key]={'user':{'email':'owner@example.com'},'accessToken':'token','sessionToken':'session-secret','expires':'2099-01-01T00:00:00Z'}
        async def provider(_): return self.provider
        self.engine.provider=provider

    def tearDown(self):
        self.engine.close()
        self.store.db.close()
        self.temp.cleanup()

    def execute(self, coro, timeout=5):
        return asyncio.run_coroutine_threadsafe(coro,self.engine.loop).result(timeout)

    def wait_job(self, key=None):
        key=key or self.key
        end=time.monotonic()+3
        while time.monotonic()<end:
            if self.engine.jobs[key].done(): return
            time.sleep(.02)
        self.fail('job did not finish')

    def reserve_current_card(self):
        self.store.update(self.key, execution_mode='session', active_task_id=None,
                          active_card_id=None, probe_only=False, submitted=False,
                          secret=crypt(''), card='未分配卡密')
        self.store.inventory_import_cards('plus',['TEST-PLUS-CARD'])
        assignments=self.store.inventory_quote([self.key],'plus')
        self.store.inventory_reserve(assignments)

    def test_storage_encryption_atomic_import_and_secret_redaction(self):
        data=(Path(self.temp.name)/'db.sqlite3').read_bytes()
        self.assertNotIn(b'TEST-PLUS-CARD',data)
        self.assertNotIn(b'sample-password',data)
        self.assertNotIn(b'sample-totp',data)
        self.assertEqual(crypt(self.store.get(self.key)['secret'],True),'TEST-PLUS-CARD')
        with self.assertRaises(ValueError):
            self.store.add_many([dict(email='valid@example.com',code='NEW-CARD'),dict(email='bad',code='BAD-CARD')])
        self.assertEqual(len(self.store.public()),1)
        self.assertNotIn('secret',self.store.public()[0])

    def test_preflight_confirm_once_and_restart_recovery(self):
        self.reserve_current_card()
        self.execute(self.engine.preflight(self.key))
        self.assertEqual(self.store.get(self.key)['status'],'confirm')
        self.assertNotIn('secret-ticket',json.dumps(self.store.public()))
        self.execute(self.engine.redeem(self.key))
        self.assertEqual(self.provider.redeems,1)
        self.assertEqual(self.store.get(self.key)['status'],'completed')
        with self.assertRaises(ValueError): self.execute(self.engine.redeem(self.key))
        self.store.update(self.key,status='pending',submitted=True)
        restored=Store(Path(self.temp.name)/'db.sqlite3')
        self.assertTrue(restored.get(self.key)['submitted'])
        self.assertEqual(restored.get(self.key)['status'],'pending')
        restored.db.close()

    def test_wrong_preflight_email_never_submits(self):
        self.provider.email='wrong@example.com'
        with self.assertRaises(ValueError): self.execute(self.engine.preflight(self.key))
        self.assertNotIn(self.key,self.engine.tickets)
        self.assertEqual(self.provider.redeems,0)

    def test_timeout_preserves_durable_guard_and_release_required(self):
        self.reserve_current_card()
        self.execute(self.engine.preflight(self.key))
        self.provider.timeout=True
        self.engine.command('confirm',[self.key])
        end=time.monotonic()+2
        while not self.provider.redeems and time.monotonic()<end: time.sleep(.02)
        self.assertTrue(self.store.get(self.key)['submitted'])
        self.engine.command('stop',[self.key])
        self.wait_job()
        self.assertTrue(self.store.get(self.key)['submitted'])
        self.execute(self.engine.apply_lookup(self.key,dict(status='unused',can_resubmit=True)))
        self.assertFalse(self.store.get(self.key)['can_retry'])
        with self.assertRaises(ValueError): self.engine.command('retry',[self.key])
        self.execute(self.engine.apply_lookup(self.key,dict(status='failed',can_resubmit=True)))
        self.assertTrue(self.store.get(self.key)['can_retry'])
        self.assertFalse(self.store.get(self.key)['submitted'])

    def test_pause_resume_and_concurrency(self):
        key2=self.store.add_many([dict(email='second@example.com',code='OTHER-CARD')])[0]
        active=0; maximum=0
        async def mock_login(key):
            nonlocal active,maximum
            active+=1; maximum=max(active,maximum)
            await asyncio.sleep(.12)
            active-=1
            self.store.update(key,status='ready')
        self.engine.login=mock_login
        self.engine.command('pause',[])
        self.engine.command('login',[self.key,key2])
        time.sleep(.08)
        self.assertEqual(maximum,0)
        self.engine.command('resume',[])
        self.wait_job();self.wait_job(key2)
        self.assertEqual(maximum,1)

    def test_duplicate_confirm_rejected_while_queued(self):
        self.execute(self.engine.preflight(self.key))
        self.engine.command('pause',[])
        self.engine.command('confirm',[self.key])
        with self.assertRaises(ValueError): self.engine.command('confirm',[self.key])
        self.assertEqual(self.provider.redeems,0)

    def test_inprivate_login_fills_email_password_email_otp_totp_and_reads_session(self):
        class Page:
            def __init__(self):
                self.stage='email';self.url='about:blank';self.filled=[];self.session={
                    'user':{'email':'owner@example.com'},'accessToken':'auto-access','expires':'2099-01-01T00:00:00Z'}
            async def goto(self,url,**kwargs):
                if url.endswith('/api/auth/session'):
                    self.stage='session';self.url=url
                else:
                    self.stage='email';self.url=url
            def locator(self,selector): return Field(self,selector)
            def get_by_role(self,*args,**kwargs): return Field(self,'submit')
            async def wait_for_timeout(self,*args): pass
            def on(self,*args): pass
            def remove_listener(self,*args): pass
        class Field:
            def __init__(self,page,selector): self.page=page;self.selector=selector
            @property
            def first(self): return self
            def nth(self,index): return self
            async def count(self):
                p=self.page
                if self.selector=='body': return 1
                if self.selector in ['input[type="email"], input[autocomplete="username"]','input[type="email"]','input[autocomplete="username"]']: return int(p.stage=='email')
                if self.selector=='input[type="password"]': return int(p.stage=='password')
                if 'input[autocomplete="one-time-code"]' in self.selector: return int(p.stage in ['email-code','totp'])
                if self.selector=='input[maxlength="1"]': return 0
                if self.selector=='button[type="submit"]:not([formaction]), input[type="submit"]:not([formaction])': return int(p.stage in ['email','password','email-code','totp'])
                return 0
            async def is_visible(self): return bool(await self.count())
            async def is_enabled(self): return True
            async def is_editable(self,**kwargs): return True
            async def evaluate(self,*args): return True
            def locator(self,selector): return Field(self.page,selector)
            async def fill(self,value,**kwargs): self.page.filled.append((self.page.stage,value))
            async def click(self,**kwargs):
                p=self.page
                if p.stage=='email': p.stage='password';p.url='https://auth.openai.com/log-in/password'
                elif p.stage=='password': p.stage='email-code'
                elif p.stage=='email-code': p.stage='totp'
                elif p.stage=='totp': p.stage='home';p.url='https://chatgpt.com/'
            async def press(self,key,**kwargs): await self.click()
            async def inner_text(self,**kwargs):
                p=self.page
                if p.stage=='email-code': return 'Check your email. We sent you a code.'
                if p.stage=='totp': return 'Enter the code from your authenticator app.'
                if p.stage=='session': return json.dumps(p.session)
                return 'Continue'
        page=Page()
        page.evaluate=AsyncMock(return_value=page.session)
        client=SimpleNamespace(send=AsyncMock(return_value={'cookies':[dict(name='__Secure-next-auth.session-token',value='auto-cookie',domain='.chatgpt.com')]}),detach=AsyncMock())
        context=SimpleNamespace(new_page=AsyncMock(return_value=page),pages=[page],
            new_cdp_session=AsyncMock(return_value=client),
            cookies=AsyncMock(return_value=[dict(name='__Secure-next-auth.session-token',value='auto-cookie',domain='.chatgpt.com')]),
            close=AsyncMock())
        browser=SimpleNamespace(new_context=AsyncMock(return_value=context),close=AsyncMock())
        self.engine.ensure_browser=AsyncMock()
        self.engine.pw=SimpleNamespace(stop=AsyncMock())
        @asynccontextmanager
        async def private_page(pw):
            try:
                yield browser,context,page
            finally:
                await context.close()
                await browser.close()
        with patch('workbench.engine.private_login_page',private_page), patch('workbench.engine.fetch_mail_code',return_value='123456') as mail, patch('workbench.engine.totp_code',return_value='654321') as totp:
            self.execute(self.engine.login(self.key),timeout=20)
        self.assertEqual(page.filled,[('email','owner@example.com'),('password','sample-password'),('email-code','123456'),('totp','654321')])
        self.assertEqual(mail.call_count,1)
        self.assertTrue(mail.call_args.args[0].startswith('https://mail.example.com/'))
        totp.assert_called_once()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        page.evaluate.assert_awaited_once()
        self.assertEqual(page.url,'https://chatgpt.com/')
        self.assertEqual(self.engine.sessions[self.key]['sessionToken'],'auto-cookie')
        self.assertEqual(self.store.get(self.key)['status'],'ready')
        self.assertNotIn('sample-password',json.dumps(self.store.public()))

    def test_session_poll_does_not_navigate_challenge_or_request_without_cookie(self):
        page=SimpleNamespace(url='https://chatgpt.com/',goto=AsyncMock(),evaluate=AsyncMock())
        client=SimpleNamespace(send=AsyncMock(return_value={'cookies':[]}),detach=AsyncMock())
        context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=client))
        self.assertIsNone(self.execute(self.engine._try_authenticated_session(page,context,'owner@example.com')))
        page.evaluate.assert_not_awaited()
        page.goto.assert_not_awaited()

    def test_login_route_error_stops_before_any_credential_submission(self):
        field=SimpleNamespace(inner_text=AsyncMock(return_value='Route Error (400 Invalid content type: text/html; charset=UTF-8)'))
        page=SimpleNamespace(goto=AsyncMock(),locator=lambda _:field,on=lambda *_:None,remove_listener=lambda *_:None)
        context=SimpleNamespace(pages=[page])
        self.engine.ensure_browser=AsyncMock()
        self.engine.pw=SimpleNamespace(stop=AsyncMock())
        self.engine._submit_login_step=AsyncMock()
        @asynccontextmanager
        async def private_page(_):
            yield None,context,page
        with patch('workbench.engine.private_login_page',private_page):
            with self.assertRaisesRegex(ValueError,'400'):
                self.execute(self.engine.login(self.key))
        self.engine._submit_login_step.assert_not_awaited()
        self.assertNotIn(self.key,self.engine.sessions)
        self.assertNotIn(self.key,self.engine.login_contexts)

    def test_eligibility_changes_persist_without_changing_submission(self):
        before=self.store.get(self.key)
        self.store.update(self.key,submitted=True,status='pending')
        self.store.set_eligibility([self.key],3)
        self.assertEqual(self.store.get(self.key)['eligibility_months'],3)
        self.assertTrue(self.store.get(self.key)['submitted'])
        self.assertEqual(self.store.get(self.key)['status'],'pending')
        self.assertEqual(self.store.get(self.key)['secret'],before['secret'])
        with self.assertRaises(ValueError): self.store.set_eligibility([self.key,'missing-id'],1)
        self.assertEqual(self.store.get(self.key)['eligibility_months'],3)
        restored=Store(Path(self.temp.name)/'db.sqlite3')
        self.assertEqual(restored.get(self.key)['eligibility_months'],3)
        restored.db.close()

    def test_mail_url_is_encrypted_and_never_public(self):
        key=self.store.add_many([dict(email='other@example.com',code='ANOTHER-CARD',eligibility_months='1',
            mail_url='https://mail.example.com/api/code?key=PRIVATE_MAIL_KEY')])[0]
        self.assertNotIn(b'PRIVATE_MAIL_KEY',(Path(self.temp.name)/'db.sqlite3').read_bytes())
        row=next(r for r in self.store.public() if r['id']==key)
        self.assertEqual(row['eligibility_months'],1)
        self.assertTrue(row['mail_configured'])
        self.assertNotIn('mail_url',row)
        self.assertNotIn('PRIVATE_MAIL_KEY',json.dumps(self.engine.snapshot()))

    def test_invalid_eligibility_and_mail_url_import_is_atomic(self):
        for field,value in [('eligibility_months',2),('eligibility_months',True),('mail_url','http://mail.example.com/?key=PRIVATE')]:
            with self.assertRaises(ValueError):
                self.store.add_many([dict(email='valid@example.com',code='VALID-NEW-CARD'),
                    dict(email='other@example.com',code='OTHER-NEW-CARD',**{field:value})])
            self.assertEqual(len(self.store.public()),1)

    def test_legacy_records_remain_unclassified(self):
        with self.store.lock:
            original=self.store.rows[self.key]
            original.pop('eligibility_months')
            original.pop('mail_url')
            self.store._save(original)
        restored=Store(Path(self.temp.name)/'db.sqlite3')
        self.assertEqual(restored.get(self.key)['eligibility_months'],'')
        self.assertFalse(restored.public()[0]['mail_configured'])
        restored.db.close()


class HTTPTest(unittest.TestCase):
    def test_auth_origin_export_and_single_instance(self):
        with TemporaryDirectory() as temp:
            server=make_server(temp)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            origin='http://'+server.address
            auth={'Authorization':'Bearer '+server.token}
            try:
                with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(origin+'/api/state')
                req=urllib.request.Request(origin+'/api/state',headers=auth)
                self.assertEqual(json.load(urllib.request.urlopen(req))['records'],[])
                body=json.dumps({'items':[{'email':'owner@example.com','code':'TEST-CODE'}]}).encode()
                req=urllib.request.Request(origin+'/api/import',data=body,headers={**auth,'Content-Type':'application/json','Origin':'https://evil.test'})
                with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(req)
                req=urllib.request.Request(origin+'/api/import',data=body,headers={**auth,'Content-Type':'application/json','Origin':origin})
                self.assertEqual(len(json.load(urllib.request.urlopen(req))['ids']),1)
                raw=urllib.request.urlopen(urllib.request.Request(origin+'/api/export',headers=auth)).read()
                self.assertNotIn(b'TEST-CODE',raw)
                with self.assertRaises(RuntimeError): make_server(temp)
            finally:
                server.shutdown();thread.join(3);server.engine.close();server.server_close();server.store.db.close();server.instance_lock.close()


if __name__=='__main__':
    unittest.main(verbosity=2)
