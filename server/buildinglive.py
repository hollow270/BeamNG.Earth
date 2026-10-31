"""Local manual controls and bounded background orchestration over Stages 1--5B1.

All slow workflow/catalog/activation I/O runs on one authority worker. The asyncio
thread owns real connections/trackers; the authority sees copied connection state
and queues output. No worker writes into a real socket or mutates a real tracker.
"""
import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
from types import SimpleNamespace
import time

import buildingactivation as activation
import buildingrefresh as regeneration
import buildingtargets
from buildingoverlaystore import ResourceLimit, StalePreview, _intersects, _union
import mapcells
import osmbuildings as osm

OPS = {"status", "preview", "accept", "retry", "disable", "enable", "rollback", "cleanup"}
TERMINAL = {"activated", "failed", "superseded", "orphaned", "retired"}
TRACKERS = ("roads", "bldfar", "bldsky")
TABLES = ("sent", "empty", "building", "car", "futs", "retry", "info", "skins", "shown", "started")
RENDER_FLAGS=("near_visual","sidewalks","gas_stations","buildings","far_format")


def _render_signature(server,conn):
    return tuple(getattr(server,k,None) for k in RENDER_FLAGS)+(getattr(conn,'building_scale',None),)


def preview_for_ui(report):
    """Bounded review projection. The original server proposal/token is untouched."""
    keys=("token","selection","context_bounds","overlay_revision","enabled_before","enabled_on_accept",
          "replication_timestamp","omission_is_deletion")
    out={k:deepcopy(report[k]) for k in keys}
    out["counts"]=deepcopy(report["merge"]["counts"])
    out["building_counts"]={a:sum(r["action"]==a and r["subject"]=="building" for r in report["merge"]["decisions"]) for a in out["counts"]}
    out["part_counts"]={a:sum(r["action"]==a and r["subject"]!="building" for r in report["merge"]["decisions"]) for a in out["counts"]}
    out["issue_count"]=len(report["issues"])
    out["overture_only_count"]=len(report["overture_only"])
    out["overture_only"]=report["overture_only"][:64]
    out["decisions"]=report["merge"]["decisions"][:128]
    out["issues"]=report["issues"][:128]
    out["details_truncated"]=len(report["merge"]["decisions"])>128 or len(report["overture_only"])>64 or len(report["issues"])>128
    out["changes"]=[dict(feature_id=r["feature_id"],action=r["action"],
        geometry_changed=r["geometry_before"]!=r["geometry_after"],
        attributes=list(r["attributes_changed"])[:24],parts_before=len(r["parts_before"]),parts_after=len(r["parts_after"]))
        for r in report["changes"][:64]]
    if len(regeneration._json(out).encode())>128*1024:
        raise ResourceLimit("review projection exceeds UI budget")
    return out


def _loopback(conn):
    try:
        return ipaddress.ip_address(conn.peer[0]).is_loopback
    except (ValueError, TypeError, AttributeError):
        return False


def _public_error(error):
    # Endpoint credentials and local paths must never escape via exception text.
    kind = type(error).__name__
    messages = {
        "StalePreview": "Preview expired or its accepted state/baseline changed. Preview again.",
        "StaleGeneration": "A newer revision or world change superseded this preparation. Retry local preparation.",
        "BaselineMismatch": "Original building context is unavailable, incomplete or changed. Visit/pre-cache the area first.",
        "ResourceLimit": "A resource quota was reached. Inspect retention diagnostics before retrying.",
        "CorruptStorage": "Storage or an ownership journal failed validation. Retained resources require recovery.",
        "Offline": "Offline: fetching OSM is unavailable. Local preparation and retry do not fetch OSM.",
        "PolicyDenied": "The configured endpoint is denied by the existing network policy.",
        "Throttled": "Request spacing, area cooldown or endpoint Retry-After is still in effect.",
        "StaleSnapshot": "The OSM snapshot is stale or has no usable replication timestamp. Preview again later.",
        "IncompleteResponse": "The endpoint returned incomplete geometry; no empty-area or deletion assumption was made.",
        "OverpassError": "The endpoint reported an Overpass error; no data was accepted.",
        "Busy": "Another building operation is still running.",
        "TimeoutError": "The OSM or local preparation operation timed out. Retry explicitly; no fetch is repeated automatically.",
        "Rejected": "Activation could not establish current targets, assets or world safety. Retry local preparation.",
    }
    result = dict(type=kind, message=messages.get(kind,"The operation failed. No automatic network retry will be made."))
    if kind=="HTTPFailure" and type(getattr(error,"code",None)) is int:
        result["message"]=f"The configured OSM endpoint returned HTTP {error.code}. No data was accepted."
    wait = getattr(error,"retry_after",None)
    if isinstance(wait,(int,float)) and 0 <= wait <= 86400:
        result["retry_after"] = wait
    return result


class World:
    """Copied descriptors, with existing compute dispatch delegated to the real server."""
    def __init__(self, server, conn):
        self.server = server
        self.building_current_targets=True
        self.connections = [conn]
        self.anchors = dict(server.anchors)
        self._far_geo_done = dict(getattr(server,"_far_geo_done",{}))
        self.orphans=dict(getattr(server,"orphans",{}))
        self.claimed=frozenset(sid for c in server.connections for sid in getattr(c,"sessions",()))
        for name in ("near_visual", "sidewalks", "gas_stations", "buildings", "far_format"):
            if hasattr(server,name):
                setattr(self,name,getattr(server,name))

    def __getattr__(self,name):
        return getattr(self.server,name)

    def claimed_sessions(self): return self.claimed

    def activate_building_overlays(self,connection,publication,*,manual=False):
        from earthd import Server
        return Server.activate_building_overlays(self,connection,publication,manual=manual)


def freeze(conn):
    result = SimpleNamespace()
    for name in ("active", "building_epoch", "building_activation", "building_batches", "focus", "vel", "building_scale"):
        setattr(result,name,deepcopy(getattr(conn,name,None)))
    result.slot_holds = dict(conn.slot_holds)
    for name in TRACKERS:
        original = getattr(conn,name,None)
        tracker = copy(original) if original is not None else None
        if tracker is not None:
            for table in TABLES:
                if hasattr(original,table):
                    setattr(tracker,table,copy(getattr(original,table)))
        setattr(result,name,tracker)
    return result


def _frame(conn):
    return (conn.active,getattr(conn,"building_epoch",None))


def _stream_signature(conn):
    tables=[]
    for name in TRACKERS:
        tracker=getattr(conn,name,None)
        keys=set()
        if tracker is not None:
            for field in ("sent","empty","building","car","shown"):
                keys.update(getattr(tracker,field,()))
        if len(keys)>16384: raise ResourceLimit("streaming inspection exceeds quota")
        tables.append((name,tuple(sorted(keys))))
    return tuple(tables)


def _pending_live(conn,row):
    import geotiles
    try:
        key=row['key']; kind=row['kind']
        if kind.endswith('_geo'):
            _,x,y=geotiles.parse(key)
        else: x,y=map(int,key.split('_'))
        return activation._live(conn,regeneration.TileRequest(kind,x,y))
    except (TypeError,ValueError):
        return False


class Service:
    def __init__(self, server, *, local_control=False, clock=time.monotonic):
        self.server, self.clock, self.local_control = server,clock,local_control
        self.pool = ThreadPoolExecutor(1,thread_name_prefix="building-authority")
        self.owner, self.secret, self.epoch = None,None,None
        self.authority, self.facade, self.mirror = None,None,None
        self.pending = {}
        self.coverage = set()
        self.busy = False
        self.closed = False
        self.last_command = float("-inf")
        self.seen = OrderedDict()
        self.operations = OrderedDict()
        self.snapshot = None
        self.state_token = None
        self.failed_signature = None
        self.applied = {}
        self.current = dict(state="idle",game_activated=False,accepted_revision=None,activated_revision=None)
        self.runner = None
        self.task = None
        self.context_task = None
        self.unavailable_reason = None
        self.ack_queue=OrderedDict()
        self.release_queue={}
        self.drain_task=None
        self.completed_state=None
        self.last_scan=float("-inf")
        self.client_id=None
        self.activation_states=OrderedDict()
        self.challenge=None
        self.inventory_future=None
        self.authorized_client=None
        self.revoked=False
        self.initialized=False
        self.inflight_ack=0
        self.boot_messages=[]
        self.boot_bytes=0

    def hello(self,conn,msg):
        self._drop_boot_messages()
        self.initialized=False
        workflow=self.server.__dict__.get("_building_manual")
        configured = bool(workflow is not None and workflow.client is not None and workflow.client.config.enabled and workflow.client.config.endpoint)
        permitted = configured and self.local_control and _loopback(conn) and msg.get("buildingControl")==1 \
            and type(msg.get("buildingControl")) is int and type(msg.get("buildingBatches")) is int \
            and msg.get("buildingBatches")==1 and msg.get("buildingMultiplayer",False) is False \
            and getattr(conn,'building_activation',0)==1 and isinstance(getattr(conn,'building_epoch',None),str)
        if permitted and self.owner not in (None,conn):
            permitted = False
        result = dict(buildingControl=0,buildingControlReason="Enable a permitted endpoint and local manual controls in the companion app.")
        if permitted:
            supplied=msg.get("buildingClient")
            if self.authorized_client is not None and supplied!=self.authorized_client:
                result["buildingControlReason"]="This companion is bound to a different local game; use a separate companion instance."
                conn.building_control_denied=True
                return result
            self.client_id=supplied if isinstance(supplied,str) and len(supplied)==64 and all(c in "0123456789abcdef" for c in supplied) else secrets.token_hex(32)
            self.authorized_client=self.client_id
            self.revoked=False
            self.owner, self.secret, self.epoch = conn,secrets.token_hex(32),conn.building_epoch
            self.seen.clear()
            self.applied.clear()
            # The authority worker switches identity using the copied socket/epoch key.
            result.update(buildingControl=1,buildingControlToken=self.secret,buildingControlReason="",buildingClient=self.client_id)
            if self.runner is None or self.runner.done():
                self.runner=asyncio.create_task(self.run())
        elif configured and not _loopback(conn):
            result["buildingControlReason"]="Manual building writes are restricted to the explicitly enabled local game."
        elif configured and self.owner is not None:
            result["buildingControlReason"]="Another local game owns manual building controls."
        return result

    def public(self,conn):
        authorized = conn is not None and conn is self.owner and _frame(conn)[1]==self.epoch and conn in self.server.connections and not self.revoked
        state=deepcopy(self.current) if authorized else dict(state="unavailable",game_activated=False)
        state.update(available=authorized,reason=self.unavailable_reason or ("" if authorized else
            "Manual refresh requires explicit endpoint configuration and local authorization."),
            max_dimension_m=500,collision_engine_verified=False)
        if self.revoked: state["reason"]="Manual building writes are unavailable in multiplayer."
        workflow=self.server.__dict__.get("_building_manual")
        configured=bool(workflow is not None and workflow.client is not None and workflow.client.config.enabled and workflow.client.config.endpoint)
        offline=bool(configured and workflow.client.fetcher.offline_flag)
        state.update(configured=configured,fetch_available=authorized and configured and not offline,
                     fetch_reason="Offline: local retry is available; fetching OSM is disabled." if offline else "")
        if authorized:
            state.update(accepted_state=self.state_token,enabled=bool(self.snapshot and self.snapshot.enabled),
                can_rollback=bool(self.snapshot and self.snapshot.history),deferred=list(self.pending.values())[:128])
            if self.pending and state["state"]=="activated":
                state.update(state="activation_deferred",game_activated=False,
                    detail="Additional dependent tiles need local preparation; the last acknowledged revision remains visible.")
        return state

    def publish(self,conn=None):
        conn=conn or self.owner
        if conn is not None and conn in self.server.connections:
            conn.send(dict(type="building_status",**self.public(conn)))

    def _set(self,state,**values):
        self.current.update(state=state,game_activated=state=="activated",**values)
        self.publish()

    def command(self,conn,msg):
        fields={"type","id","sessionId","epoch","authorization","op","bounds","preview_token","confirm","expected_state"}
        if not isinstance(msg,dict) or set(msg)-fields or len(regeneration._json(msg).encode())>4096:
            raise ValueError("invalid building command")
        op=msg.get("op")
        if not isinstance(op,str) or op not in OPS or type(msg.get("id")) is not int or not 0<=msg["id"]<2**31:
            raise ValueError("invalid building operation/id")
        if self.revoked or conn is not self.owner or conn not in self.server.connections or not _loopback(conn) or \
                (msg.get("sessionId"),msg.get("epoch"))!=(self.server.sid,self.epoch) or \
                not isinstance(msg.get("authorization"),str) or len(msg["authorization"])!=64 or \
                any(c not in '0123456789abcdef' for c in msg["authorization"]) or not hmac.compare_digest(msg["authorization"],self.secret or ""):
            conn.send(dict(type="building_status",**dict(self.public(None),error=dict(type="PermissionDenied",message="Local building control is not authorized."))))
            return
        signature=hashlib.sha256(regeneration._json({k:v for k,v in msg.items() if k!="authorization"}).encode()).hexdigest()
        if msg["id"] in self.seen:
            if self.seen[msg["id"]]!=signature:
                raise ValueError("building request id reused with different input")
            self.publish(conn); return
        if op=="status":
            self.publish(conn); return
        if self.busy or self.clock()-self.last_command<0.25:
            conn.send(dict(type="building_status",**dict(self.public(conn),error=dict(type="Busy",message="A building operation is running or was just requested."))))
            return
        if op in ("accept","disable","enable","rollback","cleanup") and msg.get("confirm") is not True:
            raise ValueError("explicit confirmation is required")
        if op=="preview":
            bounds=osm._area(msg.get("bounds"))
            w,s,e,n=bounds
            import math
            if (n-s)*111320>500 or (e-w)*111320*math.cos(math.radians(0 if s<=0<=n else min(abs(s),abs(n))))>500:
                raise ValueError("select at most 500 metres per dimension")
        elif "bounds" in msg:
            raise ValueError("bounds are only accepted for preview")
        if op=="accept" and (not isinstance(msg.get("preview_token"),str) or len(msg["preview_token"])!=64):
            raise ValueError("invalid preview token")
        if op in ("disable","enable","rollback") and (not isinstance(msg.get("expected_state"),str) or len(msg["expected_state"])!=64 or msg["expected_state"]!=self.state_token):
            self._set("superseded",error=_public_error(StalePreview())); return
        self.last_command=self.clock()
        self.seen[msg["id"]]=signature
        while len(self.seen)>32: self.seen.popitem(last=False)
        self.busy=True
        self.task=asyncio.create_task(self._command(conn,deepcopy(msg)))

    async def _work(self,fn,*args):
        return await asyncio.get_running_loop().run_in_executor(self.pool,fn,*args)

    def _capture(self,conn):
        frozen=freeze(conn)
        frozen._authority_key=(id(conn),conn.building_epoch)
        return frozen,World(self.server,frozen),_frame(conn)

    def _worker(self,frozen,world,fn):
        if self.facade is None or getattr(self.facade,"_authority_key",None)!=frozen._authority_key:
            self.facade=freeze(frozen)
            self.facade._authority_key=frozen._authority_key
        else:
            self.facade.__dict__.update(vars(frozen))
        facade=self.facade
        output=[]
        facade.send=lambda m:output.append(deepcopy(m))
        world.connections=[facade]
        if self.authority is not None:
            self.authority.s=world
        workflow=self.server._manual_building_workflow(retry=True)
        token=workflow.discovery_context.set(world)
        try:
            import buildingretention
            guards=buildingretention.retained_guards(Path(self.server.cache_dir)/'building-generations')
            result=fn(world,facade)
            snapshot,state=workflow.store.load_state()
            stale=[m for m in output if m.get('type') in ('building_activate','building_activate_batch') and
                   self.authority.transactions[m['transaction']]['state_token']!=state]
            if stale:
                self._abandon_unsent(output)
                output=[]
                result=dict(result,state='superseded',game_activated=False,held_slots=0,
                            detail='Accepted data changed before the buffered activation could be sent.')
            routing=None
            if self.authority is not None:
                routing=dict(holds=dict(self.authority.holds),guards=set(self.authority.guards),
                    bindings={k:deepcopy(v) for k,v in self.authority.bindings.items() if k[0]==id(facade)},
                    transactions={k:dict(state=t["state"],state_token=t["state_token"],requests=deepcopy(t["requests"]),targets=[dict(kind=r["kind"],key=r["key"]) for r in t["targets"]])
                                  for k,t in self.authority.transactions.items() if t["conn"] is facade})
            return result,output,snapshot,state,routing,freeze(facade),guards
        finally:
            workflow.discovery_context.reset(token)

    async def call(self,conn,fn):
        previous_state=self.state_token
        frozen,world,frame=self._capture(conn)
        rendering=_render_signature(world,frozen)
        before=dict(frozen.slot_holds)
        result,output,snapshot,state,routing,updated,guards=await self._work(self._worker,frozen,world,fn)
        self.snapshot,self.state_token=snapshot,state
        self.current["accepted_revision"]=snapshot.revision
        import blockworker
        bounds=[e.bounds for e in snapshot.entries]
        self.coverage=set()
        self.coverage.update(guards)
        for box in bounds:
            w,s,e,n=box
            self.coverage.update(map(str,mapcells.cells_for_bbox(s,w,n,e,max_cells=512)))
        if len(self.coverage)>512:
            raise ResourceLimit("active overlay coverage exceeds budget")
        if self.authority is not None:
            self.coverage.update(self.authority.guards)
        activation_output=any(m.get('type') in ('building_activate','building_activate_batch') for m in output)
        current=not self.closed and not self.revoked and conn in self.server.connections and _frame(conn)==frame and conn is self.owner and \
                (not activation_output or rendering==_render_signature(self.server,conn))
        if current:
            for n,v in updated.slot_holds.items():
                if before.get(n)!=v: conn.slot_holds[n]=v
            for n,v in before.items():
                if n not in updated.slot_holds and conn.slot_holds.get(n)==v:
                    conn.slot_holds.pop(n,None)
                    if not self.server.slot_held(n): self.server.matslots.unheld(n)
            for name in TRACKERS:
                real,old,new=getattr(conn,name,None),getattr(frozen,name,None),getattr(updated,name,None)
                if real is not None and old is not None and new is not None:
                    for field in ("sent","empty","building"):
                        a,b=getattr(old,field,set()),getattr(new,field,set())
                        getattr(real,field,set()).difference_update(a-b)
                        getattr(real,field,set()).update(b-a)
                    for k,future in getattr(old,"futs",{}).items():
                        if k not in getattr(new,"futs",{}) and getattr(real,"futs",{}).get(k) is future:
                            real.futs.pop(k,None)
            self._routing(conn,routing)
            self.initialized=True
            queued,self.boot_messages=self.boot_messages,[]
            self.boot_bytes=0
            for owner,queued_frame,message in queued:
                if owner is conn and queued_frame==frame: conn.send(message)
            for message in output: conn.send(message)
            if previous_state!=self.state_token: self.publish(conn)
        elif output and self.authority is not None:
            # No queued command crossed the socket; this is positive non-delivery evidence.
            await self._work(self._abandon_unsent,output)
            self.mirror=None
        return result,current

    def _abandon_unsent(self,output):
        abandoned={}
        for m in output:
            t=self.authority.transactions.get(m.get("transaction"))
            if t and t["state"]=="scheduled":
                for n,(serial,tx) in list(self.authority.holds.items()):
                    if tx==t["transaction"]: self.authority.release(n,serial,persist=False)
                for owner in t["owners"]: self.server.matslots.release(owner)
                t["state"]="retired"
                abandoned[id(t["conn"])]=t["conn"]
        for conn in abandoned.values(): self.authority.context_changed(conn)

    def _routing(self,conn,routing):
        if routing is None: return
        mirror=object.__new__(activation.Manager)
        mirror.s=self.server
        mirror.holds=routing["holds"]
        mirror.guards=routing["guards"]|self.coverage
        mirror.bindings={(id(conn),k[1],k[2]):v for k,v in routing["bindings"].items()}
        mirror.transactions={k:dict(v,conn=conn) for k,v in routing["transactions"].items()}
        mirror.pending_targets=self.pending
        self.mirror=mirror
        for _,kind,key in mirror.bindings:
            self.pending.pop(kind+":"+key,None)
        self.applied={k:v["content"] for k,v in mirror.bindings.items()}

    def filter_message(self,conn,msg):
        if conn is not self.owner or msg.get("type") not in ("roads_tile","roads_exact","bldfar_tile","bldsky_tile"):
            return msg
        if not self.initialized:
            size=len(regeneration._json(msg).encode())
            if len(self.boot_messages)>=128 or self.boot_bytes+size>16*1024*1024:
                raise ResourceLimit('building recovery message queue exceeds quota')
            self.boot_messages.append((conn,_frame(conn),deepcopy(msg))); self.boot_bytes+=size
            return None
        if self.mirror is None and self.coverage:
            mirror=object.__new__(activation.Manager)
            mirror.s=self.server; mirror.holds={}; mirror.guards=self.coverage
            mirror.bindings={}; mirror.transactions={}; mirror.pending_targets=self.pending
            self.mirror=mirror
        return self.mirror.filter_message(conn,msg) if self.mirror is not None else msg

    def _drop_boot_messages(self):
        for conn,_,msg in self.boot_messages:
            for path,serial in msg.get('slots',{}).values():
                n=int(path.rsplit('slot_',1)[-1].split('.')[0])
                if conn.slot_holds.get(n)==serial:
                    conn.slot_holds.pop(n,None); self.server.matslots.unheld(n)
        self.boot_messages=[]; self.boot_bytes=0

    def _manager(self,world):
        if self.authority is None:
            self.authority=activation.Manager(world,self.server.__dict__.get("_building_refresh"))
            self.authority.client_id=self.client_id
        self.authority.compact()
        return self.authority

    async def _command(self,conn,msg):
        op=msg["op"]
        try:
            if self.revoked or conn is not self.owner: return
            if op=="preview":
                self._set("fetching",error=None,preview=None,selection=msg["bounds"])
                def preview(world,c):
                    return preview_for_ui(self.server.preview_osm_buildings(msg["bounds"],manual=True,enable=True))
                report,current=await self.call(conn,preview)
                if current:
                    self._set("preview_available",preview=report)
                    self._set("awaiting_acceptance")
                return
            if op=="accept":
                self._set("accepting",error=None)
                result,current=await self.call(conn,lambda w,c:self.server.accept_osm_buildings(msg["preview_token"],manual=True,prepare=False))
                if result["status"]!="accepted": raise StalePreview("acceptance could not be confirmed")
                if not current: self._set("superseded"); return
                self.current.update(workflow_token=msg["preview_token"],preview=None)
                self._set("accepted")
            elif op in ("disable","enable","rollback"):
                def change(w,c):
                    store=self.server.mapdata._building_store
                    return store.rollback(expected_state=msg["expected_state"]) if op=="rollback" else \
                        store.set_enabled(op=="enable",expected_state=msg["expected_state"])
                _,current=await self.call(conn,change)
                if not current: self._set("superseded"); return
                self._set("accepted",preview=None,error=None)
            elif op=="cleanup":
                await self.cleanup(conn)
                return
            elif op=="retry":
                _,current=await self.call(conn,lambda w,c:self.server.retry_osm_buildings(manual=True,prepare=False))
                if not current: self._set("superseded"); return
            self.failed_signature=None
            if conn is self.owner:
                await self.prepare(conn,explicit=True)
        except Exception as e:
            self._set("superseded" if isinstance(e,StalePreview) else "failed",error=_public_error(e))
        finally:
            self.busy=False

    def _regions(self):
        if self.snapshot is None: return []
        regions=[e.bounds for e in self.snapshot.entries] or [r["info"]["bounds"] for r in self.snapshot.refreshes]
        changed=self.snapshot.affected_bounds
        if changed is not None and (not regions or tuple(_union(regions))!=tuple(changed)):
            # Rollback can bound widely separated old regions with one huge box.
            # Retained cell fences supply separate affected components after restart.
            guarded=[mapcells.CellKey(*map(int,k.split('/'))).bounds() for k in self.coverage]
            clipped=[(max(b[0],changed[0]),max(b[1],changed[1]),min(b[2],changed[2]),min(b[3],changed[3]))
                     for b in guarded if _intersects(b,changed)]
            clipped=[b for b in clipped if b[0]<b[2] and b[1]<b[3]]
            regions.extend(clipped or [changed])
        return regions

    async def prepare(self,conn,*,explicit=False):
        if self.mirror is not None:
            pending=next((t for t in self.mirror.transactions.values() if t["state"] in ("scheduled","deferred","installing")),None)
            if pending:
                def resume(w,c):
                    transaction=next(t for t in self.authority.transactions.values() if t["conn"] is c and t["state"] in ("scheduled","deferred","installing"))
                    if transaction["state_token"]==self.state_token:
                        return self.authority.activate(c,transaction["publication"],manual=True)
                    self.authority.cancel_if_stale(c)
                    return self.authority.status(transaction["transaction"])
                state,current=await self.call(conn,resume)
                if current: self._activation_state(state)
                return
        regions=self._regions()
        if not regions:
            self.completed_state=self.state_token
            self._set("accepted",detail="No currently visible targets. Accepted data will be prepared when visited.")
            return
        self._set("preparing",error=None)
        def prepare(world,c):
            import buildingretention
            self.current_retention=buildingretention.collect(world,self.authority,limit=8)
            requests={}
            for bounds in regions:
                discovery=buildingtargets.discover(world,bounds,current_only=True)
                for req in discovery.requests:
                    if req.slot in discovery.relevant and activation._live(c,req): requests[req.slot]=req
            if len(requests)>128: raise ResourceLimit("live target registry exceeds 128 entries")
            if not requests: return None
            import blockworker
            cells=set()
            for request in requests.values(): cells.update(blockworker.building_cells(*request.geometry()))
            if len(cells)>512: raise ResourceLimit("local building dependency warming exceeds quota")
            for key in sorted(cells):
                self.server.mapdata.cell(key,layers=("buildings","building_parts","roads"))
            def priority(req):
                tracker=activation._tracker(c,req.kind)
                return (0 if req.kind.startswith("near") and (req.x,req.y) in getattr(tracker,"car",()) else
                        1 if req.kind.startswith("near") else 2 if req.kind.startswith("far") else 3,req.slot)
            order=tuple(r.slot for r in sorted(requests.values(),key=priority))
            return dict(self.server.refresh_building_overlays(tuple(requests.values()),expected_state=self.state_token,replace_requests=True,priority=order),
                        live_requests=[r.wire() for r in requests.values()])
        result,current=await self.call(conn,prepare)
        if not current: self._set("superseded"); return
        self.current["resources"]=self.current_retention
        if result is None:
            self.completed_state=self.state_token
            self._set("accepted",detail="Accepted locally; no visible dependent tiles yet."); return
        self._set("prepared",publication=result["publication"])
        if any(req["kind"].startswith("near") and not any((req["x"],req["y"]) in getattr(getattr(conn,"roads",None),name,())
                for name in ("sent","empty")) for req in result["live_requests"]):
            self.failed_signature=(self.state_token,_frame(conn),tuple(sorted(self.pending)),_stream_signature(conn))
            self._set("activation_deferred",detail="Prepared locally; waiting for ordinary near-road streaming to complete.")
            return
        # One logical handoff can span four bounded messages, but only one game commit.
        def activate(world,c):
            world._building_activation=self._manager(world)
            state=world.activate_building_overlays(c,result["publication"],manual=True)
            if state["state"]=="rejected": raise activation.Rejected("current generation could not be activated")
            return state
        state,current=await self.call(conn,activate)
        if current:
            self.current.update(transaction=state["transaction"])
            self._activation_state(state)
        else: self._set('superseded',detail='The world or rendering settings changed before dispatch; retry locally.')

    def _activation_state(self,result):
        self.activation_states[result["transaction"]]=deepcopy(result)
        while len(self.activation_states)>32: self.activation_states.popitem(last=False)
        state={"scheduled":"activation_scheduled","deferred":"activation_deferred","installing":"activating"}.get(result["state"],result["state"])
        if state=='activation_deferred' and (result.get('detail') or '').startswith('staging building objects'):
            state='activating'
        if state=="activating" and "old collision retirement" in (result.get("detail") or ""):
            state="retiring"
        values=dict(detail=result.get("detail"),held_slots=result.get("held_slots",0))
        if state=="activated":
            values.update(activated_revision=result["revision"],activated_enabled=result["overlay_enabled"])
            self.completed_state=self.state_token
            self.failed_signature=None
        self._set(state,**values)

    def queue_ack(self,conn,msg):
        if conn is not self.owner or len(regeneration._json(msg).encode())>2048:
            return
        key=msg.get("transaction")
        if not isinstance(key,str) or len(key)!=32 or (key not in self.ack_queue and len(self.ack_queue)>=8):
            raise ValueError("building acknowledgement queue exceeds bounds")
        self.ack_queue[key]=(conn,deepcopy(msg))
        self._drain()

    def queue_releases(self,conn,rows):
        if conn is not self.owner: return
        for n,serial in rows:
            if type(n) is int and type(serial) is int:
                if len(self.release_queue)>=256 and n not in self.release_queue: break
                self.release_queue[n]=serial
        self._drain()

    def _drain(self):
        if self.drain_task is None or self.drain_task.done():
            self.drain_task=asyncio.create_task(self._drain_messages())

    async def _drain_messages(self):
        while self.ack_queue or self.release_queue:
            if self.ack_queue:
                _,(conn,msg)=self.ack_queue.popitem(last=False)
                await self.acknowledge(conn,msg)
            if self.release_queue:
                rows=list(self.release_queue.items()); self.release_queue.clear()
                await self.releases(self.owner,rows)

    async def acknowledge(self,conn,msg):
        if conn is not self.owner or self.authority is None: return
        if msg.get("transaction")!=self.current.get("transaction") or \
                (msg.get("sessionId"),msg.get("epoch"),msg.get("anchorId"))!=(self.server.sid,self.epoch,conn.active): return
        self.inflight_ack+=1
        try:
            state,current=await self.call(conn,lambda w,c:self.authority.acknowledge(c,msg))
            if current: self._activation_state(state)
        except activation.Rejected:
            return
        except Exception as e:
            self._set("superseded",error=_public_error(e))
        finally:
            self.inflight_ack-=1

    async def releases(self,conn,rows):
        if conn is None or conn is not self.owner or self.authority is None: return
        def release(w,c):
            for n,serial in rows: self.authority.release(n,serial,persist=False)
            self.authority.compact()
            self.authority._save()
        await self.call(conn,release)
        self.failed_signature=None

    def context_changed(self,conn,*,disconnect=False):
        if conn is not self.owner: return
        self._drop_boot_messages()
        self.initialized=False
        self.last_scan=float('-inf')
        self.completed_state=None
        old=self.facade
        self.mirror=None
        if old is not None and self.authority is not None:
            async def invalidate():
                await self._work(self.authority.context_changed,old)
            self.context_task=asyncio.create_task(invalidate())
        for status in self.activation_states.values():
            status.update(state='orphaned',game_activated=False,detail='Connection or anchor changed; current activation is unconfirmed.')
        self._set("superseded",activated_revision=None,activated_enabled=None,transaction=None,
                  detail="The game connection or anchor changed; local data remains accepted.")
        if disconnect:
            self.owner=None; self.secret=None; self.epoch=None

    async def run(self):
        while not self.closed:
            try:
                conn=self.owner
                if not self.revoked and conn is not None and conn in self.server.connections and not self.busy and conn.active in self.server.anchors:
                    for name,row in list(self.pending.items()):
                        if not _pending_live(conn,row): self.pending.pop(name,None)
                    if self.snapshot is None or self.clock()-self.last_scan>=1:
                        await self.call(conn,lambda w,c:None)
                        self.last_scan=self.clock()
                    signature=(self.state_token,_frame(conn),tuple(sorted(self.pending)),_stream_signature(conn))
                    active=self.mirror and any(t["state"] in ("scheduled","deferred","installing") for t in self.mirror.transactions.values())
                    if active and any(t["state"] in ("scheduled","deferred","installing") and (t["state_token"]!=self.state_token or
                                      any(not activation._live(conn,regeneration.TileRequest(**r)) for r in t["requests"].values()))
                                      for t in self.mirror.transactions.values()):
                        await self.call(conn,lambda w,c:self.authority.cancel_if_stale(c))
                        self._set("superseded",detail="Accepted data changed; the older handoff is being cancelled or retired safely.")
                    if (self.pending or self.completed_state!=self.state_token and self._regions()) and not active and not self.inflight_ack and signature!=self.failed_signature:
                        self.busy=True
                        self.failed_signature=signature
                        try:
                            await self.prepare(conn)
                        except Exception as e:
                            self.failed_signature=signature
                            self._set("failed",error=_public_error(e))
                        finally: self.busy=False
            except Exception as e:
                self._set("failed",error=_public_error(e))
            await asyncio.sleep(0.5)

    async def cleanup(self,conn):
        import buildingretention
        self.inventory_future=asyncio.get_running_loop().create_future()
        self.challenge=secrets.token_hex(32)
        sequence=self.authority.sequence if self.authority is not None else 0
        conn.send(dict(type="building_reconcile",sessionId=self.server.sid,epoch=self.epoch,
            challenge=self.challenge,sequence=sequence))
        proof=None
        try:
            proof=await asyncio.wait_for(self.inventory_future,5)
        except asyncio.TimeoutError:
            pass  # missing evidence is retention, never deletion
        finally:
            self.challenge=None; self.inventory_future=None
        report,current=await self.call(conn,lambda w,c:buildingretention.collect(w,self.authority,proof=proof))
        if current:
            self.failed_signature=None
            for sid in report["released_sessions"]: self.server.release_session(sid)
            if proof is None or not proof["complete"]:
                report["uncertain"].append("No complete current game inventory; session and slot resources retained.")
            self.current["resources"]=report; self.publish()

    def inventory(self,conn,msg):
        fields={"type","sessionId","epoch","challenge","client","complete","sessions","slots","pending","sequence","reason"}
        if not isinstance(msg,dict) or set(msg)!=fields or len(regeneration._json(msg).encode())>512*1024:
            raise ValueError("invalid resource inventory")
        if conn is not self.owner or self.challenge is None or msg["challenge"]!=self.challenge or \
                (msg["sessionId"],msg["epoch"],msg["client"])!=(self.server.sid,self.epoch,self.client_id):
            raise ValueError("stale resource inventory")
        import re
        msg=deepcopy(msg)
        for key in ("sessions","slots","pending"):
            if msg[key]=={}: msg[key]=[] # an empty Lua array may serialize as an object
        if type(msg["complete"]) is not bool or not isinstance(msg["sessions"],list) or len(msg["sessions"])>64 or \
                any(not isinstance(s,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",s) for s in msg["sessions"]) or \
                not isinstance(msg["slots"],list) or len(msg["slots"])>4096 or \
                any(not isinstance(s,list) or len(s)!=2 or not isinstance(s[0],str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",s[0])
                    or type(s[1]) is not int or not 0<=s[1]<2**31 for s in msg["slots"]) or \
                not isinstance(msg["pending"],list) or len(msg["pending"])>1 or \
                any(not isinstance(t,str) or not re.fullmatch(r"[0-9a-f]{32}",t) for t in msg["pending"]) or \
                type(msg["sequence"]) is not int or msg["sequence"]!=(self.authority.sequence if self.authority else 0) or \
                not isinstance(msg["reason"],str) or len(msg["reason"])>256:
            raise ValueError("resource inventory exceeds bounds or fences")
        if self.inventory_future is not None and not self.inventory_future.done():
            self.inventory_future.set_result(deepcopy(msg))

    async def close(self):
        self.closed=True
        if self.runner is not None:
            self.runner.cancel()
            await asyncio.gather(self.runner,return_exceptions=True)
        if self.task is not None: await asyncio.gather(self.task,return_exceptions=True)
        if self.drain_task is not None: await asyncio.gather(self.drain_task,return_exceptions=True)
        if self.context_task is not None: await asyncio.gather(self.context_task,return_exceptions=True)
        await asyncio.to_thread(self.pool.shutdown,wait=True,cancel_futures=True)


def install_from_environment(server):
    """Explicit process-local configuration. No filename, endpoint or credential comes from UI."""
    if os.environ.get("BNGE_OSM_BUILDINGS")!="on": return None
    service=server._building_live=Service(server,local_control=os.environ.get("BNGE_OSM_LOCAL_CONTROL")=="on")
    try:
        endpoint=os.environ.get("BNGE_OSM_ENDPOINT")
        if not endpoint: raise osm.ConfigurationError("an explicit endpoint is required")
        raw=os.environ.get("BNGE_OSM_HEADERS","{}")
        if len(raw)>8192: raise osm.ConfigurationError("credential header configuration is too large")
        spacing=float(os.environ.get("BNGE_OSM_SPACING_SECONDS","60"))
        cooldown=float(os.environ.get("BNGE_OSM_COOLDOWN_SECONDS","600"))
        if spacing<60 or cooldown<600: raise osm.ConfigurationError("production request limits cannot be lowered")
        config=osm.Config(enabled=True,endpoint=endpoint,headers=json.loads(raw),min_spacing_s=spacing,area_cooldown_s=cooldown)
        from urllib.parse import urlsplit
        if server.net.policy(urlsplit(endpoint).hostname).denied:
            raise osm.ConfigurationError("the endpoint is denied by network policy")
        server.configure_osm_buildings(config)
    except Exception:
        service.unavailable_reason="Local OSM configuration is missing, invalid or denied by network policy. Check companion configuration."
    return service
