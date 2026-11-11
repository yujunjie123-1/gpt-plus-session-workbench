"""Five native InPrivate windows; local fixture only, never navigates to GPT."""
import ctypes
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from app import Handler, make_server
from workbench.browser_profiles import PUBLIC_KEY, prepare_helper, prepare_profile_template, _initialize
from workbench.edge_session import open_ordinary_edge
from test_edge_session import fixture


def wait_for(predicate, seconds=45):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.1)
    raise AssertionError('Native local-only isolation fixture did not settle')


class FixtureHandler(Handler):
    def do_GET(self):
        parsed=urlsplit(self.path)
        if parsed.path=='/test/fixture':
            instance=parse_qs(parsed.query)['instance'][0]
            self.server.initial_cookies[instance]=self.headers.get('Cookie','')
            return self.send(200,'<!doctype html><html><head><title>Workbench Isolation Fixture '+instance[:8]+'</title></head><body><h1>Local isolated account</h1><script src="/test/fixture.js"></script></body></html>','text/html; charset=utf-8')
        if parsed.path=='/test/fixture.js':
            return self.send(200,"const instance=new URL(location.href).searchParams.get('instance');window.testPrevious=localStorage.getItem('isolation_marker');localStorage.setItem('isolation_marker',instance);document.cookie='isolation_probe='+instance+'; path=/; SameSite=Lax';",'text/javascript')
        return super().do_GET()

    def do_POST(self):
        if urlsplit(self.path).path=='/test/proof':
            if not self.bridge_authorized() or not self.bridge_scope:
                return self.send(403,{'error':'Scope required'})
            data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if data['instance_id']!=self.bridge_scope:
                return self.send(403,{'error':'Wrong instance'})
            self.server.proofs[self.bridge_scope]=data
            return self.send(200,{'ok':True})
        return super().do_POST()


@unittest.skipUnless(os.name=='nt' and os.environ.get('RUN_ISOLATED_EDGE_TESTS')=='1','Opt-in native Edge windows')
class NativeIsolationTest(unittest.TestCase):
    def test_actual_helper_bootstrap_profiles_inprivate_and_cookie_isolation(self):
        with TemporaryDirectory() as temporary:
            base=Path(temporary).resolve()
            helper=prepare_helper(base/'fixture-helper-root')
            # Replace only this test copy's destination. Production remains the
            # actual GPT login URL, while this test contacts loopback exclusively.
            server=make_server(base/'data')
            server.RequestHandlerClass=FixtureHandler
            server.proofs={};server.initial_cookies={}
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            origin='http://'+server.address
            background=(helper/'background.js').read_text(encoding='utf-8')
            original="await chrome.tabs.update(tab.id, {url:'https://chatgpt.com/auth/login#wb_login=' + paired.request_id});"
            self.assertIn(original,background)
            background=background.replace(original,"await chrome.tabs.update(tab.id, {url:'"+origin+"/test/fixture?instance=' + paired.instance_id});")
            background+='''
setInterval(async()=>{try{
  const {connection}=await chrome.storage.session.get('connection');if(!connection?.instance_id)return;
  const tabs=await chrome.tabs.query({url:connection.origin+'/test/fixture*'});
  for(const tab of tabs){if(!tab.incognito)continue;
    const store=(await chrome.cookies.getAllCookieStores()).find(item=>item.tabIds.includes(tab.id));if(!store)continue;
    const cookie=await chrome.cookies.get({url:connection.origin,name:'isolation_probe',storeId:store.id});
    const result=(await chrome.scripting.executeScript({target:{tabId:tab.id},world:'MAIN',func:()=>({stored:localStorage.getItem('isolation_marker'),previous:window.testPrevious})}))[0]?.result;
    if(!cookie||!result?.stored)continue;
    await bridgeFetch(connection,'/test/proof',{instance_id:connection.instance_id,incognito:tab.incognito,cookie:cookie.value,...result});
  }
}catch{}},500);
'''
            (helper/'background.js').write_text(background,encoding='utf-8')
            handles=[]
            launch_times=[]
            def launch(*args,**kwargs):
                start=time.monotonic()
                handle=open_ordinary_edge(*args,**kwargs);handles.append(handle)
                launch_times.append(time.monotonic()-start)
                return handle
            try:
                with patch('workbench.browser_profiles.prepare_helper',return_value=helper),patch('workbench.edge_session.open_ordinary_edge',side_effect=launch),patch('workbench.browser_profiles._initialize',wraps=_initialize) as initialized:
                    start=time.monotonic()
                    prepare_profile_template(server.store.path.parent/'browser-profiles',threading.Event())
                    cold_seconds=time.monotonic()-start
                    start=time.monotonic()
                    prepare_profile_template(server.store.path.parent/'browser-profiles',threading.Event())
                    cached_seconds=time.monotonic()-start
                    keys=server.store.inventory_import_accounts([{'email':f'native-{index}@example.com'} for index in range(5)])['ids']
                    self.assertEqual((server.engine.limit,server.engine.session_limit),(1,5))
                    start=time.monotonic()
                    server.engine.inventory_command('session',{'ids':keys})
                    wait_for(lambda:len(server.proofs)==5)
                    all_visible_seconds=time.monotonic()-start
                    self.assertEqual(initialized.call_count,1)
                    self.assertEqual(len(handles),5)
                    self.assertEqual(len({handle.profile for handle in handles}),5)
                    self.assertEqual(len({handle.process.pid for handle in handles}),5)
                    self.assertEqual(len(server.engine.sessions),0)
                    for handle in handles:
                        self.assertFalse((handle.profile/'DevToolsActivePort').exists())
                        self.assertNotIn('--remote-debugging-port=0',handle.process.args)
                        self.assertIn('--inprivate',handle.process.args)
                    for instance,proof in server.proofs.items():
                        self.assertTrue(proof['incognito'])
                        self.assertEqual(proof['cookie'],instance)
                        self.assertEqual(proof['stored'],instance)
                        self.assertIsNone(proof['previous'])
                        self.assertEqual(server.initial_cookies[instance],'')
                    titles=[]
                    callback_type=ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)
                    def enum(hwnd,extra):
                        if ctypes.windll.user32.IsWindowVisible(hwnd):
                            value=ctypes.create_unicode_buffer(1024);ctypes.windll.user32.GetWindowTextW(hwnd,value,1024)
                            if 'Workbench Isolation Fixture' in value.value:titles.append(value.value)
                        return True
                    ctypes.windll.user32.EnumWindows(callback_type(enum),0)
                    self.assertEqual(len(titles),5)
                    self.assertTrue(all('InPrivate' in title for title in titles))
                    for job in server.engine.edge_jobs():
                        server.engine.receive_edge_session({**job,'session':fixture(job['email'])},scope=job['instance_id'])
                    wait_for(lambda:all(server.engine.jobs[key].done() for key in keys),25)
                    self.assertTrue(all(handle.process.poll() is not None for handle in handles))
                    self.assertTrue(all(not handle.profile.exists() for handle in handles))
                    self.assertEqual(len(server.engine.sessions),5)
                    report={'native_windows':5,'inprivate_windows':5,'default_session_concurrency':5,'recharge_concurrency':1,
                            'cold_preparation_seconds':round(cold_seconds,3),'cached_preparation_seconds':round(cached_seconds,3),
                            'launch_call_seconds':[round(value,3) for value in launch_times],
                            'five_fixture_windows_connected_seconds':round(all_visible_seconds,3),
                            'initialization_count':initialized.call_count,'distinct_profiles':True,'same_origin_cookies_isolated':True,
                            'same_origin_local_storage_isolated':True,'actual_login_has_no_debugger':True,'helper_autoconnected':True,
                            'windows_and_profiles_cleaned':True,'only_loopback_fixture_visited':True,'real_login_or_redemption':False}
                    destination=ROOT/'output'/'v222-native-isolation.json';destination.parent.mkdir(parents=True,exist_ok=True)
                    destination.write_text(json.dumps(report,indent=2),encoding='utf-8')
            finally:
                server.shutdown();thread.join(3);server.engine.close();server.server_close();server.store.db.close();server.instance_lock.close()
                for handle in handles:handle.close()


if __name__=='__main__':unittest.main(verbosity=2)
