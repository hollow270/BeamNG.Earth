"""Local UI protocol -> actual store/native preparation -> real Lua, no real network/game."""
import asyncio
from copy import deepcopy
import itertools
import json
from pathlib import Path
import socket
import sys
from types import MethodType
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import test_buildingmanual as manual
import test_buildingrender as fx
import test_buildingrefresh as regen
from test_buildingactivation import LuaGame
import buildinglive as live
import earthd
import matslots
import vegetation


class LiveTest(unittest.TestCase):
    setUp=fx.RenderTest.setUp
    md=fx.RenderTest.md
    close=regen.RefreshTest.close
    setup=manual.ManualTest.setup

    async def start(self,originals=(),**kwargs):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(socket,"create_connection",side_effect=AssertionError("live network forbidden")).start()
        self.conn=self.setup(originals,**kwargs)
        server=self.server
        server.sid="activation-test"
        server.out_dir=str(self.root/"session"); Path(server.out_dir).mkdir()
        server.vfs_dir="/temp/beamngEarth/activation-test"
        server.slot_serial=itertools.count(1)
        server.slot_held=MethodType(earthd.Server.slot_held,server)
        server.matslots=matslots.MatSlots(server.sid,server.slot_held)
        self.conn.s=server; self.conn.slot_holds={}; self.conn.peer=("127.0.0.1",12345)
        self.conn.building_activation=1; self.conn.building_batches=1; self.conn.building_epoch="e"*32
        self.messages=[]
        self.service=server._building_live=live.Service(server,local_control=True,clock=self.clock)
        self.conn.send=lambda msg:self.messages.append(deepcopy(msg))
        hello=self.service.hello(self.conn,dict(buildingControl=1,buildingBatches=1))
        self.authorization=hello["buildingControlToken"]
        self.sequence=0
        await self.service.call(self.conn,lambda w,c:None)
        return self.service

    def command(self,op,**fields):
        self.clock.advance(1); self.sequence+=1
        msg=dict(type="building_control",op=op,id=self.sequence,sessionId=self.server.sid,
            epoch=self.conn.building_epoch,authorization=self.authorization,**fields)
        self.service.command(self.conn,msg)
        return msg

    async def until(self,predicate):
        for _ in range(2000):
            if predicate(): return
            await asyncio.sleep(0.005)
        self.fail("workflow did not settle: "+str(self.service.current))

    async def preview(self):
        self.command("preview",bounds=list(manual.bounds()))
        await self.until(lambda:not self.service.busy)
        self.assertEqual(self.service.current["state"],"awaiting_acceptance",self.service.current)
        return self.service.current["preview"]

    async def accept(self,preview):
        self.command("accept",preview_token=preview["token"],confirm=True)
        await self.until(lambda:not self.service.busy)
        self.assertEqual(self.service.current["state"],"activation_scheduled",self.service.current)
        return [m for m in self.messages if m["type"] in ("building_activate","building_activate_batch")]

    async def activate_game(self,packets,game=None):
        game=game or LuaGame()
        for packet in packets: self.assertTrue(game.packet(packet))
        game.tick(45)
        self.assertEqual(game.last()["status"],"activated",game.last())
        await self.service.acknowledge(self.conn,game.last())
        return game

    def test_complete_preview_accept_prepare_activate_and_retry_without_fetch(self):
        async def scenario():
            await self.start(street=True)
            try:
                preview=await self.preview()
                self.assertEqual(preview["counts"]["addition"],1)
                self.assertIsNone(self.store.load().revision)
                self.assertFalse(any(m["type"]=="building_activate" for m in self.messages))
                packets=await self.accept(preview)
                self.assertFalse(self.service.current["game_activated"])
                game=await self.activate_game(packets)
                self.assertEqual(self.service.current["state"],"activated",self.service.current)
                self.assertEqual(self.service.current["accepted_revision"],self.service.current["activated_revision"])
                self.assertEqual(len(self.transport.calls),1)
                self.command("retry")
                await self.until(lambda:not self.service.busy)
                self.assertEqual(len(self.transport.calls),1)
                self.assertTrue(game)
                self.service.context_changed(self.conn)
                self.assertIsNone(self.service.current.get('activated_enabled'))
                self.assertFalse(self.service.activation_states[game.last()['transaction']]['game_activated'])
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_permission_and_confirmation_are_required_and_secret_is_not_ui_state(self):
        async def scenario():
            await self.start()
            try:
                msg=self.command("status")
                msg["authorization"]="0"*64; msg["id"]+=1; msg["op"]="preview"; msg["bounds"]=list(manual.bounds())
                self.service.command(self.conn,msg)
                self.assertFalse(self.transport.calls)
                self.assertNotIn(self.authorization,json.dumps(self.service.public(self.conn)))
                with self.assertRaises(ValueError): self.command("disable",expected_state=self.service.state_token)
                self.conn.peer=("192.0.2.5",1234)
                msg["authorization"]=self.authorization
                self.service.command(self.conn,msg)
                self.assertFalse(self.transport.calls)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_oversize_selection_and_expired_preview_never_commit(self):
        async def scenario():
            await self.start()
            try:
                with self.assertRaises(ValueError): self.command("preview",bounds=list(manual.bounds(w=501,d=100)))
                preview=await self.preview()
                self.wall+=301
                self.command("accept",preview_token=preview["token"],confirm=True)
                await self.until(lambda:not self.service.busy)
                self.assertEqual(self.service.current["state"],"superseded")
                self.assertIsNone(self.store.load().revision)
                self.assertEqual(len(self.transport.calls),1)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_http_failure_and_offline_are_visible_and_never_automatically_refetched(self):
        async def scenario():
            await self.start()
            try:
                self.transport.status=503
                self.command("preview",bounds=list(manual.bounds()))
                await self.until(lambda:not self.service.busy)
                self.assertEqual(self.service.current["state"],"failed")
                self.assertEqual(len(self.transport.calls),1)
                await asyncio.sleep(0.6)
                self.assertEqual(len(self.transport.calls),1)
                self.base.fetcher.offline_flag=True
                self.command("preview",bounds=list(manual.bounds()))
                await self.until(lambda:not self.service.busy)
                self.assertEqual(self.service.current["error"]["type"],"Offline")
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_preview_keeps_overture_only_buildings_and_exposes_conflicts(self):
        async def scenario():
            await self.start([fx.record("gers-other",130,130,osm="w99@1"),fx.record("a",60,60,osm="w4@1"),fx.record("b",65,65,osm="w5@1")])
            try:
                preview=await self.preview()
                self.assertGreaterEqual(preview["overture_only_count"],1)
                self.assertGreater(preview["counts"]["conflict"],0)
                self.assertFalse(preview["omission_is_deletion"])
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_worker_does_not_block_event_loop(self):
        async def scenario():
            await self.start()
            import threading
            gate=threading.Event(); entered=threading.Event()
            original=self.transport.post
            def slow(*args,**kwargs):
                entered.set(); gate.wait(2); return original(*args,**kwargs)
            self.transport.post=slow
            try:
                self.command("preview",bounds=list(manual.bounds()))
                await self.until(entered.is_set)
                ticks=0
                for _ in range(10): await asyncio.sleep(0); ticks+=1
                self.assertEqual(ticks,10); self.assertTrue(self.service.busy)
                gate.set(); await self.until(lambda:not self.service.busy)
            finally: gate.set(); await self.service.close()
        asyncio.run(scenario())

    def test_disable_enable_and_validated_rollback_use_local_activation_without_fetch(self):
        async def scenario():
            await self.start(street=True)
            try:
                proposal=await self.preview(); game=await self.activate_game(await self.accept(proposal))
                for op in ('disable','enable','rollback'):
                    before=len(self.messages)
                    self.command(op,confirm=True,expected_state=self.service.state_token)
                    await self.until(lambda:not self.service.busy)
                    self.assertEqual(self.service.current['state'],'activation_scheduled',self.service.current)
                    packets=[m for m in self.messages[before:] if m['type'] in ('building_activate','building_activate_batch')]
                    await self.activate_game(packets,game)
                    self.assertEqual(self.service.current['activated_enabled'],op=='enable') if op!='rollback' else None
                self.assertIsNone(self.store.load().revision)
                self.assertEqual(len(self.transport.calls),1)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_future_far_tile_is_prepared_on_movement_and_return_without_fetch(self):
        async def scenario():
            await self.start(street=True)
            try:
                game=await self.activate_game(await self.accept(await self.preview()))
                self.conn.bldfar=vegetation.Tracker(kind='bldfar'); self.conn.bldfar.aid=1; self.conn.bldfar.sent={(0,0)}
                before=len(self.messages)
                original=dict(type='bldfar_tile',anchorId=1,key='0_0',data=dict(shape='stale.cdae',pos=[0,0,0]))
                pending=self.service.filter_message(self.conn,original)
                self.assertTrue(pending['data']['buildingPending']); self.assertNotIn('shape',pending['data'])
                self.assertFalse(self.service.public(self.conn)['game_activated'])
                await self.until(lambda:len(self.messages)>before and self.service.current['state']=='activation_scheduled' and not self.service.busy)
                packets=[m for m in self.messages[before:] if m['type'] in ('building_activate','building_activate_batch')]
                self.assertTrue(any(t['kind']=='far' for m in packets for t in m['targets']))
                await self.activate_game(packets,game)
                self.assertFalse(self.service.pending)
                game.lua.globals().roads.onFarDrop(game.table(dict(anchorId=1,key='0_0')))
                self.conn.bldfar.sent.clear(); game.tick(15)
                gone=game.python(game.lua.globals().roads.takeSlotsGone('activation-test'))
                if gone: await self.service.releases(self.conn,gone)
                self.conn.bldfar.sent.add((0,0)); before=len(self.messages)
                self.service.filter_message(self.conn,original)
                await self.until(lambda:self.service.current['state']=='activation_scheduled' and not self.service.busy)
                packets=[m for m in self.messages[before:] if m['type'] in ('building_activate','building_activate_batch')]
                await self.activate_game(packets,game)
                self.assertEqual(len(self.transport.calls),1)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_javascript_and_real_lua_bridge_preserve_preview_token_and_keep_authorization_private(self):
        async def scenario():
            await self.start()
            import subprocess
            try:
                game=LuaGame(); g=game.lua.globals()
                ui=game.lua.eval("require('beamngEarth/buildingui')")
                if isinstance(ui,tuple): ui=ui[0]
                ui.hello(game.table(dict(sessionId=self.server.sid,buildingEpoch=self.conn.building_epoch,
                    buildingControl=1,buildingControlToken=self.authorization,buildingClient=self.service.client_id)))
                captured=[]
                def send(value): captured.append(game.python(value)); return True
                g.ui=ui; g.uiSend=send
                game.lua.execute('beamngEarth_settings={uiBuildingsCommand=function(op,payload) return ui.command(op,payload,uiSend) end}')
                args=json.dumps(dict(op='preview',payload=dict(bounds=list(manual.bounds()))))
                script=subprocess.check_output(['node','tools/test_building_ui.mjs','--command',args],cwd=Path(__file__).resolve().parents[2],text=True)
                game.lua.execute('return '+script)
                self.service.command(self.conn,captured[-1]); await self.until(lambda:not self.service.busy)
                report=self.service.current['preview']
                ui.message(game.table(dict(type='building_status',**self.service.public(self.conn))))
                public=game.python(ui.state())
                self.assertEqual(public['preview']['token'],report['token'])
                self.assertNotIn(self.authorization,json.dumps(public))
                self.clock.advance(1)
                args=json.dumps(dict(op='accept',payload=dict(preview_token=public['preview']['token'],confirm=True)))
                script=subprocess.check_output(['node','tools/test_building_ui.mjs','--command',args],cwd=Path(__file__).resolve().parents[2],text=True)
                game.lua.execute('return '+script)
                self.service.command(self.conn,captured[-1]); await self.until(lambda:not self.service.busy)
                packets=[m for m in self.messages if m['type'] in ('building_activate','building_activate_batch')]
                await self.activate_game(packets,game)
                self.assertEqual(self.service.current['state'],'activated')
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_python_protocol_lua_bridge_preview_renders_in_real_vue_runtime(self):
        async def scenario():
            from dataclasses import replace
            import subprocess
            import protocol
            payload=manual.ofx.response(manual.way()+manual.way(2,x=120))
            source=manual.osm.parse(payload,manual.bounds(),now='2026-10-30T12:01:00Z')
            await self.start([replace(source.buildings[0],feature_id='gers-same')],payload=payload)
            try:
                report=await self.preview()
                self.assertEqual(report['changes'][0]['attributes'],[])
                game=LuaGame(); ui=game.lua.eval("require('beamngEarth/buildingui')")
                if isinstance(ui,tuple): ui=ui[0]
                game.lua.globals().ui=ui
                game.lua.execute('beamngEarth_settings={uiBuildingsState=function() return ui.state() end}')
                public=self.service.public(self.conn)
                public['resources']=dict(held=0,removed_generations=[],reclaimed_slots=[],released_sessions=[],uncertain=[])
                cases=[]
                public['progress']=dict(required=312,acknowledged=96,pending=216,blocked=0,
                                        components=22,remaining_components=15,oversized=0)
                for patch in ({},{'preview':dict(report,changes=[],issues=[],decisions=[],overture_only=[])},
                              {'preview':dict(report,changes=[dict(feature_id='bad',attributes={'bad':1})],issues=[None])}):
                    wire=protocol.encode(dict(type='building_status',**dict(public,**patch)))
                    self.assertEqual(int.from_bytes(wire[:4],'little'),len(wire)-4)
                    ui.message(game.lua.globals().jsonDecode(wire[4:].decode()))
                    value=game.python(game.lua.execute('return beamngEarth_settings.uiBuildingsState()'))
                    self.assertNotIn(self.authorization,json.dumps(value))
                    cases.append(dict(status=value,bounds=dict(zip(('w','s','e','n'),manual.bounds()))))
                self.assertEqual(cases[0]['status']['preview']['changes'][0]['attributes'],{})
                self.assertEqual(cases[0]['status']['preview']['issues'],{})
                self.assertEqual(cases[0]['status']['preview']['decisions'][1]['baseline_ids'],{})
                result=subprocess.run(['node','tools/building_ui_render.mjs','--render-stdin'],
                    cwd=Path(__file__).resolve().parents[2],input=json.dumps(cases),text=True,capture_output=True,check=True)
                rendered=json.loads(result.stdout)
                for row in rendered:
                    self.assertEqual(row['errors'],[]); self.assertEqual(row['commands'],0)
                self.assertIn('attributes: unchanged',rendered[0]['text'])
                self.assertIn('Unchanged records1',rendered[0]['text'])
                self.assertIn('96 / 312 current targets acknowledged; 216 pending',rendered[0]['text'])
                self.assertFalse(rendered[0]['accept_disabled']); self.assertFalse(rendered[1]['accept_disabled'])
                self.assertTrue(rendered[2]['accept_disabled']); self.assertIn('malformed',rendered[2]['text'])
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_later_throttled_preview_keeps_review_and_explicit_acceptance_works(self):
        async def scenario():
            await self.start()
            try:
                first=await self.preview()
                self.command('preview',bounds=list(manual.bounds(x=300)))
                await self.until(lambda:not self.service.busy)
                self.assertEqual(self.service.current['state'],'awaiting_acceptance')
                self.assertEqual(self.service.current['preview'],first)
                self.assertEqual(self.service.current['error']['type'],'Throttled')
                self.assertIsNone(self.store.load().revision)
                self.assertEqual(len(self.transport.calls),1)
                await self.accept(first)
                self.assertIsNotNone(self.store.load().revision)
                self.assertEqual(len(self.transport.calls),1)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_same_selection_preview_is_reused_without_fetch_or_renewing_expiry(self):
        async def scenario():
            await self.start()
            try:
                first=await self.preview()
                created=self.workflow.previews[first['token']]['created']
                for _ in range(2):
                    self.command('preview',bounds=list(manual.bounds()))
                    await self.until(lambda:not self.service.busy)
                    self.assertEqual(self.service.current['state'],'awaiting_acceptance')
                    self.assertEqual(self.service.current['preview'],first)
                    self.assertIsNone(self.service.current.get('error'))
                    self.assertEqual(self.workflow.previews[first['token']]['created'],created)
                self.assertEqual(len(self.transport.calls),1)
                self.assertIsNone(self.store.load().revision)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_failed_later_fetch_keeps_review_but_expired_or_changed_state_never_revives(self):
        async def scenario():
            await self.start()
            try:
                first=await self.preview()
                self.clock.advance(61); self.transport.status=503
                self.command('preview',bounds=list(manual.bounds(x=300)))
                await self.until(lambda:not self.service.busy)
                self.assertEqual(self.service.current['state'],'awaiting_acceptance')
                self.assertEqual(self.service.current['preview']['token'],first['token'])
                self.assertEqual(self.service.current['error']['type'],'HTTPFailure')
                self.store.set_enabled(True)
                self.command('preview',bounds=list(manual.bounds(x=300)))
                await self.until(lambda:not self.service.busy)
                self.assertIsNone(self.service.current['preview'])
                self.assertNotEqual(self.service.current['state'],'awaiting_acceptance')
                self.command('accept',preview_token=first['token'],confirm=True)
                await self.until(lambda:not self.service.busy)
                self.assertEqual(self.service.current['state'],'superseded')
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_frame_change_during_failed_fetch_never_restores_previous_preview(self):
        async def scenario():
            import threading
            started,release=threading.Event(),threading.Event()
            await self.start()
            try:
                await self.preview()
                def fail(*args,**kwargs):
                    started.set(); release.wait(5); raise RuntimeError('synthetic failed fetch')
                with mock.patch.object(self.server,'preview_osm_buildings',side_effect=fail):
                    self.command('preview',bounds=list(manual.bounds(x=300)))
                    await self.until(started.is_set)
                    self.conn.active=2; self.service.context_changed(self.conn)
                    release.set(); await self.until(lambda:not self.service.busy)
                self.assertEqual(self.service.current['state'],'superseded')
                self.assertIsNone(self.service.current['preview'])
                self.assertIsNone(self.store.load().revision)
            finally:
                release.set(); await self.service.close()
        asyncio.run(scenario())

    def test_expired_review_is_not_restored_after_throttling(self):
        async def scenario():
            await self.start()
            try:
                first=await self.preview(); self.wall+=301
                self.command('preview',bounds=list(manual.bounds()))
                await self.until(lambda:not self.service.busy)
                self.assertIsNone(self.service.current['preview'])
                self.command('accept',preview_token=first['token'],confirm=True)
                await self.until(lambda:not self.service.busy)
                self.assertEqual(self.service.current['state'],'superseded')
                self.assertIsNone(self.store.load().revision)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_lua_single_flight_uses_matching_companion_receipt_and_resets_on_disconnect(self):
        game=LuaGame(); ui=game.lua.eval("require('beamngEarth/buildingui')")
        if isinstance(ui,tuple): ui=ui[0]
        ui.hello(game.table(dict(buildingControl=1,buildingControlToken='a'*64,sessionId='session',buildingEpoch='e'*32)))
        sent=[]
        def send(value): sent.append(game.python(value)); return True
        first=game.python(ui.command('preview','{}',send))
        self.assertIn('error',game.python(ui.command('preview','{}',send)))
        self.assertEqual(len(sent),1)
        ui.message(game.table(dict(type='building_status',available=True,command_id=first['id']-1,command_pending=False)))
        self.assertTrue(game.python(ui.state())['operation_pending'])
        ui.message(game.table(dict(type='building_status',available=True,command_id=first['id'],command_pending=False)))
        self.assertFalse(game.python(ui.state())['operation_pending'])
        ui.command('preview','{}',send); self.assertEqual(len(sent),2)
        ui.disconnect(); self.assertFalse(game.python(ui.state())['available'])
        ui.hello(game.table(dict(buildingControl=1,buildingControlToken='b'*64,sessionId='session',buildingEpoch='f'*32)))
        self.assertIn('error',game.python(ui.command('preview','{}',lambda value:False)))
        self.assertFalse(game.python(ui.state())['operation_pending'])
        ui.command('preview','{}',send); ui.revoke(send)
        self.assertFalse(game.python(ui.state())['available'])
        self.assertFalse(game.python(ui.state())['operation_pending'])

    def test_rapid_revision_changes_and_late_ack_cannot_claim_old_activation(self):
        async def scenario():
            await self.start(street=True)
            try:
                packets=await self.accept(await self.preview()); game=LuaGame()
                for p in packets: self.assertTrue(game.packet(p))
                game.tick(45); old_ack=game.last(); before=len(self.messages)
                await asyncio.to_thread(self.store.disable); await asyncio.to_thread(self.store.enable)
                await self.service.acknowledge(self.conn,old_ack)
                self.assertEqual(self.service.current['state'],'superseded')
                self.assertFalse(self.service.current['game_activated'])
                await self.until(lambda:self.service.current['state']=='activation_scheduled' and not self.service.busy)
                current=[m for m in self.messages[before:] if m['type'] in ('building_activate','building_activate_batch')]
                await self.activate_game(current,game)
                transaction=self.service.current['transaction']
                await self.service.acknowledge(self.conn,old_ack)
                self.assertEqual(self.service.current['transaction'],transaction)
                self.assertEqual(self.service.current['state'],'activated')
                self.assertEqual(len(self.transport.calls),1)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_reconnect_rotates_capability_and_preserves_same_client_binding(self):
        async def scenario():
            await self.start()
            try:
                client=self.service.client_id; old=self.authorization
                self.service.current.update(command_id=1,command_pending=False)
                self.service.context_changed(self.conn,disconnect=True)
                self.conn.building_epoch='f'*32
                hello=self.service.hello(self.conn,dict(buildingControl=1,buildingBatches=1,buildingClient=client))
                self.assertEqual(hello['buildingControl'],1); self.assertNotEqual(hello['buildingControlToken'],old)
                self.assertNotIn('command_id',self.service.current)
                self.assertNotIn('command_pending',self.service.current)
                self.service.context_changed(self.conn,disconnect=True)
                denied=self.service.hello(self.conn,dict(buildingControl=1,buildingBatches=1,buildingClient='b'*64))
                self.assertEqual(denied['buildingControl'],0); self.assertTrue(self.conn.building_control_denied)
                self.assertFalse(self.transport.calls)
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_environment_requires_explicit_endpoint_and_keeps_denied_host_policy(self):
        async def scenario():
            server=SimpleNamespace(net=SimpleNamespace(policy=lambda host:SimpleNamespace(denied=True)),configure_osm_buildings=mock.Mock())
            with mock.patch.dict(live.os.environ,{},clear=True):
                self.assertIsNone(live.install_from_environment(server))
            for extra in ({},{'BNGE_OSM_ENDPOINT':'https://configured.example.invalid/api/interpreter'}):
                with mock.patch.dict(live.os.environ,dict(BNGE_OSM_BUILDINGS='on',BNGE_OSM_LOCAL_CONTROL='on',**extra),clear=True):
                    service=live.install_from_environment(server)
                    self.assertTrue(service.unavailable_reason); self.assertFalse(service.public(None)['available'])
                    self.assertFalse(server.configure_osm_buildings.called)
                    await service.close()
        asyncio.run(scenario())

    def test_anchor_change_during_preparation_never_delivers_old_frame_command(self):
        async def scenario():
            await self.start(street=True)
            import threading
            gate=threading.Event(); entered=threading.Event()
            try:
                proposal=await self.preview(); original=self.server.refresh_building_overlays
                def blocked(*args,**kwargs):
                    entered.set(); gate.wait(3); return original(*args,**kwargs)
                self.server.refresh_building_overlays=blocked
                self.command('accept',preview_token=proposal['token'],confirm=True)
                await self.until(entered.is_set)
                self.conn.active=2; self.service.context_changed(self.conn)
                gate.set(); await self.until(lambda:not self.service.busy)
                self.assertEqual(self.service.current['state'],'superseded')
                self.assertIsNotNone(self.store.load().revision)
                self.assertFalse(any(m['type'] in ('building_activate','building_activate_batch') for m in self.messages))
                self.assertEqual(len(self.transport.calls),1)
            finally: gate.set(); await self.service.close()
        asyncio.run(scenario())

    def test_driving_away_from_an_unprepared_target_does_not_regenerate_unrelated_targets(self):
        async def scenario():
            await self.start(street=True)
            gate=asyncio.Event(); entered=asyncio.Event(); ack=None
            try:
                packets=await self.accept(await self.preview()); game=LuaGame()
                for packet in packets: self.assertTrue(game.packet(packet))
                game.tick(45); original=self.service.call
                async def delayed(c,fn):
                    result=await original(c,fn)
                    if 'acknowledge' in fn.__code__.co_names:
                        entered.set(); await gate.wait() # routing export precedes completion-state publication
                    return result
                with mock.patch.object(self.service,'call',delayed), mock.patch.object(self.server,'refresh_building_overlays',wraps=self.server.refresh_building_overlays) as prepare:
                    ack=asyncio.create_task(self.service.acknowledge(self.conn,game.last()))
                    await self.until(entered.is_set)
                    self.service.pending['far:0_0']=dict(kind='far',key='0_0',status='deferred',reason='awaiting local preparation')
                    await asyncio.sleep(1.1)
                    self.assertFalse(self.service.pending); self.assertFalse(prepare.called)
                    gate.set(); await ack
                self.assertEqual(len(self.transport.calls),1)
            finally:
                gate.set()
                if ack is not None: await ack
                await self.service.close()
        asyncio.run(scenario())

    def test_startup_queues_building_messages_until_retained_coverage_is_loaded(self):
        async def scenario():
            await self.start(street=True)
            try:
                await self.activate_game(await self.accept(await self.preview()))
                self.service.initialized=False
                original=dict(type='bldfar_tile',anchorId=1,key='0_0',data=dict(shape='stale.cdae',pos=[0,0,0]))
                self.assertIsNone(self.service.filter_message(self.conn,original))
                self.assertEqual(len(self.service.boot_messages),1)
                received=[]
                def send(message):
                    filtered=self.service.filter_message(self.conn,message)
                    if filtered is not None: received.append(filtered)
                self.conn.send=send
                await self.service.call(self.conn,lambda w,c:None)
                self.assertTrue(self.service.initialized); self.assertFalse(self.service.boot_messages)
                message=next(m for m in received if m['type']=='bldfar_tile')
                self.assertTrue(message['data']['buildingPending']); self.assertNotIn('shape',message['data'])
            finally: await self.service.close()
        asyncio.run(scenario())

    def test_store_change_after_materialization_blocks_buffered_old_revision_before_send(self):
        async def scenario():
            await self.start(street=True)
            try:
                proposal=await self.preview(); activate=live.activation.Manager.activate
                def supersede(manager,*args,**kwargs):
                    result=activate(manager,*args,**kwargs)
                    self.store.disable() # after slot copy/journal publication, before socket dispatch
                    return result
                with mock.patch.object(live.activation.Manager,'activate',supersede):
                    self.command('accept',preview_token=proposal['token'],confirm=True)
                    await self.until(lambda:not self.service.busy)
                self.assertEqual(self.service.current['state'],'superseded',self.service.current)
                self.assertFalse(any(m['type'] in ('building_activate','building_activate_batch') for m in self.messages))
                self.assertFalse(self.service.authority.holds)
                self.assertIsNotNone(self.store.load().revision); self.assertFalse(self.store.load().enabled)
                self.assertEqual(len(self.transport.calls),1)
            finally: await self.service.close()
        asyncio.run(scenario())
