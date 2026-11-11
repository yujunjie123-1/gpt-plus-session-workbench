"""Start or reopen the separate private mobile gateway; desktop remains running."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit
from workbench.paths import storage_root
from workbench.store import crypt
from workbench.mobile_gateway import make_mobile_server
from workbench.private_browser import edge_executable

ROOT=Path(__file__).resolve().parent


def open_settings(url):
    subprocess.Popen([edge_executable(),url],cwd=str(ROOT),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def main(origin=None,port=51931,no_open=False):
    storage=storage_root()
    metadata_path=storage/'mobile-runtime.json'
    if metadata_path.is_file():
        try:
            metadata=json.loads(metadata_path.read_text(encoding='utf-8'))
            setup=urlsplit(crypt(metadata['setup_blob'],True))
            if setup.scheme!='http' or setup.hostname!='127.0.0.1' or setup.port!=port or setup.path!='/mobile/setup':raise ValueError()
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req=urllib.request.Request(f'http://127.0.0.1:{port}/mobile/admin-state',headers={'Authorization':'Bearer '+setup.fragment})
            with opener.open(req,timeout=2) as response:current=json.load(response)
            if origin and current['origin']!=origin:raise ValueError('The mobile hostname differs from the running gateway')
            if not no_open:open_settings(setup.geturl())
            print('Mobile gateway is already running.',flush=True)
            return 0
        except (OSError,ValueError,KeyError):pass
    if origin is None:
        tailscale=Path(r'C:\Program Files\Tailscale\tailscale.exe')
        state=json.loads(subprocess.check_output([str(tailscale),'status','--json'],cwd=str(ROOT),text=True,
                                                 creationflags=subprocess.CREATE_NO_WINDOW,timeout=8))
        if state.get('BackendState')!='Running':raise RuntimeError('Connect Tailscale before starting mobile access')
        origin='https://'+state['Self']['DNSName'].rstrip('.')
    server=make_mobile_server(storage,origin,port)
    setup=f'http://{server.local_address}/mobile/setup#{server.state.admin_token}'
    metadata_path.write_text(json.dumps({'origin':origin,'port':port,'setup_blob':crypt(setup)},indent=2),encoding='utf-8')
    if not no_open:open_settings(setup)
    print('Private mobile gateway running on loopback port '+str(port),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        server.server_close()
        metadata_path.unlink(missing_ok=True)
    return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--origin')
    parser.add_argument('--port',type=int,default=51931)
    parser.add_argument('--no-open',action='store_true')
    options=parser.parse_args()
    raise SystemExit(main(options.origin,options.port,options.no_open))
