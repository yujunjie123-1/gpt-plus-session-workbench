"""Disposable localhost fixture for Lighthouse; no real account database is opened."""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app import make_server


with TemporaryDirectory() as temporary:
    server=make_server(Path(temporary)/'data')
    server.store.inventory_import_accounts([{'email':f'audit{index+1:02}@example.com','eligibility_months':3 if index%2 else 1} for index in range(24)])
    for sku in ('plus','pro100','pro200','pro500'):
        server.store.inventory_import_cards(sku,[f'AUDIT-{sku}-CARD-{index+1:04}' for index in range(10)])
    print(json.dumps({'url':'http://'+server.address+'/#'+server.token}),flush=True)
    try:
        server.serve_forever()
    finally:
        server.engine.close()
        server.server_close()
        server.store.db.close()
        server.instance_lock.close()
