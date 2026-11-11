import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.browser_profiles import _profile_fingerprint


class BrowserProfileFingerprintTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.edge = root / 'msedge.exe'
        self.edge.write_bytes(b'synthetic edge binary')
        self.helper = root / 'helper'
        self.helper.mkdir()
        self.manifest = {
            'key': 'synthetic-extension-key',
            'manifest_version': 3,
            'permissions': ['alarms', 'cookies', 'scripting', 'storage', 'tabs'],
            'optional_permissions': [],
            'host_permissions': ['http://127.0.0.1/*', 'https://auth.openai.com/*', 'https://chatgpt.com/*'],
            'optional_host_permissions': [],
            'incognito': 'spanning',
            'version': '1.0.0',
            'background': {'service_worker': 'background.js'},
        }
        (self.helper / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')
        (self.helper / 'background.js').write_text('const version = 1;', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def fingerprint(self):
        return _profile_fingerprint(self.edge, self.helper)

    def test_login_script_update_does_not_invalidate_profile_permissions(self):
        before = self.fingerprint()
        (self.helper / 'background.js').write_text('const version = 2;', encoding='utf-8')
        self.manifest['version'] = '1.0.1'
        (self.helper / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')
        self.assertEqual(self.fingerprint(), before)

    def test_extension_permission_or_browser_update_invalidates_template(self):
        before = self.fingerprint()
        self.manifest['permissions'].append('webNavigation')
        (self.helper / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')
        self.assertNotEqual(self.fingerprint(), before)

        permissions_changed = self.fingerprint()
        with self.edge.open('ab') as edge_file:
            edge_file.write(b'updated')
        self.assertNotEqual(self.fingerprint(), permissions_changed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
