"""Local records with Windows DPAPI encryption for sensitive credentials."""
import base64
import ctypes
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


def eligibility(value):
    if value in [None, '']:
        return ''
    if isinstance(value, bool) or str(value) not in ['1', '3']:
        raise ValueError('账号资格只能选择 1 个月或 3 个月')
    return int(value)


def mail_address(value):
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        url = urlsplit(text)
        valid = url.scheme == 'https' and url.hostname and not url.username and not url.password and not url.fragment
    except ValueError:
        valid = False
    if not valid or any(c.isspace() for c in text):
        raise ValueError('邮箱取码链接须为有效的 HTTPS 地址')
    return text


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


class Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_byte))]


def crypt(text, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('Encrypted storage requires Windows DPAPI')
    raw = base64.b64decode(text) if decrypt else text.encode('utf-8')
    buf = ctypes.create_string_buffer(raw)
    src = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    dst = Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise ctypes.WinError()
    try:
        data = ctypes.string_at(dst.data, dst.size)
        return data.decode('utf-8') if decrypt else base64.b64encode(data).decode('ascii')
    finally:
        ctypes.windll.kernel32.LocalFree(dst.data)


def mask(value):
    return value[:4] + '••••' + value[-4:] if len(value) > 8 else '••••'


def mask_email(value):
    """Keep only a safe, human-checkable portion of an upstream email."""
    text = str(value or '').strip()
    if '@' not in text or any(char.isspace() for char in text) or len(text) > 320:
        return ''
    local, domain = text.rsplit('@', 1)
    if not local or not domain or '.' not in domain:
        return ''
    if any(marker in local for marker in ('*', '•')):
        return text
    if len(local) <= 2:
        masked = local[0] + '••••' + (local[-1] if len(local) == 2 else '')
    elif len(local) <= 4:
        masked = local[0] + '••••' + local[-1]
    else:
        masked = local[:2] + '••••' + local[-2:]
    return masked + '@' + domain


from .inventory_store import InventoryStoreMixin
from .account_policy import AccountPolicyMixin


class Store(AccountPolicyMixin, InventoryStoreMixin):
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, document TEXT NOT NULL)')
        self.db.commit()
        self.rows = {key: json.loads(doc) for key, doc in self.db.execute('SELECT id,document FROM records')}
        for row in self.rows.values():
            row.setdefault('eligibility_months', '')
            if row.get('submitted') and row['status'] != 'completed':
                row.update(status='pending', message='程序已恢复，请查询原卡密，禁止重复提交')
            elif row['status'] in ['login', 'queued', 'running', 'preflight', 'confirm', 'ready']:
                row.update(status='idle', message='请重新登录获取 Session')
            self._save(row)
        self.init_inventory()

    def _save(self, row):
        self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?)', (row['id'], json.dumps(row, ensure_ascii=False)))
        self.db.commit()

    def add_many(self, items):
        if not isinstance(items, list) or not 1 <= len(items) <= 500:
            raise ValueError('每次可导入 1–500 条')
        import re
        with self.lock:
            existing = {crypt(r['secret'], True) for r in self.rows.values() if not r.get('probe_only')}
            prepared = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError('每条账号资料须为对象')
                months = eligibility(item.get('eligibility_months'))
                mail_url = mail_address(item.get('mail_url'))
                email = str(item.get('email', '')).strip()
                code = str(item.get('code') or '').strip()
                if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                    raise ValueError('邮箱格式不正确')
                if code and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{3,255}', code):
                    raise ValueError('卡密须为一张完整的字母数字卡密')
                if code and code in existing:
                    raise ValueError('存在重复卡密，整批未导入')
                if code:
                    existing.add(code)
                prepared.append(dict(id=uuid.uuid4().hex, email=email, secret=crypt(code),
                    card=mask(code) if code else '未绑定卡密', probe_only=not bool(code),
                    password=crypt(str(item.get('password', ''))), totp=crypt(str(item.get('totp', ''))),
                    mail_url=crypt(mail_url), eligibility_months=months,
                    status='idle', message='等待登录', plan='未检测', current_plan='未检测',
                    submitted=False, can_retry=False, created=now(), updated=now(), history=[]))
            for row in prepared:
                self.rows[row['id']] = row
                self._save(row)
            return [r['id'] for r in prepared]

    def get(self, key):
        with self.lock:
            if key not in self.rows:
                raise ValueError('记录不存在')
            return dict(self.rows[key])

    def update(self, key, **fields):
        with self.lock:
            row = self.rows[key]
            previous_message = row.get('message')
            row.update(fields, updated=now())
            if 'message' in fields and fields['message'] != previous_message:
                row['history'] = (row['history'] + [dict(time=now(), message=fields['message'], status=row['status'])])[-100:]
            self.sync_inventory(row)
            self._save(row)

    def public(self):
        with self.lock:
            return [dict({k: v for k, v in r.items() if k not in ['secret', 'password', 'totp', 'mail_url']},
                         password_configured=bool(r.get('password') and crypt(r['password'], True)),
                         totp_configured=bool(r.get('totp') and crypt(r['totp'], True)),
                         mail_configured=bool(r.get('mail_url') and crypt(r['mail_url'], True))) for r in self.rows.values()]

    def edit_account(self, key, data):
        with self.lock:
            row = self.get(key)
            if row.get('submitted') or row['status'] == 'completed':
                raise ValueError('已提交或完成的账号不可更换资料或卡密')
            email = str(data.get('email', row['email'])).strip()
            if email.casefold() != row['email'].casefold():
                raise ValueError('补全资料中的邮箱必须与当前账号一致；其他邮箱请添加新账号')
            fields = {}
            for name in ['password', 'totp', 'mail_url']:
                if name not in data or data[name] == '':
                    continue
                if not isinstance(data[name], str) or len(data[name]) > 4096:
                    raise ValueError('账号资料格式或长度不正确')
                value = data[name]
                if name == 'mail_url':
                    value = mail_address(value)
                if name == 'totp':
                    from .auth import totp_code
                    totp_code(value)
                fields[name] = crypt(value)
            if 'eligibility_months' in data and data['eligibility_months'] != '':
                fields['eligibility_months'] = eligibility(data['eligibility_months'])
            code = str(data.get('code', '')).strip()
            if code:
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{3,255}', code) or code.startswith('LOCAL-PROBE-'):
                    raise ValueError('请粘贴一张完整的真实兑换卡密')
                if any(other['id'] != key and crypt(other['secret'], True) == code for other in self.rows.values()):
                    raise ValueError('这张卡密已绑定其他记录，未保存修改')
                fields.update(secret=crypt(code), card=mask(code), probe_only=False)
            self.update(key, **fields)
            return self.get(key)

    def set_eligibility(self, ids, value):
        months = eligibility(value)
        if not months or not isinstance(ids, list) or not 1 <= len(ids) <= 500:
            raise ValueError('请选择账号和 1 个月或 3 个月资格')
        ids = list(dict.fromkeys(ids))
        with self.lock:
            if any(key not in self.rows for key in ids):
                raise ValueError('选中账号已不存在，未修改分类')
            for key in ids:
                self.update(key, eligibility_months=months, message=f'已设置为 {months} 个月资格账号（人工分类）')

    def delete(self, key):
        with self.lock:
            del self.rows[key]
            with self.db:
                self.db.execute('DELETE FROM records WHERE id=?', (key,))
                self.db.execute('DELETE FROM session_credentials WHERE account_id=?',(key,))
