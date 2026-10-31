"""Development-only activation of a checked prepared catalog, through existing shape slots.

This is not retrieval, acceptance, a second mesher or a game-object resource pool.
Run on the server event-loop thread. Game completion requires an explicit fenced ack;
slot owners survive disconnect until an exact slot/serial release is observed.
"""
from copy import deepcopy
import hashlib
import os
from itertools import islice
import json
from pathlib import Path
import re
import threading
import uuid

import buildingrefresh as br
from buildingoverlaystore import CorruptStorage, ResourceLimit
import geotiles
import matslots

VERSION = 1
WHATS = ("bld", "bld2", "bld3", "bld4", "bldcol")
MAX_TARGETS, MAX_SLOTS, MAX_TRANSACTIONS, MAX_MESSAGE = 32, 256, 32, 1024*1024
MAX_COPY_BYTES, MAX_RETAINED_BYTES = 128*1024*1024,512*1024*1024


def defer_message(server,conn,msg):
    """Retain normal road/terrain content; never stream unprepared building geometry."""
    out=deepcopy(msg)
    near=out.get("type") in ("roads_tile","roads_exact")
    if out.get("type")=="roads_exact":
        out["parts"]={w:p for w,p in out.get("parts",{}).items() if w not in WHATS}
    else:
        data=out.get("data")
        if not isinstance(data,dict):
            import staging
            data=staging.inline_json(server.out_dir,out.get("path",""),max_bytes=MAX_MESSAGE)
        if not isinstance(data,dict):
            data={} if not near else None
        if data is not None:
            for w in WHATS if near else ("shape","parts","lamps"):
                data.pop(w,None)
            data["colliders"]={w:b for w,b in data.get("colliders",{}).items() if w not in WHATS}
            data["buildingPending"]=True
            data["h"]="building-pending-"+hashlib.sha256(str(msg.get("h",data.get("h",""))).encode()).hexdigest()[:20]
            out["data"]=data
            if near: out["h"]=data["h"]; out["exact"]={w:p for w,p in out.get("exact",{}).items() if w not in WHATS}
    for w,sl in list(out.get("slots",{}).items()):
        if not near or w in WHATS or w.startswith("x:bld") or out.get("data") is None and out.get("type")!="roads_exact":
            n=int(sl[0].rsplit("slot_",1)[-1].split(".")[0])
            if conn.slot_holds.get(n)==sl[1]:
                del conn.slot_holds[n]; server.matslots.unheld(n)
            del out["slots"][w]
    return None if near and out.get("type")=="roads_tile" and out.get("data") is None else out


class Rejected(ValueError):
    pass


def _key(req):
    if req.kind.endswith("_geo"):
        import blockworker
        z = geotiles.Z if req.kind=="near_geo" else blockworker.far_geo_zoom("bldfar" if req.kind=="far_geo" else "bldsky")
        return geotiles.key(req.x,req.y,z)
    return f"{req.x}_{req.y}"


def _tracker(conn, kind):
    return getattr(conn,"roads" if kind.startswith("near") else "bldsky" if kind.startswith("skyline") else "bldfar",None)


def _live(conn,req):
    tracker = _tracker(conn,req.kind)
    if tracker is None or bool(getattr(tracker,"geo",False))!=req.kind.endswith("_geo"):
        return False
    return any((req.x,req.y) in getattr(tracker,n,()) for n in
               ("sent","empty","building","car","futs","retry","info","skins","shown"))


def _matches(path,info):
    try:
        if path.stat().st_size!=info["size"]:
            return False
        digest=hashlib.sha256()
        with path.open("rb") as f:
            remaining=info["size"]
            while remaining:
                chunk=f.read(min(65536,remaining))
                if not chunk:
                    return False
                digest.update(chunk); remaining-=len(chunk)
            if f.read(1): return False
        return digest.hexdigest()==info["sha256"]
    except OSError:
        return False


class _OneConnection:
    def __init__(self, server, conn):
        self.server,self.connections = server,[conn]
    def __getattr__(self,name):
        return getattr(self.server,name)


class Manager:
    def __init__(self, server, coordinator=None):
        self.s, self.thread = server,threading.get_ident()
        if server.mapdata is None or server.mapdata._building_store is None or not server.buildings:
            raise Rejected("building overlays must be configured")
        if getattr(server,"far_format","cdae")!="cdae":
            raise Rejected("building activation requires the existing cdae shape-slot format")
        directory = Path(server.cache_dir)/"building-generations"
        if coordinator is None:
            if not (directory/"catalog.json").is_file():
                raise Rejected("prepare a catalog before activation")
            coordinator = br.Coordinator(directory,server.mapdata,server.mapdata._building_store,None)
        self.c = coordinator
        self.store = server.mapdata._building_store
        self.sequence, self.transactions, self.bindings, self.holds = 0,{}, {},{}
        self.guards,self.pending_targets=set(),{}
        suffix = hashlib.sha256(server.sid.encode()).hexdigest()[:16]
        self.path = self.c.io.root/f"activation-{suffix}.json"
        if self.path.exists():
            # A live manager must not be recreated and forget pending ownership.
            raise Rejected("activation journal already exists for this session; retain its original manager")
        roots = tuple(islice(self.c.io.root.iterdir(),257))
        if len(roots)>254:
            raise ResourceLimit("prepared control-directory entry budget reached; retain uncertain files")
        if sum(p.name.startswith("activation-") and p.suffix==".json" for p in roots)>=16:
            raise ResourceLimit("activation journal retention budget reached; explicit retirement is required")
        for path in roots:
            if path.name.startswith("activation-") and path.suffix==".json":
                old=self.c.io._read(path)
                if not isinstance(old,dict):
                    raise CorruptStorage("invalid prior activation journal")
                guard=old.get("guards",[])
                if not isinstance(guard,list) or len(guard)>512 or any(not isinstance(k,str) or not re.fullmatch(r"\d+/\d+/\d+",k) for k in guard):
                    raise CorruptStorage("invalid prior activation coverage journal")
                self.guards.update(guard)
        if len(self.guards)>512:
            raise ResourceLimit("retained activation coverage exceeds budget")

    def _thread(self):
        if threading.get_ident()!=self.thread:
            raise Rejected("invoke activation on the server event-loop thread")

    def _save(self):
        if sum(1 for _ in islice(self.c.io.root.iterdir(),255))>=254:
            raise ResourceLimit("activation temporary/control-file entry budget reached")
        rows = []
        for t in self.transactions.values():
            rows.append({k:deepcopy(t[k]) for k in ("transaction","seq","state","publication","revision","overlay_enabled","acknowledged","anchor","epoch","state_token","generations")})
        body = self.c.io._document(dict(session=self.s.sid,transactions=rows,guards=sorted(self.guards),
            holds={str(n):dict(serial=h[0],transaction=h[1]) for n,h in self.holds.items()}))
        if len(body)>MAX_MESSAGE:
            raise ResourceLimit("activation ownership journal exceeds budget")
        self.c.io._atomic_write(self.path,body)

    def held(self, n):
        return n in self.holds

    def release(self, n, serial, *, persist=True):
        """Only an exact observed release permits reuse, including after disconnect/reconnect."""
        h = self.holds.get(n)
        if h is None or type(n) is not int or type(serial) is not int or h[0]!=serial:
            return False
        del self.holds[n]
        self.s.matslots.unheld(n)
        for t in self.transactions.values():
            if t["state"] in ("failed","superseded","retiring","orphaned") and \
                    not any(b["transaction"]==t["transaction"] for b in self.bindings.values()) and \
                    not any(v[1]==t["transaction"] for v in self.holds.values()):
                t["state"]="retired"
        if persist:
            self._save()
        return True

    def _validate(self, conn, publication):
        if conn not in self.s.connections or getattr(conn,"building_activation",0)!=VERSION:
            raise Rejected("current connection must negotiate buildingActivation=1")
        if getattr(conn,"building_epoch",None) is None or conn.active not in self.s.anchors:
            raise Rejected("current session/anchor is unavailable")
        catalog = self.c._load()
        if catalog["publication"]!=publication or not publication or set(catalog["requests"])!=set(catalog["entries"]):
            raise Rejected("catalog publication is stale or incomplete")
        snapshot = self.store._load()
        if catalog["snapshot"]!=snapshot.revision:
            raise Rejected("prepare the current accepted revision first")
        if not 0<len(catalog["entries"])<=MAX_TARGETS:
            raise ResourceLimit("activation supports 1..32 catalog targets")
        anchor = self.s.anchors[conn.active]
        boxes,selected = [],set()
        for slot,row in catalog["entries"].items():
            req = br.TileRequest(**row["request"])
            if not req.kind.endswith("_geo") or req.kind=="near_geo":
                if (req.aid,req.lat,req.lon)!=(anchor.id,float(anchor.lat),float(anchor.lon)):
                    raise Rejected("prepared target belongs to a different anchor/frame")
            frame,box = req.geometry()
            import numpy as np
            x0,y0,x1,y1 = box
            la,lo,_ = frame.enu_to_geodetic(np.array([x0,x1,x0,x1]),np.array([y0,y0,y1,y1]),0)
            if _live(conn,req):
                selected.add(slot)
                boxes.append((float(lo.min()),float(la.min()),float(lo.max()),float(la.max())))
            with self.s.mapdata.building_snapshot(snapshot):
                inputs = req.inputs(self.s.mapdata)
            if inputs!=(row["identity"],row["dependency"]) or not self.c._valid(row):
                raise Rejected("prepared inputs or artifact checksums no longer match")
        from buildingoverlaystore import _union
        if not selected:
            raise Rejected("no prepared target corresponds to this connection's current world")
        import buildingbaseline
        context = buildingbaseline.collect(self.s.mapdata,_union(boxes),snapshot)
        self.store.compose(snapshot,context.buildings,baseline_parts=context.parts,
            baseline_release=self.s.mapdata.release,bounds=context.bounds)
        import buildingtargets
        discovered = buildingtargets.discover(_OneConnection(self.s,conn),_union(boxes))
        current = {r.slot:r.wire() for r in discovered.requests}
        if any(current.get(k)!=catalog["requests"][k] for k in selected) or any(
                k not in catalog["entries"] and _live(conn,br.TileRequest(**current[k])) for k in discovered.relevant):
            raise Rejected("prepared targets/settings do not match the current world")
        for k in selected:
            req=br.TileRequest(**catalog["requests"][k]); tracker=_tracker(conn,req.kind)
            if req.kind.startswith("near") and not any((req.x,req.y) in getattr(tracker,n,()) for n in ("sent","empty")):
                raise Rejected("near target must finish normal world streaming before activation")
        return catalog,snapshot,self.store._state_token(),selected

    def _payload(self,row):
        req = br.TileRequest(**row["request"])
        result = row["result"]
        source = result["content"][0] if req.kind=="near_geo" else result[0]
        name = str(source).rsplit("/",1)[-1]
        if name not in row["files"] or not name.endswith(".json"):
            raise Rejected("prepared receipt has no checked tile JSON")
        path = self.c.io.objects/row["generation"]/name
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data,dict):
            raise Rejected("invalid prepared tile JSON")
        return req,data

    def _shape(self,conn,t,row,name,what):
        if len(self.holds)>=MAX_SLOTS:
            raise ResourceLimit("activation shape hold budget reached")
        basename = str(name).rsplit("/",1)[-1]
        if basename not in row["files"] or not basename.endswith(".cdae"):
            raise Rejected("mesh is not in the checked generation manifest")
        size = row["files"][basename]["size"]
        retained = sum(v.get("bytes",0) for v in self.transactions.values() if
            any(owner in self.s.matslots.slot for owner in v["owners"]) or any(h[1]==v["transaction"] for h in self.holds.values()))
        if t["bytes"]+size>MAX_COPY_BYTES or retained+size>MAX_RETAINED_BYTES:
            raise ResourceLimit("activation mesh byte budget exceeded")
        owner = ("building-activation",t["transaction"],row["generation"],what)
        pooled = self.s.matslots.take(owner)
        n = matslots.MatSlots.number(pooled)
        destination = Path(self.s.out_dir)/matslots.MatSlots.file(pooled)
        try:
            matslots.place(str(self.c.io.objects/row["generation"]/basename),str(destination))
            if not _matches(destination,row["files"][basename]):
                raise Rejected("copied mesh checksum differs from prepared data")
        except BaseException:
            self.s.matslots.release(owner)
            raise
        serial = next(self.s.slot_serial)
        conn.slot_holds[n] = serial
        self.holds[n] = (serial,t["transaction"])
        t["owners"].append(owner)
        t["bytes"]+=size
        return [f"{self.s.vfs_dir}/{destination.name}",serial]

    def activate(self,conn,publication,*,manual=False):
        self._thread()
        if manual is not True:
            raise Rejected("manual=True is required; activation does not fetch or accept data")
        pending = [t for t in self.transactions.values() if t["conn"] is conn and t["state"] in ("scheduled","deferred","installing")]
        if pending:
            t = pending[0]
            if (publication,t["anchor"],t["epoch"])!=(t["publication"],conn.active,conn.building_epoch):
                raise Rejected("finish/cancel the outstanding activation before scheduling a new one")
            self.cancel_if_stale(conn)
            if t["state"] not in ("scheduled","deferred","installing"):
                return self.status(t["transaction"])
            conn.send(deepcopy(t["message"]))
            return self.status(t["transaction"])
        if len(self.transactions)>=MAX_TRANSACTIONS:
            raise ResourceLimit("activation transaction history is full; explicit retirement is required")
        with self.c.io._locked(),self.store._locked():
            catalog,snapshot,state,selected = self._validate(conn,publication)
            guard=set(self.guards)
            for k in selected:
                req=br.TileRequest(**catalog["requests"][k])
                import blockworker
                guard.update(map(str,blockworker.building_cells(*req.geometry())))
            if len(guard)>512:
                raise ResourceLimit("activation coverage budget exceeded")
            previous = max((r for r in self.transactions.values() if r["conn"] is conn),key=lambda r:r["seq"],default=None)
            force = previous is not None and previous["state"]=="failed"
            self.sequence += 1
            tx = uuid.uuid4().hex
            t = dict(transaction=tx,seq=self.sequence,state="scheduled",publication=publication,revision=snapshot.revision,
                overlay_enabled=snapshot.enabled,acknowledged=False,
                state_token=state,anchor=conn.active,epoch=conn.building_epoch,conn=conn,owners=[],targets=[],bytes=0,
                generations=sorted({r["generation"] for k,r in catalog["entries"].items() if k in selected}),
                requests={k:deepcopy(v) for k,v in catalog["requests"].items() if k in selected},guard=guard)
            self.transactions[tx] = t
            try:
                for slot,row in sorted(catalog["entries"].items()):
                    if slot not in selected:
                        continue
                    req,data = self._payload(row)
                    old = self.bindings.get((id(conn),req.kind,_key(req)))
                    reuse = bool(not force and old and old["content"]==row["content_digest"] and old["identity"]==row["identity"])
                    if reuse:
                        for w,sl in old["slots"].items():
                            what=w.removeprefix("x:")
                            name = old["exact"].get(what) if w.startswith("x:") else old["data"].get(what)
                            name = name.get("shape") if isinstance(name,dict) else name
                            name = str(name).rsplit("/",1)[-1]
                            if name not in row["files"] or not _matches(Path(self.s.out_dir)/sl[0].rsplit("/",1)[-1],row["files"][name]):
                                reuse=False; break
                    target = dict(slot=slot,kind=req.kind,key=_key(req),generation=row["generation"],content=row["content_digest"],
                        identity=row["identity"],reuse=reuse,data={},slots={},exact={},allowEmpty=False)
                    target["frame"] = list(geotiles.centre(req.x,req.y,
                        geotiles.Z if req.kind=="near_geo" else __import__("blockworker").far_geo_zoom("bldfar" if req.kind=="far_geo" else "bldsky"))) if req.kind.endswith("_geo") else [req.lat,req.lon]
                    if not reuse:
                        names = WHATS if req.kind.startswith("near") else ("shape",)
                        target["data"] = {w:deepcopy(data[w]) for w in names if w in data}
                        target["data"].update({k:deepcopy(data[k]) for k in ("h","lamps","frame") if k in data})
                        target["data"]["colliders"] = {w:deepcopy(v) for w,v in data.get("colliders",{}).items() if w in WHATS}
                        for w in names:
                            value = data.get(w)
                            shape = value.get("shape") if isinstance(value,dict) else value if w=="shape" else None
                            if shape:
                                target["slots"][w] = self._shape(conn,t,row,shape,w)
                        if not req.kind.startswith("near"):
                            target["data"]["pos"] = deepcopy(data.get("pos"))
                            target["data"]["pivot"] = data.get("pivot",0)
                        if req.kind=="near_geo":
                            for w,path in (row["result"].get("exact") or {}).items():
                                if w in WHATS:
                                    target["exact"][w] = path
                                    target["slots"]["x:"+w] = self._shape(conn,t,row,path,"x:"+w)
                        tracker = _tracker(conn,req.kind)
                        target["allowEmpty"] = (req.x,req.y) in getattr(tracker,"empty",())
                    t["targets"].append(target)
                m = dict(type="building_activate",version=VERSION,sessionId=self.s.sid,epoch=t["epoch"],anchorId=t["anchor"],
                    seq=t["seq"],transaction=tx,publication=publication,revision=t["revision"],
                    osm=bool(snapshot.enabled and snapshot.entries),targets=t["targets"])
                if len(br._json(m).encode())>MAX_MESSAGE:
                    raise ResourceLimit("activation message exceeds 1 MiB")
                t["message"] = m
                self.guards.update(t["guard"])  # a lost ack cannot prove nothing was installed
                from earthd import BUILDING_SESSION_HOLD
                try:
                    with (Path(self.s.out_dir)/BUILDING_SESSION_HOLD).open("xb") as pin:
                        pin.write(b"Building activation ownership requires proven retirement.\n")
                        pin.flush(); os.fsync(pin.fileno())
                except FileExistsError:
                    pass
                self._save()  # ownership is durable before anything is sent
            except BaseException:
                t["state"]="failed"
                for owner in t["owners"]:
                    n = self.s.matslots.slot.get(owner)
                    self.s.matslots.release(owner)
                    if n is not None:
                        self.holds.pop(n,None)
                        conn.slot_holds.pop(n,None)
                        self.s.matslots.unheld(n)  # never sent, so no game object can hold it
                self._save()
                raise
            for target in t["targets"]:
                req = br.TileRequest(**catalog["requests"][target["slot"]])
                tracker = _tracker(conn,req.kind)
                if tracker is not None:
                    future = tracker.futs.pop((req.x,req.y),None)
                    if future is not None:
                        future.cancel()
                    tracker.building.discard((req.x,req.y))
                    getattr(tracker,"started",{}).pop((req.x,req.y),None)
        conn.send(deepcopy(m))
        return self.status(tx)

    def status(self,transaction):
        t = self.transactions[transaction]
        return dict(transaction=transaction,state=t["state"],publication=t["publication"],revision=t["revision"],
            seq=t["seq"],game_activated=t["state"]=="activated",detail=t.get("detail"),
            game_acknowledged=t["acknowledged"],overlay_enabled=t["overlay_enabled"],
            pending_targets=list(self.pending_targets.values()),
            held_slots=sum(v[1]==transaction for v in self.holds.values()))

    def cancel_if_stale(self,conn):
        for t in self.transactions.values():
            if t["conn"] is conn and t["state"] in ("scheduled","deferred","installing"):
                _,state = self.store.load_state()
                if state!=t["state_token"] or conn.active!=t["anchor"] or conn.building_epoch!=t["epoch"]:
                    conn.send(dict(type="building_cancel",sessionId=self.s.sid,epoch=t["epoch"],anchorId=t["anchor"],
                        seq=t["seq"],transaction=t["transaction"]))

    def acknowledge(self,conn,msg):
        self._thread()
        fields = {"type","sessionId","epoch","anchorId","seq","transaction","status","detail"}
        if not isinstance(msg,dict) or set(msg)!=fields or len(br._json(msg))>2048 or \
                msg["type"]!="building_ack" or not isinstance(msg["transaction"],str) or \
                not re.fullmatch(r"[0-9a-f]{32}",msg["transaction"]) or \
                not isinstance(msg["sessionId"],str) or len(msg["sessionId"])>64 or \
                not isinstance(msg["epoch"],str) or not re.fullmatch(r"[0-9a-f]{32}",msg["epoch"]) or \
                type(msg["seq"]) is not int or type(msg["anchorId"]) is not int or \
                msg["status"] not in ("deferred","installing","activated","failed","rejected") or \
                not isinstance(msg["detail"],str) or len(msg["detail"])>256:
            raise Rejected("malformed activation acknowledgment")
        t = self.transactions.get(msg["transaction"])
        if t is None or t["conn"] is not conn or msg["sessionId"]!=self.s.sid or \
                (msg["epoch"],msg["anchorId"],msg["seq"])!=(t["epoch"],t["anchor"],t["seq"]) or \
                msg["epoch"]!=getattr(conn,"building_epoch",None) or msg["anchorId"]!=conn.active:
            raise Rejected("unexpected/stale activation acknowledgment")
        if t["state"] not in ("scheduled","deferred","installing"):
            return self.status(t["transaction"])
        if msg["status"]=="activated":
            with self.c.io._locked(),self.store._locked():
                try:
                    _,_,state,_ = self._validate(conn,t["publication"])
                except (ValueError,OSError,RuntimeError):
                    state = None
                t["state"] = "activated" if state==t["state_token"] else "superseded"
                t["acknowledged"] = True
            self.guards.update(t["guard"])
            for target in t["targets"]:
                route = (id(conn),target["kind"],target["key"])
                old = self.bindings.get(route)
                if not target["reuse"]:
                    self.bindings[route] = dict(deepcopy(target),transaction=t["transaction"],anchor=t["anchor"])
                self.pending_targets.pop(target["kind"]+":"+target["key"],None)
                tracker = _tracker(conn,target["kind"])
                if tracker is not None:
                    req = br.TileRequest(**t["requests"][target["slot"]])
                    tracker.sent.add((req.x,req.y))
                    getattr(tracker,"empty",set()).discard((req.x,req.y))
            referenced = {b["transaction"] for b in self.bindings.values()}
            for previous in self.transactions.values():
                if previous is not t and previous["state"] in ("activated","superseded") and previous["transaction"] not in referenced:
                    previous["state"]="retiring" if any(h[1]==previous["transaction"] for h in self.holds.values()) else "retired"
                    for owner in previous["owners"]:
                        self.s.matslots.release(owner)
        else:
            t["state"] = msg["status"]
            if t["state"] in ("failed","rejected"):
                t["state"]="failed"
                for owner in t["owners"]:
                    self.s.matslots.release(owner)
        t["detail"] = msg["detail"]
        self._save()
        return self.status(t["transaction"])

    def context_changed(self,conn):
        """Disconnect/rebase/hello fences old callbacks, but cannot imply resource release."""
        for t in self.transactions.values():
            if t["conn"] is conn and t["state"] in ("scheduled","deferred","installing","activated","superseded"):
                if t["state"] in ("scheduled","deferred","installing"):
                    conn.send(dict(type="building_cancel",sessionId=self.s.sid,epoch=t["epoch"],anchorId=t["anchor"],
                        seq=t["seq"],transaction=t["transaction"]))
                t["state"]="orphaned"
                for owner in t["owners"]:
                    self.s.matslots.release(owner)
        self.bindings = {k:v for k,v in self.bindings.items() if k[0]!=id(conn)}
        if self.transactions:
            self._save()

    def filter_message(self,conn,msg):
        """Prevent ordinary callbacks/reconnect content from resurrecting an older building mesh."""
        kind = {"roads_tile":"near","roads_exact":"near","bldfar_tile":"far","bldsky_tile":"skyline"}.get(msg.get("type"))
        if kind is None:
            return msg
        geo = isinstance(msg.get("key"),str) and bool(re.fullmatch(r"g\d+_\d+_\d+",msg["key"]))
        route = (id(conn),kind+("_geo" if geo else ""),msg.get("key"))
        binding = self.bindings.get(route)
        pending = any(t["conn"] is conn and t["state"] in ("scheduled","deferred","installing") and
            any(r["kind"]==route[1] and r["key"]==route[2] for r in t["targets"]) for t in self.transactions.values())
        if binding is None and not pending:
            if self.guards and getattr(conn,"building_activation",0)==VERSION and conn.active in self.s.anchors:
                key=msg.get("key","")
                if geo:
                    parsed=geotiles.parse(key)
                    if parsed is None: return None
                    _,x,y=parsed
                else:
                    try: x,y=map(int,key.split("_"))
                    except (ValueError,AttributeError): return None
                a=self.s.anchors[conn.active]
                req=br.TileRequest(route[1],x,y,aid=a.id,lat=a.lat,lon=a.lon,zoom=a.zooms[0])
                import blockworker
                if self.guards.intersection(map(str,blockworker.building_cells(*req.geometry()))):
                    name=route[1]+":"+key
                    if name not in self.pending_targets and len(self.pending_targets)>=128:
                        raise ResourceLimit("unprepared activation target budget exceeded")
                    self.pending_targets[name]=dict(kind=route[1],key=key,status="deferred",reason="prepare and explicitly activate this newly streamed dependent target")
                    return defer_message(self.s,conn,msg)
            return msg
        # Drop allocations that have never reached the game; the retained activation
        # owner supplies immutable paths instead. Other normal pooled parts stay intact.
        out = deepcopy(msg)
        for w,sl in list(out.get("slots",{}).items()):
            if pending or w in WHATS or w.startswith("x:bld") or kind!="near":
                n = int(sl[0].rsplit("slot_",1)[-1].split(".")[0])
                if conn.slot_holds.get(n)==sl[1] and n not in self.holds:
                    del conn.slot_holds[n]
                    self.s.matslots.unheld(n)
                del out["slots"][w]
        if pending:
            return None
        out["buildingGeneration"] = binding["content"]
        if kind=="near" and msg["type"]=="roads_exact":
            out["parts"] = {w:p for w,p in out.get("parts",{}).items() if w not in WHATS}
            for w,p in binding["data"].items():
                if w in WHATS and isinstance(p,dict) and p.get("shape"):
                    out["parts"][w] = binding["exact"].get(w,False) if conn.active==binding["anchor"] else False
            out.setdefault("slots",{}).update({w[2:]:v for w,v in binding["slots"].items() if w.startswith("x:") and conn.active==binding["anchor"]})
        else:
            data = out.get("data")
            if not isinstance(data,dict):
                import staging
                data = staging.inline_json(self.s.out_dir,out.get("path",""),max_bytes=MAX_MESSAGE)
                if not isinstance(data,dict):
                    # Cannot safely splice building parts into an unreadable bundle.
                    for sl in out.get("slots",{}).values():
                        n = int(sl[0].rsplit("slot_",1)[-1].split(".")[0])
                        if conn.slot_holds.get(n)==sl[1] and n not in self.holds:
                            del conn.slot_holds[n]; self.s.matslots.unheld(n)
                    return None
                out["data"] = data
            for w in WHATS if kind=="near" else ("shape","pos","pivot","lamps","h"):
                data.pop(w,None)
                if w in binding["data"]:
                    data[w] = deepcopy(binding["data"][w])
            if kind=="near":
                cols = data.setdefault("colliders",{})
                for w in WHATS:
                    cols.pop(w,None)
                cols.update(deepcopy(binding["data"].get("colliders",{})))
                out["h"] = binding["data"].get("h")
                out["exact"] = deepcopy(binding["exact"]) if conn.active==binding["anchor"] else {}
            out.setdefault("slots",{}).update(binding["slots"])
        return out
