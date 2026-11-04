"""Combined OSM/graded-terrain scheduling and publication regressions. Offline only."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from functools import partial
from unittest import mock
import json,multiprocessing as mp,os,tempfile,threading,time,unittest
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import buildingworld as world,buildingoverlaystore as fs,osmbuildingcache as data
import buildingacquire,grading,plancache,roadwidth,mapdata,mapgeom,roads,overture,mapcells
import test_osmonly as fx
import test_buildingrender as render


def seed(directory,keys,features=()):
    store=data.Store(directory);world.start(directory)
    with store._locked():
        index=store.index()
        for key in keys:
            snapshot,report=data.normalize((fx.lc.Snapshot(key.bounds(),tuple(features),(),fx.sample.VERSION),))
            head=store.write(snapshot,fx.sample.VERSION,key.bounds(),report)
            index['cells'][str(key)]=head
            index['influence'][str(key)]=fs._union([key.bounds()]+[e.bounds for e in snapshot.entries])
            for entry in snapshot.entries:
                for f in (entry.feature,)+entry.parts:index['identities'].setdefault(f.feature_id,head)
        store._atomic_write(store.root/'index.json',store._document(index))


def terrain_worker(directory,session,ready,out,label,stalled=False,started=None):
    md=render.FixtureMapData(directory,street=True)
    md.pin_buildings(session);provider=md._building_world
    md.fetcher.offline_flag=False
    provider.runner=buildingacquire.Runner(partial(fx.task,stall=stalled,started=started))
    world.READ_BUDGET_S=8.
    try:
        if ready is not None:assert ready.wait(15)
        tick=time.monotonic()
        if stalled:
            target=mapcells.CellKey(render.CELL.z,render.CELL.x+30,render.CELL.y)
            try:provider.view((target,))
            except fs.LockTimeout:
                assert str(target) not in provider._state()['cells'] and not provider.errors
            else:raise AssertionError('stalled aggregate budget must defer')
            out.put(dict(label=label,seconds=time.monotonic()-tick,owned_timeout=True));return
        widths=roadwidth.RoadWidths(lambda k:md.cached_building_cell(k).layers['buildings'],roadwidth.store_dir(directory,md),
            dependency=lambda k:md.cached_building_dependencies((k,)).geometry_digest,prepare=md.prepare_buildings).get
        g=grading.Grader(render.FRAME,md,lambda la,lo:np.full(np.shape(la),10.),widths)
        os.environ[plancache.RUN_ENV]='combined-workers'
        g.shared=plancache.for_grader(directory,g,widths,None)
        la,lo,_=render.FRAME.enu_to_geodetic(np.linspace(0,256,24),np.linspace(0,256,24),0.)
        class Dem:
            def sample(self,la,lo,zoom):return np.full(np.shape(la),10.)
        # Actual GradedDem + shared grading plans, concurrently with ordinary warming.
        with ThreadPoolExecutor(2) as pool:
            warm=pool.submit(g.warm,render.CELL)
            md.warm(((render.CELL,'buildings'),))
            z=grading.GradedDem(Dem(),g).sample(la,lo,15)
            warm.result(timeout=8)
        assert np.isfinite(z).all()
        out.put(dict(label=label,seconds=time.monotonic()-tick,graded=True,
                     cells=len(provider._state()['cells']),lock=provider.store.lock_stats))
    except BaseException as e:out.put(dict(label=label,error=repr(e)))
    finally:md.close();md.fetcher.close()


class StreamingTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parents[2],prefix='.osmstream-')
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def provider(self):
        md=render.FixtureMapData(self.root/'cache',street=True)
        self.addCleanup(md.close);self.addCleanup(md.fetcher.close)
        md.fetcher.offline_flag=False
        p=world.Provider(md,world.start(md.cache_dir),reader_factory=lambda:fx.Reader(fx.source_features()))
        md.pin_buildings(p.session);md._building_world=p
        return md,p

    def test_water_routes_cannot_expand_the_building_request_to_the_canaries(self):
        # Actual bounds from the offending published water route (998 cells).
        ferry=np.array([[-6.8230581,37.1488311],[-9.55,30.41],[-16.2450467,28.4694531]])
        road=np.array(fx.source_features()[0].geometry['coordinates'][0])[:2]
        f=mapgeom.build('roads',mapgeom.text_column(['water-route','local-road']),
                        [(mapgeom.LINE,[[ferry]]),(mapgeom.LINE,[[road]])])
        f.attrs=dict(subtype=mapgeom.text_column(['water','road']),**{'class':mapgeom.text_column(['','residential'])},
                     cx=np.array([ferry[:,0].mean(),road[:,0].mean()]),cy=np.array([ferry[:,1].mean(),road[:,1].mean()]))
        f.rules=overture.empty('roads').rules
        expanded=set().union(*(roadwidth.wall_cells(r) for i in range(len(f)) for r in roads.ground_parts(f,i)))
        self.assertGreater(len(expanded),500)
        fixed=roadwidth.measurement_cells(f)
        self.assertLessEqual(len(fixed),4)
        self.assertNotIn(mapcells.CellKey(14,7452,6838),fixed)
        self.assertIn(render.CELL,fixed)
        md,p=self.provider()
        with mock.patch.object(md,'cell',return_value=NS(layers={'roads':f})),mock.patch.object(md,'prepare_buildings') as prepare:
            md.prepare_building_context((render.CELL,))
        self.assertEqual(set(prepare.call_args.args[0]),fixed)

    def test_cache_identity_is_read_only_and_requires_explicit_preparation(self):
        md,p=self.provider()
        with mock.patch.object(md,'cell',side_effect=AssertionError('cache key read roads')), \
             mock.patch.object(p,'_acquire',side_effect=AssertionError('cache key acquired OSM')):
            with self.assertRaises(fs.LockTimeout):md.building_context_revision((render.CELL,))
        md.prepare_building_context((render.CELL,))
        with mock.patch.object(md,'cell',side_effect=AssertionError('cache key read roads')), \
             mock.patch.object(p,'_acquire',side_effect=AssertionError('cache key acquired OSM')):
            a=md.building_context_revision((render.CELL,));b=md.building_context_revision((render.CELL,))
        self.assertEqual(a,b)

    def test_revisioned_widths_cannot_reuse_generic_building_dependent_road_facts(self):
        md,p=self.provider()
        class Widths:
            dependency=staticmethod(lambda k:'revision')
            def __init__(self,width):self.width=width;self.calls=0
            def get(self,fid,ll):self.calls+=1;return self.width,1.
        first,second=Widths(4.),Widths(5.)
        f=md.cell(render.CELL,layers=('roads',)).layers['roads']
        with mock.patch.object(roads,'_facts_load',side_effect=AssertionError('stale shared widths')), \
             mock.patch.object(roads,'_facts_store',side_effect=AssertionError('unversioned shared widths')):
            # Precompute the reusable, building-independent specs separately.
            with mock.patch.object(roads,'_facts_load',return_value=None),mock.patch.object(roads,'_facts_store'):
                roads.road_specs(f)
            a=roads.road_specs(f,widths=first.get)[0]
            b=roads.road_specs(f,widths=second.get)[0]
        self.assertEqual((first.calls,second.calls),(1,1))
        self.assertEqual((a.width,b.width),(4.,5.))

    def test_geometry_validation_and_serialization_do_not_hold_metadata_lock(self):
        md,p=self.provider();other=data.Store(md.cache_dir);other.lock_timeout=.05
        original=p.store._decode_object;checked=[]
        def validate(*a):
            with other._locked():checked.append(True)
            return original(*a)
        with mock.patch.object(p.store,'_decode_object',side_effect=validate):p.view((render.CELL,))
        self.assertTrue(checked)
        self.assertLess(p.store.lock_stats['max_hold_s'],1.)

    def test_pending_waiters_do_not_poll_the_global_writer_lock(self):
        md,p=self.provider();claim=dict(session=p.session.identity,cell=str(render.CELL),token='a'*32,
                                      status='pending',owner=os.getpid(),expires=time.time()+30)
        p.store._atomic_write(p._claim_path(render.CELL),p.store._document(claim))
        before=p.store.lock_stats['count']
        with mock.patch.object(world,'READ_BUDGET_S',.25),self.assertRaises(fs.LockTimeout):p.prepare((render.CELL,))
        self.assertEqual(p.store.lock_stats['count'],before)
        self.assertFalse(p._state()['cells'])

    def test_transient_publication_deferral_expires_only_its_token_and_retries(self):
        md,p=self.provider();original=p._publish
        with mock.patch.object(p,'_publish',side_effect=fs.LockTimeout('concurrent publication')):
            with self.assertRaises(fs.LockTimeout):p.prepare((render.CELL,))
        old=p._claim(render.CELL)
        self.assertLess(old['expires'],p.wall())
        self.assertFalse(p._state()['cells']);self.assertFalse(p.errors)
        with mock.patch.object(p,'_publish',wraps=original):p.prepare((render.CELL,))
        self.assertNotEqual(p._claim(render.CELL)['token'],old['token'])
        self.assertTrue(p.view_prepared((render.CELL,)).snapshot.entries)

    def test_aggregate_budget_is_deferral_not_source_failure_or_partial_choice(self):
        md,p=self.provider();p.reader_factory=None
        p.runner=buildingacquire.Runner(partial(fx.task,stall=True))
        with mock.patch.object(world,'READ_BUDGET_S',1.5),self.assertRaises(fs.LockTimeout):
            p.prepare((render.CELL,))
        self.assertFalse(p._state()['cells']);self.assertFalse(p.errors)
        self.assertLess(p._claim(render.CELL)['expires'],p.wall())
        p.reader_factory=lambda:fx.Reader(fx.source_features())
        p.prepare((render.CELL,))
        self.assertTrue(p.view_prepared((render.CELL,)).snapshot.entries)

    def test_cached_validation_notices_replaced_corrupted_metadata(self):
        md,p=self.provider();p.view((render.CELL,));p.store.index()
        (p.store.root/'index.json').write_text('{corrupt')
        with self.assertRaises(fs.CorruptStorage):p.store.index()
        p._state();p.path.write_text('{corrupt')
        with self.assertRaises(fs.CorruptStorage):p._state()

    @unittest.skipUnless(os.name=='nt','Windows atomic replacement sharing violations')
    def test_metadata_sharing_violation_retries_atomically_and_is_bounded(self):
        md,p=self.provider();path=p.store.root/'probe.json'
        p.store._atomic_write(path,b'previous')
        original=os.replace;attempts=[]
        error=PermissionError('temporary sharing violation');error.winerror=5
        def replace(a,b):
            attempts.append(True)
            self.assertEqual(path.read_bytes(),b'previous')
            if len(attempts)==1:raise error
            return original(a,b)
        with mock.patch.object(os,'replace',side_effect=replace):p.store._atomic_write(path,b'next')
        self.assertEqual(path.read_bytes(),b'next');self.assertEqual(len(attempts),2)
        tick=time.monotonic()
        with mock.patch.object(os,'replace',side_effect=error),self.assertRaises(PermissionError):
            p.store._atomic_write(path,b'never')
        self.assertLess(time.monotonic()-tick,1.)
        self.assertEqual(path.read_bytes(),b'next')
        self.assertFalse(list(path.parent.glob('.probe.json.*.tmp')))

    def test_concurrent_publication_cannot_overrun_object_byte_quota(self):
        md,p=self.provider();session=p.session;p._pin_source(fx.sample.VERSION)
        keys=[render.CELL,mapcells.CellKey(render.CELL.z,render.CELL.x+1,render.CELL.y)]
        providers=[world.Provider(md,session) for _ in keys];snapshots=[]
        for i,(q,key) in enumerate(zip(providers,keys)):
            f=replace(fx.source_features()[0],feature_id=f'osm:way:{i+10}',osm_ids=(f'osm:way:{i+10}',),
                      sources=tuple(dict(s,record_id=f'w{i+10}@2') for s in fx.source_features()[0].sources))
            snapshot,report=data.normalize((fx.lc.Snapshot(key.bounds(),(f,),(),fx.sample.VERSION),))
            snapshots.append((snapshot,report))
            q.store._atomic_write(q._claim_path(key),q.store._document(dict(session=session.identity,cell=str(key),token='a'*32,
                status='pending',owner=os.getpid(),expires=time.time()+30)))
        size=max(len(q.store.prepare(s,fx.sample.VERSION,k.bounds(),r).body) for q,k,(s,r) in zip(providers,keys,snapshots))+1024
        for q in providers:q.store.limits=replace(data.LIMITS,max_total_bytes=size)
        def publish(i):
            try:return providers[i]._publish(keys[i],'a'*32,*snapshots[i][:1],fx.sample.VERSION,snapshots[i][1])
            except fs.ResourceLimit:return None
        with ThreadPoolExecutor(2) as pool:got=list(pool.map(publish,range(2)))
        self.assertEqual(sum(v is not None for v in got),1)
        self.assertLessEqual(sum(f.stat().st_size for f in p.store.objects.iterdir()),size)
        for head in p.store.index()['cells'].values():p.store.object(head)

    def combined(self,kill):
        directory=self.root/'combined'
        keys=[mapcells.CellKey(render.CELL.z,render.CELL.x+dx,render.CELL.y+dy) for dx in (-1,0,1) for dy in (-1,0,1)]
        seed(directory,keys,fx.source_features());session=world.start(directory)
        ctx=mp.get_context('spawn');started=ctx.Event();out=ctx.Queue()
        owner=ctx.Process(target=terrain_worker,args=(str(directory),session,None,out,'owner',True,started))
        workers=[owner]
        try:
            owner.start();self.assertTrue(started.wait(15))
            if kill:owner.terminate();owner.join(3)
            for i in range(3):
                worker=ctx.Process(target=terrain_worker,args=(str(directory),session,started,out,f'grade-{i}'))
                worker.start();workers.append(worker)
            rows=[out.get(timeout=20) for _ in range(3 if kill else 4)]
            self.assertTrue(all('error' not in r for r in rows),rows)
            grades=[r for r in rows if r.get('graded')]
            self.assertEqual(len(grades),3)
            self.assertTrue(all(r['seconds']<6 for r in grades),rows)
            self.assertTrue(all(r['lock']['max_hold_s']<1 for r in grades),rows)
            for w in workers:w.join(5);self.assertFalse(w.is_alive())
            store=data.Store(directory)
            for head in store.index()['cells'].values():store.object(head)
            print('COMBINED_GRADED '+json.dumps(dict(killed_owner=kill,workers=rows)),flush=True)
        finally:
            for w in workers:
                if w.is_alive():w.terminate();w.join(3)
            out.close();out.join_thread()

    def test_four_workers_grade_and_warm_while_source_query_stalls(self):self.combined(False)
    def test_killed_acquisition_owner_does_not_block_grading_or_corrupt_cache(self):self.combined(True)


if __name__=='__main__':unittest.main()
