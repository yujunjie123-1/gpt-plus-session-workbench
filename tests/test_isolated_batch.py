"""Scope boundaries and true concurrent login scheduling without real services."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tests'))
from workbench.engine import Engine
from workbench.store import Store
from workbench.browser_profiles import launch_isolated_edge, _remove_profile, _stop
from test_edge_session import fixture, wait_until
from test_inventory import StockProvider
from app import make_server


class Handle:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class ExitedProcess:
    pid = 987654

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class IsolatedBatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'records.sqlite3')
        self.engine = Engine(self.store)
        self.engine.edge_origin = 'http://127.0.0.1:12345'
        self.handles = []

        def launch(*args, **kwargs):
            handle = Handle()
            self.handles.append(handle)
            return handle
        self.launcher = patch('workbench.edge_session.open_ordinary_edge', side_effect=launch)
        self.launch = self.launcher.start()

    def tearDown(self):
        self.engine.close()
        self.launcher.stop()
        self.store.db.close()
        self.temp.cleanup()

    def accounts(self, count):
        return self.store.inventory_import_accounts([{'email':f'isolated-{index}@example.com','password':'SYNTHETIC-PASSWORD'}
                                                     for index in range(count)])['ids']

    def test_stop_kills_children_when_edge_parent_already_exited(self):
        profile=Path(self.temp.name)/'profile'
        with patch('workbench.browser_profiles._profile_process_ids',side_effect=[[123456,123457],[]]), \
             patch('workbench.browser_profiles.subprocess.run') as run:
            _stop(ExitedProcess(),profile)
        commands=[call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0],['taskkill','/PID','987654','/T','/F'])
        self.assertIn(['taskkill','/PID','123456','/T','/F'],commands)
        self.assertIn(['taskkill','/PID','123457','/T','/F'],commands)

    def begin(self, keys, limit):
        self.engine.command('session-limit', [], limit)
        self.engine.inventory_command('session', {'ids':keys})
        wait_until(lambda: len(self.engine.edge_jobs()) == min(limit, 5, len(keys)))

    def bind(self, job):
        request = self.engine.edge_requests[job['record_id']]
        return self.engine.bind_edge_instance({'instance_id':job['instance_id'],
            'bootstrap_nonce':request['private']['bootstrap_nonce'],'incognito':True})

    def receive(self, job):
        return self.engine.receive_edge_session({**job,'session':fixture(job['email'])}, scope=job['instance_id'])

    def test_ten_logins_are_queued_with_a_maximum_of_five_active_instances(self):
        keys = self.accounts(10)
        self.begin(keys, 5)
        self.assertEqual(len({job['instance_id'] for job in self.engine.edge_jobs()}), 5)
        self.engine.command('session-limit', [], 10)
        self.assertEqual(self.engine.session_limit, 5)
        self.assertEqual(len(self.engine.edge_jobs()), 5)
        seen = set()
        while len(self.engine.sessions) < len(keys):
            wait_until(lambda: any(job['request_id'] not in seen for job in self.engine.edge_jobs()))
            jobs = [job for job in self.engine.edge_jobs() if job['request_id'] not in seen]
            seen.update(job['request_id'] for job in jobs)
            for job in jobs:
                self.bind(job)
            with ThreadPoolExecutor(max_workers=5) as pool:
                list(pool.map(self.receive, reversed(jobs)))
            wait_until(lambda: len(self.engine.sessions) >= min(len(keys), len(seen)))
        wait_until(lambda: self.launch.call_count == 10)
        self.assertEqual(len(seen), 10)
        self.assertEqual(len({call.kwargs['bootstrap_url'] for call in self.launch.call_args_list}), 10)
        wait_until(lambda: all(self.engine.jobs[key].done() for key in keys))
        self.assertEqual(len(self.engine.sessions), 10)
        self.assertTrue(all(self.engine.sessions[key]['user']['email'] == self.store.get(key)['email'] for key in keys))
        self.assertTrue(all(handle.closed for handle in self.handles))
        self.assertFalse(self.store.cards)

    def test_account_pool_defaults_to_five_even_when_recharge_limit_is_one(self):
        keys=self.accounts(6)
        self.assertEqual((self.engine.limit,self.engine.session_limit),(1,5))
        self.engine.inventory_command('session',{'ids':keys})
        wait_until(lambda:len(self.engine.edge_jobs())==5)
        state=self.engine.inventory_snapshot()
        self.assertEqual((state['session_active'],state['session_queued']),(5,1))
        first=self.engine.edge_jobs()[0]
        self.receive(first)
        wait_until(lambda:self.engine.jobs[first['record_id']].done())
        wait_until(lambda:keys[-1] in [job['record_id'] for job in self.engine.edge_jobs()])
        self.assertEqual(len(self.engine.edge_jobs()),5)
        self.assertEqual(len(self.engine.sessions),1)
        self.assertEqual(self.engine.limit,1)

    def test_session_and_recharge_settings_are_independent_and_persist(self):
        self.engine.command('session-limit',[],7)
        self.engine.command('limit',[],2)
        self.engine.close()
        self.engine=Engine(self.store)
        self.assertEqual((self.engine.session_limit,self.engine.limit),(5,2))

    def test_tokens_are_scoped_single_use_and_do_not_appear_in_state_or_database(self):
        keys = self.accounts(2)
        self.begin(keys, 2)
        first, second = self.engine.edge_jobs()
        request = self.engine.edge_requests[first['record_id']]
        bootstrap = request['private']['bootstrap_nonce']
        one, two = self.bind(first), self.bind(second)
        self.assertNotEqual(one['token'], two['token'])
        self.assertEqual(self.engine.edge_token_scope(one['token']), first['instance_id'])
        self.assertEqual(self.engine.edge_jobs(first['instance_id']), [first])
        self.assertEqual(self.engine.edge_jobs(''), [])
        with self.assertRaises(ValueError):
            self.engine.bind_edge_instance({'instance_id':first['instance_id'],'bootstrap_nonce':bootstrap,'incognito':True})
        for kind in ('email', 'password', 'totp', 'email_code'):
            with self.assertRaises(ValueError):
                self.engine.edge_step({**second,'kind':kind}, scope=first['instance_id'])
        with self.assertRaises(ValueError):
            self.engine.receive_edge_session({**second,'session':fixture(second['email'])}, scope=first['instance_id'])
        with self.assertRaises(ValueError):
            self.engine.receive_edge_session({**first,'session':fixture(second['email'])}, scope=first['instance_id'])
        public = json.dumps(self.engine.inventory_snapshot())
        database = self.store.path.read_bytes()
        for secret in (bootstrap, one['token'], two['token'], first['request_id'], 'SYNTHETIC-PASSWORD'):
            self.assertNotIn(secret, public)
            self.assertNotIn(secret.encode(), database)
        self.receive(first)
        wait_until(lambda: self.engine.jobs[first['record_id']].done())
        self.assertIsNone(self.engine.edge_token_scope(one['token']))
        self.assertEqual(self.engine.edge_token_scope(two['token']), second['instance_id'])

    def test_limit_queue_failure_isolation_and_stopped_instances_reject_late_data(self):
        keys = self.accounts(11)
        self.begin(keys, 10)
        jobs = self.engine.edge_jobs()
        failed = jobs[0]
        bound = self.bind(failed)
        self.engine.edge_step({**failed,'kind':'failure','code':'route400'}, scope=failed['instance_id'])
        wait_until(lambda: self.engine.jobs[failed['record_id']].done())
        wait_until(lambda: len(self.engine.edge_jobs()) == 5)
        self.assertEqual(self.store.get(failed['record_id'])['status'], 'error')
        self.assertIsNone(self.engine.edge_token_scope(bound['token']))
        self.assertIn(keys[5], [job['record_id'] for job in self.engine.edge_jobs()])
        stop = self.engine.edge_jobs()[0]
        self.engine.command('stop', [stop['record_id']])
        wait_until(lambda: self.engine.jobs[stop['record_id']].done())
        with self.assertRaises(ValueError):
            self.receive(stop)
        self.assertEqual(len(self.engine.edge_jobs()), 4)
        seen = {failed['request_id'], stop['request_id']}
        while len(self.engine.sessions) < 9:
            wait_until(lambda: any(job['request_id'] not in seen for job in self.engine.edge_jobs()))
            jobs = [job for job in self.engine.edge_jobs() if job['request_id'] not in seen]
            seen.update(job['request_id'] for job in jobs)
            for job in jobs:
                self.bind(job)
            for job in jobs:
                self.receive(job)
            wait_until(lambda: len(self.engine.sessions) >= min(9, len(seen) - 2))
        wait_until(lambda: all(self.engine.jobs[key].done() for key in keys))
        self.assertEqual(len(self.engine.sessions), 9)

    def test_crashed_login_page_times_out_releases_account_and_rejects_late_session(self):
        key = self.accounts(1)[0]
        self.begin([key], 1)
        job = self.engine.edge_jobs()[0]
        bound = self.bind(job)
        heartbeat = {**job, 'kind':'page_heartbeat', 'page_id':123}
        self.engine.edge_step(heartbeat, scope=job['instance_id'])
        with self.assertRaises(ValueError):
            self.engine.edge_step({**heartbeat, 'page_id':124}, scope=job['instance_id'])
        with self.assertRaises(ValueError):
            self.engine.inventory_command('delete', {'kind':'accounts', 'ids':[key]})

        request = self.engine.edge_requests[key]
        request['private']['page_seen'] = time.monotonic() - 61
        asyncio.run_coroutine_threadsafe(self.engine._expire_stale_edge_pages(), self.engine.loop).result(3)
        wait_until(lambda: self.engine.jobs[key].done())

        row = self.store.get(key)
        self.assertEqual(row['status'], 'error')
        self.assertIn('连续 60 秒无响应', row['message'])
        self.assertIn('释放账号', row['message'])
        self.assertTrue(self.handles[-1].closed)
        self.assertIsNone(self.engine.edge_token_scope(bound['token']))
        with self.assertRaises(ValueError):
            self.receive(job)

        self.engine.inventory_command('delete', {'kind':'accounts', 'ids':[key]})
        replacement = self.accounts(1)[0]
        self.assertNotEqual(replacement, key)
        self.engine.inventory_command('session', {'ids':[replacement]})
        wait_until(lambda: any(item['record_id'] == replacement for item in self.engine.edge_jobs()))
        self.engine.command('stop', [replacement])
        wait_until(lambda: self.engine.jobs[replacement].done())

    def test_page_watchdog_preserves_a_submitted_task(self):
        key = self.accounts(1)[0]
        self.begin([key], 1)
        job = self.engine.edge_jobs()[0]
        self.bind(job)
        request = self.engine.edge_requests[key]
        request['private']['page_seen'] = time.monotonic() - 61
        self.store.update(key, submitted=True)
        asyncio.run_coroutine_threadsafe(self.engine._expire_stale_edge_pages(), self.engine.loop).result(3)
        self.assertFalse(request['future'].done())
        self.assertFalse(self.engine.jobs[key].done())
        self.store.update(key, submitted=False)
        asyncio.run_coroutine_threadsafe(self.engine._expire_stale_edge_pages(), self.engine.loop).result(3)
        wait_until(lambda: self.engine.jobs[key].done())
        self.assertEqual(self.store.get(key)['status'], 'error')

    def test_cancel_during_initialization_does_not_open_a_late_login_window(self):
        started, returned = threading.Event(), threading.Event()
        def initialize(*args, **kwargs):
            started.set()
            self.assertTrue(kwargs['cancel'].wait(3))
            returned.set()
            raise RuntimeError('Fixture initialization cancelled')
        self.launch.side_effect = initialize
        key = self.accounts(1)[0]
        self.engine.inventory_command('session', {'ids':[key]})
        self.assertTrue(started.wait(2))
        self.engine.command('stop', [key])
        wait_until(lambda: self.engine.jobs[key].done())
        self.assertTrue(returned.is_set())
        self.assertFalse(self.handles)
        self.assertFalse(self.engine.edge_jobs())
        self.assertEqual(self.store.get(key)['status'], 'stopped')

    def test_one_initialization_failure_does_not_block_other_instances(self):
        keys=self.accounts(5)
        def initialize(*args,**kwargs):
            if kwargs['profile_key']==keys[0]:raise RuntimeError('Fixture profile preparation rejected')
            handle=Handle();self.handles.append(handle);return handle
        self.launch.side_effect=initialize
        self.engine.command('session-limit',[],5)
        self.engine.inventory_command('session',{'ids':keys})
        wait_until(lambda:self.engine.jobs[keys[0]].done() and len(self.engine.edge_jobs())==4)
        self.assertEqual(self.store.get(keys[0])['status'],'error')
        for job in self.engine.edge_jobs():self.receive(job)
        wait_until(lambda:all(self.engine.jobs[key].done() for key in keys))
        self.assertEqual(len(self.engine.sessions),4)
        self.assertTrue(all(handle.closed for handle in self.handles))

    def test_pause_and_lowered_limit_affect_admission_without_cancelling_running_jobs(self):
        keys = self.accounts(6)
        self.begin(keys, 5)
        self.engine.command('pause', [])
        self.engine.command('session-limit', [], 1)
        for job in self.engine.edge_jobs()[:4]:
            self.receive(job)
        wait_until(lambda: sum(not task.done() for task in self.engine.jobs.values()) == 2)
        self.assertEqual(len(self.engine.edge_jobs()), 1)
        self.receive(self.engine.edge_jobs()[0])
        wait_until(lambda: not self.engine.edge_jobs())
        self.assertEqual(self.store.get(keys[-1])['status'], 'queued')
        self.engine.command('resume', [])
        wait_until(lambda: len(self.engine.edge_jobs()) == 1)
        self.assertEqual(self.engine.edge_jobs()[0]['record_id'], keys[-1])
        self.engine.command('session-limit', [], 100)
        self.assertEqual(self.engine.session_limit, 5)

    def test_profile_paths_are_validated_before_launch_or_recursive_cleanup(self):
        root = Path(self.temp.name).resolve()/'profiles'
        root.mkdir()
        with self.assertRaises(ValueError):
            launch_isolated_edge(profile_root=root,profile_key='../outside',instance_id='a'*43,
                bootstrap_url='http://127.0.0.1:12345/edge-session-bridge#instance=test',cancel=threading.Event())
        with self.assertRaises(RuntimeError):
            _remove_profile(root.parent, root)
        self.assertTrue(root.is_dir())

    def test_ten_accounts_finish_mock_redemption_once_each_with_their_own_sessions(self):
        keys = self.accounts(10)
        self.store.inventory_import_cards('plus',[f'SYNTHETIC-BATCH-CARD-{index:02}' for index in range(10)])
        providers = {key:StockProvider(self.store.get(key)['email']) for key in keys}
        async def provider(key):return providers[key]
        self.engine.provider = provider
        self.engine.command('limit',[],10)
        quote = self.engine.inventory_command('quote',{'ids':keys,'sku':'plus'})
        self.engine.inventory_command('start',{'quote':quote['quote']})
        self.assertEqual(self.engine.session_limit,5)
        seen=set()
        while not all(self.engine.jobs[key].done() for key in keys):
            wait_until(lambda:any(job['request_id'] not in seen for job in self.engine.edge_jobs())
                       or all(self.engine.jobs[key].done() for key in keys))
            jobs=[job for job in self.engine.edge_jobs() if job['request_id'] not in seen]
            if not jobs:
                continue
            seen.update(job['request_id'] for job in jobs)
            for job in jobs:self.bind(job)
            for job in jobs:self.receive(job)
        wait_until(lambda:all(self.engine.jobs[key].done() for key in keys))
        self.assertTrue(all(task['status']=='completed' for task in self.store.charge_tasks.values()))
        self.assertTrue(all(card['status']=='used' for card in self.store.cards.values()))
        self.assertTrue(all(provider.redeems==1 for provider in providers.values()))
        self.assertTrue(all(row['has_recharged'] for row in self.engine.inventory_snapshot()['accounts']))

    def test_http_instance_token_cannot_read_other_credentials_or_submit_other_session(self):
        server = make_server(Path(self.temp.name)/'http-data')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        origin='http://'+server.address
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(path,body=None,token=None):
            headers={'Origin':'chrome-extension://'+'a'*32,'Content-Type':'application/json'}
            if token:headers['Authorization']='Bearer '+token
            req=urllib.request.Request(origin+path,headers=headers,data=json.dumps(body).encode() if body is not None else None)
            with opener.open(req,timeout=5) as response:return json.load(response)
        try:
            keys=server.store.inventory_import_accounts([{'email':'http-one@example.com','password':'ONLY-FIRST-PASSWORD'},
                {'email':'http-two@example.com','password':'ONLY-SECOND-PASSWORD'}])['ids']
            server.engine.command('limit',[],2)
            server.engine.inventory_command('session',{'ids':keys})
            wait_until(lambda:len(server.engine.edge_jobs())==2)
            jobs=server.engine.edge_jobs();tokens=[]
            for job in jobs:
                nonce=server.engine.edge_requests[job['record_id']]['private']['bootstrap_nonce']
                body={'instance_id':job['instance_id'],'bootstrap_nonce':nonce,'incognito':True}
                with self.assertRaises(urllib.error.HTTPError):request('/api/edge-session/bind',{**body,'incognito':False})
                tokens.append(request('/api/edge-session/bind',body)['token'])
                with self.assertRaises(urllib.error.HTTPError):request('/api/edge-session/bind',body)
            for index in range(2):
                own,other=jobs[index],jobs[1-index]
                self.assertEqual(request('/api/edge-session/jobs',token=tokens[index])['jobs'],[own])
                self.assertTrue(request('/api/edge-session/step',{**own,'kind':'password'},tokens[index])['available'])
                for path,body in [('/api/edge-session/step',{**other,'kind':'password'}),
                                  ('/api/edge-session/receive',{**other,'session':fixture(other['email'])})]:
                    with self.assertRaises(urllib.error.HTTPError) as result:request(path,body,tokens[index])
                    self.assertEqual(result.exception.code,400)
                with self.assertRaises(urllib.error.HTTPError):request('/api/inventory/state',token=tokens[index])
            self.assertEqual(request('/api/edge-session/jobs',token=server.bridge_token)['jobs'],[])
            self.assertTrue(request('/api/edge-session/receive',{**jobs[0],'session':fixture(jobs[0]['email'])},tokens[0])['ok'])
            wait_until(lambda:server.engine.jobs[keys[0]].done())
            with self.assertRaises(urllib.error.HTTPError):request('/api/edge-session/jobs',token=tokens[0])
        finally:
            server.shutdown();thread.join(3);server.engine.close();server.server_close();server.store.db.close();server.instance_lock.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
