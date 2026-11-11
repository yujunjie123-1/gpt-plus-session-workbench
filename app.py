"""Run on Windows with Python 3.12. Only binds to the IPv4 loopback interface."""
import argparse
import csv
import io
import json
import secrets
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from workbench.store import Store
from workbench.engine import Engine
from workbench.paths import data_directory
from workbench.data_migration import merge_v2_database
from workbench.session_file_extract import MAX_FILE_BYTES, extract_session
from workbench.provider import PartnerConfigurationError

ROOT = Path(__file__).resolve().parent


class AlreadyRunningError(RuntimeError):
    pass


def safe_csv(value):
    text = str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # URLs, payloads and credentials must never enter console logs.

    def send(self, code, body, mime='application/json; charset=utf-8', style_nonce=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store, private')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        style_policy = "'self'" + (" 'nonce-" + style_nonce + "'" if style_nonce else '')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src " + style_policy + "; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        expected = 'Bearer ' + self.server.token
        host = self.headers.get('Host', '')
        if host != self.server.address:
            return False
        return secrets.compare_digest(self.headers.get('Authorization', ''), expected)

    def bridge_authorized(self):
        origin = self.headers.get('Origin', '')
        if (self.headers.get('Host', '') != self.server.address
                or (origin and re.fullmatch(r'chrome-extension://[a-p]{32}', origin) is None)):
            return False
        authorization = self.headers.get('Authorization', '')
        if secrets.compare_digest(authorization, 'Bearer ' + self.server.bridge_token):
            self.bridge_scope = ''  # Legacy helpers can only recover legacy sessions.
            return True
        if not authorization.startswith('Bearer '):
            return False
        self.bridge_scope = self.server.engine.edge_token_scope(authorization[7:])
        return self.bridge_scope is not None

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/edge-session/jobs':
            if not self.bridge_authorized():
                return self.send(403, {'error': '请先在扩展中连接本机工作台'})
            if not self.bridge_scope:
                self.server.helper_seen = time.monotonic()
                version = self.headers.get('X-Workbench-Helper-Version', '')
                self.server.helper_version = version if re.fullmatch(r'[0-9.]{1,20}', version) else '0.1'
                self.server.helper_incognito = {'1': True, '0': False}.get(self.headers.get('X-Workbench-Helper-InPrivate'))
            return self.send(200, {'jobs': self.server.engine.edge_jobs(self.bridge_scope)})
        if path == '/edge-session-bridge':
            return self.send(200, (ROOT/'web'/'edge-session-bridge.html').read_bytes(), 'text/html; charset=utf-8')
        distribution = ROOT / 'web' / 'dist'
        if path == '/' and (distribution / 'index.html').is_file():
            nonce = secrets.token_urlsafe(24)
            html = (distribution / 'index.html').read_text(encoding='utf-8').replace('__WORKBENCH_STYLE_NONCE__', nonce)
            return self.send(200, html, 'text/html; charset=utf-8', style_nonce=nonce)
        if path.startswith('/assets/'):
            candidate = (distribution / path.lstrip('/')).resolve()
            if not candidate.is_relative_to((distribution / 'assets').resolve()) or not candidate.is_file():
                return self.send(404, {'error':'资源不存在'})
            mime = {'.js':'text/javascript', '.css':'text/css', '.svg':'image/svg+xml', '.woff2':'font/woff2'}.get(candidate.suffix)
            if not mime:
                return self.send(404, {'error':'资源不存在'})
            return self.send(200, candidate.read_bytes(), mime)
        if path in ['/', '/app.js', '/workflow.js', '/precheck-panel.js', '/import-parser.js', '/style.css']:
            name = 'index.html' if path == '/' else path[1:]
            mime = {'index.html':'text/html', 'app.js':'text/javascript', 'workflow.js':'text/javascript', 'precheck-panel.js':'text/javascript', 'import-parser.js':'text/javascript', 'style.css':'text/css'}[name]
            return self.send(200, (ROOT / 'web' / name).read_bytes(), mime+'; charset=utf-8')
        if not self.authorized():
            return self.send(401, {'error':'请使用启动器打开工作台'})
        if path == '/api/ping':
            return self.send(200, {'ok': True})
        if path == '/api/inventory/state':
            state = self.server.engine.inventory_snapshot()
            state['storage'] = {'backend':'SQLite','database':str(self.server.store.path),'persistent':True}
            state['helper'] = {'connected': time.monotonic() - self.server.helper_seen < 90,
                               'version': self.server.helper_version, 'incognito': self.server.helper_incognito}
            return self.send(200, state)
        if path == '/api/state':
            state = self.server.engine.snapshot()
            state['helper'] = {'connected': time.monotonic() - self.server.helper_seen < 90,
                               'version': self.server.helper_version, 'incognito': self.server.helper_incognito}
            return self.send(200, state)
        if path == '/api/export':
            stream = io.StringIO(newline='')
            writer = csv.writer(stream)
            writer.writerow(['邮箱','资格（月，人工分类）','卡密（脱敏）','状态','目标套餐','当前套餐','说明','更新时间'])
            for row in self.server.engine.snapshot()['records']:
                writer.writerow([safe_csv(row.get(k,'')) for k in ['email','eligibility_months','card','status','plan','current_plan','message','updated']])
            return self.send(200, ('\ufeff'+stream.getvalue()).encode('utf-8'), 'text/csv; charset=utf-8')
        self.send(404, {'error':'未找到资源'})

    def do_POST(self):
        path = urlsplit(self.path).path
        bridge_request = path in ['/api/edge-session/receive', '/api/edge-session/step']
        if path == '/api/edge-session/bind':
            request_origin = self.headers.get('Origin', '')
            valid = (self.headers.get('Host', '') == self.server.address and
                     (not request_origin or re.fullmatch(r'chrome-extension://[a-p]{32}', request_origin) is not None))
        else:
            valid = self.bridge_authorized() if bridge_request else (self.authorized() and self.headers.get('Origin') == 'http://' + self.server.address)
        if not valid:
            return self.send(403, {'error':'请求来源或本地授权无效'})
        if path == '/api/session/extract':
            if self.headers.get('Content-Type','').split(';')[0] != 'application/octet-stream':
                return self.send(415, {'error':'仅接受本机 DOCX / TXT 文件'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_FILE_BYTES:
                    raise ValueError('文件超过 10 MiB 上限或为空')
                payload = self.rfile.read(length)
                if len(payload) != length:
                    raise ValueError('文件上传未完成，请重试')
                result = extract_session(payload, self.headers.get('X-Session-File-Type', ''))
                return self.send(200, result)
            except (ValueError, KeyError, TypeError) as exc:
                return self.send(400, {'error': str(exc)[:160]})
            except Exception:
                return self.send(500, {'error':'文件解析失败，请检查格式后重试'})
        if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
            return self.send(415, {'error':'仅接受 JSON'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2000000:
                raise ValueError('请求大小不合法')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('请求格式不合法')
            if path == '/api/edge-session/bind':
                return self.send(200, self.server.engine.bind_edge_instance(data))
            if bridge_request:
                method = self.server.engine.edge_step if path.endswith('/step') else self.server.engine.receive_edge_session
                return self.send(200, method(data, scope=self.bridge_scope))
            if path == '/api/inventory/accounts':
                return self.send(200, self.server.store.inventory_import_accounts(data.get('items'), data.get('group', '')))
            if path == '/api/inventory/cards':
                return self.send(200, self.server.store.inventory_import_cards(data.get('sku'), data.get('codes')))
            if path.startswith('/api/inventory/'):
                return self.send(200, self.server.engine.inventory_command(path.rsplit('/',1)[-1], data))
            if path == '/api/session/direct-preview':
                return self.send(200, self.server.engine.session_direct_prepare(data))
            if path == '/api/session/direct-start':
                return self.send(200, self.server.engine.session_direct_start(data))
            if path == '/api/session/direct-task':
                return self.send(200, self.server.engine.session_direct_task(data))
            if path == '/api/account/update':
                return self.send(200, self.server.engine.update_account(data))
            if path == '/api/session':
                return self.send(200, self.server.engine.view_session(data.get('id')))
            if path == '/api/edge-session/pair':
                return self.send(200, {'bridge_token': self.server.bridge_token})
            if path == '/api/edge-session/probe':
                key = self.server.store.add_many([{'email': data.get('email')}])[0]
                self.server.store.update(key, probe_only=True, card='Session 测试 / 无卡密')
                try:
                    self.server.engine.command('edge_login', [key])
                except Exception:
                    self.server.store.delete(key)
                    raise
                return self.send(200, {'ok': True, 'record_id': key})
            if path == '/api/import':
                return self.send(200, {'ids': self.server.store.add_many(data.get('items'))})
            if path == '/api/eligibility':
                self.server.store.set_eligibility(data.get('ids'), data.get('eligibility_months'))
                return self.send(200, {'ok': True})
            if path == '/api/command':
                return self.send(200, self.server.engine.command(data.get('action'), data.get('ids', []), data.get('limit', 1)))
            if path == '/api/shutdown':
                self.send(200, {'ok':True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self.send(404, {'error':'未找到接口'})
        except (ValueError, KeyError, TypeError) as exc:
            self.send(400, {'error': str(exc)[:160]})
        except PartnerConfigurationError as exc:
            self.send(503, {'error': str(exc)[:160]})
        except Exception:
            self.send(500, {'error':'本地服务执行失败，请检查数据文件权限或重新启动'})


def make_server(data_dir=None, port=0, legacy_sources=None):
    import msvcrt
    directory = Path(data_dir).resolve() if data_dir is not None else data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / 'instance.lock').open('a+b')
    if (directory / 'instance.lock').stat().st_size == 0:
        lock.write(b'1')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        raise AlreadyRunningError('This data directory is already open in another workbench. Close it first.')
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.instance_lock = lock
    server.address = f'127.0.0.1:{server.server_port}'
    server.token = secrets.token_urlsafe(32)
    server.bridge_token = secrets.token_urlsafe(32)
    server.helper_seen = float('-inf')
    server.helper_version = ''
    server.helper_incognito = None
    try:
        server.store = Store(directory / 'records.sqlite3')
        sources = legacy_sources if legacy_sources is not None else ([ROOT/'data'/'records.sqlite3'] if data_dir is None else [])
        for source in sources:
            merge_v2_database(server.store,source)
        server.engine = Engine(server.store)
        server.engine.edge_origin = 'http://' + server.address
    except Exception:
        if hasattr(server,'store'):server.store.db.close()
        server.server_close()
        lock.close()
        raise
    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    server = make_server(port=args.port)
    print('GPT Plus Workbench running at http://' + server.address)
    print('Keep this window open. Close it to stop the application.')
    if not args.no_open:
        webbrowser.open('http://' + server.address + '/#' + server.token)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.engine.close()
        server.server_close()
        server.store.db.close()
        server.instance_lock.close()
