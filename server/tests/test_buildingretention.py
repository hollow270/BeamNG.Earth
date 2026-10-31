"""Proof-based local cleanup; all simulated sessions and work stay in fixture root."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import test_buildingactivation as afx
import test_buildingmanual as manual
import test_buildingrender as fx
import test_buildingrefresh as regen
import buildingretention as retention
import earthd
import buildingrefresh
from buildingoverlaystore import ResourceLimit


class RetentionTest(unittest.TestCase):
    setUp=fx.RenderTest.setUp
    md=fx.RenderTest.md
    close=regen.RefreshTest.close
    setup=manual.ManualTest.setup
    prepared=afx.ActivationTest.prepared
    preview=manual.ManualTest.preview
    accept=manual.ManualTest.accept
    activate=afx.ActivationTest.activate

    def start(self):
        self.prepared(); self.activate()
        self.manager=self.server._building_activation
        self.manager.client_id='c'*64; self.manager._save()
        self.server.root=str(self.root/'sessions'); Path(self.server.root).mkdir()
        self.server.orphans={}; self.server.claimed_sessions=lambda:set()
        self.io=self.manager.c.io

    def proof(self,**values):
        return dict(complete=True,sessionId=self.server.sid,client='c'*64,sequence=self.manager.sequence,
            sessions=[self.server.sid],slots=[[self.server.sid,n] for n in self.manager.holds],
            pending=list(self.manager.transactions),**values)

    def old(self,client='c'*64):
        path=self.io.root/('activation-'+'a'*16+'.json')
        value=self.io._read(self.manager.path); value['session']='older-session'; value['client']=client
        self.io._atomic_write(path,self.io._document(value))
        folder=Path(self.server.root)/'older-session'; folder.mkdir()
        (folder/earthd.BUILDING_SESSION_HOLD).write_text('held')
        (folder/earthd.SESSION_MARKER).write_text('old')
        (folder/'slot_00000.cdae').write_bytes(b'held original')
        self.server.orphans['older-session']=0
        return path,folder

    def test_published_held_and_uncertain_generations_are_retained(self):
        self.start(); gen=next(iter(self.manager.c.outputs()['entries'].values()))['generation']
        report=retention.collect(self.server,self.manager)
        self.assertTrue((self.io.objects/gen).is_dir())
        self.assertFalse(report['removed_generations']); self.assertGreater(report['held'],0)
        self.assertTrue((Path(self.server.out_dir)/earthd.BUILDING_SESSION_HOLD).is_file())

    def test_unreferenced_generation_and_completed_private_lease_can_be_reclaimed(self):
        self.start()
        unused=self.io.objects/('d'*32); unused.mkdir(); (unused/'empty.json').write_text('{}')
        private=self.io.root/'work'/('e'*32); private.mkdir(); (private/'partial.bin').write_bytes(b'partial')
        lease=retention.WorkLease(self.io.root,private.name,create=True).acquire(create=True)
        first=retention.collect(self.server,self.manager)
        self.assertTrue(private.exists()); self.assertIn(unused.name,first['removed_generations'])
        lease.close()
        second=retention.collect(self.server,self.manager)
        self.assertIn(private.name,second['removed_work']); self.assertFalse(private.exists())

    def test_corrupt_journal_blocks_generation_cleanup_and_legacy_work_is_uncertain(self):
        self.start()
        bad=self.io.root/('activation-'+'b'*16+'.json'); bad.write_text('{interrupted')
        unused=self.io.objects/('d'*32); unused.mkdir()
        legacy=self.io.root/'work'/('f'*32); legacy.mkdir()
        report=retention.collect(self.server,self.manager)
        self.assertTrue(unused.exists()); self.assertTrue(legacy.exists())
        self.assertTrue(any('Corrupt' in row for row in report['uncertain']))
        self.assertTrue(any('Private work retained' in row for row in report['uncertain']))

    def test_old_session_requires_matching_complete_owner_proof(self):
        self.start(); path,folder=self.old()
        held=self.proof(); held['sessions'].append('older-session')
        retention.collect(self.server,self.manager,proof=held)
        self.assertTrue(path.exists()); self.assertTrue((folder/earthd.BUILDING_SESSION_HOLD).exists())
        report=retention.collect(self.server,self.manager,proof=self.proof())
        self.assertIn('older-session',report['released_sessions'])
        self.assertFalse(path.exists()); self.assertFalse((folder/earthd.BUILDING_SESSION_HOLD).exists())
        self.assertTrue((folder/'slot_00000.cdae').exists())  # ordinary event-loop delete queue owns folder retirement

    def test_legacy_ownerless_journal_is_not_deleted_even_with_empty_scene(self):
        self.start(); path,folder=self.old(client=None)
        report=retention.collect(self.server,self.manager,proof=self.proof())
        self.assertTrue(path.exists()); self.assertTrue((folder/earthd.BUILDING_SESSION_HOLD).exists())
        self.assertTrue(report['uncertain'])

    def test_unused_fenced_slot_proof_retires_transaction_but_incomplete_proof_does_not(self):
        self.start(); rows=dict(self.manager.holds)
        incomplete=self.proof(); incomplete.update(complete=False,slots=[],pending=[])
        retention.collect(self.server,self.manager,proof=incomplete)
        self.assertEqual(self.manager.holds,rows)
        proof=self.proof(); proof.update(slots=[],pending=[])
        report=retention.collect(self.server,self.manager,proof=proof)
        self.assertFalse(self.manager.holds); self.assertFalse(self.manager.transactions)
        self.assertGreater(report['history_compacted'],0)
        self.assertTrue(report['reclaimed_slots'])

    def test_completed_worker_remains_leased_until_catalog_publication(self):
        self.start(); self.store.disable()
        scan=buildingrefresh.scan; observed=[]
        def inspect(directory,*args,**kwargs):
            directory=Path(directory)
            if directory.parent==self.io.root/'work':
                report=retention.collect(self.server,self.manager)
                observed.append(directory.exists())
                self.assertNotIn(directory.name,report['removed_work'])
            return scan(directory,*args,**kwargs)
        with mock.patch.object(buildingrefresh,'scan',side_effect=inspect):
            result=self.server.refresh_building_overlays()
        self.assertTrue(observed); self.assertTrue(all(observed)); self.assertTrue(result['publication'])

    def test_full_journal_budget_can_be_cleaned_only_with_matching_owner_proof(self):
        self.start(); body=self.io._read(self.manager.path)
        for i in range(15):
            old=deepcopy(body); old['session']='older-'+str(i)
            path=self.io.root/('activation-'+format(i,'016x')+'.json')
            self.io._atomic_write(path,self.io._document(old))
        original=self.server.sid; self.server.sid='next-server'
        try:
            with self.assertRaises(ResourceLimit):
                import buildingactivation
                buildingactivation.Manager(self.server,self.manager.c)
        finally: self.server.sid=original
        report=retention.collect(self.server,self.manager,proof=self.proof())
        self.assertEqual(len(report['removed_journals']),15)
