"""Account edits and explicit Session access run on the same loop as jobs."""
import asyncio
import json

from .edge_session import validate_handoff


class AccountFlowMixin:
    def update_account(self, data):
        async def edit():
            key = data.get('id')
            if not isinstance(key, str):
                raise ValueError('请选择一个账号')
            if key in self.jobs and not self.jobs[key].done():
                raise ValueError('请等待当前任务完成或停止后再补全资料')
            row = self.store.edit_account(key, data)
            self.tickets.pop(key, None)
            valid = key in self.sessions
            if valid:
                try:
                    validate_handoff(self.sessions[key], row['email'])
                except ValueError:
                    self.clear_session(key)
                    valid = False
            message = ('Session 已保留；请绑定卡密后预检' if row.get('probe_only')
                       else '资料已保存；Session 已保留，可直接预检' if valid
                       else '资料已保存，可开始自动登录并预检')
            self.store.update(key, status='ready' if valid else 'idle', plan='未检测',
                              current_plan='未检测', precheck={}, message=message)
            return {'ok': True, 'id': key, 'session_valid': valid, 'probe_only': bool(row.get('probe_only'))}
        return asyncio.run_coroutine_threadsafe(edit(), self.loop).result(timeout=5)

    def view_session(self, key):
        async def read():
            row = self.store.get(key)
            session = self.sessions.get(key)
            if not session:
                raise ValueError('尚未获取 Session，请先点击自动登录')
            try:
                validate_handoff(session, row['email'])
            except ValueError:
                info=self.session_info(key)
                self._session_meta(key,**{name:info.get(name) for name in ('session_expires_at','access_token_expires_at','effective_expires_at')})
                self.clear_session(key)
                if row['status'] in ['ready', 'confirm']:
                    self.store.update(key, status='idle', message='Session 已过期或无效，请重新获取')
                raise
            # Copy the value rather than sharing the mutable engine object with HTTP.
            return {'email': row['email'], 'session': json.loads(json.dumps(session))}
        return asyncio.run_coroutine_threadsafe(read(), self.loop).result(timeout=5)
