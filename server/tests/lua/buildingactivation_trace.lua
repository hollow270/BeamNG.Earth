-- Deterministic engine doubles. The real controller, adapter and roads.lua run.
clock,objects,names,vehicles,acks = 0,{}, {},{},{}
local nextId=0
function log() end
function vec3(x,y,z) return {x=x,y=y,z=z} end
function loadJsonMaterialsFile() end
function readFile() return nil end
FS={fileExists=function(self,path) return not missingPath or missingPath~=path end}
function createObject(class)
  nextId=nextId+1
  local o={id=nextId,class=class,fields={},pos=vec3(0,0,0),children={}}
  function o:setField(k,i,v) self.fields[k]=v end
  function o:getField(k,i) return self.fields[k] or '' end
  function o:setPosition(p) self.pos=p end
  function o:getPosition() return self.pos end
  function o:getVelocity() return self.vel or vec3(0,0,0) end
  function o:setPosRot(x,y,z) self.pos=vec3(x,y,z) end
  function o:setHidden(v) if failReveal and not v and self.fields.shapeName==failReveal then error('injected reveal failure') end self.hidden=v end
  function o:registerObject(name)
    objects[self.id]=self; if name~='' then names[name]=self end
    if failRegisterPath and self.fields.shapeName==failRegisterPath then error('injected partial registration failure') end
  end
  function o:getID() return self.id end
  function o:getId() return self.id end
  function o:getClassName() return self.class end
  function o:addObject(child) self.children[child.id]=child end
  function o:getCount() local n=0 for id in pairs(self.children) do if objects[id] then n=n+1 end end return n end
  function o:deleteAllObjects() for id,child in pairs(self.children) do child:delete() end self.children={} end
  function o:delete() objects[self.id]=nil; self.deleted=true end
  return o
end
scenetree={findObject=function(name) return names[name] end,findObjectById=function(id) return objects[id] end}
scenetree.MissionGroup=createObject('SimGroup'); scenetree.MissionGroup:registerObject('MissionGroup')
be={reloadCount=0,reloadCollision=function(self) self.reloadCount=self.reloadCount+1 end,
  getObjectCount=function() return #vehicles end,getObject=function(self,i) return vehicles[i+1] end}
function getPlayerVehicle() return vehicles[1] end
package.preload['beamngEarth/stopwatch']=function() return {
  clock=function() return function() return clock end end,
  new=function() local t=clock; return function() return clock-t end end} end
for _,name in ipairs({'nightlights','gasstations','trees','parking','pumpjacks','ambience'}) do
  package.preload['beamngEarth/'..name]=function() return setmetatable({MATERIAL_FILES={}}, {__index=function() return function() end end}) end
end
roads=require('beamngEarth/roads')
activation=require('beamngEarth/buildingactivation')
gov={try=function() return true end,charge=function() end,chargeMotion=function() end,roomFor=function() return true end,
  isHeavyFrame=function() return false end}
anchor={lat0=0,lon0=0,geodetic_to_enu=function() return 0,0,0 end}
function send(m) acks[#acks+1]=m end
function start(sid,epoch)
  roads.setSession(sid); activation.hello(sid,epoch)
  local vehicle=createObject('BeamNGVehicle'); vehicle:setPosition(vec3(450,65,0)); vehicles={vehicle}
end
function tick(n)
  for i=1,(n or 1) do
    clock=clock+1000
    roads.activationSimStep=not simPaused
    activation.update(gov,1,anchor,send,simPaused and 0 or 0.1)
    roads.update(gov,1,vehicles[1] and vehicles[1]:getPosition(),nil,anchor)
  end
end
function receive(m)
  if m.type=='building_activate_batch' then return activation.receiveBatch(m,1,roads.buildingActivationHost(),send) end
  return activation.receive(m,1,roads.buildingActivationHost(),send)
end
function latest() return acks[#acks] end
function tile(key) return roads.buildingActivationHost() end
function nearSeed(key,withBuildings)
  local data={chunks={},road={shape='/temp/beamngEarth/activation-test/slot_90000.cdae',pos={0,0,0}},
    colliders={}}
  if withBuildings then
    data.bld={shape='/temp/beamngEarth/activation-test/slot_90001.cdae',pos={0,0,0}}
    data.bldcol={shape='/temp/beamngEarth/activation-test/slot_90002.cdae',pos={0,0,0}}
    data.colliders.bldcol={{60,60,0,70,70,30}}
  end
  local slots={road={data.road.shape,90000}}
  if withBuildings then slots.bld={data.bld.shape,90001}; slots.bldcol={data.bldcol.shape,90002} end
  local frame={anchor.lat,anchor.lon}
  local geo=require('beamngEarth/geotile')
  if geo.isKey(key) then local z,x,y=geo.parse(key); frame={geo.centre(z,x,y)} end
  roads.onTile({anchorId=1,key=key,data=data,slots=slots,frame=frame},1,anchor)
  tick(10)
end
function roadId() for id,o in pairs(objects) do if o.fields.shapeName=='/temp/beamngEarth/activation-test/slot_90000.cdae' then return id end end end
function shapeCount(path) local n=0 for _,o in pairs(objects) do if o.fields.shapeName==path then n=n+1 end end return n end
function liveCount() local n=0 for _ in pairs(objects) do n=n+1 end return n end
function reloadRoads()
  package.loaded['beamngEarth/roads']=nil
  roads=require('beamngEarth/roads')
  package.loaded['beamngEarth/buildingactivation']=nil
  activation=require('beamngEarth/buildingactivation')
  roads.setSession('activation-test'); activation.hello('activation-test',string.rep('e',32))
end
function shapeIds(path)
  local ids={} for id,o in pairs(objects) do if o.fields.shapeName==path then ids[#ids+1]=id end end
  table.sort(ids); return ids
end
