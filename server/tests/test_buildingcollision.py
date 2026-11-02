"""Real Agadir footprint/box regression through the actual Lua hold queues.

Only scene/vehicle/governor engine bindings are mocked. No game process, network,
collision ACK or claim of physical engine collision is involved.
"""
import json
from pathlib import Path
import unittest

import numpy as np
from lupa.luajit21 import LuaRuntime

import buildingoverlay as bo
import buildings
import geotiles

ROOT=Path(__file__).resolve().parents[2]
FIXTURE=json.loads((Path(__file__).parent/'fixtures/agadir_collision.json').read_text(encoding='utf-8-sig'))
OSM=json.loads((Path(__file__).parent/'fixtures/layercake_agadir.json').read_text(encoding='utf-8'))


class Game:
    def __init__(self):
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().package.path=(ROOT/'mod/beamng_earth/lua/ge/extensions/?.lua').as_posix()+';'+self.lua.globals().package.path
        self.scene=self.lua.execute((ROOT/'mod/tests/stubs.lua').read_text())
        self.lua.globals().payload=self.table(FIXTURE['tile'])
        self.lua.execute('''
        local geo=require('beamngEarth/geodesy')
        anchor=geo.newAnchor(30.41,-9.55)
        roads=require('beamngEarth/roads')
        roads.setGeo(true);roads.setSession('collision-test')
        roads.RELOAD_EVERY_S=0
        getPlayerVehicle=function()return {getPosition=function()return vehicle end,getVelocity=function()return vec3(0,0,0)end}end
        readFile=function()return 'fixture' end
        jsonDecode=function()return payload end
        gov={try=function()return true end,charge=function()end,chargeMotion=function()end,roomFor=function()return true end}
        function inside(boxes,what,t)
          for i=1,60 do local name,f=debug.getupvalue(roads.update,i)
            if name=='playerIn' then return f(boxes,what,t) end
            if not name then break end
          end
          error('playerIn closure missing')
        end
        function tilePlacement()
          local t={geo=true,frame=geo.newAnchor(payload.frame[1],payload.frame[2]),parts={bldcol=payload.bldcol,bld=payload.bld}}
          roads.placeGeoTile(t,anchor);return t
        end
        function tick()roads.update(gov,1,{x=vehicle.x,y=vehicle.y,speed=0},nil,anchor)end
        function loadTile()
          assert(roads.onTile({anchorId=1,key=payload.key,path='/fixture/tile.json',n=0,frame=payload.frame,h=payload.h},1,anchor))
          tick();tick()
        end
        function countShape(shape)
          local n=0
          for _,o in pairs(scenetree.MissionGroup and testObjects or {})do
            if o.fields.shapeName==shape then n=n+1 end
          end
          return n
        end
        ''')
        self.lua.globals().testObjects=self.scene.objects
        self.roads=self.lua.globals().roads
        self.placement=self.lua.globals().tilePlacement()
        self.spawn()

    def table(self,v):
        if isinstance(v,dict):return self.lua.table_from({k:self.table(q) for k,q in v.items()})
        if isinstance(v,(tuple,list)):return self.lua.table_from([self.table(q) for q in v])
        return v

    def spawn(self):
        s=FIXTURE['spawn'];a=self.lua.globals().anchor
        x,y,_=a.geodeticToWorld(a,s['lat'],s['lon'],0)
        self.lua.globals().vehicle=self.lua.globals().vec3(x,y,s['z'])

    def at_box(self,inside=True):
        b=FIXTURE['tile']['colliders']['bldcol'][FIXTURE['reference_box']-1]
        x=(b[0]+b[3])/2 if inside else b[3]+20
        y=(b[1]+b[4])/2
        z=(b[2]+b[5])/2
        xf=self.placement.pls.bldcol.xf
        x,y,z=xf.apply(xf,x,y,z)
        self.lua.globals().vehicle=self.lua.globals().vec3(x,y,z)

    def count(self,kind):return self.lua.globals().countShape('/fixture/agadir_'+kind+'.cdae')
    def tick(self):self.lua.globals().tick()
    def load(self):self.lua.globals().loadTile()
    def predicate(self,kind='bldcol'):
        return self.lua.globals().inside(self.table(FIXTURE['tile']['colliders']['bldcol']),kind,self.placement)


class BuildingCollisionTest(unittest.TestCase):
    def test_committed_footprint_and_actual_emitted_box_reproduce_false_positive(self):
        g=Game();p=g.lua.globals().vehicle;inv=g.placement.pls.bldcol.inv
        x,y,z=inv.apply(inv,p.x,p.y,p.z)
        ring=np.array(OSM['row']['geometry']['coordinates'][0])
        f=geotiles.WorldFrame(*FIXTURE['tile']['frame'])
        e,n,_=f.geodetic_to_enu(ring[:,1],ring[:,0],0.)
        # Actual collider_boxes output, using the committed geometry and real z range.
        b=FIXTURE['tile']['colliders']['bldcol'][FIXTURE['reference_box']-1]
        class Spec:
            cls='house';roof='flat';deck=False;height=9.6
        spec=Spec();spec.ring=np.c_[e,n][:-1]
        from unittest.mock import patch
        with patch.object(buildings,'deck_plan',return_value=None):
            generated=buildings.collider_boxes([(spec,b[2]+buildings.FOUNDATION_M,b[5]-9.6-6)],0,0)[0]
        np.testing.assert_allclose(generated,b,atol=.01)
        self.assertAlmostEqual(x-b[3],.91098,places=3)
        self.assertAlmostEqual(y-b[4],1.51840,places=3)
        self.assertTrue(b[0]-3<=x<=b[3]+3 and b[1]-3<=y<=b[4]+3 and b[2]-2.5<=z<=b[5]+2.5)
        self.assertFalse(bo._inside((x,y),[tuple(q) for q in np.c_[e,n][:-1]],boundary=True))
        self.assertFalse(g.predicate(),'outside the building: padding must not hold the geotile')
        g.placement.pls.railcol=g.placement.pls.bldcol
        self.assertTrue(g.predicate('railcol'),'unrelated collider safety radius stays unchanged')

    def test_outside_vehicle_gets_complete_shared_tile_collider(self):
        g=Game();g.load()
        self.assertEqual(g.count('bld'),1)
        self.assertEqual(g.count('bldcol'),1,'the one collision mesh covers all 57 buildings in this tile')
        self.assertEqual(g.roads.status().heldBuildingMeshes,0)
        self.assertGreater(g.scene.calls['reloadCollision'] or 0,0)

    def test_inside_vehicle_holds_then_releases_when_clear(self):
        g=Game();g.at_box();self.assertTrue(g.predicate())
        # This centre is inside the actual committed polygon too, not just its AABB.
        p=g.lua.globals().vehicle;inv=g.placement.pls.bldcol.inv
        x,y,_=inv.apply(inv,p.x,p.y,p.z)
        raw=np.asarray(OSM['row']['geometry']['coordinates'][0])
        frame=geotiles.WorldFrame(*FIXTURE['tile']['frame'])
        e,n,_=frame.geodetic_to_enu(raw[:,1],raw[:,0],0.)
        self.assertTrue(bo._inside((x,y),[tuple(q) for q in np.c_[e,n][:-1]],boundary=True))
        self.assertTrue(g.predicate('bld'),'legacy colliding building meshes keep the inside guard too')
        g.load();self.assertEqual(g.count('bld'),1);self.assertEqual(g.count('bldcol'),0)
        self.assertEqual(g.roads.status().heldBuildingMeshes,1)
        g.spawn();g.tick();self.assertEqual(g.count('bldcol'),1)
        self.assertEqual(g.roads.status().heldBuildingMeshes,0)

    def test_pre_rebuild_guard_still_removes_a_mesh_around_an_inside_vehicle(self):
        g=Game();g.roads.RELOAD_EVERY_S=1e9;g.load()
        self.assertEqual(g.count('bldcol'),1)
        g.at_box();g.roads.collisionRebuilt()
        self.assertEqual(g.count('bldcol'),0);self.assertEqual(g.roads.solidAround,1)
        self.assertEqual(g.roads.status().heldBuildingMeshes,1)
        g.spawn();g.tick();self.assertEqual(g.count('bldcol'),1)

    def test_clear_vehicle_is_not_removed_at_collision_rebuild(self):
        g=Game();g.roads.RELOAD_EVERY_S=1e9;g.load();g.roads.collisionRebuilt()
        self.assertEqual(g.count('bldcol'),1);self.assertEqual(g.roads.solidAround or 0,0)
        self.assertEqual(g.roads.status().heldBuildingMeshes,0)

    def test_old_padding_holds_whole_tile_and_corrected_guard_releases_it(self):
        g=Game();g.roads.BUILDING_PLAYER_R=3;g.load()
        self.assertEqual(g.count('bld'),1);self.assertEqual(g.count('bldcol'),0)
        self.assertEqual(g.roads.status().heldBuildingMeshes,1)
        g.roads.BUILDING_PLAYER_R=0;g.tick()
        self.assertEqual(g.count('bldcol'),1);self.assertEqual(g.roads.status().heldBuildingMeshes,0)


if __name__=='__main__':unittest.main()
