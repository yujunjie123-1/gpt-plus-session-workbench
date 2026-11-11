import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from app import make_server
from test_complete_flow import ProviderFixture, session


class CompleteBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='msedge', headless=True)
        cls.dom_source = (ROOT / 'edge-extension' / 'login-dom.mjs').read_text(encoding='utf-8').replace('export ', '')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def fixture(self, html, url='https://auth.openai.com/log-in/password'):
        context = self.browser.new_context()
        context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html if route.request.url == url else '<html></html>'))
        page = context.new_page()
        page.goto(url, wait_until='load')
        page.add_script_tag(content=self.dom_source)
        self.addCleanup(context.close)
        return page

    def test_readonly_email_does_not_block_controlled_password_field(self):
        page = self.fixture('''<form><p>Enter your password</p><input type="email" value="owner@example.com" readonly><input id="password" type="password" required><button type="submit">Continue</button></form><script>
        window.directAssignments=0;window.submissions=0;
        const field=document.querySelector('#password');const descriptor=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
        Object.defineProperty(field,'value',{configurable:true,get(){return descriptor.get.call(this)},set(){window.directAssignments++}});
        document.querySelector('form').addEventListener('submit',event=>{event.preventDefault();window.submissions++});
        </script>''')
        self.assertEqual(page.evaluate('observeLogin()')['kind'], 'password')
        result = page.evaluate("async () => fillLogin('password','TEST-PASSWORD','owner@example.com')")
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(page.locator('#password').input_value(), 'TEST-PASSWORD')
        self.assertEqual(page.evaluate('window.directAssignments'), 0)
        self.assertEqual(page.evaluate('window.submissions'), 1)

    def test_totp_split_boxes_and_email_otp_are_distinguished(self):
        boxes = '<input maxlength="1" inputmode="numeric" required>' * 6
        page = self.fixture('<form><p>Two-factor security verification: code from your authenticator app</p>' + boxes + '<button type="submit">Verify</button></form><script>document.querySelector("form").onsubmit=e=>e.preventDefault()</script>')
        self.assertEqual(page.evaluate('observeLogin()')['kind'], 'totp')
        self.assertEqual(page.evaluate("async () => fillLogin('totp','654321','owner@example.com')")['state'], 'submitted')
        self.assertEqual(page.locator('input').evaluate_all("boxes=>boxes.map(box=>box.value).join('')"), '654321')
        mail = self.fixture('<p>Check your email. We sent you a code.</p><iframe style="display:none" src="https://challenges.cloudflare.com/test"></iframe><form><input autocomplete="one-time-code"><button type="submit">Continue</button></form>')
        self.assertEqual(mail.evaluate('observeLogin()')['kind'], 'email_code')

    def test_email_code_page_can_switch_to_password_login_for_totp(self):
        page = self.fixture('''<p>Check your email. We sent you a verification code.</p>
        <form><input autocomplete="one-time-code"><button type="submit">Continue</button></form>
        <button id="password-option" type="button">Use password instead</button>
        <script>document.querySelector('#password-option').onclick=()=>document.body.dataset.password='selected'</script>''')
        self.assertEqual(page.evaluate('observeLogin()')['kind'], 'password_choice')
        result = page.evaluate("async () => fillLogin('password_choice', 'password', 'owner@example.com')")
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(page.locator('body').get_attribute('data-password'), 'selected')

        methods = self.fixture('''<p>Check your email. We sent you a verification code.</p>
        <form><input autocomplete="one-time-code"></form>
        <button id="other-methods" type="button">Try another method</button>
        <script>document.querySelector('#other-methods').onclick=()=>document.body.dataset.method='other'</script>''')
        self.assertEqual(methods.evaluate('observeLogin()')['method'], 'other_methods')
        self.assertEqual(methods.evaluate("async () => fillLogin('password_choice','other_methods','owner@example.com')")['state'], 'submitted')
        self.assertEqual(methods.locator('body').get_attribute('data-method'), 'other')

        password = self.fixture('<button id="password" type="button">Password</button><script>document.querySelector("#password").onclick=()=>document.body.dataset.method="password"</script>')
        self.assertEqual(password.evaluate('observeLogin()')['method'], 'password')
        self.assertEqual(password.evaluate("async () => fillLogin('password_choice','password','owner@example.com')")['state'], 'submitted')
        self.assertEqual(password.locator('body').get_attribute('data-method'), 'password')

    def test_email_code_page_can_switch_to_password_login_for_totp(self):
        page = self.fixture('''<p>Check your email. We sent you a verification code.</p>
        <form><input autocomplete="one-time-code"><button type="submit">Continue</button></form>
        <button id="password-option" type="button">Use password instead</button>
        <script>document.querySelector('#password-option').onclick=()=>document.body.dataset.password='selected'</script>''')
        self.assertEqual(page.evaluate('observeLogin()')['kind'], 'password_choice')
        result = page.evaluate("async () => fillLogin('password_choice', '', 'owner@example.com')")
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(page.locator('body').get_attribute('data-password'), 'selected')

    def test_challenge_wrong_account_and_wrong_origin_prevent_form_submission(self):
        challenge = self.fixture('<p>Verify you are human</p><form><input type="email"><button>Continue</button></form>')
        self.assertEqual(challenge.evaluate('observeLogin()')['kind'], 'challenge')
        wrong = self.fixture('<form><input type="email" value="other@example.com" readonly><input type="password"><button>Continue</button></form>')
        self.assertEqual(wrong.evaluate("async () => fillLogin('password','TEST-PASSWORD','owner@example.com')")['state'], 'account_mismatch')
        self.assertEqual(wrong.locator('input[type=password]').input_value(), '')
        foreign = self.fixture('<form><input type="email"><button>Continue</button></form>', 'https://example.invalid/login')
        self.assertEqual(foreign.evaluate('observeLogin()')['kind'], 'none')
        self.assertEqual(foreign.evaluate("async () => fillLogin('email','owner@example.com','owner@example.com')")['state'], 'waiting')

    @unittest.skipIf((ROOT/'web'/'dist'/'index.html').exists(), 'Legacy combined-page UI replaced by test_inventory_browser')
    def test_workbench_can_view_copy_bind_preflight_and_explicitly_redeem(self):
        with TemporaryDirectory() as temporary:
            server = make_server(Path(temporary) / 'data')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            context = self.browser.new_context(viewport={'width': 1580, 'height': 1100})
            # Use a page-local fake clipboard; never overwrite the user's OS clipboard.
            context.add_init_script("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.testClipboard=text}}})")
            try:
                key = server.store.add_many([{'email': 'owner@example.com', 'code': 'LOCAL-PROBE-UI'}])[0]
                server.store.update(key, probe_only=True, card='Session 测试 / 无卡密', status='ready')
                server.engine.sessions[key] = session()
                provider = ProviderFixture()
                async def get_provider(_):
                    return provider
                server.engine.provider = get_provider
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto('http://' + server.address + '/#' + server.token, wait_until='networkidle')
                page.locator('#rows [data-row-action=session]').wait_for()
                page.locator('#rows [data-select]').check()
                self.assertTrue(page.locator('[data-action=confirm]').is_disabled())
                self.assertIn('绑定卡密', page.locator('#workflow-reason').inner_text())
                page.locator('#rows [data-row-action=session]').click()
                self.assertIn('TEST-COOKIE-ONLY', page.locator('#session-value').input_value())
                page.locator('#copy-session').click()
                page.locator('#session-copy-status').filter(has_text='已复制').wait_for()
                self.assertEqual(json.loads(page.evaluate('window.testClipboard'))['user']['email'], 'owner@example.com')
                page.locator('#session-dialog [data-close=session-dialog]').last.click()
                self.assertEqual(page.locator('#session-value').input_value(), '')
                page.locator('#rows [data-row-action=edit]').click()
                page.locator('#edit-account-card').fill('卡密：REAL-FORMAT-TEST-CARD\n兑换地址：https://cdk.bar/')
                page.locator('#edit-account-months').select_option('3')
                page.get_by_role('button', name='只保存资料', exact=True).click()
                page.locator('#rows [data-row-action=prepare]').wait_for()
                self.assertTrue(server.engine.sessions.get(key))
                self.assertFalse(server.store.get(key)['probe_only'])
                page.locator('#rows [data-row-action=prepare]').click()
                page.locator('#rows [data-row-action=confirm]').wait_for(timeout=8000)
                self.assertEqual(provider.redeems, 0)
                self.assertFalse(page.locator('[data-action=confirm]').is_disabled())
                page.locator('#rows [data-row-action=confirm]').click()
                self.assertIn('owner@example.com', page.locator('#confirm-content').inner_text())
                page.locator('#confirm-execute').click()
                page.locator('#rows .pill.completed').wait_for(timeout=8000)
                self.assertEqual(provider.redeems, 1)
                self.assertEqual(errors, [])
                artifacts = ROOT / 'output' / 'playwright'
                artifacts.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(artifacts / 'complete-flow.png'), full_page=True)
            finally:
                context.close()
                server.shutdown()
                thread.join(3)
                server.engine.close()
                server.server_close()
                server.store.db.close()
                server.instance_lock.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
