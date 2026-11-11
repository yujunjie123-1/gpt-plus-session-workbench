import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.engine import Engine
from workbench.store import Store, crypt
from workbench.auth import extract_mail_code, totp_code


def session(email='owner@example.com'):
    return {'user': {'email': email}, 'accessToken': 'TEST-ACCESS-ONLY',
            'sessionToken': 'TEST-COOKIE-ONLY', 'expires': '2099-01-01T00:00:00Z'}


def wait_until(predicate):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('Asynchronous task did not settle')


class ProviderFixture:
    def __init__(self):
        self.redeems = 0

    async def lookup(self, code):
        return {'cdk_code': code, 'status': 'used' if self.redeems else 'unused', 'used': bool(self.redeems), 'account_email': 'owner@example.com'}

    async def result(self, token):
        email = getattr(self, 'email', 'owner@example.com')
        return {'status': 'completed', 'account_email': email} if self.redeems else {'status': 'processing'}

    async def call(self, kind, body):
        if kind == 'preview':
            return {'product': 'gpt', 'plan': 'plus', 'redemption_token': 'TEST-PREVIEW'}
        if kind == 'preflight':
            assert json.loads(body['credential']['session'])['sessionToken'] == 'TEST-COOKIE-ONLY'
            return {'email': 'owner@example.com', 'current_plan': 'free', 'subscription_has_active': False, 'preflight_token': 'TEST-PREFLIGHT'}
        if kind == 'redeem':
            self.redeems += 1
            return {}


class CompleteFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'records.sqlite3')
        self.key = self.store.add_many([{'email': 'owner@example.com', 'code': 'LOCAL-PROBE-TEST',
                                       'password': 'TEST-PASSWORD', 'totp': 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',
                                       'mail_url': 'https://mail.example.com/?key=TEST-MAIL-KEY'}])[0]
        self.store.update(self.key, probe_only=True, card='Session 测试 / 无卡密', status='ready')
        self.engine = Engine(self.store)
        self.engine.sessions[self.key] = session()

    def tearDown(self):
        self.engine.close()
        self.store.db.close()
        self.temp.cleanup()

    def test_binding_preserves_session_then_preflight_requires_explicit_confirmation(self):
        original = self.engine.sessions[self.key]
        self.engine.tickets[self.key] = {'stale': True}
        result = self.engine.update_account({'id': self.key, 'code': 'REAL-FORMAT-TEST-CARD', 'eligibility_months': 3})
        self.assertTrue(result['session_valid'])
        self.assertFalse(result['probe_only'])
        self.assertIs(self.engine.sessions[self.key], original)
        self.assertNotIn(self.key, self.engine.tickets)
        provider = ProviderFixture()
        async def get_provider(_):
            return provider
        self.engine.provider = get_provider
        with patch('workbench.edge_session.open_ordinary_edge') as browser:
            self.engine.command('prepare', [self.key])
            wait_until(lambda: self.engine.jobs[self.key].done())
        browser.assert_not_called()
        self.assertEqual(self.store.get(self.key)['status'], 'confirm')
        self.assertEqual(provider.redeems, 0)
        self.store.inventory_import_cards('plus', ['REAL-FORMAT-TEST-CARD'])
        assignments = self.store.inventory_quote([self.key], 'plus')
        self.store.inventory_reserve(assignments)
        self.engine.command('prepare', [self.key])
        wait_until(lambda: self.engine.jobs[self.key].done())
        self.engine.command('confirm', [self.key])
        wait_until(lambda: self.engine.jobs[self.key].done())
        self.assertEqual(provider.redeems, 1)
        self.assertEqual(self.store.get(self.key)['status'], 'completed')
        with self.assertRaises(ValueError):
            self.engine.command('confirm', [self.key])

    def test_account_edit_is_atomic_and_blocks_other_email_and_duplicate_card(self):
        self.store.add_many([{'email': 'other@example.com', 'code': 'OTHER-EXISTING-CARD'}])
        original = self.store.get(self.key)['password']
        with self.assertRaises(ValueError):
            self.engine.update_account({'id': self.key, 'password': 'NEW-SECRET', 'code': 'OTHER-EXISTING-CARD'})
        self.assertEqual(self.store.get(self.key)['password'], original)
        with self.assertRaises(ValueError):
            self.engine.update_account({'id': self.key, 'email': 'other@example.com', 'code': 'NEW-CARD'})
        self.assertEqual(self.engine.sessions[self.key]['user']['email'], 'owner@example.com')

    def test_session_view_is_explicit_and_expiry_clears_stale_confirmation(self):
        value = self.engine.view_session(self.key)
        self.assertEqual(value['session']['sessionToken'], 'TEST-COOKIE-ONLY')
        value['session']['accessToken'] = 'CHANGED-COPY'
        self.assertEqual(self.engine.sessions[self.key]['accessToken'], 'TEST-ACCESS-ONLY')
        self.assertNotIn('TEST-COOKIE-ONLY', json.dumps(self.engine.snapshot()))
        self.engine.sessions[self.key]['expires'] = '2020-01-01T00:00:00Z'
        self.engine.tickets[self.key] = {'stale': True}
        with self.assertRaises(ValueError):
            self.engine.view_session(self.key)
        self.assertNotIn(self.key, self.engine.sessions)
        self.assertNotIn(self.key, self.engine.tickets)

    def test_nonce_scoped_credentials_mail_otp_and_totp_never_enter_public_state(self):
        with patch('workbench.edge_session.open_ordinary_edge'):
            self.engine.command('edge_login', [self.key])
            wait_until(lambda: bool(self.engine.edge_jobs()))
        job = self.engine.edge_jobs()[0]
        self.assertTrue(job['automatic'])
        body = {'record_id': self.key, 'request_id': job['request_id']}
        self.assertEqual(self.engine.edge_step({**body, 'kind': 'password'})['value'], 'TEST-PASSWORD')
        with patch('workbench.edge_session.fetch_mail_code', return_value='123456') as mail, patch('workbench.edge_session.totp_code', return_value='654321'):
            first = self.engine.edge_step({**body, 'kind': 'email_code'})
            second = self.engine.edge_step({**body, 'kind': 'email_code'})
            totp = self.engine.edge_step({**body, 'kind': 'totp'})
        self.assertEqual(first['value'], '123456')
        self.assertEqual(first['code_id'], second['code_id'])
        self.assertEqual(totp['value'], '654321')
        self.assertTrue(mail.called)
        public = json.dumps(self.engine.snapshot()) + json.dumps(self.engine.edge_jobs())
        for secret in ['TEST-PASSWORD', 'TEST-MAIL-KEY', '123456', '654321', 'GEZDGNBVGY3TQOJQ']:
            self.assertNotIn(secret, public)
        with self.assertRaises(ValueError):
            self.engine.edge_step({**body, 'request_id': 'wrong-nonce', 'kind': 'password'})
        self.engine.edge_step({**body, 'kind': 'failure', 'code': 'route400'})
        wait_until(lambda: self.engine.jobs[self.key].done())
        self.assertEqual(self.store.get(self.key)['status'], 'error')
        self.assertIn('400', self.store.get(self.key)['message'])
        with self.assertRaises(ValueError):
            self.engine.edge_step({**body, 'kind': 'password'})

    def test_recovery_reads_existing_browser_without_requesting_password_or_new_window(self):
        with patch('workbench.edge_session.open_ordinary_edge') as browser:
            self.engine.command('edge_recover', [self.key])
            wait_until(lambda: bool(self.engine.edge_jobs()))
            job = self.engine.edge_jobs()[0]
            self.assertFalse(job['automatic'])
            self.assertTrue(job['recovery'])
            body = {'record_id': self.key, 'request_id': job['request_id']}
            with self.assertRaises(ValueError):
                self.engine.edge_step({**body, 'kind': 'password'})
            self.engine.receive_edge_session({**body, 'session': session()})
            wait_until(lambda: self.engine.jobs[self.key].done())
        browser.assert_not_called()
        self.assertEqual(self.store.get(self.key)['status'], 'ready')

    def test_plain_mail_codes_and_unpadded_base32_secrets_are_supported(self):
        for body in ['123456', '"123456"', b'123456', 123456]:
            self.assertEqual(extract_mail_code(body), '123456')
        self.assertEqual(totp_code('JBSWY3DPEHPK3PXPAA', timestamp=100), totp_code('JBSWY3DPEHPK3PXPAA======', timestamp=100))

    def test_independent_batch_logins_overlap_and_one_failure_does_not_block_other_accounts(self):
        keys = self.store.add_many([{'email': 'first@example.com', 'code': 'BATCH-FIRST', 'password': 'TEST'},
                                    {'email': 'second@example.com', 'code': 'BATCH-SECOND', 'password': 'TEST'}])
        active = 0
        maximum = 0
        async def login(key):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            self.engine.edge_requests[key] = {'test': True}
            try:
                await asyncio.sleep(0.05)
                if key == keys[0]:
                    raise ValueError('Fixture login failed')
                self.engine.sessions[key] = session(self.store.get(key)['email'])
            finally:
                self.engine.edge_requests.pop(key, None)
                active -= 1
        async def preflight(key):
            self.store.update(key, status='confirm', message='Fixture preflight passed')
        self.engine.edge_login = login
        self.engine.preflight = preflight
        self.engine.command('limit', [], 3)
        self.engine.command('pause', [])
        self.engine.command('auto_batch', keys)
        time.sleep(0.05)
        self.assertEqual(maximum, 0)
        self.engine.command('resume', [])
        wait_until(lambda: all(self.engine.jobs[key].done() for key in keys))
        self.assertEqual(maximum, 2)
        self.assertEqual(self.store.get(keys[0])['status'], 'error')
        self.assertEqual(self.store.get(keys[1])['status'], 'confirm')


if __name__ == '__main__':
    unittest.main(verbosity=2)
