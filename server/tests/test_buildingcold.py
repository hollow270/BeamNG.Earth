"""Cold-batch ownership, staging limits and real scheduler retry regressions."""
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
import asyncio,json,os,tempfile,time,unittest
import numpy as np
import buildingworld as world,buildingacquire,buildingwait,buildingoverlaystore as fs,osmbuildingcache as data
import layercakebuildings as lc,mapcells,geodesy,earthd
import roads
import test_osmonly as fx
import test_buildingrender as render

class BatchReader(fx.Reader):
    scans=0
    def __init__(self,features):super().__init__(features);self.diagnostics={}
    def prepare_cells(self,keys):
        BatchReader.scans+=1;self.diagnostics['batch_cells']=len(keys);return keys

class ColdStreamingTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parents[2],prefix='.cold-stream-')
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.md=render.FixtureMapData(self.root/'cache',street=True)
        self.addCleanup(self.md.close);self.addCleanup(self.md.fetcher.close);self.md.fetcher.offline_flag=False
        self.session=world.start(self.md.cache_dir);self.md.pin_buildings(self.session);self.p=self.md._building_world
        c=render.CELL;self.keys=tuple(mapcells.CellKey(c.z,c.x+x,c.y+y) for x in (0,1) for y in (0,1))
        self.features=[]
        base=fx.source_features()[0]
        for i,k in enumerate(self.keys):
            identity=f'osm:way:{i+20}'
            f=replace(base,feature_id=identity,osm_ids=(identity,),geometry=render.polygon(0,0,frame=geodesy.EnuFrame(*k.center())),
                sources=tuple(dict(s,record_id=f'w{i+20}@2') for s in base.sources))
            self.features.append(f)
        self.requests=[];BatchReader.scans=0
    def runner(self):
        outer=self
        class Local:
            def run(self,request,deadline,progress):
                outer.requests.append(request)
                p=world.Provider(outer.md,outer.session,reader_factory=lambda:BatchReader(outer.features))
                return p._acquire(request['key'],p._state(),progress=progress,token=request['token'],batch=request['batch'])
        return Local()
    def test_one_scan_publishes_four_requested_cells_and_pins_every_identity(self):
        with mock.patch.object(buildingacquire,'Runner',side_effect=self.runner):self.p.prepare(self.keys)
        self.assertEqual(len(self.requests),1);self.assertEqual(len(self.requests[0]['batch']),3)
        self.assertEqual(BatchReader.scans,1)
        self.assertEqual(len(self.p.store.index()['cells']),4);self.assertEqual(len(self.p._state()['cells']),4)
        self.assertEqual(len(self.p.view_prepared(self.keys).snapshot.entries),4)
        self.assertTrue(all(self.p._claim(k)['status']=='chosen' for k in self.keys))
    def test_legacy_authorization_is_read_only_and_does_not_import_geometry(self):
        path=Path(self.md.cache_dir)/'osm-primary/v1/worldwide.json';path.parent.mkdir(parents=True)
        body=self.p.store._document(dict(version=1,source=lc.URL,policy_confirmed=True));path.write_bytes(body)
        self.assertTrue(world.configured(self.md.cache_dir));self.assertEqual(path.read_bytes(),body)
        self.assertEqual([p.name for p in path.parent.iterdir()],['worldwide.json'])
        self.assertFalse(self.p.store.index()['cells'])
    def test_failed_batch_releases_own_unfinished_claims_and_can_retry(self):
        runner=NS(run=mock.Mock(side_effect=fs.LockTimeout('publication busy')))
        with mock.patch.object(buildingacquire,'Runner',return_value=runner),self.assertRaises(fs.LockTimeout):self.p.prepare(self.keys)
        claims={k:self.p._claim(k) for k in self.keys}
        self.assertTrue(all(v['expires']<time.time() for v in claims.values()));self.assertFalse(self.p._state()['cells'])
        with mock.patch.object(buildingacquire,'Runner',side_effect=self.runner):self.p.prepare(self.keys)
        self.assertEqual(len(self.p._state()['cells']),4)
        self.assertTrue(all(self.p._claim(k)['token']!=claims[k]['token'] for k in self.keys))
    def test_other_live_owner_is_not_stolen_by_a_batch(self):
        k=self.keys[-1]
        self.p.store._atomic_write(self.p._claim_path(k),self.p.store._document(dict(session=self.session.identity,cell=str(k),token='a'*32,status='pending',owner=os.getpid(),expires=time.time()+30)))
        with mock.patch.object(buildingacquire,'Runner',side_effect=self.runner):self.p.prepare(self.keys[:-1])
        self.assertEqual(self.p._claim(k)['token'],'a'*32);self.assertNotIn(str(k),self.p._state()['cells'])
    def test_normalized_batch_budget_defers_cells_without_omitting_them(self):
        with mock.patch.object(data,'MAX_FEATURES',1),mock.patch.object(buildingacquire,'Runner',side_effect=self.runner):self.p.prepare(self.keys[:2])
        self.assertEqual(len(self.p.store.index()['cells']),2);self.assertEqual(len(self.requests),2)
    def test_dense_staging_falls_back_to_primary_without_increasing_limits(self):
        reader=object.__new__(lc.Reader);reader.diagnostics={};reader._region=None
        con=mock.Mock();reader._connection=lambda:(con,None,None);calls=[]
        def prepare(bounds,span):
            calls.append(span)
            if span==6000:raise fs.ResourceLimit('source staging feature capacity')
        reader._prepare_region=prepare
        chosen=reader.prepare_cells(self.keys)
        self.assertEqual(chosen,self.keys[:1]);self.assertEqual(calls,[6000,3000])
        con.execute.assert_called_once_with('DROP TABLE IF EXISTS lc_region')
    def test_warm_buildings_do_not_start_acquisition_or_claim_inputs(self):
        with mock.patch.object(self.p,'_acquire',side_effect=AssertionError('speculative acquisition')):
            self.md.warm(((k,'buildings') for k in self.keys));self.md._warm_ex.shutdown(wait=True)
        self.assertFalse(self.p._state()['cells']);self.assertFalse(list((self.p.store.root/'claims').glob('*.json')))
    def test_regroup_cannot_restart_a_tile_after_its_retry_budget_expires(self):
        conn=object.__new__(earthd.Connection);conn.active=1;conn.ck=lambda k:(1,k)
        conn.queue=[NS(key='failed'),NS(key='ready')];conn.qi=0
        conn.sent={};conn.inflight={};conn.desired={'failed','ready'}
        conn.building_failed={'failed'};conn.building_wait=buildingwait.Pending()
        self.assertEqual(conn._next_tile().key,'ready');self.assertIsNone(conn._next_tile())
    def test_road_progress_does_not_count_waiting_inputs_as_delivered(self):
        tracker=roads.GeoTracker();key=(20,30);tracker.car={key}
        tracker.retry[key]=time.monotonic()+3;tracker.input_wait.add(key)
        self.assertEqual(tracker.progress(),(0,1))
        tracker.sent.add(key);tracker.skins.add(key);tracker.input_wait.clear()
        self.assertEqual(tracker.progress(),(1,1))
    def test_effective_layers_reuse_the_bounded_lru_and_session_dependency(self):
        key=self.keys[0]
        with mock.patch.object(buildingacquire,'Runner',side_effect=self.runner):self.p.prepare((key,))
        with mock.patch.object(self.p,'view_prepared',wraps=self.p.view_prepared) as read:
            a=self.md.cached_building_cell(key)
            b=self.md.building_cell(key,layers=('buildings',))
            self.assertIs(a.layers['buildings'],b.layers['buildings']);self.assertEqual(read.call_count,1)
            self.assertEqual(a.versions,b.versions);self.assertEqual(a.provenance,b.provenance)
            self.md._mem.clear();self.md._mem_bytes=0
            c=self.md.cached_building_cell(key)
            self.assertIsNot(a.layers['buildings'],c.layers['buildings']);self.assertEqual(read.call_count,2)
            self.assertEqual(a.digest(),c.digest())
    @unittest.skipUnless(os.name=='nt','Windows metadata handles')
    def test_open_metadata_reader_delays_only_rename_then_publication_completes(self):
        path=self.p.store.root/'sharing.json';self.p.store._atomic_write(path,b'original')
        with ThreadPoolExecutor(1) as pool:
            with self.p.store._open_read(path) as reader:
                result=pool.submit(self.p.store._atomic_write,path,b'replaced')
                time.sleep(.04);self.assertFalse(result.done())
                self.assertEqual(reader.read(),b'original')
            result.result(timeout=2)
        self.assertEqual(path.read_bytes(),b'replaced')
    def test_retry_queue_is_delayed_bounded_and_discards_stale_anchors(self):
        clock=[0.];q=buildingwait.Pending(clock=lambda:clock[0]);key=(1,'tile')
        self.assertTrue(q.defer(key,'job'));self.assertTrue(q.delayed(key));self.assertEqual(q.ready(lambda k:True),[])
        clock[0]=3.;self.assertEqual(q.ready(lambda k:True),['job'])
        self.assertTrue(q.defer(key,'job'));self.assertEqual(q.timeout(),6.)
        self.assertEqual(q.ready(lambda k:False),[]);self.assertFalse(q);self.assertFalse(q.age)
        q.defer(key,'job');clock[0]+=buildingwait.LIMIT;self.assertFalse(q.defer(key,'job'));self.assertFalse(q)
    def test_actual_terrain_future_retries_transient_and_coalesces_callers(self):
        async def run():
            pool=ThreadPoolExecutor(1);anchor=NS(id=1);calls=[]
            server=object.__new__(earthd.Server);server.hm={};server.hm_inflight={};server.hm_pool=pool;server.anchors={1:anchor}
            def make(*a):
                calls.append(True)
                if len(calls)==1:raise fs.LockTimeout('source batch pending')
                return dict(done=True)
            server.make_heightmap=make
            def done(key,f):
                server.hm_inflight.pop(key,None)
                if not f.cancelled() and f.exception() is None:server.hm[key]=f.result()
            server._hm_done=done
            try:
                with mock.patch.object(buildingwait,'DELAY',.01):
                    result=await asyncio.gather(server.terrain(anchor,0,0),server.terrain(anchor,0,0))
                self.assertEqual(result,[dict(done=True)]*2);self.assertEqual(len(calls),2)
            finally:pool.shutdown()
        asyncio.run(run())

if __name__=='__main__':unittest.main()
