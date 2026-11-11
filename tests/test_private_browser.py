"""Opt-in offline Edge regression: set RUN_EDGE_TESTS=1 to run."""
import asyncio
import ctypes
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright
from workbench.engine import Engine
from workbench.private_browser import private_login_page


@unittest.skipUnless(os.name == 'nt' and os.environ.get('RUN_EDGE_TESTS') == '1', 'Opt-in Windows Edge test')
class PrivateBrowserTest(unittest.IsolatedAsyncioTestCase):
    async def test_visible_inprivate_session_and_fresh_login(self):
        async with async_playwright() as pw:
            async with private_login_page(pw) as (_, context, page):
                async def respond(route):
                    if route.request.url.endswith('/api/auth/session'):
                        await route.fulfill(content_type='application/json', body=json.dumps({
                            'user': {'email': 'owner@example.com'}, 'accessToken': 'offline-token',
                            'expires': '2099-01-01T00:00:00Z'}))
                    else:
                        await route.fulfill(content_type='text/html', headers={
                            'Set-Cookie': '__Secure-next-auth.session-token=offline-cookie; Secure; HttpOnly; Path=/'},
                            body='<title>Workbench Private Regression</title><h1>Offline login</h1>')
                await page.route('**/*', respond)
                await page.goto('https://chatgpt.com/')
                await asyncio.sleep(.5)
                titles = []
                callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                def enum(hwnd, extra):
                    if ctypes.windll.user32.IsWindowVisible(hwnd):
                        buf = ctypes.create_unicode_buffer(1024)
                        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 1024)
                        if 'Workbench Private Regression' in buf.value:
                            titles.append(buf.value)
                    return True
                ctypes.windll.user32.EnumWindows(callback_type(enum), 0)
                self.assertEqual(len(titles), 1)
                self.assertIn('InPrivate', titles[0])
                self.assertEqual(len(context.pages), 1)
                session = await Engine._try_authenticated_session(None, page, context, 'owner@example.com')
                self.assertEqual(session['sessionToken'], 'offline-cookie')
                self.assertEqual(page.url, 'https://chatgpt.com/')
            async with private_login_page(pw) as (_, context, page):
                await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='<h1>Fresh login</h1>'))
                await page.goto('https://chatgpt.com/')
                self.assertIsNone(await Engine._try_authenticated_session(None, page, context, 'owner@example.com'))
