import asyncio
import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from app import make_server
from workbench.engine import Engine
from workbench.store import Store
from test_complete_flow import ProviderFixture, session, wait_until


class DisplayProvider(ProviderFixture):
    def __init__(self, blocked=False):
        super().__init__()
        self.calls = []
        self.blocked = blocked

    async def call(self, kind, body):
        self.calls.append(kind)
        result = await super().call(kind, body)
        if kind == 'preflight':
            result.update(current_plan='plus' if self.blocked else 'free',
                          subscription_has_active=self.blocked,
                          subscription_active_until='2027-03-01T00:00:00Z',
                          subscription_will_renew=False)
        return result


class ModeEngineTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'records.sqlite3')
        self.engine = Engine(self.store)

    def tearDown(self):
        self.engine.close()
        self.store.db.close()
        self.temp.cleanup()

    def provider(self, instance):
        async def get_provider(_):
            return instance
        self.engine.provider = get_provider

    def test_optional_card_import_is_atomic_and_only_real_duplicates_are_rejected(self):
        keys = self.store.add_many([{'email':'first@example.com'}, {'email':'second@example.com','code':''},
                                    {'email':'owner@example.com','code':'UNIQUE-TEST-CARD'}])
        self.assertTrue(self.store.get(keys[0])['probe_only'])
        self.assertEqual(self.store.get(keys[1])['card'], '未绑定卡密')
        self.assertFalse(self.store.get(keys[2])['probe_only'])
        with self.assertRaises(ValueError):
            self.store.add_many([{'email':'third@example.com'}, {'email':'duplicate@example.com','code':'UNIQUE-TEST-CARD'}])
        self.assertEqual(len(self.store.public()), 3)

    def test_session_only_accepts_both_kinds_without_touching_provider(self):
        keys = self.store.add_many([{'email':'only@example.com'}, {'email':'owner@example.com','code':'TEST-BOUND-CARD'}])
        touched = []
        async def login(key):
            self.engine.sessions[key] = session(self.store.get(key)['email'])
            self.store.update(key, status='ready')
        async def forbidden(_):
            touched.append(True)
            raise AssertionError('Session-only must not contact a provider')
        self.engine.edge_login = login
        self.engine.provider = forbidden
        self.engine.command('session_only', keys)
        wait_until(lambda: all(self.engine.jobs[key].done() for key in keys))
        self.assertEqual(touched, [])
        self.assertTrue(all(self.store.get(key)['status']=='ready' for key in keys))
        self.assertTrue(all(not self.store.get(key)['submitted'] for key in keys))
        self.assertTrue(all(not self.store.get(key).get('precheck') for key in keys))

    def test_auto_exchange_reuses_session_and_submits_once_after_precheck(self):
        key = self.store.inventory_import_accounts([{'email':'owner@example.com'}])['ids'][0]
        self.store.inventory_import_cards('plus',['AUTO-TEST-CARD'])
        self.store.inventory_reserve(self.store.inventory_quote([key],'plus'))
        self.engine.sessions[key] = session()
        provider = DisplayProvider()
        self.provider(provider)
        with patch('workbench.edge_session.open_ordinary_edge') as browser:
            self.engine.command('redeem_auto', [key])
            wait_until(lambda: self.engine.jobs[key].done())
        browser.assert_not_called()
        self.assertEqual(provider.calls, ['preview','preflight','redeem'])
        self.assertEqual(provider.redeems, 1)
        row = self.store.get(key)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['precheck']['current_plan'], 'free')
        self.assertIs(row['precheck']['will_renew'], False)
        public = json.dumps(self.engine.snapshot())
        for secret in ['TEST-PREVIEW','TEST-PREFLIGHT','TEST-COOKIE-ONLY','TEST-ACCESS-ONLY']:
            self.assertNotIn(secret, public)
        with self.assertRaises(ValueError):
            self.engine.command('redeem_auto', [key])

    def test_auto_exchange_without_card_is_rejected_before_job_creation(self):
        key = self.store.add_many([{'email':'only@example.com'}])[0]
        with self.assertRaises(ValueError):
            self.engine.command('redeem_auto', [key])
        self.assertNotIn(key, self.engine.jobs)
        self.assertFalse(self.store.get(key)['submitted'])

    def test_blocked_precheck_keeps_visible_details_and_does_not_submit(self):
        key = self.store.add_many([{'email':'owner@example.com','code':'BLOCKED-TEST-CARD'}])[0]
        self.engine.sessions[key] = session()
        provider = DisplayProvider(blocked=True)
        self.provider(provider)
        self.engine.command('redeem_auto', [key])
        wait_until(lambda: self.engine.jobs[key].done())
        row = self.store.get(key)
        self.assertEqual(row['status'], 'error')
        self.assertEqual(row['precheck']['current_plan'], 'plus')
        self.assertEqual(row['precheck']['email'], 'owner@example.com')
        self.assertIn('已有', row['message'])
        self.assertFalse(row['submitted'])
        self.assertEqual(provider.redeems, 0)


@unittest.skipIf((ROOT/'web'/'dist'/'index.html').exists(), 'Legacy combined-page UI replaced by test_inventory_browser')
class ModeBrowserTest(unittest.TestCase):
    def test_save_without_card_session_only_and_confirmed_auto_exchange_panel(self):
        from playwright.sync_api import sync_playwright, expect
        with TemporaryDirectory() as temporary, sync_playwright() as pw:
            server = make_server(Path(temporary) / 'data')
            provider = DisplayProvider()
            async def get_provider(_):
                return provider
            async def login(key):
                server.engine.sessions[key] = session(server.store.get(key)['email'])
                server.store.update(key, status='ready', message='Fixture Session ready')
            server.engine.provider = get_provider
            server.engine.edge_login = login
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            browser = pw.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page(viewport={'width':1530,'height':1100})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            try:
                page.goto('http://' + server.address + '/#' + server.token, wait_until='networkidle')
                page.locator('#add-open').click()
                form = page.locator('#add-form')
                form.locator('[name=email]').fill('only@example.com')
                form.locator('[name=eligibility_months]').select_option('1')
                form.locator('[data-mode=save]').click()
                expect(page.locator('#add-dialog')).not_to_be_visible()
                key = server.store.public()[0]['id']
                self.assertFalse(server.engine.jobs)
                self.assertIn('未绑定卡密', page.locator('#precheck-facts').inner_text())
                expect(page.locator('#precheck-actions [data-precheck-action=redeem_auto]')).to_be_disabled()
                page.locator(f'#rows [data-row-action=session_only][data-id="{key}"]').click()
                page.locator(f'#rows [data-row-action=session][data-id="{key}"]').wait_for()
                self.assertEqual(provider.calls, [])
                self.assertEqual(provider.redeems, 0)

                page.locator('#add-open').click()
                form.locator('[name=email]').fill('owner@example.com')
                form.locator('[name=password]').fill('TEST-PASSWORD')
                form.locator('[name=code]').fill('UI-AUTO-TEST-CARD')
                form.locator('[name=eligibility_months]').select_option('3')
                form.locator('[data-mode=save]').click()
                expect(page.locator('#add-dialog')).not_to_be_visible()
                other = next(row for row in server.store.public() if row['email']=='owner@example.com')['id']
                page.locator(f'#rows [data-row-action=redeem_auto][data-id="{other}"]').click()
                expect(page.locator('#confirm-dialog')).to_be_visible()
                self.assertFalse(server.engine.jobs.get(other))
                self.assertEqual(provider.redeems, 0)
                self.assertIn('不再弹第二次', page.locator('#confirm-content').inner_text())
                page.locator('#confirm-execute').click()
                page.locator('#precheck-status.completed').wait_for(timeout=8000)
                self.assertEqual(provider.redeems, 1)
                facts = page.locator('#precheck-facts').inner_text()
                for value in ['owner@example.com','Plus','free','未订阅','2027','到期后停用']:
                    self.assertIn(value, facts)
                self.assertEqual(errors, [])
                artifact = ROOT / 'output' / 'playwright' / 'precheck-panel.png'
                artifact.parent.mkdir(parents=True, exist_ok=True)
                page.locator('#precheck-panel').screenshot(path=str(artifact))
            finally:
                browser.close()
                server.shutdown()
                thread.join(3)
                server.engine.close()
                server.server_close()
                server.store.db.close()
                server.instance_lock.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
