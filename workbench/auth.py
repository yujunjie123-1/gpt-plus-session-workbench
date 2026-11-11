"""Bounded mail-code retrieval and TOTP generation for the explicit login flow."""
import base64
import hashlib
import hmac
import ipaddress
import json
import re
import socket
import struct
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(NoRedirect)


def totp_code(secret, timestamp=None, period=30, digits=6):
    key = re.sub(r'[\s=-]', '', str(secret or '')).upper()
    if not re.fullmatch(r'[A-Z2-7]{16,128}', key):
        raise ValueError('2FA 密钥格式不正确，请核对后重新登录')
    try:
        raw = base64.b32decode(key + '=' * (-len(key) % 8), casefold=True)
    except Exception as exc:
        raise ValueError('2FA 密钥格式不正确，请核对后重新登录') from exc
    counter = int((time.time() if timestamp is None else timestamp) // period)
    digest = hmac.new(raw, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def extract_mail_code(payload):
    if isinstance(payload, bytes):
        payload = payload.decode('utf-8', errors='replace')
    if isinstance(payload, str):
        if re.fullmatch(r'\s*\d{6}\s*', payload):
            return payload.strip()
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            match = re.search(r'(?<!\d)(\d{6})(?!\d)', payload)
            return match.group(1) if match else ''
    if isinstance(payload, (str, int)) and not isinstance(payload, bool):
        scalar = str(payload).strip()
        return scalar if re.fullmatch(r'\d{6}', scalar) else ''
    preferred = {'code', 'otp', 'email_code', 'verification_code', 'verify_code', '验证码'}
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in preferred and isinstance(item, (str, int)):
                    match = re.fullmatch(r'\s*(\d{6})\s*', str(item))
                    if match:
                        return match.group(1)
            for item in value.values():
                found = visit(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = visit(item)
                if found:
                    return found
        return ''
    found = visit(payload)
    if found:
        return found
    if isinstance(payload, (dict, list)):
        match = re.search(r'(?<!\d)(\d{6})(?!\d)', json.dumps(payload, ensure_ascii=False))
        return match.group(1) if match else ''
    return ''


def fetch_mail_code(url):
    """Fetch only public HTTPS endpoints. Never follow redirects or leak keys to another host."""
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.fragment or len(url) > 4096):
            raise ValueError
        port = parts.port or 443
        if port != 443:
            raise ValueError
        addresses = {entry[4][0] for entry in socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)}
        if not addresses:
            raise ValueError
        for address in addresses:
            ip = ipaddress.ip_address(address.split('%')[0])
            if not ip.is_global:
                raise ValueError
        request = urllib.request.Request(url, headers={'Accept': 'application/json,text/plain', 'Cache-Control': 'no-store'})
        with _opener.open(request, timeout=8) as response:
            if response.status != 200 or response.geturl() != url:
                raise ValueError
            body = response.read(65537)
        if len(body) > 65536:
            raise ValueError
        code = extract_mail_code(body)
        if not code:
            raise ValueError
        return code
    except Exception as exc:
        raise RuntimeError('暂未从邮箱取码接口读到有效验证码，正在重试') from exc
