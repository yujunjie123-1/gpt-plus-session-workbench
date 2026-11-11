import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import make_server
from workbench.engine import Engine
from workbench.store import Store
from workbench.edge_session import validate_handoff


def fixture(email='owner@example.com'):
    return {'user': {'email': email}, 'expires': '2099-01-01T00:00:00Z',
            'accessToken': 'TEST-ACCESS-SECRET', 'sessionToken': 'TEST-COOKIE-SECRET'}


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('Local asynchronous state did not settle')


class EdgeEngineTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'records.sqlite3')
        self.key = self.store.add_many([{'email': 'owner@example.com', 'code': 'TEST-ONLY-CARD'}])[0]
        self.engine = Engine(self.store)
        self.launcher = patch('workbench.edge_session.open_ordinary_edge')
        self.launch_mock = self.launcher.start()

    def tearDown(self):
        self.engine.close()
        self.store.db.close()
        self.launcher.stop()
        self.temp.cleanup()

    def start(self):
        self.engine.command('edge_login', [self.key])
        wait_until(lambda: self.store.get(self.key)['message'] == '独立 InPrivate 窗口已启动，正在连接登录助手')
        job = self.engine.edge_jobs()[0]
        pending = self.engine.edge_requests[self.key]
        self.engine.bind_edge_instance({'instance_id': pending['instance_id'],
                                        'bootstrap_nonce': pending['private']['bootstrap_nonce'],
                                        'incognito': True})
        return job

    def test_handoff_is_single_use_in_memory_and_never_redeems(self):
        job = self.start()
        self.assertTrue(job['incognito'])
        self.assertNotIn('future', job)
        self.assertNotIn('ready', job)
        payload = dict(record_id=self.key, request_id=job['request_id'], session=fixture())
        self.assertTrue(self.engine.receive_edge_session(payload)['ok'])
        wait_until(lambda: self.store.get(self.key)['status'] == 'ready')
        self.assertEqual(self.engine.sessions[self.key]['sessionToken'], 'TEST-COOKIE-SECRET')
        self.assertFalse(self.store.get(self.key)['submitted'])
        self.assertFalse(self.engine.providers)
        public = json.dumps(self.engine.snapshot())
        database = self.store.path.read_bytes()
        for secret in ('TEST-ACCESS-SECRET', 'TEST-COOKIE-SECRET', job['request_id']):
            self.assertNotIn(secret, public)
            self.assertNotIn(secret.encode(), database)
        with self.assertRaises(ValueError):
            self.engine.receive_edge_session(payload)
        self.launch_mock.assert_called_once()

    def test_wrong_account_and_invalid_session_leave_request_available(self):
        job = self.start()
        cases = [fixture('wrong@example.com'), {**fixture(), 'sessionToken': ''},
                 {**fixture(), 'expires': '2020-01-01T00:00:00Z'},
                 {**fixture(), 'expires': '2099-01-01T00:00:00'},
                 {**fixture(), 'accessToken': 42}]
        for session in cases:
            with self.assertRaises(ValueError):
                self.engine.receive_edge_session(dict(record_id=self.key, request_id=job['request_id'], session=session))
            self.assertNotIn(self.key, self.engine.sessions)
            self.assertTrue(self.engine.edge_jobs())

    def test_unbound_window_fails_readily_and_cleans_up_its_handle(self):
        handle = self.launch_mock.return_value
        with patch('workbench.edge_session.EDGE_LAUNCH_TIMEOUT', 0.05), \
             patch('workbench.edge_session.PAGE_HEARTBEAT_TIMEOUT', 0.05):
            self.engine.command('edge_login', [self.key])
            wait_until(lambda: self.engine.jobs[self.key].done())
        row = self.store.get(self.key)
        self.assertEqual(row['status'], 'error')
        self.assertIn('未能连接登录助手', row['message'])
        self.assertEqual(self.engine.edge_jobs(), [])
        handle.close.assert_called_once()
        self.assertNotIn(self.key, self.engine.sessions)

    def test_window_preparation_timeout_cancels_launcher_and_cleans_handle(self):
        started = threading.Event()
        cancel_events = []
        handle = self.launch_mock.return_value

        def cancellable_launch(*args, cancel, **kwargs):
            cancel_events.append(cancel)
            started.set()
            cancel.wait(2)
            return handle

        self.launch_mock.side_effect = cancellable_launch
        with patch('workbench.edge_session.EDGE_LAUNCH_TIMEOUT', 0.05):
            self.engine.command('edge_login', [self.key])
            wait_until(started.is_set)
            wait_until(lambda: self.engine.jobs[self.key].done())
        row = self.store.get(self.key)
        self.assertEqual(row['status'], 'error')
        self.assertIn('准备独立 InPrivate 窗口超过一分钟', row['message'])
        self.assertTrue(cancel_events[0].is_set())
        handle.close.assert_called_once()
        self.assertEqual(self.engine.edge_jobs(), [])

    def test_bound_helper_reports_ready_stage_before_session_handoff(self):
        ready = threading.Event()
        continue_launch = threading.Event()
        handle = self.launch_mock.return_value

        def delayed_launch(*args, **kwargs):
            ready.set()
            continue_launch.wait(2)
            return handle

        self.launch_mock.side_effect = delayed_launch
        self.engine.command('edge_login', [self.key])
        wait_until(ready.is_set)
        self.assertIn('正在准备独立 InPrivate 窗口', self.store.get(self.key)['message'])
        continue_launch.set()
        wait_until(lambda: self.store.get(self.key)['message'] == '独立 InPrivate 窗口已启动，正在连接登录助手')
        job = self.engine.edge_jobs()[0]
        pending = self.engine.edge_requests[self.key]
        self.engine.bind_edge_instance({'instance_id': pending['instance_id'],
                                        'bootstrap_nonce': pending['private']['bootstrap_nonce'],
                                        'incognito': True})
        self.assertEqual(self.store.get(self.key)['message'], '登录助手已连接，正在等待登录页面')
        self.assertTrue(self.engine.receive_edge_session({**job, 'session': fixture()})['ok'])
        wait_until(lambda: self.store.get(self.key)['status'] == 'ready')

    def test_stop_rejects_late_handoff_and_reissue_rejects_old_nonce(self):
        old = self.start()
        self.engine.command('stop', [self.key])
        wait_until(lambda: self.engine.jobs[self.key].done())
        with self.assertRaises(ValueError):
            self.engine.receive_edge_session(dict(record_id=self.key, request_id=old['request_id'], session=fixture()))
        new = self.start()
        self.assertNotEqual(old['request_id'], new['request_id'])
        with self.assertRaises(ValueError):
            self.engine.receive_edge_session(dict(record_id=self.key, request_id=old['request_id'], session=fixture()))
        self.assertNotIn(self.key, self.engine.sessions)

    def test_same_account_is_guarded_while_independent_accounts_can_run_together(self):
        self.start()
        key2 = self.store.add_many([{'email': 'second@example.com', 'code': 'SECOND-TEST-CARD'}])[0]
        with self.assertRaises(ValueError):
            self.engine.command('edge_login', [self.key])
        self.engine.command('edge_login', [key2])
        self.assertEqual(self.store.get(key2)['status'], 'queued')
        self.engine.command('limit', [], 2)
        wait_until(lambda: len(self.engine.edge_jobs()) == 2)
        self.assertEqual(len({job['instance_id'] for job in self.engine.edge_jobs()}), 2)
        with self.assertRaises(ValueError):
            self.engine.command('edge_login', [self.key, key2])


class EdgeHTTPTest(unittest.TestCase):
    def test_pairing_scope_origin_handoff_and_probe_card_guard(self):
        with TemporaryDirectory() as temp, patch('workbench.edge_session.open_ordinary_edge'):
            server = make_server(temp)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            origin = 'http://' + server.address
            extension_origin = 'chrome-extension://' + 'a' * 32

            def request(path, body=None, token=None, request_origin=None):
                headers = {}
                if token:
                    headers['Authorization'] = 'Bearer ' + token
                if request_origin:
                    headers['Origin'] = request_origin
                if body is not None:
                    headers['Content-Type'] = 'application/json'
                req = urllib.request.Request(origin + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
                with urllib.request.urlopen(req, timeout=5) as response:
                    return json.load(response)

            try:
                with self.assertRaises(urllib.error.HTTPError):
                    request('/api/edge-session/pair', {}, server.token, 'https://evil.test')
                pair = request('/api/edge-session/pair', {}, server.token, origin)
                self.assertEqual(pair['bridge_token'], server.bridge_token)
                with self.assertRaises(urllib.error.HTTPError):
                    request('/api/edge-session/jobs', token=server.token)
                with self.assertRaises(urllib.error.HTTPError):
                    request('/api/state', token=server.bridge_token)
                with self.assertRaises(urllib.error.HTTPError):
                    request('/api/edge-session/jobs', token=server.bridge_token, request_origin='https://evil.test')
                probe = request('/api/edge-session/probe', {'email': 'owner@example.com'}, server.token, origin)
                wait_until(lambda: bool(server.engine.edge_jobs()))
                self.assertEqual(request('/api/edge-session/jobs', token=server.bridge_token, request_origin=extension_origin)['jobs'], [])
                pending = server.engine.edge_requests[probe['record_id']]
                bound = request('/api/edge-session/bind', {'instance_id': pending['instance_id'],
                    'bootstrap_nonce': pending['private']['bootstrap_nonce'], 'incognito':True}, request_origin=extension_origin)
                instance_token = bound['token']
                job = request('/api/edge-session/jobs', token=instance_token, request_origin=extension_origin)['jobs'][0]
                payload = dict(record_id=probe['record_id'], request_id=job['request_id'], session=fixture())
                with self.assertRaises(urllib.error.HTTPError):
                    request('/api/edge-session/receive', payload, server.bridge_token, 'https://evil.test')
                with self.assertRaises(urllib.error.HTTPError):
                    request('/api/edge-session/receive', payload, server.bridge_token, extension_origin)
                self.assertTrue(request('/api/edge-session/receive', payload, instance_token, extension_origin)['ok'])
                wait_until(lambda: not server.engine.edge_jobs())
                self.assertEqual(server.store.get(probe['record_id'])['status'], 'ready')
                self.assertEqual(request('/api/session', {'id': probe['record_id']}, server.token, origin)['session']['sessionToken'], 'TEST-COOKIE-SECRET')
                with self.assertRaises(urllib.error.HTTPError):
                    request('/api/session', {'id': probe['record_id']}, server.bridge_token, extension_origin)
                self.assertTrue(server.store.get(probe['record_id'])['probe_only'])
                for action in ('preflight', 'confirm', 'batch', 'refresh'):
                    with self.assertRaises(ValueError):
                        server.engine.command(action, [probe['record_id']])
                with self.assertRaises(urllib.error.HTTPError):
                    request('/api/edge-session/receive', payload, server.bridge_token, extension_origin)
            finally:
                server.shutdown()
                thread.join(3)
                server.engine.close()
                server.server_close()
                server.store.db.close()
                server.instance_lock.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
