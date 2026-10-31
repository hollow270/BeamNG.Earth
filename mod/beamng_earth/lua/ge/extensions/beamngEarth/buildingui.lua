-- UI bridge: private per-connection write capability never enters Vue state.
local M={}
local secret,session,epoch=nil,nil,nil
local id=0
local state={available=false,state='unavailable',reason='Companion app not connected'}
local client=nil
pcall(function() client=settings.getValue('beamngEarth_buildingClient') end)
function M.client() return client end
local allowed={status=true,preview=true,accept=true,retry=true,disable=true,enable=true,rollback=true,cleanup=true}
function M.hello(m)
  require('beamngEarth/buildinginventory').reset()
  secret,session,epoch=m.buildingControlToken,m.sessionId,m.buildingEpoch
  if type(m.buildingClient)=='string' and #m.buildingClient==64 then
    client=m.buildingClient
    pcall(function() settings.setValue('beamngEarth_buildingClient',client) end)
  end
  state={available=m.buildingControl==1,state='idle',reason=m.buildingControlReason or '',collision_engine_verified=false}
end
function M.disconnect()
  require('beamngEarth/buildinginventory').reset()
  secret,session,epoch=nil,nil,nil
  state={available=false,state='unavailable',reason='Companion app disconnected; accepted local data is retained'}
end
function M.message(m)
  if type(m)~='table' or m.type~='building_status' or #jsonEncode(m)>262144 then return false end
  state=m; return true
end
function M.state() return state end
function M.inventory(m,context,send)
  if m.sessionId~=session or m.epoch~=epoch or type(m.challenge)~='string' or #m.challenge~=64 then return false end
  if not context.fence(m.sequence) then return false end
  return require('beamngEarth/buildinginventory').start(m,context,function(msg,complete,sessions,reason,slots,transactions)
    send({type='building_inventory',sessionId=session,epoch=epoch,challenge=msg.challenge,client=client,
      complete=complete,sessions=sessions,reason=reason,slots=slots or {},pending=transactions or {},sequence=msg.sequence})
  end)
end
function M.revoke(send)
  if session and epoch then send({type='building_revoke',sessionId=session,epoch=epoch}) end
  secret=nil; state.available=false; state.reason='Manual building writes are unavailable in multiplayer'
end
function M.command(op,payload,send)
  if not allowed[op] or not secret or not state.available then return {error=state.reason or 'Not authorized'} end
  if type(payload)~='string' or #payload>2048 then return {error='Invalid building request'} end
  local ok,data=pcall(jsonDecode,payload)
  if not ok or type(data)~='table' then return {error='Invalid building request'} end
  local fields={bounds=true,preview_token=true,confirm=true,expected_state=true}
  for k in pairs(data) do if not fields[k] then return {error='Unsupported building request field'} end end
  id=id+1
  local m={type='building_control',id=id,sessionId=session,epoch=epoch,authorization=secret,op=op}
  for k,v in pairs(data) do m[k]=v end
  if not send(m) then return {error='Companion app disconnected'} end
  return {id=id}
end
return M
