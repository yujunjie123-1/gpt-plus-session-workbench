"""Single-use handoff from the user's ordinary Edge extension, held in memory."""
import asyncio
import hashlib
import json
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .browser_profiles import launch_isolated_edge
from .store import crypt
from .auth import fetch_mail_code, totp_code
from .session_lifecycle import local_facts, SessionNeedsAttention

PAGE_HEARTBEAT_TIMEOUT = 60
EDGE_LAUNCH_TIMEOUT = 60


class EdgePageLostError(SessionNeedsAttention):
    pass


def open_ordinary_edge(request_id='', **options):
    # Initialization only touches local settings. The actual login is native Edge
    # without a DevTools connection, in a unique disposable profile.
    return launch_isolated_edge(**options)


def validate_handoff(session, email):
    if not isinstance(session, dict) or not isinstance(session.get('user'), dict):
        raise ValueError('尚未读取到完整的登录 Session')
    actual = session['user'].get('email')
    if not isinstance(actual, str) or actual.strip().casefold() != email.strip().casefold():
        raise ValueError('当前登录邮箱与任务邮箱不一致，未接收 Session')
    for name in ('accessToken', 'sessionToken'):
        if not isinstance(session.get(name), str) or not session[name].strip():
            raise ValueError('Session 缺少登录凭证，请检查扩展的 Cookie 读取权限')
    try:
        expires = datetime.fromisoformat(session['expires'].replace('Z', '+00:00'))
        if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            raise ValueError()
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        raise ValueError('Session 已过期或到期时间无效，请重新登录') from None
    if len(json.dumps(session, ensure_ascii=False).encode('utf-8')) > 60000:
        raise ValueError('Session 超出兑换组件允许的大小')
    facts=local_facts(session,email)
    if facts['state'] in ('invalid','expired'):
        raise ValueError(facts.get('problem') or '必要登录凭证已过期，请重新登录')


class EdgeSessionMixin:
    async def edge_login(self, key, open_window=True, automatic=None):
        if getattr(self, 'login_gate', None) is None:
            self.login_gate = asyncio.Semaphore(5)
        self.store.update(key, status='queued', message='等待 InPrivate 登录名额（最多同时 5 个）')
        async with self.login_gate:
            self.store.update(key, status='running', message='已取得 InPrivate 登录名额')
            return await self._edge_login(key, open_window=open_window, automatic=automatic)

    async def _edge_login(self, key, open_window=True, automatic=None):
        row = self.store.get(key)
        self.clear_session(key)
        future = self.loop.create_future()
        request = dict(record_id=key, request_id=secrets.token_urlsafe(32),
                       email=row['email'], expires_at=time.time() + 600,
                       incognito=True, future=future, private={},
                       automatic=bool(crypt(row['password'], True)) if automatic is None else automatic,
                       recovery=not open_window,
                       instance_id=secrets.token_urlsafe(32) if open_window else '')
        if open_window:
            request['private']['ready'] = self.loop.create_future()
            request['private'].update(bootstrap_nonce=secrets.token_urlsafe(32),
                                      instance_token=secrets.token_urlsafe(32), bound=False)
        self.edge_requests[key] = request
        message = ('正在从已登录的 InPrivate 窗口恢复 Session，无需重新输入账号'
                   if not open_window else '正在准备独立 InPrivate 窗口，随后自动填写账号并监听验证码'
                   if request['automatic'] else '正在准备独立 InPrivate 窗口，随后请手动登录（未配置 GPT 密码）')
        self.store.update(key, status='login', precheck={}, message=message)
        launch, handle = None, None
        cancel = threading.Event()
        try:
            if open_window:
                bootstrap = ((self.edge_origin or '') + '/edge-session-bridge#instance=' + request['instance_id']
                             + '&nonce=' + request['private']['bootstrap_nonce'])
                launch = asyncio.create_task(asyncio.to_thread(
                    open_ordinary_edge, request['request_id'], profile_root=self.store.path.parent/'browser-profiles',
                    profile_key=key, instance_id=request['instance_id'], bootstrap_url=bootstrap, cancel=cancel))
                try:
                    handle = await asyncio.wait_for(asyncio.shield(launch), timeout=EDGE_LAUNCH_TIMEOUT)
                except asyncio.TimeoutError:
                    raise ValueError('准备独立 InPrivate 窗口超过一分钟，请检查 Edge 安装或本机策略后重试') from None
                self.store.update(key, status='login',
                                  message='独立 InPrivate 窗口已启动，正在连接登录助手')
                ready = request['private']['ready']
                try:
                    done, _ = await asyncio.wait_for(
                        asyncio.wait((ready, future), return_when=asyncio.FIRST_COMPLETED),
                        timeout=PAGE_HEARTBEAT_TIMEOUT)
                except asyncio.TimeoutError:
                    raise ValueError('独立 InPrivate 窗口未能连接登录助手，请检查 Edge 的 InPrivate 扩展权限后重试') from None
                if future in done:
                    future.result()
            try:
                session = await asyncio.wait_for(future, timeout=600)
            except asyncio.TimeoutError:
                raise ValueError('等待 Edge 登录超过十分钟，请检查扩展连接及 InPrivate 权限后重新开始') from None
            validate_handoff(session, row['email'])
            self.remember_session(key,session,recovered=not open_window)
            self.store.update(key, status='ready', message='已获取并核对 Session，可查看、复制或选择账号充值')
        finally:
            self.edge_requests.pop(key, None)
            cancel.set()
            if not future.done():
                future.cancel()
            ready = request['private'].get('ready')
            if ready is not None and not ready.done():
                ready.cancel()
            if launch is not None:
                try:
                    if handle is None:
                        handle = await asyncio.shield(launch)
                    if callable(getattr(handle, 'close', None)):
                        await asyncio.to_thread(handle.close)
                except Exception:
                    pass

    def edge_jobs(self, scope=None):
        async def collect():
            if scope:
                for request in self.edge_requests.values():
                    if request.get('instance_id') == scope:
                        request['private']['helper_seen'] = time.monotonic()
            return [dict((k, v) for k, v in request.items() if k not in ['future', 'private'])
                    for request in self.edge_requests.values()
                    if not request['future'].done() and request['expires_at'] > time.time()
                    and (scope is None or request.get('instance_id', '') == scope)]
        return asyncio.run_coroutine_threadsafe(collect(), self.loop).result(timeout=5)

    def bind_edge_instance(self, payload):
        async def bind():
            instance, nonce = payload.get('instance_id'), payload.get('bootstrap_nonce')
            if not isinstance(instance, str) or not isinstance(nonce, str) or payload.get('incognito') is not True:
                raise ValueError('请使用本次任务打开的独立 InPrivate 窗口连接')
            request = next((item for item in self.edge_requests.values() if item.get('instance_id') == instance), None)
            if (not request or request['future'].done() or request['expires_at'] <= time.time()
                    or request['private'].get('bound')
                    or not secrets.compare_digest(nonce, request['private'].get('bootstrap_nonce', ''))):
                raise ValueError('独立窗口连接已过期或已使用，请重新开始本次任务')
            stamp = time.monotonic()
            request['private'].update(bound=True, bootstrap_nonce='', bound_at=stamp,
                                      helper_seen=stamp, page_seen=None)
            if not request['private']['ready'].done():
                request['private']['ready'].set_result(True)
            self.store.update(request['record_id'], status='login',
                              message='登录助手已连接，正在等待登录页面')
            return {'origin': self.edge_origin, 'instance_id': instance,
                    'token': request['private']['instance_token'], 'request_id': request['request_id']}
        return asyncio.run_coroutine_threadsafe(bind(), self.loop).result(timeout=5)

    def edge_token_scope(self, token):
        async def resolve():
            if not isinstance(token, str) or not 32 <= len(token) <= 128:
                return None
            for request in self.edge_requests.values():
                private = request['private']
                if (request.get('instance_id') and private.get('bound') and not request['future'].done()
                        and request['expires_at'] > time.time()
                        and secrets.compare_digest(token, private.get('instance_token', ''))):
                    return request['instance_id']
            return None
        return asyncio.run_coroutine_threadsafe(resolve(), self.loop).result(timeout=5)

    def _edge_request(self, payload, scope=None):
        key = payload.get('record_id')
        if not isinstance(key, str) or key not in self.edge_requests:
            raise ValueError('登录请求不存在或已经停止，请重新开始')
        request = self.edge_requests[key]
        if scope is not None and request.get('instance_id', '') != scope:
            raise ValueError('该登录任务不属于当前独立窗口，未读取或写入账号资料')
        if payload.get('instance_id') is not None and payload['instance_id'] != request.get('instance_id', ''):
            raise ValueError('登录实例不匹配')
        nonce = payload.get('request_id')
        if (not isinstance(nonce, str) or not secrets.compare_digest(nonce, request['request_id'])
                or request['expires_at'] <= time.time() or request['future'].done()):
            raise ValueError('登录请求已过期或已经接收，未重复写入')
        row = self.store.get(key)
        if self.job_actions.get(key)=='session_refresh' and self.session_refresh_reason(key,include_busy=False):
            if not request['future'].done():
                request['future'].set_exception(SessionNeedsAttention('账号不再符合自动更新范围'))
            raise ValueError('账号不再符合自动更新范围')
        if row.get('submitted') or row['status'] != 'login':
            raise ValueError('任务状态已改变，未接收登录数据')
        return request, row

    def edge_step(self, payload, scope=None):
        async def step():
            request, row = self._edge_request(payload, scope)
            kind = payload.get('kind')
            messages = {
                'email': '已自动填写邮箱，等待密码页面',
                'password': '已自动填写 GPT 密码，正在监听邮箱验证码或 2FA',
                'password_choice': '已切换为密码登录，正在等待密码步骤',
                'other_methods': '正在打开其他登录方式',
                'email_code': '正在自动读取并填写邮箱验证码',
                'email_code_submitted': '邮箱验证码已填写，正在等待密码登录或 2FA 步骤',
                'totp': '正在本机生成并填写 2FA 动态验证码',
                'totp_submitted': '2FA 动态验证码已填写，正在等待登录完成或 Session 返回',
                'waiting_login_result': '登录表单已提交，正在等待 OpenAI 返回登录结果',
                'operation_timeout_retry': 'OpenAI 登录页操作超时，已点击重试，正在等待页面恢复',
                'switching': '正在为本次任务退出其他无痕账号并切换登录',
                'challenge': '遇到人机或安全验证，请在 Edge 中完成；程序随后继续',
                'unknown_code': '验证码类型无法确定，请在 Edge 检查；程序仍在监听',
            }
            def progress(stage):
                message = messages.get(stage)
                if message and self.store.get(row['id'])['message'] != message:
                    self.store.update(row['id'], message=message)
            if kind == 'progress':
                progress(payload.get('stage'))
                if self.job_actions.get(row['id'])=='session_refresh' and payload.get('stage') in ('challenge','unknown_code'):
                    request['future'].set_exception(SessionNeedsAttention('需要人工完成安全验证，请手动获取 Session'))
                return {'ok': True}
            if kind == 'page_heartbeat':
                page_id = payload.get('page_id')
                if isinstance(page_id, bool) or not isinstance(page_id, int) or page_id <= 0:
                    raise ValueError('登录页面心跳格式不合法')
                previous_page = request['private'].get('page_id')
                if previous_page is not None and previous_page != page_id:
                    raise ValueError('登录页面实例不匹配')
                request['private']['page_seen'] = time.monotonic()
                request['private']['page_id'] = page_id
                return {'ok': True}
            if kind == 'password_choice':
                method = payload.get('method')
                if method not in ('password', 'other_methods'):
                    raise ValueError('登录方式选项不合法')
                return {'available': True, 'method': method}
            if kind == 'failure':
                errors = {
                    'route400': 'OpenAI 登录页返回 400，本次自动填写已停止；尚未获取 Session',
                    'credentials': '登录页提示账号、密码或验证码错误，请核对账号资料后重试',
                    'account_mismatch': 'InPrivate 中当前账号与任务邮箱不同，请先退出该账号后重试',
                    'operation_timeout': 'OpenAI 登录页连续操作超时，已停止自动重试；请稍后重新获取 Session',
                }
                message = errors.get(payload.get('code'), '登录未完成，请检查 Edge 窗口')
                request['future'].set_exception(ValueError(message))
                return {'ok': True}
            if kind == 'operation_timeout':
                if request.get('timeout_retries', 0) >= 2:
                    request['future'].set_exception(ValueError('OpenAI 登录页连续操作超时，请稍后重新获取 Session'))
                    return {'available': False, 'message': '登录页连续超时，已停止自动重试'}
                request['timeout_retries'] = request.get('timeout_retries', 0) + 1
                return {'available': True, 'retry': request['timeout_retries']}
            if request.get('automatic') is not True:
                raise ValueError('此任务仅恢复 Session，没有自动填写授权')
            if kind in ['email', 'password']:
                return {'available': True, 'value': row['email'] if kind == 'email' else crypt(row['password'], True)}
            if kind == 'totp':
                seed = crypt(row['totp'], True)
                if not seed:
                    if self.job_actions.get(row['id'])=='session_refresh':
                        request['future'].set_exception(SessionNeedsAttention('请先补全 2FA 密钥'))
                    return {'available': False, 'message': '请在补全资料中填写 2FA 密钥'}
                progress('totp')
                remaining = 30 - time.time() % 30
                if remaining < 4:
                    await asyncio.sleep(remaining + 0.1)
                code = totp_code(seed)
            elif kind == 'email_code':
                url = crypt(row['mail_url'], True)
                if not url:
                    if self.job_actions.get(row['id'])=='session_refresh':
                        request['future'].set_exception(SessionNeedsAttention('请先补全邮箱取码链接'))
                    return {'available': False, 'message': '请在补全资料中填写邮箱取码链接'}
                progress('email_code')
                started = request['private'].setdefault('mail_started', time.monotonic())
                if time.monotonic() - started > 180:
                    request['future'].set_exception(ValueError('等待邮箱验证码超过三分钟，请核对取码链接后重试'))
                    return {'available': False, 'stopped': True}
                try:
                    code = await asyncio.to_thread(fetch_mail_code, url)
                except RuntimeError:
                    return {'available': False, 'message': '暂未收到有效邮件验证码，正在继续监听'}
            else:
                raise ValueError('未知登录步骤')
            # Network waits and TOTP boundary waits must not outlive cancellation.
            self._edge_request(payload, scope)
            code_id = hashlib.sha256((request['request_id'] + kind + code).encode()).hexdigest()
            return {'available': True, 'value': code, 'code_id': code_id}
        return asyncio.run_coroutine_threadsafe(step(), self.loop).result(timeout=15)

    async def _expire_stale_edge_pages(self):
        stamp = time.monotonic()
        for key, request in list(self.edge_requests.items()):
            private = request['private']
            if not request.get('instance_id') or not private.get('bound') or request['future'].done():
                continue
            try:
                row = self.store.get(key)
            except KeyError:
                request['future'].set_exception(EdgePageLostError('账号已删除，登录任务已结束'))
                continue
            # A login watcher must never release an account whose remote redemption may be unresolved.
            if row.get('submitted') or row.get('status') != 'login':
                continue
            page_seen = private.get('page_seen')
            if page_seen is None:
                page_seen = private.get('bound_at', stamp)
            helper_seen = private.get('helper_seen', private.get('bound_at', stamp))
            if (stamp - helper_seen >= PAGE_HEARTBEAT_TIMEOUT
                    or stamp - page_seen >= PAGE_HEARTBEAT_TIMEOUT):
                request['future'].set_exception(EdgePageLostError(
                    'InPrivate 登录页面连续 60 秒无响应，已结束任务并释放账号；请检查窗口后重新获取 Session'))

    async def watch_edge_pages(self):
        while True:
            await self._expire_stale_edge_pages()
            await asyncio.sleep(2)

    def receive_edge_session(self, payload, scope=None):
        async def receive():
            request, row = self._edge_request(payload, scope)
            key = row['id']
            session = payload.get('session')
            validate_handoff(session, request['email'])
            request['future'].set_result(session)
            return {'ok': True, 'record_id': key}
        return asyncio.run_coroutine_threadsafe(receive(), self.loop).result(timeout=5)
