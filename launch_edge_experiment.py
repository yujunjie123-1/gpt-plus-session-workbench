"""Launch the isolated experiment in normal Edge; keep this process running."""
import json
import argparse
import os
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from app import AlreadyRunningError, make_server
from workbench.private_browser import edge_executable
from workbench.store import crypt
from workbench.paths import storage_root
from workbench.browser_profiles import prepare_profile_template

ROOT = Path(__file__).resolve().parent
STORAGE_ROOT = storage_root()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def open_workbench(url, setup=False):
    args = [edge_executable()]
    if setup:
        args.append('--new-window')
    args.append(url)
    if setup:
        args.append('edge://extensions/')
    subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def save_runtime(server, root):
    root.mkdir(parents=True,exist_ok=True)
    origin = 'http://' + server.address
    # The launch credential is encrypted here; Session credentials are stored
    # separately with the current Windows user's DPAPI protection.
    metadata = {'url': origin, 'pid': os.getpid(), 'launch_blob': crypt(origin + '/#' + server.token),
                'source_root':str(ROOT),'database':str(server.store.path)}
    pending = root / 'runtime-info.pending.json'
    pending.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    pending.replace(root / 'runtime-info.json')


def reopen_existing(root):
    try:
        runtime = json.loads((root / 'runtime-info.json').read_text(encoding='utf-8'))
        launch_url = crypt(runtime['launch_blob'], decrypt=True)
        parsed = urlsplit(launch_url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                or parsed.username or parsed.password or parsed.path != '/' or parsed.query
                or not parsed.fragment or runtime['url'] != f'http://127.0.0.1:{parsed.port}'):
            return False
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(runtime['url'] + '/api/ping', headers={'Authorization': 'Bearer ' + parsed.fragment})
        with opener.open(request, timeout=2) as response:
            if response.status != 200 or json.loads(response.read(128)).get('ok') is not True:
                return False
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        return False
    open_workbench(launch_url)
    return True


def read_upgrade(path):
    if path.resolve() != (STORAGE_ROOT / 'runtime-upgrade.json').resolve():
        raise ValueError('Upgrade file must belong to this workbench directory')
    data = json.loads(crypt(path.read_text(encoding='utf-8'), decrypt=True))
    if (not isinstance(data.get('port'), int) or not 1 <= data['port'] <= 65535
            or any(not isinstance(data.get(name), str) or not 32 <= len(data[name]) <= 128 for name in ['token', 'bridge_token'])
            or not isinstance(data.get('recover_ids'), list)
            or any(not isinstance(key, str) or len(key) != 32 for key in data['recover_ids'])):
        raise ValueError('Upgrade metadata is invalid')
    return data


def main(upgrade=None):
    if upgrade is None and reopen_existing(STORAGE_ROOT):
        print('Workbench is already running. Opened the existing instance.', flush=True)
        return 0
    try:
        server = make_server(data_dir=STORAGE_ROOT / 'data', port=upgrade['port'] if upgrade else 0,
                             legacy_sources=[ROOT/'data'/'records.sqlite3'])
    except AlreadyRunningError:
        # A second click can arrive before the first process writes its metadata.
        for _ in range(10):
            if reopen_existing(STORAGE_ROOT):
                print('Workbench is already running. Opened the existing instance.', flush=True)
                return 0
            time.sleep(0.2)
        print('Workbench is already running. Please return to its Edge tab; do not remove the data lock.', flush=True)
        return 0
    origin = 'http://' + server.address
    warm_cancel=threading.Event()
    def warm_profiles():
        try:prepare_profile_template(server.store.path.parent/'browser-profiles',warm_cancel)
        except Exception:pass  # An actual login retries and reports a local preparation error.
    warmer=threading.Thread(target=warm_profiles,daemon=True,name='workbench-empty-profile')
    try:
        if upgrade:
            server.token = upgrade['token']
            server.bridge_token = upgrade['bridge_token']
        save_runtime(server, STORAGE_ROOT)
        warmer.start()
        if upgrade:
            (STORAGE_ROOT / 'runtime-upgrade.json').unlink(missing_ok=True)
            if upgrade['recover_ids']:
                server.engine.command('edge_recover', upgrade['recover_ids'])
        open_workbench(origin + '/#' + server.token)
        print('Edge Session experiment running at ' + origin, flush=True)
        print('Keep this process running. Each login prepares its own isolated InPrivate helper automatically.', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        warm_cancel.set()
        if warmer.is_alive():warmer.join(timeout=20)
        server.engine.close()
        server.server_close()
        server.store.db.close()
        (STORAGE_ROOT / 'runtime-info.json').unlink(missing_ok=True)
        server.instance_lock.close()
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--upgrade-file', type=Path)
    args = parser.parse_args()
    sys.exit(main(read_upgrade(args.upgrade_file) if args.upgrade_file else None))
