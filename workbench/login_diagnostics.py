"""Bounded login metadata only: never retain request bodies, URLs or cookies."""
from collections import deque
from urllib.parse import urlsplit


def mime_label(value):
    mime = str(value).lower().split(';', 1)[0].strip()
    return {'application/json': 'JSON', 'application/x-www-form-urlencoded': '表单',
            'multipart/form-data': '表单', 'text/html': 'HTML', 'text/plain': '文本',
            'text/javascript': '脚本', 'application/javascript': '脚本', '': '无'}.get(mime, '其他')


class LoginDiagnostics:
    def __init__(self):
        self.responses = deque(maxlen=8)

    def observe(self, response):
        request = response.request
        parsed = urlsplit(request.url)
        if parsed.hostname not in ('auth.openai.com', 'chatgpt.com'):
            return
        if request.resource_type not in ('document', 'fetch', 'xhr'):
            return
        method = request.method if request.method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE') else '其他'
        if method == 'GET' and response.status < 400:
            return
        self.responses.append({
            'method': method, 'mode': '页面' if request.resource_type == 'document' else '接口',
            'request': mime_label(request.headers.get('content-type', '')),
            'response': mime_label(response.headers.get('content-type', '')), 'status': int(response.status),
        })

    def summary(self):
        if not self.responses:
            return '诊断：尚无登录提交响应'
        mutation = next((r for r in reversed(self.responses) if r['method'] != 'GET'), None)
        latest = self.responses[-1]
        row = mutation or latest
        result = ('诊断：' + row['method'] + '/' + row['mode'] + ' 请求' + row['request']
                  + '→响应' + row['response'] + ' HTTP' + str(row['status']))
        if latest is not row and latest['status'] >= 400:
            result += '；随后HTTP' + str(latest['status'])
        return result
