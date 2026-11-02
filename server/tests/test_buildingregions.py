"""Offline city-volume, durable retrieval and pre-game launcher regressions."""
from dataclasses import replace
import contextlib
import io
import json
from pathlib import Path
import sys
import time
import threading
import math
import unittest
from unittest import mock

import buildingoverlay as bo
import buildingoverlaystore as fs
import buildingprimary as primary
import buildingregions as regions
import layercakebuildings as lc
import osmbuildings as osm
import test_buildingprimary as old
import test_buildingrender as fx


class Reader:
    def __init__(self,features=(),fail=None):
        self.features=tuple(features);self.calls=[];self.fail=fail;self.version=dict(old.VERSION)
    def select(self,**kw):
        assert kw=={'explicit':True}
        return self.version
    def attach(self,version):
        self.version=version;return version
    def close(self):pass
    def query(self,bounds):
        self.calls.append(tuple(bounds))
        if len(self.calls)==self.fail: raise OSError('synthetic halfway failure')
        got=[f for f in self.features if fs._intersects(bo._bbox(f.geometry),bounds)]
        if len(got)>lc.MAX_FEATURES: raise fs.ResourceLimit('building partition feature budget exceeded')
        return lc.Snapshot(tuple(bounds),tuple(got),(),self.version)


def features(bounds,n):
    w,s,e,north=bounds
    side=math.ceil(n**.5)
    dx,dy=(e-w)/side,(north-s)/side
    result=[]
    for i in range(n):
        x,y=w+(i%side+.15)*dx,s+(i//side+.15)*dy
        sx=min(dx*.5,25/(111320*math.cos(math.radians(y))))
        sy=min(dy*.5,25/111320)
        ring=[[x,y],[x+sx,y],[x+sx,y+sy],[x,y+sy],[x,y]]
        identity=f'osm:way:{i+1}'
        result.append(bo.Feature(identity,dict(type='Polygon',coordinates=[ring]),
            dict(height=9.6,num_floors=3,roof_shape='flat'),
            (dict(dataset='OpenStreetMap',record_id=f'w{i+1}@2',update_time='2026-10-28T00:57:56Z'),),
            osm_ids=(identity,)))
    return result


class RegionTest(unittest.TestCase):
    setUp=fx.RenderTest.setUp
    close=fx.RenderTest.close
    md=fx.RenderTest.md

    def run_update(self,md,bounds,reader,wall=1000,**kw):
        md.fetcher.offline_flag=False  # Only synthetic reader; socket guard remains installed.
        return regions.update(md,self.root/'primary',bounds,reader=reader,policy_confirmed=True,
                              wall=lambda:wall,**kw)

    def test_realistic_city_volume_and_second_region_without_agadir_constants(self):
        measurements=[]
        for lat,lon in ((30.412876,-9.5524592),(48.8566,2.3522)):
            bounds=regions.area(lat,lon,1.2)
            data=features(bounds,3600)
            md=self.md(frame=fx.geodesy.EnuFrame(lat,lon,0))
            reader=Reader(data);start=time.perf_counter()
            snapshot,report=self.run_update(md,bounds,reader,wall=1000+len(measurements)*1000)
            self.assertTrue(report['coverage_complete']);self.assertTrue(report['all_features_updated'])
            self.assertGreater(len(reader.calls),1)
            self.assertEqual(report['source_features'],3600)
            self.assertEqual(len({e.feature.feature_id for e in snapshot.entries}),3600)
            self.assertEqual(report['completed'],report['total'])
            self.assertEqual(report['deferred_count'],0)
            measurements.append(dict(centre=[lat,lon],features=3600,partitions=len(reader.calls),
                                     seconds=round(time.perf_counter()-start,3)))
            # Isolate the regions' stores without deleting either test evidence.
            if len(measurements)==1:
                self.root=self.root/'second';self.root.mkdir()
        print('REGIONAL_VOLUME '+json.dumps(measurements),flush=True)

    def test_halfway_failure_resume_boundary_dedupe_and_unchanged_reuse(self):
        md=self.md();bounds=regions.area(fx.LAT,fx.LON,.6)
        data=features(bounds,80)
        # Leave a genuine empty gap at the partition crossing; overlap must
        # exercise the separate conflict case, not this deduplication fixture.
        data=[replace(f,geometry=dict(type='Polygon',coordinates=[[
            [f.geometry['coordinates'][0][0][0]+(x-f.geometry['coordinates'][0][0][0])*.1,
             f.geometry['coordinates'][0][0][1]+(y-f.geometry['coordinates'][0][0][1])*.1]
            for x,y in f.geometry['coordinates'][0]]])) for f in data]
        first_box=regions.partition(bounds)[0];x,y=first_box[2],first_box[3];d=.00001
        crossing=replace(data[0],geometry=dict(type='Polygon',coordinates=[[[x-d,y-d],[x+d,y-d],[x+d,y+d],[x-d,y+d],[x-d,y-d]]]))
        data[0]=crossing
        reader=Reader(data,fail=3)
        with self.assertRaisesRegex(OSError,'halfway'):self.run_update(md,bounds,reader)
        state=regions.status(self.root/'primary')
        self.assertEqual(state['completed'],2);self.assertEqual(state['status'],'failed')
        self.assertIsNone(state['saved_revision'])
        second=Reader(data)
        snapshot,report=self.run_update(md,bounds,second,wall=1700)
        self.assertEqual(len(snapshot.entries),80,report['deferred'])
        self.assertEqual(len(second.calls),report['total']-2)
        self.assertEqual(len({e.feature.feature_id for e in snapshot.entries}),80)
        third=Reader(data)
        repeated,again=self.run_update(md,bounds,third,wall=2400)
        self.assertEqual(third.calls,[])
        self.assertEqual(snapshot.revision,repeated.revision)
        self.assertTrue(again['coverage_complete'])

    def test_cancel_and_atomic_publication_failure_preserve_previous_head(self):
        md=self.md();bounds=regions.area(fx.LAT,fx.LON,.6);data=features(bounds,10)
        reader=Reader(data)
        with self.assertRaises(InterruptedError):
            self.run_update(md,bounds,reader,cancel=lambda:len(reader.calls)>=2)
        self.assertEqual(regions.status(self.root/'primary')['status'],'cancelled')
        self.assertIsNone(primary.Store(self.root/'primary').load().revision)
        original=primary.Store._atomic_write
        def fail(store,path,body):
            if path==store.root/'state.json':raise OSError('head interrupted')
            return original(store,path,body)
        with mock.patch.object(primary.Store,'_atomic_write',fail),self.assertRaisesRegex(OSError,'head interrupted'):
            self.run_update(md,bounds,Reader(data),wall=1700)
        self.assertIsNone(primary.Store(self.root/'primary').load().revision)
        snapshot,report=self.run_update(md,bounds,Reader(data),wall=2400)
        self.assertEqual(len(snapshot.entries),10);self.assertTrue(report['coverage_complete'])

    def test_unsupported_and_ambiguous_features_do_not_block_independent_buildings(self):
        md=self.md([fx.record('a'),fx.record('b',x=62)])
        valid=lc.normalize([old.row(8,fx.polygon(150,150))],bo._bbox(fx.polygon(0,0,300,300)),old.VERSION)
        conflict=replace(valid.buildings[0],feature_id='osm:way:9',geometry=fx.polygon(60,60),osm_ids=('osm:way:9',))
        unsupported=replace(valid.buildings[0],feature_id='osm:way:10',osm_ids=('osm:way:10',),
            geometry=dict(type='MultiPolygon',coordinates=[fx.polygon(200,200)['coordinates'],fx.polygon(230,230)['coordinates']]))
        data=replace(valid,buildings=valid.buildings+(conflict,unsupported),issues=(('osm:way:10','unsupported_multipolygon'),))
        before=primary.Store(self.root/'primary').load()
        snapshot,report,checks=regions.candidate(md,before,[data],data.bounds,old.VERSION)
        self.assertEqual([e.feature.feature_id for e in snapshot.entries],['osm:way:8'])
        self.assertTrue(report['coverage_complete']);self.assertFalse(report['all_features_updated'])
        self.assertEqual(report['deferred_count'],2)
        for context in checks:context.validate(md)

    def test_unverified_absence_preserves_previous_and_ordinary_startup_never_checks_source(self):
        md=self.md();bounds=regions.area(fx.LAT,fx.LON,.3);data=features(bounds,2)
        first,_=self.run_update(md,bounds,Reader(data))
        empty=Reader();empty.version=dict(old.VERSION,etag='"new-publication"')
        second,report=self.run_update(md,bounds,empty,wall=1700)
        self.assertEqual(first.entries,second.entries)
        self.assertEqual(report['missing_unverified_count'],2)
        self.assertEqual(report['status'],'complete_with_warnings')
        with mock.patch.object(lc,'Reader',side_effect=AssertionError('no freshness on launch')):
            pinned,startup=primary.cached(md,self.root/'primary')
        self.assertEqual(pinned.revision,second.revision);self.assertFalse(startup['checked'])
        self.assertTrue(regions.configured(self.root/'primary'))

    def test_dense_jobs_subdivide_instead_of_raising_feature_quota(self):
        md=self.md();bounds=regions.area(fx.LAT,fx.LON,.15);reader=Reader(features(bounds,529))
        snapshot,report=self.run_update(md,bounds,reader)
        self.assertEqual(report['total'],4);self.assertEqual(report['completed'],4)
        self.assertEqual(len(snapshot.entries),529);self.assertEqual(len(reader.calls),5)
        self.assertEqual(lc.MAX_FEATURES,512)

    def test_region_limits_session_exclusion_persistent_cooldown_and_corrupt_jobs(self):
        md=self.md();bounds=regions.area(fx.LAT,fx.LON,.2);data=features(bounds,2)
        first,_=self.run_update(md,bounds,Reader(data))
        with self.assertRaises(osm.Throttled):self.run_update(md,bounds,Reader(data),wall=1001)
        store=primary.Store(self.root/'primary')
        with store.session(),self.assertRaises(fs.LockTimeout):
            self.run_update(md,bounds,Reader(data),wall=1700)
        job=next((store.root/'regions/objects').glob('*.json'));job.write_bytes(b'{broken')
        with self.assertRaises(fs.CorruptStorage):self.run_update(md,bounds,Reader(data),wall=1700)
        self.assertEqual(store.load().revision,first.revision)
        for lat,lon,r in ((89,0,1),(0,179.99,10),(0,0,11)):
            with self.assertRaises(ValueError):regions.area(lat,lon,r)

    def test_request_reservation_retry_after_restart_and_offline(self):
        store=primary.Store(self.root/'requests');jobs=regions.Jobs(store);state=regions.empty()
        clock=[1000.];requests=regions.Requests(jobs,state,self.root/'raw',lambda:clock[0],lambda:False,
                                             lambda seconds:clock.__setitem__(0,clock[0]+seconds))
        requests.admit(16)
        self.assertEqual(jobs.read()['next_request'],1001)
        requests.failed(429,{'retry-after':'120'})
        self.assertEqual(jobs.read()['next_request'],1120)
        requests.admit(16);self.assertEqual(clock[0],1120)
        requests.used=regions.OPERATION_BYTES
        with self.assertRaises(fs.ResourceLimit):requests.admit(16)
        md=self.md();reader=Reader()
        with self.assertRaisesRegex(ValueError,'offline'):
            regions.update(md,self.root/'offline',regions.area(fx.LAT,fx.LON,.2),reader=reader,policy_confirmed=True)
        self.assertEqual(reader.calls,[])

    def test_tolerant_normalization_and_supported_parent_safety(self):
        rows=[old.row(1),dict(old.row(2),geometry=b'broken'),old.row(3,building_id='way/1',**{'building:part':'yes'})]
        result=lc.normalize(rows,bo._bbox(fx.polygon(0,0,300,300)),old.VERSION,tolerant=True)
        self.assertEqual(len(result.buildings),1);self.assertEqual(len(result.parts),1)
        self.assertTrue(any(i[0]=='osm:way:2' for i in result.issues))
        with self.assertRaises(ValueError):lc.normalize([old.row(1),old.row(1)],result.bounds,old.VERSION,tolerant=True)
        oversized=dict(old.row(4),geometry=None,building_id='way/1',_oversized_geometry=True,**{'building:part':'yes'})
        checked=lc.normalize([old.row(1),oversized],result.bounds,old.VERSION,tolerant=True)
        self.assertIn(('osm:way:1','incomplete_part_geometry'),checked.issues)
        boolean=lc.normalize([dict(old.row(5),**{'building:part':False})],result.bounds,old.VERSION,tolerant=True)
        self.assertEqual(len(boolean.buildings),1);self.assertEqual(len(boolean.parts),0)

    def test_sql_rejects_oversized_blobs_and_tags_before_python_normalization(self):
        import duckdb
        geometry=old.FIXTURE['row']['geometry'];box=bo._bbox(geometry)
        path=self.root/'oversized.parquet'
        con=duckdb.connect(config={'autoinstall_known_extensions':False,'autoload_known_extensions':False})
        try:
            con.execute('''CREATE TABLE b(id BIGINT,type VARCHAR,version INTEGER,timestamp VARCHAR,
                building VARCHAR,name VARCHAR,geometry BLOB,bbox STRUCT(xmin DOUBLE,ymin DOUBLE,xmax DOUBLE,ymax DOUBLE))''')
            for identity,name,blob in ((1,'x'*5000,old.wkb(geometry)),(2,'normal',b'\x00'*131073)):
                con.execute('INSERT INTO b VALUES (?,\'way\',2,\'2026-10-28T00:57:56Z\',\'house\',?,?,struct_pack(xmin:=?,ymin:=?,xmax:=?,ymax:=?))',
                            [identity,name,blob,*box])
            con.execute('COPY b TO ? (FORMAT PARQUET)',[str(path)])
        finally:con.close()
        import netfetch
        fetcher=netfetch.Fetcher(transport=object());self.addCleanup(fetcher.close)
        reader=lc.Reader(self.root/'bounded-ranges',fetcher,transport=old.ParquetHTTP(path.read_bytes()),tolerant=True)
        self.addCleanup(reader.close);reader.select(explicit=True)
        data=reader.query(box)
        self.assertIn(('osm:way:1','oversized_attributes'),data.issues)
        self.assertIn(('osm:way:2','geometry_byte_limit'),data.issues)
        self.assertEqual(len(data.buildings),1)
        self.assertNotIn('name',data.buildings[0].attrs)

    def test_publication_only_change_does_not_invalidate_unchanged_meshes(self):
        md=self.md();bounds=regions.area(fx.LAT,fx.LON,.2);data=features(bounds,2)
        first,_=self.run_update(md,bounds,Reader(data))
        changed=[replace(f,sources=tuple(dict(s,dataset_etag='"later"',dataset_timestamp='2026-11-01T15:00:00Z') for s in f.sources)) for f in data]
        reader=Reader(changed);reader.version=dict(old.VERSION,etag='"later"',last_modified='2026-11-01T15:00:00Z')
        second,_=self.run_update(md,bounds,reader,wall=1700)
        self.assertNotEqual(first.revision,second.revision)
        import buildingrender
        a=buildingrender.BuildingView(None,md.release,snapshot=first)
        b=buildingrender.BuildingView(None,md.release,snapshot=second)
        self.assertEqual(a.dependency((fx.CELL,)),b.dependency((fx.CELL,)))

    def test_launcher_update_exits_before_any_install_process_scan_or_launch(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
        import launcher
        with mock.patch.object(sys,'argv',['launcher.py','--osm-buildings-update','--osm-center','48.8','2.3',
                    '--cache-dir',str(self.root/'cli'),'--osm-source-policy-confirmed']),\
                mock.patch.object(regions,'main') as entry,\
                mock.patch.object(launcher,'windows_console',side_effect=AssertionError('must exit before platform paths')),\
                mock.patch.object(launcher,'game_alive',side_effect=AssertionError('must not inspect or launch applications')):
            launcher.main()
        self.assertIn('--confirm-source-policy',entry.call_args.args[0])
        self.assertIn('48.8',entry.call_args.args[0])

    def test_package_and_game_api_contract(self):
        repo=Path(__file__).resolve().parents[2]
        sys.path.insert(0,str(repo/'tools'))
        import package
        files=package.collect()
        self.assertIn('server/buildingregions.py',files)
        self.assertIn('docs/LAYERCAKE_BUILDINGS.md',files)
        for tree in ('mod','tools','probes'):
            for path in (repo/tree).rglob('*.lua'):
                self.assertNotIn('getUserPath(',path.read_text(encoding='utf-8-sig'),str(path))

    def test_parallel_parquet_ranges_are_serialized_and_failure_stops_queued_requests(self):
        import netfetch
        fetcher=netfetch.Fetcher(transport=object());self.addCleanup(fetcher.close)
        class Transport:
            active=0;peak=0;calls=0
            def request(self,url,headers,timeout):
                self.active+=1;self.peak=max(self.peak,self.active);self.calls+=1
                time.sleep(.01);self.active-=1
                return 206,{},b'x'*16
        transport=Transport();ranges=lc.RangeFetcher(fetcher,transport);errors=[]
        def run():
            try:ranges.get(lc.URL,headers={'Range':'bytes=0-15'})
            except Exception as error:errors.append(error)
        threads=[threading.Thread(target=run) for _ in range(3)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(5)
        self.assertEqual(errors,[]);self.assertEqual(transport.peak,1);self.assertEqual(transport.calls,3)
        failed=mock.Mock();failed.request.return_value=(429,{'retry-after':'120'},b'')
        ranges=lc.RangeFetcher(fetcher,failed)
        for _ in range(2):
            with self.assertRaises(osm.HTTPFailure):ranges.get(lc.URL,headers={'Range':'bytes=0-15'})
        self.assertEqual(failed.request.call_count,1)

    def test_overlapping_aabbs_do_not_make_rotated_neighbours_one_huge_group(self):
        records=[]
        for i in range(513):
            x=y=i*1.5
            xy=fx.np.asarray([[x-1,y],[x,y-1],[x+1,y],[x,y+1],[x-1,y]])
            la,lo,_=fx.FRAME.enu_to_geodetic(xy[:,0],xy[:,1],0)
            records.append(bo.Feature(f'osm:way:{i+1}',dict(type='Polygon',coordinates=[fx.np.c_[lo,la].tolist()]),
                                      osm_ids=(f'osm:way:{i+1}',)))
        self.assertEqual(len(regions._groups(records)),513)


if __name__=='__main__':unittest.main()
