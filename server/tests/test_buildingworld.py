"""Real ordinary generation accessors + offline worldwide first-visit fixtures."""
from dataclasses import replace
import json
import asyncio
import os
from types import SimpleNamespace as NS
import pickle
import threading
import unittest
from unittest import mock

import buildingoverlay as bo
import buildingprimary as primary
import buildingworld as world
import buildingregions as regions
import buildingoverlaystore as fs
import blockworker as bw
import geodesy,mapcells
import layercakebuildings as lc
import test_buildingrender as fx
from test_buildingregions import Reader,features


class WorldTest(unittest.TestCase):
    setUp=fx.RenderTest.setUp
    close=fx.RenderTest.close
    md=fx.RenderTest.md

    def session(self,md):
        world.enable(md.cache_dir,policy_confirmed=True)
        return world.start(md.cache_dir,primary.Store(md.cache_dir).load())

    def provider(self,md,session,data):
        md.fetcher.offline_flag=False
        calls=[]
        def factory():
            r=Reader(data);calls.append(r);return r
        return world.Provider(md,session,reader_factory=factory),calls

    def test_two_unrelated_first_visits_revisit_restart_and_native_generation(self):
        md=self.md(street=True)
        a=mapcells.cell_of(30.412876,-9.5524592)
        b=mapcells.cell_of(48.8566,2.3522)
        af=features(a.bounds(),3)
        bf=[replace(f,feature_id='osm:way:'+str(i+101),osm_ids=('osm:way:'+str(i+101),),
                    sources=({'dataset':'OpenStreetMap','record_id':f'w{i+101}@2'},)) for i,f in enumerate(features(b.bounds(),3))]
        descriptor=self.session(md)
        provider,calls=self.provider(md,descriptor,af+bf)
        md.pin_buildings(descriptor);md._building_world=provider
        before_roads=md.cell(a,layers=('roads',)).layers['roads'].to_arrays('')
        dependencies=[]
        for key in (a,b,a):
            dependency=md.building_dependencies((key,))
            effective=md.building_cell(key).layers['buildings']
            self.assertEqual(len(effective),3)
            frame=geodesy.EnuFrame(*key.center(),0)
            items=fx.buildings.tile_buildings(frame,-3000,-3000,3000,3000,[effective],fx.ground)
            self.assertEqual(len(items),3);self.assertIsNotNone(fx.mesh_fingerprint(items))
            dependencies.append(dependency)
        self.assertEqual(dependencies[0],dependencies[2]);self.assertEqual(len(calls),2)
        objects=len(list(provider.store.objects.iterdir()))
        for name,value in before_roads.items():fx.np.testing.assert_array_equal(value,md.cell(a,layers=('roads',)).layers['roads'].to_arrays('')[name])
        # A fresh worker and then a fresh session both use the actual durable cache.
        worker=world.Provider(md,pickle.loads(pickle.dumps(descriptor)),reader_factory=lambda:(_ for _ in ()).throw(AssertionError('cached worker must not fetch')))
        self.assertEqual(worker.view((a,)).dependency((a,)),dependencies[0])
        another=world.start(md.cache_dir,primary.Store(md.cache_dir).load())
        restarted=world.Provider(md,another,reader_factory=lambda:(_ for _ in ()).throw(AssertionError('cached restart must not fetch')))
        self.assertEqual(restarted.view((b,)).dependency((b,)),dependencies[1])
        self.assertEqual(len(list(provider.store.objects.iterdir())),objects,'warm sessions reuse objects instead of growing history')

    def test_network_failure_fallback_is_frozen_no_later_collision_upgrade(self):
        md=self.md([fx.record()]);descriptor=self.session(md);key=fx.CELL
        md.fetcher.offline_flag=True
        provider=world.Provider(md,descriptor,reader_factory=lambda:(_ for _ in ()).throw(AssertionError('offline must not construct reader')))
        self.assertFalse(provider.view((key,)).active)
        md.fetcher.offline_flag=False
        provider.reader_factory=lambda:(_ for _ in ()).throw(AssertionError('a frozen fallback must never retry this session'))
        self.assertFalse(provider.view((key,)).active)
        index=provider._index();self.assertNotIn(str(key),index['cells'])
        state=provider.store._read(provider.path)
        raw=provider.store._read(provider.store.objects/(state['cells'][str(key)]+'.json'))
        self.assertEqual(raw['report']['mode'],'world_fallback')

    def test_cross_cell_building_is_atomic_and_earlier_fallback_cannot_be_replaced(self):
        md=self.md();a=fx.CELL;b=mapcells.CellKey(a.z,a.x+1,a.y)
        w,s,e,n=a.bounds();x=e;y=(s+n)/2;d=.00005
        f=replace(features(a.bounds(),1)[0],geometry=dict(type='Polygon',coordinates=[[[x-d,y-d],[x+d,y-d],[x+d,y+d],[x-d,y+d],[x-d,y-d]]]))
        provider,calls=self.provider(md,self.session(md),[f])
        av=provider.view((a,));bv=provider.view((b,))
        self.assertEqual([fs._entry_wire(e) for e in av.snapshot.entries],[fs._entry_wire(e) for e in bv.snapshot.entries])
        self.assertEqual(len(av.snapshot.entries),1)
        self.assertEqual(provider.view((a,)).dependency((a,)),av.dependency((a,)))
        # A separate fresh session starts with a fallback, then acquires next door.
        md.fetcher.offline_flag=True
        other=world.Provider(md,world.start(md.cache_dir,primary.Store(md.cache_dir).load()))
        # Force an unused adjacent pair so no cached object masks the failure.
        c=mapcells.CellKey(a.z,a.x+8,a.y);dkey=mapcells.CellKey(c.z,c.x+1,c.y)
        self.assertFalse(other.view((c,)).active)
        x=c.bounds()[2]
        shifted=replace(f,feature_id='osm:way:2',osm_ids=('osm:way:2',),
            sources=({'dataset':'OpenStreetMap','record_id':'w2@2'},),
            geometry=dict(type='Polygon',coordinates=[[[x-d,y-d],[x+d,y-d],[x+d,y+d],[x-d,y+d],[x-d,y-d]]]))
        md.fetcher.offline_flag=False
        import time
        other.wall=lambda:time.time()+61  # clear the shared failure cooldown without sleeping
        other.reader_factory=lambda:Reader([shifted])
        later=other.view((dkey,))
        self.assertFalse(later.active)
        self.assertFalse(other.view((c,)).active)
        head=other.store._read(other.path)['cells'][str(dkey)]
        report=other.store._read(other.store.objects/(head+'.json'))['report']
        self.assertTrue(report['coverage_complete'])
        self.assertEqual(report['session_boundary_deferred'],1)
        self.assertFalse(report['all_features_updated'])

    def test_two_workers_racing_first_visit_publish_one_choice(self):
        md=self.md();descriptor=self.session(md);key=fx.CELL
        provider,calls=self.provider(md,descriptor,features(key.bounds(),2))
        other=world.Provider(md,descriptor,reader_factory=provider.reader_factory)
        results=[];errors=[]
        def run(p):
            try:results.append(p.view((key,)).dependency((key,)))
            except Exception as error:errors.append(error)
        threads=[threading.Thread(target=run,args=(p,)) for p in (provider,other)]
        for t in threads:t.start()
        for t in threads:t.join(30)
        self.assertFalse(any(t.is_alive() for t in threads));self.assertEqual(errors,[])
        self.assertEqual(len(calls),1);self.assertEqual(results[0],results[1])

    def test_manual_refresh_changes_only_next_session_and_invalidates_local_geometry(self):
        md=self.md();key=fx.CELL;data=features(key.bounds(),2)
        provider,calls=self.provider(md,self.session(md),data)
        old=provider.view((key,)).dependency((key,))
        changed=[replace(f,attrs=dict(f.attrs,height=30)) for f in data]
        # A running companion's lease excludes this path in production; release
        # only in this data test, retaining the old session ledger for comparison.
        saved,report=regions.update(md,md.cache_dir,key.bounds(),reader=Reader(changed),policy_confirmed=True,wall=lambda:1000)
        self.assertTrue(report['coverage_complete'])
        self.assertEqual(provider.view((key,)).dependency((key,)),old)
        new_session=world.start(md.cache_dir,saved)
        new=world.Provider(md,new_session,reader_factory=lambda:(_ for _ in ()).throw(AssertionError('saved data only')))
        self.assertNotEqual(new.view((key,)).dependency((key,)),old)
        self.assertEqual({e.feature.attrs['height'] for e in new.view((key,)).snapshot.entries},{30})

    def test_corruption_and_object_capacity_never_reclaim_or_modify_old_choices(self):
        md=self.md();key=fx.CELL
        provider,calls=self.provider(md,self.session(md),features(key.bounds(),1))
        old=provider.view((key,)).dependency((key,))
        sibling=mapcells.CellKey(key.z,key.x+20,key.y)
        with mock.patch.object(world,'MAX_CELLS',1),self.assertRaises(fs.ResourceLimit):provider.view((sibling,))
        self.assertEqual(provider.view((key,)).dependency((key,)),old)
        state=provider.store._read(provider.path);path=provider.store.objects/(state['cells'][str(key)]+'.json')
        path.write_bytes(b'{incomplete')
        with self.assertRaises(fs.CorruptStorage):provider.view((key,))

    def test_actual_parquet_range_reader_through_world_generation_barrier(self):
        import duckdb
        from test_buildingprimary import FIXTURE,wkb,ParquetHTTP
        path=self.root/'world-source.parquet';geometry=FIXTURE['row']['geometry'];box=bo._bbox(geometry)
        con=duckdb.connect(config={'autoinstall_known_extensions':False,'autoload_known_extensions':False})
        try:
            con.execute('''CREATE TABLE b AS SELECT 1572086438::BIGINT AS id,'way'::VARCHAR AS type,
                2::INTEGER AS version,'2026-10-28T00:57:56Z'::TIMESTAMPTZ AS timestamp,
                'house' AS building,'3' AS "building:levels",?::BLOB AS geometry,
                struct_pack(xmin:=?,ymin:=?,xmax:=?,ymax:=?) AS bbox''',[wkb(geometry),*box])
            con.execute('COPY b TO ? (FORMAT PARQUET)',[str(path)])
        finally:con.close()
        md=self.md();md.fetcher.offline_flag=False;http=ParquetHTTP(path.read_bytes());clock=[1000.]
        descriptor=self.session(md)
        provider=world.Provider(md,descriptor,
            reader_factory=lambda:lc.Reader(self.root/'raw',md.fetcher,transport=http,tolerant=True,raw_budget=regions.RAW_BYTES),
            wall=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
        key=mapcells.cell_of(30.412876,-9.5524592)
        view=provider.view((key,))
        self.assertEqual([e.feature.feature_id for e in view.snapshot.entries],['osm:way:1572086438'])
        self.assertGreater(len(http.calls),1)
        before=len(http.calls);provider.view((key,))
        self.assertEqual(len(http.calls),before)

    def test_session_choice_write_failure_recovers_without_refetch_or_unfenced_output(self):
        md=self.md();key=fx.CELL;descriptor=self.session(md)
        provider,calls=self.provider(md,descriptor,features(key.bounds(),2))
        original=provider.store._atomic_write
        def fail(path,body):
            if path==provider.path and json.loads(body)['payload']['cells']:raise OSError('session head interrupted')
            return original(path,body)
        with mock.patch.object(provider.store,'_atomic_write',fail),self.assertRaisesRegex(OSError,'session head'):
            provider.view((key,))
        self.assertFalse(provider.ready((key,)))
        self.assertIn(str(key),provider._index()['cells'])
        worker=world.Provider(md,descriptor,reader_factory=lambda:(_ for _ in ()).throw(AssertionError('complete cache survives interrupted choice')))
        self.assertEqual(len(worker.view((key,)).snapshot.entries),2)
        self.assertTrue(worker.ready((key,)))

    def test_adjacent_first_visit_does_not_refresh_previously_acquired_identity(self):
        md=self.md();a=fx.CELL;b=mapcells.CellKey(a.z,a.x+1,a.y)
        x=a.bounds()[2];y=sum(a.bounds()[1::2])/2;d=.00005
        f=replace(features(a.bounds(),1)[0],geometry=dict(type='Polygon',coordinates=[[[x-d,y-d],[x+d,y-d],[x+d,y+d],[x-d,y+d],[x-d,y-d]]]))
        provider,_=self.provider(md,self.session(md),[f]);provider.view((a,))
        descriptor=world.start(md.cache_dir,primary.Store(md.cache_dir).load())
        def newer():
            reader=Reader([replace(f,attrs=dict(f.attrs,height=30))])
            reader.version=dict(reader.version,etag='"newer"')
            return reader
        other=world.Provider(md,descriptor,reader_factory=newer)
        self.assertEqual({e.feature.attrs['height'] for e in other.view((b,)).snapshot.entries},{9.6})
        self.assertEqual({e.feature.attrs['height'] for e in other.view((a,)).snapshot.entries},{9.6})

    def test_actual_worldwide_startup_pins_descriptor_before_workers_without_freshness(self):
        import earthd
        md=self.md();world.enable(md.cache_dir,policy_confirmed=True);captured=[]
        class StopBeforeWorkers(Exception):pass
        def pool(*args,**kw):
            descriptor=kw['initargs'][-1];captured.append(descriptor)
            self.assertIsInstance(descriptor,world.Session)
            self.assertEqual(md._building_world.session,descriptor)
            raise StopBeforeWorkers()
        args=NS(far_format='dae',lod_k=None,near_radii=None,cache_dir=md.cache_dir,offline=True,
            cache_cap=1.,surface='off',roads='on',sea='off',trees='off',buildings='on',background_duty=.5,
            detail_ring=500.,download_ahead='off',route_prefetch='off',route_prefetch_km=1.,block_process='on',
            grading='off',road_widths='off',airports='off',gas_stations='off')
        with mock.patch.object(lc,'Reader',side_effect=AssertionError('no startup freshness')),\
             mock.patch.object(earthd.netfetch,'Fetcher',return_value=md.fetcher),\
             mock.patch.object(earthd.cachemod,'Cache'),\
             mock.patch.object(earthd,'mapdatamod',NS(MapData=mock.Mock(return_value=md))),\
             mock.patch.object(earthd,'prune_own_caches'),\
             mock.patch.object(earthd.plancachemod,'start_run'),\
             mock.patch.object(earthd.pacer,'DUTY',earthd.pacer.DUTY),\
             mock.patch.multiple(earthd,DETAIL_RING_M=earthd.DETAIL_RING_M,AHEAD_ON=earthd.AHEAD_ON,\
                 ROUTE_AHEAD_ON=earthd.ROUTE_AHEAD_ON,ROUTE_AHEAD_MAX_M=earthd.ROUTE_AHEAD_MAX_M),\
             mock.patch.object(earthd.poolsmod,'RespawningPool',side_effect=pool):
            with self.assertRaises(StopBeforeWorkers):asyncio.run(earthd.main_async(args))
        self.assertEqual(len(captured),1)
        with primary.Store(md.cache_dir).session():pass

    def test_index_publication_failure_pins_fallback_not_the_unpublished_candidate(self):
        md=self.md();key=fx.CELL;provider,calls=self.provider(md,self.session(md),features(key.bounds(),2))
        original=provider.store._atomic_write
        def fail(path,body):
            if path==provider.store.root/'index.json':raise OSError('index publication interrupted')
            return original(path,body)
        with mock.patch.object(provider.store,'_atomic_write',fail):
            self.assertFalse(provider.view((key,)).active)
        self.assertFalse(provider.view((key,)).active)
        worker=world.Provider(md,provider.session,reader_factory=lambda:(_ for _ in ()).throw(AssertionError('frozen fallback')))
        self.assertFalse(worker.view((key,)).active)
        self.assertNotIn(str(key),provider._index()['cells'])

    def test_unknown_region_cannot_implicitly_move_a_known_global_osm_identity(self):
        md=self.md();a=fx.CELL;b=mapcells.cell_of(48.8566,2.3522)
        first=features(a.bounds(),1)[0]
        provider,_=self.provider(md,self.session(md),[first]);provider.view((a,))
        moved=replace(first,geometry=features(b.bounds(),1)[0].geometry,attrs=dict(first.attrs,height=80))
        other,_=self.provider(md,world.start(md.cache_dir,primary.Store(md.cache_dir).load()),[moved])
        self.assertFalse(other.view((b,)).active)
        self.assertEqual({e.feature.attrs['height'] for e in other.view((a,)).snapshot.entries},{9.6})

    def test_source_failure_keeps_valid_cached_cross_boundary_geometry(self):
        md=self.md();a=fx.CELL;b=mapcells.CellKey(a.z,a.x+1,a.y)
        x=a.bounds()[2];y=sum(a.bounds()[1::2])/2;d=.00005
        f=replace(features(a.bounds(),1)[0],geometry=dict(type='Polygon',coordinates=[[[x-d,y-d],[x+d,y-d],[x+d,y+d],[x-d,y+d],[x-d,y-d]]]))
        first,_=self.provider(md,self.session(md),[f]);first.view((a,))
        descriptor=world.start(md.cache_dir,primary.Store(md.cache_dir).load())
        md.fetcher.offline_flag=True
        provider=world.Provider(md,descriptor)
        view=provider.view((b,))
        self.assertTrue(view.active)
        self.assertEqual([e.feature.feature_id for e in view.snapshot.entries],['osm:way:1'])
        head=provider.store._read(provider.path)['cells'][str(b)]
        self.assertFalse(provider.store._read(provider.store.objects/(head+'.json'))['report']['coverage_complete'])

    def test_full_influence_cannot_overwrite_cached_neighbour_outside_query_cell(self):
        md=self.md();a=fx.CELL;b=mapcells.CellKey(a.z,a.x+1,a.y)
        f=features(a.bounds(),1)[0]
        first,_=self.provider(md,self.session(md),[f]);first.view((a,))
        x,y=f.geometry['coordinates'][0][0];end=sum(b.bounds()[::2])/2;d=.00002
        crossing=replace(f,feature_id='osm:way:2',osm_ids=('osm:way:2',),
            sources=({'dataset':'OpenStreetMap','record_id':'w2@2'},),
            geometry=dict(type='Polygon',coordinates=[[[x,y],[end,y],[end,y+d],[x,y+d],[x,y]]]))
        descriptor=world.start(md.cache_dir,primary.Store(md.cache_dir).load())
        provider,_=self.provider(md,descriptor,[crossing])
        self.assertFalse(provider.view((b,)).active)
        self.assertEqual([e.feature.feature_id for e in provider.view((a,)).snapshot.entries],['osm:way:1'])

    def test_travel_does_not_accumulate_a_global_geometry_admission_quota(self):
        md=self.md();keys=[mapcells.cell_of(lat,lon) for lat,lon in ((30.4,-9.5),(48.8,2.3),(-33.8,151.2))]
        data=[]
        for group,key in enumerate(keys):
            for i,f in enumerate(features(key.bounds(),2)):
                identity=f'osm:way:{group*100+i+1}'
                data.append(replace(f,feature_id=identity,osm_ids=(identity,),
                    sources=({'dataset':'OpenStreetMap','record_id':f'w{group*100+i+1}@2'},)))
        provider,_=self.provider(md,self.session(md),data)
        with mock.patch.object(primary,'REGIONAL_LIMITS',replace(primary.REGIONAL_LIMITS,max_entries=3)):
            for key in keys+keys[:1]:
                self.assertEqual(len(provider.view((key,)).snapshot.entries),2)
        state=provider.store._read(provider.path)
        self.assertEqual(len(state['cells']),3)
        self.assertEqual(set(state['influence']),set(state['cells']))

    def test_spatial_matching_cannot_implicitly_update_a_cached_baseline_owner(self):
        a=fx.CELL;b=mapcells.CellKey(a.z,a.x+1,a.y)
        x=a.bounds()[2];y=sum(a.bounds()[1::2])/2;d=.00005
        geometry=dict(type='Polygon',coordinates=[[[x-d,y-d],[x+d,y-d],[x+d,y+d],[x-d,y+d],[x-d,y-d]]])
        md=self.md([fx.record('gers-reference',geometry=geometry,attrs={'height':18})])
        f=replace(features(a.bounds(),1)[0],geometry=geometry,attrs={'height':6})
        first,_=self.provider(md,self.session(md),[f])
        self.assertEqual(first.view((a,)).snapshot.entries[0].feature.attrs['height'],6)
        newer=replace(f,feature_id='osm:way:2',osm_ids=('osm:way:2',),attrs={'height':30},
                      sources=({'dataset':'OpenStreetMap','record_id':'w2@3'},))
        def factory():
            reader=Reader([newer]);reader.version=dict(reader.version,etag='"new-identity"');return reader
        other=world.Provider(md,world.start(md.cache_dir,primary.Store(md.cache_dir).load()),reader_factory=factory)
        self.assertEqual(other.view((b,)).snapshot.entries[0].feature.attrs['height'],6)


if __name__=='__main__':unittest.main()
