"""Anchor/tracker lifecycle restoration through actual orchestration and native workers."""
import asyncio
from copy import copy,deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import test_buildinglive as fx
import test_buildingrender as render
import test_buildingmanual as manual
import buildingactivation as activation
import buildingrefresh as regeneration
import buildinglive as live
import roads,vegetation
import geodesy
import mapcells
from test_buildingactivation import LuaGame


class RestorationTest(unittest.TestCase):
    setUp=fx.LiveTest.setUp
    md=fx.LiveTest.md
    close=fx.LiveTest.close
    setup=fx.LiveTest.setup
    start=fx.LiveTest.start
    command=fx.LiveTest.command
    until=fx.LiveTest.until
    preview=fx.LiveTest.preview
    accept=fx.LiveTest.accept
    activate_game=fx.LiveTest.activate_game

    async def initial(self,originals=()):
        await self.start(originals,street=True)
        return await self.activate_game(await self.accept(await self.preview()))

    async def reanchor(self,aid,*,settled=True):
        anchor=copy(self.server.anchors[1]); anchor.id=aid; self.server.anchors[aid]=anchor
        self.conn.active=aid; self.service.context_changed(self.conn)
        if self.service.context_task: await self.service.context_task
        if settled:
            self.conn.roads=roads.Tracker(); self.conn.roads.aid=aid
            self.conn.roads.car={(0,0)}; self.conn.roads.empty={(0,0)}

    def packets(self,start=0):
        return [m for m in self.messages[start:] if m['type'] in ('building_activate','building_activate_batch')]

    def test_same_location_native_far_and_skyline_revisit_includes_actual_meshes(self):
        async def scenario():
            await self.start([render.record(w=60,d=60,attrs={'height':40,'class':'retail'})],street=True)
            w,s,e,n=manual.bounds(-2000,-2000,4000,4000)
            for key in mapcells.cells_for_bbox(s,w,n,e):
                self.worker_md.cell(key,layers=('buildings','building_parts','roads'))
            for name in ('bldfar','bldsky'):
                tracker=vegetation.Tracker(kind=name); tracker.aid=1
                tracker.car={(0,0)}; tracker.sent={(0,0)}; setattr(self.conn,name,tracker)
            try:
                await self.activate_game(await self.accept(await self.preview()))
                await self.reanchor(2,settled=False)
                for name in ('roads','bldfar','bldsky'): getattr(self.conn,name).aid=2
                await self.until(lambda:self.service.current['state']=='activation_scheduled' and not self.service.busy)
                packet=self.packets()[-1]
                self.assertTrue(packet['osm'])
                self.assertEqual({t['kind'] for t in packet['targets']},{'near','far','skyline'})
                self.assertTrue(all(t['slots'] for t in packet['targets']))
                await self.ack_current()
                self.assertTrue(self.service.current['game_activated'])
                self.assertEqual(len(self.transport.calls),1)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_missing_mesh_reference_is_not_a_valid_generation_despite_intact_listed_json(self):
        directory=self.root/'generation'; directory.mkdir()
        (directory/'tile.json').write_text(json.dumps({'shape':'@BUILDING_OVERLAY@/test/mesh.cdae'}))
        with self.assertRaises(regeneration.IncompleteGeneration):
            regeneration.scan(directory,'@BUILDING_OVERLAY@/test',regeneration.Limits())
        (directory/'mesh.cdae').write_bytes(b'synthetic mesh')
        self.assertIn('mesh.cdae',regeneration.scan(directory,'@BUILDING_OVERLAY@/test',regeneration.Limits())[0])

    async def ack_current(self):
        m=self.packets()[-1]
        await self.service.acknowledge(self.conn,dict(type='building_ack',
            **{key:m[key] for key in ('sessionId','epoch','anchorId','seq','transaction')},
            status='activated',detail='TEST ONLY simulated engine acknowledgement'))

    def test_same_location_tracker_anchor_reset_without_key_change_recovers_automatically(self):
        async def scenario():
            await self.initial()
            try:
                await self.reanchor(2,settled=False)
                await self.until(lambda:self.service.current.get('restoration',{}).get('code')=='trackers_settling')
                before=len(self.packets())
                await asyncio.sleep(.6); self.assertEqual(len(self.packets()),before)
                # Same keys, only the delayed tracker's anchor changes.
                self.conn.roads.aid=2
                await self.until(lambda:self.service.current['state']=='activation_scheduled' and not self.service.busy)
                self.assertEqual(self.packets()[-1]['anchorId'],2)
                await self.ack_current()
                self.assertTrue(self.service.current['game_activated'])
                self.assertEqual(len(self.transport.calls),1)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_near_membership_completion_with_identical_union_wakes_restoration_without_regeneration_loop(self):
        async def scenario():
            await self.initial()
            try:
                await self.reanchor(2)
                self.conn.roads.empty.clear(); self.conn.roads.building={(0,0)}
                with mock.patch.object(self.server,'refresh_building_overlays',wraps=self.server.refresh_building_overlays) as generate:
                    await self.until(lambda:self.service.current.get('restoration',{}).get('code')=='near_streaming')
                    await asyncio.sleep(.6); self.assertEqual(generate.call_count,0)
                    self.conn.roads.building.clear(); self.conn.roads.empty.add((0,0))
                    await self.until(lambda:self.service.current['state']=='activation_scheduled' and not self.service.busy)
                    self.assertEqual(generate.call_count,1)
                    await asyncio.sleep(.6); self.assertEqual(generate.call_count,1)
                await self.ack_current()
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_precise_final_rejection_when_streaming_adds_a_target_after_preparation_then_recovers(self):
        async def scenario():
            await self.initial(); original=self.service.call; injected=False; seen=[]
            async def moved(conn,fn):
                nonlocal injected
                result=await original(conn,fn)
                if fn.__name__=='prepare' and not injected:
                    injected=True
                    self.conn.bldfar=vegetation.Tracker(kind='bldfar'); self.conn.bldfar.aid=2
                    self.conn.bldfar.sent={(0,0)}; self.conn.bldfar.car={(0,0)}
                return result
            public_error=live._public_error
            def record(error):
                seen.append((type(error).__name__,str(error),getattr(error,'code',None)))
                return public_error(error)
            try:
                await self.reanchor(2)
                with mock.patch.object(self.service,'call',moved),mock.patch.object(live,'_public_error',record):
                    await self.until(lambda:self.service.current['state']=='activation_scheduled' and not self.service.busy)
                self.assertIn(('Rejected','prepared targets/settings do not match the current world','targets_changed'),seen)
                self.assertEqual(self.packets()[-1]['anchorId'],2)
                self.assertEqual(self.packets()[-1]['seq'],2,'rejection allocates no extra activation transaction')
                await self.ack_current(); self.assertEqual(len(self.transport.calls),1)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_permanent_overlay_failure_activates_baseline_without_changing_enabled_store_or_roads(self):
        async def scenario():
            await self.start([render.record(attrs={'height':30,'class':'retail','roof_shape':'flat'})],street=True)
            game=LuaGame(); game.lua.globals().nearSeed('0_0',True)
            await self.activate_game(await self.accept(await self.preview()),game)
            road=game.lua.globals().roadId(); old=self.packets()[-1]['targets'][0]['slots']['bldcol'][0]
            saved=manual.tree(self.store.root); revision=self.store.load().revision
            manager=self.service.authority; original=manager.activate
            def failed_overlay(*args,**kwargs):
                if not isinstance(manager.store,live._BaselineView):
                    raise activation.Rejected('prepared inputs or artifact checksums no longer match')
                return original(*args,**kwargs)
            try:
                # Refence the same frame, as on resync: existing objects remain.
                self.service.context_changed(self.conn)
                with mock.patch.object(manager,'activate',failed_overlay):
                    await self.until(lambda:self.service.current['state']=='activation_scheduled' and not self.service.busy)
                    packet=self.packets()[-1]
                    self.assertFalse(packet['osm'])
                    self.assertNotIn('road',packet['targets'][0]['data'])
                    self.assertNotIn('walk',packet['targets'][0]['data'])
                    await self.activate_game([packet],game)
                self.assertEqual(self.service.current['state'],'activated')
                self.assertFalse(self.service.current['activated_enabled'])
                self.assertEqual(self.service.current['restoration']['mode'],'baseline')
                self.assertTrue(self.store.load().enabled); self.assertEqual(self.store.load().revision,revision)
                self.assertEqual(manual.tree(self.store.root),saved)
                self.assertEqual(game.lua.globals().roadId(),road)
                self.assertEqual(game.lua.globals().shapeCount(old),0)
                new=packet['targets'][0]['slots']['bldcol'][0]
                self.assertEqual(game.lua.globals().shapeCount(new),1)
                self.assertEqual(len(self.transport.calls),1)
                count=len(self.packets()); await asyncio.sleep(1.1); self.assertEqual(len(self.packets()),count)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_failed_baseline_is_explicitly_blocked_and_does_not_loop(self):
        async def scenario():
            await self.initial()
            try:
                manager=self.service.authority
                self.service.context_changed(self.conn)
                with mock.patch.object(manager,'activate',side_effect=activation.Rejected('prepared inputs or artifact checksums no longer match')) as activate:
                    await self.until(lambda:self.service.current.get('restoration',{}).get('mode')=='blocked' and not self.service.busy)
                    count=activate.call_count
                    self.assertEqual(count,2)
                    await asyncio.sleep(1.1); self.assertEqual(activate.call_count,count)
                self.assertFalse(self.service.current['game_activated']); self.assertTrue(self.store.load().enabled)
                self.assertIn('Stop',self.service.current['restoration']['message'])
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_repeated_anchor_changes_disconnect_and_old_ack_are_fenced(self):
        async def scenario():
            await self.initial(); old=deepcopy(self.packets()[-1])
            try:
                await self.reanchor(2,settled=False)
                await self.reanchor(3,settled=False)
                self.conn.roads.aid=3
                await self.until(lambda:self.service.current['state']=='activation_scheduled' and not self.service.busy)
                self.assertEqual(self.packets()[-1]['anchorId'],3)
                await self.service.acknowledge(self.conn,dict(type='building_ack',
                    **{k:old[k] for k in ('sessionId','epoch','anchorId','seq','transaction')},status='activated',detail='old frame'))
                self.assertFalse(self.service.current['game_activated'])
                self.service.context_changed(self.conn,disconnect=True)
                count=len(self.packets()); await asyncio.sleep(.6); self.assertEqual(len(self.packets()),count)
                self.assertIsNone(self.service.owner)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_outside_region_then_reentry_uses_local_data_and_no_unrelated_activation(self):
        async def scenario():
            await self.initial()
            try:
                await self.reanchor(2)
                self.server.anchors[2].lat+=.2; self.server.anchors[2].lon+=.2
                self.server.anchors[2].frame=geodesy.EnuFrame(self.server.anchors[2].lat,self.server.anchors[2].lon)
                self.conn.roads.empty={(100,100)}; self.conn.roads.car={(100,100)}
                count=len(self.packets())
                await self.until(lambda:self.service.current['state']=='accepted' and not self.service.busy)
                self.assertEqual(len(self.packets()),count)
                await self.reanchor(3)
                await self.until(lambda:self.service.current['state']=='activation_scheduled' and not self.service.busy)
                self.assertEqual(self.packets()[-1]['anchorId'],3)
                await self.ack_current(); self.assertEqual(len(self.transport.calls),1)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_stable_transient_failures_have_three_attempts_then_one_validated_fallback(self):
        async def scenario():
            await self.initial(); manager=self.service.authority; original=manager.activate
            calls=[]
            def reject_overlay(*args,**kw):
                baseline=isinstance(manager.store,live._BaselineView); calls.append(baseline)
                if not baseline: raise activation.Rejected('prepared targets/settings do not match the current world')
                return original(*args,**kw)
            try:
                await self.reanchor(2)
                with mock.patch.object(manager,'activate',reject_overlay):
                    await self.until(lambda:self.service.current['state']=='activation_scheduled' and not self.service.busy)
                    self.assertEqual(calls,[False,False,False,True])
                    await self.ack_current()
                    await asyncio.sleep(.6); self.assertEqual(len(calls),4)
                self.assertFalse(self.service.current['activated_enabled'])
                self.assertTrue(self.store.load().enabled)
                self.assertEqual(len(self.transport.calls),1)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_pending_old_frame_cancellation_is_delivered_and_new_transaction_is_distinct(self):
        async def scenario():
            await self.start(street=True)
            try:
                await self.accept(await self.preview()); old=self.packets()[-1]
                await self.reanchor(2)
                cancel=[m for m in self.messages if m['type']=='building_cancel']
                self.assertTrue(any(m['transaction']==old['transaction'] and m['anchorId']==1 for m in cancel))
                await self.until(lambda:self.service.current['state']=='activation_scheduled' and not self.service.busy)
                current=self.packets()[-1]
                self.assertNotEqual(current['transaction'],old['transaction']); self.assertEqual(current['anchorId'],2)
                await self.ack_current()
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_public_diagnostics_and_lua_notification_are_bounded_and_deduplicated(self):
        game=LuaGame(); ui=game.lua.eval("require('beamngEarth/buildingui')")
        if isinstance(ui,tuple): ui=ui[0]
        messages=[]
        game.lua.globals().guihooks=game.table({})
        game.lua.globals().guihooks.trigger=lambda event,value:messages.append((event,game.python(value)))
        state=dict(type='building_status',available=True,state='failed',restoration=dict(mode='blocked',code='artifacts_invalid',anchor=2))
        for _ in range(10): self.assertTrue(ui.message(game.table(state)))
        self.assertEqual(len(messages),1)
        self.assertIn('Stop',messages[0][1]['msg'])
        error=live._public_error(activation.Rejected('prepared targets/settings do not match the current world'))
        self.assertEqual(error['validation_code'],'targets_changed')
        self.assertLess(len(json.dumps(error)),512)
        self.assertNotIn('private-path',json.dumps(live._public_error(activation.Rejected('private-path-and-secret'))))

    def test_failed_engine_ack_gets_one_baseline_attempt_and_failed_fallback_is_visible(self):
        async def fail_current():
            packet=self.packets()[-1]
            await self.service.acknowledge(self.conn,dict(type='building_ack',
                **{key:packet[key] for key in ('sessionId','epoch','anchorId','seq','transaction')},
                status='failed',detail='TEST ONLY simulated collision fence failure'))
        async def scenario():
            await self.start(street=True)
            try:
                await self.accept(await self.preview()); await fail_current()
                await self.until(lambda:len(self.packets())==2 and not self.service.busy)
                self.assertFalse(self.packets()[-1]['osm']); await fail_current()
                self.assertEqual(self.service.current['restoration']['mode'],'blocked')
                await asyncio.sleep(1.1)
                self.assertEqual(len(self.packets()),2)
                self.assertTrue(self.store.load().enabled); self.assertFalse(self.service.current['game_activated'])
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_baseline_ack_preserves_real_store_aba_fence(self):
        async def scenario():
            await self.initial(); manager=self.service.authority; original=manager.activate
            def fail_overlay(*args,**kwargs):
                if not isinstance(manager.store,live._BaselineView):
                    raise activation.Rejected('prepared inputs or artifact checksums no longer match')
                return original(*args,**kwargs)
            try:
                await self.reanchor(2)
                with mock.patch.object(manager,'activate',fail_overlay):
                    await self.until(lambda:self.service.current['state']=='activation_scheduled' and not self.service.busy)
                    before=self.store.load_state()[1]
                    self.store.set_enabled(False); self.store.set_enabled(True)
                    self.assertNotEqual(before,self.store.load_state()[1])
                    await self.ack_current()
                    self.assertEqual(self.service.current['state'],'superseded')
                    self.assertFalse(self.service.current['game_activated'])
                    self.assertTrue(self.store.load().enabled)
            finally: await self.service.close()
        asyncio.run(scenario())


if __name__=='__main__': unittest.main()
