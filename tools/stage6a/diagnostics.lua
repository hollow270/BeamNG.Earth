-- Development-only, read-only observations. Never claims physical readiness.
local M={}
local function snapshot()
  local baseline=jsonReadFile('/stage6a-baseline-roads.json')
  assert(baseline and baseline.anchor,'Wait for baseline road streaming in the synthetic world')
  local out={roads={},terrain={},status=beamngEarth_settings.uiBuildingsState()}
  for _,class in ipairs({'TSStatic','TerrainBlock'}) do
    for _,id in ipairs(scenetree.findClassObjects(class)) do
      local o=scenetree.findObject(id) or scenetree.findObjectById(id)
      if o then
        local path=o:getField(class=='TerrainBlock' and 'terrainFile' or 'shapeName',0)
        if class=='TerrainBlock' then out.terrain[tostring(o:getID())]=path
        elseif baseline.roads[path] then out.roads[tostring(o:getID())]=path end
      end
    end
  end
  return out
end
function M.capture(label)
  local value=snapshot()
  if label=='before' then M.before=value end
  jsonWriteFile('/stage6a-'..(label=='before' and 'before' or 'after')..'.json',value,true)
  dump(value)
  return value
end
function M.compare()
  assert(M.before,'Run s6.capture("before") first, and keep the player stationary')
  local after=snapshot(); local lost={}
  for _,kind in ipairs({'roads','terrain'}) do
    for id,path in pairs(M.before[kind]) do
      if after[kind][id]~=path then lost[#lost+1]={kind=kind,id=id,path=path} end
    end
  end
  local out={changed_unrelated_objects=lost,status=after.status,engine_collision_proven=false}
  dump(out); jsonWriteFile('/stage6a-compare.json',out,true)
  return out
end
function M.rays()
  assert(type(castRayStatic)=='function','This engine build has no castRayStatic helper; use the physical contact checks')
  local baseline=jsonReadFile('/stage6a-baseline-roads.json')
  assert(baseline and baseline.anchor,'Baseline metadata is not available')
  local geo=require('beamngEarth/geodesy')
  local origin=geo.newAnchor(baseline.scenario.reference_origin.lat,baseline.scenario.reference_origin.lon)
  local current=geo.newAnchor(baseline.anchor.lat,baseline.anchor.lon)
  local function point(x,y)
    local lat,lon=origin:enuToGeodetic(x,y,0)
    local xx,yy,zz=current:geodeticToWorld(lat,lon,3)
    return vec3(xx,yy,zz)
  end
  local function ray(x1,y1,x2,y2)
    local start,finish=point(x1,y1),point(x2,y2)
    local direction=finish-start; local distance=direction:length()
    return castRayStatic(start,direction/distance,distance)
  end
  local out={a_east_wall_distance=ray(90,88,50,88),c_west_wall_distance=ray(205,88,240,88),
    expected_baseline='A about 18 m; C no hit before 35 m',
    expected_overlay='A about 6 m; C about 5 m',status=beamngEarth_settings.uiBuildingsState()}
  dump(out); jsonWriteFile('/stage6a-rays.json',out,true)
  return out
end
return M
