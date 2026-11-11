import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import make_server
import launch_edge_experiment as launcher
from workbench.store import crypt


class EdgeLauncherTest(unittest.TestCase):
    def test_duplicate_launch_reuses_authenticated_instance_and_preserves_memory(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            server = make_server(data_dir=root / 'data')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                launcher.save_runtime(server, root)
                runtime = (root / 'runtime-info.json').read_text(encoding='utf-8')
                self.assertNotIn(server.token, runtime)
                server.engine.sessions['test-preserved-session'] = {'sentinel': True}
                with patch.object(launcher, 'STORAGE_ROOT', root), patch.object(launcher, 'open_workbench') as opened, patch.object(launcher, 'make_server') as new_server:
                    self.assertEqual(launcher.main(), 0)
                new_server.assert_not_called()
                opened.assert_called_once_with('http://' + server.address + '/#' + server.token)
                self.assertEqual(server.engine.sessions['test-preserved-session'], {'sentinel': True})
                self.assertTrue(thread.is_alive())
            finally:
                server.shutdown()
                thread.join(3)
                server.engine.close()
                server.server_close()
                server.store.db.close()
                server.instance_lock.close()

    def test_missing_stale_and_nonlocal_metadata_never_open_a_browser(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(launcher, 'open_workbench') as opened:
                self.assertFalse(launcher.reopen_existing(root))
                for url in ('https://evil.test/#private', 'http://user:pass@127.0.0.1:9/#private', 'http://127.0.0.1:9/#private'):
                    (root / 'runtime-info.json').write_text(json.dumps({'url': 'http://127.0.0.1:9', 'launch_blob': crypt(url)}), encoding='utf-8')
                    self.assertFalse(launcher.reopen_existing(root))
                opened.assert_not_called()

    def test_upgrade_metadata_is_encrypted_and_constrained_to_this_directory(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / 'runtime-upgrade.json'
            metadata = {'port': 12345, 'token': 'a' * 43, 'bridge_token': 'b' * 43, 'recover_ids': ['c' * 32]}
            path.write_text(crypt(json.dumps(metadata)), encoding='utf-8')
            with patch.object(launcher, 'STORAGE_ROOT', root):
                self.assertEqual(launcher.read_upgrade(path), metadata)
                with self.assertRaises(ValueError):
                    launcher.read_upgrade(root / 'other.json')
            self.assertNotIn(metadata['token'], path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
