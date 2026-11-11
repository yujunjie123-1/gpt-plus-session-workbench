"""Explicit payment failures and durable account quarantine; no remote text is saved."""
import re


RISK_REASONS = {
    'card_declined': '银行卡拒付',
    'approve_not_allowed': 'approve 未允许',
    'manual': '手动移入风控池',
}
ERROR_FIELDS = {'error', 'errors', 'code', 'error_code', 'decline_code', 'message', 'reason',
                'failure_reason', 'failure_message', 'error_message', 'last_error', 'detail',
                'details', 'error_details', 'description', 'data'}


def payment_risk_code(payload):
    """Only recognize explicit failures, never a bare false or generic permission error."""
    values = []

    def collect(value, depth=0):
        if depth > 4 or len(values) >= 32:
            return
        if isinstance(value, str):
            values.append(value[:4000].lower())
        elif isinstance(value, dict):
            for key, child in value.items():
                if key in ERROR_FIELDS:
                    collect(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:8]:
                collect(child, depth + 1)

    collect(payload)
    for text in values:
        if re.search(r'\b(?:card[ _-]declined|card[ _-]was[ _-]declined|card (?:has been |is )?declined|do[ _-]not[ _-]honou?r)\b', text) or re.search(r'银行卡.{0,6}(?:拒付|拒绝|被拒)|信用卡.{0,6}(?:拒付|被拒)', text):
            return 'card_declined'
        if re.search(r'\bapprov(?:e|al)[ _-](?:not[ _-](?:allowed|permitted|approved)|denied|disallowed|forbidden)\b', text) or re.search(r'approve\s*(?:未(?:被)?允许|不允许|未获准|被拒绝)', text):
            return 'approve_not_allowed'
    return None


class ProviderRiskError(RuntimeError):
    def __init__(self, code):
        self.risk_code = code
        super().__init__(RISK_REASONS[code] + '，已移入风控账号池')


class AccountPolicyMixin:
    def has_successful_recharge(self, row):
        email = row['email'].strip().casefold()
        return bool(row.get('last_successful_recharge') or row.get('last_recharge', {}).get('status') == 'completed'
                    or any(task['status'] == 'completed' and task['email'].strip().casefold() == email
                           for task in self.charge_tasks.values()))

    def recharge_block_reason(self, row):
        if row.get('risk', {}).get('active'):
            return '账号在风控池，请养号后手动移回普通池'
        if self.has_successful_recharge(row):
            return '账号已有充值成功记录，只能选择从未充值成功的账号'
        return ''

    def ensure_recharge_allowed(self, key):
        with self.lock:
            reason = self.recharge_block_reason(self.get(key))
            if reason:
                raise ValueError(reason)

    def quarantine_account(self, key, code):
        from .store import now
        if code not in RISK_REASONS:
            raise ValueError('未知的风控原因')
        with self.lock:
            row = self.get(key)
            previous = row.get('risk', {})
            if previous.get('active') and previous.get('code') == code:
                return
            stamp = now()
            event = {'action': 'quarantine', 'time': stamp, 'code': code, 'reason': RISK_REASONS[code],
                     'task_id': row.get('active_task_id')}
            risk = dict(active=True, code=code, reason=RISK_REASONS[code],
                        since=previous['since'] if previous.get('active') else stamp,
                        history=(previous.get('history', []) + [event])[-100:])
            self.update(key, risk=risk)

    def restore_account(self, key):
        from .store import now
        with self.lock:
            row = self.get(key)
            previous = row.get('risk', {})
            if not previous.get('active'):
                raise ValueError('账号已不在风控池，请刷新列表')
            stamp = now()
            risk = dict(previous, active=False, restored_at=stamp,
                        history=(previous.get('history', []) + [{'action': 'restore', 'time': stamp}])[-100:])
            # Do not change submission, task or card state while restoring an account.
            self.update(key, risk=risk)
