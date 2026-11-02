"""Real merge/store/ordinary native writers; mocked HTTP, no game or live service."""
import ast
import asyncio
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace as NS
import unittest
from unittest import mock

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import buildingstartup as startup
import buildingoverlay as bo
from buildingoverlaystore import OverlayStore, CorruptStorage, Limits, ResourceLimit
import blockworker as bw
import buildingrender
import buildingtargets
import buildingrefresh
import earthd
import geodesy,geotiles,mapcells,roads,revisit
import osmbuildings as osm
import netfetch
import test_buildingrender as fx
import test_buildingrefresh as regen
import test_buildingmanual as manual
import test_osmbuildings as ofx


def worker_probe(cache,snapshot):
    # Same initializer contract as production and respawn, without any game/HTTP.
    original=bw.mapdatamod.MapData
    with mock.patch.object(bw.mapdatamod,'MapData',side_effect=lambda *a,**kw:original(*a,**dict(kw,release=fx.RELEASE))):
        bw.init(str(cache),True,True,False,False,False,None,building_snapshot=snapshot)
    return bw._s.md._building_view.snapshot,bw._s.md.versions()


class StartupTest(unittest.TestCase):
    setUp=fx.RenderTest.setUp
    close=regen.RefreshTest.close
    md=fx.RenderTest.md

    def setup(self,rows=()):
        self.base=self.md(rows,street=True)
        self.now=ofx.WALL
        self.cache=self.root/'startup-cache'
        self.config=startup.Config((('test',manual.bounds()),))
        self.net=netfetch.Fetcher(transport=object(),wall=lambda:self.now,clock=lambda:self.now)
        self.base.fetcher=self.net
        self.addCleanup(self.net.close)
        self.transport=ofx.FakeTransport()
        self.client=osm.Client(ofx.CONFIG,fetcher=self.net,transport=self.transport,wall=lambda:self.now)
        self.payload()

    def payload(self,*,width=18,rows=None,replication=None,**kwargs):
        stamp=replication or datetime.fromtimestamp(self.now-60,timezone.utc).isoformat().replace('+00:00','Z')
        self.transport.payload=ofx.response(manual.way(w=width) if rows is None else rows,replication=stamp,**kwargs)

    def run_startup(self,*,config=None,**kwargs):
        return startup.prepare(self.base,self.cache,config or self.config,client=self.client,store=self.store,
            wall=lambda:self.now,clock=lambda:self.now,**kwargs)

    def view(self,snapshot):
        md=self.md(self.base.originals,self.base.original_parts,street=True)
        md.pin_buildings(snapshot)
        return md

    def native_far(self,md,path):
        path.mkdir(parents=True,exist_ok=True)
        with mock.patch.object(bw,'_s',regen.state(md,self.root/'ordinary-cache')):
            result=bw.far_buildings_tile(1,fx.LAT,fx.LON,15,0,0,str(path),'/synthetic')
        data=json.loads(Path(path,Path(result[0]).name).read_text())
        shape=data.get('shape')
        return data,None if not shape else Path(path,Path(shape).name).read_bytes()

    def test_two_sessions_changed_geometry_same_id_old_cache_and_unrelated_baseline(self):
        self.setup([fx.record()])
        first,report=self.run_startup()
        self.assertTrue(report['checked'] and report['accepted']); self.assertFalse(report['edit_observed'])
        self.assertTrue(first.enabled)
        a=self.view(first)
        first_key=bw.building_versions(a,fx.FRAME,(0,0,256,256))
        remote_key=bw.building_versions(a,fx.FRAME,(1100,0,1356,256))
        data1,mesh1=self.native_far(a,self.root/'one')
        self.assertIsNotNone(mesh1)
        baseline_arrays={k:v.copy() for k,v in self.base.cell(fx.CELL,layers=('roads','buildings')).layers['roads'].to_arrays('').items()}
        self.now+=601; self.payload(width=26)
        second,report2=self.run_startup()
        b=self.view(second)
        self.assertTrue(report2['accepted']); self.assertNotEqual(first.revision,second.revision)
        self.assertNotEqual(first_key,bw.building_versions(b,fx.FRAME,(0,0,256,256)))
        self.assertEqual(remote_key,bw.building_versions(b,fx.FRAME,(1100,0,1356,256)))
        data2,mesh2=self.native_far(b,self.root/'two')
        self.assertNotEqual(mesh1,mesh2)
        _,reused=self.native_far(b,self.root/'three')
        self.assertEqual(mesh2,reused)
        self.assertEqual(first_key,bw.building_versions(a,fx.FRAME,(0,0,256,256)))
        self.assertEqual(a.versions(),b.versions())
        for k,v in baseline_arrays.items():
            __import__('numpy').testing.assert_array_equal(v,b.cell(fx.CELL,layers=('roads',)).layers['roads'].to_arrays('')[k])
        self.assertFalse((self.cache/'building-generations').exists())

    def test_unchanged_checks_do_not_append_revisions_or_refreshes(self):
        self.setup([fx.record()]); first,_=self.run_startup()
        for _ in range(4):
            self.now+=601; self.payload()
            snapshot,report=self.run_startup()
            self.assertTrue(report['checked']); self.assertFalse(report['accepted'])
            self.assertEqual(snapshot.revision,first.revision)
            self.assertEqual(len(snapshot.history),1); self.assertEqual(len(snapshot.refreshes),1)
        self.assertEqual(len(self.transport.calls),5)

    def test_conflicts_and_bad_responses_preserve_previous_valid_data(self):
        self.setup([fx.record()]); first,_=self.run_startup()
        variants=[dict(rows=manual.way(i=2,x=62,w=8)),dict(remark='out of memory'),dict(sentinel=False),
                  dict(replication='2020-01-01T00:00:00Z')]
        for kw in variants:
            self.now+=601; self.payload(**kw)
            snapshot,report=self.run_startup()
            self.assertFalse(report['checked']); self.assertEqual(snapshot.revision,first.revision)
            self.assertTrue(snapshot.enabled)

    def test_empty_response_is_not_a_deletion(self):
        self.setup([fx.record()]); first,_=self.run_startup()
        self.now+=601; self.payload(rows=[])
        snapshot,report=self.run_startup()
        # Known-ID completeness cannot certify a genuinely absent/deleted way.
        self.assertFalse(report['checked']); self.assertEqual(snapshot,first)

    def test_complete_empty_new_area_is_a_successful_check(self):
        self.setup(); self.payload(rows=[])
        snapshot,report=self.run_startup()
        self.assertTrue(report['checked']); self.assertFalse(report['accepted'])
        self.assertIsNone(snapshot.revision)

    def test_restart_cooldown_retry_after_and_clock_regression_are_durable(self):
        self.setup(); self.transport.status=429; self.transport.headers={'Retry-After':'1800'}
        _,report=self.run_startup(); self.assertFalse(report['checked'])
        self.assertEqual(len(self.transport.calls),1)
        self.client=osm.Client(ofx.CONFIG,fetcher=self.net,transport=self.transport,wall=lambda:self.now)
        self.now+=601; self.payload(); self.run_startup()
        self.assertEqual(len(self.transport.calls),1)
        self.now-=1200; self.run_startup(); self.assertEqual(len(self.transport.calls),1)
        self.now+=2401; self.transport.status=200; self.payload()
        _,report=self.run_startup(); self.assertTrue(report['checked'])

    def test_corrupt_or_missing_ledger_never_resets_network_permissions(self):
        self.setup(); first,_=self.run_startup()
        path=self.cache/'osm-startup/ledger.json'
        path.write_text('{broken')
        self.now+=601; self.payload()
        second,report=self.run_startup()
        self.assertEqual(second,first); self.assertEqual(report['error'],'CorruptStorage')
        self.assertEqual(len(self.transport.calls),1)
        path.rename(path.with_suffix('.retained'))
        self.run_startup(); self.assertEqual(len(self.transport.calls),1)

    def test_crash_reservation_precedes_http_and_atomic_write_failure(self):
        self.setup()
        with mock.patch.object(startup.State,'_atomic_write',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.run_startup()
        self.assertFalse(self.transport.calls); self.assertIsNone(self.store.load().revision)
        state=startup.State(self.cache)
        with state._locked():
            ledger=state.read(); coordinator=startup.Requests(state,ledger,wall=lambda:self.now,clock=lambda:self.now)
            with self.assertRaises(KeyboardInterrupt):
                with coordinator.request('osm-source.invalid',manual.bounds(),ofx.CONFIG): raise KeyboardInterrupt()
        self.now+=30; _,report=self.run_startup()
        self.assertEqual(report['areas'][0]['status'],'cooldown'); self.assertFalse(self.transport.calls)

    def test_disabled_cached_offline_rollback_and_reenable(self):
        self.setup([fx.record()]); first,_=self.run_startup()
        self.now+=601; self.payload(width=24); second,_=self.run_startup()
        self.store.disable(); frozen,report=self.run_startup()
        self.assertFalse(frozen.enabled); self.assertEqual(report['areas'][0]['status'],'disabled')
        self.assertEqual(len(self.transport.calls),2)
        self.store.rollback(); self.store.enable()
        restored,report=self.run_startup(config=replace(self.config,refresh_on_startup=False))
        self.assertEqual(restored.revision,first.revision)
        self.net.offline_flag=True
        offline,report=self.run_startup(); self.assertEqual(offline.revision,first.revision)
        self.assertEqual(report['areas'][0]['status'],'offline'); self.assertEqual(len(self.transport.calls),2)

    def test_cancel_after_fetch_never_accepts_and_does_not_bypass_cooldown(self):
        self.setup()
        _,report=self.run_startup(cancel=lambda:bool(self.transport.calls))
        self.assertFalse(report['checked']); self.assertIsNone(self.store.load().revision)
        self.assertEqual(report['areas'][0]['error'],'InterruptedError')
        self.run_startup(); self.assertEqual(len(self.transport.calls),1)

    def test_snapshot_survives_spawn_and_new_worker_after_store_mutation(self):
        self.setup([fx.record()]); frozen,_=self.run_startup()
        self.now+=601; self.payload(width=28); newer,_=self.run_startup()
        for _ in range(2):
            with ProcessPoolExecutor(1,mp_context=multiprocessing.get_context('spawn')) as pool:
                observed,versions=pool.submit(worker_probe,self.root/'spawn-cache',frozen).result(timeout=30)
                self.assertEqual(observed,frozen); self.assertNotEqual(observed.revision,newer.revision)
        self.assertEqual(self.store.load().revision,newer.revision)

    def test_baseline_release_change_or_invalid_original_fails_before_streaming(self):
        self.setup([fx.record()]); first,_=self.run_startup()
        self.base.release='changed-release'
        with self.assertRaises(OSError): self.run_startup(config=replace(self.config,refresh_on_startup=False))
        self.assertEqual(self.store.load().revision,first.revision)

    def test_quotas_preserve_saved_data_while_unchanged_checks_still_work(self):
        self.setup([fx.record()]); first,_=self.run_startup()
        self.store.limits=replace(self.store.limits,max_revisions=1)
        self.now+=601; self.payload(); snapshot,report=self.run_startup()
        self.assertTrue(report['checked']); self.assertEqual(snapshot.revision,first.revision)
        self.now+=601; self.payload(width=25); snapshot,report=self.run_startup()
        self.assertEqual(snapshot.revision,first.revision); self.assertEqual(report['areas'][0]['error'],'ResourceLimit')

    def test_saved_area_priority_spacing_and_overlapping_cooldowns(self):
        self.setup()
        other=manual.bounds(x=250,y=250,w=150,d=150)
        config=startup.Config((('a',manual.bounds()),('b',other)))
        self.run_startup(config=config)
        self.now+=61; self.payload(rows=[])
        _,report=self.run_startup(config=config)
        self.assertEqual(report['areas'][0]['name'],'b'); self.assertTrue(report['checked'])
        self.assertEqual(len(self.transport.calls),2)

    def test_configuration_authorization_limits_credentials_and_denied_endpoint(self):
        self.setup()
        p=self.root/'config.json'
        p.write_text(json.dumps(dict(version=1,authorized=True,endpoint_policy_confirmed=True,
            refresh_on_startup=True,areas=[dict(name='test',bounds=manual.bounds())])))
        self.assertEqual(startup.Config.read(p),self.config)
        p.write_text(p.read_text().replace('"authorized": true','"authorized": false'))
        with self.assertRaises(ValueError): startup.Config.read(p)
        with self.assertRaises(ValueError): startup.Config((('wide',manual.bounds(w=600)),))
        with self.assertRaises(ValueError): startup.retrieval_config({'BNGE_OSM_ENDPOINT':ofx.CONFIG.endpoint,'BNGE_OSM_SPACING_SECONDS':'1'})
        denied=replace(ofx.CONFIG,endpoint='https://overpass-api.de/api/interpreter',headers={'Authorization':'secret-value'})
        self.client=osm.Client(denied,fetcher=self.net,transport=self.transport,wall=lambda:self.now)
        _,report=self.run_startup()
        self.assertFalse(self.transport.calls)
        self.assertNotIn('secret-value',json.dumps(report))
        self.assertNotIn('secret-value',(self.cache/'osm-startup/ledger.json').read_text())

    def test_main_barrier_precedes_workers_listener_and_legacy_recovery_is_bypassed(self):
        code=Path(earthd.__file__).read_text()
        section=code[code.index('async def main_async'):]
        self.assertLess(section.index('buildingstartup.prepare('),section.index('initializer=blockworker.init'))
        self.assertLess(section.index('buildingstartup.prepare('),section.index('asyncio.start_server'))
        tree=ast.parse(code)
        calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='RespawningPool']
        maincalls=[n for n in calls if any(k.arg=='initializer' and ast.unparse(k.value)=='blockworker.init' for k in n.keywords)]
        self.assertEqual(len(maincalls),4)
        for call in maincalls:
            args=next(k.value for k in call.keywords if k.arg=='initargs')
            self.assertEqual(ast.unparse(args.elts[-1]),'frozen_buildings')
        server=NS(_building_startup={'mode':'startup'})
        self.assertEqual(earthd.Server.activate_building_overlays(server,None,None,manual=True)['error']['type'],'RestartRequired')
        with self.assertRaises(RuntimeError): earthd.Server.refresh_building_overlays(server)

    def test_game_surviving_companion_restart_is_rejected_without_releasing_resources(self):
        server=NS(sid='new-session')
        self.assertTrue(startup.fresh_connection(server,dict(sessions={})))
        self.assertTrue(startup.fresh_connection(server,dict(sessions=['new-session'],slotSession='new-session')))
        for message in (dict(sessions=['old-session']),dict(slotSession='old-session'),dict(sessions='bad')):
            self.assertFalse(startup.fresh_connection(server,message))

    def test_near_normal_191_312_and_194_332_ordinary_targets_generate_without_activation(self):
        self.setup([fx.record()]); frozen,_=self.run_startup(); md=self.view(frozen)
        frame=geodesy.EnuFrame(39.29008564698664,-76.5842096724785)
        bounds=(-76.5854615264661,39.29215733555047,-76.58279539789308,39.29337328507307)
        conn=NS(active=1)
        server=NS(connections=[conn],cache_dir=str(self.cache),near_visual='mesh',sidewalks=True,gas_stations=False,
            anchors={1:NS(id=1,lat=frame.lat0,lon=frame.lon0,frame=frame,zooms=(16,))},
            geo_rules=lambda *a:(False,True,0.,None,None,16),anchor_q=lambda a:.05)
        for scale,original_count,expanded_count in ((.6,191,194),(1.,312,332)):
            def near(radius,z): return {(int(x),int(y)) for x,y,_ in geotiles.tiles_near(frame,0,0,radius,z)}
            groups=near(1600*scale,geotiles.SKY_Z)
            far={tuple(map(int,k)) for x,y in groups for k in geotiles.children(x,y,geotiles.SKY_Z,geotiles.FAR_Z)}
            for name,keys in dict(roads=near(640,geotiles.Z),bldfar=far,bldsky=near(4800*scale,geotiles.SKY_Z)-groups).items():
                setattr(conn,name,NS(aid=1,geo=True,sent=keys,car=keys,empty=set(),building=set(),futs={},retry={},shown={}))
            original=buildingtargets.discover(server,bounds,max_targets=512,current_only=True)
            self.assertEqual(len(original.requests),original_count)
            def box(req):
                f,(x0,y0,x1,y1)=req.geometry()
                la,lo,_=f.enu_to_geodetic(__import__('numpy').array([x0,x1,x0,x1]),__import__('numpy').array([y0,y0,y1,y1]),0.)
                return float(lo.min()),float(la.min()),float(lo.max()),float(la.max())
            def cells(bounds):
                w,s,e,n=bounds; return set(mapcells.cells_for_bbox(s,w,n,e))
            universe=buildingtargets.discover(server,manual.bounds(-6000,-6000,12000,12000),max_targets=512,current_only=True)
            selected=[r for r in universe.requests if cells(box(r))&cells(bounds)]
            area=cells(startup._union(box(r) for r in selected))
            expanded=[r for r in universe.requests if cells(box(r))&area]
            self.assertEqual(len(expanded),expanded_count)
            with mock.patch.object(bw,'_s',regen.state(md,self.root/'ordinary-cache')):
                for i,req in enumerate(expanded):
                    for key in bw.building_cells(*req.geometry()):
                        md.cell(key,layers=('buildings','building_parts','roads','infrastructure','water'))
                    directory=self.root/f'normal-{scale}'/str(i); directory.mkdir(parents=True)
                    req=replace(req,options={'skin':False,'exact':None})
                    result=req.execute(str(directory),'/synthetic')
                    self.assertIsNotNone(result,(req.kind,req.x,req.y))
            self.assertFalse((self.cache/'building-generations').exists())

    def test_boundary_crossing_parent_parts_share_one_pinned_owner(self):
        self.setup()
        frame=geodesy.EnuFrame(fx.LAT,fx.CELL.bounds()[2])
        geometry=fx.polygon(-20,60,40,20,frame)
        rows=manual.way(i=2,geometry=geometry)+manual.way(i=10,geometry=fx.polygon(5,62,10,10,frame),
            tags={'building:part':'yes','height':'16'})
        rows.append(ofx.relation(100,[('way',2,'outline'),('way',10,'part')],{'type':'building'}))
        self.transport.payload=ofx.response(rows)
        self.config=startup.Config((('edge',manual.bounds(-80,0,160,160,frame)),))
        frozen,report=self.run_startup()
        self.assertTrue(report['accepted'],report)
        md=self.view(frozen)
        right=mapcells.CellKey(fx.CELL.z,fx.CELL.x+1,fx.CELL.y)
        a,b=md.building_cell(fx.CELL),md.building_cell(right)
        self.assertEqual(a.layers['buildings'].ids.tolist(),b.layers['buildings'].ids.tolist())
        self.assertEqual(int(a.layers['buildings'].own[0])+int(b.layers['buildings'].own[0]),1)
        self.assertEqual(a.layers['building_parts'].ids.tolist(),b.layers['building_parts'].ids.tolist())
        __import__('numpy').testing.assert_array_equal(a.layers['building_parts'].lonlat,b.layers['building_parts'].lonlat)
        import buildings
        left=buildings.road_tile_items(frame,-1,0,md,fx.ground)
        right_items=buildings.road_tile_items(frame,0,0,md,fx.ground)
        self.assertTrue(left or right_items)

    def test_disable_empty_store_persists_and_enable_allows_first_refresh(self):
        self.setup()
        startup.control(self.cache,'disable',store=self.store)
        frozen,report=self.run_startup()
        self.assertEqual(report['areas'][0]['status'],'disabled'); self.assertFalse(self.transport.calls)
        startup.control(self.cache,'enable',store=self.store)
        frozen,report=self.run_startup()
        self.assertTrue(report['accepted']); self.assertTrue(frozen.enabled)

    def test_failed_overlay_publication_preserves_prior_revision_and_old_session(self):
        self.setup([fx.record()]); first,_=self.run_startup(); running=self.view(first)
        key=bw.building_versions(running,fx.FRAME,(0,0,256,256))
        original=self.store._atomic_write
        def fail_manifest(path,data):
            if path.name=='state.json': raise OSError('synthetic disk full before replacement')
            return original(path,data)
        self.now+=601; self.payload(width=30)
        with mock.patch.object(self.store,'_atomic_write',side_effect=fail_manifest):
            frozen,report=self.run_startup()
        self.assertFalse(report['checked']); self.assertEqual(frozen,first)
        self.assertEqual(key,bw.building_versions(running,fx.FRAME,(0,0,256,256)))

    def test_concurrent_startups_serialize_network_and_preserve_valid_head(self):
        self.setup(); arrived=threading.Event(); release=threading.Event()
        post=self.transport.post
        def hold(*args,**kw):
            arrived.set()
            if not release.wait(5): raise TimeoutError('test did not release HTTP')
            return post(*args,**kw)
        with mock.patch.object(self.transport,'post',side_effect=hold),ThreadPoolExecutor(1) as pool:
            future=pool.submit(self.run_startup)
            try:
                self.assertTrue(arrived.wait(5))
                with self.assertRaises(OSError): self.run_startup()
                self.assertFalse(self.transport.calls)
            finally: release.set()
            frozen,report=future.result(timeout=10)
        self.assertTrue(report['accepted']); self.assertEqual(self.store.load(),frozen)
        self.assertEqual(len(self.transport.calls),1)

    def test_checked_timestamp_of_empty_area_rejects_older_snapshot_after_restart(self):
        self.setup(); self.payload(rows=[])
        self.run_startup()
        self.now+=601; self.payload(rows=[],replication='2026-10-30T11:59:30Z')
        _,report=self.run_startup()
        self.assertFalse(report['checked']); self.assertEqual(report['areas'][0]['error'],'StaleSnapshot')

    def test_height_only_change_updates_keys_and_real_mesh_with_same_footprint(self):
        self.setup([fx.record()]); a,_=self.run_startup()
        self.now+=601
        self.payload(rows=manual.way(w=18,tags={'building':'retail','height':'30','roof:shape':'flat'}))
        b,report=self.run_startup()
        self.assertTrue(report['accepted'])
        da,db=self.view(a),self.view(b)
        ga,gb=da.building_dependencies((fx.CELL,)),db.building_dependencies((fx.CELL,))
        self.assertEqual(ga.geometry_digest,gb.geometry_digest); self.assertNotEqual(ga.revision,gb.revision)
        self.assertNotEqual(self.native_far(da,self.root/'height-a')[1],self.native_far(db,self.root/'height-b')[1])

    def test_startup_summary_is_visible_in_normal_launcher(self):
        sys.path.insert(0,str(fx.REPO))
        import launcher
        line='2026-11-01 00:00:00,000 INFO earthd.osm_startup: OSM startup: {"checked": false, "freshness": "not_confirmed"}'
        shown=launcher.echo_lines([line],{})
        self.assertEqual(len(shown),1); self.assertIn('not_confirmed',shown[0])

    def test_real_main_startup_selects_snapshot_before_first_pool_is_constructed(self):
        self.setup([fx.record()]); captured=[]
        class StopBeforeWorkers(Exception): pass
        def pool(*args,**kw):
            captured.append(kw['initargs'][-1])
            self.assertEqual(self.base._building_view.snapshot,captured[-1])
            raise StopBeforeWorkers()
        args=NS(far_format='dae',lod_k=None,near_radii=None,cache_dir=str(self.cache),offline=False,
            cache_cap=1.,surface='off',roads='on',sea='off',trees='off',buildings='on',background_duty=.5,
            detail_ring=500.,download_ahead='off',route_prefetch='off',route_prefetch_km=1.,block_process='on',
            grading='off',road_widths='off',airports='off',gas_stations='off')
        prepare=startup.prepare
        def startup_barrier(md,cache,config):
            self.assertFalse(captured)
            return prepare(md,cache,config,client=self.client,store=self.store,wall=lambda:self.now,clock=lambda:self.now)
        with mock.patch.dict(os.environ,{'BNGE_OSM_STARTUP_CONFIG':'authorized-fixture'}), \
             mock.patch.object(startup.Config,'read',return_value=self.config), \
             mock.patch.object(startup,'prepare',side_effect=startup_barrier), \
             mock.patch.object(earthd.netfetch,'Fetcher',return_value=self.net), \
             mock.patch.object(earthd.cachemod,'Cache'), \
             mock.patch.object(earthd,'mapdatamod',NS(MapData=mock.Mock(return_value=self.base))), \
             mock.patch.object(earthd,'prune_own_caches'), \
             mock.patch.object(earthd.plancachemod,'start_run'), \
             mock.patch.object(earthd.pacer,'DUTY',earthd.pacer.DUTY), \
             mock.patch.multiple(earthd,DETAIL_RING_M=earthd.DETAIL_RING_M,AHEAD_ON=earthd.AHEAD_ON,
                 ROUTE_AHEAD_ON=earthd.ROUTE_AHEAD_ON,ROUTE_AHEAD_MAX_M=earthd.ROUTE_AHEAD_MAX_M), \
             mock.patch.object(earthd.poolsmod,'RespawningPool',side_effect=pool):
            with self.assertRaises(StopBeforeWorkers): asyncio.run(earthd.main_async(args))
        self.assertEqual(len(captured),1); self.assertTrue(captured[0].enabled)
        self.assertEqual(len(self.transport.calls),1)

    def test_ordinary_geographic_near_far_skyline_write_current_geometry_each_session(self):
        self.setup([fx.record()]); a,_=self.run_startup()
        self.now+=601; self.payload(width=24); b,_=self.run_startup()
        center=fx.FRAME.enu_to_geodetic(65,65,0.)
        outputs=[]
        for index,snapshot in enumerate((a,b)):
            md=self.view(snapshot); meshes={}; stable={}
            with mock.patch.object(bw,'_s',regen.state(md,self.root/'ordinary-cache')):
                for kind,z in (('near_geo',17),('far_geo',17),('skyline_geo',15)):
                    x,y=geotiles.tile_of(float(center[0]),float(center[1]),z)
                    req=buildingrefresh.TileRequest(kind,x,y,lat=fx.LAT,lon=fx.LON,options={'skin':False,'exact':None})
                    for key in bw.building_cells(*req.geometry()):
                        md.cell(key,layers=('buildings','building_parts','roads','infrastructure','water'))
                    directory=self.root/f'lod-session-{index}'/kind; directory.mkdir(parents=True)
                    self.assertIsNotNone(req.execute(str(directory),'/synthetic'))
                    shapes={p.name:p.read_bytes() for p in directory.glob('*.cdae')}
                    building_shapes={k:v for k,v in shapes.items() if kind!='near_geo' or 'bld' in k}
                    self.assertTrue(building_shapes,kind)
                    meshes[kind]=building_shapes
                    stable[kind]={k:v for k,v in shapes.items() if kind=='near_geo' and 'bld' not in k}
            outputs.append((meshes,stable))
        for kind in outputs[0][0]: self.assertNotEqual(outputs[0][0][kind],outputs[1][0][kind],kind)
        self.assertEqual(outputs[0][1],outputs[1][1])

    def test_snapshot_expiring_during_baseline_validation_is_not_accepted(self):
        self.setup([fx.record()])
        context=startup.context
        calls=[]
        def slow_context(*a,**kw):
            result=context(*a,**kw); calls.append(1)
            if len(calls)==2: self.now+=4000
            return result
        with mock.patch.object(startup,'context',side_effect=slow_context):
            snapshot,report=self.run_startup()
        self.assertFalse(report['checked']); self.assertIsNone(snapshot.revision)
        self.assertEqual(report['areas'][0]['error'],'StaleSnapshot')


if __name__=='__main__': unittest.main()
