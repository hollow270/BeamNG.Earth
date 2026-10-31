-- Explicit, bounded prepared-building transaction. Objects, slots, deletion and
-- collision reloads belong to roads.lua; this module only sequences that adapter.
if rawget(_G,'beamngEarthBuildingController') then return rawget(_G,'beamngEarthBuildingController') end
local M = { VERSION = 1 }
local session, epoch, high = nil, nil, 0
local pending, last = nil, nil
local assembly = nil
local frame = 0
local kinds = { near=true, near_geo=true, far=true, far_geo=true, skyline=true, skyline_geo=true }
local whats = { bld=true,bld2=true,bld3=true,bld4=true,bldcol=true,shape=true }
local function integer(n) return type(n)=='number' and n==math.floor(n) and n>=0 and n<9007199254740992 end
local function hex(s,n) return type(s)=='string' and #s==n and s:match('^[0-9a-f]+$')~=nil end
local function ack(p,status,detail,send)
  detail = tostring(detail or ''):sub(1,256)
  if p.status==status and p.detail==detail then return end
  p.status,p.detail = status,detail
  send({type='building_ack',sessionId=p.msg.sessionId,epoch=p.msg.epoch,anchorId=p.msg.anchorId,
    seq=p.msg.seq,transaction=p.msg.transaction,status=status,detail=detail})
end
local function valid(m,limit)
  if type(m)~='table' or m.type~='building_activate' or m.version~=1 or m.sessionId~=session or m.epoch~=epoch
      or not integer(m.anchorId) or not integer(m.seq) or not hex(m.transaction,32) or not hex(m.publication,32)
      or (m.revision~=nil and not hex(m.revision,64)) or type(m.osm)~='boolean' or type(m.targets)~='table' or #m.targets<1 or #m.targets>(limit or 32) then return false end
  local top={type=true,version=true,sessionId=true,epoch=true,anchorId=true,seq=true,transaction=true,publication=true,revision=true,osm=true,targets=true}
  for k in pairs(m) do if not top[k] then return false end end
  local seen,routes,slots,total = {},{},{},0
  for _,t in ipairs(m.targets) do
    if type(t)~='table' or not kinds[t.kind] or not hex(t.slot,64) or not hex(t.generation,32) or not hex(t.content,64)
        or not hex(t.identity,40) or type(t.key)~='string' or #t.key>80 or seen[t.slot]
        or type(t.data)~='table' or type(t.slots)~='table' or type(t.exact)~='table'
        or type(t.reuse)~='boolean' or type(t.allowEmpty)~='boolean' or type(t.frame)~='table' or #t.frame~=2 then return false end
    local allowed={slot=true,kind=true,key=true,generation=true,content=true,identity=true,reuse=true,data=true,slots=true,exact=true,allowEmpty=true,frame=true}
    for k in pairs(t) do if not allowed[k] then return false end end
    if not t.key:match('^g%d+_%d+_%d+$') and not t.key:match('^%-?%d+_%-?%d+$') then return false end
    if (t.kind:sub(-4)=='_geo')~=(t.key:match('^g%d+_%d+_%d+$')~=nil) or routes[t.kind..':'..t.key] then return false end
    routes[t.kind..':'..t.key]=true
    for _,v in ipairs(t.frame) do if type(v)~='number' or v~=v or math.abs(v)==math.huge then return false end end
    if math.abs(t.frame[1])>85 or math.abs(t.frame[2])>180 then return false end
    seen[t.slot]=true
    for w,sl in pairs(t.slots) do
      local base = w:match('^x:(.*)$') or w
      if not whats[base] or type(sl)~='table' or #sl~=2 or type(sl[1])~='string' or not integer(sl[2]) then return false end
      if t.kind:sub(1,4)=='near' then
        if base=='shape' or (w:sub(1,2)=='x:' and t.kind~='near_geo') then return false end
      elseif w~='shape' then return false end
      local sid,n = sl[1]:match('^/temp/beamngEarth/([%w_-]+)/slot_(%d+)%.cdae$')
      if sid~=session or not n or slots[sl[1]] then return false end
      slots[sl[1]],total = true,total+1
      if total>256 then return false end
    end
  end
  return #jsonEncode(m)<=1048576
end

function M.hello(s,e)
  if pending then pending.host.abort(pending.plans or {},pending.msg); pending=nil end
  session,epoch,high,last,assembly = s,e,0,nil,nil
end
function M.disconnect()
  if pending then pending.host.abort(pending.plans or {},pending.msg); pending=nil end
  epoch=nil
  assembly=nil
end
local function receive(m,aid,host,send,limit)
  if not valid(m,limit) or m.anchorId~=aid then return false end
  if m.seq<=high then
    local p = pending or last
    if p and m.seq==p.msg.seq and m.transaction==p.msg.transaction then
      if p.status=='activated' and not p.host.exists(p.plans) then
        p.status=nil; ack(p,'failed','previous activation objects are no longer present; retry a new transaction',send)
        return true
      end
      local status,detail = p.status or 'deferred',p.detail or 'waiting'
      p.status=nil; ack(p,status,detail,send)
      return true
    end
    return false
  end
  if pending then return false end -- no superseding a partially installed transaction
  high=m.seq
  pending={msg=m,host=host,phase='waiting',plans={},cursor=1,stage=1}
  ack(pending,'deferred','waiting for complete targets and a safe collision region',send)
  return true
end
function M.receive(m,aid,host,send) return receive(m,aid,host,send,32) end
function M.receiveBatch(m,aid,host,send)
  if type(m)~='table' or m.type~='building_activate_batch' or not integer(m.part) or not integer(m.parts)
      or m.parts<2 or m.parts>4 or m.part<1 or m.part>m.parts or not integer(m.total)
      or m.total<33 or m.total>128 or m.parts~=math.ceil(m.total/32) then return false end
  local packet={} for k,v in pairs(m) do if k~='part' and k~='parts' and k~='total' then packet[k]=v end end
  packet.type='building_activate'
  if not valid(packet,32) or packet.anchorId~=aid then return false end
  if m.seq<=high then
    local previous=pending or last
    if previous and previous.msg.seq==m.seq and previous.msg.transaction==m.transaction then return receive(previous.msg,aid,host,send,128) end
    return false
  end
  if not assembly then assembly={transaction=m.transaction,seq=m.seq,parts=m.parts,total=m.total,rows={}} end
  local a=assembly
  if a.transaction~=m.transaction or a.seq~=m.seq or a.parts~=m.parts or a.total~=m.total then return false end
  if a.rows[m.part] and jsonEncode(a.rows[m.part])~=jsonEncode(packet) then return false end
  a.rows[m.part]=packet
  local count=0 for _ in pairs(a.rows) do count=count+1 end
  if count<a.parts then return true end
  local whole={} for k,v in pairs(a.rows[1]) do whole[k]=v end
  whole.targets={}
  for i=1,a.parts do
    local row=a.rows[i]
    for _,k in ipairs({'sessionId','epoch','anchorId','seq','transaction','publication','revision','osm'}) do
      if row[k]~=whole[k] then assembly=nil; return false end
    end
    for _,t in ipairs(row.targets) do whole.targets[#whole.targets+1]=t end
  end
  assembly=nil
  if #whole.targets~=m.total then return false end
  return receive(whole,aid,host,send,128)
end
function M.cancel(m,send)
  local p=pending
  if not p and assembly and m.sessionId==session and m.epoch==epoch and m.transaction==assembly.transaction and m.seq==assembly.seq then
    high=math.max(high,m.seq); assembly=nil
    ack({msg=m},'failed','incomplete batch cancelled before any staging',send)
    return true
  end
  if not p or m.sessionId~=session or m.epoch~=epoch or m.seq~=p.msg.seq or m.transaction~=p.msg.transaction
      or m.anchorId~=p.msg.anchorId then return false end
  if p.phase=='retiring' then return false end -- complete observable retirement; never roll back an installed world
  p.host.abort(p.plans,p.msg)
  ack(p,'failed','cancelled before installation; staged resources are retiring',send)
  last,pending=p,nil
  return true
end
local function fail(p,reason,send)
  p.host.abort(p.plans,p.msg)
  ack(p,'failed',reason,send)
  last,pending=p,nil
end
function M.update(gov,aid,a,send,dtSim)
  if dtSim~=nil and dtSim<=0 then return end -- no collision-readiness claim while physics is paused
  frame=frame+1
  local p=pending
  if not p then return end
  if aid~=p.msg.anchorId or not a then return fail(p,'anchor changed before completion',send) end
  if p.phase=='waiting' then
    local ok,plans,reason=pcall(p.host.plan,p.msg,a)
    if not ok then return fail(p,'invalid/unavailable prepared asset: '..tostring(plans),send) end
    if not plans then return ack(p,'deferred',reason,send) end
    local checked,safe,why=pcall(p.host.safe,plans)
    if not checked then return ack(p,'deferred','vehicle safety enumeration failed',send) end
    if not safe then return ack(p,'deferred',why,send) end
    local ok,err=pcall(p.host.hold,p.msg,plans)
    if not ok then return fail(p,tostring(err),send) end
    p.plans,p.phase=plans,'staging'
    ack(p,'deferred','staging building objects under the existing governor',send)
  end
  if p.phase=='staging' or p.phase=='ready' then
    local checked,safe,why=pcall(p.host.safe,p.plans)
    if not checked then return fail(p,'vehicle safety enumeration failed during staging',send) end
    if not safe then return fail(p,'safety changed during staging: '..tostring(why),send) end
    if not p.host.current(p.plans) then return fail(p,'target objects changed during staging',send) end
  end
  if p.phase=='staging' then
    while p.cursor<=#p.plans do
      local plan=p.plans[p.cursor]
      if p.stage>#plan.create then p.cursor,p.stage=p.cursor+1,1
      else
        if not gov:try('roadWalk') then return end
        local timer=p.host.now()
        local ok,err=pcall(p.host.create,plan,p.stage)
        gov:charge('roadWalk',p.host.now()-timer)
        if not ok then return fail(p,'mesh import failed: '..tostring(err),send) end
        p.stage=p.stage+1
      end
    end
    p.fence,p.at=p.host.reloads(),frame
    p.collision=p.host.colliding(p.plans)
    if p.collision then p.host.requestCollision() end
    p.phase='ready'
  end
  if p.phase=='ready' then
    if p.collision then
      if p.host.reloads()<=p.fence then return end
      p.readyAfter=p.readyAfter or frame
      if frame<p.readyAfter+2 then return end
    elseif frame<p.at+2 then return end
    if not p.host.exists(p.plans) then return fail(p,'staged object disappeared',send) end
    local ok,err=pcall(p.host.commit,p.plans,p.msg)
    if not ok then return fail(p,'handoff failed: '..tostring(err),send) end
    p.phase,p.at='retiring',frame
    p.fence=p.host.reloads()
    ack(p,'installing','building objects handed off; waiting for old collision retirement',send)
  end
  if p.phase=='retiring' then
    -- Finish collision retirement even if a vehicle approaches after commit. Do
    -- not tear out new colliders or recreate old invisible obstacles in its path.
    local checked,safe=pcall(p.host.safe,p.plans)
    p.safetyChanged=not checked or not safe
    if not p.host.retired(p.plans) then return end
    if not p.requested then
      p.requested=true
      p.fence,p.at=p.host.reloads(),frame
      if p.collision then p.host.requestCollision() end
    end
    if p.collision then
      if p.host.reloads()<=p.fence then return end
      p.retireAfter=p.retireAfter or frame
      if frame<p.retireAfter+2 then return end
    elseif frame<p.at+2 then return end
    if not p.host.exists(p.plans) then return fail(p,'installed building object was removed before acknowledgment',send) end
    ack(p,'activated',p.safetyChanged and 'retired old collision; new building retained as a vehicle approached; engine readiness remains observational'
      or 'objects present; old objects gone; collision reload observed plus two update frames',send)
    last,pending=p,nil
  end
end
function M.status() return pending and {phase=pending.phase,status=pending.status,seq=pending.msg.seq} or last and {status=last.status,seq=last.msg.seq} or {} end
function M.inventoryFence(seq)
  if not integer(seq) then return false end
  high=math.max(high,seq)
  assembly=nil -- never staged; later unseen packets at/below this fence are rejected
  return true
end
function M.references() return pending and {pending.msg.transaction} or {} end
rawset(_G,'beamngEarthBuildingController',M)
return M
