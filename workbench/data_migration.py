"""Merge pre-existing v2 inventories once, without resetting history or consuming cards."""
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .store import crypt
from .inventory_store import task_unresolved,ACTIVE


def backup_database(path, backup_root, label):
    path, backup_root = Path(path).resolve(), Path(backup_root).resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    destination = backup_root / f'{label}-{stamp}.sqlite3'
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
    return destination


def merge_v2_database(store, source_path):
    source_path = Path(source_path).resolve()
    if not source_path.is_file() or source_path == store.path:
        return {'accounts':0,'cards':0,'tasks':0,'skipped':True}
    marker = 'import-v2:' + hashlib.sha256(str(source_path).casefold().encode()).hexdigest()
    with store.lock:
        if store.db.execute('SELECT 1 FROM inventory_meta WHERE key=?',(marker,)).fetchone():
            return {'accounts':0,'cards':0,'tasks':0,'skipped':True}
        with closing(sqlite3.connect(source_path.as_uri()+'?mode=ro',uri=True)) as source:
            tables={row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'records','cards','recharge_tasks','inventory_meta'} <= tables:
                raise ValueError('发现旧格式数据，请先保留旧版并迁移到 v2，未创建空库替代旧数据')
            documents={table:[json.loads(row[0]) for row in source.execute(f'SELECT document FROM {table}')]
                       for table in ('records','cards','recharge_tasks')}

        # Everything below is prepared in copies, so a conflict cannot partly import.
        accounts=json.loads(json.dumps(store.rows))
        cards=json.loads(json.dumps(store.cards))
        tasks=json.loads(json.dumps(store.charge_tasks))
        counts={'accounts':0,'cards':0,'tasks':0,'skipped':False}
        account_map,card_map={},{}
        by_email={row['email'].strip().casefold():row for row in accounts.values()}
        next_sequence=max((row.get('sequence',0) for row in accounts.values()),default=0)
        for incoming in sorted(documents['records'],key=lambda row:(row.get('sequence',0),row['id'])):
            folded=incoming['email'].strip().casefold()
            existing=by_email.get(folded)
            if existing:
                account_map[incoming['id']]=existing['id']
                incoming_newer=incoming.get('updated','')>existing.get('updated','')
                for field in ('password','totp','mail_url'):
                    if incoming.get(field) and crypt(incoming[field],True) and (incoming_newer or not crypt(existing.get(field,crypt('')),True)):
                        existing[field]=incoming[field]
                if incoming_newer:
                    for field in ('eligibility_months','current_plan','subscription','active_until'):
                        if incoming.get(field) not in (None,'','未检测'):
                            existing[field]=incoming[field]
                for field in ('last_recharge','last_successful_recharge'):
                    if incoming.get(field) and incoming[field].get('time','')>existing.get(field,{}).get('time',''):
                        existing[field]=incoming[field]
            else:
                key=incoming['id'] if incoming['id'] not in accounts else uuid.uuid4().hex
                row=dict(incoming,id=key,sequence=next_sequence+1)
                next_sequence+=1
                accounts[key]=row
                by_email[folded]=row
                account_map[incoming['id']]=key
                counts['accounts']+=1

        by_digest={row['digest']:row for row in cards.values()}
        next_sequence=max((row.get('sequence',0) for row in cards.values()),default=0)
        priority={'unused':0,'exception':1,'reserved':2,'used':3}
        for incoming in sorted(documents['cards'],key=lambda row:row['sequence']):
            existing=by_digest.get(incoming['digest'])
            if existing:
                if existing['sku']!=incoming['sku']:
                    raise ValueError('两份数据库存在规格不同的相同卡密，未合并，请保留备份后核对')
                card_map[incoming['id']]=existing['id']
                if priority[incoming['status']]>priority[existing['status']]:
                    existing.update({key:value for key,value in incoming.items() if key not in ('id','sequence')})
                if incoming.get('archived') and incoming['status']=='used':
                    existing.update(archived=True,archived_at=incoming.get('archived_at',incoming.get('updated','')))
            else:
                key=incoming['id'] if incoming['id'] not in cards else uuid.uuid4().hex
                row=dict(incoming,id=key,sequence=next_sequence+1)
                next_sequence+=1
                cards[key]=row
                by_digest[row['digest']]=row
                card_map[incoming['id']]=key
                counts['cards']+=1

        task_map={}
        next_sequence=max((row.get('sequence',0) for row in tasks.values()),default=0)
        for incoming in sorted(documents['recharge_tasks'],key=lambda row:row['sequence']):
            account_id=account_map.get(incoming['account_id'],incoming['account_id'])
            card_id=card_map.get(incoming['card_id'],incoming['card_id'])
            existing=tasks.get(incoming['id'])
            if existing:
                if existing['account_id']!=account_id or existing['card_id']!=card_id:
                    raise ValueError('充值记录标识冲突，未合并，请保留两份数据')
                task_map[incoming['id']]=existing['id']
                if incoming['status']=='completed' and existing['status']!='completed':
                    sequence=existing['sequence']
                    existing.update(incoming,account_id=account_id,card_id=card_id,sequence=sequence)
                continue
            row=dict(incoming,account_id=account_id,card_id=card_id,sequence=next_sequence+1)
            next_sequence+=1
            tasks[row['id']]=row
            task_map[incoming['id']]=row['id']
            counts['tasks']+=1

        for task in tasks.values():
            if task['status'] in ACTIVE and not task.get('submitted'):
                task.update(status='stopped',message='旧版未提交任务已停止，未执行兑换')
                card=cards[task['card_id']]
                if card['status']=='reserved' and card.get('task_id')==task['id']:
                    remote=task.get('precheck',{}).get('card_status')
                    card['status']='unused' if remote in (None,'','unused') else 'exception'
                    if card['status']=='unused':card.pop('task_id',None)

        for row in accounts.values():
            row['active_card_id']=card_map.get(row.get('active_card_id'),row.get('active_card_id'))
            row['active_task_id']=task_map.get(row.get('active_task_id'),row.get('active_task_id'))
            pending=[task for task in tasks.values() if task['account_id']==row['id'] and task_unresolved(task) and task.get('submitted')]
            if len(pending)>1:
                raise ValueError('同一账号存在多笔未核实的已提交充值，请先在旧版核对，未合并')
            if pending:
                task=pending[0]
                row.update(active_task_id=task['id'],active_card_id=task['card_id'],execution_mode='recharge',
                           secret=cards[task['card_id']]['secret'],card=cards[task['card_id']]['masked'],submitted=True,status='pending',probe_only=False)
            elif row.get('status') in ('login','ready','queued','running','confirm','preflight'):
                row.update(status='idle',message='资料已保存，请获取 Session 后继续')
        for card in cards.values():
            card['task_id']=task_map.get(card.get('task_id'),card.get('task_id'))
            if card['task_id'] is None:
                card.pop('task_id',None)

        backups=store.path.parent.parent/'backups'
        backup_database(source_path,backups,'source-'+marker[-12:])
        backup_database(store.path,backups,'before-merge')
        with store.db:
            for row in accounts.values():
                store.db.execute('INSERT OR REPLACE INTO records VALUES (?,?)',(row['id'],json.dumps(row,ensure_ascii=False)))
            for card in cards.values():store._inventory_write('cards',card)
            for task in tasks.values():store._inventory_write('recharge_tasks',task)
            store.db.execute('INSERT INTO inventory_meta VALUES (?,?)',(marker,'1'))
        store.rows,store.cards,store.charge_tasks=accounts,cards,tasks
        store.init_inventory()
        return counts
