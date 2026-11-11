"""Allowlisted cdk.bar Partner API client; all credentials stay server-side."""
import asyncio
import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from .account_policy import ProviderRiskError, payment_risk_code
from .session_lifecycle import SessionRejectedError
from .products import PRODUCTS


def session_rejected(payload):
    """An HTTP 401/403 alone may be provider authorization, not a revoked GPT session."""
    codes={'invalid_session','session_expired','session_invalid','access_token_expired',
           'invalid_access_token','token_expired','session_revoked','token_revoked'}
    def inspect(value,depth=0):
        if not isinstance(value,dict) or depth>3:
            return False
        if value.get('session_valid') is False:
            return True
        for field in ('code','error_code','type'):
            if isinstance(value.get(field),str) and value[field].lower() in codes:
                return True
        return any(inspect(value.get(field),depth+1) for field in ('data','error','details'))
    return inspect(payload)


def unwrap(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
        raise PartnerAPIError('INVALID_RESPONSE', '兑换服务响应格式异常，请查询原交易')
    return payload['data']


def credential_token(*values):
    """Accept only non-empty string ticket values from documented fields."""
    for value in values:
        if value is None or value == '':
            continue
        if not isinstance(value, str):
            raise ValueError('兑换服务返回的凭证格式异常；未提交兑换，请稍后重试或联系供货方')
        if value.strip():
            return value.strip()
    return ''


def normalize_response(action, payload):
    """Unwrap the documented data envelope without merging response outer fields."""
    data = dict(unwrap(payload))
    if action == 'preview':
        token = credential_token(data.get('redemption_token'))
        if token:
            data['redemption_token'] = token
        else:
            data.pop('redemption_token', None)
    elif action == 'preflight':
        token = credential_token(data.get('preflight_token'))
        if token:
            data['preflight_token'] = token
        else:
            data.pop('preflight_token', None)
    return data


def partner_qualification(preview):
    """Read the product, plan and redemption ticket selected by the card.

    The Partner API does not accept a product or plan override. Keep this
    parser generic so Session direct-charge can handle every product exposed
    by the upstream service without inventing local SKU mappings.
    """
    product = preview.get('product')
    if product is None or product == '':
        raise ValueError('上游未返回卡密产品信息，无法识别卡密类型；未提交兑换，请核实卡密或联系供货方')
    if not isinstance(product, str) or not product.strip() or len(product.strip()) > 128:
        raise ValueError('上游返回的卡密产品格式异常，无法识别卡密类型；未提交兑换')
    value = preview.get('plan')
    if value is None or value == '':
        raise ValueError('上游未返回卡密套餐信息，无法识别卡密类型；未提交兑换，请核实卡密或联系供货方')
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128:
        raise ValueError('上游返回的卡密套餐格式异常，无法识别卡密类型；未提交兑换')
    token = credential_token(preview.get('redemption_token'))
    if not token:
        raise ValueError('卡密已返回产品和套餐，但缺少兑换资格凭证；未提交兑换，请稍后重试或联系供货方')
    return product.strip(), value.strip(), token


def plus_qualification(preview, expected_sku='plus'):
    """Require the documented GPT product and an exact Plus plan match."""
    if expected_sku not in ('plus','plus_half'):
        raise ValueError('当前仅支持 Plus 和半价 Plus 兑换；未提交兑换')
    product, value, token = partner_qualification(preview)
    if product.lower() != 'gpt':
        raise ValueError('上游返回的卡密产品不是 GPT，当前仅支持 Plus 和半价 Plus；未提交兑换')
    plan = value.lower()
    label=PRODUCTS[expected_sku]['label']
    if plan not in ('plus','plus_half'):
        raise ValueError(f'上游返回的卡密套餐不是 {label}，当前仅支持 Plus 和半价 Plus 兑换；未提交兑换')
    if plan!=expected_sku:
        actual=PRODUCTS[plan]['label']
        raise ValueError(f'卡密类型不符：上游确认是{actual}，本次选择的是{label}；未提交兑换，请在卡密管理修改未使用卡密的规格后重新选择')
    return plan, token


def lookup_state(row):
    status = str(row.get('status', '')).lower()
    if status == 'completed' or (status == 'used' and row.get('used') is True):
        return 'completed'
    if status in ('queued', 'processing', 'pending', 'accepted'):
        return 'pending'
    if status in ('failed', 'failed_precharge', 'declined', 'cancelled'):
        return 'failed'
    if status in ['expired', 'disabled']:
        return 'blocked'
    if status == 'unused':
        return 'unused'
    return 'unknown'


def blocking_reason(account, target, flow, *, enforce_plan_conflict=True):
    if account.get('subscription_recovery_required') is True or account.get('subscription_is_delinquent') is True:
        return '当前订阅需要先处理欠费或恢复问题'
    if account.get('blocking_reason') or account.get('can_purchase') is False:
        return '上游确认当前账号暂不支持购买，请联系供货方'
    if flow == 'card_attach' or account.get('subscription_has_active') is False:
        return ''
    current = str(account.get('currentPlan', account.get('current_plan', ''))).lower()
    if enforce_plan_conflict and target in ('plus','plus_half') and ('plus' in current or 'pro' in current):
        return '当前账号已有同档或更高套餐'
    return ''


class PartnerAPIError(RuntimeError):
    def __init__(self, code, message, *, status=0, uncertain=False, retryable=False, retry_after=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.uncertain = uncertain
        self.retryable = retryable
        self.retry_after = retry_after


class PartnerConfigurationError(RuntimeError):
    """The local Partner API credential is missing before a request is sent."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class Provider:
    origin = 'https://cdk.bar/api/partner/v1'
    operations = frozenset(('preview', 'preflight', 'redeem', 'result', 'result-by-code',
                            'lookup', 'lookup-batch', 'billing'))
    messages = {
        'RATE_LIMITED': '兑换服务限流，稍后将继续查询原任务',
        'UPSTREAM_TIMEOUT': '兑换服务响应超时，正在查询原交易状态',
        'UPSTREAM_UNAVAILABLE': '兑换服务暂不可用，正在查询原交易状态',
        'NETWORK_ERROR': '兑换服务网络暂不可用，正在查询原交易状态',
        'INVALID_RESPONSE': '兑换服务响应格式异常，请查询原交易状态',
        'INVALID_REQUEST': '请求内容超出接口限制',
        'REQUEST_REJECTED': '兑换服务拒绝请求，请核实卡密及接口权限',
        'AUTHENTICATION_FAILED': 'Partner API 授权失败，请检查 CDK_PARTNER_PASSWORD',
    }

    def __init__(self, device, gate=None, password=None, fetch_impl=None):
        self.device = device
        if not isinstance(device, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', device):
            raise ValueError('Partner API 设备 ID 格式不正确')
        password = os.environ.get('CDK_PARTNER_PASSWORD', '') if password is None else password
        if not isinstance(password, str) or not password.strip():
            raise PartnerConfigurationError(
                '尚未配置 Partner API 授权；请在 Windows 用户环境变量中设置 CDK_PARTNER_PASSWORD，并完全重启工作台')
        self.authorization = 'Basic ' + base64.b64encode(('partner:' + password).encode('utf-8')).decode('ascii')
        self.gate = gate or asyncio.Semaphore(2)
        self.fetch_impl = fetch_impl

    @staticmethod
    def _retry_after(response_headers):
        try:
            return max(1, min(60, int(response_headers.get('Retry-After', ''))))
        except (TypeError, ValueError, AttributeError):
            return None

    def _request_sync(self, action, body):
        payload = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(payload) > 256 * 1024:
            raise PartnerAPIError('INVALID_REQUEST', self.messages['INVALID_REQUEST'], status=413)
        if action == 'preflight':
            credential = body.get('credential') if isinstance(body, dict) else None
            session = credential.get('session') if isinstance(credential, dict) else None
            if isinstance(session, str) and len(session.encode('utf-8')) > 128 * 1024:
                raise PartnerAPIError('INVALID_REQUEST', self.messages['INVALID_REQUEST'], status=413)
        headers = {'Authorization': self.authorization, 'Content-Type': 'application/json',
                   'X-Redemption-Device': self.device, 'Accept': 'application/json'}
        url = self.origin + '/' + action
        timeout = 155 if action == 'redeem' else 125
        if self.fetch_impl:
            try:
                response = self.fetch_impl(url, body, headers, timeout)
            except (urllib.error.URLError, TimeoutError, OSError):
                raise PartnerAPIError('NETWORK_ERROR', self.messages['NETWORK_ERROR'],
                                      uncertain=action == 'redeem', retryable=action != 'redeem') from None
            status, response_headers, raw = response
        else:
            request = urllib.request.Request(url, data=payload, headers=headers, method='POST')
            opener = urllib.request.build_opener(_NoRedirect())
            try:
                with opener.open(request, timeout=timeout) as response:
                    status = response.status
                    response_headers = response.headers
                    raw = response.read(256 * 1024 + 1)
            except urllib.error.HTTPError as error:
                status, response_headers = error.code, error.headers
                raw = error.read(256 * 1024 + 1)
            except (urllib.error.URLError, TimeoutError, OSError):
                raise PartnerAPIError('NETWORK_ERROR', self.messages['NETWORK_ERROR'],
                                      uncertain=action == 'redeem', retryable=action != 'redeem') from None
        if status == 429:
            raise PartnerAPIError('RATE_LIMITED', self.messages['RATE_LIMITED'], status=status,
                                  retryable=True, retry_after=self._retry_after(response_headers))
        if status == 413:
            raise PartnerAPIError('INVALID_REQUEST', self.messages['INVALID_REQUEST'], status=status)
        if len(raw) > 256 * 1024:
            raise PartnerAPIError('INVALID_RESPONSE', self.messages['INVALID_RESPONSE'], status=status,
                                  uncertain=action == 'redeem' and status >= 500)
        try:
            envelope = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            code = 'UPSTREAM_TIMEOUT' if status == 504 else 'UPSTREAM_UNAVAILABLE' if status in (502, 503) else 'INVALID_RESPONSE'
            raise PartnerAPIError(code, self.messages[code], status=status,
                                  uncertain=action == 'redeem' and status >= 500,
                                  retryable=action != 'redeem' and status >= 500) from None
        if status == 429 or isinstance(envelope, dict) and envelope.get('ok') is False and (envelope.get('error') or {}).get('code') == 'RATE_LIMITED':
            raise PartnerAPIError('RATE_LIMITED', self.messages['RATE_LIMITED'], status=status,
                                  retryable=True, retry_after=self._retry_after(response_headers))
        if status in (502, 503, 504):
            code = 'UPSTREAM_TIMEOUT' if status == 504 else 'UPSTREAM_UNAVAILABLE'
            raise PartnerAPIError(code, self.messages[code], status=status,
                                  uncertain=action == 'redeem', retryable=action != 'redeem')
        if status in (401, 403):
            raise PartnerAPIError('AUTHENTICATION_FAILED', self.messages['AUTHENTICATION_FAILED'], status=status)
        if not isinstance(envelope, dict) or not 200 <= status < 300 or envelope.get('ok') is not True:
            error = envelope.get('error') if isinstance(envelope, dict) else None
            raw_code = error.get('code') if isinstance(error, dict) else ''
            code = raw_code if isinstance(raw_code, str) and re.fullmatch(r'[A-Z0-9_-]{1,64}', raw_code) else 'REQUEST_REJECTED'
            retryable = bool(error.get('retryable') is True) if isinstance(error, dict) else False
            uncertain = bool(error.get('uncertain') is True) if isinstance(error, dict) else False
            raise PartnerAPIError(code, self.messages.get(code, self.messages['REQUEST_REJECTED']), status=status,
                                  uncertain=uncertain or action == 'redeem' and (status >= 500 or status == 408 or 300 <= status < 400),
                                  retryable=retryable and action != 'redeem')
        data = normalize_response(action, envelope)
        if action in ('preflight', 'redeem') and session_rejected(data):
            raise SessionRejectedError()
        return data

    async def call(self, action, body):
        if action not in self.operations:
            raise ValueError('不支持的 Partner API 操作')
        attempts = 1 if action == 'redeem' else 3
        for attempt in range(attempts):
            try:
                async with self.gate:
                    return await asyncio.to_thread(self._request_sync, action, body)
            except PartnerAPIError as error:
                if action in ('preflight', 'redeem') and session_rejected({'code': error.code}):
                    raise SessionRejectedError() from None
                if action in ('preflight', 'redeem') and error.status in (402, 409, 422):
                    risk = payment_risk_code({'code': error.code})
                    if risk:
                        raise ProviderRiskError(risk) from None
                if not error.retryable or attempt + 1 >= attempts:
                    raise
                delay = error.retry_after or min(15, 3 * (2 ** attempt))
                await asyncio.sleep(delay)

    async def preview(self, code):
        return await self.call('preview', {'code': code})

    async def preflight(self, code, redemption_token, session):
        return await self.call('preflight', {'code': code, 'redemption_token': redemption_token,
            'credential': {'mode': 'session', 'session': session}})

    async def redeem(self, redemption_token, preflight_token, client_request_id):
        return await self.call('redeem', {'redemption_token': redemption_token,
            'preflight_token': preflight_token, 'client_request_id': client_request_id})

    async def result(self, token):
        return await self.call('result', {'token': token})

    async def result_by_code(self, code):
        return await self.call('result-by-code', {'code': code})

    async def lookup(self, code):
        data = await self.call('lookup', {'code': code})
        if data.get('cdk_code') != code:
            raise RuntimeError('查询结果无法精确关联原卡密')
        return data

    async def lookup_batch(self, codes):
        if (not isinstance(codes, list) or not 1 <= len(codes) <= 100
                or any(not isinstance(code, str) or not 1 <= len(code) <= 256 for code in codes)):
            raise ValueError('批量卡密查询须提供 1-100 张有效卡密')
        return await self.call('lookup-batch', {'codes': codes})

    async def billing(self, *, session=None, code=None):
        if bool(session) == bool(code):
            raise ValueError('账单查询须且只能提供 Session 或卡密')
        return await self.call('billing', {'token_input': session} if session else {'cdk_code': code})

    async def close(self):
        return None
