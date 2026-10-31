-- Read-only, governed resource proof. No objects/files are deleted here.
local M={}
local pending=nil
local function names()
  if not scenetree.findClassObjects then error('scene inventory API unavailable') end
  local rows={}
  for _,class in ipairs({'TSStatic','TerrainBlock'}) do
    local found=scenetree.findClassObjects(class)
    if type(found)~='table' then error('incomplete scene inventory') end
    for _,id in ipairs(found) do
      rows[#rows+1]={class=class,id=id}
      if #rows>10000 then error('scene inventory exceeds quota') end
    end
  end
  table.sort(rows,function(a,b) return a.class..tostring(a.id)<b.class..tostring(b.id) end)
  return rows
end
local function identity(rows)
  local out={} for _,r in ipairs(rows) do out[#out+1]=r.class..':'..tostring(r.id) end
  return table.concat(out,'|')
end
function M.start(message,context,send)
  if pending then return false end
  pending={message=message,context=context,send=send,index=1,sessions={},slots={}}
  return true
end
function M.update(gov)
  local p=pending; if not p then return end
  if not gov:try('roadWalk') then return end
  local started=os.clock()
  local ok,err=pcall(function()
    if not p.rows then
      if p.context.blocked() then error('road ownership unavailable after full Lua VM reset') end
      p.rows=names(); p.identity=identity(p.rows); p.before=jsonEncode(p.context.sessions())
    end
    for _=1,64 do
      local row=p.rows[p.index]; if not row then break end
      local obj=scenetree.findObject(row.id) or (scenetree.findObjectById and scenetree.findObjectById(row.id))
      if not obj or not obj.getField then error('scene changed during resource inventory') end
      local path=obj:getField(row.class=='TerrainBlock' and 'terrainFile' or 'shapeName',0)
      if type(path)~='string' then error('scene resource field unavailable') end
      local sid=path:match('/[Bb]eam[Nn][Gg][Ee]arth/([%w_-]+)/')
      if sid then p.sessions[sid]=true end
      local n=path:match('/slot_(%d+)%.cdae$')
      if sid and n then p.slots[sid..':'..n]={sid,tonumber(n)} end
      p.index=p.index+1
    end
    if p.index>#p.rows then
      local sessions=p.context.sessions()
      if p.before~=jsonEncode(sessions) or p.identity~=identity(names()) then error('scene/queues changed during resource inventory; retry cleanup') end
      for _,sid in ipairs(sessions) do p.sessions[sid]=true end
      for _,sl in ipairs(p.context.slots()) do p.slots[sl[1]..':'..sl[2]]=sl end
      local out={} for sid in pairs(p.sessions) do out[#out+1]=sid end; table.sort(out)
      if #out>64 then error('session inventory exceeds quota') end
      local slots={} for _,sl in pairs(p.slots) do slots[#slots+1]=sl end
      if #slots>4096 then error('slot inventory exceeds quota') end
      p.send(p.message,true,out,'Complete stable scene and renderer inventory',slots,p.context.pending())
      pending=nil
    end
  end)
  gov:charge('roadWalk',(os.clock()-started)*1000)
  if not ok then p.send(p.message,false,{},tostring(err):sub(1,200)); pending=nil end
end
function M.reset() pending=nil end
return M
