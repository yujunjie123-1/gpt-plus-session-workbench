"""Account/card/history durability across processes and different extracted copies."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from workbench.store import Store,crypt
from workbench.data_migration import merge_v2_database
from workbench.paths import data_directory


class PersistentDataTest(unittest.TestCase):
    def test_user_directory_is_absolute_and_independent_of_code_directory(self):
        with TemporaryDirectory() as temporary,patch('workbench.paths.windows_user_profile',return_value=Path(temporary)):
            expected=Path(temporary).resolve()/'GPTPlusWorkbench'/'data'
            for inherited in ('',str(Path(temporary)/'redirected-package-cache')):
                with patch.dict(os.environ,{'LOCALAPPDATA':inherited}):
                    self.assertEqual(data_directory(),expected)
                    self.assertTrue(data_directory().is_absolute())
        with patch.dict(os.environ,{'GPTPLUS_WORKBENCH_STORAGE_ROOT':''}):
            with self.assertRaises(RuntimeError):data_directory()
        with patch('workbench.paths.windows_user_profile',side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):data_directory()

    def test_merge_keeps_two_sources_credentials_stock_history_and_never_reimports_deleted_data(self):
        with TemporaryDirectory() as temporary:
            base=Path(temporary)
            source=Store(base/'old-one'/'records.sqlite3')
            other=Store(base/'old-two'/'records.sqlite3')
            shared=Store(base/'shared'/'data'/'records.sqlite3')
            try:
                account=source.inventory_import_accounts([{'email':'owner@example.com','password':'OLD-PASSWORD'}])['ids'][0]
                source.inventory_import_cards('plus',['USED-CARD-0001'])
                task=source.inventory_reserve(source.inventory_quote([account],'plus'))[0]
                source.update(account,status='completed',submitted=True,message='TEST SUCCESS')
                source.rows[account]['updated']='2000-01-01T00:00:00+00:00'
                source._save(source.rows[account])
                second=other.inventory_import_accounts([{'email':'OWNER@example.com','password':'NEW-PASSWORD'},{'email':'second@example.com'}])['ids'][0]
                other.inventory_import_cards('plus',['USED-CARD-0001','UNUSED-CARD-0002'])
                first_result=merge_v2_database(shared,source.path)
                second_result=merge_v2_database(shared,other.path)
                self.assertEqual(first_result['tasks'],1)
                self.assertEqual(second_result['accounts'],1)
                self.assertEqual(len(shared.rows),2)
                self.assertEqual(len(shared.cards),2)
                self.assertEqual(len(shared.charge_tasks),1)
                self.assertEqual(shared.charge_tasks[task]['status'],'completed')
                owner=next(row for row in shared.rows.values() if row['email'].lower()=='owner@example.com')
                self.assertEqual(crypt(owner['password'],True),'NEW-PASSWORD')
                self.assertEqual(owner['last_recharge']['status'],'completed')
                self.assertEqual(sorted(card['status'] for card in shared.cards.values()),['unused','used'])
                self.assertGreaterEqual(len(list((base/'shared'/'backups').glob('*.sqlite3'))),4)
                extra=next(row['id'] for row in shared.rows.values() if row['email']=='second@example.com')
                shared.inventory_delete('accounts',[extra])
                self.assertTrue(merge_v2_database(shared,other.path)['skipped'])
                self.assertNotIn(extra,shared.rows)
            finally:
                source.db.close();other.db.close();shared.db.close()

    def test_conflicting_skus_do_not_partly_import(self):
        with TemporaryDirectory() as temporary:
            source=Store(Path(temporary)/'source.sqlite3')
            shared=Store(Path(temporary)/'shared.sqlite3')
            try:
                source.inventory_import_accounts([{'email':'new@example.com'}])
                source.inventory_import_cards('pro100',['CONFLICT-CARD'])
                shared.inventory_import_cards('plus',['CONFLICT-CARD'])
                with self.assertRaises(ValueError):merge_v2_database(shared,source.path)
                self.assertEqual(shared.rows,{})
                self.assertEqual(len(shared.cards),1)
            finally:source.db.close();shared.db.close()

    def test_http_writes_survive_process_restart_from_a_different_application_directory(self):
        script=r'''
import json,sys,threading,urllib.request
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import app
app.ROOT=Path(sys.argv[2])
server=app.make_server()
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def request(path,body=None):
    headers={'Authorization':'Bearer '+server.token,'Content-Type':'application/json','Origin':'http://'+server.address}
    req=urllib.request.Request('http://'+server.address+path,headers=headers,data=json.dumps(body).encode() if body is not None else None)
    with opener.open(req,timeout=5) as response:return json.load(response)
try:
    if sys.argv[3]=='write':
        key=request('/api/inventory/accounts',{'items':[{'email':'persist@example.com','password':'TEST-PASSWORD'}]})['ids'][0]
        request('/api/inventory/cards',{'sku':'plus','codes':['PERSIST-CARD-0001','PERSIST-CARD-0002']})
        quote=request('/api/inventory/quote',{'ids':[key],'sku':'plus'})
        request('/api/command',{'action':'pause'})
        task=request('/api/inventory/start',{'quote':quote['quote']})['tasks'][0]
        request('/api/inventory/task',{'id':task,'action':'stop'})
    state=request('/api/inventory/state')
    print(json.dumps({'accounts':len(state['accounts']),'cards':len(state['cards']),'tasks':len(state['tasks']),
        'statuses':[task['status'] for task in state['tasks']],'database':state['storage']['database']}))
finally:
    server.shutdown();thread.join(3);server.engine.close();server.server_close();server.store.db.close();server.instance_lock.close()
'''
        with TemporaryDirectory() as temporary:
            base=Path(temporary)
            env=dict(os.environ,GPTPLUS_WORKBENCH_STORAGE_ROOT=str(base/'user-data'/'GPTPlusWorkbench'))
            results=[]
            for stage,name in [('write','copy-A'),('read','copy-B'),('read','copy-A')]:
                code_root=base/name;code_root.mkdir(exist_ok=True)
                result=subprocess.run([sys.executable,'-c',script,str(ROOT),str(code_root),stage],cwd=str(code_root),env=env,
                                      capture_output=True,text=True,timeout=25,creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.returncode,0,result.stderr)
                results.append(json.loads(result.stdout))
                self.assertFalse((code_root/'data'/'records.sqlite3').exists())
            for result in results:
                self.assertEqual((result['accounts'],result['cards'],result['tasks']),(1,2,1))
                self.assertEqual(result['statuses'],['stopped'])
            self.assertEqual(len({result['database'] for result in results}),1)


if __name__=='__main__':unittest.main(verbosity=2)
