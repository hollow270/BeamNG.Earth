"""Real Parquet/SQLite, cross-process reuse/recovery, coverage and fair admission."""
from pathlib import Path
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace as NS
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
import json,multiprocessing as mp,os,sqlite3,time,unittest

import blockworker as bw,buildingpriority as priority,buildingworld as world
import buildingsourcecache as cache,buildingoverlay as geom,buildingoverlaystore as fs
import layercakebuildings as lc,netfetch,mapcells,geodesy,osmbuildingcache as data
import test_buildingquery as query,test_buildingprimary as fixture,test_buildingrender as render


def build_worker(root,body,version,out,started=None):
    """Only this test's process may be terminated; no game or real HTTP."""
    if started is not None:
        original=sqlite3.connect
        class Connection:
            def __init__(self,db):self.db=db
            def __getattr__(self,n):return getattr(self.db,n)
            def executemany(self,sql,rows):
                value=self.db.executemany(sql,rows)
                if sql.startswith('INSERT INTO records'):
                    started.set();time.sleep(60)
                return value
        sqlite3.connect=lambda *a,**k:Connection(original(*a,**k))
    fetch=netfetch.Fetcher(transport=object());reader=lc.Reader(Path(root),fetch,transport=fixture.ParquetHTTP(body))
    try:
        reader.attach(version);box=geom._bbox(fixture.FIXTURE['row']['geometry']);w,s,e,n=box
        bounds=(w-.001,s-.001,e+.001,n+.001);reader.prepare_region(bounds)
        value=reader.query(bounds)
        out.put(dict(ids=[f.feature_id for f in value.buildings],built=reader.diagnostics.get('indexed_groups_built',0)))
    except BaseException as error:out.put(dict(error=repr(error)))
    finally:reader.close();fetch.close()


def hold_lane(root,started,release):
    with data.Store(root).lease('query-lane',10)._locked():
        started.set();release.wait(8)


class PerformanceTest(unittest.TestCase):
    setUp=render.RenderTest.setUp
    close=render.RenderTest.close
    reader=query.QueryTest.reader

    def clone(self,reader,http):
        value=lc.Reader(reader.raw_dir,reader.ranges.fetcher,transport=http,tolerant=reader.tolerant)
        value.attach(reader.selected);self.addCleanup(value.close);return value

    def test_reopen_uses_index_without_duckdb_or_network_and_matches_source(self):
        reader,http,bounds=self.reader(3);original=reader.query(bounds)
        reader.prepare_region(bounds);self.assertEqual(reader.query(bounds),original)
        reopened=self.clone(reader,http);before=len(http.calls)
        with mock.patch.object(reopened,'_connection',side_effect=AssertionError('repeated planet query')):
            reopened.prepare_region(bounds);self.assertEqual(reopened.query(bounds),original)
        self.assertEqual(len(http.calls),before);self.assertEqual(reopened.diagnostics['remote_scans'],0)

    def spread(self):
        import duckdb
        path=self.root/'spread.parquet';g=fixture.FIXTURE['row']['geometry'];w,s,e,n=geom._bbox(g)
        parent=dict(type='Polygon',coordinates=[[[w-.0001,s-.0001],[e+.0001,s-.0001],[e+.0001,n+.0001],[w-.0001,n+.0001],[w-.0001,s-.0001]]])
        shifted=dict(type='Polygon',coordinates=[[[x+.015,y] for x,y in g['coordinates'][0]]])
        with closing(duckdb.connect(config={'autoinstall_known_extensions':False,'autoload_known_extensions':False})) as con:
            con.execute('''CREATE TABLE b(id BIGINT,type VARCHAR,version INTEGER,timestamp VARCHAR,building VARCHAR,
                "building:part" VARCHAR,building_id VARCHAR,height VARCHAR,min_height VARCHAR,geometry BLOB,
                bbox STRUCT(xmin DOUBLE,ymin DOUBLE,xmax DOUBLE,ymax DOUBLE))''')
            for identity,shape,part,pid,height,low in ((1,parent,None,None,'12',None),(2,g,'yes','way/1','9','3'),(3,shifted,None,None,'6',None)):
                box=geom._bbox(shape)
                con.execute('INSERT INTO b VALUES (?,?,?,?,?,?,?,?,?,?,struct_pack(xmin:=?,ymin:=?,xmax:=?,ymax:=?))',
                    [identity,'way',2,'2026-10-28T00:57:56Z','yes',part,pid,height,low,fixture.wkb(shape),*box])
            con.execute('COPY b TO ? (FORMAT PARQUET)',[str(path)])
        fetch=netfetch.Fetcher(transport=object());self.addCleanup(fetch.close)
        http=fixture.ParquetHTTP(path.read_bytes());reader=lc.Reader(self.root/'ranges',fetch,transport=http)
        reader.select(explicit=True);self.addCleanup(reader.close)
        return reader,http,(w-.0002,s-.0002,e+.0002,n+.0002),(w+.0148,s-.0002,e+.0152,n+.0002)

    def test_new_neighborhood_reuses_full_footprints_parts_and_attributes(self):
        reader,http,a,b=self.spread();expected_a=reader.query(a);expected_b=reader.query(b)
        reader.prepare_region(a)
        self.assertEqual(reader.query(a),expected_a);self.assertEqual(len(expected_a.parts),1)
        self.assertEqual(expected_a.parts[0].parent_id,'osm:way:1')
        self.assertEqual(expected_a.parts[0].attrs['min_height'],3)
        reopened=self.clone(reader,http)
        with mock.patch.object(reopened,'_connection',side_effect=AssertionError('source decoded twice')):
            reopened.prepare_region(b);self.assertEqual(reopened.query(b),expected_b)
        self.assertEqual(expected_b.buildings[0].feature_id,'osm:way:3')

    def test_sparse_world_cell_uses_one_complete_job(self):
        reader,http,a,b=self.spread();session=world.start(self.root/'world')
        md=NS(cache_dir=self.root/'world',fetcher=reader.ranges.fetcher)
        def factory():return lc.Reader(reader.raw_dir,reader.ranges.fetcher,transport=http)
        provider=world.Provider(md,session,reader_factory=factory);provider._pin_source(reader.selected)
        key=mapcells.cell_of((a[1]+a[3])/2,(a[0]+a[2])/2);provider.prepare((key,))
        _,raw=provider.object(provider._state()['cells'][str(key)])
        self.assertEqual(raw['report']['partitions'],1)

    def test_capacity_deferred_index_uses_original_query_without_omissions(self):
        reader,http,bounds=self.reader(3);expected=reader.query(bounds)
        with mock.patch.object(cache.Index,'_room',side_effect=cache.Unavailable('test capacity')):
            reader.prepare_region(bounds)
        self.assertEqual(reader.query(bounds),expected);self.assertIn('index_deferred',reader.diagnostics)

    def test_corrupt_index_rejected_and_different_source_cannot_reuse_it(self):
        reader,http,bounds=self.reader();reader.prepare_region(bounds)
        index=reader._source_index;group=reader._indexed_groups[0];path=index._valid(group)
        with path.open('r+b') as f:f.seek(4096);f.write(b'broken')
        with self.assertRaises(fs.CorruptStorage):self.clone(reader,http).prepare_region(bounds)
        other=self.clone(reader,http);other.selected=dict(other.selected,last_modified='2026-11-02T00:00:00Z')
        with self.assertRaises(fs.CorruptStorage):other.prepare_region(bounds)

    def test_crash_between_database_and_marker_is_recovered(self):
        reader,http,bounds=self.reader();expected=reader.query(bounds)
        original=fs.OverlayStore._atomic_write
        def fail(store,path,body):
            if path.name=='0.json':raise OSError('simulated marker failure')
            return original(store,path,body)
        with mock.patch.object(fs.OverlayStore,'_atomic_write',fail),self.assertRaises(OSError):reader.prepare_region(bounds)
        reopened=self.clone(reader,http);reopened.prepare_region(bounds)
        self.assertEqual(reopened.query(bounds),expected)
        self.assertFalse(list(reopened._source_index.root.glob('*.work*')))

    def test_lost_marker_never_reclaims_a_published_database_with_uncertain_readers(self):
        reader,http,bounds=self.reader();reader.prepare_region(bounds);before=reader.query(bounds)
        index=reader._source_index;group=reader._indexed_groups[0];old=index._valid(group);body=old.read_bytes()
        (index.root/f'{group}.json').unlink()
        reopened=self.clone(reader,http);reopened.prepare_region(bounds)
        self.assertEqual(old.read_bytes(),body)
        self.assertNotEqual(reopened._source_index._valid(group),old)
        self.assertEqual(reopened.query(bounds),before)

    def processes(self,kill=False):
        reader,http,bounds=self.reader(100);body=(self.root/'fixture.parquet').read_bytes()
        ctx=mp.get_context('spawn');out=ctx.Queue();event=ctx.Event();workers=[]
        try:
            if kill:
                process=ctx.Process(target=build_worker,args=(reader.raw_dir,body,reader.selected,out,event));process.start();workers.append(process)
                self.assertTrue(event.wait(15));process.terminate();process.join(5);self.assertFalse(process.is_alive())
            for _ in range(4):
                process=ctx.Process(target=build_worker,args=(reader.raw_dir,body,reader.selected,out));process.start();workers.append(process)
            rows=[out.get(timeout=25) for _ in range(4)]
            self.assertTrue(all('error' not in r for r in rows),rows)
            self.assertTrue(all(r['ids']==rows[0]['ids'] and len(r['ids'])==100 for r in rows))
            self.assertEqual(sum(r['built'] for r in rows),1)
            for p in workers:p.join(5);self.assertFalse(p.is_alive())
        finally:
            for p in workers:
                if p.is_alive():p.terminate();p.join(3)
            out.close();out.join_thread()

    def test_four_processes_share_one_completed_index(self):self.processes()
    def test_forced_builder_termination_then_four_process_recovery(self):self.processes(True)

    def test_render_bounds_keep_required_density_and_drop_unused_halo(self):
        for lat,lon in ((30.412876,-9.5524592),(65.6839,-18.1105),(37.4,-118.4)):
            frame=geodesy.EnuFrame(lat,lon);box=(-128,-128,128,128)
            for kind in (None,'bldfar','skyline','bldsky'):
                w,s,e,n=bw.building_render_bounds(frame,box,kind)
                self.assertEqual(bw.building_cells(frame,box,kind),mapcells.cells_for_bbox(s,w,n,e))
                self.assertIn(mapcells.cell_of(lat,lon),bw.building_cells(frame,box,kind))
            if lon<-100:self.assertGreater(len(bw.building_cells(frame,box)),len(bw.building_cells(frame,box,'skyline')))

    def test_near_requests_go_first_and_distant_work_ages_to_equal_priority(self):
        store=data.Store(self.root/'queue');queue=priority.Queue(store);order=[]
        def wait(p):
            with priority.Queue(store).enter(p,time.monotonic()+5):order.append(p)
        ctx=mp.get_context('spawn');started=ctx.Event();release=ctx.Event()
        owner=ctx.Process(target=hold_lane,args=(self.root/'queue',started,release));owner.start()
        try:
            self.assertTrue(started.wait(5))
            with ThreadPoolExecutor(3) as pool:
                futures=[]
                for p in (2,1,0):
                    futures.append(pool.submit(wait,p));end=time.monotonic()+2
                    while len(list(queue.io.root.glob('*.json')))<len(futures) and time.monotonic()<end:time.sleep(.01)
                release.set()
                for f in futures:f.result(timeout=5)
            owner.join(5);self.assertFalse(owner.is_alive())
        finally:
            release.set()
            if owner.is_alive():owner.terminate();owner.join(3)
        self.assertEqual(order,[0,1,2]);self.assertFalse(list(queue.io.root.glob('*.json')))
        self.assertLess(priority.rank(dict(priority=2,created=0,token='old'),60),priority.rank(dict(priority=0,created=50,token='new'),60))

    def test_admission_exception_does_not_swallow_or_repeat_the_job(self):
        store=data.Store(self.root/'queue');queue=priority.Queue(store)
        with self.assertRaisesRegex(fs.LockTimeout,'inside source'):
            with queue.enter(0,time.monotonic()+1):raise fs.LockTimeout('inside source')
        self.assertFalse(list(queue.io.root.glob('*.json')))

    def test_concurrent_readers_share_one_checked_catalog_load(self):
        root=self.root/'state';session=world.start(root)
        p=world.Provider(NS(cache_dir=root),session)
        for owner,method,path,memo in ((p,'_state',p.path,'_state_memo'),
                (p.store,'index',p.store.root/'index.json','_index_memo')):
            setattr(owner,memo,None);store=p.store;original=store._read;calls=[]
            def read(value):
                if value==path:calls.append(value);time.sleep(.02)
                return original(value)
            with mock.patch.object(store,'_read',side_effect=read),ThreadPoolExecutor(8) as pool:
                values=list(pool.map(lambda _:getattr(owner,method)(),range(8)))
            self.assertEqual(len(calls),1);self.assertTrue(all(v==values[0] for v in values))

    def stream_setup(self):
        reader,http,a,b=self.spread();root=self.root/'stream';session=world.start(root)
        made=[]
        def factory():
            r=lc.Reader(reader.raw_dir,reader.ranges.fetcher,transport=http);made.append(r);return r
        provider=world.Provider(NS(cache_dir=root,fetcher=reader.ranges.fetcher),session,reader_factory=factory)
        provider._pin_source(reader.selected)
        key=mapcells.cell_of((a[1]+a[3])/2,(a[0]+a[2])/2)
        requested=[mapcells.CellKey(key.z,key.x+x,key.y+y) for x in (-1,0,1,2) for y in (-1,0,1,2)]
        token='a'*32;deadline=time.monotonic()+world.READ_BUDGET_S
        with provider.store._locked():
            provider.store._atomic_write(provider._claim_path(key),provider.store._document(dict(session=session.identity,
                cell=str(key),token=token,status='pending',owner=os.getpid(),expires=time.time()+world.READ_BUDGET_S+world.PUBLICATION_S)))
            batch=provider._batch_claims(key,requested,deadline)
        return provider,key,token,batch,made

    def test_bounded_queue_streams_sixteen_cells_one_at_a_time(self):
        provider,key,token,batch,made=self.stream_setup();events=[]
        self.assertEqual(len(batch),world.PREPARE_CELLS-1)
        head=provider._acquire(key,provider._state(),token=token,batch=batch,progress=events.append)
        self.assertEqual(len(made),1);self.assertEqual(len(provider.store.index()['cells']),16)
        self.assertEqual(provider.store.index()['cells'][str(key)],head)
        published=[e for e in events if e['phase']=='published'];self.assertEqual(len(published),16)
        first=next(i for i,e in enumerate(events) if e['phase']=='published')
        second=next(i for i,e in enumerate(events) if e['phase']=='cell_processing')
        self.assertLess(first,second)
        provider._release_batch(batch)
        with mock.patch.object(provider.store,'_atomic_write',wraps=provider.store._atomic_write) as write:
            provider.prepare((key,)+tuple(k for k,t in batch))
        self.assertEqual(sum(c.args[0]==provider.path for c in write.call_args_list),1)
        self.assertEqual(len(provider._state()['cells']),16);self.assertEqual(len(made),1)

    def test_unfinished_stream_defers_extras_and_keeps_primary_valid(self):
        provider,key,token,batch,made=self.stream_setup();events=[]
        head=provider._acquire(key,provider._state(),token=token,batch=batch,progress=events.append,deadline=time.monotonic()+20)
        self.assertEqual(len(provider.store.index()['cells']),1);provider.object(head)
        self.assertTrue(any(e['phase']=='batch_deferred' for e in events))
        provider._release_batch(batch)
        self.assertTrue(all(provider._claim(k)['expires']<time.time() for k,t in batch))

    def test_expired_queue_tickets_do_not_reclaim_building_objects(self):
        store=data.Store(self.root/'queue');queue=priority.Queue(store)
        held=store.objects/'held-evidence';held.write_bytes(b'retained')
        path=queue.io.root/('e'*32+'.json');now=time.time()
        queue.io._atomic_write(path,queue.io._document(dict(token='e'*32,priority=0,created=now-10,expires=now-1)))
        with queue.enter(1,time.monotonic()+1):self.assertFalse(path.exists())
        self.assertEqual(held.read_bytes(),b'retained')

    def test_owned_source_index_survives_download_cache_clear(self):
        import cache as downloads
        reader,http,bounds=self.reader();root=self.root/'owned';store=data.Store(root)
        owned=lc.Reader(root/'mapdata/raw',reader.ranges.fetcher,transport=http,index_dir=store.root/'source-rows')
        self.addCleanup(owned.close);owned.attach(reader.selected);owned.prepare_region(bounds)
        before=owned.query(bounds);files=list((store.root/'source-rows').rglob('*.sqlite'))
        self.assertTrue(files)
        disk=downloads.Cache(root,start=False)
        try:disk.scan();disk.clear()
        finally:disk.close()
        self.assertTrue(all(p.exists() for p in files))
        with mock.patch.object(http,'request',side_effect=AssertionError('unnecessary retrieval')):
            self.assertEqual(owned.query(bounds),before)

    def test_source_indexes_share_the_existing_raw_byte_admission(self):
        import buildingregions as jobs
        root=self.root/'quota';store=data.Store(root);ledger=jobs.Jobs(store)
        extra=store.root/'source-rows';extra.mkdir();(extra/'retained').write_bytes(b'x'*128)
        gate=jobs.Requests(ledger,ledger.read(),root/'mapdata/raw',lambda:100.,lambda:False,lambda s:None,extra_raw=extra)
        with mock.patch.object(jobs,'RAW_BYTES',128),self.assertRaises(fs.ResourceLimit):gate.admit(1)
        self.assertEqual(ledger.read()['next_request'],0.)


if __name__=='__main__':unittest.main()
