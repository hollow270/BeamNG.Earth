"""Production-distance coverage, catalogs, real Lua and durable ownership.

The large empty receipts isolate orchestration/capacity from mesh cost. Separate
tests exercise real native meshes. Neither kind verifies actual BeamNG collision.
"""
import asyncio
from collections import Counter
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest import mock

import numpy as np

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import buildinginfluence as impact
import buildingtargets
import buildingrefresh as refresh
import buildingactivation as activation
import buildingretention as retention
import buildingbaseline
from buildingoverlaystore import RefreshInfo, ResourceLimit, CorruptStorage
import blockworker
import geodesy, geotiles, roads, vegetation
import test_buildingrender as render
import test_buildingrefresh as refreshfx
import test_buildingmanual as manual
import test_buildinglive as livefx
from test_buildingactivation import LuaGame

FRAME=geodesy.EnuFrame(39.29008564698664,-76.5842096724785)
BOUNDS=(-76.5854615264661,39.29215733555047,-76.58279539789308,39.29337328507307)


def settled(server,conn,scale):
    def near(radius,z): return {(int(x),int(y)) for x,y,_ in geotiles.tiles_near(FRAME,0,0,radius,z)}
    groups=near(1600*scale,geotiles.SKY_Z)
    far={tuple(map(int,key)) for x,y in groups for key in geotiles.children(x,y,geotiles.SKY_Z,geotiles.FAR_Z)}
    keys=dict(roads=near(640,geotiles.Z),bldfar=far,bldsky=near(4800*scale,geotiles.SKY_Z)-groups)
    server.anchors={conn.active:NS(id=conn.active,lat=FRAME.lat0,lon=FRAME.lon0,frame=FRAME,zooms=(16,))}
    server.near_visual='detail'
    server.geo_rules=lambda *args:(False,True,0.,None,None,16)
    for name,values in keys.items():
        tracker=roads.GeoTracker() if name=='roads' else vegetation.Tracker(kind=name,geo=True)
        tracker.aid=conn.active; tracker.sent=set(values); tracker.car=set(values)
        setattr(conn,name,tracker)


def world(scale):
    conn=NS(active=1)
    server=NS(cache_dir=str(render.REPO/'.nonexistent-influence-inspection'),connections=[conn],
              sidewalks=True,gas_stations=True,anchor_q=lambda a:1/256)
    settled(server,conn,scale)
    return server,conn


def historical_coverage(requests):
    regions=[]
    for r in requests:
        if r.kind.startswith('near'): continue # far grid contains their owners
        frame,box=r.geometry(); x0,y0,x1,y1=box
        # Distinct complete, small historical source contexts; none crosses a
        # skyline-group edge. Covers exactly the formerly overexpanded inventory.
        x,y=(x0+x1)/2,(y0+y1)/2
        la,lo,_=frame.enu_to_geodetic(np.array([x-2,x+2]),np.array([y-2,y+2]),0.)
        regions.append((float(lo.min()),float(la.min()),float(lo.max()),float(la.max())))
    return impact.Coverage(tuple(regions))


class InfluenceTest(unittest.TestCase):
    def test_exact_194_332_activation_and_durable_guard_expansion_counterexample(self):
        import mapcells
        from buildingoverlaystore import _union
        def no_margin(frame,box):
            x0,y0,x1,y1=box
            la,lo,_=frame.enu_to_geodetic(np.array([x0,x1,x0,x1]),np.array([y0,y0,y1,y1]),0.)
            return mapcells.cells_for_bbox(float(la.min()),float(lo.min()),float(la.max()),float(lo.max()))
        def own_box(req):
            frame,(x0,y0,x1,y1)=req.geometry()
            la,lo,_=frame.enu_to_geodetic(np.array([x0,x1,x0,x1]),np.array([y0,y0,y1,y1]),0.)
            return float(lo.min()),float(la.min()),float(lo.max()),float(la.max())
        for scale,prepared,expanded in ((.6,114,194),(1.,126,332)):
            server,conn=world(scale)
            with mock.patch.object(blockworker,'building_cells',no_margin):
                candidates=buildingtargets.discover(server,BOUNDS,current_only=True)
                self.assertEqual(len(candidates.requests),prepared)
                validation=buildingtargets.discover(server,_union([own_box(r) for r in candidates.requests]),max_targets=512)
                self.assertEqual(len(validation.requests),expanded)
                guards={str(k) for r in candidates.requests for k in no_margin(*r.geometry())}
                durable=impact.Coverage.read(impact.Coverage(legacy_cells=tuple(guards)).wire())
                self.assertEqual(len(impact.discover(server,durable).requests),expanded)
                self.assertEqual(expanded-prepared,80 if scale==.6 else 206)

    def test_exact_historical_counts_and_corrected_complete_cohorts(self):
        for scale,count in ((.6,191),(1.,312)):
            with self.subTest(scale=scale):
                server,conn=world(scale)
                old=buildingtargets.discover(server,BOUNDS,current_only=True,max_targets=512)
                self.assertEqual(len(old.requests),count)
                with self.assertRaises(ResourceLimit): buildingtargets.discover(server,BOUNDS,current_only=True)
                precise=impact.discover(server,impact.Coverage((BOUNDS,)))
                self.assertLess(len(precise.requests),128)
                self.assertEqual(Counter(r.kind for r in precise.requests),{'near_geo':2,'far_geo':2})
                plan=impact.Plan(precise.requests,impact.Coverage((BOUNDS,)))
                self.assertTrue(plan.validate({r.slot for r in precise.requests}))
                self.assertFalse(plan.validate({precise.requests[0].slot}))
                wide=historical_coverage(old.requests)
                actual=impact.discover(server,wide)
                self.assertEqual(len(actual.requests),count)
                plan=impact.Plan(actual.requests,wide)
                done=set(); queue=(); batches=[]
                while len(done)<count:
                    chosen,queue,p=plan.choose(done,conn,queue)
                    self.assertTrue(chosen); self.assertLessEqual(len(chosen),128)
                    self.assertTrue(plan.validate({r.slot for r in chosen}))
                    batches.append(chosen); done.update(r.slot for r in chosen)
                self.assertGreater(len(batches),1)
                self.assertEqual(len(done),count)

    def test_density_is_real_read_dependency_but_skyline_has_no_district_read(self):
        server,conn=world(1.)
        base=impact.discover(server,impact.Coverage((BOUNDS,)))
        with mock.patch('surface.dry_west',return_value=True):
            expanded=impact.discover(server,impact.Coverage((BOUNDS,)))
        self.assertGreater(len(expanded.requests),len(base.requests))
        self.assertEqual([r.slot for r in expanded.requests if r.kind=='skyline_geo'],[])

    def test_unknown_history_and_mixed_ownership_never_split(self):
        server,conn=world(1.)
        legacy=impact.Coverage(legacy_cells=tuple(map(str,__import__('mapcells').cells_for_bbox(BOUNDS[1],BOUNDS[0],BOUNDS[3],BOUNDS[2]))))
        requests=impact.discover(server,legacy).requests
        plan=impact.Plan(requests,legacy)
        self.assertEqual(len(plan.components),1)
        chosen,_,p=plan.choose(set(),conn)
        self.assertFalse(chosen); self.assertEqual(p['oversized'],312)
        reqs=list(requests[:3])+[refresh.TileRequest('near',0,0,lat=FRAME.lat0,lon=FRAME.lon0)]
        self.assertEqual(len(impact.Plan(reqs,impact.Coverage((BOUNDS,))).components),1)

    def test_parent_bounds_join_owners_and_fifo_does_not_starve_distant_work(self):
        server,conn=world(1.)
        old=buildingtargets.discover(server,BOUNDS,current_only=True,max_targets=512)
        coverage=historical_coverage(old.requests)
        plan=impact.Plan(old.requests,coverage)
        chosen,queue,p=plan.choose(set(),conn)
        first=impact.Plan.key(chosen)
        done={r.slot for r in chosen}
        next_chosen,queue,p=plan.choose(done,conn,queue)
        self.assertNotEqual(impact.Plan.key(next_chosen),first)
        # Simulate that near work becomes newly necessary. Already waiting groups
        # remain ahead of it instead of re-sorting to near on every pass.
        again,_,_=plan.choose(set(),conn,queue)
        self.assertEqual(impact.Plan.key(again),impact.Plan.key(next_chosen))
        # Weighted service turns promote new player-near collision work, while
        # a distant turn guarantees progress even if near groups keep arriving.
        urgent,_,_=plan.choose(set(),conn,queue,prefer_near=True)
        self.assertTrue(any(r.kind.startswith('near') for r in urgent))
        distant,_,_=plan.choose(set(),conn,queue,prefer_near=False)
        self.assertTrue(all(not r.kind.startswith('near') for r in distant))
        joined=impact.Plan(old.requests,coverage.union(impact.Coverage((BOUNDS,))))
        self.assertTrue(all(joined.validate({r.slot for r in c}) for c in joined.components))

    def test_coverage_serialization_is_bounded_canonical_and_corruption_fails_closed(self):
        coverage=impact.Coverage((BOUNDS,BOUNDS))
        self.assertEqual(coverage,impact.Coverage.read(coverage.wire()))
        self.assertEqual(coverage.union(coverage),coverage)
        self.assertEqual(impact.Coverage(legacy_cells=('14/0001/0002',)).legacy_cells,('14/1/2',))
        with self.assertRaises(CorruptStorage): impact.Coverage.read(dict(version=True,regions=[],legacy_cells=[]))
        with self.assertRaises(CorruptStorage): impact.Coverage.read(dict(version=1,regions=[BOUNDS],legacy_cells=['14/99999/0']))
        with self.assertRaises(ResourceLimit): impact.Plan([None]*513,coverage)

    def test_cross_group_parent_parts_and_old_new_owners_are_indivisible(self):
        server,conn=world(1.)
        x,y=geotiles.tile_of(FRAME.lat0,FRAME.lon0,geotiles.SKY_Z)
        requests=[refresh.TileRequest('far_geo',(x+i)*4+1,y*4+1,aid=0) for i in (0,1)]
        separate=historical_coverage(requests)
        self.assertEqual(len(impact.Plan(requests,separate).components),2)
        bounds=__import__('buildingoverlaystore')._union(separate.regions)
        # Store Entry.bounds includes original parent, accepted parent and parts.
        snapshot=NS(entries=(NS(bounds=bounds),),refreshes=(),affected_bounds=bounds)
        joined=separate.with_snapshot(snapshot)
        plan=impact.Plan(requests,joined)
        self.assertEqual(len(plan.components),1)
        self.assertFalse(plan.validate({requests[0].slot}))

    def test_unready_near_component_does_not_starve_independent_ready_components(self):
        server,conn=world(1.)
        old=buildingtargets.discover(server,BOUNDS,current_only=True,max_targets=512)
        plan=impact.Plan(old.requests,historical_coverage(old.requests))
        conn.roads.sent.clear(); conn.roads.empty.clear()
        chosen,_,progress=plan.choose(set(),conn)
        self.assertTrue(chosen)
        self.assertTrue(all(not r.kind.startswith('near') for r in chosen))
        self.assertGreater(progress['blocked'],0)


class AdmissionTest(unittest.TestCase):
    def setup(self,*,available=2,size=64):
        server,conn=world(1.)
        x,y=geotiles.tile_of(FRAME.lat0,FRAME.lon0,geotiles.SKY_Z)
        requests=[refresh.TileRequest('far_geo',(x+i)*4+1,y*4+1,aid=0) for i in range(3)]
        coverage=historical_coverage(requests)
        entries={r.slot:dict(request=r.wire(),payload={'shape':'building.cdae'},files={'building.cdae':{'size':size}})
                 for r in requests}
        catalog=dict(requests={r.slot:r.wire() for r in requests},entries=entries,cohort=impact.Plan(requests,coverage).document())
        manager=object.__new__(activation.Manager)
        manager.holds={i:(1,'held') for i in range(activation.MAX_SLOTS-available)}
        manager.bindings={}; manager.transactions={}; manager.s=NS(matslots=NS(slot={}))
        manager._payload=lambda row:(refresh.TileRequest(**row['request']),row['payload'])
        return manager,conn,catalog,requests,coverage

    def test_slot_pressure_admits_only_complete_independent_components(self):
        manager,conn,catalog,requests,coverage=self.setup()
        self.assertEqual(len(manager._admit(conn,catalog,set(catalog['requests']),False)),2)
        self.assertEqual(len(manager.holds),254,'admission is read-only')
        # Linking old/new owners makes the three targets inseparable: two slots
        # cannot admit an arbitrary prefix of that family.
        joined=coverage.union(impact.Coverage((__import__('buildingoverlaystore')._union(coverage.regions),)))
        catalog['cohort']=impact.Plan(requests,joined).document()
        with self.assertRaises(ResourceLimit): manager._admit(conn,catalog,set(catalog['requests']),False)
        self.assertEqual(len(manager.holds),254)

    def test_copy_and_retained_byte_budgets_apply_to_the_complete_cohort(self):
        size=activation.MAX_COPY_BYTES//2
        manager,conn,catalog,_,_=self.setup(available=256,size=size)
        self.assertEqual(len(manager._admit(conn,catalog,set(catalog['requests']),False)),2)
        manager.transactions={'old':dict(transaction='old',bytes=activation.MAX_RETAINED_BYTES-size,owners=['owner'])}
        manager.s.matslots.slot['owner']='slot_1'
        self.assertEqual(len(manager._admit(conn,catalog,set(catalog['requests']),False)),1)

    def test_verified_reuse_requires_no_new_slot_or_copy_budget(self):
        manager,conn,catalog,_,_=self.setup(available=0)
        manager._reuse=lambda *args:True
        self.assertEqual(len(manager._admit(conn,catalog,set(catalog['requests']),False)),3)


class ProductionTest(unittest.TestCase):
    setUp=render.RenderTest.setUp
    md=render.RenderTest.md
    close=refreshfx.RefreshTest.close
    setup=manual.ManualTest.setup
    start=livefx.LiveTest.start
    until=livefx.LiveTest.until

    async def inspect(self):
        await self.service.call(self.conn,lambda w,c:None)

    def packets(self,start=0):
        return [m for m in self.messages[start:] if m['type'] in ('building_activate','building_activate_batch')]

    def empty_coordinator(self):
        def submit(req,snapshot,directory,vfs,identity,limits):
            self.assertLessEqual(len(list(c.io.objects.iterdir()))+len(list((c.io.root/'work').iterdir())),512)
            Path(directory,'tile.json').write_text(json.dumps(dict(v=1,h='synthetic-empty',colliders={})),encoding='utf-8')
            files,content,meshes=refresh.scan(directory,vfs,limits)
            with self.base.building_snapshot(snapshot): dependency=req.inputs(self.base)[1]
            result={'content':[vfs+'/tile.json',0]} if req.kind=='near_geo' else [vfs+'/tile.json',0]
            row=dict(request=req.wire(),identity=identity,snapshot=snapshot.revision,baseline_release=render.RELEASE,
                     dependency=dependency,files=files,content_digest=content,mesh_digest=meshes,result=result)
            future=Future(); future.set_result(row); return future
        c=refresh.Coordinator(Path(self.server.cache_dir)/'building-generations',self.base,self.store,submit)
        self.server._building_refresh=c
        return c

    async def history(self,scale=1.):
        await self.start(); self.service.busy=True
        c=self.empty_coordinator()
        settled(self.server,self.conn,scale)
        old=buildingtargets.discover(self.server,BOUNDS,current_only=True,max_targets=512)
        coverage=historical_coverage(old.requests)
        path=c.io.root/('activation-'+'d'*16+'.json')
        c.io._atomic_write(path,c.io._document(dict(session='historical',client='c'*64,holds={},transactions=[],
            guards=sorted(coverage.cells()),influence=coverage.wire())))
        await self.inspect()
        return c,coverage

    async def finish(self):
        for _ in range(40):
            await self.cycle()
            self.assertLessEqual(self.server._building_refresh._usage()[0],512)
            if self.service.current.get('progress',{}).get('pending')==0: return
            self.assertFalse(self.service.current['game_activated'])
        self.fail(str(self.service.current))

    async def cycle(self,*,game=None):
        before=len(self.messages)
        await self.service.prepare(self.conn,explicit=True)
        packets=self.packets(before)
        if packets:
            if game:
                seeded=getattr(game,'seeded',set())
                for packet in packets:
                    for target in packet['targets']:
                        if target['kind'].startswith('near') and target['key'] not in seeded:
                            game.lua.globals().nearSeed(target['key'],False)
                            seeded.add(target['key'])
                game.seeded=seeded
            for packet in packets:
                self.assertLessEqual(len(packet['targets']),32)
                if game: self.assertTrue(game.packet(packet))
            if game:
                game.tick(100)
                self.assertEqual(game.last()['status'],'activated',game.last())
                ack=game.last()
            else:
                last=packets[-1]
                ack=dict(type='building_ack',**{k:last[k] for k in ('sessionId','epoch','anchorId','seq','transaction')},
                         status='activated',detail='SIMULATED acknowledgement; not actual engine collision')
            await self.service.acknowledge(self.conn,ack)
        return packets

    def test_settled_near_and_normal_native_preparation_activation_and_persistent_guards(self):
        async def scenario():
            old=render.record(geometry=render.polygon(-20,275,10,10,FRAME))
            new=render.incoming(geometry=render.polygon(-20,275,18,10,FRAME))
            await self.start([old])
            self.service.busy=True
            try:
                game=LuaGame(); game.lua.globals().roads.setGeo(True)
                for scale,expected in ((.6,191),(1.,312)):
                    settled(self.server,self.conn,scale)
                    for md in (self.base,self.worker_md):
                        for key in __import__('mapcells').cells_for_bbox(BOUNDS[1],BOUNDS[0],BOUNDS[3],BOUNDS[2]):
                            md.cell(key,layers=('buildings','building_parts','roads'))
                    context=buildingbaseline.collect(self.base,BOUNDS,self.store.load(),incoming=(new,))
                    if not self.store.load().entries:
                        self.store.refresh(context.buildings,(new,),baseline_parts=context.parts,
                            info=RefreshInfo(context.bounds,'2026-10-30T12:00:00Z',({'dataset':'OpenStreetMap'},),render.RELEASE))
                        self.store.enable()
                    await self.inspect()
                    original=buildingtargets.discover(self.server,BOUNDS,current_only=True,max_targets=512)
                    self.assertEqual(len(original.requests),expected)
                    discovered=impact.discover(self.server,self.service.influence)
                    for req in discovered.requests:
                        for key in blockworker.building_cells(*req.geometry()):
                            self.worker_md.cell(key,layers=('buildings','building_parts','roads'))
                    packets=await self.cycle(game=game)
                    if packets:
                        self.assertTrue(any(t['slots'] for p in packets for t in p['targets']))
                    self.assertTrue(self.service.current['game_activated'],self.service.current)
                    self.assertEqual(self.service.current['progress']['pending'],0)
                    coverage=retention.retained_influence(self.server._building_refresh.io.root)
                    self.assertEqual(coverage.legacy_cells,())
                    self.assertLess(len(impact.discover(self.server,coverage).requests),128)
                    self.assertEqual(self.base.versions(),self.worker_md.versions())
                    self.assertEqual(len(self.transport.calls),0)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_real_191_and_312_target_cohort_catalogs_acknowledgements_and_restart(self):
        async def scenario():
            await self.start(); self.service.busy=True
            try:
                c=self.empty_coordinator()
                game=LuaGame(); game.lua.globals().roads.setGeo(True)
                for scale,expected in ((.6,191),(1.,312)):
                    settled(self.server,self.conn,scale)
                    old=buildingtargets.discover(self.server,BOUNDS,current_only=True,max_targets=512)
                    coverage=historical_coverage(old.requests)
                    if scale==1.:
                        # Previous Near coverage would intentionally retain its old
                        # skyline ring. Remove no evidence: use a separate fresh test
                        # context for the exact Normal inventory below.
                        coverage=coverage.union(retention.retained_influence(c.io.root))
                    path=c.io.root/('activation-'+'d'*16+'.json')
                    c.io._atomic_write(path,c.io._document(dict(session='historical',client='c'*64,holds={},transactions=[],
                        guards=sorted(coverage.cells()),influence=coverage.wire())))
                    await self.inspect()
                    wanted=len(impact.discover(self.server,self.service.influence).requests)
                    self.assertGreaterEqual(wanted,expected)
                    seen=set(); publications=[]
                    for _ in range(40):
                        packets=await self.cycle(game=game)
                        if packets:
                            targets=[t for p in packets for t in p['targets']]
                            self.assertLessEqual(len(targets),128)
                            seen.update(t['slot'] for t in targets)
                            publications.append(packets[-1]['publication'])
                            outputs=c.outputs()
                            self.assertTrue(set(outputs['entries'])<={t['slot'] for t in targets})
                            self.assertLessEqual(len(outputs['requests']),128)
                        progress=self.service.current.get('progress',{})
                        if progress.get('pending')==0: break
                        self.assertFalse(self.service.current['game_activated'])
                    self.assertEqual(self.service.current['progress']['pending'],0,self.service.current)
                    self.assertTrue(self.service.current['game_activated'])
                    self.assertGreater(len(publications),1)
                    self.assertGreater(len(self.service.authority.bindings),128)
                    self.assertEqual(retention.retained_influence(c.io.root),self.service.influence)
                    reopened=refresh.Coordinator(c.io.root,self.base,self.store,c.submit)
                    self.assertEqual(reopened.outputs(),c.outputs())
                    self.assertLessEqual(c._usage()[0],512)
                    self.assertEqual(list((c.io.root/'work').iterdir()),[])
                self.assertEqual(len(self.transport.calls),0)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_journal_mismatch_and_legacy_history_fail_closed(self):
        async def scenario():
            await self.start(); self.service.busy=True
            try:
                c=self.empty_coordinator()
                path=c.io.root/('activation-'+'d'*16+'.json')
                cov=impact.Coverage((BOUNDS,))
                value=dict(session='historical',holds={},transactions=[],guards=[],influence=cov.wire())
                c.io._atomic_write(path,c.io._document(value))
                with self.assertRaises(CorruptStorage): retention.retained_influence(c.io.root)
                value.pop('influence'); value['guards']=sorted(cov.cells())
                c.io._atomic_write(path,c.io._document(value))
                self.assertEqual(set(retention.retained_influence(c.io.root).legacy_cells),cov.cells())
                c.path.unlink()
                with self.assertRaises(CorruptStorage): retention.retained_influence(c.io.root)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_partial_components_supersession_reanchor_outside_return_and_bounded_generations(self):
        async def scenario():
            c,coverage=await self.history()
            try:
                await self.cycle()
                self.assertFalse(self.service.current['game_activated'])
                self.assertGreater(self.service.current['progress']['acknowledged'],0)
                self.assertGreater(self.service.current['progress']['pending'],0)
                # ABA of enable state requires new acknowledgements even when
                # empty native inputs are identical. No old validation stamp wins.
                self.store.set_enabled(True); self.store.set_enabled(False)
                await self.inspect(); await self.finish()
                self.assertTrue(self.service.current['game_activated'])
                for aid in (2,3):
                    self.conn.active=aid; self.service.context_changed(self.conn)
                    if self.service.context_task: await self.service.context_task
                    before=c._usage()
                    await self.service.prepare(self.conn)
                    self.assertEqual(c._usage(),before)
                    self.assertEqual(self.service.current['restoration']['code'],'trackers_settling')
                    settled(self.server,self.conn,1.)
                    if aid==2:
                        # Outside the accepted regions: no success claim for the
                        # absent geometry, and no artificial regeneration batch.
                        for name in ('roads','bldfar','bldsky'):
                            getattr(self.conn,name).sent.clear(); getattr(self.conn,name).car.clear()
                        await self.cycle()
                        self.assertFalse(self.service.current['game_activated'])
                    else:
                        await self.finish()
                        self.assertTrue(self.service.current['game_activated'])
                self.assertEqual(self.service.current['progress']['required'],312)
                self.assertEqual(list((c.io.root/'work').iterdir()),[])
                self.assertEqual(retention.retained_influence(c.io.root),coverage)
                self.service.context_changed(self.conn,disconnect=True)
                if self.service.context_task: await self.service.context_task
                self.assertFalse(self.service.current['game_activated'])
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_guard_overflow_is_only_a_bounded_hint_cache_and_roads_are_unchanged(self):
        async def scenario():
            c,coverage=await self.history()
            try:
                requests=impact.discover(self.server,coverage).requests
                for req in requests:
                    near=req.kind.startswith('near')
                    message=dict(type='roads_tile' if near else 'bldsky_tile' if req.kind=='skyline_geo' else 'bldfar_tile',anchorId=1,key=activation._key(req),
                        frame=[FRAME.lat0,FRAME.lon0],slots={},data=dict(road={'shape':'road.cdae'},terrain='untouched',
                        bld={'shape':'unprepared.cdae'},shape='unprepared.cdae',colliders={'road':[[1,2,3,4,5,6]],'bldcol':[[2,3,4,5,6,7]]}))
                    if req.kind=='skyline_geo': message['kind']='S'
                    before=deepcopy(message)
                    out=self.service.filter_message(self.conn,message)
                    self.assertEqual(message,before)
                    self.assertTrue(out['data'].get('buildingPending'))
                    self.assertEqual(out['data']['road'],message['data']['road'])
                    self.assertEqual(out['data']['terrain'],'untouched')
                    self.assertEqual(out['data']['colliders']['road'],message['data']['colliders']['road'])
                    self.assertNotIn('bldcol',out['data']['colliders'])
                self.assertEqual(len(self.service.pending),128)
                self.assertTrue(self.service.public(self.conn)['deferred_overflow'])
                await self.finish()
                self.assertEqual(self.service.current['progress']['acknowledged'],312)
                self.assertEqual(len(self.service.pending),0)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_incomplete_lod_catalog_and_new_coupled_streaming_target_are_rejected(self):
        async def scenario():
            await self.start(); self.service.busy=True
            try:
                c=self.empty_coordinator(); settled(self.server,self.conn,1.)
                coverage=impact.Coverage((BOUNDS,))
                requests=impact.discover(self.server,coverage).requests
                plan=impact.Plan(requests,coverage)
                first=requests[:1]
                result=c.refresh(first,replace_requests=True,cohort=plan.document())
                def activate(w,conn): return self.service._manager(w).activate(conn,result['publication'],manual=True)
                with self.assertRaisesRegex(activation.Rejected,'targets/settings'):
                    await self.service.call(self.conn,activate)
                self.assertEqual(self.service.authority.holds,{})
                result=c.refresh(requests,replace_requests=True,cohort=plan.document())
                # An overlapping skyline representation appears after preparation.
                x,y=geotiles.tile_of(39.2927,-76.5842,geotiles.SKY_Z)
                self.conn.bldsky.sent.add((x,y))
                with self.assertRaisesRegex(activation.Rejected,'targets/settings'):
                    await self.service.call(self.conn,activate)
                self.assertEqual(self.service.authority.holds,{})
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_proven_active_roots_cause_true_admission_failure_without_partial_publication(self):
        async def scenario():
            c,coverage=await self.history()
            try:
                generations=[]
                for i in range(510):
                    name=format(i,'032x'); path=c.io.objects/name; path.mkdir()
                    (path/'local.json').write_text('{}'); generations.append(name)
                rows=[dict(transaction=format(i+1,'032x'),state='orphaned',generations=generations[i*128:(i+1)*128]) for i in range(4)]
                path=c.io.root/('activation-'+'b'*16+'.json')
                c.io._atomic_write(path,c.io._document(dict(session='held',holds={},transactions=rows)))
                before=c.outputs()
                with self.assertRaises(ResourceLimit): await self.service.prepare(self.conn)
                self.assertEqual(c.outputs()['publication'],before['publication'])
                self.assertEqual(c.outputs()['entries'],before['entries'])
                self.assertEqual(len(list(c.io.objects.iterdir())),510)
                self.assertEqual(list((c.io.root/'work').iterdir()),[])
                self.assertFalse(self.service.current['game_activated'])
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_native_unchanged_remote_mesh_and_dependency_are_both_unchanged(self):
        source=render.record()
        remote=render.record('gers-remote',x=1100,y=60,osm='w3@1')
        baseline=self.md([source,remote])
        render.RenderTest.save(self,self.md([source]),[render.incoming(w=18)])
        overlay=self.md([source,remote],overlay=self.store)
        req=refresh.TileRequest('far',4,0,lat=render.LAT,lon=render.LON)
        self.assertEqual(req.inputs(baseline),req.inputs(overlay))
        meshes=[]
        import buildings
        for md in (baseline,overlay):
            items,_,_=buildings.far_items(render.FRAME,4,0,md,render.ground)
            self.assertEqual(len(items),1)
            meshes.append(buildings.build(items,lite=True))
        for a,b in zip(*meshes): np.testing.assert_array_equal(a,b)

    def test_buffered_activation_is_cancelled_if_coupled_streaming_changes_before_send(self):
        async def scenario():
            c,coverage=await self.history()
            try:
                await self.cycle()
                installed=deepcopy(self.service.authority.bindings)
                original=activation.Manager.activate
                loop=asyncio.get_running_loop()
                def materialize(manager,conn,*args,**kwargs):
                    result=original(manager,conn,*args,**kwargs)
                    target=manager.transactions[result['transaction']]['targets'][0]
                    z,x,y=geotiles.parse(target['key'])
                    if z==geotiles.SKY_Z: x,y=x*4,y*4
                    loop.call_soon_threadsafe(self.conn.bldsky.sent.add,(x//4,y//4))
                    return result
                before=len(self.messages)
                with mock.patch.object(activation.Manager,'activate',materialize):
                    await self.service.prepare(self.conn)
                self.assertEqual(self.packets(before),[])
                self.assertFalse(self.service.current['game_activated'])
                self.assertEqual(self.service.authority.holds,{})
                self.assertTrue(all(t['state']=='retired' for t in self.service.authority.transactions.values()))
                self.assertEqual(self.service.authority.bindings,installed)
                # Accepted data and durable source fencing are preserved for retry.
                self.assertEqual(retention.retained_influence(c.io.root),coverage)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_pause_duplicate_retry_and_superseded_ack_keep_one_pending_cohort(self):
        async def scenario():
            c,coverage=await self.history()
            try:
                game=LuaGame(); game.lua.globals().roads.setGeo(True)
                await self.service.prepare(self.conn)
                packets=self.packets()
                for packet in packets:
                    for target in packet['targets']:
                        if target['kind'].startswith('near'): game.lua.globals().nearSeed(target['key'],False)
                    self.assertTrue(game.packet(packet))
                game.lua.globals().simPaused=True
                game.tick(100)
                self.assertEqual(game.last()['status'],'deferred')
                before=c.outputs()['publication']; usage=c._usage()
                await self.service.prepare(self.conn)
                self.assertEqual(c.outputs()['publication'],before)
                self.assertEqual(c._usage(),usage)
                self.assertEqual(len(self.service.authority.transactions),1)
                self.assertFalse(self.service.current['game_activated'])
                game.lua.globals().simPaused=False; game.tick(100)
                self.assertEqual(game.last()['status'],'activated')
                # Same enabled value and native bytes after ABA do not validate a
                # transaction created for the old accepted-state capability.
                self.store.set_enabled(True); self.store.set_enabled(False)
                await self.service.acknowledge(self.conn,game.last())
                self.assertEqual(self.service.current['state'],'superseded')
                self.assertFalse(self.service.current['game_activated'])
                self.assertFalse(any(b.get('validated_state')==self.store.load_state()[1]
                                     for b in self.service.authority.bindings.values()))
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_cancelled_cohort_never_publishes_its_coverage_or_loses_old_catalog(self):
        from concurrent.futures import ThreadPoolExecutor,CancelledError
        import threading
        async def scenario():
            c,coverage=await self.history()
            try:
                await self.cycle()
                before=c.outputs()
                req=refresh.TileRequest('far',100,100,lat=render.LAT,lon=render.LON)
                entered=threading.Event(); cancelled=threading.Event(); future=Future(); future.set_running_or_notify_cancel()
                args=[]
                def submit(*a): args.extend(a); entered.set(); return future
                original=c.submit; c.submit=submit
                with ThreadPoolExecutor(1) as runner:
                    try:
                        job=runner.submit(c.refresh,[req],cancel=cancelled,replace_requests=True,
                                          cohort=impact.Plan((req,),coverage).document())
                        self.assertTrue(entered.wait(5)); cancelled.set()
                        with self.assertRaises(CancelledError): job.result(5)
                        self.assertEqual(c.outputs(),before | {'sequence':before['sequence']+1})
                        self.assertTrue(Path(args[2]).exists(),'running worker retains its leased private work')
                    finally:
                        if args:
                            future.set_result(refreshfx.fake_receipt(*args))
                        c.submit=original
                self.assertEqual(c.outputs()['cohort'],before['cohort'])
                self.assertEqual(c.outputs()['publication'],before['publication'])
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_native_disable_reenable_and_rollback_keep_coherent_accepted_geometry(self):
        async def scenario():
            old=render.record(geometry=render.polygon(-20,275,10,10,FRAME))
            await self.start([old]); self.service.busy=True
            try:
                settled(self.server,self.conn,1.)
                def save(width):
                    new=render.incoming(geometry=render.polygon(-20,275,width,10,FRAME))
                    for md in (self.base,self.worker_md):
                        for key in __import__('mapcells').cells_for_bbox(BOUNDS[1],BOUNDS[0],BOUNDS[3],BOUNDS[2]):
                            md.cell(key,layers=('buildings','building_parts','roads'))
                    context=buildingbaseline.collect(self.base,BOUNDS,self.store.load(),incoming=(new,))
                    return self.store.refresh(context.buildings,(new,),baseline_parts=context.parts,
                        info=RefreshInfo(context.bounds,'2026-10-30T12:00:00Z',({'dataset':'OpenStreetMap'},),render.RELEASE))
                first=save(18); self.store.enable(); await self.inspect()
                for req in impact.discover(self.server,self.service.influence).requests:
                    for key in blockworker.building_cells(*req.geometry()):
                        self.worker_md.cell(key,layers=('buildings','building_parts','roads'))
                game=LuaGame(); game.lua.globals().roads.setGeo(True)
                await self.cycle(game=game)
                self.assertTrue(self.service.current['game_activated'])
                c=self.server._building_refresh
                first_meshes={k:v['mesh_digest'] for k,v in c.outputs()['entries'].items()}
                for enabled in (False,True):
                    self.store.set_enabled(enabled); await self.inspect()
                    self.assertFalse(self.service.current['game_activated'])
                    await self.cycle(game=game)
                    self.assertTrue(self.service.current['game_activated'])
                    self.assertEqual(self.service.current['activated_enabled'],enabled)
                    meshes={k:v['mesh_digest'] for k,v in c.outputs()['entries'].items()}
                    if enabled: self.assertEqual(meshes,first_meshes)
                    else: self.assertNotEqual(meshes,first_meshes)
                save(22); await self.inspect(); await self.cycle(game=game)
                self.assertNotEqual({k:v['mesh_digest'] for k,v in c.outputs()['entries'].items()},first_meshes)
                self.store.rollback(first.revision); await self.inspect(); await self.cycle(game=game)
                self.assertEqual({k:v['mesh_digest'] for k,v in c.outputs()['entries'].items()},first_meshes)
                self.assertEqual(len(self.transport.calls),0)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_streaming_never_replays_stale_binding_after_revision_or_renderer_change(self):
        async def scenario():
            c,coverage=await self.history()
            try:
                await self.finish()
                self.assertTrue(self.service.current['game_activated'])
                target=next(b for b in self.service.authority.bindings.values() if b['kind']=='near_geo')
                message=dict(type='roads_tile',anchorId=1,key=target['key'],slots={},
                    data=dict(road={'shape':'original-road.cdae'},bld={'shape':'baseline.cdae'},colliders={}))
                self.server.near_visual='mesh'
                self.assertFalse(self.service.public(self.conn)['game_activated'])
                self.assertTrue(self.service.filter_message(self.conn,message)['data']['buildingPending'])
                await self.inspect()
                out=self.service.filter_message(self.conn,message)
                self.assertTrue(out['data']['buildingPending'])
                self.assertEqual(out['data']['road'],message['data']['road'])
                self.assertNotIn('buildingGeneration',out)
                await self.finish()
                self.assertTrue(self.service.current['game_activated'])
                self.store.set_enabled(True); self.store.set_enabled(False); await self.inspect()
                out=self.service.filter_message(self.conn,message)
                self.assertTrue(out['data']['buildingPending'])
                self.assertNotIn('buildingGeneration',out)
                self.assertFalse(self.service.public(self.conn)['game_activated'])
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_partial_restart_rebuilds_progress_and_retains_unproven_older_session(self):
        import buildinglive
        import matslots
        async def scenario():
            c,coverage=await self.history()
            old_service=self.service
            try:
                await self.cycle()
                self.assertGreater(self.service.current['progress']['acknowledged'],0)
                self.assertGreater(self.service.current['progress']['pending'],0)
                previous={p.name for p in c.io.objects.iterdir()}
                await old_service.close()
                # New process/session identity, same durable overlay/catalog,
                # with no inventory proof permitting deletion of the old journal.
                self.server.sid='restarted-companion'
                self.server.out_dir=str(self.root/'restarted-session'); Path(self.server.out_dir).mkdir()
                self.server.vfs_dir='/temp/beamngEarth/restarted-companion'
                self.server.matslots=matslots.MatSlots(self.server.sid,self.server.slot_held)
                self.conn.building_epoch='f'*32
                self.service=self.server._building_live=buildinglive.Service(self.server,local_control=True,clock=self.clock)
                hello=self.service.hello(self.conn,dict(buildingControl=1,buildingBatches=1,buildingClient=old_service.client_id))
                self.assertEqual(hello['buildingControl'],1)
                self.service.busy=True
                await self.inspect()
                self.assertFalse(self.service.current['game_activated'])
                self.assertIsNone(self.service.completed_state)
                self.assertEqual(self.service.influence,coverage)
                await self.finish()
                self.assertEqual(self.service.current['progress']['acknowledged'],312)
                self.assertTrue(self.service.current['game_activated'])
                self.assertTrue(all((c.io.objects/p).exists() for p in previous))
                self.assertEqual(len(list(c.io.root.glob('activation-*.json'))),3)
                self.assertEqual(list((c.io.root/'work').iterdir()),[])
                self.assertEqual(len(self.transport.calls),0)
            finally:
                await self.service.close()
                if not old_service.closed: await old_service.close()
        asyncio.run(scenario())

    def test_streaming_racing_ack_then_explicit_retry_replans_without_stale_pending_error(self):
        async def scenario():
            c,coverage=await self.history()
            try:
                original=activation.Manager.acknowledge
                loop=asyncio.get_running_loop()
                def acknowledge(manager,conn,msg):
                    result=original(manager,conn,msg)
                    target=manager.transactions[msg['transaction']]['targets'][0]
                    z,x,y=geotiles.parse(target['key'])
                    loop.call_soon_threadsafe(self.conn.bldsky.sent.add,(x//4,y//4))
                    return result
                with mock.patch.object(activation.Manager,'acknowledge',acknowledge):
                    await self.cycle()
                self.assertFalse(self.service.current['game_activated'])
                self.assertTrue(any(t['state']=='activated' for t in self.service.authority.transactions.values()))
                before=len(self.messages)
                await self.service.prepare(self.conn,explicit=True)
                self.assertTrue(self.packets(before))
                self.assertIsNone(self.service.fallback_frame)
                self.assertEqual(self.service.current['state'],'activation_scheduled')
            finally: await self.service.close()
        asyncio.run(scenario())


if __name__=='__main__': unittest.main()
