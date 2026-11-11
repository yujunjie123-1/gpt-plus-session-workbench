"""Serialize stock allocation and the existing login/redemption engine on one loop."""
import asyncio
import hashlib
import json
import re
import secrets
import time
import uuid
from .store import crypt, mask, now
from .inventory_store import ACTIVE, task_unresolved
from .provider import PartnerAPIError, PartnerConfigurationError, lookup_state, partner_qualification, blocking_reason
from .session_lifecycle import SessionRejectedError
from .session_lifecycle import local_facts
from .products import PRODUCTS


class InventoryFlowMixin:
    def _inventory_call(self, operation):
        return asyncio.run_coroutine_threadsafe(operation, self.loop).result(timeout=10)

    def session_direct_prepare(self, data):
        code=data.get('code') if isinstance(data,dict) else None
        session_value=data.get('session') if isinstance(data,dict) else None
        if not isinstance(code,str) or not 4<=len(code.strip())<=256 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{3,255}',code.strip()):
            raise ValueError('请输入一张完整卡密')
        code=code.strip()
        if not isinstance(session_value,str) or not 1<=len(session_value.encode('utf-8'))<=128*1024:
            raise ValueError('请先提取有效 Session')
        try:session=json.loads(session_value)
        except (ValueError,TypeError):raise ValueError('Session 格式无效，请重新提取') from None
        user=session.get('user') if isinstance(session,dict) else None
        email=user.get('email','').strip() if isinstance(user,dict) and isinstance(user.get('email'),str) else ''
        if not email or local_facts(session,email)['state'] in ('invalid','expired'):
            raise ValueError(local_facts(session,email).get('problem') or 'Session 已过期，请重新提取')
        digest=hashlib.sha256(code.encode('utf-8')).hexdigest()
        with self.store.lock:
            if any(card.get('digest')==digest for card in self.store.cards.values()):
                raise ValueError('该卡密已在卡密管理中，请从库存充值')
            if any(task.get('card_digest')==digest and not
                   (task.get('source')=='session_extract' and task.get('status')=='failed'
                    and task.get('can_retry') is True and task.get('remote_terminal'))
                   for task in self.store.charge_tasks.values()):
                raise ValueError('该卡密已有独立充值记录，不可重复提交')
        return asyncio.run_coroutine_threadsafe(
            self._session_direct_prepare(code,session,email,digest),self.loop).result(timeout=240)

    async def _session_direct_prepare(self,code,session,email,digest):
        provider=await self.provider('session-extract')
        lookup=await provider.lookup(code)
        state=lookup_state(lookup)
        if state=='completed':
            raise ValueError('上游确认这张卡密已使用；未提交新的兑换')
        if state=='pending':
            raise ValueError('这张卡密已有处理中交易；未提交新的兑换')
        if state in ('blocked','unknown') or not lookup.get('can_resubmit') is True:
            raise ValueError('上游未确认卡密可重新兑换；未提交新的兑换')
        if state not in ('unused','failed'):
            raise ValueError('卡密状态无法确认；未提交新的兑换')
        preview=await provider.call('preview',{'code':code})
        product,plan,redemption_token=partner_qualification(preview)
        preflight=await provider.call('preflight',{
            'code':code,'redemption_token':redemption_token,
            'credential':{'mode':'session','session':json.dumps(session,ensure_ascii=False,separators=(',',':'))}})
        actual=str(preflight.get('email',preflight.get('account_email',''))).strip()
        if not actual or actual.casefold()!=email.casefold():
            raise ValueError('预检邮箱与 Session 邮箱不一致或缺失，已阻止兑换')
        is_gpt=product.lower()=='gpt'
        policy_plan=plan.lower() if is_gpt else plan
        reason=blocking_reason(preflight,policy_plan,preview.get('plan_flow',''),
                               enforce_plan_conflict=is_gpt)
        if reason:
            raise ValueError(reason)
        preflight_token=preflight.get('preflight_token')
        if not isinstance(preflight_token,str) or not preflight_token.strip():
            raise ValueError('预检未返回有效确认凭证；未提交兑换')
        self.direct_quotes={key:value for key,value in self.direct_quotes.items() if value['expires']>time.monotonic()}
        nonce=secrets.token_urlsafe(24)
        summary={
            'email':email,'product':product,'plan':plan,'target_plan':f'{product} / {plan}',
            'current_plan':str(preflight.get('currentPlan',preflight.get('current_plan','上游未提供'))),
            'subscription':'有效' if preflight.get('subscription_has_active') is True else
                           '未订阅/已到期' if preflight.get('subscription_has_active') is False else '上游未提供',
            'card':mask(code),'checked_at':now(),
        }
        self.direct_quotes[nonce]={
            'email':email,'session':session,'code':code,'digest':digest,'sku':plan,
            'product':product,'plan':plan,
            'redemption_token':redemption_token,'preflight_token':preflight_token,
            'summary':summary,'expires':time.monotonic()+300,
        }
        return {'quote':nonce,'summary':summary}

    def session_direct_start(self,data):
        quote_id=data.get('quote') if isinstance(data,dict) else None
        if not isinstance(quote_id,str):raise ValueError('预检已失效，请重新核对')
        quote=self.direct_quotes.pop(quote_id,None)
        if not quote or quote['expires']<=time.monotonic():
            raise ValueError('预检已失效，请重新核对')
        request_id='agent-'+uuid.uuid4().hex
        sequence=max((task.get('sequence',0) for task in self.store.charge_tasks.values()),default=0)+1
        task={
            'id':uuid.uuid4().hex,'sequence':sequence,'source':'session_extract',
            'account_id':None,'email':quote['email'],'card_id':None,'card':quote['summary']['card'],
            'card_digest':quote['digest'],'direct_card_secret':crypt(quote['code']),
            'sku':quote['plan'],'provider_product':quote['product'],'provider_plan':quote['plan'],
            'status':'pending','submitted':True,'can_retry':False,
            'created':now(),'updated':now(),'message':'兑换已提交，正在查询上游结果',
            'history':[],'precheck':quote['summary'],'result_state':'pending',
            'client_request_id':request_id,
            'partner_redemption_token':crypt(quote['redemption_token']),
            'partner_preflight_token':crypt(quote['preflight_token']),
        }
        saved=self.store.inventory_direct_task_create(task)
        self.loop.call_soon_threadsafe(self._schedule_direct_task,saved['id'],quote['redemption_token'],quote['preflight_token'],request_id)
        from .inventory_store import public_task
        return {'ok':True,'task':public_task(saved)}

    def _schedule_direct_task(self,task_id,redemption_token='',preflight_token='',request_id=''):
        existing=self.direct_jobs.get(task_id)
        if existing and not existing.done():return
        coroutine=(self._session_direct_submit(task_id,redemption_token,preflight_token,request_id)
                   if redemption_token else self._session_direct_refresh(task_id))
        self.direct_jobs[task_id]=asyncio.create_task(coroutine)

    def session_direct_task(self,data):
        task_id=data.get('id') if isinstance(data,dict) else None
        action=data.get('action') if isinstance(data,dict) else None
        task=self.store.charge_tasks.get(task_id) if isinstance(task_id,str) else None
        if not task or task.get('source')!='session_extract' or action!='refresh':
            raise ValueError('请选择有效的独立充值记录和查询操作')
        from .inventory_store import public_task
        if task.get('remote_terminal') or task.get('status')=='completed':
            return {'ok':True,'task':public_task(task)}
        self.loop.call_soon_threadsafe(self._schedule_direct_task,task_id)
        return {'ok':True,'task':public_task(task)}

    async def _session_direct_submit(self,task_id,redemption_token,preflight_token,request_id):
        task=self.store.charge_tasks.get(task_id)
        if not task:return
        retry_lookup_delay=0
        try:
            await (await self.provider('direct-'+task_id)).redeem(redemption_token,preflight_token,request_id)
        except PartnerConfigurationError as exc:
            self._finish_direct_task(task_id,'failed',str(exc)[:180],can_retry=True)
            return
        except SessionRejectedError:
            self.store.inventory_direct_task_update(task_id,submission_rejected=True,status='pending',
                message='Partner 已拒绝本次提交，正在核查原卡密；不会重新提交')
        except PartnerAPIError as exc:
            if exc.uncertain:
                self.store.inventory_direct_task_update(task_id,status='pending',submitted=True,can_retry=False,
                    message='提交结果尚未确认，后台仅查询原交易；不会重复提交')
            else:
                self.store.inventory_direct_task_update(task_id,submission_rejected=True,status='pending',
                    submitted=True,can_retry=False,
                    message='Partner 已拒绝本次提交，正在核查原卡密；不会重新提交')
                if exc.status == 429 or exc.code == 'RATE_LIMITED':
                    retry_lookup_delay=exc.retry_after or 3
        except Exception:
            self.store.inventory_direct_task_update(task_id,status='pending',submitted=True,can_retry=False,
                message='提交结果尚未确认，后台仅查询原交易；不会重复提交')
        if retry_lookup_delay:
            self.direct_poll_after[task_id]=time.monotonic()+retry_lookup_delay
            return
        await self._session_direct_refresh(task_id)

    async def _session_direct_rejected_refresh(self,task_id):
        task=self.store.charge_tasks.get(task_id)
        if not task:return
        provider=await self.provider('direct-'+task_id)
        code=crypt(task.get('direct_card_secret',''),True) if task.get('direct_card_secret') else ''
        try:
            remote=await provider.lookup(code)
        except Exception:
            self.store.inventory_direct_task_update(task_id,status='pending',
                message='Partner 已拒绝提交，卡密状态暂不可核实；后台仅查询原卡密，不会重复提交')
            self.direct_poll_after[task_id]=time.monotonic()+3
            return

        state=lookup_state(remote)
        email=str(remote.get('account_email','')).strip()
        if state=='completed':
            self.store.inventory_direct_task_update(task_id,status='blocked',submitted=False,can_retry=False,
                message='Partner 已拒绝本次提交，但上游显示卡密已使用；未归属本次 Session',
                result_state='blocked',result_email='未确认',
                provider_result_email=email or '上游未提供',remote_terminal=True,
                submission_rejected=False,partner_redemption_token='',partner_preflight_token='',
                direct_card_secret='')
            return
        if state in ('unused','failed') and remote.get('can_resubmit') is True:
            self._finish_direct_task(task_id,'failed','Partner 已拒绝本次提交，上游确认卡密可重试',
                                     can_retry=True, submission_rejected=False)
            return
        if state=='blocked' or state in ('unused','failed'):
            self.store.inventory_direct_task_update(task_id,status='blocked',submitted=False,can_retry=False,
                message='Partner 已拒绝本次提交；上游未确认卡密可重提，请核对后再处理',
                result_state=state,remote_terminal=True,submission_rejected=False,
                partner_redemption_token='',partner_preflight_token='',direct_card_secret='')
            return
        self.store.inventory_direct_task_update(task_id,status='pending',
            message='Partner 已拒绝提交，但卡密状态仍未明确；后台只查询原卡密，不会重复提交')
        self.direct_poll_after[task_id]=time.monotonic()+3

    async def _session_direct_refresh(self,task_id):
        task=self.store.charge_tasks.get(task_id)
        if not task or task.get('remote_terminal'):return
        if task.get('submission_rejected'):
            await self._session_direct_rejected_refresh(task_id)
            return
        provider=await self.provider('direct-'+task_id)
        token=crypt(task.get('partner_redemption_token',''),True) if task.get('partner_redemption_token') else ''
        code=crypt(task.get('direct_card_secret',''),True) if task.get('direct_card_secret') else ''
        try:
            remote=await provider.result(token) if token else await provider.result_by_code(code)
            state=str(remote.get('status','')).lower()
            email=str(remote.get('account_email','')).strip()
            if state=='completed':
                message='上游已确认兑换成功，已按本次 Session 预检邮箱自动记账'
                if not email:
                    message+='；上游未提供结果邮箱'
                elif email.casefold()!=task['email'].casefold():
                    message+='；上游返回邮箱已保留供查账'
                self._finish_direct_task(task_id,'completed',message,email)
                return
            if state in ('failed','declined','failed_precharge','cancelled'):
                lookup=await provider.lookup(code)
                lookup_status=lookup_state(lookup)
                if lookup_status=='completed':
                    email=str(lookup.get('account_email','')).strip()
                    message='上游已确认兑换成功，已按本次 Session 预检邮箱自动记账'
                    if not email:
                        message+='；上游未提供结果邮箱'
                    elif email.casefold()!=task['email'].casefold():
                        message+='；上游返回邮箱已保留供查账'
                    self._finish_direct_task(task_id,'completed',message,email)
                elif lookup.get('can_resubmit') is True and lookup_status in ('unused','failed'):
                    self._finish_direct_task(task_id,'failed','上游确认本次未完成，卡密已释放；可重新核对后发起',can_retry=True)
                else:
                    self._finish_direct_task(task_id,'blocked','兑换失败但上游未确认卡密已释放；请勿重复提交')
                return
            self.store.inventory_direct_task_update(task_id,status='pending',result_state='pending',
                message='兑换处理中，后台每 3 秒查询原交易；不会重复提交')
            self.direct_poll_after[task_id]=time.monotonic()+3
        except Exception:
            self.store.inventory_direct_task_update(task_id,status='pending',result_state='pending',
                message='暂时无法读取上游结果，后台将继续查询原交易；不会重复提交')
            self.direct_poll_after[task_id]=time.monotonic()+3

    def _finish_direct_task(self,task_id,status,message,email='',can_retry=False,submission_rejected=None):
        task=self.store.charge_tasks.get(task_id) or {}
        fields={'status':status,'message':message,'can_retry':can_retry,'remote_terminal':True,
                'result_state':'completed' if status=='completed' or email else status,
                'result_email':task.get('email','') if status=='completed' else email or '未确认',
                'provider_result_email':email or '上游未提供',
                'partner_redemption_token':'','partner_preflight_token':'','direct_card_secret':''}
        if status=='completed':fields['used_at']=now()
        if submission_rejected is not None:
            fields['submission_rejected']=submission_rejected
        self.store.inventory_direct_task_update(task_id,**fields)

    async def _watch_session_direct_tasks(self):
        stamp=time.monotonic()
        self.direct_quotes={key:value for key,value in self.direct_quotes.items() if value['expires']>stamp}
        for task in list(self.store.charge_tasks.values()):
            if task.get('source')!='session_extract' or not task.get('submitted') or task.get('remote_terminal'):
                continue
            key=task['id']
            running=self.direct_jobs.get(key)
            if running and not running.done():continue
            if stamp<self.direct_poll_after.get(key,0):continue
            self.direct_jobs[key]=asyncio.create_task(self._session_direct_refresh(key))

    def inventory_snapshot(self):
        async def collect():
            state=self.store.inventory_public()
            for card in state['cards']:
                check=getattr(self,'inventory_checks',{}).get(card['id'])
                card['checking']=bool(check and not check.done())
                if card['checking']:
                    card.update(deletable=False,delete_block_reason='卡密正在核对，请稍后删除')
                    card.update(spec_editable=False,spec_edit_block_reason='卡密正在核对，请稍后修改规格')
            for row in state['accounts']:
                key=row['id']
                row['session_info']=self.session_info(key)
                row['session_valid']=row['session_info']['state'] in ('usable','expiring')
                row['busy']=key in self.jobs and not self.jobs[key].done()
                row['recharge_eligible']=row['recharge_eligible'] and not row['busy']
                row['session_refresh_eligible']=not bool(self.session_refresh_reason(key))
                row['session_refresh_block_reason']=self.session_refresh_reason(key)
            session_jobs=[key for key,job in self.jobs.items() if not job.done() and self.job_actions.get(key) in ('session_only','session_refresh')]
            state.update(paused=self.paused,limit=self.limit,session_limit=self.session_limit,
                         session_auto_enabled=self.session_auto_enabled,session_maintenance_error=self.session_maintenance_error,
                         session_active=sum(self.store.get(key)['status']!='queued' for key in session_jobs),
                         session_queued=sum(self.store.get(key)['status']=='queued' for key in session_jobs))
            requests = [item for item in self.edge_requests.values() if item.get('instance_id') and not item['future'].done()]
            state['isolation'] = {'enabled':True,'max_concurrency':10,'active_instances':len(requests),
                                  'connected_instances':sum(time.monotonic()-item['private'].get('helper_seen',float('-inf'))<15 for item in requests)}
            return state
        return self._inventory_call(collect())

    def inventory_command(self, operation, data):
        async def perform():
            ids=data.get('ids',[])
            if operation=='refresh-sessions':
                return await self.refresh_eligible_sessions(force=True)
            if operation=='session-auto':
                enabled=data.get('enabled')
                if not isinstance(enabled,bool):
                    raise ValueError('请选择是否开启自动更新')
                with self.store.lock,self.store.db:
                    self.store.db.execute('INSERT OR REPLACE INTO inventory_meta VALUES (?,?)',('session-auto-refresh','true' if enabled else 'false'))
                self.session_auto_enabled=enabled
                self.session_maintenance_error=''
                if not enabled:
                    for key,job in self.jobs.items():
                        if not job.done() and self.job_actions.get(key)=='session_refresh' and self.session_refresh_sources.get(key)=='auto':
                            self.session_refresh_sources[key]='disabled'
                            job.cancel()
                            if self.store.get(key)['status']=='queued':
                                self.store.update(key,status='idle',message='自动更新已关闭')
                return dict(enabled=enabled,**(await self.refresh_eligible_sessions() if enabled else {}))
            if operation=='copy-account':
                key=data.get('id')
                if not isinstance(key,str):
                    raise ValueError('请选择一个账号')
                row=self.store.get(key)
                password=crypt(row['password'],True) if row.get('password') else ''
                totp=crypt(row['totp'],True) if row.get('totp') else ''
                # Explicit access only: never add credentials to polling snapshots or history.
                return {'text':'\n'.join((
                    '邮箱账号：'+row['email'],
                    'GPT密码：'+(password or '未配置'),
                    '2FA密钥：'+(totp or '未配置'),
                    '在线获取2FA网站：https://2fa.vip/index.html',
                ))}
            if operation=='risk-accounts':
                busy_ids={key for key,job in self.jobs.items() if not job.done() and not (self.job_actions.get(key)=='session_refresh' and self.store.get(key)['status']=='queued')}
                result=self.store.inventory_import_accounts(data.get('items'),data.get('group',''),risk=True,busy_ids=busy_ids)
                for key in result['ids'] + result['moved_ids']:
                    self.tickets.pop(key,None)
                return result
            if operation=='risk-pool':
                if not isinstance(ids,list) or not 1<=len(ids)<=500 or len(ids)!=len(set(ids)) or not isinstance(data.get('active'),bool):
                    raise ValueError('请选择账号和有效的移入或移回操作')
                rows=[self.store.get(key) for key in ids]
                for row in rows:
                    if row['id'] in self.jobs and not self.jobs[row['id']].done() and not (self.job_actions.get(row['id'])=='session_refresh' and row['status']=='queued'):
                        raise ValueError('账号仍在处理，请等待任务结束后移动')
                    if bool(row.get('risk',{}).get('active'))==data['active']:
                        raise ValueError('账号池状态已经变化，请刷新列表')
                for row in rows:
                    if data['active']:self.store.quarantine_account(row['id'],'manual')
                    else:self.store.restore_account(row['id'])
                    self.tickets.pop(row['id'],None)
                return {'ok':True,'count':len(rows)}
            if operation=='edit-account':
                key=data.get('id')
                if not isinstance(key,str) or data.get('code'):
                    raise ValueError('请选择账号，卡密请单独管理')
                if key in self.jobs and not self.jobs[key].done():
                    raise ValueError('该账号正在处理，请稍后修改')
                row=self.store.get(key)
                task=self.store.charge_tasks.get(row.get('active_task_id'))
                if task_unresolved(task):
                    raise ValueError('该账号有未结束的充值，请先查询原任务')
                self.store.inventory_session_slot(key)
                self.store.edit_account(key,data)
                if 'eligibility_months' in data and data['eligibility_months']=='':
                    self.store.update(key,eligibility_months='')
                self.tickets.pop(key,None)
                self.store.update(key,status='ready' if key in self.sessions else 'idle',message='账号资料已保存')
                return {'ok':True}
            if operation=='quote':
                if not isinstance(ids,list):
                    raise ValueError('请选择账号')
                if any(key in self.jobs and not self.jobs[key].done() for key in ids):
                    raise ValueError('所选账号仍在处理，请等待或先停止')
                assignments=self.store.inventory_quote(ids,data.get('sku'))
                if not hasattr(self,'inventory_quotes'):
                    self.inventory_quotes={}
                self.inventory_quotes={key:value for key,value in self.inventory_quotes.items() if value['expires']>time.monotonic()}
                nonce=secrets.token_urlsafe(24)
                self.inventory_quotes[nonce]={'assignments':assignments,'expires':time.monotonic()+120}
                return {'quote':nonce,'assignments':assignments}
            if operation=='start':
                nonce=data.get('quote')
                if not isinstance(nonce,str):
                    raise ValueError('请先查看分配并确认')
                quote=getattr(self,'inventory_quotes',{}).pop(nonce,None)
                if not quote or quote['expires']<=time.monotonic():
                    raise ValueError('分配预览已过期，请重新确认')
                account_ids=[item['account_id'] for item in quote['assignments']]
                if any(key in self.jobs and not self.jobs[key].done() for key in account_ids):
                    raise ValueError('账号状态已变化，请重新确认')
                tasks=self.store.inventory_reserve(quote['assignments'])
                try:
                    await self._command('redeem_auto',account_ids,1)
                except Exception:
                    for key in account_ids:
                        self.store.update(key,status='error',message='任务未启动，已释放本地卡密')
                    raise
                return {'ok':True,'tasks':tasks}
            if operation=='session':
                if not isinstance(ids,list) or not 1<=len(ids)<=500:
                    raise ValueError('请选择账号')
                for key in ids:
                    if key in self.jobs and not self.jobs[key].done():
                        raise ValueError('所选账号有任务处理中')
                    row=self.store.get(key)
                    task=self.store.charge_tasks.get(row.get('active_task_id'))
                    if task_unresolved(task):
                        raise ValueError('所选账号仍有充值任务处理中')
                for key in ids:
                    self.store.inventory_session_slot(key)
                return await self._command('session_only',ids,1)
            if operation=='task':
                task=self.store.charge_tasks.get(data.get('id'))
                action=data.get('action')
                if not task or action not in ('refresh','stop'):
                    raise ValueError('请选择有效任务和操作')
                if task.get('source')=='session_extract':
                    raise ValueError('Session 直充任务请使用原交易查询操作')
                row=self.store.get(task['account_id'])
                if row.get('active_task_id')!=task['id'] or row.get('active_card_id')!=task['card_id']:
                    raise ValueError('这是历史任务，请查看记录或到卡密管理核对状态')
                if action=='stop' and (task['account_id'] not in self.jobs or self.jobs[task['account_id']].done()):
                    raise ValueError('该任务已经停止')
                if action=='refresh':
                    # Reattach only the same transaction; never allocate or submit again.
                    self.store.update(row['id'],execution_mode='recharge')
                return await self._command(action,[row['id']],1)
            if operation=='check-cards':
                if (not isinstance(ids,list) or not 1<=len(ids)<=100 or
                        any(not isinstance(key,str) or key not in self.store.cards or self.store.cards[key].get('archived') for key in ids)):
                    raise ValueError('每次可核对 1-100 张卡密')
                if not hasattr(self,'inventory_checks'):
                    self.inventory_checks={}
                for key in ids:
                    if key not in self.inventory_checks or self.inventory_checks[key].done():
                        self.inventory_checks[key]=asyncio.create_task(self._inventory_check_card(key))
                return {'ok':True}
            if operation=='card-detail':
                return self.store.inventory_card_detail(data.get('id'))
            if operation=='edit-card-spec':
                key=data.get('id')
                if not isinstance(key,str):
                    raise ValueError('请选择卡密')
                check=getattr(self,'inventory_checks',{}).get(key)
                if check and not check.done():
                    raise ValueError('卡密正在核对，请稍后修改规格')
                return self.store.inventory_edit_card_spec(key,data.get('sku'),data.get('expected_sku'))
            if operation=='delete':
                if not isinstance(ids,list) or any(not isinstance(key,str) for key in ids):
                    raise ValueError('请选择要删除的条目')
                if data.get('kind')=='accounts' and any(key in self.jobs and not self.jobs[key].done() for key in ids):
                    raise ValueError('处理中账号不可删除')
                if data.get('kind')=='cards' and any(key in getattr(self,'inventory_checks',{}) and not self.inventory_checks[key].done() for key in ids):
                    raise ValueError('卡密正在核对，整批未删除，请稍后再试')
                result=self.store.inventory_delete(data.get('kind'),ids)
                if data.get('kind')=='accounts':
                    for key in ids:
                        self.forget_session_memory(key)
                return result
            raise ValueError('未知库存操作')
        return self._inventory_call(perform())

    async def _inventory_check_card(self, key):
        if not hasattr(self,'inventory_check_gate'):
            self.inventory_check_gate=asyncio.Semaphore(2)
        async with self.inventory_check_gate:
            await self._inventory_check_card_now(key)

    async def _inventory_check_card_now(self, key):
        try:
            card=self.store.cards[key]
            task=self.store.charge_tasks.get(card.get('task_id'))
            if task_unresolved(task):
                row=self.store.get(task['account_id'])
                if (row.get('active_task_id')==task['id'] and
                        not (row['id'] in self.jobs and not self.jobs[row['id']].done())):
                    self.store.update(row['id'],execution_mode='recharge')
                    await self.refresh(row['id'])
                return
            remote=await (await self.provider('stock-'+key)).lookup(crypt(card['secret'],True))
            self.store.inventory_card_result(key,remote)
        except Exception as exc:
            # Classify failures without exposing credentials or response bodies.
            if isinstance(exc, PartnerAPIError):
                code = 'CARD_NOT_FOUND' if exc.status == 404 else 'INVALID_LOOKUP_REQUEST' if exc.status in (400, 422) else exc.code if exc.code in {
                    'AUTHENTICATION_FAILED','RATE_LIMITED','UPSTREAM_TIMEOUT',
                    'UPSTREAM_UNAVAILABLE','NETWORK_ERROR','INVALID_RESPONSE',
                    'REQUEST_REJECTED'} else 'REQUEST_REJECTED'
                messages = {
                    'AUTHENTICATION_FAILED':'Partner API 授权失败，请检查本机授权配置',
                    'RATE_LIMITED':'兑换服务限流，稍后再核对',
                    'UPSTREAM_TIMEOUT':'兑换服务响应超时，未改变本地卡密状态',
                    'UPSTREAM_UNAVAILABLE':'兑换服务暂不可用，未改变本地卡密状态',
                    'NETWORK_ERROR':'兑换服务网络暂不可用，未改变本地卡密状态',
                    'INVALID_RESPONSE':'兑换服务返回格式异常，未改变本地卡密状态',
                    'REQUEST_REJECTED':'兑换服务拒绝了核对请求，未改变本地卡密状态',
                    'CARD_NOT_FOUND':'上游查无此卡密或未识别卡密，请核对卡密和服务配置',
                    'INVALID_LOOKUP_REQUEST':'上游拒绝核对请求，未改变本地卡密状态',
                }
                error_code, message = code, messages[code]
            elif isinstance(exc, PartnerConfigurationError):
                error_code = 'PARTNER_CONFIGURATION_MISSING'
                message = '未配置 CDK_PARTNER_PASSWORD。请在 Windows 用户环境变量中设置后，完全退出并重新启动工作台。'
            elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                error_code, message = 'NETWORK_ERROR', '兑换服务网络暂不可用，未改变本地卡密状态'
            elif isinstance(exc, RuntimeError) and '无法精确关联' in str(exc):
                error_code, message = 'INVALID_RESPONSE', '上游返回的卡密与本次核对不一致，未改变本地卡密状态'
            else:
                error_code, message = 'CHECK_FAILED', '核对暂未成功，未改变本地卡密状态，请稍后重试'
            with self.store.lock:
                card=dict(self.store.cards.get(key,{}))
                if not card:
                    return
                card['check_error_code']=error_code
                card['check_error']=message
                self.store.cards[key]=card
                self.store._inventory_write('cards',card)
                self.store.db.commit()
