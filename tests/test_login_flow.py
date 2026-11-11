"""Offline DOM regression tests. All browser requests are fulfilled locally."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright
from workbench.engine import Engine
from workbench.login_flow import login_step, login_page_error, wait_for_login_client
from workbench.login_diagnostics import LoginDiagnostics
from workbench.store import crypt


class LoginErrorsTest(unittest.TestCase):
    def test_diagnostics_store_only_allowlisted_metadata(self):
        diagnostics = LoginDiagnostics()
        request = SimpleNamespace(url='https://auth.openai.com/login/SECRET?password=PRIVATE',
            method='POST',resource_type='fetch',headers={'content-type':'application/json','cookie':'PRIVATE'})
        response = SimpleNamespace(request=request,status=400,headers={
            'content-type':'text/html; charset=UTF-8','set-cookie':'PRIVATE'})
        diagnostics.observe(response)
        summary = diagnostics.summary()
        self.assertIn('请求JSON→响应HTML HTTP400',summary)
        retained = json.dumps(list(diagnostics.responses)) + summary
        for secret in ('PRIVATE','SECRET','password','cookie','auth.openai.com'):
            self.assertNotIn(secret, retained)
        request.url='https://mail.example.com/PRIVATE'
        diagnostics.observe(response)
        self.assertEqual(len(diagnostics.responses),1)

    def test_route_error_and_bad_password_are_actionable_without_response_dump(self):
        self.assertIn('400', login_page_error('route error (400 invalid content type: text/html; charset=utf-8)'))
        self.assertIn('密码', login_page_error('incorrect email or password'))
        self.assertIn('频繁', login_page_error('too many attempts'))
        self.assertEqual(login_page_error('请输入密码'), '')


@unittest.skipUnless(os.name == 'nt' and os.environ.get('RUN_EDGE_TESTS') == '1', 'Opt-in offline Edge test')
class LoginFlowBrowserTest(unittest.IsolatedAsyncioTestCase):
    async def test_unhydrated_client_form_stops_without_submitting(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<form><input type="password"></form><script type="module">window.__reactRouterContext={}</script>')
                with self.assertRaisesRegex(ValueError,'脚本尚未就绪'):
                    await wait_for_login_client(page,page.locator('input'),timeout=.4)
            finally:
                await browser.close()

    async def test_waits_for_client_form_handler_before_password_submission(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = await browser.new_page()
                submissions = []
                async def respond(route):
                    path = urlsplit(route.request.url).path
                    if path == '/log-in/password':
                        # SSR fields appear before the client handler is attached.
                        await route.fulfill(content_type='text/html', body='''
                            <input type="email" readonly value="owner@example.com">
                            <form method="post" action="/native-submit">
                              <input type="password" name="password"><button type="submit">继续</button>
                            </form>
                            <script type="module">
                              window.__reactRouterContext = {};
                              setTimeout(() => {
                                const form = document.querySelector('form');
                                const submit = event => {
                                  event.preventDefault();
                                  fetch('/client-submit', {method:'POST', headers:{'Content-Type':'application/json'},
                                    body:JSON.stringify({password:form.elements.password.value})});
                                };
                                form['__reactProps$offline'] = {onSubmit:submit};
                                form.addEventListener('submit',submit);
                              }, 1800);
                            </script>''')
                    else:
                        submissions.append((path, route.request.headers.get('content-type','')))
                        await route.fulfill(content_type='application/json', body='{}')
                await page.route('**/*', respond)
                await page.goto('https://auth.openai.com/log-in/password', wait_until='domcontentloaded')
                engine = Engine.__new__(Engine)
                await engine._submit_login_step(page, 'password', 'offline-password')
                await page.wait_for_timeout(300)
                self.assertEqual(submissions, [('/client-submit', 'application/json')])
            finally:
                await browser.close()

    async def test_readonly_email_password_otp_session_and_no_duplicate_password_submit(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='msedge', headless=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                submissions = []
                # The first submit is outside the login form. The password page
                # keeps a visible readonly email, as in the reported screenshot.
                def form(kind, action):
                    readonly_email = '<input type="email" readonly value="owner@example.com">'
                    hidden_email = '<input type="email" style="display:none" value="hidden@example.com">'
                    if kind == 'email':
                        field = '<input type="email" name="email">'
                        text = 'Sign in'
                    elif kind == 'password':
                        field = readonly_email + hidden_email + '<input type="password" name="password">'
                        text = '输入密码'
                    else:
                        field = readonly_email + '<input type="password" style="display:none"><input autocomplete="one-time-code" name="code">'
                        text = 'Check your email. We sent you a code.' if kind == 'email-code' else 'Enter the code from your authenticator app.'
                    return ('<meta charset="utf-8"><title>Offline login regression</title><button type="submit" style="display:none">Wrong submit</button>'
                            '<p>' + text + '</p><form method="post" action="' + action + '">'
                            + field + '<button type="submit" style="display:none">Hidden continue</button>'
                            '<button type="submit" formaction="/wrong-action">Use a code instead</button>'
                            '<button type="submit">继续</button></form>')

                async def respond(route):
                    request = route.request
                    path = urlsplit(request.url).path
                    if request.method == 'POST':
                        submissions.append((path, parse_qs(request.post_data or '')))
                        self.assertIn('application/x-www-form-urlencoded', request.headers.get('content-type', ''))
                    if path == '/auth/login':
                        body = form('email', 'https://auth.openai.com/log-in/password')
                    elif path == '/log-in/password':
                        body = form('password', '/email-code')
                    elif path == '/email-code':
                        # Keep the password page unchanged for longer than one
                        # polling interval, emulating a slow server transition.
                        body = form('password', '/email-code') + '<script>setTimeout(()=>location.href="/email-code-ready",2200)</script>'
                    elif path == '/email-code-ready':
                        body = form('email-code', '/totp')
                    elif path == '/totp':
                        body = form('totp', 'https://chatgpt.com/')
                    elif path == '/api/auth/session':
                        await route.fulfill(content_type='application/json', body=json.dumps({
                            'user': {'email': 'owner@example.com'}, 'accessToken': 'offline-access',
                            'expires': '2099-01-01T00:00:00Z'}))
                        return
                    else:
                        await route.fulfill(content_type='text/html', body='<h1>Offline home</h1>', headers={
                            'Set-Cookie': '__Secure-next-auth.session-token=offline-session; Secure; HttpOnly; Path=/'})
                        return
                    await route.fulfill(content_type='text/html', body=body)

                await page.route('**/*', respond)
                updates = []
                row = {'email': 'owner@example.com', 'password': crypt('offline-password'),
                       'totp': crypt('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'),
                       'mail_url': crypt('https://mail.example.com/offline')}
                engine = Engine.__new__(Engine)
                engine.store = SimpleNamespace(get=lambda _: row, update=lambda _, **fields: updates.append(fields))
                engine.sessions = {}; engine.tickets = {}; engine.login_browsers = {}; engine.login_contexts = {}
                engine.pw = pw
                engine.ensure_browser = AsyncMock()
                @asynccontextmanager
                async def offline_page(_):
                    yield browser, context, page
                with patch('workbench.engine.private_login_page', offline_page), patch('workbench.engine.fetch_mail_code', return_value='123456') as mail, patch('workbench.engine.totp_code', return_value='654321'):
                    await asyncio.wait_for(engine.login('offline'), timeout=45)
                self.assertEqual(submissions, [
                    ('/log-in/password', {'email': ['owner@example.com']}),
                    ('/email-code', {'password': ['offline-password']}),
                    ('/totp', {'code': ['123456']}),
                    ('/', {'code': ['654321']})])
                self.assertEqual(engine.sessions['offline']['sessionToken'], 'offline-session')
                self.assertEqual(updates[-1]['status'], 'ready')
                mail.assert_called_once()
            finally:
                await browser.close()

    async def test_visible_readonly_disabled_and_hidden_inputs_do_not_mask_password(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<input type="email" readonly><input autocomplete="username" disabled>'
                    '<input type="email" hidden><input type="password" hidden><input type="password">')
                self.assertEqual(await login_step(page), 'password')
                await page.set_content('<input type="email" readonly><input type="password" readonly>'
                    '<input name="code" disabled><input autocomplete="one-time-code">')
                self.assertEqual(await login_step(page), 'code')
            finally:
                await browser.close()
