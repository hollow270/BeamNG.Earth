"""Catalog limits differ from bounded messages; every group commits just once."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import test_buildingactivation as activationfx
import test_buildingmanual as manual
import test_buildingrender as fx
import test_buildingrefresh as regen
import buildingrefresh as br
import mapcells
import vegetation
import geotiles


class BatchTest(unittest.TestCase):
    setUp=fx.RenderTest.setUp
    md=fx.RenderTest.md
    close=regen.RefreshTest.close
    setup=manual.ManualTest.setup
    prepared=activationfx.ActivationTest.prepared
    preview=manual.ManualTest.preview
    accept=manual.ManualTest.accept
    activate=activationfx.ActivationTest.activate
    ack=activationfx.ActivationTest.ack

    @staticmethod
    def box(x,y,w,d):
        bounds=manual.bounds(x,y,w,d)
        return bounds[1],bounds[0],bounds[3],bounds[2]

    def requests(self,n):
        self.conn.roads=None
        self.conn.building_batches=1
        self.conn.bldfar=vegetation.Tracker(kind="bldfar")
        self.conn.bldfar.aid=1
        self.conn.bldfar.sent={(i,0) for i in range(n)}
        for key in mapcells.cells_for_bbox(*self.box(0,0,n*256,256)):
            self.base.cell(key,layers=("buildings","building_parts"))
        return [br.TileRequest("far",i,0,lat=fx.LAT,lon=fx.LON,options={"lamp_region":"eu"}) for i in range(n)]

    def test_catalogs_of_32_33_64_128_use_bounded_messages_and_atomic_client_handoff(self):
        self.prepared()
        game=activationfx.LuaGame()
        for n in (32,33,64,128):
            with self.subTest(catalog=n):
                reqs=self.requests(n)
                self.publication=self.server.refresh_building_overlays(reqs,replace_requests=True)["publication"]
                self.messages.clear()
                self.activate()
                packets=deepcopy(self.messages)
                self.assertEqual(sum(len(m["targets"]) for m in packets),n)
                self.assertTrue(all(len(m["targets"])<=32 for m in packets))
                before=game.lua.globals().liveCount()
                for packet in packets[:-1]:
                    self.assertTrue(game.packet(packet)); game.tick(2)
                    self.assertEqual(game.lua.globals().liveCount(),before)
                self.assertTrue(game.packet(packets[-1])); game.tick(30)
                self.assertEqual(game.last()["status"],"activated",game.last())
                self.assertEqual(self.server._building_activation.acknowledge(self.conn,game.last())["state"],"activated")

    def test_large_valid_catalog_with_one_current_target_does_not_require_batch_capability(self):
        self.prepared()
        reqs=self.requests(128)
        self.publication=self.server.refresh_building_overlays(reqs,replace_requests=True)["publication"]
        self.conn.building_batches=0
        self.conn.bldfar.sent={(0,0)}
        import buildingtargets
        discovery=buildingtargets.discover(self.server,manual.bounds(),current_only=True)
        self.assertEqual(len(discovery.requests),1)
        result=self.activate()
        self.assertEqual(result["state"],"scheduled")
        self.assertEqual(len(self.messages[-1]["targets"]),1)

    def test_mixed_near_far_skyline_group_waits_for_all_33_targets(self):
        self.prepared()
        near=br.TileRequest(**next(iter(self.server._building_refresh.outputs()["requests"].values())))
        self.conn.building_batches=1
        self.conn.bldfar=vegetation.Tracker(kind="bldfar"); self.conn.bldfar.aid=1
        self.conn.bldfar.sent={(i,0) for i in range(31)}
        self.conn.bldsky=vegetation.Tracker(kind="bldsky"); self.conn.bldsky.aid=1; self.conn.bldsky.sent={(0,0)}
        for key in mapcells.cells_for_bbox(*self.box(0,0,31*256,1024)):
            self.base.cell(key,layers=("buildings","building_parts"))
        requests=[near]+[br.TileRequest("far",i,0,lat=fx.LAT,lon=fx.LON,options={"lamp_region":"eu"}) for i in range(31)]+[
            br.TileRequest("skyline",0,0,lat=fx.LAT,lon=fx.LON,options={"lamp_region":"eu"})]
        import blockworker
        for request in requests:
            for key in blockworker.building_cells(*request.geometry()):
                self.worker_md.cell(key,layers=("buildings","building_parts","roads"))
        self.publication=self.server.refresh_building_overlays(requests,replace_requests=True)["publication"]
        self.messages.clear(); self.activate()
        game=activationfx.LuaGame()
        self.assertTrue(game.packet(self.messages[0])); game.tick(15)
        self.assertFalse(game.last())
        self.assertTrue(game.packet(self.messages[1])); game.tick(40)
        self.assertEqual(game.last()["status"],"activated",game.last())

    def test_mixed_geotile_group_uses_one_fenced_commit_across_batches(self):
        self.prepared(geo=True)
        near=br.TileRequest(**next(iter(self.server._building_refresh.outputs()["requests"].values())))
        key=next(iter(self.conn.roads.sent)); self.conn.roads.sent.clear(); self.conn.roads.empty.add(key)
        self.conn.building_batches=1
        self.conn.bldfar=vegetation.Tracker(kind='bldfar',geo=True,z=geotiles.FAR_Z); self.conn.bldfar.aid=1
        self.conn.bldfar.sent={(near.x+i,near.y) for i in range(31)}
        la,lo,_=fx.FRAME.enu_to_geodetic(65,65,0)
        sx,sy=geotiles.tile_of(float(la),float(lo),geotiles.SKY_Z)
        self.conn.bldsky=vegetation.Tracker(kind='bldsky',geo=True,z=geotiles.SKY_Z); self.conn.bldsky.aid=1
        self.conn.bldsky.sent={(sx,sy)}
        requests=[near]+[br.TileRequest('far_geo',near.x+i,near.y,gzoom=15,options={'lamp_region':'eu'}) for i in range(31)]+[
            br.TileRequest('skyline_geo',sx,sy,gzoom=15,options={'lamp_region':'eu'})]
        import blockworker
        for request in requests:
            for cell in blockworker.building_cells(*request.geometry()):
                for md in (self.base,self.worker_md): md.cell(cell,layers=('buildings','building_parts','roads'))
        self.publication=self.server.refresh_building_overlays(requests,replace_requests=True)['publication']
        self.messages.clear(); self.activate()
        game=activationfx.LuaGame(); game.lua.globals().roads.setGeo(True)
        self.assertEqual(len(self.messages),2)
        self.assertTrue(game.packet(self.messages[0])); game.tick(4); self.assertFalse(game.last())
        self.assertTrue(game.packet(self.messages[1])); game.tick(45)
        self.assertEqual(game.last()['status'],'activated',game.last())
