"""Real Lua recovery/inventory sequencing with deterministic engine doubles."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import test_buildingactivation as fx
import test_buildingmanual as manual
import test_buildingrender as render
import test_buildingrefresh as regen


class RecoveryTest(unittest.TestCase):
    setUp=render.RenderTest.setUp
    md=render.RenderTest.md
    close=regen.RefreshTest.close
    setup=manual.ManualTest.setup
    prepared=fx.ActivationTest.prepared
    preview=manual.ManualTest.preview
    accept=manual.ManualTest.accept
    activate=fx.ActivationTest.activate

    def start(self):
        self.prepared(); self.activate()
        self.packet=deepcopy(self.messages[-1])
        self.game=fx.LuaGame(); self.g=self.game.lua.globals()
        self.g.nearSeed('0_0',True)
        self.assertTrue(self.game.packet(self.packet)); self.game.tick(45)
        self.assertEqual(self.game.last()['status'],'activated')

    def test_repeated_server_handoff_keeps_occupied_original_roads_without_duplicate_buildings(self):
        self.start(); road=self.g.roadId()
        for i in range(3):
            sid='restart-'+str(i)
            self.g.roads.newSession(); self.g.roads.setSession(sid); self.g.activation.hello(sid,'f'*32)
            packet=deepcopy(self.packet); packet.update(sessionId=sid,epoch='f'*32,seq=1,transaction=str(i)*32)
            for target in packet['targets']:
                for sl in target['slots'].values(): sl[0]=sl[0].replace('/activation-test/','/'+sid+'/')
            self.assertTrue(self.game.packet(packet)); self.game.tick(45)
            self.assertEqual(self.game.last()['status'],'activated',self.game.last())
            self.assertEqual(self.g.roadId(),road)
            for target in packet['targets']:
                for sl in target['slots'].values(): self.assertEqual(self.g.shapeCount(sl[0]),1)

    def test_full_vm_reset_fails_closed_and_does_not_sweep_retained_roads(self):
        self.start(); road=self.g.roadId(); count=self.g.liveCount()
        self.game.lua.execute('_G.beamngEarthBuildingRoadRuntime=nil; _G.beamngEarthBuildingController=nil; reloadRoads()')
        self.assertEqual(self.g.roads.sweep(),0)
        self.assertTrue(self.g.roads.recoveryBlocked)
        self.g.roads.newSession(); self.game.tick(15)
        self.assertEqual(self.g.roadId(),road); self.assertEqual(self.g.liveCount(),count)
        self.assertTrue(self.game.packet(self.packet)); self.game.tick(3)
        self.assertEqual(self.game.last()['status'],'deferred')
        self.assertEqual(self.g.roadId(),road)

    def test_vehicle_approaching_during_retirement_keeps_new_collision_and_finishes_old_retirement(self):
        self.prepared(); self.activate(); packet=self.messages[-1]
        game=fx.LuaGame(); g=game.lua.globals(); g.nearSeed('0_0',True); road=g.roadId()
        self.assertTrue(game.packet(packet))
        for _ in range(30):
            game.tick(1)
            if game.python(g.activation.status()).get('phase')=='retiring': break
        self.assertEqual(game.last()['status'],'installing')
        game.lua.execute('vehicles[1]:setPosition(vec3(65,65,0))')
        game.tick(35)
        self.assertEqual(game.last()['status'],'activated',game.last())
        self.assertIn('approached',game.last()['detail']); self.assertEqual(g.roadId(),road)
        collision=packet['targets'][0]['slots']['bldcol'][0]
        self.assertEqual(g.shapeCount(collision),1)
        self.assertEqual(g.shapeCount('/temp/beamngEarth/activation-test/slot_90002.cdae'),0)

    def test_governed_inventory_reports_scene_and_pending_roots_and_rejects_unknown_ownership(self):
        self.start()
        self.game.lua.execute("""
          scenetree.findClassObjects=function(class)
            local ids={} for id,o in pairs(objects) do if o.class==class then ids[#ids+1]=id end end return ids
          end
          inventory=require('beamngEarth/buildinginventory')
          inventoryResult=nil
          inventoryContext={sessions=function() return roads.heldSessions() end,
            slots=function() return roads.buildingSlotReferences() end,
            blocked=function() return roads.recoveryBlocked end,pending=activation.references}
          function collectInventory()
            inventory.start({},inventoryContext,function(msg,complete,sessions,reason,slots,pending)
              inventoryResult={complete=complete,sessions=sessions,reason=reason,slots=slots,pending=pending}
            end)
            inventory.update(gov)
          end
          collectInventory()
        """)
        proof=self.game.python(self.g.inventoryResult)
        self.assertTrue(proof['complete']); self.assertIn('activation-test',proof['sessions'])
        self.assertTrue(proof['slots'])
        self.game.lua.execute('roads.recoveryBlocked=true; collectInventory()')
        self.assertFalse(self.game.python(self.g.inventoryResult)['complete'])
