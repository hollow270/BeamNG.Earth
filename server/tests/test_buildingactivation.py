"""Prepared -> scheduled -> engine acknowledgment, with real local meshes and LuaJIT mocks."""
from copy import deepcopy
from dataclasses import replace
from concurrent.futures import Future
import itertools
import json
import os
from pathlib import Path
import sys
from types import MethodType,SimpleNamespace
import unittest
from unittest import mock

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import test_buildingmanual as manual
import test_buildingrefresh as regen
import test_buildingrender as fx
import buildingactivation as ba
import buildingrefresh as br
import earthd
import geotiles
import matslots
import roads
import vegetation


class LuaGame:
    def __init__(self):
        # Required local test dependency; absence is a failure, never silently skipped.
        from lupa.luajit21 import LuaRuntime,lua_type
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        self.lua_type=lua_type
        root=Path(__file__).resolve().parents[2]
        self.folder=root/'mod/beamng_earth/lua/ge/extensions'
        self.lua.globals().package.path=str(self.folder).replace('\\','/')+'/?.lua;'+self.lua.globals().package.path
        self.lua.globals().jsonEncode=lambda v:json.dumps(self.python(v),separators=(',',':'))
        self.lua.globals().jsonDecode=lambda v:self.table(json.loads(v))
        self.lua.execute((root/'server/tests/lua/buildingactivation_trace.lua').read_text())
        self.lua.globals().anchor=self.lua.eval("require('beamngEarth/geodesy').newAnchor")(fx.LAT,fx.LON)
        self.lua.globals().start('activation-test','e'*32)

    def python(self,v):
        if self.lua_type(v)=='table':
            keys=list(v.keys())
            if keys and set(keys)==set(range(1,len(keys)+1)):
                return [self.python(v[i]) for i in range(1,len(keys)+1)]
            return {str(k):self.python(v[k]) for k in keys}
        if isinstance(v,float) and v.is_integer(): return int(v)
        return v

    def table(self,v):
        if isinstance(v,dict): return self.lua.table_from({k:self.table(x) for k,x in v.items()})
        if isinstance(v,(list,tuple)): return self.lua.table_from([self.table(x) for x in v])
        return v

    def packet(self,p): return self.lua.globals().receive(self.table(p))
    def tick(self,n=20): self.lua.globals().tick(n)
    def last(self): return self.python(self.lua.globals().latest())


class ActivationTest(unittest.TestCase):
    setUp=fx.RenderTest.setUp
    md=fx.RenderTest.md
    close=regen.RefreshTest.close
    setup=manual.ManualTest.setup
    preview=manual.ManualTest.preview
    accept=manual.ManualTest.accept

    def prepared(self,originals=(),*,street=True,geo=False):
        conn=self.setup(originals,street=street)
        s=self.server
        s.sid='activation-test'
        s.out_dir=str(self.root/'session'); Path(s.out_dir).mkdir()
        s.vfs_dir='/temp/beamngEarth/'+s.sid
        s.slot_serial=itertools.count(1)
        s.slot_held=MethodType(earthd.Server.slot_held,s)
        s.slots_let_go=MethodType(earthd.Server.slots_let_go,s)
        s.matslots=matslots.MatSlots(s.sid,s.slot_held)
        for name in ('activate_building_overlays','building_activation_status'):
            setattr(s,name,MethodType(getattr(earthd.Server,name),s))
        conn.s=s; conn.slot_holds={}; conn.building_activation=1; conn.building_epoch='e'*32
        self.messages=[]
        def send(msg):
            manager=s.__dict__.get('_building_activation')
            if manager is not None:
                msg=manager.filter_message(conn,msg)
            if msg is not None:
                self.messages.append(deepcopy(msg))
        conn.send=send
        if geo:
            conn.roads=roads.GeoTracker(); conn.roads.aid=1
            la,lo,_=fx.FRAME.enu_to_geodetic(65,65,0)
            conn.roads.sent.add(geotiles.tile_of(float(la),float(lo)))
        result=self.accept(self.preview(enable=True))
        self.assertEqual(result['generation']['status'],'prepared')
        self.conn=conn
        self.publication=result['generation']['result']['publication']
        return conn

    def activate(self):
        r=self.server.activate_building_overlays(self.conn,self.publication,manual=True)
        self.assertNotEqual(r['state'],'rejected',r)
        return r

    def ack(self,result,status='activated',**changes):
        packet=next(m for m in reversed(self.messages) if m['type']=='building_activate')
        msg={k:packet[k] for k in ('sessionId','epoch','anchorId','seq','transaction')}
        msg.update(type='building_ack',status=status,detail='mock observed collision criterion')
        msg.update(changes)
        return self.server._building_activation.acknowledge(self.conn,msg)

    def test_new_building_is_scheduled_not_activated_until_ack(self):
        self.prepared()
        calls=len(self.transport.calls)
        r=self.activate()
        self.assertEqual(r['state'],'scheduled'); self.assertFalse(r['game_activated'])
        packet=self.messages[-1]
        self.assertIn('bld',packet['targets'][0]['slots'])
        self.assertIn('bldcol',packet['targets'][0]['slots'])
        self.assertEqual(len(self.transport.calls),calls)
        self.assertEqual(self.ack(r)['state'],'activated')
        self.assertTrue(self.server.building_activation_status(r['transaction'])['game_activated'])

    def test_replacement_disable_and_rollback_use_new_mesh_holds(self):
        self.prepared([fx.record()])
        first=self.activate(); self.ack(first)
        old=self.messages[-1]['targets'][0]['slots']['bld']
        self.store.disable()
        self.publication=self.server.refresh_building_overlays()['publication']
        second=self.activate(); self.ack(second)
        self.assertNotEqual(self.messages[-1]['targets'][0]['slots']['bld'],old)
        self.store.enable(); self.publication=self.server.refresh_building_overlays()['publication']
        third=self.activate(); self.ack(third)
        self.store.rollback(); self.publication=self.server.refresh_building_overlays()['publication']
        fourth=self.activate(); self.ack(fourth)
        self.assertEqual(self.server.building_activation_status(fourth['transaction'])['state'],'activated')

    def test_unchanged_resources_are_reused_without_new_slots(self):
        self.prepared()
        first=self.activate(); self.ack(first)
        made=self.server.matslots.made
        second=self.activate()
        self.assertTrue(all(t['reuse'] for t in self.messages[-1]['targets']))
        self.ack(second)
        self.assertEqual(self.server.matslots.made,made)

    def test_bad_catalog_missing_mesh_stale_state_and_settings_rejected(self):
        self.prepared()
        c=self.server._building_refresh
        path=c.path
        before=path.read_bytes()
        path.write_bytes(before.replace(b'"sha256"',b'"damage"',1))
        self.assertEqual(self.server.activate_building_overlays(self.conn,self.publication,manual=True)['state'],'rejected')
        path.write_bytes(before)
        entry=next(iter(c.outputs()['entries'].values()))
        mesh=c.io.objects/entry['generation']/next(n for n in entry['files'] if n.endswith('.cdae'))
        data=mesh.read_bytes(); mesh.unlink()
        self.assertEqual(self.server.activate_building_overlays(self.conn,self.publication,manual=True)['state'],'rejected')
        mesh.write_bytes(data)
        self.server.sidewalks=False
        self.assertEqual(self.server.activate_building_overlays(self.conn,self.publication,manual=True)['state'],'rejected')
        self.server.sidewalks=True; self.store.disable()
        self.assertEqual(self.server.activate_building_overlays(self.conn,self.publication,manual=True)['state'],'rejected')

    def test_wrong_session_anchor_epoch_sequence_and_malformed_acks(self):
        self.prepared(); result=self.activate()
        for changes in ({'sessionId':'old'},{'anchorId':9},{'epoch':'0'*32},{'seq':0},{'seq':True},
                        {'transaction':'x'},{'transaction':[]},{'transaction':{}},{'epoch':[]},
                        {'sessionId':{}},{'type':'wrong'},{'path':'/arbitrary'}):
            with self.subTest(changes=changes),self.assertRaises(ba.Rejected):
                self.ack(result,**changes)
        self.assertEqual(self.server.building_activation_status(result['transaction'])['state'],'scheduled')

    def test_disconnected_slots_remain_reserved_until_exact_release(self):
        self.prepared(); result=self.activate(); self.ack(result)
        manager=self.server._building_activation
        slot,hold=next(iter(manager.holds.items()))
        manager.context_changed(self.conn)
        self.server.connections=[]; self.conn.slot_holds.clear()
        self.assertTrue(self.server.slot_held(slot))
        self.assertNotIn(slot,self.server.matslots.free)
        self.assertFalse(manager.release(slot,hold[0]+1))
        self.assertTrue(manager.release(slot,hold[0]))
        self.assertIn(slot,self.server.matslots.free)

    def test_failing_ack_keeps_holds_until_game_reports_objects_gone(self):
        self.prepared(); r=self.activate()
        self.assertEqual(self.ack(r,'failed')['state'],'failed')
        manager=self.server._building_activation
        self.assertTrue(manager.holds)
        self.assertTrue(all(n not in self.server.matslots.free for n in manager.holds))

    def test_state_changed_during_install_is_superseded_not_current_success(self):
        self.prepared(); r=self.activate()
        self.store.disable()
        self.assertEqual(self.ack(r)['state'],'superseded')
        self.assertFalse(self.server.building_activation_status(r['transaction'])['game_activated'])

    def test_legacy_and_geotile_near_far_skyline_payloads(self):
        self.prepared(geo=True)
        self.add_lods(True)
        self.activate()
        self.assertEqual({t['kind'] for t in self.messages[-1]['targets']},{'near_geo','far_geo','skyline_geo'})

    def add_lods(self,geo):
        c=self.server._building_refresh
        req=br.TileRequest(**next(iter(c.outputs()['requests'].values())))
        la,lo,_=fx.FRAME.enu_to_geodetic(65,65,0)
        for kind,z,attr in (('far_geo',geotiles.FAR_Z,'bldfar'),('skyline_geo',geotiles.SKY_Z,'bldsky')):
            x,y=geotiles.tile_of(float(la),float(lo),z) if geo else (0,0)
            tr=vegetation.Tracker(kind=attr,geo=geo,z=z if geo else None)
            tr.aid=1; tr.sent.add((x,y)); setattr(self.conn,attr,tr)
        import buildingtargets
        targets=buildingtargets.discover(self.server,manual.bounds()).requests
        import blockworker
        for r in targets:
            for k in blockworker.building_cells(*r.geometry()):
                self.worker_md.cell(k,layers=('buildings','building_parts','roads'))
        self.publication=self.server.refresh_building_overlays(targets)['publication']

    def test_late_tracker_callbacks_cannot_change_newer_state(self):
        for tracker,geo in ((roads.Tracker(),False),(roads.GeoTracker(),True),
                            (vegetation.Tracker(kind='bldfar'),False),(vegetation.Tracker(kind='bldfar',geo=True),True)):
            tracker.aid=1; old,new=Future(),Future(); tracker.futs[(0,0)]=new; tracker.building.add((0,0))
            conn=SimpleNamespace(send=lambda m:self.fail('late callback sent a message'))
            if isinstance(tracker,roads.GeoTracker): tracker._built(conn,1,(0,0),old,True,True,{})
            elif isinstance(tracker,vegetation.Tracker) and geo: tracker._built_geo(conn,(0,0),old)
            else: tracker._built(conn,1,(0,0),old)
            self.assertIs(tracker.futs[(0,0)],new); self.assertIn((0,0),tracker.building)

    def test_no_manual_or_negotiated_capability_is_rejected(self):
        self.prepared()
        self.assertEqual(self.server.activate_building_overlays(self.conn,self.publication)['state'],'rejected')
        self.conn.building_activation=0
        self.assertEqual(self.server.activate_building_overlays(self.conn,self.publication,manual=True)['state'],'rejected')

    def test_real_lua_new_mesh_collision_observation_and_acknowledgment(self):
        self.prepared(); r=self.activate(); packet=self.messages[-1]
        game=LuaGame()
        self.assertTrue(game.packet(packet))
        self.assertEqual(game.last()['status'],'deferred')
        game.tick(1)
        self.assertNotEqual(game.last()['status'],'activated')
        game.tick(25)
        self.assertEqual(game.last()['status'],'activated',game.last())
        self.assertEqual(self.server._building_activation.acknowledge(self.conn,game.last())['state'],'activated')

    def test_real_lua_defers_stationary_nearby_vehicle_then_preserves_road_objects(self):
        self.prepared([fx.record()]); self.activate(); packet=self.messages[-1]
        game=LuaGame(); g=game.lua.globals()
        g.nearSeed('0_0',True); old=g.roadId()
        game.lua.execute('vehicles[1]:setPosition(vec3(65,65,0))')
        game.packet(packet); game.tick(20)
        self.assertEqual(game.last()['status'],'deferred'); self.assertEqual(g.roadId(),old)
        game.lua.execute('vehicles[1]:setPosition(vec3(450,65,0))')
        game.tick(25)
        self.assertEqual(game.last()['status'],'activated',game.last())
        self.assertEqual(g.roadId(),old)

    def test_lua_rejects_stale_order_session_and_oversized_targets(self):
        self.prepared(); self.activate(); packet=self.messages[-1]
        game=LuaGame()
        for changes in ({'sessionId':'old'},{'epoch':'0'*32},{'anchorId':2},{'seq':True},{'targets':[packet['targets'][0]]*33}):
            self.assertFalse(game.packet(dict(packet,**changes)))
        self.assertTrue(game.packet(packet)); game.tick(25)
        self.assertFalse(game.packet(dict(packet,seq=0,transaction='0'*32)))
        count=game.lua.globals().liveCount()
        self.assertTrue(game.packet(packet))
        self.assertEqual(game.lua.globals().liveCount(),count)

    def test_lua_missing_mesh_and_partial_reveal_failure_preserve_old_roads(self):
        self.prepared([fx.record()]); self.activate(); packet=self.messages[-1]
        for missing in (True,False):
            game=LuaGame(); g=game.lua.globals()
            g.nearSeed('0_0',True); old=g.roadId()
            path=packet['targets'][0]['slots']['bld'][0]
            if missing: g.missingPath=path
            else: g.failReveal=path
            game.packet(packet); game.tick(25)
            self.assertEqual(game.last()['status'],'failed',game.last())
            self.assertEqual(g.roadId(),old)

    def test_lua_syntax_including_core_local_variable_limit(self):
        game=LuaGame()
        for name in ('core.lua','roads.lua','buildingactivation.lua','buildingadapter.lua'):
            source=(game.folder/'beamngEarth'/name).read_text(encoding='utf-8')
            game.lua.execute('assert(loadstring(...))',source)

    def test_lua_other_vehicle_and_extra_collision_focus_block_activation(self):
        self.prepared(); self.activate(); packet=self.messages[-1]
        game=LuaGame()
        game.lua.execute("local v=createObject('BeamNGVehicle'); v:setPosition(vec3(65,65,0)); vehicles[2]=v")
        game.packet(packet); game.tick(15)
        self.assertEqual(game.last()['status'],'deferred')
        game.lua.execute('vehicles[2]=nil; roads.setFoci({{x=65,y=65,r=320}})')
        game.tick(5); self.assertEqual(game.last()['status'],'deferred')
        game.lua.execute('roads.setFoci({})')
        game.tick(25); self.assertEqual(game.last()['status'],'activated',game.last())

    def test_lua_old_collision_slot_is_not_released_before_reload_barrier(self):
        self.prepared([fx.record()]); self.activate(); packet=self.messages[-1]
        game=LuaGame(); g=game.lua.globals(); g.nearSeed('0_0',True)
        game.packet(packet)
        for _ in range(20):
            game.tick(1)
            if game.last()['status']=='installing': break
        self.assertEqual(game.last()['status'],'installing')
        gone=game.python(g.roads.takeSlotsGone('activation-test'))
        self.assertNotIn(90002,[x[0] for x in gone] if gone else [])
        g.simPaused=True; game.tick(20)
        gone=game.python(g.roads.takeSlotsGone('activation-test'))
        self.assertNotIn(90002,[x[0] for x in gone] if gone else [])
        g.simPaused=False; game.tick(1)
        gone=game.python(g.roads.takeSlotsGone('activation-test'))
        self.assertNotIn(90002,[x[0] for x in gone] if gone else [])
        game.tick(25)
        gone=game.python(g.roads.takeSlotsGone('activation-test'))
        self.assertIn(90002,[x[0] for x in gone])
        self.assertNotIn(90000,[x[0] for x in gone])

    def test_lua_unrelated_road_objects_keep_their_identity(self):
        self.prepared(); self.activate(); packet=self.messages[-1]
        game=LuaGame(); g=game.lua.globals()
        g.nearSeed('0_0',False); g.nearSeed('2_0',False)
        path='/temp/beamngEarth/activation-test/slot_90000.cdae'
        ids=game.python(g.shapeIds(path))
        game.packet(packet); game.tick(25)
        self.assertEqual(game.last()['status'],'activated',game.last())
        self.assertEqual(game.python(g.shapeIds(path)),ids)

    def test_lua_disconnect_then_fresh_epoch_retry(self):
        self.prepared(); result=self.activate(); packet=self.messages[-1]
        game=LuaGame(); game.packet(packet); game.tick(1)
        game.lua.globals().activation.disconnect()
        self.server._building_activation.context_changed(self.conn)
        self.conn.building_epoch='f'*32
        game.lua.globals().activation.hello('activation-test','f'*32)
        self.assertFalse(game.packet(packet))
        game.tick(10)
        next_result=self.activate(); self.assertGreater(next_result['seq'],result['seq'])
        game.packet(self.messages[-1]); game.tick(25)
        self.assertEqual(game.last()['status'],'activated',game.last())

    def test_lua_extension_reload_retains_objects_then_releases_after_safe_retirement(self):
        self.prepared(); self.activate(); packet=self.messages[-1]
        game=LuaGame(); game.packet(packet); game.tick(25)
        path=packet['targets'][0]['slots']['bld'][0]
        self.assertEqual(game.lua.globals().shapeCount(path),1)
        game.lua.globals().reloadRoads()
        self.assertEqual(game.lua.globals().shapeCount(path),1)
        game.lua.globals().roads.onDrop(game.table(dict(anchorId=1,key='0_0')))
        game.lua.execute('vehicles[1]:setPosition(vec3(5000,5000,0))')
        game.tick(40)
        gone=game.python(game.lua.globals().roads.takeSlotsGone('activation-test'))
        expected=packet['targets'][0]['slots']['bldcol'][1]
        self.assertTrue(any(x[1]==expected for x in gone),gone)

    def test_lua_server_restart_retains_road_and_session_until_safely_retired(self):
        self.prepared(); self.activate(); packet=self.messages[-1]
        game=LuaGame(); g=game.lua.globals(); g.nearSeed('0_0',False)
        game.packet(packet); game.tick(25); old=g.roadId()
        g.roads.setSession('new-server'); g.roads.newSession()
        game.tick(200)
        self.assertEqual(g.roadId(),old)
        self.assertIn('activation-test',game.python(g.roads.heldSessions()))
        game.lua.execute('vehicles[1]:setPosition(vec3(10000,65,0))')
        game.tick(10)
        self.assertIsNone(g.roadId())

    def test_mesh_copy_budget_fails_without_sending_or_releasing_live_files(self):
        self.prepared()
        with mock.patch.object(ba,'MAX_COPY_BYTES',1):
            result=self.server.activate_building_overlays(self.conn,self.publication,manual=True)
        self.assertEqual(result['state'],'rejected')
        self.assertFalse(any(m['type']=='building_activate' for m in self.messages))
        self.assertFalse(self.server._building_activation.holds)

    def test_real_lua_legacy_and_geo_batches_keep_lods_on_one_revision(self):
        self.prepared()
        for geo in (False,True):
            with self.subTest(geo=geo):
                if geo:
                    # A new fixture isn't needed: retain the old prepared outputs as
                    # unrelated registry rows, and explicitly replace the desired set.
                    self.conn.roads=roads.GeoTracker(); self.conn.roads.aid=1
                    la,lo,_=fx.FRAME.enu_to_geodetic(65,65,0)
                    self.conn.roads.sent.add(geotiles.tile_of(float(la),float(lo)))
                    self.conn.bldfar=self.conn.bldsky=None
                    import buildingtargets
                    reqs=tuple(r for r in buildingtargets.discover(self.server,manual.bounds()).requests if r.kind=='near_geo')
                    self.publication=self.server.refresh_building_overlays(reqs,replace_requests=True)['publication']
                self.add_lods(geo)
                r=self.activate(); packet=self.messages[-1]
                game=LuaGame(); g=game.lua.globals()
                near=next(t for t in packet['targets'] if t['kind'].startswith('near'))
                if geo:
                    g.roads.setGeo(True); g.roads.setFarGeo(True)
                    g.nearSeed(near['key'],False)
                else:
                    g.nearSeed('0_0',False)
                road_ids=game.python(g.shapeIds('/temp/beamngEarth/activation-test/slot_90000.cdae'))
                self.assertTrue(game.packet(packet)); game.tick(25)
                self.assertEqual(game.last()['status'],'activated',game.last())
                self.assertEqual(game.python(g.shapeIds('/temp/beamngEarth/activation-test/slot_90000.cdae')),road_ids)
                self.server._building_activation.acknowledge(self.conn,game.last())

    def test_raw_baseline_mutation_after_preparation_is_rejected_with_warm_lru(self):
        self.prepared([fx.record()])
        import numpy as np
        path=Path(self.base.path(fx.CELL,'buildings'))
        with np.load(path,allow_pickle=False) as z: arrays={k:z[k] for k in z.files}
        arrays['attr.height'][0]=99
        np.savez(path,**arrays)
        result=self.server.activate_building_overlays(self.conn,self.publication,manual=True)
        self.assertEqual(result['state'],'rejected')
        self.assertFalse(any(m['type']=='building_activate' for m in self.messages))

    def test_lua_duplicate_world_target_and_arbitrary_mesh_path_rejected(self):
        self.prepared(); self.activate(); p=deepcopy(self.messages[-1])
        game=LuaGame()
        other=deepcopy(p['targets'][0]); other['slot']='0'*64
        p['targets'].append(other)
        self.assertFalse(game.packet(p))
        p=deepcopy(self.messages[-1]); p['targets'][0]['slots']['bld'][0]='/levels/other/unsafe.cdae'
        self.assertFalse(game.packet(p))

    def test_lua_paused_physics_cannot_announce_collision_readiness(self):
        self.prepared(); self.activate(); packet=self.messages[-1]
        game=LuaGame(); game.lua.globals().simPaused=True
        game.packet(packet); game.tick(25)
        self.assertEqual(game.last()['status'],'deferred')
        game.lua.globals().simPaused=False; game.tick(25)
        self.assertEqual(game.last()['status'],'activated',game.last())

    def test_unrelated_registered_prepared_tile_is_not_materialized_or_changed(self):
        self.prepared()
        c=self.server._building_refresh
        req=br.TileRequest(**next(iter(c.outputs()['requests'].values())))
        unrelated=replace(req,x=12)
        self.publication=self.server.refresh_building_overlays([unrelated])['publication']
        old=deepcopy(c.outputs()['entries'][unrelated.slot])
        self.activate()
        self.assertNotIn(unrelated.slot,[r['slot'] for r in self.messages[-1]['targets']])
        self.assertEqual(c.outputs()['entries'][unrelated.slot],old)

    def test_real_lua_partial_failure_can_retry_without_refetch_or_reacceptance(self):
        self.prepared([fx.record()]); r=self.activate(); packet=self.messages[-1]
        revision=self.store.load().revision; calls=len(self.transport.calls)
        game=LuaGame(); g=game.lua.globals(); g.nearSeed('0_0',True); old=g.roadId()
        g.failReveal=packet['targets'][0]['slots']['bld'][0]
        game.packet(packet); game.tick(25)
        self.assertEqual(game.last()['status'],'failed')
        self.server._building_activation.acknowledge(self.conn,game.last())
        g.failReveal=None
        next_result=self.activate()
        self.assertGreater(next_result['seq'],r['seq'])
        self.assertFalse(any(t['reuse'] for t in self.messages[-1]['targets']))
        game.packet(self.messages[-1]); game.tick(25)
        self.assertEqual(game.last()['status'],'activated',game.last())
        self.assertEqual(g.roadId(),old)
        self.assertEqual(self.store.load().revision,revision)
        self.assertEqual(len(self.transport.calls),calls)

    def test_lua_partial_collision_object_construction_does_not_leak_objects(self):
        self.prepared([fx.record()]); self.activate(); packet=self.messages[-1]
        game=LuaGame(); g=game.lua.globals(); g.nearSeed('0_0',True); old=g.roadId()
        path=packet['targets'][0]['slots']['bldcol'][0]
        g.failRegisterPath=path
        game.packet(packet); game.tick(25)
        self.assertEqual(game.last()['status'],'failed',game.last())
        self.assertEqual(g.shapeCount(path),0)
        self.assertEqual(g.roadId(),old)

    def test_future_unprepared_lods_defer_buildings_but_preserve_road_data(self):
        self.prepared(); r=self.activate(); self.ack(r)
        manager=self.server._building_activation
        road={'shape':'unchanged-road','pos':[0,0,0]}
        message=dict(type='roads_tile',anchorId=1,key='1_0',path='unused',n=1,
            data=dict(chunks=[{'unchanged':'chunk'}],road=deepcopy(road),bld={'shape':'old-building','pos':[0,0,0]},
                      colliders={'bldcol':[[0,0,0,1,1,1]],'walk':[[2,2,0,3,3,1]]}))
        filtered=manager.filter_message(self.conn,message)
        self.assertTrue(filtered['data']['buildingPending'])
        self.assertNotIn('bld',filtered['data']); self.assertNotIn('bldcol',filtered['data']['colliders'])
        self.assertEqual(filtered['data']['road'],road)
        self.assertEqual(filtered['data']['chunks'],message['data']['chunks'])
        self.assertEqual(filtered['data']['colliders']['walk'],message['data']['colliders']['walk'])
        for kind in ('bldfar_tile','bldsky_tile'):
            output=manager.filter_message(self.conn,dict(type=kind,anchorId=1,key='0_0',data={'shape':'old','pos':[0,0,0]}))
            self.assertNotIn('shape',output['data']); self.assertTrue(output['data']['buildingPending'])
        outside=dict(type='bldfar_tile',anchorId=1,key='200_200',data={'shape':'unrelated','pos':[0,0,0]})
        self.assertIs(manager.filter_message(self.conn,outside),outside)
        self.assertTrue(manager.status(r['transaction'])['pending_targets'])

    def test_restart_recovers_guard_coverage_without_claiming_old_game_activation(self):
        self.prepared(); r=self.activate(); self.ack(r)
        old=self.server._building_activation
        restarted=SimpleNamespace(**vars(self.server)); restarted.sid='restarted-session'
        recovered=ba.Manager(restarted,self.server._building_refresh)
        self.assertEqual(recovered.guards,old.guards)
        self.assertFalse(recovered.transactions)
        output=recovered.filter_message(self.conn,dict(type='bldfar_tile',anchorId=1,key='0_0',data={'shape':'old','pos':[0,0,0]}))
        self.assertTrue(output['data']['buildingPending'])

    def test_lua_unprepared_geo_building_content_does_not_hide_far_fallback(self):
        game=LuaGame(); g=game.lua.globals(); g.roads.setGeo(True)
        key=geotiles.key(*geotiles.tile_of(fx.LAT,fx.LON))
        data=dict(chunks=[],buildingPending=True,h='pending',colliders={})
        message=dict(anchorId=1,key=key,data=data,frame=list(geotiles.centre(*geotiles.tile_of(fx.LAT,fx.LON))))
        self.assertTrue(g.roads.onTile(game.table(message),1,g.anchor))
        self.assertFalse(g.roads.nearShowsBuildings(key))

    def test_scheduled_unacknowledged_generation_already_persists_guards_and_session_pin(self):
        self.prepared(); result=self.activate()
        manager=self.server._building_activation
        self.assertEqual(result['state'],'scheduled')
        self.assertTrue((Path(self.server.out_dir)/earthd.BUILDING_SESSION_HOLD).is_file())
        restarted=SimpleNamespace(**vars(self.server)); restarted.sid='restart-without-ack'
        recovered=ba.Manager(restarted,self.server._building_refresh)
        self.assertEqual(recovered.guards,manager.guards)
        self.assertTrue(recovered.guards)

    def test_aged_pinned_session_is_never_released_or_swept_without_retirement_proof(self):
        folder=self.root/'older-session'; folder.mkdir()
        marker=folder/earthd.SESSION_MARKER; marker.write_text('session')
        (folder/earthd.BUILDING_SESSION_HOLD).write_text('unresolved ownership')
        os.utime(marker,(0,0))
        fake=SimpleNamespace(root=str(self.root),sid='new-session',orphans={},orphan_s=0,heartbeat_at=float('inf'),
            claimed_sessions=lambda:set(),_rmtree_session=lambda sid:self.fail('deleted pinned folder'),
            queue_delete=lambda paths:self.fail('queued pinned files for deletion'))
        earthd.Server._scan_sessions(fake)
        self.assertIn('older-session',fake.orphans)
        fake.orphans['older-session']=float('-inf')
        self.assertEqual(earthd.Server.sweep_sessions(fake,remove=False),[])
        earthd.Server.release_session(fake,'older-session')
        earthd.Server._rmtree_session(fake,'older-session')
        self.assertTrue(marker.is_file()); self.assertIn('older-session',fake.orphans)

    def test_empty_world_journals_mesh_ownership_before_creation_and_exposes_osm_credit(self):
        self.prepared(); self.activate()
        game=LuaGame(); g=game.lua.globals()
        self.assertTrue(game.packet(self.messages[-1])); game.tick(1)
        group=g.scenetree.findObject('bngEarthRoads')
        saved=group.getField(group,'buildingHolds',0)
        self.assertTrue(json.loads(saved))
        self.assertIn('OpenStreetMap contributors',g.roads.buildingAttribution())
        game.tick(30)
        self.assertEqual(game.last()['status'],'activated')

    def test_reuse_requires_existing_game_objects_and_mesh_files(self):
        self.prepared(); first=self.activate(); initial=deepcopy(self.messages[-1])
        game=LuaGame(); game.packet(initial); game.tick(30)
        self.assertEqual(game.last()['status'],'activated'); self.ack(first)
        self.activate(); reused=deepcopy(self.messages[-1])
        self.assertTrue(reused['targets'][0]['reuse'])
        path=initial['targets'][0]['slots']['bld'][0]
        for failure in ('object','file'):
            with self.subTest(failure=failure):
                game=LuaGame(); g=game.lua.globals(); game.packet(initial); game.tick(30)
                if failure=='object':
                    oid=game.python(g.shapeIds(path))[0]
                    obj=g.scenetree.findObjectById(oid); obj.delete(obj)
                else:
                    g.missingPath=path
                game.packet(reused); game.tick(10)
                self.assertEqual(game.last()['status'],'failed',game.last())

    def test_delayed_collision_reload_waits_two_simulation_updates_after_observation(self):
        self.prepared(); self.activate(); game=LuaGame(); g=game.lua.globals()
        game.lua.execute('heldReload=0; roads.buildingActivationHost().reloads=function() return heldReload end')
        game.packet(self.messages[-1]); game.tick(10)
        self.assertNotEqual(game.last()['status'],'activated')
        g.heldReload=1
        game.tick(1); self.assertNotEqual(game.last()['status'],'installing')
        game.tick(1); self.assertNotEqual(game.last()['status'],'installing')
        game.tick(1); self.assertEqual(game.last()['status'],'installing')
        game.tick(10); self.assertNotEqual(game.last()['status'],'activated')
        g.heldReload=2
        game.tick(1); self.assertNotEqual(game.last()['status'],'activated')
        game.tick(1); self.assertNotEqual(game.last()['status'],'activated')
        game.tick(1); self.assertEqual(game.last()['status'],'activated')

    def test_new_building_can_activate_in_an_explicitly_empty_geotile(self):
        self.prepared(geo=True)
        key=next(iter(self.conn.roads.sent))
        self.conn.roads.sent.clear(); self.conn.roads.empty.add(key)
        result=self.activate(); packet=self.messages[-1]
        self.assertTrue(packet['targets'][0]['allowEmpty'])
        game=LuaGame(); g=game.lua.globals(); g.roads.setGeo(True)
        game.packet(packet); game.tick(35)
        self.assertEqual(game.last()['status'],'activated',game.last())
        self.assertEqual(self.ack(result)['state'],'activated')
        self.assertEqual(g.shapeCount(packet['targets'][0]['slots']['bld'][0]),1)

    def test_near_base_streaming_must_complete_before_scheduling(self):
        self.prepared(); self.conn.roads.sent.clear(); self.conn.roads.empty.clear(); self.conn.roads.building.add((0,0))
        result=self.server.activate_building_overlays(self.conn,self.publication,manual=True)
        self.assertEqual(result['state'],'rejected')
        self.assertIn('finish normal world streaming',result['error']['message'])
        self.assertFalse(any(m['type']=='building_activate' for m in self.messages))
        self.conn.roads.building.clear(); self.conn.roads.sent.add((0,0))
        self.assertEqual(self.activate()['state'],'scheduled')


if __name__=='__main__':
    unittest.main()
