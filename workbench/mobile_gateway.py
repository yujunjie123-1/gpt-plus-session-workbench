"""Private Tailscale HTTPS gateway. Desktop service stays bound to loopback."""
import base64
import hashlib
import hmac
import json
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request

from .store import crypt
from .session_file_extract import MAX_FILE_BYTES

COOKIE = '__Host-workbench-phone'
REMOTE_POSTS = {'/api/inventory/accounts','/api/inventory/risk-accounts','/api/inventory/cards','/api/inventory/risk-pool',
    '/api/inventory/edit-account','/api/inventory/edit-card-spec','/api/inventory/copy-account','/api/inventory/session-auto','/api/inventory/refresh-sessions','/api/inventory/quote','/api/inventory/start','/api/inventory/session',
    '/api/inventory/task','/api/inventory/check-cards','/api/inventory/delete','/api/session/extract',
    '/api/account/update','/api/session','/api/import','/api/eligibility','/api/command'}
REMOTE_GETS = {'/api/ping','/api/inventory/state','/api/state','/api/export'}
WEB = Path(__file__).resolve().parents[1]/'web'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class MobileState:
    def __init__(self,root,remote_origin,clock=time.time):
        self.root=Path(root).resolve()
        parsed=urlsplit(remote_origin)
        if (parsed.scheme!='https' or not re.fullmatch(r'[a-z0-9-]+\.[a-z0-9-]+\.ts\.net',parsed.hostname or '')
                or parsed.port not in (None,443) or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('Expected this device Tailscale HTTPS origin')
        self.origin=remote_origin
        self.host=parsed.netloc
        self.clock=clock
        self.lock=threading.RLock()
        self.admin_token=secrets.token_urlsafe(32)
        self.config_path=self.root/'mobile-access.dpapi'
        if self.config_path.is_file():
            saved=json.loads(crypt(self.config_path.read_text(encoding='utf-8'),True))
            if saved.get('origin')!=remote_origin:raise ValueError('Mobile hostname changed; reconfigure explicitly')
            self.signing_key=base64.b64decode(saved['signing_key'],validate=True)
            if len(self.signing_key)!=32:raise ValueError('Invalid mobile configuration')
        else:
            self.signing_key=secrets.token_bytes(32)
            self.save()
        self.pair_hash=None
        self.pair_expires=0
        self.failures=[]
        self.paired_count=0

    def save(self):
        self.root.mkdir(parents=True,exist_ok=True)
        saved={'origin':self.origin,'signing_key':base64.b64encode(self.signing_key).decode('ascii')}
        pending=self.config_path.with_suffix('.pending')
        pending.write_text(crypt(json.dumps(saved)),encoding='utf-8')
        pending.replace(self.config_path)

    def new_code(self):
        with self.lock:
            code=f'{secrets.randbelow(100000000):08d}'
            self.pair_hash=hashlib.sha256(code.encode()).digest()
            self.pair_expires=self.clock()+600
            return {'code':code,'expires_in':600}

    def login(self,code):
        with self.lock:
            now=self.clock()
            self.failures=[value for value in self.failures if now-value<300]
            if len(self.failures)>=10:raise ValueError('尝试次数过多，请五分钟后再试')
            valid=(isinstance(code,str) and re.fullmatch(r'\d{8}',code) and self.pair_hash is not None
                   and now<self.pair_expires and secrets.compare_digest(hashlib.sha256(code.encode()).digest(),self.pair_hash))
            if not valid:
                self.failures.append(now)
                raise ValueError('配对码不正确或已过期，请在电脑重新生成')
            self.pair_hash=None
            self.failures=[]
            self.paired_count+=1
            payload=f'{int(now)+30*86400}.{secrets.token_urlsafe(24)}'
            signature=hmac.new(self.signing_key,payload.encode(),hashlib.sha256).hexdigest()
            return payload+'.'+signature

    def valid_session(self,cookie):
        if not isinstance(cookie,str) or len(cookie)>256:return False
        pieces=cookie.split('.')
        if (len(pieces)!=3 or not pieces[0].isdigit() or not re.fullmatch(r'[A-Za-z0-9_-]{32}',pieces[1])
                or not re.fullmatch(r'[a-f0-9]{64}',pieces[2])):return False
        now=self.clock();expiry=int(pieces[0])
        if not now<expiry<=now+30*86400+60:return False
        expected=hmac.new(self.signing_key,('.'.join(pieces[:2])).encode(),hashlib.sha256).hexdigest()
        return secrets.compare_digest(expected,pieces[2])

    def revoke(self):
        with self.lock:
            self.signing_key=secrets.token_bytes(32)
            self.pair_hash=None
            self.paired_count=0
            self.save()

    def backend(self):
        runtime=json.loads((self.root/'runtime-info.json').read_text(encoding='utf-8'))
        launch=urlsplit(crypt(runtime['launch_blob'],True))
        if (launch.scheme!='http' or launch.hostname!='127.0.0.1' or not launch.port or launch.path!='/'
                or launch.query or launch.username or launch.password or not 32<=len(launch.fragment)<=128
                or runtime.get('url')!=f'http://127.0.0.1:{launch.port}'):
            raise RuntimeError('Invalid workbench backend')
        return runtime['url'],launch.fragment


class MobileHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def send(self,status,body,mime='application/json; charset=utf-8',extra=None):
        if isinstance(body,(dict,list)):body=json.dumps(body,ensure_ascii=False).encode('utf-8')
        elif isinstance(body,str):body=body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store, private')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        if self.headers.get('Host')==self.server.state.host:self.send_header('Strict-Transport-Security','max-age=31536000')
        headers=extra or {}
        if 'Content-Security-Policy' not in headers:
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for name,value in headers.items():self.send_header(name,value)
        self.end_headers()
        self.wfile.write(body)

    def remote(self):
        return self.headers.get('Host')==self.server.state.host

    def local(self):
        return self.headers.get('Host')==self.server.local_address

    def admin(self):
        return self.local() and secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.state.admin_token)

    def authenticated(self):
        if not self.remote():return False
        try:
            cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''))
            return self.server.state.valid_session(cookie[COOKIE].value if COOKIE in cookie else '')
        except Exception:return False

    def do_GET(self):
        path=urlsplit(self.path).path
        if not self.remote() and not self.local():return self.send(403,{'error':'访问地址无效'})
        if path=='/mobile/setup' and self.local():
            return self.send(200,(WEB/'mobile-setup.html').read_bytes(),'text/html; charset=utf-8')
        if path=='/mobile/admin-state':
            if not self.admin():return self.send(403,{'error':'请在电脑打开手机连接设置'})
            try:self.server.state.backend();running=True
            except Exception:running=False
            return self.send(200,{'origin':self.server.state.origin,'workbench_running':running,'paired_since_start':self.server.state.paired_count})
        if path in ('/mobile/login.js','/mobile/setup.js','/mobile/mobile.css'):
            file=WEB/Path(path).name
            return self.send(200,file.read_bytes(),'text/javascript; charset=utf-8' if file.suffix=='.js' else 'text/css; charset=utf-8')
        if path=='/mobile/login' or (path=='/' and self.remote() and not self.authenticated()):
            return self.send(200,(WEB/'mobile-login.html').read_bytes(),'text/html; charset=utf-8')
        if not self.authenticated():return self.send(401,{'error':'请先在手机入口输入电脑显示的配对码'})
        if path=='/' or (path.startswith('/assets/') and re.fullmatch(r'/assets/[A-Za-z0-9_.-]+',path)) or path in REMOTE_GETS:
            return self.proxy()
        return self.send(404,{'error':'手机入口不提供此接口'})

    def do_POST(self):
        path=urlsplit(self.path).path
        local_admin=path in ('/mobile/code','/mobile/revoke')
        if local_admin:
            if not self.admin() or self.headers.get('Origin')!='http://'+self.server.local_address:
                return self.send(403,{'error':'请在电脑打开手机连接设置'})
        elif not self.remote() or self.headers.get('Origin')!=self.server.state.origin:
            return self.send(403,{'error':'请求来源不正确'})
        content_type=self.headers.get('Content-Type','').split(';')[0]
        if path=='/api/session/extract':
            if content_type!='application/octet-stream':return self.send(415,{'error':'仅接受 DOCX / TXT 文件'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=MAX_FILE_BYTES:raise ValueError()
                body=self.rfile.read(length)
                if len(body)!=length:raise ValueError()
                file_type=self.headers.get('X-Session-File-Type','')
                if file_type not in ('docx','txt'):return self.send(400,{'error':'仅支持 .docx 和 .txt 文件'})
                if not self.authenticated():return self.send(401,{'error':'手机授权已过期，请重新配对'})
                return self.proxy(body,'application/octet-stream',{'X-Session-File-Type':file_type})
            except (ValueError,TypeError):return self.send(400,{'error':'文件大小不正确'})
        if content_type!='application/json':return self.send(415,{'error':'仅接受 JSON'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=2000000:raise ValueError('请求大小不正确')
            body=self.rfile.read(length)
            payload=json.loads(body)
            if not isinstance(payload,dict):raise ValueError('请求格式不正确')
            if path=='/mobile/code':return self.send(200,self.server.state.new_code())
            if path=='/mobile/revoke':
                self.server.state.revoke()
                return self.send(200,{'ok':True})
            if path=='/mobile/login':
                try:token=self.server.state.login(payload.get('code'))
                except ValueError as error:return self.send(400,{'error':str(error)})
                return self.send(200,{'ok':True},extra={'Set-Cookie':COOKIE+'='+token+'; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=2592000'})
            if path=='/mobile/logout':
                return self.send(200,{'ok':True},extra={'Set-Cookie':COOKIE+'=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0'})
            if not self.authenticated():return self.send(401,{'error':'手机授权已过期，请重新配对'})
            if path not in REMOTE_POSTS:return self.send(404,{'error':'手机入口不提供此接口'})
            return self.proxy(body)
        except (ValueError,TypeError):return self.send(400,{'error':'请求格式不正确'})

    def proxy(self,body=None,content_type='application/json',extra_headers=None):
        try:
            origin,token=self.server.state.backend()
            headers={'Authorization':'Bearer '+token}
            if body is not None:
                headers.update({'Origin':origin,'Content-Type':content_type})
                headers.update(extra_headers or {})
            request=urllib.request.Request(origin+self.path,headers=headers,data=body)
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
            try:response=opener.open(request,timeout=20)
            except urllib.error.HTTPError as error:response=error
            with response:
                data=response.read(12000001)
                if len(data)>12000000:raise RuntimeError('Response too large')
                extra={key:response.headers[key] for key in ('Content-Security-Policy',) if key in response.headers}
                return self.send(response.status,data,response.headers.get('Content-Type','application/octet-stream'),extra)
        except Exception:return self.send(503,{'error':'电脑上的工作台未运行或连接暂不可用，请启动工作台后重试'})


def make_mobile_server(root,remote_origin,port=51931,clock=time.time):
    server=ThreadingHTTPServer(('127.0.0.1',port),MobileHandler)
    server.daemon_threads=True
    try:server.state=MobileState(root,remote_origin,clock)
    except Exception:server.server_close();raise
    server.local_address=f'127.0.0.1:{server.server_port}'
    return server
