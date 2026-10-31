-- Building-only adapter over roads.lua's objects, placement helpers, queues and slots.
-- No road/deck/sidewalk/prop is created, removed, hidden or replaced here.
local M = {}
local B={'bld','bld2','bld3','bld4','bldcol'}
local function copy(t) local r={} for k,v in pairs(t or {}) do r[k]=v end return r end
local function finite(v) return type(v)=='number' and v==v and math.abs(v)<math.huge end
local function object(id) return id and scenetree.findObjectById(id) end
local function hidden(id,on) local o=object(id) if o then o:setHidden(on) end end
local function addBox(plan,t,what,box)
  if type(box)~='table' or #box~=6 then error('invalid building collision bounds') end
  for _,v in ipairs(box) do if not finite(v) then error('nonfinite building collision bounds') end end
  for _,x in ipairs({box[1],box[4]}) do for _,y in ipairs({box[2],box[5]}) do
    local xx,yy=x,y
    if t.geo then
      local pp=t.pls and t.pls[what]
      if not pp then error('building placement is unavailable') end
      xx,yy=pp.xf:apply(x,y,box[3])
    end
    local b=plan.box
    if b then b[1],b[2],b[3],b[4]=math.min(b[1],xx),math.min(b[2],yy),math.max(b[3],xx),math.max(b[4],yy)
    else plan.box={xx,yy,xx,yy} end
  end end
end
function M.new(c)
  local H={now=c.now,reloads=c.reloads,requestCollision=c.requestCollision}
  function H.plan(msg,a)
    local plans={}
    for _,r in ipairs(msg.targets) do
      local near=r.kind=='near' or r.kind=='near_geo'
      local geo=r.kind:sub(-4)=='_geo'
      local key=near and (geo and 'g:'..r.key or tostring(msg.anchorId)..':'..r.key)
        or (geo and c.farGeoKey(r.kind=='skyline_geo' and 'S' or 'F',r.key)
          or tostring(msg.anchorId)..':'..(r.kind=='skyline' and 'S' or '')..r.key)
      local map=near and c.tiles() or c.far()
      local old=map[key]
      if r.reuse then
        if not old or old.ghost or old.orphan or not old.activation or old.activation.content~=r.content then error('reused game resource is unavailable; retry a fresh transaction') end
        local kept={}
        local function keep(id)
          if not object(id) then error('reused building object is unavailable; retry a fresh transaction') end
          kept[#kept+1]=id
        end
        if near then
          for _,w in ipairs(B) do
            if (old.parts or {})[w] or (old.data or {})[w] or #((old.colliders or {})[w] or {})>0 then keep(old[w]) end
            for _,name in ipairs({w,'x:'..w}) do
              local sl=(old.slots or {})[name]
              if sl and (not FS or not FS:fileExists(sl.path)) then error('reused mesh file is unavailable') end
            end
          end
        else
          local part=old.geo and (old.parts or {}).shape
          local sl=part and part.slot or old.slot
          if part or sl then keep(part and part.id or old.id) end
          if sl and (not FS or not FS:fileExists(sl.path)) then error('reused mesh file is unavailable') end
        end
        plans[#plans+1]={row=r,key=key,near=near,old=old,new=old,create={},oldIds={},kept=kept,reuse=true}
      else
        if near and not old and not r.allowEmpty then return nil,'near target is not in the current world' end
        if old and (old.ghost or old.orphan) and not old.geo then
          local frame=old.activation and old.activation.frame
          if not frame or frame[1]~=r.frame[1] or frame[2]~=r.frame[2] then return nil,'retained road frame differs; preserve occupied roads until a safe world handoff' end
        end
        if old and (old.oldPart or old.oldPartSlot) then return nil,'existing exact-variant handoff is unfinished' end
        local p={row=r,key=key,near=near,old=old,new=copy(old),create={},oldIds={},oldSlots={},oldHidden={}}
        local n=p.new
        n.activation={content=r.content,session=msg.sessionId,seq=msg.seq,frame=r.frame}
        n.ghost,n.orphan,n.activationDropPending=nil,nil,nil
        if near then
          n.pendingBuildings=nil
          n.anchor,n.objs,n.queued=msg.anchorId,n.objs or {},n.queued or 0
          for _,k in ipairs({'parts','slots','colliders','colWant','colAt','cq','exactShape','exactOf','data'}) do n[k]=copy(n[k]) end
          n.data.buildingPending=nil
          if geo then
            n.geo,n.gkey=true,r.key
            n.frame=c.frameOf(r)
            if not n.frame then return nil,'invalid near geotile frame' end
          else
            local x,y=r.key:match('^(%-?%d+)_(%-?%d+)$')
            n.cx,n.cy=(tonumber(x)+0.5)*c.tileM,(tonumber(y)+0.5)*c.tileM
          end
          for _,w in ipairs(B) do
            if old and old[w] then p.oldIds[#p.oldIds+1]=old[w] end
            n[w],n.cq[w],n.colAt[w],n.colWant[w],n.parts[w],n.exactShape[w],n.exactOf[w]=nil,nil,nil,nil,nil,nil,nil
            if n.slots[w] then p.oldSlots[#p.oldSlots+1]=n.slots[w] end
            if n.slots['x:'..w] then p.oldSlots[#p.oldSlots+1]=n.slots['x:'..w] end
            n.slots[w],n.slots['x:'..w]=nil,nil
            n.colliders[w]=(r.data.colliders or {})[w]
            n.data[w]=r.data[w]
            local e=r.data[w]
            if e then
              if type(e)~='table' or type(e.pos)~='table' or #e.pos~=3 or not r.slots[w] then error('incomplete building mesh descriptor') end
              for _,v in ipairs(e.pos) do if not finite(v) then error('nonfinite building placement') end end
              local sl=c.msgSlot(r,w)
              local part={shape=sl.path,pos=e.pos,pivot=e.pivot or 0,relief=e.relief or 0}
              n.parts[w],n.slots[w]=part,sl
              if r.exact[w] then
                local ex=c.msgSlot(r,'x:'..w)
                if not ex then error('missing exact building slot') end
                n.exactShape[w],n.exactOf[w],n.slots['x:'..w]=ex.path,msg.anchorId,ex
              end
              p.create[#p.create+1]={what=w,part=part,collision=w=='bldcol'}
              if w=='bldcol' then p.collision=true; n.colWant[w]=true end
            end
          end
          if n.parts.bld and not n.parts.bldcol then return nil,'near building activation requires a lean collision mesh' end
          if old and old.bldcol then p.collision=true end
          if geo then c.placeGeo(n,a) end
          for _,t in ipairs({old or {},n}) do
            for _,w in ipairs(B) do for _,b in ipairs((t.colliders or {})[w] or {}) do addBox(p,t,w,b) end end
          end
          if (p.collision or n.parts.bld) and not p.box then return nil,'complete building collision bounds are required' end
          if n.parts.bldcol and not c.resident(n) then return nil,'building collision is outside the normal residency range' end
          n.h=r.data.h or n.h
        else
          n={anchor=msg.anchorId,activation=n.activation,shape=r.data.shape,pos=r.data.pos,lamps=r.data.lamps,h=r.data.h}
          p.new=n
          local sl=c.msgSlot(r,'shape')
          if geo then
            n.geo,n.gkey,n.kind,n.frame,n.parts=true,r.key,r.kind=='skyline_geo' and 'S' or 'F',c.frameOf(r),{}
            if not n.frame then return nil,'invalid distant geotile frame' end
            if n.kind=='S' then local z,x,y=c.parse(r.key); n.sky={z,x,y} end
            if sl then n.parts.shape={shape=sl.path,slot=sl,pos=r.data.pos,pivot=r.data.pivot or 0,near=n.kind=='F' and r.key or nil}; c.farPlace(n,n.parts.shape,a) end
          else
            n.slot,n.shape=sl,sl and sl.path or nil
            if r.kind=='skyline' then local x,y=r.key:match('^(%-?%d+)_(%-?%d+)$'); n.sky={tonumber(x),tonumber(y)} end
          end
          if sl then p.create[1]={what='shape',collision=false} end
          if old then
            if old.id then p.oldIds[#p.oldIds+1]=old.id; p.oldHidden[old.id]=old.hidden or false end
            if old.slot then p.oldSlots[#p.oldSlots+1]=old.slot end
            for _,v in pairs(old.parts or {}) do
              if v.id then p.oldIds[#p.oldIds+1]=v.id; p.oldHidden[v.id]=v.hidden or false end
              if v.slot then p.oldSlots[#p.oldSlots+1]=v.slot end
            end
          end
        end
        for _,q in ipairs(p.create) do
          local sl=r.slots[q.what]
          if not sl or not FS or not FS:fileExists(sl[1]) then error('prepared mesh file is unavailable') end
          if r.exact[q.what] and not FS:fileExists(r.slots['x:'..q.what][1]) then error('exact mesh file is unavailable') end
        end
        plans[#plans+1]=p
      end
    end
    return plans
  end
  function H.safe(plans)
    if not c.enabled() then return false,'road renderer is disabled' end
    if c.fociBroken() then return false,'collision focus enumeration is unavailable' end
    local all={}
    if not be or not be.getObjectCount or not be.getObject then return false,'vehicle enumeration is unavailable' end
    local count=be:getObjectCount()
    if count>1024 then return false,'vehicle safety enumeration exceeds budget' end
    for i=0,count-1 do
      local v=be:getObject(i)
      if not v or not v.getPosition or not v.getVelocity then return false,'incomplete vehicle safety state' end
      local pos,vel=v:getPosition(),v:getVelocity()
      if not pos or not vel or not finite(pos.x) or not finite(pos.y) or not finite(vel.x) or not finite(vel.y) then return false,'invalid vehicle safety state' end
      all[#all+1]={x=pos.x,y=pos.y,r=200+20*math.sqrt(vel.x^2+vel.y^2)}
    end
    for _,f in ipairs(c.foci()) do
      if not finite(f.x) or not finite(f.y) or not finite(f.r) then return false,'invalid collision focus' end
      all[#all+1]={x=f.x,y=f.y,r=math.max(200,f.r)+2000} -- extra foci do not report velocity
    end
    for _,p in ipairs(plans) do if p.near and not p.reuse and p.box then
      for _,f in ipairs(all) do
        local dx=math.max(p.box[1]-f.x,0,f.x-p.box[3]); local dy=math.max(p.box[2]-f.y,0,f.y-p.box[4])
        if dx*dx+dy*dy<=f.r*f.r then return false,'vehicle/collision focus inside building activation safety margin' end
      end
    end end
    return true
  end
  function H.current(plans)
    for _,p in ipairs(plans) do if (p.near and c.tiles() or c.far())[p.key]~=p.old then return false end end
    return true
  end
  function H.hold(msg,plans)
    if msg.osm then c.attribution() end
    for _,p in ipairs(plans) do for _,sl in ipairs(p.oldSlots or {}) do c.journal(sl,p.near and p.collision) end end
    for _,r in ipairs(msg.targets) do for w in pairs(r.slots) do c.journal(c.msgSlot(r,w),w=='bldcol' or w=='x:bldcol') end end
    c.flushJournal()
  end
  function H.create(p,i)
    local q,n=p.create[i],p.new
    local walk
    if p.near then walk=n.geo and c.geoWalk(n,q.what) or n.parts[q.what]
    elseif n.geo then local part=n.parts.shape; walk={shape=part.shape,pos=part.at,rot=part.rot}
    else walk={shape=n.shape,pos=n.pos} end
    c.materials()
    c.used(walk.shape)
    local allocated
    local ok,id=pcall(c.create,walk,q.collision and 'Collision Mesh' or false,function(o) allocated=o end)
    if not ok then
      if allocated then pcall(function() allocated:delete() end) end
      if q.collision then c.requestCollision() end
      error(id)
    end
    q.id=id
    c.slotMade(id,walk.shape)
    if not q.collision then hidden(id,true) end -- lean collision meshes contain no visible geometry
    if p.near then n[q.what]=id
    elseif n.geo then n.parts.shape.id=id else n.id=id end
  end
  function H.exists(plans)
    for _,p in ipairs(plans) do
      if (p.committed or p.reuse) and (p.near and c.tiles() or c.far())[p.key]~=p.new then return false end
      for _,id in ipairs(p.kept or {}) do if not object(id) then return false end end
      for _,q in ipairs(p.create) do if not object(q.id) then return false end end
    end
    return true
  end
  function H.colliding(plans) for _,p in ipairs(plans) do if p.collision then return true end end return false end
  function H.commit(plans,msg)
    if not H.current(plans) then error('target changed before handoff') end
    -- Engine decoration callbacks must succeed before any old building is hidden.
    for _,p in ipairs(plans) do if not p.reuse and not p.near and p.old then c.farLamps(p.key,p.old,false) end end
    -- Reveal can fail; keep all old objects until every new object is usable.
    local ok,err=pcall(function()
      for _,p in ipairs(plans) do if not p.reuse then
        for _,q in ipairs(p.create) do hidden(q.id,false) end
        for _,id in ipairs(p.oldIds) do hidden(id,true) end
      end end
    end)
    if not ok then
      for _,p in ipairs(plans) do for _,id in ipairs(p.oldIds) do pcall(hidden,id,(p.oldHidden or {})[id] or false) end end
      error(err)
    end
    for _,p in ipairs(plans) do if not p.reuse then
      local n=p.new
      if p.near then
        if p.old then n.queued=p.old.queued end
        c.replaceQueues(p.old,n,p.key)
        for _,q in ipairs(p.create) do if q.collision then
          n.cq[q.what]={key=p.key,tile=n,what=q.what,geo=n.geo,walk=n.parts[q.what],activation=true}
          n.colAt[q.what]=c.now()
        end end
        c.tiles()[p.key]=n
      else
        c.far()[p.key]=n
        if p.row.kind=='far' or p.row.kind=='far_geo' then c.farSeen()[p.key]=true end
      end
      for _,sl in ipairs(p.oldSlots) do c.slotDrop(sl) end
      for _,id in ipairs(p.oldIds) do c.delete(id) end
      p.committed=true
    end end
    c.refreshFar(plans)
  end
  function H.retired(plans)
    for _,p in ipairs(plans) do for _,id in ipairs(p.oldIds) do if object(id) then return false end end end
    return true
  end
  function H.abort(plans,msg)
    local collision=false
    for _,p in ipairs(plans) do if not p.committed then
      for _,q in ipairs(p.create) do if q.id then
        if not q.collision then pcall(hidden,q.id,true) end
        c.delete(q.id); collision=collision or q.collision
      end end
    end end
    for _,r in ipairs(msg.targets) do
      local committed=false
      for _,p in ipairs(plans) do if p.row==r and p.committed then committed=true end end
      if not committed then for w in pairs(r.slots) do c.slotDrop(c.msgSlot(r,w)) end end
    end
    if collision then c.requestCollision() end
  end
  return H
end
return M
