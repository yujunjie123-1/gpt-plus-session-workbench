"""HTTPS-facing protocol tests via a local fixture; no real account operations."""
import json
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from workbench.mobile_gateway import make_mobile_server,MobileState,COOKIE
from workbench.store import crypt


class Backend(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        assert self.headers['Host']==self.server.address
        assert self.headers['Authorization']=='Bearer '+self.server.token
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
        self.wfile.write(json.dumps({'ok':True,'accounts':[]}).encode())
    def do_POST(self):
        assert self.headers['Origin']=='http://'+self.server.address
        body=self.rfile.read(int(self.headers['Content-Length']))
        if self.headers.get('Content-Type')=='application/octet-stream':
            self.server.received={'binary':body,'file_type':self.headers.get('X-Session-File-Type')}
        else:
            self.server.received=json.loads(body)
        self.do_GET()


class MobileGatewayTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.origin='https://fixture.tailtest.ts.net';self.host='fixture.tailtest.ts.net'
        self.backend=ThreadingHTTPServer(('127.0.0.1',0),Backend)
        self.backend.address=f'127.0.0.1:{self.backend.server_port}';self.backend.token='x'*43
        self.backend_thread=threading.Thread(target=self.backend.serve_forever,daemon=True);self.backend_thread.start()
        upstream='http://'+self.backend.address
        (self.root/'runtime-info.json').write_text(json.dumps({'url':upstream,'launch_blob':crypt(upstream+'/#'+self.backend.token)}),encoding='utf-8')
        self.clock=[1000000.0]
        self.server=make_mobile_server(self.root,self.origin,0,lambda:self.clock[0])
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown();self.thread.join(3);self.server.server_close()
        self.backend.shutdown();self.backend_thread.join(3);self.backend.server_close();self.temp.cleanup()

    def request(self,path,body=None,cookie=None,admin=False,host=None,origin=None):
        headers={'Host':host or (self.server.local_address if admin else self.host)}
        if cookie:headers['Cookie']=COOKIE+'='+cookie
        if admin:headers['Authorization']='Bearer '+self.server.state.admin_token
        if body is not None:
            headers.update({'Content-Type':'application/json','Origin':origin or ('http://'+self.server.local_address if admin else self.origin)})
        req=urllib.request.Request('http://'+self.server.local_address+path,headers=headers,data=json.dumps(body).encode() if body is not None else None)
        with self.opener.open(req,timeout=5) as response:return response.status,response.headers,response.read()

    def request_bytes(self,path,body,cookie=None,file_type='txt'):
        headers={'Host':self.host,'Cookie':COOKIE+'='+cookie,'Origin':self.origin,
                 'Content-Type':'application/octet-stream','X-Session-File-Type':file_type}
        req=urllib.request.Request('http://'+self.server.local_address+path,headers=headers,data=body)
        with self.opener.open(req,timeout=5) as response:return response.status,response.headers,response.read()

    def paired(self):
        _,_,data=self.request('/mobile/code',{},admin=True)
        code=json.loads(data)['code']
        _,headers,_=self.request('/mobile/login',{'code':code})
        cookie=SimpleCookie();cookie.load(headers['Set-Cookie'])
        return cookie[COOKIE].value,code,headers['Set-Cookie']

    def test_pair_cookie_and_proxy_do_not_expose_desktop_token(self):
        _,_,page=self.request('/')
        self.assertIn('配对码'.encode(),page)
        with self.assertRaises(urllib.error.HTTPError) as error:self.request('/api/inventory/state')
        self.assertEqual(error.exception.code,401)
        cookie,code,header=self.paired()
        for attribute in ('Secure','HttpOnly','SameSite=Strict'):self.assertIn(attribute,header)
        self.assertNotIn(self.backend.token,cookie)
        self.assertNotIn(code,cookie)
        _,_,data=self.request('/api/inventory/state',cookie=cookie)
        self.assertTrue(json.loads(data)['ok'])
        _,_,data=self.request('/api/inventory/accounts',{'items':[{'email':'fixture@example.com'}]},cookie=cookie)
        self.assertEqual(self.backend.received['items'][0]['email'],'fixture@example.com')
        self.request('/api/inventory/risk-accounts',{'items':[{'email':'risk@example.com'}]},cookie=cookie)
        self.assertEqual(self.backend.received['items'][0]['email'],'risk@example.com')
        self.request('/api/inventory/delete',{'kind':'cards','ids':['used-fixture']},cookie=cookie)
        self.assertEqual(self.backend.received,{'kind':'cards','ids':['used-fixture']})
        self.request('/api/inventory/copy-account',{'id':'copy-fixture'},cookie=cookie)
        self.assertEqual(self.backend.received,{'id':'copy-fixture'})
        self.request('/api/inventory/session-auto',{'enabled':False},cookie=cookie)
        self.assertEqual(self.backend.received,{'enabled':False})
        self.request('/api/inventory/refresh-sessions',{},cookie=cookie)
        self.assertEqual(self.backend.received,{})
        self.request('/api/inventory/edit-card-spec',{'id':'fixture','sku':'plus_half','expected_sku':'plus'},cookie=cookie)
        self.assertEqual(self.backend.received,{'id':'fixture','sku':'plus_half','expected_sku':'plus'})
        self.assertNotIn(self.backend.token.encode(),data)
        self.assertNotIn(code,(self.root/'mobile-access.dpapi').read_text(encoding='utf-8'))

    def test_session_file_extract_route_forwards_binary_payload(self):
        cookie,_,_=self.paired()
        payload=b'{"synthetic":"document"}'
        _,_,data=self.request_bytes('/api/session/extract',payload,cookie=cookie)
        self.assertTrue(json.loads(data)['ok'])
        self.assertEqual(self.backend.received,{'binary':payload,'file_type':'txt'})

    def test_wrong_origin_host_and_internal_routes_rejected(self):
        cookie,_,_=self.paired()
        cases=[('/api/inventory/accounts',{},dict(cookie=cookie,origin='https://evil.test')),
               ('/api/inventory/copy-account',{'id':'fixture'},dict(cookie=cookie,origin='https://evil.test')),
               ('/api/inventory/copy-account',{'id':'fixture'},{}),
               ('/api/inventory/edit-card-spec',{'id':'fixture','sku':'plus_half','expected_sku':'plus'},{}),
               ('/api/inventory/edit-card-spec',{'id':'fixture','sku':'plus_half','expected_sku':'plus'},dict(cookie=cookie,origin='https://evil.test')),
               ('/api/inventory/state',None,dict(cookie=cookie,host='evil.test')),
               ('/mobile/code',{},dict(cookie=cookie)),
               ('/api/edge-session/pair',{},dict(cookie=cookie)),
               ('/api/shutdown',{},dict(cookie=cookie)),
               ('/api/edge-session/jobs',None,dict(cookie=cookie))]
        for path,body,options in cases:
            with self.subTest(path=path),self.assertRaises(urllib.error.HTTPError):self.request(path,body,**options)

    def test_single_use_expiry_bruteforce_and_revocation(self):
        cookie,code,_=self.paired()
        with self.assertRaises(urllib.error.HTTPError):self.request('/mobile/login',{'code':code})
        next_code=self.server.state.new_code()['code'];self.clock[0]+=601
        with self.assertRaises(ValueError):self.server.state.login(next_code)
        next_code=self.server.state.new_code()['code']
        for _ in range(10):
            try:self.server.state.login('not-a-code')
            except ValueError:pass
        with self.assertRaisesRegex(ValueError,'五分钟'):self.server.state.login(next_code)
        self.clock[0]+=301
        self.assertTrue(self.server.state.valid_session(self.server.state.login(next_code)))
        self.assertTrue(self.server.state.valid_session(cookie))
        self.request('/mobile/revoke',{},admin=True)
        self.assertFalse(self.server.state.valid_session(cookie))

    def test_phone_cookie_survives_gateway_restart_and_expires(self):
        cookie,_,_=self.paired()
        state=MobileState(self.root,self.origin,lambda:self.clock[0])
        self.assertTrue(state.valid_session(cookie))
        self.clock[0]+=30*86400+1
        self.assertFalse(state.valid_session(cookie))

    def test_stopped_backend_has_clear_error_and_public_assets_have_expected_mime(self):
        for path,mime in [('/mobile/login.js','text/javascript'),('/mobile/setup.js','text/javascript'),('/mobile/mobile.css','text/css')]:
            _,headers,data=self.request(path);self.assertTrue(headers['Content-Type'].startswith(mime));self.assertTrue(data)
        cookie,_,_=self.paired()
        (self.root/'runtime-info.json').unlink()
        with self.assertRaises(urllib.error.HTTPError) as error:self.request('/api/inventory/state',cookie=cookie)
        self.assertEqual(error.exception.code,503)
        self.assertIn('工作台未运行'.encode(),error.exception.read())


if __name__=='__main__':unittest.main(verbosity=2)
