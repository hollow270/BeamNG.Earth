"""Generation admission/collection with real catalogs, leases and synthetic local receipts."""
from concurrent.futures import Future,ThreadPoolExecutor,CancelledError
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import unittest
from unittest import mock

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import test_buildingrender as fx
import test_buildingrefresh as refresh
import test_buildingretention as proofs
import test_buildinglive as live
import buildingrefresh as br
import buildingretention as retention
from buildingoverlaystore import ResourceLimit,CorruptStorage


class CapacityTest(unittest.TestCase):
    setUp=fx.RenderTest.setUp
    md=fx.RenderTest.md
    close=refresh.RefreshTest.close

    def setup(self,*,artifacts=512,submit=None):
        self.base=self.md(); self.counts=[]; self.submissions=0; self.reports=[]
        def immediate(*args):
            self.submissions+=1
            count=len(list(self.coordinator.io.objects.iterdir()))+len(list((self.coordinator.io.root/'work').iterdir()))
            self.counts.append(count); self.assertLessEqual(count,artifacts)
            future=Future(); future.set_result(refresh.fake_receipt(*args)); return future
        self.coordinator=br.Coordinator(self.root/'catalog',self.base,self.store,submit or immediate,
                                       limits=replace(br.Limits(),artifacts=artifacts))
        self.server=SimpleNamespace(_building_refresh=self.coordinator,sid='new-session',root=str(self.root/'sessions'),
                                    orphans={},claimed_sessions=lambda:set())

    def requests(self,aid,count=80):
        return [br.TileRequest('near',i%10,i//10,aid=aid,lat=fx.LAT,lon=fx.LON) for i in range(count)]

    def reclaim(self,required):
        report={}; self.reports.append(report)
        return retention.make_room(self.server,required=required,report=report)

    def batch(self,aid,count=80):
        return self.coordinator.refresh(self.requests(aid,count),replace_requests=True,reclaim=self.reclaim)

    def seed(self,count):
        result=[]
        for i in range(count):
            name=format(i,'032x'); folder=self.coordinator.io.objects/name
            folder.mkdir(); (folder/'local.json').write_text('{}'); result.append(name)
        return result

    def journal(self,generations,state='orphaned',holds=None):
        transactions=[dict(transaction=format(i+1,'032x'),generations=generations[i*108:(i+1)*108],state=state)
                      for i in range((len(generations)+107)//108)]
        path=self.coordinator.io.root/('activation-'+'e'*16+'.json')
        self.coordinator.io._atomic_write(path,self.coordinator.io._document(dict(session='older',client='c'*64,
            holds=holds or {},transactions=transactions)))
        return path

    def test_eight_item_policy_exhausts_a_repeated_eighty_target_lifecycle(self):
        self.setup(); failed=False
        for aid in range(1,10):
            retention.collect(self.server,limit=8)
            previous=self.coordinator.outputs()['publication']; submitted=self.submissions
            try: self.coordinator.refresh(self.requests(aid),replace_requests=True)
            except ResourceLimit:
                failed=True
                self.assertEqual(self.submissions,submitted,'admission must now fail before creating an oversized work batch')
                self.assertEqual(self.coordinator.outputs()['publication'],previous)
                break
        self.assertTrue(failed); self.assertLessEqual(max(self.counts),512)

    def test_twelve_eighty_target_transitions_reclaim_on_demand_and_stay_bounded(self):
        self.setup()
        for aid in range(1,13):
            self.batch(aid)
            rows=self.coordinator.outputs()['entries']
            self.assertEqual(len(rows),80)
            self.assertTrue(all(self.coordinator._valid(row) for row in rows.values()))
            self.assertFalse(list((self.coordinator.io.root/'work').iterdir()))
        self.assertEqual(max(self.counts),512)
        self.assertGreater(sum(len(r['removed_generations']) for r in self.reports),400)
        self.assertTrue(all(r['capacity']['passes']<=2 for r in self.reports))

    def test_retained_510_case_frees_78_in_two_passes_before_allocating_80(self):
        self.setup(); self.batch(1); old=self.coordinator.outputs()['entries']
        self.seed(430); self.batch(2)
        report=self.reports[-1]
        self.assertEqual(report['capacity'],dict(required=80,passes=2,before=510,after=432,free=80))
        self.assertEqual(len(report['removed_generations']),78)
        self.assertTrue(all((self.coordinator.io.objects/r['generation']).is_dir() for r in old.values()))
        self.assertEqual(self.coordinator._usage()[0],512)

    def test_reuse_and_exact_boundary_use_only_actual_new_generation_capacity(self):
        self.setup(artifacts=4); self.batch(1,2); self.seed(1)
        again=self.batch(1,2)
        self.assertFalse(again['rebuilt']); self.assertEqual(self.reports[-1]['capacity']['required'],0)
        self.batch(2,2)
        self.assertEqual(len(self.reports[-1]['removed_generations']),1)
        self.assertEqual(self.coordinator._usage()[0],4)

    def test_incomplete_proof_and_held_retired_transactions_survive_true_exhaustion(self):
        self.setup(); self.batch(1); pinned=self.seed(432); path=self.journal(pinned)
        proof=dict(complete=False,sessionId='new-session',client='c'*64,sessions=[],slots=[],pending=[],sequence=99)
        self.assertFalse(retention.collect(self.server,proof=proof,limit=64)['removed_generations'])
        before=self.coordinator.outputs(); count=self.submissions
        with self.assertRaises(ResourceLimit): self.batch(2)
        self.assertEqual(self.submissions,count); self.assertEqual(self.coordinator.outputs()['entries'],before['entries'])
        self.assertEqual(self.reports[-1]['capacity']['passes'],1)
        self.assertTrue(all((self.coordinator.io.objects/name).exists() for name in pinned))
        # Even terminal history stays rooted while an exact slot hold references it.
        self.journal(pinned,state='retired',holds={'1':dict(serial=10,transaction=format(1,'032x'))})
        self.batch(2)
        self.assertTrue(all((self.coordinator.io.objects/name).exists() for name in pinned[:108]))
        self.assertTrue(path.exists())

    def test_corrupt_journal_blocks_collection_without_any_worker_or_catalog_loss(self):
        self.setup(); self.batch(1); self.seed(430)
        (self.coordinator.io.root/('activation-'+'f'*16+'.json')).write_text('{interrupted')
        before=self.coordinator.outputs(); submitted=self.submissions
        with self.assertRaises(ResourceLimit): self.batch(2)
        self.assertEqual(self.submissions,submitted)
        self.assertEqual(self.coordinator.outputs()['entries'],before['entries'])
        self.assertEqual(self.coordinator._usage()[0],510)
        self.assertTrue(any('Corrupt' in r for r in self.reports[-1]['uncertain']))

    def test_restart_keeps_catalog_and_busy_or_legacy_work_then_collects_proven_crashed_work(self):
        self.setup(); self.batch(1); self.seed(430); root=self.coordinator.io.root
        busy=root/'work'/('a'*32); busy.mkdir()
        lease=retention.WorkLease(root,busy.name,create=True).acquire(create=True)
        legacy=root/'work'/('b'*32); legacy.mkdir()
        try:
            old=self.coordinator
            self.coordinator=br.Coordinator(root,self.base,self.store,old.submit)
            self.server._building_refresh=self.coordinator
            self.batch(2)
            self.assertTrue(busy.exists()); self.assertTrue(legacy.exists())
            self.assertEqual(self.coordinator._usage()[0],512)
        finally: lease.close()
        report=retention.collect(self.server,limit=64)
        self.assertIn(busy.name,report['removed_work']); self.assertTrue(legacy.exists())
        self.assertTrue(all(self.coordinator._valid(row) for row in self.coordinator.outputs()['entries'].values()))

    def test_entire_unstarted_batch_is_leased_and_cancellation_releases_its_reservations(self):
        self.setup(); self.batch(1); controller=refresh.Controlled(self.base)
        self.coordinator.submit=controller.submit
        cancel=threading.Event()
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(self.coordinator.refresh,self.requests(2),replace_requests=True,reclaim=self.reclaim,cancel=cancel)
            first=controller.next()
            try:
                self.assertEqual(len(list((self.coordinator.io.root/'work').iterdir())),80)
                report=retention.collect(self.server,limit=64)
                self.assertFalse(report['removed_work'])
                cancel.set()
                with self.assertRaises(CancelledError): future.result(5)
            finally:
                cancel.set(); controller.cleanup()
        self.assertFalse(list((self.coordinator.io.root/'work').iterdir()))
        self.assertEqual(len(self.coordinator.outputs()['entries']),80)

    def test_two_coordinators_cannot_overbook_or_collect_each_others_reserved_work(self):
        self.setup(artifacts=3); controller=refresh.Controlled(self.base); self.coordinator.submit=controller.submit
        other=br.Coordinator(self.coordinator.io.root,self.base,self.store,controller.submit,limits=self.coordinator.limits)
        with ThreadPoolExecutor(2) as pool:
            first=pool.submit(self.coordinator.refresh,self.requests(1,2),replace_requests=True)
            job=controller.next()
            try:
                with self.assertRaises(ResourceLimit): other.refresh(self.requests(2,2),replace_requests=True)
                self.assertEqual(self.coordinator._usage()[0],2)
                self.assertFalse(retention.collect(self.server)['removed_work'])
                controller.finish(job); controller.finish(controller.next())
                with self.assertRaises(br.StaleGeneration): first.result(5)
            finally: controller.cleanup()
        self.assertEqual(self.coordinator._usage()[0],0)


class HeldCapacityTest(unittest.TestCase):
    setUp=fx.RenderTest.setUp
    md=fx.RenderTest.md
    close=refresh.RefreshTest.close
    setup=proofs.RetentionTest.setup
    prepared=proofs.RetentionTest.prepared
    preview=proofs.RetentionTest.preview
    accept=proofs.RetentionTest.accept
    activate=proofs.RetentionTest.activate
    start=proofs.RetentionTest.start
    proof=proofs.RetentionTest.proof

    def test_paused_deferred_native_activation_holds_survive_pressure_until_exact_release_proof(self):
        self.start(); manager=self.manager; old=set(g for t in manager.transactions.values() for g in t['generations'])
        for t in manager.transactions.values(): t['state']='deferred'
        manager._save()
        self.store.disable(); self.server.refresh_building_overlays()
        for i in range(510): (self.io.objects/format(i,'032x')).mkdir()
        report={}; retention.make_room(self.server,manager,required=80,report=report)
        self.assertTrue(old.isdisjoint(report['removed_generations']))
        incomplete=self.proof(); incomplete.update(complete=False,slots=[],pending=[])
        retention.collect(self.server,manager,proof=incomplete,limit=64)
        self.assertTrue(manager.holds)
        self.assertTrue(all((self.io.objects/g).exists() for g in old))
        complete=self.proof(); complete.update(slots=[],pending=[])
        retention.collect(self.server,manager,proof=complete,limit=64)
        self.assertFalse(manager.holds)
        for _ in range(8): retention.collect(self.server,manager,limit=64)
        self.assertTrue(all(not (self.io.objects/g).exists() for g in old))
        self.assertTrue(all(manager.c._valid(row) for row in manager.c.outputs()['entries'].values()))


class PreparationDiagnosticsTest(unittest.TestCase):
    setUp=live.LiveTest.setUp
    md=live.LiveTest.md
    close=live.LiveTest.close
    setup=live.LiveTest.setup
    start=live.LiveTest.start
    command=live.LiveTest.command
    until=live.LiveTest.until
    preview=live.LiveTest.preview

    async def accepted(self):
        await self.start(street=True)
        self.service.runner.cancel()
        await asyncio.gather(self.service.runner,return_exceptions=True)
        proposal=await self.preview()
        await self.service.call(self.conn,lambda w,c:self.server.accept_osm_buildings(proposal['token'],manual=True,prepare=False))
        self.assertTrue(self.service._regions())

    def test_early_authority_failure_preserves_corruption_error_and_empty_report(self):
        async def scenario():
            await self.accepted()
            try:
                error=CorruptStorage('TEST ONLY early ownership validation failure')
                with mock.patch.object(self.service,'call',new=mock.AsyncMock(side_effect=error)):
                    with self.assertRaises(CorruptStorage) as caught: await self.service.prepare(self.conn)
                self.assertIs(caught.exception,error)
                self.assertEqual(self.service.current['resources'],{})
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_early_cancellation_is_not_masked_or_published_as_a_resource_report(self):
        async def scenario():
            await self.accepted()
            try:
                self.service.current['resources']={'held':99}
                with mock.patch.object(self.service,'call',new=mock.AsyncMock(side_effect=asyncio.CancelledError)):
                    with self.assertRaises(asyncio.CancelledError): await self.service.prepare(self.conn)
                self.assertEqual(self.service.current['resources'],{'held':99})
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_superseded_frame_does_not_publish_old_resource_diagnostics(self):
        async def scenario():
            await self.accepted()
            try:
                self.service.current['resources']={'held':99}
                async def moved(*args):
                    self.conn.active=2
                    return None,False
                with mock.patch.object(self.service,'call',new=moved): await self.service.prepare(self.conn)
                self.assertEqual(self.service.current['resources'],{'held':99})
                self.assertEqual(self.service.current['state'],'superseded')
            finally: await self.service.close()
        asyncio.run(scenario())
