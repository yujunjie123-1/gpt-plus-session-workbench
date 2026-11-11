import asyncio
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from workbench.store import Store,crypt
from workbench.engine import Engine
from workbench.mobile_gateway import REMOTE_POSTS
from workbench.data_migration import merge_v2_database


class InventoryUsabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.path=Path(self.temp.name)/'records.sqlite3'
        self.store=Store(self.path)
        self.engine=Engine(self.store)

    def tearDown(self):
        self.engine.close()
        self.store.db.close()
        self.temp.cleanup()

    def account(self,email='test@example.com'):
        return self.store.inventory_import_accounts([dict(email=email,password='ORIGINAL')])['ids'][0]

    def card(self,code='SYNTHETIC-CARD'):
        self.store.inventory_import_cards('plus',[code])
        return next(card['id'] for card in self.store.cards.values() if crypt(card['secret'],True)==code)

    def test_direct_risk_import_moves_existing_without_overwriting_credentials(self):
        key=self.account()
        result=self.engine.inventory_command('risk-accounts',{'group':'3','items':[
            dict(email='test@example.com',password='REPLACEMENT'),dict(email='new@example.com'),dict(email='NEW@example.com')]})
        self.assertEqual((result['imported'],result['moved'],result['skipped']),(1,1,1))
        self.assertEqual(crypt(self.store.get(key)['password'],True),'ORIGINAL')
        state=self.engine.inventory_snapshot()
        self.assertTrue(all(row['risk']['active'] and not row['recharge_eligible'] for row in state['accounts']))
        self.assertEqual(self.store.get(result['ids'][0])['eligibility_months'],3)
        again=self.engine.inventory_command('risk-accounts',{'items':[dict(email='test@example.com')]})
        self.assertEqual((again['imported'],again['moved'],again['skipped']),(0,0,1))
        self.assertIn('/api/inventory/risk-accounts',REMOTE_POSTS)

    def test_invalid_or_busy_risk_batch_changes_nothing(self):
        key=self.account()
        for items in ([dict(email='new@example.com'),dict(email='bad')],):
            with self.assertRaises(ValueError):self.engine.inventory_command('risk-accounts',{'items':items})
        self.assertEqual(len(self.store.rows),1)
        async def block():
            self.engine.jobs[key]=asyncio.create_task(asyncio.sleep(600))
        self.engine._inventory_call(block())
        with self.assertRaises(ValueError):
            self.engine.inventory_command('risk-accounts',{'items':[dict(email='new@example.com'),dict(email='test@example.com')]})
        self.assertEqual(len(self.store.rows),1)
        self.assertFalse(self.store.get(key).get('risk',{}).get('active'))

    def test_used_delete_retains_history_and_dedupe_across_restart(self):
        key=self.account()
        card=self.card()
        task=self.store.inventory_reserve(self.store.inventory_quote([key],'plus'))[0]
        self.store.update(key,status='completed',submitted=True,message='Synthetic completion')
        self.assertEqual(self.store.charge_tasks[task]['status'],'completed')
        self.assertTrue(self.engine.inventory_snapshot()['cards'][0]['deletable'])
        self.engine.inventory_command('delete',{'kind':'cards','ids':[card]})
        self.assertEqual(self.store.inventory_public()['cards'],[])
        self.assertEqual(len(self.store.inventory_public()['tasks']),1)
        self.assertTrue(self.store.cards[card]['archived'])
        self.assertEqual(self.store.inventory_import_cards('plus',['SYNTHETIC-CARD'])['skipped'],1)
        with self.assertRaises(ValueError):self.engine.inventory_command('check-cards',{'ids':[card]})
        self.engine.close();self.store.db.close()
        self.store=Store(self.path);self.engine=Engine(self.store)
        self.assertEqual(self.store.inventory_public()['cards'],[])
        self.assertEqual(len(self.store.inventory_public()['tasks']),1)
        self.assertEqual(self.store.inventory_import_cards('plus',['SYNTHETIC-CARD'])['skipped'],1)

    def test_reserved_unknown_and_checking_blocks_atomic_deletion(self):
        key=self.account()
        first=self.card('FIRST-CARD');second=self.card('SECOND-CARD')
        task=self.store.inventory_reserve(self.store.inventory_quote([key],'plus'))[0]
        for status,submitted in [('queued',False),('error',True)]:
            self.store.charge_tasks[task].update(status=status,submitted=submitted,can_retry=False)
            # Even a stale card status must not bypass its unresolved transaction.
            self.store.cards[first]['status']='used'
            with self.assertRaises(ValueError):self.engine.inventory_command('delete',{'kind':'cards','ids':[second,first]})
            self.assertFalse(self.store.cards[first].get('archived'))
            self.assertIn(second,self.store.cards)
        async def checking():
            self.engine.inventory_checks={second:asyncio.create_task(asyncio.sleep(600))}
        self.engine._inventory_call(checking())
        with self.assertRaises(ValueError):self.engine.inventory_command('delete',{'kind':'cards','ids':[second]})
        visible=next(card for card in self.engine.inventory_snapshot()['cards'] if card['id']==second)
        self.assertFalse(visible['deletable'])
        async def cancel():
            job=self.engine.inventory_checks[second];job.cancel()
            await asyncio.gather(job,return_exceptions=True)
        self.engine._inventory_call(cancel())

    def test_unused_card_deletes_and_invalid_batch_is_atomic(self):
        key=self.card()
        with self.assertRaises(ValueError):self.engine.inventory_command('delete',{'kind':'cards','ids':[key,'missing']})
        self.assertIn(key,self.store.cards)
        self.engine.inventory_command('delete',{'kind':'cards','ids':[key]})
        self.assertEqual(self.store.cards,{})

    def test_merge_keeps_archived_used_marker_in_either_direction(self):
        key=self.card()
        self.store.cards[key]['status']='used'
        self.store.inventory_delete('cards',[key])
        source=Store(Path(self.temp.name)/'source.sqlite3')
        destination=Store(Path(self.temp.name)/'destination.sqlite3')
        try:
            source.inventory_import_cards('plus',['SYNTHETIC-CARD'])
            merge_v2_database(self.store,source.path)
            self.assertEqual(self.store.inventory_public()['cards'],[])
            destination.inventory_import_cards('plus',['SYNTHETIC-CARD'])
            merge_v2_database(destination,self.store.path)
            self.assertEqual(destination.inventory_public()['cards'],[])
            self.assertEqual(destination.inventory_import_cards('plus',['SYNTHETIC-CARD'])['skipped'],1)
        finally:
            source.db.close();destination.db.close()


if __name__=='__main__':unittest.main()
