"""Four real spawned callers, stalled native-work substitute and killed owners."""
from dataclasses import replace
from functools import partial
import ast
import json
import os
import multiprocessing as mp
from pathlib import Path
import time
from types import SimpleNamespace as NS
import unittest
from unittest import mock

import buildingacquire
import buildingworld as world
import buildingprimary as primary
import mapcells
from test_buildingprimary import VERSION
import test_buildingrender as fx


def stalled(request,emit,started):
    emit(dict(phase='deliberately_stalled_query'))
    started.set()
    time.sleep(120)
    raise AssertionError('a stalled acquisition must not reach publication')


def complete_empty(request,emit):
    md=NS(cache_dir=request['cache'],release=request['release'],fetcher=NS(offline_flag=False))
    provider=world.Provider(md,request['session'])
    provider._pin_source(VERSION)
    emit(dict(phase='fixture_complete'))
    return world._empty(request['release']),VERSION,dict(mode='world_first_visit',coverage_complete=True,all_features_updated=True)


def caller(cache,release,session,key,task,ready,queue,label):
    world.READ_BUDGET_S=4.
    if ready is not None and not ready.wait(10):raise RuntimeError('owner did not start')
    md=NS(cache_dir=cache,release=release,fetcher=NS(offline_flag=False))
    provider=world.Provider(md,session,runner=buildingacquire.Runner(task))
    tick=time.monotonic()
    try:
        view=provider.view((key,))
        head=provider._state()['cells'][str(key)]
        report=provider.store._read(provider.store.objects/(head+'.json'))['report']
        queue.put(dict(label=label,seconds=time.monotonic()-tick,active=view.active,report=report))
    except BaseException as error:queue.put(dict(label=label,error=repr(error)))


class AcquisitionTest(unittest.TestCase):
    setUp=fx.RenderTest.setUp
    close=fx.RenderTest.close
    md=fx.RenderTest.md

    def setup_world(self):
        md=self.md();world.enable(md.cache_dir,policy_confirmed=True)
        descriptor=world.start(md.cache_dir,primary.Store(md.cache_dir).load())
        provider=world.Provider(md,descriptor)
        key=fx.CELL
        cached=mapcells.CellKey(key.z,key.x+10,key.y)
        with provider.store._locked():
            head=world._write_object(provider.store,world._empty(md.release),VERSION,cached.bounds(),
                    dict(mode='world_cached',coverage_complete=True,all_features_updated=True))
            index=provider._index();index['cells'][str(cached)]=head;index['influence'][str(cached)]=list(cached.bounds())
            provider.store._atomic_write(provider.store.root/'index.json',provider.store._document(index))
        return md,descriptor,provider,key,cached

    def exercise(self,kill_owner):
        md,descriptor,provider,key,cached=self.setup_world()
        ctx=mp.get_context('spawn');started=ctx.Event();queue=ctx.Queue()
        owner=ctx.Process(target=caller,args=(md.cache_dir,md.release,descriptor,key,
                          partial(stalled,started=started),None,queue,'owner'))
        processes=[owner]
        try:
            owner.start();self.assertTrue(started.wait(10),'real acquisition child started')
            if kill_owner:
                owner.terminate();owner.join(3)  # only this test's owned Python caller
            others=[('cached',cached,complete_empty),('same-cell',key,complete_empty),
                    ('unrelated',mapcells.CellKey(key.z,key.x+20,key.y),complete_empty)]
            for label,k,task in others:
                p=ctx.Process(target=caller,args=(md.cache_dir,md.release,descriptor,k,task,None,queue,label))
                p.start();processes.append(p)
            expected=3 if kill_owner else 4
            results=[queue.get(timeout=15) for _ in range(expected)]
            bylabel={r['label']:r for r in results}
            self.assertTrue(all('error' not in r for r in results),results)
            self.assertLess(bylabel['cached']['seconds'],1.5,'cached work must not queue behind network/DuckDB')
            self.assertLess(bylabel['same-cell']['seconds'],6)
            self.assertLess(bylabel['unrelated']['seconds'],6)
            self.assertFalse(bylabel['same-cell']['report']['coverage_complete'])
            before=provider._state()['cells'][str(key)]
            # Immutable fallback is not retried by a replacement caller.
            self.assertFalse(provider.view((key,)).active)
            self.assertEqual(provider._state()['cells'][str(key)],before)
            self.assertNotIn(str(key),provider._index()['cells'])
            for p in processes:
                p.join(3);self.assertFalse(p.is_alive())
            for digest in provider._state()['cells'].values():world._load_object(provider.store,digest,md.release)
            print('FOUR_WORKERS '+json.dumps(dict(killed_owner=kill_owner,results=results)),flush=True)
        finally:
            for p in processes:
                if p.is_alive():p.terminate();p.join(3)
            queue.close();queue.join_thread()

    def test_four_workers_stalled_query_does_not_hold_global_publication_lock(self):self.exercise(False)
    def test_forced_owner_termination_recovers_without_restarting_road_pool(self):self.exercise(True)

    def test_one_read_budget_covers_all_missing_cells_not_each_cell(self):
        md,descriptor,provider,key,cached=self.setup_world()
        provider.md.fetcher.offline_flag=True
        keys=[mapcells.CellKey(key.z,key.x+i,key.y) for i in range(5)]
        tick=time.monotonic();provider.view(keys)
        self.assertLess(time.monotonic()-tick,2)
        self.assertTrue(provider.ready(keys))

    def test_late_result_cannot_replace_frozen_fallback(self):
        md,descriptor,provider,key,cached=self.setup_world()
        token='a'*32
        with provider.store._locked():
            provider.store._atomic_write(provider._claim_path(key),provider.store._document(dict(
                session=descriptor.identity,cell=str(key),token=token,status='pending',owner=os.getpid(),expires=time.time()+30)))
        provider._pin_source(VERSION)
        provider._commit((key,),TimeoutError('reader chose fallback'))
        old=provider._state()['cells'][str(key)]
        provider._commit((key,),(world._empty(md.release),VERSION,dict(coverage_complete=True)),token)
        self.assertEqual(provider._state()['cells'][str(key)],old)
        self.assertNotIn(str(key),provider._index()['cells'])

    def test_pin_source_preserves_concurrent_choices_and_rejects_version_change(self):
        md,descriptor,provider,key,cached=self.setup_world()
        provider.view((cached,));head=provider._state()['cells'][str(cached)]
        provider._pin_source(VERSION)
        self.assertEqual(provider._state()['cells'][str(cached)],head)
        with self.assertRaisesRegex(ValueError,'source version changed'):
            provider._pin_source(dict(VERSION,etag='"different"'))

    def test_corrupt_or_incomplete_claim_fails_closed_without_query(self):
        md,descriptor,provider,key,cached=self.setup_world()
        claim=dict(session=descriptor.identity,cell=str(key),token='a'*32,status='chosen',owner=os.getpid(),expires=time.time()+30)
        for value in (claim,dict(claim,status='pending',expires='invalid')):
            provider.store._atomic_write(provider._claim_path(key),provider.store._document(value))
            with mock.patch.object(provider,'_acquire',side_effect=AssertionError('must not query')), \
                    self.assertRaises(world.fs.CorruptStorage):provider.view((key,))

    def test_render_view_never_holds_world_publication_mutex(self):
        md,descriptor,provider,key,cached=self.setup_world()
        original=provider._view
        def render(*args):
            # Another independent storage handle must acquire the OS mutex even
            # while this reader performs arbitrarily expensive render conversion.
            other=world._store(md.cache_dir);other.lock_timeout=.05
            with other._locked():pass
            return original(*args)
        with mock.patch.object(provider,'_view',side_effect=render):provider.view((cached,))

    def test_embedded_entrypoint_paths_do_not_add_frozen_bundle_parent(self):
        source=ast.parse(Path(world.__file__).with_name('earthd.py').read_text())
        index=next(i for i,node in enumerate(source.body) if isinstance(node,ast.Assign) and
                   any(isinstance(t,ast.Name) and t.id=='_server_path' for t in node.targets))
        code=compile(ast.Module(source.body[index:index+2],type_ignores=[]),'bootstrap','exec')
        entry=self.root/'app/server/earthd.py'
        for frozen in (False,True):
            fake=NS(frozen=frozen,path=['bundle-importer'])
            exec(code,dict(os=os,sys=fake,__file__=str(entry)))
            if frozen:self.assertEqual(fake.path,['bundle-importer'])
            else:self.assertTrue({str(entry.parent),str(entry.parent.parent)}<=set(fake.path))


if __name__=='__main__':unittest.main()
