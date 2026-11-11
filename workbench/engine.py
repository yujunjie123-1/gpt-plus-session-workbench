import asyncio
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from playwright.async_api import async_playwright
from .store import crypt, now
from .private_browser import private_login_page
from .login_flow import editable_login_field, login_step, login_page_error, wait_for_login_client
from .login_diagnostics import LoginDiagnostics
from .auth import fetch_mail_code, totp_code
from .provider import PartnerAPIError, PartnerConfigurationError, Provider, blocking_reason, lookup_state, session_rejected, plus_qualification
from .products import PRODUCTS
from .edge_session import EdgeSessionMixin, validate_handoff
from .account_flow import AccountFlowMixin
from .inventory_flow import InventoryFlowMixin
from .account_policy import ProviderRiskError, payment_risk_code
from .session_lifecycle import SessionLifecycleMixin, SessionRejectedError, local_facts, fingerprint


def session_cookie(cookies):
    for base in ['__Secure-next-auth.session-token', '__Secure-authjs.session-token', 'next-auth.session-token', 'authjs.session-token']:
        eligible = [c for c in cookies if c.get('domain', '').lstrip('.') == 'chatgpt.com' and c.get('name', '').startswith(base)]
        direct = next((c['value'] for c in eligible if c['name'] == base), '')
        if direct:
            return direct
        pieces = [(int(c['name'][len(base)+1:]), c['value']) for c in eligible
                  if re.fullmatch(re.escape(base) + r'\.\d+', c['name'])]
        if pieces:
            pieces.sort()
            if [p[0] for p in pieces] == list(range(len(pieces))):
                return ''.join(p[1] for p in pieces)
    return ''


def check_identity(session, email):
    facts=local_facts(session,email)
    if facts['state'] in ('invalid','expired'):
        raise ValueError(facts.get('problem') or '必要登录凭证已过期，请重新登录')


class Engine(SessionLifecycleMixin, EdgeSessionMixin, AccountFlowMixin, InventoryFlowMixin):
    def __init__(self, store):
        self.store = store
        self.loop = asyncio.new_event_loop()
        self.jobs = {}
        self.job_actions = {}
        self.sessions = {}
        self.session_checks = {}
        self.session_clock = time.time
        self.restore_session_credentials()
        self.session_refresh_sources = {}
        self.session_auto_enabled = self.store.db.execute("SELECT value FROM inventory_meta WHERE key='session-auto-refresh'").fetchone() == ('true',)
        self.session_maintenance_error = ''
        self.provider_device = self._provider_device_id()
        self.provider_gate = None
        self.provider_client = None
        self.provider_poll_after = {}
        self.login_gate = None
        self.edge_requests = {}
        self.edge_origin = None
        self.tickets = {}
        self.providers = {}
        self.direct_quotes = {}
        self.direct_jobs = {}
        self.direct_poll_after = {}
        self.limit = self._saved_concurrency('recharge-concurrency',1)
        self.session_limit = min(self._saved_concurrency('session-concurrency',5),5)
        self.paused = False
        self.browser = None
        self.browser_start_lock = asyncio.Lock()
        self.login_browsers = {}
        self.login_contexts = {}
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._thread, daemon=True)
        self.thread.start()
        self.ready.wait(5)

    def _thread(self):
        asyncio.set_event_loop(self.loop)
        self.maintenance_task=self.loop.create_task(self._session_maintenance())
        self.edge_page_watchdog=self.loop.create_task(self.watch_edge_pages())
        self.partner_transaction_watchdog=self.loop.create_task(self.watch_partner_transactions())
        self.ready.set()
        self.loop.run_forever()

    async def ensure_browser(self):
        async with self.browser_start_lock:
            if not self.browser or not self.browser.is_connected():
                self.pw = await async_playwright().start()
                self.browser = await self.pw.chromium.launch(channel='msedge', headless=True)
        return self.browser

    async def provider(self, key):
        if self.provider_client is None:
            if self.provider_gate is None:
                self.provider_gate = asyncio.Semaphore(2)
            self.provider_client = Provider(self.provider_device, gate=self.provider_gate)
        return self.provider_client

    def command(self, action, ids, limit=1):
        return asyncio.run_coroutine_threadsafe(self._command(action, ids, limit), self.loop).result(timeout=10)

    def _saved_concurrency(self, name, default):
        with self.store.lock:
            saved=self.store.db.execute('SELECT value FROM inventory_meta WHERE key=?',(name,)).fetchone()
        try:return max(1,min(int(saved[0]),10)) if saved else default
        except (ValueError,TypeError):return default

    def _provider_device_id(self):
        with self.store.lock:
            saved = self.store.db.execute("SELECT value FROM inventory_meta WHERE key='partner-device'").fetchone()
            if saved and isinstance(saved[0], str) and re.fullmatch(r'[A-Za-z0-9_-]{16,128}', saved[0]):
                return saved[0]
            value = 'device-' + uuid.uuid4().hex
            with self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO inventory_meta VALUES (?,?)', ('partner-device', value))
            return value

    async def _command(self, action, ids, limit):
        if action in ['pause', 'resume']:
            self.paused = action == 'pause'
            return {'ok': True}
        if action in ('limit','session-limit'):
            value=max(1,min(int(limit),5 if action=='session-limit' else 10))
            name='session-concurrency' if action=='session-limit' else 'recharge-concurrency'
            with self.store.lock,self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO inventory_meta VALUES (?,?)',(name,str(value)))
            if action=='session-limit':self.session_limit=value
            else:self.limit=value
            return {'ok': True}
        if action not in ['login', 'edge_login', 'edge_recover', 'session_only', 'session_refresh', 'redeem_auto', 'prepare', 'auto_batch', 'batch', 'preflight', 'confirm', 'refresh', 'retry', 'stop', 'delete']:
            raise ValueError('未知操作')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
            raise ValueError('请先选择记录')
        ids = list(dict.fromkeys(ids))
        rows = [self.store.get(key) for key in ids]
        # Validate the complete selection before mutating anything.
        for row in rows:
            key = row['id']
            if action=='session_refresh':
                reason=self.session_refresh_reason(key)
                if reason:
                    raise ValueError(reason)
            if action in ['redeem_auto','prepare','auto_batch','batch','preflight','confirm','retry']:
                self.store.ensure_recharge_allowed(key)
            active = key in self.jobs and not self.jobs[key].done()
            if row.get('probe_only') and action not in ['edge_login', 'edge_recover', 'session_only', 'session_refresh', 'stop', 'delete']:
                raise ValueError('尚未绑定卡密，请点击该账号的“补全资料 / 绑定卡密”')
            if action not in ['stop'] and active:
                raise ValueError('选中记录仍有任务运行，请等待或先停止')
            if action in ['login', 'edge_login', 'edge_recover', 'session_only', 'session_refresh', 'redeem_auto', 'prepare', 'auto_batch', 'batch', 'preflight', 'retry', 'delete'] and row.get('submitted'):
                raise ValueError('选中记录已提交兑换，请仅查询原卡密状态')
            if action == 'confirm' and (row['status'] != 'confirm' or key not in self.tickets):
                raise ValueError('选中记录须全部通过预检后才能确认兑换')
            if action == 'retry' and not row.get('can_retry'):
                raise ValueError('选中记录尚未确认释放，不可重试')
        for key in ids:
            if action == 'stop':
                if key in self.jobs:
                    self.jobs[key].cancel()
                row = self.store.get(key)
                self.store.update(key, status='pending' if row['submitted'] else 'stopped',
                                  message='已停止本地任务；已提交的远端兑换不会撤销' if row['submitted'] else '已停止本地任务')
            elif action == 'delete':
                if key in self.providers:
                    await self.providers.pop(key).close()
                self.store.delete(key)
                self.forget_session_memory(key)
            else:
                if action=='session_refresh':
                    self.store.inventory_session_slot(key)
                self.store.update(key, status='queued', message='任务已加入队列')
                self.job_actions[key]=action
                self.jobs[key] = asyncio.create_task(self.run(key, action))
        return {'ok': True}

    async def run(self, key, action):
        try:
            while self.paused or sum(1 for k,t in self.jobs.items() if k != key and not t.done()
                    and (self.job_actions.get(k) in ('session_only','session_refresh'))==(action in ('session_only','session_refresh'))
                    and self.store.get(k)['status'] not in ['queued']) >= (self.session_limit if action in ('session_only','session_refresh') else self.limit) or (action == 'edge_recover' and any(not item.get('instance_id') for item in self.edge_requests.values())):
                if action=='session_refresh' and self.session_refresh_reason(key,include_busy=False):
                    self.store.update(key,status='idle',message='已跳过更新：账号已不符合条件')
                    return
                await asyncio.sleep(.25)
            if action=='session_refresh':
                reason=self.session_refresh_reason(key,include_busy=False)
                if reason:
                    self.store.update(key,status='idle',message='已跳过更新：'+reason)
                    return
            self.store.update(key, status='running')
            row = self.store.get(key)
            if action=='session_refresh':
                await self._run_session_refresh(key)
                return
            if action == 'session_only':
                session = self.sessions.get(key)
                if session:
                    try:
                        validate_handoff(session, row['email'])
                    except ValueError:
                        self.clear_session(key)
                        session = None
                if not session:
                    await self.edge_login(key)
                else:
                    ticket = self.tickets.get(key)
                    current = bool(ticket and time.monotonic() - ticket['timestamp'] <= 300)
                    if not current:
                        self.tickets.pop(key, None)
                    self.store.update(key, status='confirm' if current else 'ready',
                                      message='Session 已就绪，可查看和复制；本次仅获取 Session，未执行卡密预检或兑换')
                return
            if action == 'edge_login':
                await self.edge_login(key)
                return
            if action == 'edge_recover':
                await self.edge_login(key, open_window=False, automatic=False)
                return
            if action in ['prepare', 'auto_batch', 'redeem_auto']:
                await self.ensure_charge_session(key)
                await self.preflight(key)
                if action == 'redeem_auto' and self.store.get(key)['status'] == 'confirm':
                    await self.redeem(key)
                return
            if action == 'refresh':
                await self.refresh(key)
            elif action == 'confirm':
                await self.redeem(key)
            else:
                if action == 'retry':
                    # Always query again immediately before allowing a fresh preview.
                    remote = await (await self.provider(key)).lookup(crypt(row['secret'], True))
                    if remote.get('can_resubmit') is not True or remote.get('status') not in ['failed', 'unused']:
                        await self.apply_lookup(key, remote)
                        return
                if action in ['batch', 'preflight', 'retry'] and key not in self.sessions:
                    raise ValueError('请先获取 Session，或使用一键充值自动登录后完成预检')
                if action == 'login':
                    await self.login(key)
                if action in ['batch', 'preflight', 'retry']:
                    await self.preflight(key)
        except asyncio.CancelledError:
            self.tickets.pop(key, None)
            if action!='session_refresh' or self.store.get(key)['status'] not in ('queued','idle'):
                self.clear_session(key)
            if action=='session_refresh' and self.store.get(key)['status'] in ('queued','running','login'):
                self.store.update(key,status='idle',message='自动更新已停止；需要时可手动点击更新重试')
        except Exception as exc:
            if isinstance(exc, ProviderRiskError):
                self.store.quarantine_account(key,exc.risk_code)
            row = self.store.get(key)
            safe = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else '浏览器或网络连接失败；请检查浏览器窗口、网络和接口部署后重试'
            fields = dict(status='pending' if row['submitted'] else 'error', message=safe[:180])
            if row.get('precheck') and not row['submitted'] and action in ['prepare', 'auto_batch', 'redeem_auto', 'preflight', 'retry']:
                fields['precheck'] = dict(row['precheck'], stage='error', message=safe[:180])
            self.store.update(key, **fields)

    async def _submit_login_step(self, page, kind, value):
        field = await editable_login_field(page, kind)
        if field is None:
            return False
        await wait_for_login_client(page, field)
        if kind == 'code':
            boxes = page.locator('input[maxlength="1"]')
            count = await boxes.count()
            if 4 <= count <= 10:
                visible_boxes = []
                for index in range(count):
                    candidate = boxes.nth(index)
                    if await candidate.is_visible():
                        visible_boxes.append(candidate)
                if len(visible_boxes) == len(value):
                    for candidate, digit in zip(visible_boxes, value):
                        await candidate.fill(digit, timeout=5000)
                    field = visible_boxes[-1]
                else:
                    await field.fill(value, timeout=5000)
            else:
                await field.fill(value, timeout=5000)
        else:
            await field.fill(value, timeout=5000)
        form = field.locator('xpath=ancestor::form[1]')
        scope = form if await form.count() else page
        submit = None
        for candidates in (scope.get_by_role('button', name=re.compile(r'^(continue|next|verify|sign in|log in|继续|下一步|验证|登录)$', re.I)),
                           scope.locator('button[type="submit"]:not([formaction]), input[type="submit"]:not([formaction])')):
            for index in range(min(await candidates.count(), 12)):
                candidate = candidates.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    submit = candidate
                    break
            if submit is not None:
                break
        if submit is not None:
            await submit.click(timeout=5000)
        else:
            await field.press('Enter', timeout=5000)
        await page.wait_for_timeout(900)
        return True

    async def _challenge_text(self, page):
        try:
            return (await page.locator('body').inner_text(timeout=1200)).lower()[:12000]
        except Exception:
            return ''

    async def _try_authenticated_session(self, page, context, email):
        current = page.url
        if not current.startswith('https://chatgpt.com/') or '/auth/login' in current:
            return None
        # CDP's default context can refer to the regular profile even when its
        # startup target is InPrivate. Read cookies from this page's target.
        client = await context.new_cdp_session(page)
        try:
            cookies = await client.send('Network.getCookies', {'urls': ['https://chatgpt.com/']})
        finally:
            await client.detach()
        cookie = session_cookie(cookies.get('cookies', []))
        if not cookie:
            return None
        try:
            value = await page.evaluate('''async () => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 10000);
                try {
                    const response = await fetch('/api/auth/session', {
                        credentials: 'same-origin', cache: 'no-store', signal: controller.signal
                    });
                    return response.ok ? await response.json() : null;
                } finally { clearTimeout(timer); }
            }''')
        except Exception:
            return None
        if not isinstance(value, dict) or not value.get('user', {}).get('email'):
            return None
        value['sessionToken'] = cookie
        check_identity(value, email)
        if len(json.dumps(value, ensure_ascii=False)) > 60000:
            raise ValueError('Session 超出兑换组件允许的大小')
        return value

    async def login(self, key):
        row = self.store.get(key)
        self.clear_session(key)
        if not row.get('password') or not row.get('totp') or not row.get('mail_url'):
            raise ValueError('自动登录需要完整的邮箱、GPT 密码、2FA 密钥和邮箱取码链接；请重新添加资料')
        self.store.update(key, status='login', message='正在启动隔离的 InPrivate 浏览器并填写邮箱')
        await self.ensure_browser()
        password = crypt(row['password'], True)
        seed = crypt(row['totp'], True)
        mail_url = crypt(row['mail_url'], True)
        submitted_steps = set()
        code_poll_end = 0.0
        email_submitted = set()
        totp_submitted = set()
        deadline = time.monotonic() + 600
        async with private_login_page(self.pw) as (browser, context, page):
            self.login_browsers[key] = browser
            self.login_contexts[key] = context
            diagnostics = LoginDiagnostics()
            page.on('response', diagnostics.observe)
            try:
                await page.goto('https://chatgpt.com/auth/login', wait_until='domcontentloaded', timeout=60000)
                while time.monotonic() < deadline:
                    if not context.pages:
                        raise ValueError('登录窗口已关闭，可重新点击登录')
                    body = await self._challenge_text(page)
                    if not body:
                        await page.wait_for_timeout(1000)
                        continue
                    error = login_page_error(body)
                    if error:
                        raise ValueError(error + '；' + diagnostics.summary())
                    if any(marker in body for marker in ['captcha', 'verify you are human', 'prove you are human', 'security challenge', '人机验证']):
                        self.store.update(key, status='login', message='登录遇到人机或安全验证，请在 InPrivate 窗口手动完成；程序随后继续')
                        await page.wait_for_timeout(1500)
                        continue
                    session = await self._try_authenticated_session(page, context, row['email'])
                    if session:
                        self.remember_session(key,session)
                        self.store.update(key, status='ready', message='已自动登录、读取并核对 Session；可开始兑换预检')
                        return
                    try:
                        step = await login_step(page)
                        # A redirect can retain the same password form at a new
                        # URL. Credentials are submitted once per login attempt.
                        attempt = step
                        if step in ('email', 'password') and attempt in submitted_steps:
                            await page.wait_for_timeout(1000)
                            continue
                        if step == 'email':
                            submitted_steps.add(attempt)
                            if await self._submit_login_step(page, 'email', row['email']):
                                self.store.update(key, status='login', message='邮箱已填写，正在等待密码步骤')
                            else:
                                submitted_steps.discard(attempt)
                            continue
                        if step == 'password':
                            submitted_steps.add(attempt)
                            if await self._submit_login_step(page, 'password', password):
                                self.store.update(key, status='login', message='密码已填写，正在检查邮箱验证码或 2FA 步骤')
                            else:
                                submitted_steps.discard(attempt)
                            continue
                        if step == 'code':
                            is_totp = any(marker in body for marker in ['authenticator', 'authentication app', 'two-factor', 'two factor', '2fa', '验证器', '身份验证应用'])
                            is_email = any(marker in body for marker in ['check your email', 'email verification', 'sent you a code', 'code we sent', '收件箱', '邮件验证码', '已发送验证码'])
                            if is_totp:
                                current_code = totp_code(seed)
                                if current_code not in totp_submitted:
                                    if await self._submit_login_step(page, 'code', current_code):
                                        totp_submitted.add(current_code)
                                        self.store.update(key, status='login', message='已自动填写 2FA 动态验证码，正在等待登录结果')
                                        continue
                            elif is_email:
                                if time.monotonic() < code_poll_end or not code_poll_end:
                                    if not code_poll_end:
                                        code_poll_end = time.monotonic() + 180
                                        self.store.update(key, status='login', message='检测到邮箱验证码步骤，正在实时读取取码接口')
                                    try:
                                        code = await asyncio.to_thread(fetch_mail_code, mail_url)
                                    except RuntimeError as exc:
                                        self.store.update(key, status='login', message=str(exc))
                                        await page.wait_for_timeout(1800)
                                        continue
                                    if code not in email_submitted and await self._submit_login_step(page, 'code', code):
                                        email_submitted.add(code)
                                        self.store.update(key, status='login', message='已自动填写邮箱验证码，正在等待 2FA 或登录结果')
                                        continue
                                    await page.wait_for_timeout(1800)
                                    continue
                                raise ValueError('等待邮箱验证码超过三分钟，请检查取码接口后重新登录')
                            else:
                                self.store.update(key, status='login', message='检测到登录验证码步骤，但无法判断邮箱码或 2FA；请在 InPrivate 窗口完成，程序会继续')
                        await page.wait_for_timeout(900)
                    except ValueError:
                        raise
                    except Exception:
                        self.store.update(key, status='login', message='登录页面暂未完成操作，等待页面响应；可在 InPrivate 窗口检查或手动继续')
                        await page.wait_for_timeout(900)
                raise ValueError('自动登录等待超时，请检查 InPrivate 窗口后重新开始')
            finally:
                page.remove_listener('response', diagnostics.observe)
                self.login_browsers.pop(key, None)
                self.login_contexts.pop(key, None)

    async def apply_lookup(self, key, remote):
        state = lookup_state(remote)
        row = self.store.get(key)
        account = str(remote.get('account_email', '')).strip()
        risk=payment_risk_code(remote) if state=='failed' and row['submitted'] and (not account or account.casefold()==row['email'].casefold()) else None
        if risk:
            self.store.quarantine_account(key,risk)
            row=self.store.get(key)
        if state == 'completed' and not row.get('submitted'):
            precheck=dict(row.get('precheck') or {})
            precheck['card_status']='completed'
            self.store.update(key, status='blocked', message='上游确认卡密已使用，但本地未提交此兑换；当前账号未记为充值成功',
                              can_retry=False, result_state='completed', provider_result_email=account or '上游未提供',
                              remote_terminal=True, precheck=precheck)
            return 'blocked'
        released = remote.get('can_resubmit') is True and state in ['failed', 'unused']
        # A post-submit unused result alone never authorizes a second submission.
        if row['submitted'] and not (state == 'failed' and released) and state not in ['completed', 'blocked']:
            state = 'pending'
            released = False
        message = {'completed':'服务端查询确认卡密兑换成功', 'pending':'兑换处理中或待核验，请勿重复提交',
                   'failed':'本次兑换失败，已释放可重试' if released else '本次兑换失败，尚未确认释放',
                   'blocked':'卡密已禁用或过期', 'unused':'卡密未使用', 'unknown':'服务端状态未知，请稍后查询'}[state]
        if row.get('risk',{}).get('active') and state!='completed':
            message=row['risk']['reason']+'，已移入风控池；'+('卡密已确认释放，账号暂缓充值' if released else '原卡密结果仍需核实，仅可查询')
        fields = dict(status=state, can_retry=released, message=message,
                      result_state=state, attribution_pending=False,
                      remote_terminal=(state in ('completed', 'blocked') or state == 'failed' and not released))
        if row.get('precheck'):
            fields['precheck'] = dict(row['precheck'], card_status=state)
        if state == 'completed':
            fields['result_email'] = row['email']
            fields['provider_result_email'] = account or '上游未提供'
            fields['current_plan'] = 'plus'
            if not account:
                fields['message'] = '上游确认兑换成功，已按本次预检绑定邮箱自动记账；上游未提供结果邮箱'
            elif account.casefold() != row['email'].casefold():
                fields['message'] = '上游确认兑换成功，已按本次预检绑定邮箱自动记账；上游返回邮箱已保留供查账'
        if state == 'failed' and released:
            fields['submitted'] = False
        self.store.update(key, **fields)
        if state == 'completed':
            self.forget_session_memory(key)
        if state in ('completed', 'blocked', 'failed'):
            self.store.inventory_update_task(key, partner_redemption_token='', partner_preflight_token='')
        return state

    async def refresh(self, key):
        row = self.store.get(key)
        provider = await self.provider(key)
        code = crypt(row['secret'], True)
        task = self.store.charge_tasks.get(row.get('active_task_id'))
        token = crypt(task.get('partner_redemption_token', ''), True) if task and task.get('partner_redemption_token') else ''
        if row.get('submitted'):
            if task and task.get('remote_terminal'):
                return task.get('result_state') or task.get('status')
            if task and task.get('submission_rejected'):
                return await self._refresh_rejected_submission(key, provider, code)
            remote = await (provider.result(token) if token else provider.result_by_code(code))
            remote_status = str(remote.get('status', '')).lower()
            if remote_status == 'completed':
                account_email = str(remote.get('account_email', '')).strip()
                remote = {'status': 'completed', 'used': True, 'account_email': account_email}
                self.provider_poll_after[key] = time.monotonic() + 3
            elif remote_status in ('declined', 'failed_precharge', 'cancelled', 'failed'):
                lookup = await provider.lookup(code)
                if lookup.get('used') is True and str(lookup.get('account_email', '')).strip():
                    remote = {'status': 'completed', 'used': True,
                              'account_email': str(lookup.get('account_email', '')).strip()}
                elif lookup.get('can_resubmit') is True:
                    # Keep only the structured classification fields from the
                    # exact-card lookup so explicit payment failures still
                    # reach the risk policy without persisting provider text.
                    remote = {field: lookup[field] for field in
                              ('status', 'can_resubmit', 'error_code', 'decline_code',
                               'failure_reason', 'failure_message') if field in lookup}
                    remote.update(status='failed', can_resubmit=True)
                else:
                    # Route terminal failures through the same structured
                    # lookup policy so explicit payment failures are
                    # quarantined without releasing an unconfirmed card.
                    remote = dict(lookup)
                    remote.update(status='failed', can_resubmit=False)
                    return await self.apply_lookup(key, remote)
            elif remote_status in ('queued', 'processing', 'pending', 'accepted'):
                message = '兑换已受理，后台每 3 秒查询原交易；不会重复提交'
                current = self.store.get(key)
                if current.get('status') != 'pending' or current.get('message') != message:
                    self.store.update(key, status='pending', message=message)
                self.provider_poll_after[key] = time.monotonic() + 3
                return 'pending'
            else:
                self.provider_poll_after[key] = time.monotonic() + 3
                current = self.store.get(key)
                message = '上游暂未提供可判定的结果，后台将继续查询原交易'
                if current.get('status') != 'pending' or current.get('message') != message:
                    self.store.update(key, status='pending', message=message)
                return 'pending'
        else:
            remote = await provider.lookup(code)
        state = await self.apply_lookup(key, remote)
        if row.get('submitted') and state == 'failed' and self.store.get(key).get('can_retry'):
            task = self.store.charge_tasks.get(row.get('active_task_id'))
            if task:
                self.store.inventory_update_task(key, client_request_id='', partner_redemption_token='',
                                                 partner_preflight_token='')
        return state

    async def _refresh_rejected_submission(self, key, provider, code):
        try:
            remote = await provider.lookup(code)
        except Exception:
            self.store.update(key, status='pending',
                              message='Partner 已拒绝提交，卡密状态暂不可核实；后台仅查询原卡密，不会重复提交')
            self.provider_poll_after[key] = time.monotonic() + 3
            return 'pending'

        state = lookup_state(remote)
        if state == 'completed':
            self.store.update(key, submitted=False)
            result = await self.apply_lookup(key, remote)
            self.store.inventory_update_task(key, submission_rejected=False)
            return result

        can_resubmit = remote.get('can_resubmit') is True
        if state == 'blocked' or state in ('unused', 'failed') and not can_resubmit:
            self.store.update(key, submitted=False)
            if state == 'unused':
                remote = dict(remote, status='failed', can_resubmit=False)
            result = await self.apply_lookup(key, remote)
            self.store.inventory_update_task(key, submission_rejected=False)
            return result

        if state in ('unused', 'failed') and can_resubmit:
            self.store.update(key, submitted=False)
            result = await self.apply_lookup(key, dict(remote, status='failed', can_resubmit=True))
            self.store.inventory_update_task(key, client_request_id='', partner_redemption_token='',
                                             partner_preflight_token='', submission_rejected=False)
            return result

        self.store.update(key, status='pending',
                          message='Partner 已拒绝提交，但卡密状态仍未明确；后台只查询原卡密，不会重复提交')
        self.provider_poll_after[key] = time.monotonic() + 3
        return 'pending'

    async def watch_partner_transactions(self):
        while True:
            configured = bool(os.environ.get('CDK_PARTNER_PASSWORD'))
            if configured:
                stamp = time.monotonic()
                await self._watch_session_direct_tasks()
                for task in list(self.store.charge_tasks.values()):
                    if task.get('source') == 'session_extract':
                        continue
                    if (not task.get('submitted') or task.get('remote_terminal') or task.get('attribution_pending')
                            or task.get('status') == 'completed'):
                        continue
                    key = task.get('account_id')
                    if key not in self.store.rows or stamp < self.provider_poll_after.get(key, 0):
                        continue
                    running = self.jobs.get(key)
                    if running and not running.done():
                        continue
                    self.provider_poll_after[key] = stamp + 3
                    self.job_actions[key] = 'refresh'
                    self.jobs[key] = asyncio.create_task(self.run(key, 'refresh'))
            await asyncio.sleep(.5)

    async def preflight(self, key):
        for attempt in range(2):
            await self.ensure_charge_session(key)
            try:
                return await self._preflight_once(key)
            except SessionRejectedError:
                self.invalidate_session(key)
                if attempt:
                    raise
        raise SessionRejectedError()

    async def _preflight_once(self, key):
        self.store.ensure_recharge_allowed(key)
        row = self.store.get(key)
        session = self.sessions.get(key)
        if not session:
            raise ValueError('请先登录获取 Session')
        check_identity(session, row['email'])
        self.tickets.pop(key, None)
        summary = dict(stage='card', checked_at=now(), card_status='正在查询',
                       message='正在查询卡密状态')
        self.store.update(key, status='preflight', precheck=dict(summary),
                          message='正在核验卡密、账号身份和当前套餐')
        p = await self.provider(key)
        code = crypt(row['secret'], True)
        remote = await p.lookup(code)
        state = lookup_state(remote)
        summary.update(card_status=state, stage='qualification', message='正在查询卡密对应的套餐资格')
        self.store.update(key, precheck=dict(summary))
        if state in ['completed', 'pending', 'blocked'] or state == 'failed' and remote.get('can_resubmit') is not True:
            await self.apply_lookup(key, remote)
            summary.update(stage='blocked', message=self.store.get(key)['message'])
            self.store.update(key, precheck=dict(summary))
            return
        if state not in ['unused', 'failed']:
            raise ValueError('卡密状态未知，未提交兑换')
        preview = await p.call('preview', {'code': code})
        task=self.store.charge_tasks.get(row.get('active_task_id'))
        if row.get('execution_mode')=='recharge' and not task:
            raise ValueError('缺少充值任务规格，未提交兑换，请重新确认分配')
        expected_sku=task['sku'] if task and row.get('execution_mode')=='recharge' else 'plus'
        plan, token = plus_qualification(preview,expected_sku)
        target_label=PRODUCTS[plan]['label']
        summary.update(stage='account', target_plan=target_label, card_product=plan,
                       message='卡密资格已通过，正在校验 Session 和账号订阅')
        self.store.update(key, precheck=dict(summary), plan=target_label)
        account = await p.call('preflight', dict(code=code, redemption_token=token,
                         credential=dict(mode='session', session=json.dumps(session, ensure_ascii=False))))
        if session_rejected(account):
            raise SessionRejectedError()
        email = str(account.get('email', account.get('account_email', ''))).strip()
        risk=payment_risk_code(account) if not email or email.casefold()==row['email'].casefold() else None
        if risk:
            raise ProviderRiskError(risk)
        if email.casefold() != row['email'].casefold():
            raise ValueError('预检返回邮箱与任务邮箱不一致或缺失，已阻止兑换')
        current = str(account.get('currentPlan', account.get('current_plan', '上游未提供')))
        active = account.get('subscription_has_active')
        subscription = '有效' if active is True else '未订阅/已到期' if active is False else '上游未提供'
        expiry = str(account.get('subscription_active_until') or '上游未提供')
        renewal = account.get('subscription_will_renew')
        summary.update(email=email, current_plan=current, subscription=subscription,
                       active_until=expiry, will_renew=renewal if isinstance(renewal, bool) else None,
                       stage='account', message='已取得账号及订阅信息')
        self.store.update(key, precheck=dict(summary), current_plan=current,
                          subscription=subscription, active_until=expiry)
        reason = blocking_reason(account, plan, preview.get('plan_flow', ''))
        if reason:
            summary.update(stage='blocked', message=reason)
            self.store.update(key, precheck=dict(summary))
            raise ValueError(reason)
        ticket = account.get('preflight_token')
        if not ticket:
            raise ValueError('预检未返回有效确认凭证')
        self.session_checks[key]={'fingerprint':fingerprint(session),'time':now()}
        self.tickets[key] = dict(redemption_token=token, preflight_token=ticket,
                                 timestamp=time.monotonic(),
                                 session_fingerprint=fingerprint(session))
        summary.update(stage='passed', message='预检通过，请核对下方结果后确认兑换', checked_at=now())
        # Only select display fields, never store the full response/ticket.
        self.store.update(key, status='confirm', plan=target_label, precheck=dict(summary),
            message='预检通过，请核对邮箱、当前套餐与目标套餐后确认兑换')

    async def redeem(self, key):
        self.store.ensure_recharge_allowed(key)
        row = self.store.get(key)
        ticket = self.tickets.get(key)
        if row['submitted'] or not ticket:
            raise ValueError('缺少预检凭证或已提交，请查询结果')
        if time.monotonic() - ticket['timestamp'] > 300:
            self.tickets.pop(key, None)
            raise ValueError('预检已超过五分钟，请重新预检')
        if self.session_info(key)['state']!='usable' or ticket.get('session_fingerprint')!=fingerprint(self.sessions.get(key)):
            self.tickets.pop(key,None)
            await self.preflight(key)
            ticket=self.tickets.get(key)
            if not ticket:
                raise ValueError('登录凭证变化后预检未通过，未提交兑换')
        self.store.ensure_recharge_allowed(key)
        task = self.store.charge_tasks.get(row.get('active_task_id'))
        if not task:
            raise ValueError('充值任务不存在，未提交兑换')
        client_request_id = 'agent-' + uuid.uuid4().hex
        body = {k:v for k,v in ticket.items() if k in ('redemption_token','preflight_token')}
        body['client_request_id'] = client_request_id
        self.store.inventory_update_task(key, client_request_id=client_request_id,
                                         partner_redemption_token=crypt(ticket['redemption_token']),
                                         partner_preflight_token=crypt(ticket['preflight_token']))
        # Durable guard BEFORE the network side effect, including cancellation/restart.
        self.store.update(key, submitted=True, can_retry=False, status='pending', message='已提交兑换，正在查询服务端结果')
        refresh_after = True
        retry_lookup_delay = 0
        try:
            result=await (await self.provider(key)).call('redeem', body)
            risk=payment_risk_code(result)
            if risk:
                raise ProviderRiskError(risk)
        except SessionRejectedError:
            self.invalidate_session(key)
            self.store.inventory_update_task(key, submission_rejected=True)
            self.store.update(key,status='pending',message='Partner 已拒绝本次提交，正在核查原卡密；不会重新提交')
        except PartnerConfigurationError as exc:
            self.store.inventory_update_task(key, partner_redemption_token='', partner_preflight_token='',
                                             submission_rejected=False)
            self.store.update(key,submitted=False,can_retry=True,status='error',message=str(exc)[:180])
            refresh_after = False
        except PartnerAPIError as exc:
            if exc.uncertain:
                self.store.update(key,status='pending',message='提交结果尚未确认，仅查询原卡密，不自动重发')
            else:
                self.store.inventory_update_task(key, submission_rejected=True)
                self.store.update(key,status='pending',message='Partner 已拒绝本次提交，正在核查原卡密；不会重新提交')
                if exc.status == 429 or exc.code == 'RATE_LIMITED':
                    retry_lookup_delay = exc.retry_after or 3
        except ProviderRiskError as exc:
            self.store.quarantine_account(key,exc.risk_code)
            self.store.update(key,status='pending',message=str(exc)+'；正在核实原卡密，不会重新提交')
        except Exception:
            self.store.update(key, status='pending', message='提交结果尚未确认，仅查询原卡密，不自动重发')
        finally:
            self.tickets.pop(key, None)
        if refresh_after:
            self.provider_poll_after[key] = time.monotonic() + retry_lookup_delay
            if not retry_lookup_delay:
                await self.refresh(key)

    def snapshot(self):
        async def collect():
            return dict(paused=self.paused, limit=self.limit, session_limit=self.session_limit, records=[dict(r, session_valid=self.session_info(r['id'])['state'] in ('usable','expiring'),session_info=self.session_info(r['id']),
                busy=r['id'] in self.jobs and not self.jobs[r['id']].done()) for r in self.store.public()])
        return asyncio.run_coroutine_threadsafe(collect(), self.loop).result(timeout=5)

    def close(self):
        async def cleanup():
            tasks = [t for t in self.jobs.values() if not t.done()]
            tasks.append(self.maintenance_task)
            tasks.append(self.edge_page_watchdog)
            tasks.append(self.partner_transaction_watchdog)
            tasks += [t for t in getattr(self, 'inventory_checks', {}).values() if not t.done()]
            tasks += [t for t in self.direct_jobs.values() if not t.done()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for context in list(self.login_contexts.values()):
                await context.close()
            for browser in list(self.login_browsers.values()):
                await browser.close()
            for provider in self.providers.values():
                await provider.close()
            if self.provider_client:
                await self.provider_client.close()
            if self.browser:
                await self.browser.close()
            if hasattr(self, 'pw'):
                await self.pw.stop()
            self.sessions.clear()
            self.tickets.clear()
            self.direct_quotes.clear()
        try:
            asyncio.run_coroutine_threadsafe(cleanup(), self.loop).result(timeout=45)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(3)
            if not self.thread.is_alive():
                self.loop.close()
