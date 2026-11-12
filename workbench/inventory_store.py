"""Independent stock and immutable recharge identities, alongside account execution slots."""
import hashlib
import json
import re
import uuid
from .store import crypt, eligibility, mail_address, mask, mask_email, now
from .products import PRODUCTS

ACTIVE = {'queued', 'running', 'login', 'ready', 'preflight', 'confirm', 'pending'}


def attribution_pending_task(task):
    """Recognize current and legacy successful-use records awaiting attribution."""
    if (not task or task.get('manual_resolution') or task.get('status') != 'blocked'
            or not task.get('submitted')):
        return False
    if task.get('attribution_pending'):
        return True
    message = str(task.get('message', ''))
    return any(marker in message for marker in ('邮箱不一致', '其他邮箱', '返回邮箱'))


def direct_success_attribution_pending_task(task):
    if (not task or task.get('source') != 'session_extract' or not task.get('submitted')
            or task.get('status') != 'blocked'):
        return False
    message = str(task.get('message', ''))
    return task.get('result_state') == 'completed' or any(
        marker in message for marker in ('上游确认卡密已使用', '上游邮箱与 Session'))


def task_unresolved(task):
    if task and (task.get('manual_resolution') or task.get('attribution_pending')):
        return False
    return bool(task and (task['status'] in ACTIVE or
                task.get('submitted') and task['status']!='completed' and not task.get('can_retry')))


def public_task(task):
    """Return task status without Partner API credentials or transaction tickets."""
    hidden = {'partner_redemption_token', 'partner_preflight_token', 'direct_card_secret', 'card_digest',
              'submission_rejected'}
    return {key: value for key, value in task.items() if key not in hidden}


class InventoryStoreMixin:
    def _resequence_accounts(self):
        """Keep the account-pool numbering contiguous after deletions."""
        ordered = sorted(self.rows.values(), key=lambda row: (
            row.get('sequence') or 0, row.get('created') or '', row['id']))
        for sequence, row in enumerate(ordered, 1):
            if row.get('sequence') == sequence:
                continue
            row['sequence'] = sequence
            self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?)',
                            (row['id'], json.dumps(row, ensure_ascii=False)))

    def _resequence_cards(self):
        """Keep visible card-management rows numbered from one.

        Archived used cards remain in storage for recharge history, but are not
        part of the card-management list and therefore do not consume a number.
        """
        ordered = sorted((card for card in self.cards.values() if not card.get('archived')),
                         key=lambda card: (card.get('sequence') or 0,
                                           card.get('created') or '', card['id']))
        for sequence, card in enumerate(ordered, 1):
            if card.get('sequence') == sequence:
                continue
            card['sequence'] = sequence
            self._inventory_write('cards', card)

    def _inventory_write(self, table, document):
        if table not in ('cards', 'recharge_tasks'):
            raise ValueError('Unknown inventory collection')
        self.db.execute(f'INSERT OR REPLACE INTO {table} VALUES (?,?)',
                        (document['id'], json.dumps(document, ensure_ascii=False)))

    def init_inventory(self):
        for table in ('cards', 'recharge_tasks'):
            self.db.execute(f'CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY, document TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS session_credentials(account_id TEXT PRIMARY KEY, credential TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS inventory_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.cards = {key: json.loads(doc) for key, doc in self.db.execute('SELECT id,document FROM cards')}
        self.charge_tasks = {key: json.loads(doc) for key, doc in self.db.execute('SELECT id,document FROM recharge_tasks')}
        for task in self.charge_tasks.values():
            task.setdefault('client_request_id', '')
            task.setdefault('partner_redemption_token', '')
            task.setdefault('partner_preflight_token', '')
        if not self.db.execute("SELECT 1 FROM inventory_meta WHERE key='schema-v2'").fetchone():
            canonical = {}
            for source in sorted(list(self.rows.values()), key=lambda row: (row['created'], row['id'])):
                folded = source['email'].strip().casefold()
                account = canonical.get(folded)
                if account is None:
                    account = source
                    account['sequence'] = len(canonical) + 1
                    canonical[folded] = account
                else:
                    for field in ('password', 'totp', 'mail_url'):
                        if not crypt(account.get(field, crypt('')), True) and crypt(source.get(field, crypt('')), True):
                            account[field] = source[field]
                code = crypt(source['secret'], True)
                if code and not source.get('probe_only') and not code.startswith('LOCAL-PROBE-'):
                    digest = hashlib.sha256(code.encode()).hexdigest()
                    card = next((item for item in self.cards.values() if item['digest'] == digest), None)
                    if card is None:
                        card = dict(id=uuid.uuid4().hex, sequence=len(self.cards)+1, secret=source['secret'],
                                    digest=digest, masked=mask(code), sku='plus', created=source['created'],
                                    updated=source['updated'], status='unused', source='legacy-plus')
                        self.cards[card['id']] = card
                    if source.get('history') or source.get('submitted'):
                        task = dict(id=uuid.uuid4().hex, sequence=len(self.charge_tasks)+1,
                                    account_id=account['id'], email=account['email'], card_id=card['id'],
                                    card=card['masked'], sku='plus', status=source['status'],
                                    submitted=bool(source.get('submitted')), can_retry=bool(source.get('can_retry')),
                                    created=source['created'], updated=source['updated'],
                                    message=source.get('message',''), history=source.get('history',[]),
                                    precheck=source.get('precheck',{}), legacy=True)
                        if not task['submitted'] and task['status'] in ACTIVE:
                            task['status'] = 'stopped'
                        self.charge_tasks[task['id']] = task
                        if task['status'] == 'completed':
                            card.update(status='used', used_at=task['updated'], task_id=task['id'])
                            account['last_recharge'] = {'task_id':task['id'],'status':'completed','sku':'plus','time':task['updated']}
                            account['last_successful_recharge'] = dict(account['last_recharge'])
                        elif task['submitted']:
                            card.update(status='reserved', task_id=task['id'])
                            account.update(active_task_id=task['id'], active_card_id=card['id'],
                                           execution_mode='recharge', secret=source['secret'],
                                           card=card['masked'], probe_only=False, submitted=True, status='pending')
                if account is not source:
                    self.rows.pop(source['id'], None)
                    self.db.execute('DELETE FROM records WHERE id=?', (source['id'],))
            for account in canonical.values():
                if account.get('execution_mode') != 'recharge':
                    account.update(secret=crypt(''), card='未分配卡密', probe_only=True, submitted=False,
                                   execution_mode='session', active_task_id=None, active_card_id=None)
                self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?)',
                                (account['id'], json.dumps(account, ensure_ascii=False)))
            for card in self.cards.values():
                self._inventory_write('cards', card)
            for task in self.charge_tasks.values():
                self._inventory_write('recharge_tasks', task)
            self.db.execute("INSERT INTO inventory_meta VALUES ('schema-v2','1')")
        for task in self.charge_tasks.values():
            if direct_success_attribution_pending_task(task):
                stamp = now()
                task.update(status='completed', message='程序恢复后已按 Session 预检邮箱自动归属成功',
                            result_state='completed', result_email=task.get('email', ''),
                            attribution_pending=False, remote_terminal=True,
                            can_retry=False, updated=stamp, partner_redemption_token='',
                            partner_preflight_token='', direct_card_secret='')
                task.pop('manual_resolution', None)
                self._inventory_write('recharge_tasks', task)
                continue
            if attribution_pending_task(task):
                stamp = now()
                account = self.rows.get(task.get('account_id'))
                card = self.cards.get(task.get('card_id'))
                target_email = account['email'] if account else task.get('email', '')
                message = '程序恢复后已按本次预检目标邮箱自动归属成功'
                task.update(status='completed', submitted=True, message=message,
                            result_state='completed', result_email=target_email,
                            attribution_pending=False, remote_terminal=True,
                            can_retry=False, updated=stamp)
                task.pop('manual_resolution', None)
                if account:
                    account.update(status='completed', submitted=True, message=message,
                                   current_plan='plus', result_state='completed',
                                   attribution_pending=False, last_recharge={
                                       'task_id':task['id'],'status':'completed',
                                       'sku':task.get('sku','plus'),'time':stamp},
                                   last_successful_recharge={
                                       'task_id':task['id'],'status':'completed',
                                       'sku':task.get('sku','plus'),'time':stamp},
                                   execution_mode='session', updated=stamp)
                    account.pop('manual_resolution', None)
                    self.db.execute('DELETE FROM session_credentials WHERE account_id=?', (account['id'],))
                    self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?)',
                                    (account['id'], json.dumps(account, ensure_ascii=False)))
                if card:
                    card.update(status='used', used_at=card.get('used_at') or stamp,
                                updated=stamp, task_id=task['id'])
                    card.pop('attribution_pending', None)
                    self._inventory_write('cards', card)
                self._inventory_write('recharge_tasks', task)
                continue
            if not task_unresolved(task):
                continue
            if task.get('source') == 'session_extract':
                task.update(status='pending', submitted=True,
                            message='工作台已恢复，请查询原交易；不会重复提交')
                self._inventory_write('recharge_tasks', task)
                continue
            account=self.rows.get(task['account_id'])
            card=self.cards[task['card_id']]
            if task.get('submitted') or account and account.get('submitted'):
                task.update(status='pending',submitted=True,message='程序已恢复，请查询原任务，禁止重复提交')
                card['status']='reserved'
                if account:
                    account.update(status='pending',submitted=True,execution_mode='recharge',
                                   active_task_id=task['id'],active_card_id=card['id'],secret=card['secret'],
                                   card=card['masked'],probe_only=False)
            else:
                task.update(status='stopped',message='程序重启，未提交的任务已停止')
                remote=task.get('precheck',{}).get('card_status')
                card['status']='unused' if remote in (None,'','unused') else 'exception'
                if card['status']=='unused':
                    card.pop('task_id',None)
                if account:
                    account.update(execution_mode='session',status='idle',submitted=False)
            self._inventory_write('cards',card)
            self._inventory_write('recharge_tasks',task)

        # Repair gaps left by older versions so the current visible lists are
        # contiguous before the first page render.
        with self.lock, self.db:
            self._resequence_accounts()
            self._resequence_cards()

    def save_session_credential(self, key, session):
        if not isinstance(key,str) or not isinstance(session,dict):
            raise ValueError('Session 凭证格式不正确')
        encrypted=crypt(json.dumps(session,ensure_ascii=False,separators=(',',':')))
        with self.lock:
            if key not in self.rows:
                raise ValueError('账号不存在，未保存 Session')
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO session_credentials VALUES (?,?)',(key,encrypted))

    def load_session_credentials(self):
        with self.lock:
            records=list(self.db.execute('SELECT account_id,credential FROM session_credentials'))
        sessions={}
        for key,encrypted in records:
            try:
                value=json.loads(crypt(encrypted,True))
            except Exception:
                continue
            if isinstance(value,dict):
                sessions[key]=value
        return sessions

    def delete_session_credential(self, key):
        with self.lock,self.db:
            self.db.execute('DELETE FROM session_credentials WHERE account_id=?',(key,))

    def inventory_update_task(self, key, **fields):
        allowed = {'client_request_id', 'partner_redemption_token', 'partner_preflight_token',
                   'submission_rejected'}
        if set(fields) - allowed:
            raise ValueError('充值任务字段不允许写入')
        with self.lock:
            row = self.get(key)
            task = self.charge_tasks.get(row.get('active_task_id'))
            if not task:
                raise ValueError('充值任务不存在')
            task = dict(task)
            task.update(fields, updated=now())
            self.charge_tasks[task['id']] = task
            with self.db:
                self._inventory_write('recharge_tasks', task)
            return dict(task)

    def inventory_direct_task_create(self, task):
        required = {'id','sequence','email','card','card_digest','sku','status','submitted',
                    'can_retry','created','updated','message','history','source'}
        if not isinstance(task, dict) or not required.issubset(task) or task.get('source') != 'session_extract':
            raise ValueError('直充记录格式不正确')
        with self.lock:
            digest = task['card_digest']
            if any(card.get('digest') == digest for card in self.cards.values()):
                raise ValueError('该卡密已存在于卡密管理中，请从库存充值')
            if any(item.get('card_digest') == digest and not
                   (item.get('source') == 'session_extract' and item.get('status') == 'failed'
                    and item.get('can_retry') is True and item.get('remote_terminal'))
                   for item in self.charge_tasks.values()):
                raise ValueError('该卡密已有独立充值记录，不可重复提交')
            if task['id'] in self.charge_tasks:
                raise ValueError('直充记录已存在')
            task=dict(task)
            task['sequence']=max((item.get('sequence',0) for item in self.charge_tasks.values()),default=0)+1
            with self.db:
                self._inventory_write('recharge_tasks', task)
            self.charge_tasks[task['id']] = dict(task)
            return dict(task)

    def inventory_direct_task_update(self, task_id, **fields):
        allowed = {'status','submitted','can_retry','message','updated','result_state','result_email',
                   'provider_result_email','attribution_pending','remote_terminal','used_at',
                   'client_request_id','partner_redemption_token','partner_preflight_token',
                   'direct_card_secret','precheck','history','submission_rejected'}
        if set(fields) - allowed:
            raise ValueError('直充记录字段不允许写入')
        with self.lock:
            task = self.charge_tasks.get(task_id)
            if not task or task.get('source') != 'session_extract':
                raise ValueError('直充记录不存在')
            task = dict(task)
            stamp = fields.pop('updated', now())
            task.update(fields, updated=stamp)
            with self.db:
                self._inventory_write('recharge_tasks', task)
            self.charge_tasks[task_id] = task
            return public_task(dict(task))
            if account:
                self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?)',(account['id'],json.dumps(account,ensure_ascii=False)))
        self.db.commit()

    def inventory_import_accounts(self, items, default_group='', risk=False, busy_ids=()):
        if not isinstance(items, list) or not 1 <= len(items) <= 500:
            raise ValueError('每批可导入 1-500 个账号')
        prepared, skipped, moves = [], 0, []
        with self.lock:
            existing = {row['email'].casefold():row for row in self.rows.values()}
            emails = set(existing)
            seen = set()
            for item in items:
                if not isinstance(item, dict) or item.get('code'):
                    raise ValueError('账号资料不应包含卡密，请到卡密管理单独导入')
                email = str(item.get('email','')).strip()
                if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                    raise ValueError('账号中有邮箱格式不正确，整批未导入')
                group = eligibility(item.get('eligibility_months', default_group))
                link = mail_address(item.get('mail_url'))
                for field in ('password','totp'):
                    if not isinstance(item.get(field,''), str) or len(item.get(field,'')) > 4096:
                        raise ValueError('账号资料格式或长度不正确')
                if email.casefold() in emails:
                    row = existing.get(email.casefold())
                    if risk and row and not row.get('risk',{}).get('active') and email.casefold() not in seen:
                        if row['id'] in busy_ids:
                            raise ValueError('账号仍在处理，整批未添加；请等待任务结束后移动')
                        moves.append(row['id'])
                    else:
                        skipped += 1
                    seen.add(email.casefold())
                    continue
                emails.add(email.casefold())
                seen.add(email.casefold())
                prepared.append(dict(item, email=email, mail_url=link, eligibility_months=group))
            if prepared:
                start = max((row.get('sequence',0) for row in self.rows.values()), default=0)
                ids = self.add_many(prepared)
                for index, key in enumerate(ids, 1):
                    self.rows[key].update(sequence=start+index, execution_mode='session',
                                          active_task_id=None, active_card_id=None)
                    self._save(self.rows[key])
            else:
                ids = []
            if risk:
                for key in ids + moves:
                    self.quarantine_account(key,'manual')
            return {'ok':True,'ids':ids,'imported':len(ids),'moved':len(moves),'moved_ids':moves,'skipped':skipped}

    def inventory_import_cards(self, sku, codes):
        if sku not in PRODUCTS or not isinstance(codes,list) or not 1 <= len(codes) <= 500:
            raise ValueError('请选择卡密规格，每批导入 1-500 张')
        with self.lock:
            digests = {card['digest'] for card in self.cards.values()}
            prepared, skipped = [], 0
            sequence = max((card['sequence'] for card in self.cards.values()
                            if not card.get('archived')), default=0)
            for value in codes:
                if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{3,255}',value.strip()):
                    raise ValueError('卡密中有无法识别的内容，整批未导入')
                value = value.strip()
                digest = hashlib.sha256(value.encode()).hexdigest()
                if digest in digests:
                    existing = next((card for card in self.cards.values() if card['digest']==digest),None)
                    if existing and existing['sku'] != sku:
                        raise ValueError('已有卡密属于其他规格，请核对后重新导入')
                    skipped += 1
                    continue
                digests.add(digest)
                sequence += 1
                prepared.append(dict(id=uuid.uuid4().hex,sequence=sequence,secret=crypt(value),digest=digest,
                                     masked=mask(value),sku=sku,status='unused',created=now(),updated=now()))
            with self.db:
                for card in prepared:
                    self._inventory_write('cards',card)
            self.cards.update({card['id']:card for card in prepared})
            return {'ok':True,'imported':len(prepared),'skipped':skipped}

    def inventory_quote(self, ids, sku):
        if sku not in PRODUCTS or not PRODUCTS[sku]['redeem_enabled']:
            raise ValueError('该规格可管理库存，实际兑换接口尚未确认；目前支持 Plus 和半价 Plus 充值')
        if not isinstance(ids,list) or not 1 <= len(ids) <= 500 or len(ids)!=len(set(ids)):
            raise ValueError('请选择 1-500 个不同账号')
        with self.lock:
            accounts = [self.get(key) for key in ids]
            for row in accounts:
                self.ensure_recharge_allowed(row['id'])
                task = self.charge_tasks.get(row.get('active_task_id'))
                if task_unresolved(task):
                    raise ValueError('所选账号有尚未结束的充值，请先查询原任务')
            stock = sorted((card for card in self.cards.values() if not card.get('archived') and card['sku']==sku and card['status']=='unused'),
                           key=lambda card:card['sequence'])
            if len(stock) < len(accounts):
                raise ValueError(f'可用卡密不足：需要 {len(accounts)} 张，当前只有 {len(stock)} 张')
            return [dict(account_id=row['id'],email=row['email'],card_id=card['id'],card=card['masked'],sku=sku)
                    for row,card in zip(accounts,stock)]

    def inventory_reserve(self, assignments):
        with self.lock:
            if not assignments:
                raise ValueError('充值预览已失效，请重新选择')
            ids = [item['account_id'] for item in assignments]
            cards = [item['card_id'] for item in assignments]
            if len(ids)!=len(set(ids)) or len(cards)!=len(set(cards)):
                raise ValueError('账号或卡密分配重复')
            for item in assignments:
                row = self.get(item['account_id'])
                self.ensure_recharge_allowed(row['id'])
                task = self.charge_tasks.get(row.get('active_task_id'))
                card = self.cards.get(item['card_id'])
                if (not card or card.get('archived') or card['status']!='unused' or card['sku']!=item['sku']
                        or not PRODUCTS[item['sku']]['redeem_enabled'] or task_unresolved(task)):
                    raise ValueError('账号或库存状态发生变化，请重新确认分配')
            prepared = []
            sequence = max((task['sequence'] for task in self.charge_tasks.values()),default=0)
            for offset,item in enumerate(assignments,1):
                row = self.get(item['account_id'])
                card = dict(self.cards[item['card_id']])
                task = dict(id=uuid.uuid4().hex,sequence=sequence+offset,account_id=row['id'],email=row['email'],
                            card_id=card['id'],card=card['masked'],sku=card['sku'],status='queued',submitted=False,
                            can_retry=False,created=now(),updated=now(),message='等待获取 Session',history=[],precheck={},
                            client_request_id='',
                            partner_redemption_token='', partner_preflight_token='')
                card.update(status='reserved',task_id=task['id'],updated=now())
                row.update(active_task_id=task['id'],active_card_id=card['id'],execution_mode='recharge',
                           secret=card['secret'],card=card['masked'],probe_only=False,submitted=False,
                           can_retry=False,status='idle',precheck={},history=[],plan='未检测')
                prepared.append((row,card,task))
            with self.db:
                for row,card,task in prepared:
                    self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?)',(row['id'],json.dumps(row,ensure_ascii=False)))
                    self._inventory_write('cards',card)
                    self._inventory_write('recharge_tasks',task)
            for row,card,task in prepared:
                self.rows[row['id']]=row
                self.cards[card['id']]=card
                self.charge_tasks[task['id']]=task
            return [task['id'] for _,_,task in prepared]

    def sync_inventory(self, row):
        if row.get('execution_mode')!='recharge':
            return
        task = self.charge_tasks.get(row.get('active_task_id'))
        if not task:
            return
        task = dict(task)
        for field in ('status','submitted','can_retry','message','history','precheck','updated',
                      'result_state','result_email','provider_result_email','attribution_pending',
                      'manual_resolution','result_verified_at','remote_terminal'):
            if field in row:
                task[field]=row[field]
        if row.get('risk',{}).get('active'):
            task['risk']={key:row['risk'][key] for key in ('code','reason','since')}
        card = dict(self.cards[task['card_id']])
        status = row['status']
        if status=='completed':
            card.update(status='used',used_at=now())
            card.pop('attribution_pending',None)
            row['last_recharge']={'task_id':task['id'],'status':'completed','sku':task['sku'],'time':now()}
            row['last_successful_recharge']=dict(row['last_recharge'])
            row['current_plan']='plus'
            row['execution_mode']='session'
            self.db.execute('DELETE FROM session_credentials WHERE account_id=?',(row['id'],))
        elif row.get('attribution_pending') and row.get('result_state')=='completed':
            card.update(status='used', used_at=card.get('used_at') or now(), attribution_pending=True)
        elif status in ('error','stopped','failed','blocked') and not row.get('submitted'):
            remote = row.get('precheck',{}).get('card_status')
            card['status'] = 'unused' if remote in (None,'','unused') or row.get('can_retry') else 'used' if remote=='completed' else 'exception'
            card.pop('attribution_pending',None)
            if card['status']=='unused':
                card.pop('task_id',None)
            row['last_recharge']={'task_id':task['id'],'status':status,'sku':task['sku'],'time':now()}
            row['execution_mode']='session'
        else:
            card['status']='reserved'
        card['updated']=now()
        self.cards[card['id']]=card
        self.charge_tasks[task['id']]=task
        self._inventory_write('cards',card)
        self._inventory_write('recharge_tasks',task)

    def inventory_session_slot(self, key):
        with self.lock:
            row=self.get(key)
            task=self.charge_tasks.get(row.get('active_task_id'))
            if task_unresolved(task):
                raise ValueError('该账号仍有充值处理中，请先查询原任务')
            self.update(key,execution_mode='session',active_task_id=None,active_card_id=None,
                        secret=crypt(''),card='未分配卡密',probe_only=True,submitted=False,can_retry=False,precheck={})

    def inventory_public(self):
        with self.lock:
            accounts=[]
            for row in self.public():
                fields=('id','email','sequence','eligibility_months','status','message','created','updated',
                        'password_configured','totp_configured','mail_configured','last_recharge','last_successful_recharge','current_plan')
                account={key:row.get(key) for key in fields}
                account['risk']=row.get('risk',{})
                account['has_recharged']=self.has_successful_recharge(row)
                reason=self.recharge_block_reason(row)
                if not reason and task_unresolved(self.charge_tasks.get(row.get('active_task_id'))):
                    reason='账号仍有充值处理中，请先查询原任务'
                account.update(recharge_eligible=not bool(reason),recharge_block_reason=reason)
                accounts.append(account)
            account_order={False:0,True:0}
            for account in sorted(accounts,key=lambda row:(bool(row.get('risk',{}).get('active')),row.get('sequence') or 0,row['created'])):
                pool=bool(account.get('risk',{}).get('active'))
                account_order[pool]+=1
                account['display_sequence']=account_order[pool]
            cards=[]
            for card in self.cards.values():
                if card.get('archived'):
                    continue
                public={key:value for key,value in card.items() if key not in (
                    'secret','digest','provider_account_email','provider_product',
                    'provider_plan','provider_used_at')}
                reason=self.inventory_card_delete_reason(card)
                public.update(deletable=not bool(reason),delete_block_reason=reason)
                edit_reason=self.inventory_card_spec_reason(card)
                public.update(spec_editable=not bool(edit_reason),spec_edit_block_reason=edit_reason)
                cards.append(public)
            card_order={}
            for card in sorted(cards,key=lambda row:(row.get('sku') or '',row.get('sequence') or 0,row.get('created') or '')):
                sku=card.get('sku') or ''
                card_order[sku]=card_order.get(sku,0)+1
                card['display_sequence']=card_order[sku]
            return {'accounts':sorted(accounts,key=lambda row:(row.get('sequence') or 0,row['created'])),
                    'cards':sorted(cards,key=lambda card:card['sequence']),
                    'tasks':sorted((public_task(json.loads(json.dumps(task))) for task in self.charge_tasks.values()),key=lambda task:task['sequence'],reverse=True),
                    'products':PRODUCTS}

    def inventory_card_result(self, key, remote):
        from .provider import lookup_state
        with self.lock:
            card=dict(self.cards[key])
            if card.get('archived'):
                raise ValueError('卡密已从库存移除')
            if not isinstance(remote, dict):
                raise ValueError('上游核对结果格式异常')
            state=lookup_state(remote)
            task=self.charge_tasks.get(card.get('task_id'))
            if task_unresolved(task):
                raise ValueError('卡密有正在处理的充值，请查询对应任务')
            if card['status']=='used' and state!='completed':
                card.update(checked_at=now(), updated=now(), check_error_code='STATE_CONFLICT',
                            check_error='此前已确认使用，本次核对结果不一致，保持已使用状态')
                self.cards[key]=card
                self._inventory_write('cards',card)
                self.db.commit()
                return
            provider_email = mask_email(remote.get('account_email'))
            provider_product = remote.get('product') if isinstance(remote.get('product'), str) else ''
            provider_plan = remote.get('plan') if isinstance(remote.get('plan'), str) else ''
            provider_used_at = remote.get('used_at') if isinstance(remote.get('used_at'), str) else ''
            for field, value, limit in (
                ('provider_product', provider_product.strip(), 128),
                ('provider_plan', provider_plan.strip(), 128),
                ('provider_used_at', provider_used_at.strip(), 64),
            ):
                if value and len(value) <= limit:
                    card[field] = value
            if provider_email:
                card['provider_account_email'] = provider_email
            can_release = remote.get('can_resubmit') is True
            card['status']=('used' if state=='completed' else 'unused' if
                            (state=='unused' and can_release) or
                            (state=='failed' and can_release) else
                            'reserved' if state=='pending' else 'exception')
            card.update(checked_at=now(),updated=now())
            card.pop('check_error',None)
            card.pop('check_error_code',None)
            if card['status']=='exception':
                card.update(check_error_code='NOT_RESUBMITTABLE',
                            check_error='上游未明确允许重新兑换，卡密保留为异常状态')
            if card['status']=='unused':
                card.pop('task_id',None)
            self.cards[key]=card
            self._inventory_write('cards',card)
            self.db.commit()

    def inventory_card_detail(self, key):
        """Decrypt one card only after an explicit detail request.

        The regular inventory snapshot never includes ``secret``. This method is
        intentionally separate so a full code cannot enter polling snapshots,
        exports, or ordinary logs by accident.
        """
        if not isinstance(key, str):
            raise ValueError('请选择卡密')
        with self.lock:
            card = self.cards.get(key)
            if not card or card.get('archived'):
                raise ValueError('卡密不存在或已从库存移除')
            code = crypt(card.get('secret', ''), True)
            if not code:
                raise ValueError('该卡密没有可查看的完整值')
            detail = {field: card.get(field) for field in (
                'id','sequence','masked','sku','status','created','updated',
                'checked_at','used_at','provider_account_email','provider_product',
                'provider_plan','provider_used_at','task_id','check_error',
                'check_error_code')}
            detail['code'] = code
            return detail

    def inventory_card_spec_reason(self, card):
        if card.get('archived'):
            return '卡密已从库存移除'
        if any(task.get('card_id')==card['id'] and (task_unresolved(task) or task['status']=='completed')
               for task in self.charge_tasks.values()):
            return '卡密已有成功记录或存在处理中、待核实的充值，不可修改规格'
        if card['status']!='unused':
            return '仅未使用且已释放的卡密可修改规格'
        return ''

    def inventory_edit_card_spec(self, key, sku, expected_sku):
        if not isinstance(key,str) or not isinstance(sku,str) or sku not in PRODUCTS or not isinstance(expected_sku,str):
            raise ValueError('请选择卡密和有效规格')
        with self.lock:
            card=self.cards.get(key)
            if not card:
                raise ValueError('卡密不存在')
            reason=self.inventory_card_spec_reason(card)
            if reason:
                raise ValueError(reason)
            if card['sku']!=expected_sku:
                raise ValueError('卡密规格已变化，请刷新后重新确认')
            if card['sku']==sku:
                raise ValueError('请选择不同的卡密规格')
            updated=dict(card,sku=sku,updated=now(),
                         last_spec_change={'from':card['sku'],'to':sku,'time':now()})
            with self.db:
                self._inventory_write('cards',updated)
            self.cards[key]=updated
            return {'ok':True,'id':key,'sku':sku}

    def inventory_card_delete_reason(self, card):
        if card.get('archived'):
            return '卡密已从库存移除'
        if any(task.get('card_id')==card['id'] and task_unresolved(task) for task in self.charge_tasks.values()):
            return '卡密有处理中或待核实的充值，请先查询原任务'
        if card['status'] not in ('unused','used'):
            return '占用中或状态异常的卡密，请先核对结果'
        return ''

    def inventory_delete(self, kind, ids):
        if (kind not in ('accounts','cards') or not isinstance(ids,list) or not 1<=len(ids)<=500
                or any(not isinstance(key,str) for key in ids) or len(ids)!=len(set(ids))):
            raise ValueError('请选择要删除的条目')
        with self.lock:
            if kind=='accounts':
                for key in ids:
                    row=self.get(key)
                    task=self.charge_tasks.get(row.get('active_task_id'))
                    if task_unresolved(task):
                        raise ValueError('处理中账号不可删除')
                with self.db:
                    for key in ids:
                        self.rows.pop(key)
                        self.db.execute('DELETE FROM records WHERE id=?',(key,))
                        self.db.execute('DELETE FROM session_credentials WHERE account_id=?',(key,))
                    self._resequence_accounts()
            else:
                for key in ids:
                    if key not in self.cards:
                        raise ValueError('卡密已不存在，请刷新列表')
                    reason=self.inventory_card_delete_reason(self.cards[key])
                    if reason:
                        raise ValueError(reason)
                with self.db:
                    for key in ids:
                        if self.cards[key]['status']=='used':
                            card=dict(self.cards[key],archived=True,archived_at=now(),updated=now())
                            self._inventory_write('cards',card)
                            self.cards[key]=card
                        else:
                            self.db.execute('DELETE FROM cards WHERE id=?',(key,))
                            self.cards.pop(key)
                    self._resequence_cards()
            return {'ok':True,'deleted':len(ids)}
