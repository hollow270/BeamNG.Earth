"""OSM-only source/storage/session/render contracts. No game or external HTTP."""
from dataclasses import replace
from functools import partial
import json,multiprocessing as mp,os,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import buildingacquire as acquire
import buildingoverlay as geom
import buildingoverlaystore as fs
import buildingworld as world
import buildings,geotiles,mapcells,mapdata,netfetch
import osmbuildingcache as cache
import layercakebuildings as lc
import test_buildingprimary as sample
import test_buildingrender as fx


def source_features():
    return lc.normalize([sample.row()],fx.CELL.bounds(),sample.VERSION).buildings


class Reader:
    def __init__(self,features=(),fail=False,version=None):
        self.version=version or sample.VERSION
        self.features=tuple(replace(f,sources=tuple(dict(s,dataset_etag=self.version['etag']) for s in f.sources)) for f in features)
        self.fail=fail;self.calls=0
    def select(self,**kw):
        if self.fail:raise OSError('source unavailable')
        return self.version
    def attach(self,v):assert v==self.version
    def close(self):pass
    def query(self,bounds):
        self.calls+=1
        if self.fail:raise OSError('source unavailable')
        got=tuple(f for f in self.features if fs._intersects(geom._bbox(f.geometry),bounds))
        return lc.Snapshot(tuple(bounds),tuple(f for f in got if f.parent_id is None),tuple(f for f in got if f.parent_id is not None),self.version)


def task(request,emit,stall=False,near_deadline=False,started=None):
    md=NS(cache_dir=request['cache'],fetcher=NS(offline_flag=False))
    p=world.Provider(md,request['session'],reader_factory=lambda:Reader(source_features()))
    p._pin_source(sample.VERSION)
    if started is not None:started.set()
    if stall:emit(dict(phase='stalled'));time.sleep(120)
    if near_deadline:
        time.sleep(2.);emit(dict(phase='parquet_staged'));time.sleep(2.5)
    return p._acquire(request['key'],p._state(),progress=emit,token=request['token'])


def caller(directory,descriptor,key,kind,out,started=None):
    world.READ_BUDGET_S=5.
    p=world.Provider(NS(cache_dir=directory,fetcher=NS(offline_flag=False)),descriptor,
                     runner=acquire.Runner(partial(task,stall=kind=='stall',started=started)))
    tick=time.monotonic()
    try:
        v=p.view((key,));out.put(dict(kind=kind,seconds=time.monotonic()-tick,count=len(v.snapshot.entries),errors=v.report['errors']))
    except fs.LockTimeout:out.put(dict(kind=kind,seconds=time.monotonic()-tick,deferred=True))
    except BaseException as e:out.put(dict(kind=kind,error=repr(e)))


def late_publication(request,emit):
    p=world.Provider(NS(cache_dir=request['cache'],fetcher=NS(offline_flag=False)),request['session'])
    p._pin_source(sample.VERSION);emit(dict(phase='parquet_staged'))
    time.sleep(3.)
    snapshot,report=cache.normalize((lc.Snapshot(request['key'].bounds(),source_features(),(),sample.VERSION),))
    emit(dict(phase='validated',entries=len(snapshot.entries)))
    time.sleep(2.)
    return p._publish(request['key'],request['token'],snapshot,sample.VERSION,report)


class OSMOnlyTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parents[2],prefix='.osmonly-')
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.md=fx.FixtureMapData(self.root/'cache',originals=(fx.record('overture-sentinel'),))
        self.addCleanup(self.md.close);self.addCleanup(self.md.fetcher.close)
        world.enable(self.md.cache_dir,policy_confirmed=True)

    def provider(self,features=(),fail=False):
        self.md.fetcher.offline_flag=False
        descriptor=world.start(self.md.cache_dir)
        reader=Reader(features,fail)
        return world.Provider(self.md,descriptor,reader_factory=lambda:reader),reader

    def pin(self,provider):self.md.pin_buildings(provider.session);self.md._building_world=provider

    def test_empty_source_never_returns_overture_and_versions_empty_mesh(self):
        p,r=self.provider();self.pin(p)
        self.assertEqual([f.feature_id for f in self.md.originals],['overture-sentinel'])
        effective=self.md.building_cell(fx.CELL).layers['buildings']
        self.assertEqual(len(effective),0)
        self.assertIsNotNone(self.md.building_dependencies((fx.CELL,)).revision)

    def test_brand_new_application_cache_acquires_without_enable_or_city_preparation(self):
        from types import SimpleNamespace
        directory=self.root/'new-user-cache'
        descriptor=world.start(directory)
        self.assertTrue(descriptor.enabled)
        p=world.Provider(SimpleNamespace(cache_dir=str(directory),fetcher=SimpleNamespace(offline_flag=False)),
                         descriptor,reader_factory=lambda:Reader(source_features()))
        self.assertTrue(p.view((fx.CELL,)).snapshot.entries)

    def test_unavailable_first_visit_is_frozen_empty_not_baseline_or_retry(self):
        p,r=self.provider(fail=True);v=p.view((fx.CELL,));calls=r.calls
        self.assertFalse(v.snapshot.entries);self.assertTrue(v.report['errors'])
        p.view((fx.CELL,));self.assertEqual(r.calls,calls)
        self.assertNotIn(str(fx.CELL),p.store.index()['cells'])
        self.assertIn('unavailable',p.note())

    def test_osm_geometry_attributes_and_near_far_collision_use_existing_generator(self):
        f=source_features()[0];p,r=self.provider((f,));self.pin(p)
        original=self.md.cell
        def guarded(key,*,layers,**kw):
            self.assertFalse(set(layers)&{'buildings','building_parts'})
            return original(key,layers=layers,**kw)
        with mock.patch.object(self.md,'cell',side_effect=guarded):
            cd=self.md.building_cell(fx.CELL)
        layer=cd.layers['buildings'];self.assertEqual(layer.ids.tolist(),[f.feature_id])
        items=buildings.tile_buildings(fx.FRAME,0,0,256,256,[layer],fx.ground)
        self.assertTrue(items);self.assertIsNotNone(buildings.build(items));self.assertIsNotNone(buildings.build(items,lite=True))
        self.assertTrue(buildings.collider_boxes(items,0,0))
        self.assertEqual(layer.attrs['src_dataset'].tolist(),['OpenStreetMap'])

    def test_real_agadir_reference_survives_normalize_store_select_and_meshes(self):
        raw=dict(sample.FIXTURE['row']);geometry=raw['geometry'];raw['geometry']=sample.wkb(geometry)
        key=mapcells.cell_of(30.412876,-9.5524592)
        result=lc.normalize([raw],key.bounds(),sample.VERSION)
        p,r=self.provider(result.buildings);self.pin(p)
        effective=self.md.building_cell(key).layers['buildings']
        self.assertEqual(effective.ids.tolist(),['osm:way:1572086438'])
        persisted=p.store.object(p.store.index()['cells'][str(key)])[0].entries[0]
        self.assertEqual(geom.geometry_digest(persisted.feature.geometry),geom.geometry_digest(geometry))
        self.assertIsNone(persisted.baseline_digest)
        self.assertEqual(effective.attrs['num_floors'].tolist(),[3.])
        frame=fx.geodesy.EnuFrame(30.4119,-9.5552,0.)
        items=buildings.tile_buildings(frame,0,0,512,512,[effective],fx.ground)
        self.assertEqual(len(items),1)
        self.assertIsNotNone(buildings.build(items));self.assertIsNotNone(buildings.build(items,lite=True))
        self.assertIsNotNone(buildings.collision_mesh(items));self.assertTrue(buildings.collider_boxes(items,0,0))

    def test_cached_restart_and_offline_revisit_do_not_construct_source_reader(self):
        p,r=self.provider(source_features());first=p.view((fx.CELL,)).snapshot
        restarted=world.Provider(self.md,world.start(self.md.cache_dir),reader_factory=lambda:(_ for _ in ()).throw(AssertionError('freshness request')))
        self.md.fetcher.offline_flag=True
        self.assertEqual(restarted.view((fx.CELL,)).snapshot.entries,first.entries)

    def test_cross_cell_footprint_is_not_suppressed_by_neighbor_choice(self):
        a=fx.CELL;b=mapcells.CellKey(a.z,a.x+1,a.y)
        x=a.bounds()[2];y=sum(a.bounds()[1::2])/2;d=.00005
        f=replace(source_features()[0],geometry=dict(type='Polygon',coordinates=[[[x-d,y-d],[x+d,y-d],[x+d,y+d],[x-d,y+d],[x-d,y-d]]]))
        p,r=self.provider((f,));one=p.view((a,));two=p.view((b,))
        self.assertEqual([e.feature.feature_id for e in one.snapshot.entries],[f.feature_id])
        self.assertEqual(two.snapshot.entries,one.snapshot.entries)
        self.assertEqual(len(p.view((a,b)).snapshot.entries),1)

    def test_bad_and_unsupported_geometry_reported_without_substitution(self):
        f=source_features()[0];hole=replace(f,geometry=dict(type='Polygon',coordinates=f.geometry['coordinates']*2))
        p,r=self.provider((hole,));v=p.view((fx.CELL,))
        self.assertFalse(v.snapshot.entries);self.assertIn('unsupported_holes',str(v.report))

    def test_bad_height_is_reported_without_losing_valid_neighbor(self):
        f=source_features()[0]
        bad=replace(f,feature_id='osm:way:2',osm_ids=('osm:way:2',),attrs={'height':1e12},
                    sources=tuple(dict(s,record_id='w2@2') for s in f.sources))
        p,r=self.provider((f,bad));v=p.view((fx.CELL,))
        self.assertEqual([e.feature.feature_id for e in v.snapshot.entries],[f.feature_id])
        self.assertIn('unsupported_attribute_extent:height',str(v.report))

    def test_parent_and_parts_remain_one_immutable_osm_group(self):
        parent=source_features()[0]
        part=replace(parent,feature_id='osm:way:2',osm_ids=('osm:way:2',),parent_id=parent.feature_id,
                     attrs={'height':8},sources=tuple(dict(s,record_id='w2@2') for s in parent.sources))
        p,r=self.provider((parent,part));self.pin(p)
        cd=self.md.building_cell(fx.CELL)
        self.assertEqual(cd.layers['building_parts'].ids.tolist(),['osm:way:2'])
        self.assertEqual(cd.layers['building_parts'].attrs['building_id'].tolist(),[parent.feature_id])
        self.assertTrue(cd.layers['buildings'].attrs['has_parts'][0])

    def test_missing_height_uses_generator_defaults_without_other_sources(self):
        p,r=self.provider(source_features());self.pin(p)
        layer=self.md.building_cell(fx.CELL).layers['buildings']
        items=buildings.tile_buildings(fx.FRAME,0,0,256,256,[layer],fx.ground)
        self.assertTrue(items);self.assertGreater(items[0][0].height,0)
        self.assertEqual(layer.attrs['height_src'].tolist(),['OpenStreetMap'])

    def test_other_infrastructure_cannot_override_osm_building_geometry_or_attributes(self):
        p,r=self.provider(source_features());self.pin(p)
        layer=self.md.building_cell(fx.CELL).layers['buildings']
        plain=buildings.tile_buildings(fx.FRAME,0,0,256,256,[layer],fx.ground)
        with mock.patch.object(buildings,'_covers_garage',side_effect=AssertionError('non-OSM garage override')), \
             mock.patch.object(buildings,'_passages',side_effect=AssertionError('non-OSM footprint subtraction')):
            contextual=buildings.tile_buildings(fx.FRAME,0,0,256,256,[layer],fx.ground,
                roads_xy=[object()],garages_xy=[object()],bridges_xy=[object()],decks=[object()])
        self.assertEqual(fx.mesh_fingerprint(plain),fx.mesh_fingerprint(contextual))

    def test_unavailable_neighbor_reuses_known_full_osm_footprint(self):
        a=fx.CELL;b=mapcells.CellKey(a.z,a.x+1,a.y);x=a.bounds()[2];y=sum(a.bounds()[1::2])/2;d=.00005
        f=replace(source_features()[0],geometry=dict(type='Polygon',coordinates=[[[x-d,y-d],[x+d,y-d],[x+d,y+d],[x-d,y+d],[x-d,y-d]]]))
        p,r=self.provider((f,));first=p.view((a,))
        other,reader=self.provider(fail=True);second=other.view((b,))
        self.assertEqual(second.snapshot.entries,first.snapshot.entries)
        self.assertEqual(second.report['cells'][0]['mode'],'osm_cached_partial')

    def test_new_neighbor_cannot_implicitly_refresh_previously_cached_osm_identity(self):
        a=fx.CELL;b=mapcells.CellKey(a.z,a.x+1,a.y);x=a.bounds()[2];y=sum(a.bounds()[1::2])/2;d=.00005
        f=replace(source_features()[0],geometry=dict(type='Polygon',coordinates=[[[x-d,y-d],[x+d,y-d],[x+d,y+d],[x-d,y+d],[x-d,y-d]]]),attrs={'height':8})
        p,r=self.provider((f,));p.view((a,))
        newer=replace(f,attrs={'height':25})
        self.md.fetcher.offline_flag=False
        other=world.Provider(self.md,world.start(self.md.cache_dir),
            reader_factory=lambda:Reader((newer,),version=dict(sample.VERSION,etag='"newer-first-visit"')))
        self.assertEqual(other.view((b,)).snapshot.entries[0].feature.attrs['height'],8)
        restarted,r=self.provider()
        self.assertEqual(restarted.view((b,a)).snapshot.entries[0].feature.attrs['height'],8)

    def test_skyline_does_not_wait_for_or_prefetch_overture_buildings(self):
        p,r=self.provider(source_features());self.pin(p)
        original_has=self.md.has
        def has(key,layer):
            self.assertNotIn(layer,('buildings','building_parts'))
            return True
        with mock.patch.object(self.md,'has',side_effect=has):
            result=buildings.sky_items(fx.FRAME,(0,0,256,256),(fx.CELL,),self.md,fx.ground)
        self.assertIsNotNone(result)

    def test_capacity_error_does_not_replace_cached_osm_or_enable_baseline(self):
        p,r=self.provider(source_features());old=p.view((fx.CELL,)).snapshot
        unknown=mapcells.CellKey(fx.CELL.z,fx.CELL.x+30,fx.CELL.y)
        other,reader=self.provider()
        with mock.patch.object(cache,'MAX_OBJECTS',0):
            with self.assertRaises(fs.ResourceLimit):other.view((unknown,))
        self.assertEqual(p.view((fx.CELL,)).snapshot.entries,old.entries)

    def test_download_cache_clear_does_not_delete_osm_snapshots_or_session_choices(self):
        import cache as download_cache
        p,r=self.provider(source_features());old=p.view((fx.CELL,)).snapshot
        disk=download_cache.Cache(self.md.cache_dir,start=False)
        try:disk.scan();disk.clear()
        finally:disk.close()
        self.assertEqual(p.view((fx.CELL,)).snapshot.entries,old.entries)

    def test_refresh_expands_cached_copies_of_cross_cell_groups(self):
        a=fx.CELL;b=mapcells.CellKey(a.z,a.x+1,a.y);x=a.bounds()[2];y=sum(a.bounds()[1::2])/2;d=.00005
        f=replace(source_features()[0],geometry=dict(type='Polygon',coordinates=[[[x-d,y-d],[x+d,y-d],[x+d,y+d],[x-d,y+d],[x-d,y-d]]]))
        p,r=self.provider((f,));p.view((a,b))
        updated=replace(f,attrs={'height':28})
        report=world.refresh(self.md.cache_dir,(a,),self.md.fetcher,
            reader_factory=lambda:Reader((updated,),version=dict(sample.VERSION,etag='"cross-update"')))
        self.assertEqual(report['cells'],2);self.assertTrue(report['checked'])
        new,r=self.provider();self.assertEqual(new.view((b,a)).snapshot.entries[0].feature.attrs['height'],28)

    def test_non_osm_persistent_source_and_legacy_matched_ids_rejected(self):
        f=source_features()[0]
        for changed in (replace(f,feature_id='gers-id'),replace(f,sources=({'dataset':'Microsoft'},))):
            result=lc.Snapshot(fx.CELL.bounds(),(changed,),(),sample.VERSION)
            with self.assertRaises(ValueError):cache.normalize((result,))
        from osmbuildingview import View
        legacy=fx.record('gers-forged')
        entry=fs.Entry(legacy,(),None,geom._bbox(legacy.geometry),'2026-11-01T14:35:59Z')
        with self.assertRaises(ValueError):View(replace(cache.empty(),entries=(entry,)))

    def test_active_acquisition_and_rendering_never_call_legacy_merge_or_baseline_validator(self):
        import buildingbaseline,buildingprimary
        p,r=self.provider(source_features());self.pin(p)
        with mock.patch.object(geom,'merge',side_effect=AssertionError('legacy merge')), \
             mock.patch.object(buildingbaseline,'collect',side_effect=AssertionError('baseline lookup')), \
             mock.patch.object(buildingprimary,'prepare',side_effect=AssertionError('baseline preparation')):
            self.assertEqual(self.md.building_cell(fx.CELL).layers['buildings'].ids.tolist(),['osm:way:1'])

    def test_manual_update_replaces_complete_cell_in_new_session_and_rollback(self):
        p,r=self.provider(source_features());first=p.view((fx.CELL,)).snapshot
        f=replace(source_features()[0],attrs={'height':25})
        report=world.refresh(self.md.cache_dir,(fx.CELL,),self.md.fetcher,reader_factory=lambda:Reader((f,),version=dict(sample.VERSION,etag='"updated"')))
        self.assertTrue(report['checked'])
        self.assertEqual(p.view((fx.CELL,)).snapshot.entries,first.entries)
        new,r=self.provider();self.assertEqual(new.view((fx.CELL,)).snapshot.entries[0].feature.attrs['height'],25)
        world.rollback(self.md.cache_dir)
        old,r=self.provider();self.assertEqual(old.view((fx.CELL,)).snapshot.entries,first.entries)

    def test_complete_empty_manual_snapshot_removes_osm_buildings_in_area(self):
        p,r=self.provider(source_features());p.view((fx.CELL,))
        report=world.refresh(self.md.cache_dir,(fx.CELL,),self.md.fetcher,reader_factory=lambda:Reader(version=dict(sample.VERSION,etag='"empty-new"')))
        self.assertTrue(report['checked']);new,r=self.provider()
        self.assertFalse(new.view((fx.CELL,)).snapshot.entries)

    def test_manual_failure_retains_saved_osm(self):
        p,r=self.provider(source_features());old=p.view((fx.CELL,)).snapshot
        report=world.refresh(self.md.cache_dir,(fx.CELL,),self.md.fetcher,reader_factory=lambda:Reader(fail=True))
        self.assertFalse(report['checked']);new,r=self.provider();self.assertEqual(new.view((fx.CELL,)).snapshot.entries,old.entries)

    def test_original_road_records_and_terrain_source_versions_are_unchanged(self):
        p,r=self.provider(source_features());before=self.md.versions();baseline=self.md.cell(fx.CELL,layers=('roads',)).digest()
        self.pin(p);self.md.building_cell(fx.CELL,layers=('buildings','roads'))
        self.assertEqual(self.md.versions(),before)
        self.assertEqual(self.md.cell(fx.CELL,layers=('roads',)).digest(),baseline)

    def test_empty_osm_also_removes_building_dependent_road_context(self):
        p,r=self.provider();self.pin(p)
        rendered,road_context=buildings.road_context_items(fx.FRAME,0,0,self.md,fx.ground)
        self.assertFalse(rendered)
        self.assertIs(road_context,rendered)
        self.assertFalse(buildings.centres(road_context,0,0))
        self.assertEqual(buildings.coverage(road_context),0.)

    def test_road_context_uses_the_same_osm_specs_without_a_second_generation(self):
        p,r=self.provider(source_features());self.pin(p)
        original=buildings.road_tile_items
        with mock.patch.object(buildings,'road_tile_items',wraps=original) as generate:
            rendered,context=buildings.road_context_items(fx.FRAME,0,0,self.md,fx.ground)
        self.assertEqual(generate.call_count,1);self.assertTrue(context);self.assertIs(context,rendered)
        self.assertEqual([s.seed for s,_,_ in context],[s.seed for s,_,_ in rendered])

    def test_every_cell_entrypoint_ignores_retained_overture_building_files(self):
        # A retained baseline file must never be served, including low-level warm /
        # saved-area access. Keep the sentinel to catch accidental cache reads.
        self.md._store(self.md.path(fx.CELL,'buildings'),self.md._build(fx.CELL,'buildings','demand'))
        p,r=self.provider(source_features());self.pin(p)
        build=self.md._build
        def only_other_sources(key,layer,kind):
            self.assertNotIn(layer,('buildings','building_parts'))
            return build(key,layer,kind)
        with mock.patch.object(self.md,'_build',side_effect=only_other_sources), \
             mock.patch.object(self.md.overture,'query',side_effect=AssertionError('Overture building query')):
            self.assertFalse(self.md.has(fx.CELL,'buildings'),'a retained baseline file is not OSM readiness')
            self.assertEqual(self.md.warm(((fx.CELL,'buildings'),)),1)
            cd=self.md.cell(fx.CELL,layers=('buildings','building_parts','roads'))
            self.assertEqual(cd.layers['buildings'].ids.tolist(),['osm:way:1'])
            arrays,_=self.md.layer_arrays(fx.CELL,'buildings')
            self.assertEqual(fx.mg.Features.from_arrays('buildings',arrays,'').ids.tolist(),['osm:way:1'])
            self.assertTrue(self.md.prefetch(fx.CELL,layers=('buildings','building_parts')))
            self.assertFalse(self.md.predownload(fx.CELL,'buildings'))
        with self.assertRaises(ValueError):mapdata.MapData._build(self.md,fx.CELL,'buildings','demand')

    def test_production_mapdata_never_queries_overture_buildings(self):
        fetch=netfetch.Fetcher(offline=True,transport=object());self.addCleanup(fetch.close)
        md=mapdata.MapData(str(self.root/'production'),fetcher=fetch);self.addCleanup(md.close)
        snapshot,_=cache.normalize((lc.Snapshot(fx.CELL.bounds(),source_features(),(),sample.VERSION),))
        md.pin_buildings(snapshot)
        with mock.patch.object(md.overture,'query',side_effect=AssertionError('Overture building query')):
            self.assertEqual(md.cell(fx.CELL,layers=('buildings',)).layers['buildings'].ids.tolist(),['osm:way:1'])
            self.assertEqual(md.cell_at(*fx.CELL.center(),layers=('buildings',)).layers['buildings'].ids.tolist(),['osm:way:1'])
            self.assertTrue(md.prefetch(fx.CELL,layers=('buildings','building_parts')))
            self.assertEqual(md.layer_provenance(fx.CELL,'buildings')['id'],'osm-buildings')

    def test_photo_mask_and_legacy_effective_flag_cannot_bypass_osm(self):
        p,r=self.provider();self.pin(p)
        self.assertIsNone(buildings.photo_mask(fx.FRAME,0,0,256,32,self.md))
        self.assertFalse(buildings.road_tile_items(fx.FRAME,0,0,self.md,fx.ground,effective=False))
        world.refresh(self.md.cache_dir,(fx.CELL,),self.md.fetcher,
                      reader_factory=lambda:Reader(source_features(),version=dict(sample.VERSION,etag='\"context-update\"')))
        p,r=self.provider(source_features());self.md._building_world=p
        self.assertTrue(buildings.photo_mask(fx.FRAME,0,0,256,32,self.md).any())

    def test_width_wall_cache_uses_osm_revision_and_never_reads_legacy_walls(self):
        import roadwidth
        legacy=self.root/'walls';legacy_path=legacy/str(fx.CELL.z)/str(fx.CELL.x)/(str(fx.CELL.y)+'.npy')
        roadwidth._write_segs(str(legacy_path),fx.np.array([[0,0,100,100]],fx.np.float32))
        p,r=self.provider();self.pin(p)
        def widths():return roadwidth.RoadWidths(lambda key:self.md.cell(key,layers=('buildings',)).layers['buildings'],
                    str(legacy),dependency=lambda key:self.md.building_dependencies((key,)).geometry_digest)
        self.assertEqual(len(widths()._walls(fx.CELL)[1].segs),0)
        world.refresh(self.md.cache_dir,(fx.CELL,),self.md.fetcher,
                      reader_factory=lambda:Reader(source_features(),version=dict(sample.VERSION,etag='\"context-update\"')))
        p,r=self.provider(source_features());self.md._building_world=p
        self.assertGreater(len(widths()._walls(fx.CELL)[1].segs),0)
        self.assertGreater(len(list(legacy.rglob('*.npy'))),1)

    def test_context_plan_keys_are_local_revision_bound(self):
        import plancache
        self.md.street=True
        p,r=self.provider();self.pin(p)
        def plan_cache():
            self.md.prepare_building_context((fx.CELL,))
            plans=plancache.PlanCache(str(self.root/'plans'),'w1s0')
            plans.building_context=self.md.building_context_revision
            plans.persist=(str(self.root/'cache'),('frames','w1s0',self.md.versions()))
            return plans
        a=plan_cache();path=a._path(tuple(fx.CELL));key=a._revisit_key(tuple(fx.CELL))
        world.refresh(self.md.cache_dir,(fx.CELL,),self.md.fetcher,
                      reader_factory=lambda:Reader(source_features(),version=dict(sample.VERSION,etag='\"context-update\"')))
        p,r=self.provider(source_features());self.md._building_world=p
        b=plan_cache();self.assertNotEqual(path,b._path(tuple(fx.CELL)))
        self.assertNotEqual(key,b._revisit_key(tuple(fx.CELL)))
        self.assertEqual(b._path(tuple(fx.CELL)),plan_cache()._path(tuple(fx.CELL)))

    def test_context_semantics_exclude_old_derived_artifacts_without_changing_sources(self):
        versions=self.md.versions();self.assertEqual(versions['building_context'],'osm-only-context-v1')
        self.assertEqual(versions['overture'],fx.RELEASE)

    def test_vegetation_cached_masks_use_osm_context_and_ready_never_fetches(self):
        import vegetation
        p,r=self.provider();self.pin(p)
        plant=vegetation.Vegetation(self.md,str(self.root/'plants'))
        with mock.patch.object(self.md,'building_context_revision',side_effect=AssertionError('event-loop fetch')):
            self.assertFalse(plant.ready(tuple(fx.CELL)))
        def prepared_path():
            keys=[mapcells.CellKey(fx.CELL.z,fx.CELL.x+dx,fx.CELL.y+dy) for dx in (-1,0,1) for dy in (-1,0,1)]
            self.md.prepare_buildings(keys);self.md.prepare_building_context(keys)
            return plant._path(fx.CELL.x,fx.CELL.y)
        a=prepared_path()
        world.refresh(self.md.cache_dir,(fx.CELL,),self.md.fetcher,
                      reader_factory=lambda:Reader(source_features(),version=dict(sample.VERSION,etag='\"context-update\"')))
        p,r=self.provider(source_features());self.md._building_world=p
        b=prepared_path()
        self.assertNotEqual(a,b)

    def test_validated_result_after_download_budget_gets_publication_phase(self):
        descriptor=world.start(self.md.cache_dir);self.md.fetcher.offline_flag=False
        p=world.Provider(self.md,descriptor,runner=acquire.Runner(partial(task,near_deadline=True)))
        with mock.patch.multiple(world,READ_BUDGET_S=12.,DOWNLOAD_S=4.,PROCESS_S=4.,PUBLICATION_S=2.):
            tick=time.monotonic();v=p.view((fx.CELL,))
        self.assertGreater(time.monotonic()-tick,4.)
        self.assertTrue(v.snapshot.entries)

    def test_validated_result_receives_bounded_grace_after_aggregate_deadline(self):
        self.md.fetcher.offline_flag=False
        p=world.Provider(self.md,world.start(self.md.cache_dir),runner=acquire.Runner(late_publication))
        with mock.patch.multiple(world,READ_BUDGET_S=5.,DOWNLOAD_S=3.,PROCESS_S=5.,PUBLICATION_S=3.):
            tick=time.monotonic();v=p.view((fx.CELL,))
        self.assertGreater(time.monotonic()-tick,5.)
        self.assertLess(time.monotonic()-tick,9.)
        self.assertTrue(v.snapshot.entries)

    def test_pending_owner_is_deferred_without_freezing_missing_geometry(self):
        p,r=self.provider(source_features());token='a'*32
        p.store._atomic_write(p._claim_path(fx.CELL),p.store._document(dict(session=p.session.identity,
            cell=str(fx.CELL),token=token,status='pending',owner=os.getpid(),expires=time.time()+30)))
        with mock.patch.object(world,'READ_BUDGET_S',.05),self.assertRaises(fs.LockTimeout):p.view((fx.CELL,))
        self.assertFalse(p._state()['cells']);self.assertEqual(r.calls,0)
        p._pin_source(sample.VERSION)
        snapshot,report=cache.normalize((lc.Snapshot(fx.CELL.bounds(),source_features(),(),sample.VERSION),))
        p._publish(fx.CELL,token,snapshot,sample.VERSION,report)
        self.assertTrue(p.view((fx.CELL,)).snapshot.entries)

    def test_source_pin_preserves_choices_and_rejects_a_different_publication(self):
        p,r=self.provider(source_features());p.view((fx.CELL,));before=p._state()['cells']
        p._pin_source(sample.VERSION)
        self.assertEqual(p._state()['cells'],before)
        with self.assertRaisesRegex(ValueError,'source version changed'):
            p._pin_source(dict(sample.VERSION,etag='"different"'))
        self.assertEqual(p._state()['cells'],before)

    def test_corrupt_claim_is_rejected_before_source_work(self):
        p,r=self.provider(source_features())
        claim=dict(session=p.session.identity,cell=str(fx.CELL),token='a'*32,
                   status='chosen',owner=os.getpid(),expires=time.time()+30)
        for value in (claim,dict(claim,status='pending',expires='invalid')):
            p.store._atomic_write(p._claim_path(fx.CELL),p.store._document(value))
            with self.assertRaises(fs.CorruptStorage):p.view((fx.CELL,))
        self.assertEqual(r.calls,0)

    def test_late_publication_cannot_replace_an_immutable_unavailable_choice(self):
        p,r=self.provider(source_features());token='a'*32
        p._pin_source(sample.VERSION)
        p.store._atomic_write(p._claim_path(fx.CELL),p.store._document(dict(session=p.session.identity,
            cell=str(fx.CELL),token=token,status='pending',owner=os.getpid(),expires=time.time()+30)))
        p._choose(fx.CELL,TimeoutError('owned query ended'));before=p._state()['cells']
        snapshot,report=cache.normalize((lc.Snapshot(fx.CELL.bounds(),source_features(),(),sample.VERSION),))
        with self.assertRaises(fs.StalePreview):p._publish(fx.CELL,token,snapshot,sample.VERSION,report)
        self.assertEqual(p._state()['cells'],before)
        self.assertNotIn(str(fx.CELL),p.store.index()['cells'])

    def test_expired_pending_claim_can_be_renewed_with_a_fresh_token(self):
        p,r=self.provider(source_features());token='a'*32
        p.store._atomic_write(p._claim_path(fx.CELL),p.store._document(dict(session=p.session.identity,
            cell=str(fx.CELL),token=token,status='pending',owner=os.getpid(),expires=time.time()-1)))
        self.assertTrue(p.view((fx.CELL,)).snapshot.entries)
        self.assertNotEqual(p._claim(fx.CELL)['token'],token)

    def test_request_reservations_and_retry_after_survive_reopening_the_source_store(self):
        import buildingregions as regions
        store=cache.Store(self.root/'requests');ledger=regions.Jobs(store);clock=[1000.]
        requests=regions.Requests(ledger,ledger.read(),self.root/'raw',lambda:clock[0],lambda:False,
                                 lambda seconds:clock.__setitem__(0,clock[0]+seconds))
        requests.admit(16)
        self.assertEqual(regions.Jobs(cache.Store(self.root/'requests')).read()['next_request'],1001.)
        requests.failed(429,{'retry-after':'120'})
        self.assertEqual(regions.Jobs(cache.Store(self.root/'requests')).read()['next_request'],1120.)
        requests.admit(16);self.assertEqual(clock[0],1120.)
        requests.used=regions.OPERATION_BYTES
        with self.assertRaises(fs.ResourceLimit):requests.admit(16)

    def test_render_conversion_does_not_hold_the_publication_mutex(self):
        p,r=self.provider(source_features());p.view((fx.CELL,));original=world.View
        def convert(*args):
            other=cache.Store(self.md.cache_dir);other.lock_timeout=.05
            with other._locked():pass
            return original(*args)
        with mock.patch.object(world,'View',side_effect=convert):self.assertTrue(p.view((fx.CELL,)).snapshot.entries)

    def test_offline_multi_cell_read_is_bounded_and_uses_no_source_reader(self):
        p,r=self.provider();p.md.fetcher.offline_flag=True
        keys=[mapcells.CellKey(fx.CELL.z,fx.CELL.x+i,fx.CELL.y) for i in range(5)]
        tick=time.monotonic();self.assertFalse(p.view(keys).snapshot.entries)
        self.assertLess(time.monotonic()-tick,2.)
        self.assertTrue(p.ready(keys));self.assertEqual(r.calls,0)

    def test_corrupt_index_and_publication_failure_never_select_baseline(self):
        p,r=self.provider(source_features())
        original=p.store._atomic_write
        def fail(path,body):
            if Path(path).name=='index.json':raise OSError('atomic index failure')
            return original(path,body)
        with mock.patch.object(p.store,'_atomic_write',side_effect=fail):v=p.view((fx.CELL,))
        self.assertFalse(v.snapshot.entries);self.assertFalse(p.store.index()['cells'])
        (p.store.root/'index.json').write_text('{broken')
        with self.assertRaises(fs.CorruptStorage):world.start(self.md.cache_dir)

    def test_unrelated_regions_do_not_depend_on_city_constants(self):
        for i,(lat,lon) in enumerate(((48.8566,2.3522),(-33.8688,151.2093)),1001):
            key=mapcells.cell_of(lat,lon);x,y=key.center()[1],key.center()[0];d=.0001
            f=replace(source_features()[0],feature_id=f'osm:way:{i}',osm_ids=(f'osm:way:{i}',),
                      sources=tuple(dict(s,record_id=f'w{i}@2') for s in source_features()[0].sources),
                      geometry=dict(type='Polygon',coordinates=[[[x,y],[x+d,y],[x+d,y+d],[x,y+d],[x,y]]]))
            p,r=self.provider((f,));self.assertEqual(len(p.view((key,)).snapshot.entries),1)

    def four(self,kill):
        descriptor=world.start(self.md.cache_dir);p=world.Provider(self.md,descriptor)
        cached=mapcells.CellKey(fx.CELL.z,fx.CELL.x+10,fx.CELL.y)
        with p.store._locked():
            head=p.store.write(cache.empty(),sample.VERSION,cached.bounds(),dict(coverage_complete=True))
            index=p.store.index();index['cells'][str(cached)]=head;index['influence'][str(cached)]=cached.bounds()
            p.store._atomic_write(p.store.root/'index.json',p.store._document(index))
        ctx=mp.get_context('spawn');started=ctx.Event();out=ctx.Queue()
        owner=ctx.Process(target=caller,args=(str(self.md.cache_dir),descriptor,fx.CELL,'stall',out,started))
        owned=[owner]
        try:
            owner.start();self.assertTrue(started.wait(12))
            if kill:owner.terminate();owner.join(3)
            for key,kind in ((cached,'cached'),(fx.CELL,'same'),(mapcells.CellKey(fx.CELL.z,fx.CELL.x+20,fx.CELL.y),'other')):
                worker=ctx.Process(target=caller,args=(str(self.md.cache_dir),descriptor,key,kind,out));worker.start();owned.append(worker)
            rows=[out.get(timeout=15) for _ in range(3 if kill else 4)]
            self.assertTrue(all('error' not in r for r in rows),rows)
            self.assertLess(next(r['seconds'] for r in rows if r['kind']=='cached'),1.5)
            self.assertTrue(all(r['seconds']<8 for r in rows),rows)
            for worker in owned:worker.join(3);self.assertFalse(worker.is_alive())
        finally:
            for worker in owned:
                if worker.is_alive():worker.terminate();worker.join(3)
            out.close();out.join_thread()

    def test_four_workers_stalled_query_cached_progress(self):self.four(False)
    def test_killed_owner_releases_leases_and_preserves_cache(self):self.four(True)


if __name__=='__main__':unittest.main()
