"""Credential-free lifetime metadata and bounded refreshes on the existing job loop."""
import asyncio
import base64
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone

from .store import crypt
from .inventory_store import task_unresolved

REFRESH_AHEAD = 600
SCAN_INTERVAL = 30
RETRY_DELAYS = (60, 300)
META_FIELDS = {'acquired_at','restored_at','session_expires_at','access_token_expires_at',
               'effective_expires_at','invalidated_at','attempts','next_retry_at',
               'manual_required','refresh_message'}


class SessionNeedsAttention(ValueError):
    pass


class SessionRejectedError(RuntimeError):
    def __init__(self):
        super().__init__('兑换服务明确拒绝登录凭证，Session 已标记失效')


def iso(stamp):
    return datetime.fromtimestamp(stamp,timezone.utc).isoformat(timespec='seconds')


def timestamp(value):
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        return result.timestamp() if result.tzinfo is not None else None
    except (AttributeError,TypeError,ValueError,OverflowError,OSError):
        return None


def token_expiry(token):
    """Read a well-formed JWT exp as an upper bound only; this does not verify a signature."""
    try:
        if not isinstance(token,str) or len(token)>60000:
            return None
        parts=token.split('.')
        if len(parts)!=3 or any(not re.fullmatch(r'[A-Za-z0-9_-]+',part) for part in parts):
            return None
        def decode(part):
            return json.loads(base64.b64decode(part+'='*(-len(part)%4),altchars=b'-_',validate=True))
        header,payload=decode(parts[0]),decode(parts[1])
        if not isinstance(header,dict) or header.get('alg') not in ('RS256','RS384','RS512','ES256','ES384','ES512','EdDSA','HS256','HS384','HS512'):
            return None
        expiry=payload.get('exp') if isinstance(payload,dict) else None
        if isinstance(expiry,bool) or not isinstance(expiry,(int,float)) or not math.isfinite(expiry):
            return None
        if not 0<expiry<253402300799:
            return None
        return float(expiry)
    except (ValueError,TypeError,UnicodeError):
        return None


def local_facts(value,email,clock=None):
    clock=time.time() if clock is None else clock
    if not isinstance(value,dict) or not isinstance(value.get('user'),dict):
        return {'state':'invalid','problem':'Session 格式不完整'}
    actual=value['user'].get('email')
    if not isinstance(actual,str) or actual.strip().casefold()!=email.strip().casefold():
        return {'state':'invalid','problem':'Session 邮箱与账号不一致'}
    if any(not isinstance(value.get(name),str) or not value[name].strip() for name in ('accessToken','sessionToken')):
        return {'state':'invalid','problem':'Session 缺少必要登录凭证'}
    expiry=timestamp(value.get('expires'))
    if expiry is None:
        return {'state':'invalid','problem':'Session 到期时间无法确认'}
    access=token_expiry(value['accessToken'])
    deadline=min(expiry,access) if access is not None else expiry
    return {'state':'expired' if deadline<=clock else 'expiring' if deadline-clock<REFRESH_AHEAD else 'usable',
            'session_expires_at':iso(expiry),'access_token_expires_at':iso(access) if access is not None else None,
            'effective_expires_at':iso(deadline),'token_expiry_known':access is not None,
            'problem':''}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()


class SessionLifecycleMixin:
    def _session_meta(self,key,**fields):
        assert not set(fields)-META_FIELDS
        row=self.store.get(key)
        meta={name:value for name,value in row.get('session_meta',{}).items() if name in META_FIELDS}
        meta.update(fields)
        self.store.update(key,session_meta=meta)
        return meta

    def remember_session(self,key,value,recovered=False):
        row=self.store.get(key)
        facts=local_facts(value,row['email'],self.session_clock())
        if facts['state'] in ('invalid','expired'):
            raise ValueError(facts.get('problem') or '必要登录凭证已过期，请重新登录')
        self.store.save_session_credential(key,value)
        self.sessions[key]=value
        self.session_checks.pop(key,None)
        fields={name:facts.get(name) for name in ('session_expires_at','access_token_expires_at','effective_expires_at')}
        fields.update(invalidated_at=None)
        if recovered:
            fields['restored_at']=iso(self.session_clock())
        else:
            fields.update(acquired_at=iso(self.session_clock()),attempts=0,next_retry_at=None,
                          manual_required=False,refresh_message='')
        self._session_meta(key,**fields)

    def forget_session_memory(self,key):
        self.sessions.pop(key,None)
        self.session_checks.pop(key,None)
        self.tickets.pop(key,None)

    def clear_session(self,key):
        self.forget_session_memory(key)
        self.store.delete_session_credential(key)

    def restore_session_credentials(self):
        for key,value in self.store.load_session_credentials().items():
            row=self.store.rows.get(key)
            if not row or row.get('status')=='completed' or self.store.has_successful_recharge(row):
                self.store.delete_session_credential(key)
                continue
            try:
                from .edge_session import validate_handoff
                validate_handoff(value,row['email'])
            except (ValueError,TypeError):
                self.store.delete_session_credential(key)
                continue
            self.sessions[key]=value

    def invalidate_session(self,key):
        self.clear_session(key)
        self._session_meta(key,invalidated_at=iso(self.session_clock()))

    def session_info(self,key):
        row=self.store.get(key)
        meta=row.get('session_meta',{})
        value=self.sessions.get(key)
        info={name:meta.get(name) for name in META_FIELDS}
        info.update(present=value is not None,verification='not_checked',verified_at=None,
                    token_expiry_known=False,local_checked_at=iso(self.session_clock()))
        if value is None:
            cutoff=timestamp(meta.get('effective_expires_at'))
            info.update(state='invalid' if meta.get('invalidated_at') else 'expired' if cutoff is not None and cutoff<=self.session_clock() else 'missing',
                        problem='内存中没有 Session；历史时间不代表当前可用')
            if meta.get('invalidated_at'):
                info.update(verification='provider_rejected',verified_at=meta['invalidated_at'])
            return info
        facts=local_facts(value,row['email'],self.session_clock())
        info.update(facts)
        check=self.session_checks.get(key)
        if check and check['fingerprint']==fingerprint(value):
            info.update(verification='provider_accepted',verified_at=check['time'])
        return info

    def session_refresh_reason(self,key,include_busy=True):
        row=self.store.get(key)
        reason=self.store.recharge_block_reason(row)
        if reason:
            return reason
        # Eligibility comes from immutable successful history, never the current plan.
        if row.get('submitted') or task_unresolved(self.store.charge_tasks.get(row.get('active_task_id'))):
            return '账号有充值任务或待核实提交'
        if include_busy and key in self.jobs and not self.jobs[key].done():
            return '账号已有登录或充值任务'
        return ''

    async def refresh_eligible_sessions(self,force=False):
        result={'queued':0,'excluded':0,'not_needed':0,'waiting':0,'manual':0}
        with self.store.lock:
            candidates=[self.store.get(key) for key in self.store.rows]
        for row in candidates:
            key=row['id']
            reason=self.session_refresh_reason(key,include_busy=False)
            job=self.jobs.get(key)
            if reason:
                if job and not job.done() and self.job_actions.get(key)=='session_refresh':
                    self.session_refresh_sources[key]='excluded'
                    job.cancel()
                    self.store.update(key,status='idle',message='自动更新已跳过：'+reason)
                result['excluded']+=1
                continue
            if job and not job.done():
                result['excluded']+=1
                continue
            facts=self.session_info(key)
            if not force and facts['state']=='usable':
                result['not_needed']+=1
                continue
            meta=row.get('session_meta',{})
            if not force and meta.get('manual_required'):
                result['manual']+=1
                continue
            if not force and (timestamp(meta.get('next_retry_at')) or 0)>self.session_clock():
                result['waiting']+=1
                continue
            try:
                configured=bool(row.get('password') and crypt(row['password'],True))
            except Exception:
                configured=False
            if not configured:
                self._session_meta(key,manual_required=True,refresh_message='缺少 GPT 密码，请补全资料后点击更新重试')
                result['manual']+=1
                continue
            if force:
                self._session_meta(key,attempts=0,next_retry_at=None,manual_required=False,refresh_message='')
            self.session_refresh_sources[key]='manual' if force else 'auto'
            await self._command('session_refresh',[key],1)
            result['queued']+=1
        return result

    async def _session_maintenance(self):
        while True:
            await asyncio.sleep(SCAN_INTERVAL)
            if self.session_auto_enabled:
                try:
                    await self.refresh_eligible_sessions()
                except Exception:
                    # Do not log remote errors, account values or credentials.
                    self.session_maintenance_error='自动更新检查暂未完成，请检查本地服务'

    async def _run_session_refresh(self,key):
        meta=self.store.get(key).get('session_meta',{})
        attempts=int(meta.get('attempts') or 0)+1
        self._session_meta(key,attempts=attempts,next_retry_at=None,refresh_message='正在更新 Session')
        try:
            await self.edge_login(key)
            reason=self.session_refresh_reason(key,include_busy=False)
            if reason:
                self.store.update(key,status='idle',message='已跳过更新：'+reason)
                return
            if self.session_info(key)['state']!='usable':
                raise RuntimeError('新登录凭证的剩余时间不足')
            self._session_meta(key,attempts=0,next_retry_at=None,manual_required=False,refresh_message='Session 更新完成；尚未进行兑换服务验证')
        except asyncio.CancelledError:
            manual=self.session_refresh_sources.get(key) not in ('disabled','excluded')
            self._session_meta(key,manual_required=manual,next_retry_at=None,refresh_message='自动更新已停止；需要时可手动点击更新重试' if manual else '自动更新已关闭或账号已不符合条件')
            raise
        except Exception as error:
            manual=isinstance(error,ValueError) or attempts>=3
            self._session_meta(key,attempts=attempts,manual_required=manual,
                next_retry_at=None if manual else iso(self.session_clock()+RETRY_DELAYS[attempts-1]),
                refresh_message='需要人工处理：请检查账号、验证码或人机验证，完成后手动点击更新重试' if manual else '暂未更新成功，将在退避后重试（本轮最多 3 次）')
            self.store.update(key,status='error',message=self.store.get(key)['session_meta']['refresh_message'])

    async def ensure_charge_session(self,key):
        self.store.ensure_recharge_allowed(key)
        info=self.session_info(key)
        if info['state'] in ('missing','invalid','expired','expiring'):
            row=self.store.get(key)
            if not row.get('password') or not crypt(row['password'],True):
                raise SessionNeedsAttention('登录凭证不可用，请补全 GPT 密码后重新获取 Session')
            await self.edge_login(key)
            if self.session_info(key)['state']!='usable':
                raise SessionNeedsAttention('新登录凭证的有效时间不足，请检查后重新获取 Session')
