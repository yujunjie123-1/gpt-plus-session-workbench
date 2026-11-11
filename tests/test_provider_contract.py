"""Partner API contract tests use synthetic responses and never call cdk.bar."""
import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.engine import Engine
from workbench.provider import PartnerAPIError, Provider, partner_qualification, plus_qualification
from workbench.session_lifecycle import SessionRejectedError
from workbench.store import Store


class SyntheticTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        action = url.rsplit('/', 1)[-1]
        self.calls.append({'action': action, 'url': url, 'body': body,
                           'headers': dict(headers), 'timeout': timeout})
        response = self.responses[action]
        response = response(body) if callable(response) else response
        if isinstance(response, tuple):
            status, envelope, response_headers = response
        else:
            status, envelope, response_headers = 200, {'ok': True, 'data': response}, {}
        raw = envelope if isinstance(envelope, bytes) else json.dumps(envelope).encode('utf-8')
        return status, response_headers, raw


def transport(responses):
    synthetic = SyntheticTransport(responses)
    provider = Provider('synthetic-device-id-123456', password='synthetic-password',
                        fetch_impl=synthetic)
    provider.synthetic_transport = synthetic
    return provider


class PartnerAPIContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_eight_documented_operations_use_fixed_partner_paths(self):
        expected = {
            'preview': {'code': 'SYNTHETIC-CARD'},
            'preflight': {'code': 'SYNTHETIC-CARD', 'redemption_token': 'SYNTHETIC-REDEMPTION',
                          'credential': {'mode': 'session', 'session': 'SYNTHETIC-SESSION'}},
            'redeem': {'redemption_token': 'SYNTHETIC-REDEMPTION',
                       'preflight_token': 'SYNTHETIC-PREFLIGHT', 'client_request_id': 'agent-synthetic'},
            'result': {'token': 'SYNTHETIC-REDEMPTION'},
            'result-by-code': {'code': 'SYNTHETIC-CARD'},
            'lookup': {'code': 'SYNTHETIC-CARD'},
            'lookup-batch': {'codes': ['SYNTHETIC-CARD']},
            'billing': {'token_input': 'SYNTHETIC-SESSION'},
        }
        provider = transport({action: {'probe': action} for action in expected})
        for action, body in expected.items():
            self.assertEqual(await provider.call(action, body), {'probe': action})
        self.assertEqual(provider.operations, frozenset(expected))
        for call, (action, body) in zip(provider.synthetic_transport.calls, expected.items()):
            self.assertEqual(call['url'], 'https://cdk.bar/api/partner/v1/' + action)
            self.assertEqual(call['body'], body)
            self.assertEqual(call['headers']['X-Redemption-Device'], 'synthetic-device-id-123456')
            self.assertEqual(call['headers']['Content-Type'], 'application/json')
            self.assertEqual(call['headers']['Accept'], 'application/json')
            self.assertEqual(call['headers']['Authorization'], 'Basic ' + base64.b64encode(
                b'partner:synthetic-password').decode('ascii'))

    async def test_provider_rejects_invalid_device_and_missing_password(self):
        with self.assertRaises(ValueError):
            Provider('short', password='synthetic-password', fetch_impl=lambda *args: None)
        with self.assertRaisesRegex(RuntimeError, 'CDK_PARTNER_PASSWORD'):
            Provider('synthetic-device-id-123456', password='', fetch_impl=lambda *args: None)

    async def test_only_documented_data_envelope_is_returned(self):
        provider = transport({'lookup': ({'results': [], 'inside': 'kept'})})
        result = await provider.call('lookup', {'code': 'SYNTHETIC-CARD'})
        self.assertEqual(result, {'results': [], 'inside': 'kept'})
        malformed = transport({'lookup': (200, {'ok': True, 'debug': 'OUTER-SECRET'}, {})})
        with self.assertRaises(PartnerAPIError) as raised:
            await malformed.call('lookup', {'code': 'SYNTHETIC-CARD'})
        self.assertEqual(raised.exception.code, 'INVALID_RESPONSE')
        self.assertNotIn('OUTER-SECRET', str(raised.exception))

    async def test_preview_and_preflight_read_partner_ticket_fields_without_echoing_bad_values(self):
        provider = transport({
            'preview': {'product': 'gpt', 'plan': 'plus', 'redemption_token': 'SYNTHETIC-REDEMPTION'},
            'preflight': {'email': 'owner@example.com', 'preflight_token': 'SYNTHETIC-PREFLIGHT'},
        })
        preview = await provider.call('preview', {'code': 'SYNTHETIC-CARD'})
        self.assertEqual(plus_qualification(preview), ('plus', 'SYNTHETIC-REDEMPTION'))
        account = await provider.call('preflight', {'code': 'SYNTHETIC-CARD'})
        self.assertEqual(account['preflight_token'], 'SYNTHETIC-PREFLIGHT')
        for action, payload in (
            ('preview', {'product': 'gpt', 'plan': 'plus', 'redemption_token': {'REMOTE-SECRET': 'hidden'}}),
            ('preflight', {'preflight_token': ['REMOTE-SECRET']}),
        ):
            with self.subTest(action=action):
                invalid = transport({action: payload})
                with self.assertRaisesRegex(ValueError, '凭证格式异常') as raised:
                    await invalid.call(action, {})
                self.assertNotIn('REMOTE-SECRET', str(raised.exception))

    async def test_preview_does_not_accept_unlisted_product_aliases_as_plus(self):
        with self.assertRaisesRegex(ValueError, '未返回卡密产品'):
            plus_qualification({'plan': 'plus', 'redemption_token': 'SYNTHETIC-TOKEN'})
        with self.assertRaisesRegex(ValueError, '产品不是 GPT'):
            plus_qualification({'product': 'claude', 'plan': 'plus',
                                'redemption_token': 'SYNTHETIC-TOKEN'})
        with self.assertRaisesRegex(ValueError, '未返回卡密套餐信息'):
            plus_qualification({'product': 'gpt', 'redemption_token': 'SYNTHETIC-TOKEN'})
        with self.assertRaisesRegex(ValueError, '产品格式异常'):
            plus_qualification({'product': {'name': 'gpt'}, 'plan': 'plus',
                                'redemption_token': 'SYNTHETIC-TOKEN'})
        with self.assertRaisesRegex(ValueError, '缺少兑换资格凭证'):
            plus_qualification({'product': 'gpt', 'plan': 'plus', 'token': 'UNDOCUMENTED-TOKEN'})

    async def test_generic_partner_qualification_keeps_upstream_product_and_plan(self):
        for product, plan in (('gpt','plus'),('gpt','plus_half'),('ios','ios5x'),
                              ('card','5x'),('card','20x'),('card','50x'),('points','points')):
            with self.subTest(product=product,plan=plan):
                self.assertEqual(partner_qualification({'product':product,'plan':plan,
                    'redemption_token':'SYNTHETIC-REDEMPTION'}),(product,plan,'SYNTHETIC-REDEMPTION'))
        with self.assertRaisesRegex(ValueError,'未返回卡密产品'):
            partner_qualification({'plan':'plus','redemption_token':'SYNTHETIC-REDEMPTION'})

    async def test_auth_failure_and_session_rejection_are_not_confused(self):
        auth = transport({'preflight': (401, {'ok': False, 'error': {'code': 'AUTHENTICATION_FAILED'}}, {})})
        with self.assertRaises(PartnerAPIError) as raised:
            await auth.call('preflight', {})
        self.assertEqual(raised.exception.code, 'AUTHENTICATION_FAILED')
        rejected = transport({'preflight': {'session_valid': False}})
        with self.assertRaises(SessionRejectedError):
            await rejected.call('preflight', {})
        rejected = transport({'preflight': (422, {'ok': False,
            'error': {'code': 'SESSION_EXPIRED'}}, {})})
        with self.assertRaises(SessionRejectedError):
            await rejected.call('preflight', {})

    async def test_redeem_never_retries_and_uncertain_errors_keep_original_transaction(self):
        limited = transport({'redeem': (429, {'ok': False,
            'error': {'code': 'RATE_LIMITED'}}, {'Retry-After': '1'})})
        with self.assertRaises(PartnerAPIError) as raised:
            await limited.call('redeem', {'client_request_id': 'agent-synthetic'})
        self.assertEqual(raised.exception.code, 'RATE_LIMITED')
        self.assertEqual(len(limited.synthetic_transport.calls), 1)

        limited_html = transport({'redeem': (429, b'<html>rate limited</html>', {'Retry-After': '2'})})
        with self.assertRaises(PartnerAPIError) as raised:
            await limited_html.call('redeem', {'client_request_id': 'agent-synthetic'})
        self.assertEqual(raised.exception.code, 'RATE_LIMITED')
        self.assertEqual(raised.exception.retry_after, 2)
        self.assertFalse(raised.exception.uncertain)
        self.assertEqual(len(limited_html.synthetic_transport.calls), 1)

        for status in (502, 504):
            with self.subTest(status=status):
                gateway = transport({'redeem': (status, b'gateway timeout', {})})
                with self.assertRaises(PartnerAPIError) as raised:
                    await gateway.call('redeem', {'client_request_id': 'agent-synthetic'})
                self.assertTrue(raised.exception.uncertain)
                self.assertFalse(raised.exception.retryable)
                self.assertEqual(len(gateway.synthetic_transport.calls), 1)

        def timeout(*args):
            raise TimeoutError('synthetic timeout')
        network = transport({'redeem': timeout})
        with self.assertRaises(PartnerAPIError) as raised:
            await network.call('redeem', {'client_request_id': 'agent-synthetic'})
        self.assertTrue(raised.exception.uncertain)
        self.assertFalse(raised.exception.retryable)

    async def test_read_operation_retries_rate_limit_with_retry_after(self):
        responses = [
            (429, b'<html>rate limited</html>', {'Retry-After': '1'}),
            {'status': 'processing'},
        ]
        provider = transport({'result': lambda _: responses.pop(0)})
        result = await provider.call('result', {'token': 'SYNTHETIC-REDEMPTION'})
        self.assertEqual(result['status'], 'processing')
        self.assertEqual(len(provider.synthetic_transport.calls), 2)

    async def test_lookup_batch_enforces_documented_count_and_code_limits_locally(self):
        provider = transport({'lookup-batch': {'results': []}})
        await provider.lookup_batch(['SYNTHETIC-CARD'] * 100)
        self.assertEqual(len(provider.synthetic_transport.calls), 1)
        self.assertEqual(len(provider.synthetic_transport.calls[0]['body']['codes']), 100)
        for codes in ([], ['SYNTHETIC-CARD'] * 101, [''], ['X' * 257]):
            with self.subTest(count=len(codes)):
                with self.assertRaises(ValueError):
                    await provider.lookup_batch(codes)
        self.assertEqual(len(provider.synthetic_transport.calls), 1)

    async def test_lookup_requires_exact_card_association(self):
        provider = transport({'lookup': {'cdk_code': 'OTHER-CARD', 'status': 'unused'}})
        with self.assertRaisesRegex(RuntimeError, '无法精确关联原卡密'):
            await provider.lookup('SYNTHETIC-CARD')


class AttributionFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = Path(self.temp.name) / 'records.sqlite3'
        self.store = Store(self.database)
        self.key = self.store.inventory_import_accounts([{'email': 'owner@example.com'}])['ids'][0]
        self.store.inventory_import_cards('plus', ['SYNTHETIC-CARD'])
        self.engine = Engine(self.store)
        self.engine.sessions[self.key] = {'user': {'email': 'owner@example.com'},
            'accessToken': 'SYNTHETIC-ACCESS', 'sessionToken': 'SYNTHETIC-COOKIE',
            'expires': '2099-01-01T00:00:00Z'}

    def tearDown(self):
        self.engine.close()
        self.store.db.close()
        self.temp.cleanup()

    def execute(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.engine.loop).result(timeout=8)

    def begin(self):
        assignments = self.store.inventory_quote([self.key], 'plus')
        return self.store.inventory_reserve(assignments)[0]

    def install(self, preview=None, result=None, lookup_email='owner@example.com'):
        self.redeemed = False

        def lookup(body):
            return {'cdk_code': body['code'], 'status': 'used' if self.redeemed else 'unused',
                    'used': self.redeemed, 'can_resubmit': not self.redeemed,
                    **({'account_email': lookup_email} if lookup_email else {})}

        def redeem(body):
            self.assertEqual(body['redemption_token'], 'SYNTHETIC-REDEMPTION')
            self.assertEqual(body['preflight_token'], 'SYNTHETIC-PREFLIGHT')
            self.assertRegex(body['client_request_id'], r'^agent-[0-9a-f]{32}$')
            self.redeemed = True
            return {'status': 'queued'}

        self.provider = transport({
            'lookup': lookup,
            'preview': preview or {'product': 'gpt', 'plan': 'plus', 'redemption_token': 'SYNTHETIC-REDEMPTION'},
            'preflight': {'email': 'owner@example.com', 'currentPlan': 'free',
                          'subscription_has_active': False, 'preflight_token': 'SYNTHETIC-PREFLIGHT'},
            'redeem': redeem,
            'result': result or {'status': 'completed', 'account_email': 'owner@example.com'},
            'result-by-code': result or {'status': 'completed', 'account_email': 'owner@example.com'},
        })

        async def get_provider(_):
            return self.provider

        self.engine.provider = get_provider

    def test_completed_matching_email_marks_account_and_keeps_used_card_in_inventory(self):
        self.install()
        self.begin()
        self.execute(self.engine.preflight(self.key))
        self.assertEqual(self.store.get(self.key)['status'], 'confirm')
        self.execute(self.engine.redeem(self.key))
        row = self.store.get(self.key)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['last_successful_recharge']['status'], 'completed')
        self.assertEqual(sum(call['url'].endswith('/redeem') for call in self.provider.synthetic_transport.calls), 1)
        self.assertTrue(any(call['url'].endswith('/result') for call in self.provider.synthetic_transport.calls))
        task = self.store.charge_tasks[row['active_task_id']]
        card = self.store.cards[task['card_id']]
        self.assertEqual(card['status'], 'used')
        self.assertNotIn('SYNTHETIC-REDEMPTION', json.dumps(self.engine.snapshot()))
        self.assertNotIn('SYNTHETIC-PREFLIGHT', json.dumps(self.engine.snapshot()))
        self.assertNotIn('SYNTHETIC-COOKIE', json.dumps(self.engine.snapshot()))
        database = self.database.read_bytes()
        for secret in ('SYNTHETIC-REDEMPTION', 'SYNTHETIC-PREFLIGHT', 'SYNTHETIC-COOKIE'):
            self.assertNotIn(secret.encode(), database)

    def test_non_gpt_product_stops_before_account_session_preflight(self):
        self.install(preview={'product': 'claude', 'plan': 'plus',
                              'redemption_token': 'SYNTHETIC-REDEMPTION'})
        self.begin()
        with self.assertRaisesRegex(ValueError, '产品不是 GPT'):
            self.execute(self.engine.preflight(self.key))
        calls = self.provider.synthetic_transport.calls
        self.assertEqual([call['action'] for call in calls], ['lookup', 'preview'])
        self.assertNotIn('SYNTHETIC-COOKIE', json.dumps(calls))

    def test_completion_email_falls_back_to_exact_lookup_association(self):
        self.install(result={'status': 'completed'}, lookup_email='owner@example.com')
        self.begin()
        self.execute(self.engine.preflight(self.key))
        self.execute(self.engine.redeem(self.key))
        row = self.store.get(self.key)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['result_email'], 'owner@example.com')
        operations = [call['action'] for call in self.provider.synthetic_transport.calls]
        self.assertIn('lookup', operations)

    def test_missing_completion_email_is_attributed_to_preflight_email(self):
        self.install(result={'status': 'completed'}, lookup_email='')
        self.begin()
        self.execute(self.engine.preflight(self.key))
        self.execute(self.engine.redeem(self.key))
        row = self.store.get(self.key)
        self.assertFalse(row.get('attribution_pending',False))
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['result_email'], 'owner@example.com')
        self.assertEqual(row['provider_result_email'], '上游未提供')
        self.assertTrue(row.get('last_successful_recharge'))
        task = self.store.charge_tasks[row['active_task_id']]
        self.assertEqual(self.store.cards[task['card_id']]['status'], 'used')

    def test_mismatched_completion_email_is_attributed_to_preflight_email(self):
        self.install(result={'status': 'completed', 'account_email': 'other@example.com'})
        self.begin()
        self.execute(self.engine.preflight(self.key))
        self.execute(self.engine.redeem(self.key))
        row = self.store.get(self.key)
        self.assertFalse(row.get('attribution_pending',False))
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['result_email'], 'owner@example.com')
        self.assertEqual(row['provider_result_email'], 'other@example.com')
        self.assertTrue(row.get('last_successful_recharge'))
        task = self.store.charge_tasks[row['active_task_id']]
        self.assertEqual(self.store.cards[task['card_id']]['status'], 'used')

    def test_old_submitted_task_recovers_by_code_without_another_redeem(self):
        self.install(result={'status': 'completed', 'account_email': 'owner@example.com'})
        self.begin()
        self.execute(self.engine.preflight(self.key))
        row = self.store.get(self.key)
        task_id = row['active_task_id']
        self.store.inventory_update_task(self.key, client_request_id='agent-original-request',
                                         partner_redemption_token='', partner_preflight_token='')
        self.store.update(self.key, submitted=True, status='pending', message='synthetic legacy submit')
        self.execute(self.engine.refresh(self.key))
        operations = [call['url'].rsplit('/', 1)[-1] for call in self.provider.synthetic_transport.calls]
        self.assertIn('result-by-code', operations)
        self.assertNotIn('redeem', operations)
        self.assertEqual(self.store.charge_tasks[task_id]['client_request_id'], 'agent-original-request')
        self.assertEqual(self.store.get(self.key)['status'], 'completed')

    def test_queued_result_remains_pending_and_is_queried_without_resubmission(self):
        self.install(result={'status': 'processing'})
        self.begin()
        self.execute(self.engine.preflight(self.key))
        self.execute(self.engine.redeem(self.key))
        self.assertEqual(self.store.get(self.key)['status'], 'pending')
        calls_before = len(self.provider.synthetic_transport.calls)
        self.execute(self.engine.refresh(self.key))
        operations = [call['url'].rsplit('/', 1)[-1]
                      for call in self.provider.synthetic_transport.calls[calls_before:]]
        self.assertEqual(operations, ['result'])
        self.assertEqual(sum(call['url'].endswith('/redeem')
                             for call in self.provider.synthetic_transport.calls), 1)

    def test_definite_redeem_rejection_releases_only_after_exact_card_lookup(self):
        self.install(result={'status': 'processing'})
        self.provider.synthetic_transport.responses['redeem'] = (
            422, {'ok': False, 'error': {'code': 'INVALID_REQUEST'}}, {})
        self.begin()
        self.execute(self.engine.preflight(self.key))
        self.execute(self.engine.redeem(self.key))

        row = self.store.get(self.key)
        task = self.store.charge_tasks[row['active_task_id']]
        card = self.store.cards[task['card_id']]
        operations = [call['action'] for call in self.provider.synthetic_transport.calls]
        self.assertEqual(row['status'], 'failed')
        self.assertFalse(row['submitted'])
        self.assertTrue(row['can_retry'])
        self.assertEqual(card['status'], 'unused')
        self.assertEqual(operations.count('redeem'), 1)
        self.assertNotIn('result', operations)
        self.assertFalse(task.get('submission_rejected', False))

    def test_redeem_rate_limit_waits_before_checking_original_card(self):
        self.install(result={'status': 'processing'})
        self.provider.synthetic_transport.responses['redeem'] = (
            429, b'<html>rate limited</html>', {'Retry-After': '5'})
        self.begin()
        self.execute(self.engine.preflight(self.key))
        self.execute(self.engine.redeem(self.key))

        row = self.store.get(self.key)
        task = self.store.charge_tasks[row['active_task_id']]
        operations = [call['action'] for call in self.provider.synthetic_transport.calls]
        self.assertEqual(row['status'], 'pending')
        self.assertTrue(row['submitted'])
        self.assertTrue(task.get('submission_rejected'))
        self.assertEqual(operations.count('redeem'), 1)
        self.assertEqual(operations.count('lookup'), 1)
        self.assertNotIn('result', operations)
        self.assertGreaterEqual(self.engine.provider_poll_after[self.key] - __import__('time').monotonic(), 4.9)

    def test_definite_rejection_never_attributes_card_used_by_another_email(self):
        self.install(result={'status': 'processing'})
        lookups = [
            {'cdk_code': 'SYNTHETIC-CARD', 'status': 'unused', 'used': False, 'can_resubmit': True},
            {'cdk_code': 'SYNTHETIC-CARD', 'status': 'used', 'used': True,
             'can_resubmit': False, 'account_email': 'other@example.com'},
        ]
        self.provider.synthetic_transport.responses['lookup'] = lambda _: lookups.pop(0)
        self.provider.synthetic_transport.responses['redeem'] = (
            422, {'ok': False, 'error': {'code': 'INVALID_REQUEST'}}, {})
        self.begin()
        self.execute(self.engine.preflight(self.key))
        self.execute(self.engine.redeem(self.key))

        row = self.store.get(self.key)
        task = self.store.charge_tasks[row['active_task_id']]
        self.assertEqual(row['status'], 'blocked')
        self.assertFalse(row['submitted'])
        self.assertFalse(self.store.has_successful_recharge(row))
        self.assertEqual(task['result_state'], 'completed')
        self.assertEqual(self.store.cards[task['card_id']]['status'], 'used')


if __name__ == '__main__':
    unittest.main(verbosity=2)
