"""Bounded proof-based reclamation within the existing generation/session stores.

Age is never release evidence. Old schema journals without a client identity,
corrupt journals, busy work leases and missing inventory proofs remain retained.
"""
from pathlib import Path
from itertools import islice
import re
import shutil
import threading

from buildingoverlaystore import OverlayStore, CorruptStorage, LockTimeout, ResourceLimit, Limits
import matslots

UUID=re.compile(r"[0-9a-f]{32}")
SESSION=re.compile(r"[A-Za-z0-9_-]{1,64}")


class WorkLease:
    """Use OverlayStore's existing OS lock primitive, not process-age heuristics."""
    def __init__(self,root,generation,*,create=False):
        if not UUID.fullmatch(generation): raise CorruptStorage("invalid private work id")
        root=Path(root).absolute()
        self.root=root
        self.path=root/"workleases"/generation
        OverlayStore._no_link(root)
        OverlayStore._no_link(root/"workleases")
        if create: self.path.mkdir(parents=True,exist_ok=True)
        self.io=object.__new__(OverlayStore)
        self.io.root=self.io.directory=self.path
        self.io.limits=Limits(max_file_bytes=4096)
        self.io.lock_timeout=0
        self.lock=None
        self.users=1
        self.guard=threading.Lock()

    def acquire(self,*,create=False):
        OverlayStore._no_link(self.path.parent); OverlayStore._no_link(self.path)
        if not self.path.is_dir(): raise CorruptStorage("private work lease is unavailable")
        self.lock=self.io._locked(); self.lock.__enter__()
        try:
            marker=self.path/"owner.json"
            value=dict(protocol="building-work-v1",generation=self.path.name)
            if create: self.io._atomic_write(marker,self.io._document(value))
            elif self.io._read(marker)!=value: raise CorruptStorage("private work lease failed validation")
        except BaseException:
            self.close(); raise
        return self

    def close(self):
        finished=False
        with self.guard:
            self.users-=1
            if self.users==0 and self.lock is not None:
                self.lock.__exit__(None,None,None); self.lock=None
                finished=True
        if finished and not (self.root/'work'/self.path.name).exists():
            try:
                _remove(self.path,self.path.parent)
            except (OSError,ValueError):
                pass # leave an unverifiable marker for bounded retention diagnostics

    def retain(self):
        with self.guard:
            if self.users<=0 or self.lock is None: raise RuntimeError("private work lease already released")
            self.users+=1
        return self


def _remove(directory,parent):
    directory,parent=Path(directory),Path(parent)
    OverlayStore._no_link(parent); OverlayStore._no_link(directory)
    if directory.parent.resolve()!=parent.resolve(): raise CorruptStorage("cleanup target escaped its owning directory")
    if directory.exists():
        pending=[(directory,0)]; count=0
        while pending:
            folder,depth=pending.pop()
            for child in islice(folder.iterdir(),4097):
                count+=1
                if count>4096: raise ResourceLimit("cleanup tree inspection exceeds quota")
                OverlayStore._no_link(child)
                if child.is_dir():
                    if depth>=3: raise CorruptStorage("unexpected nested cleanup directory")
                    pending.append((child,depth+1))
        shutil.rmtree(directory)


def _journal(io,path):
    value=io._read(path)
    if not isinstance(value,dict) or not isinstance(value.get("holds"),dict) or not isinstance(value.get("transactions"),list) \
            or not isinstance(value.get("session"),str) or not SESSION.fullmatch(value["session"]) \
            or len(value["holds"])>256 or len(value["transactions"])>32:
        raise CorruptStorage("invalid activation ownership journal")
    transactions={}
    for row in value["transactions"]:
        if not isinstance(row,dict) or not isinstance(row.get("transaction"),str) or not UUID.fullmatch(row["transaction"]) \
                or not isinstance(row.get("generations"),list) or len(row["generations"])>128 \
                or any(not isinstance(g,str) or not UUID.fullmatch(g) for g in row["generations"]):
            raise CorruptStorage("invalid activation transaction reference")
        transactions[row["transaction"]]=row
    for n,h in value["holds"].items():
        if not isinstance(n,str) or not n.isdigit() or not isinstance(h,dict) or type(h.get("serial")) is not int \
                or h.get("transaction") not in transactions:
            raise CorruptStorage("invalid activation hold reference")
    guards=value.get('guards',[])
    if not isinstance(guards,list) or len(guards)>512 or any(not isinstance(k,str) or not re.fullmatch(r'14/\d+/\d+',k) or
            any(not 0<=int(v)<2**14 for v in k.split('/')[1:]) for k in guards):
        raise CorruptStorage("invalid activation coverage references")
    return value


def retained_guards(directory):
    """Read startup fences before any ordinary building message can leave the socket."""
    directory=Path(directory)
    if not (directory/'catalog.json').exists(): return set()
    from buildingrefresh import _CatalogFiles
    io=_CatalogFiles(directory,create=False)
    guards=set()
    with io._locked():
        paths=list(islice(directory.glob('activation-*.json'),17))
        if len(paths)>16: raise ResourceLimit('activation journal inspection exceeds quota')
        for path in paths: guards.update(_journal(io,path).get('guards',()))
    if len(guards)>512: raise ResourceLimit('retained activation coverage exceeds budget')
    return guards


def collect(server,manager=None,*,proof=None,limit=32):
    if type(limit) is not int or not 1<=limit<=64: raise ValueError("invalid cleanup budget")
    coordinator=manager.c if manager is not None else getattr(server,"_building_refresh",None)
    report=dict(removed_generations=[],removed_work=[],removed_journals=[],released_sessions=[],
                reclaimed_slots=[],uncertain=[],held=0,reclaimable=0,history_compacted=0)
    if coordinator is None:
        report["uncertain"].append("No validated preparation catalog is open."); return report
    io=coordinator.io
    if manager is not None:
        # A fenced complete game inventory proves unused source slots are not
        # queued, staged, visible or collision-owned. A timeout proves none of this.
        if proof and proof.get("complete") and proof.get("sessionId")==server.sid:
            slots={tuple(s) for s in proof["slots"]}
            pending=set(proof["pending"])
            for n,(serial,tx) in list(manager.holds.items()):
                t=manager.transactions[tx]
                if t["seq"]<=proof["sequence"] and tx not in pending and (server.sid,n) not in slots:
                    manager.release(n,serial,persist=False)
                    if not any(v[1]==tx for v in manager.holds.values()):
                        for owner in t["owners"]: server.matslots.release(owner)
                        t["state"]="retired"
            referenced={b["transaction"] for b in manager.bindings.values()}|{v[1] for v in manager.holds.values()}
            for tx,t in manager.transactions.items():
                if t["seq"]<=proof["sequence"] and tx not in pending and tx not in referenced:
                    t["state"]="retired"
            manager._save()
        report["history_compacted"]=manager.compact()
        report["held"]=len(manager.holds)
        for n,stamp in list(manager.released_slots.items())[:limit]:
            if not server.matslots.lock.acquire(blocking=False): break
            try:
                if n in server.matslots.owner or n in server.matslots.waiting or server.slot_held(n): continue
                path=Path(server.out_dir)/matslots.MatSlots.file(server.matslots.name(n))
                OverlayStore._no_link(path)
                if path.exists():
                    st=path.stat()
                    if (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)!=stamp: continue
                    if path.parent.resolve()!=Path(server.out_dir).resolve(): raise CorruptStorage("invalid pooled cleanup target")
                    path.unlink(); report["reclaimed_slots"].append(n)
                manager.released_slots.pop(n,None)
            finally: server.matslots.lock.release()
    roots=set()
    blocked=False
    with io._locked():
        catalog=coordinator._load()
        roots.update(r["generation"] for r in catalog["entries"].values())
        journals=list(islice(io.root.glob("activation-*.json"),17))
        if len(journals)>16: raise ResourceLimit("activation journal inspection exceeds quota")
        for path in journals:
            try:
                journal=_journal(io,path)
            except (OSError,ValueError):
                blocked=True; report["uncertain"].append("Corrupt/incomplete ownership journal; generations retained."); continue
            sid=journal["session"]
            released=bool(proof and proof.get("complete") and journal.get("client") and
                journal.get("client")==proof.get("client") and sid!=server.sid and sid not in proof["sessions"] and
                sid not in server.claimed_sessions())
            if released:
                marker=Path(server.root)/sid/".building-activation-hold"
                OverlayStore._no_link(marker.parent); OverlayStore._no_link(marker)
                # Only a marked session already admitted by the ordinary scanner.
                if sid in server.orphans and marker.parent.parent.resolve()==Path(server.root).resolve():
                    marker.unlink(missing_ok=True)
                    report["released_sessions"].append(sid)
                path.unlink(); report["removed_journals"].append(path.name)
                continue
            referenced={h["transaction"] for h in journal["holds"].values()}
            for row in journal["transactions"]:
                if row["transaction"] in referenced or row.get("state") not in ("retired","failed"):
                    roots.update(row["generations"])
            if sid!=server.sid:
                report["uncertain"].append("Older session retained: missing matching complete client release proof." if not journal.get("client") else
                    "Older session still held or has no current inventory proof.")
        if not blocked:
            candidates=list(islice(io.objects.iterdir(),coordinator.limits.artifacts+1))
            if len(candidates)>coordinator.limits.artifacts: raise ResourceLimit("generation inventory exceeds quota")
            for directory in sorted(candidates):
                if not UUID.fullmatch(directory.name): raise CorruptStorage("unexpected generation directory")
                if directory.name in roots: continue
                report["reclaimable"]+=1
                if len(report["removed_generations"])<limit:
                    _remove(directory,io.objects); report["removed_generations"].append(directory.name)
        work=list(islice((io.root/"work").iterdir(),coordinator.limits.artifacts+1))
        if len(work)>coordinator.limits.artifacts: raise ResourceLimit("private work inventory exceeds quota")
        for directory in work[:limit]:
            lease=None
            try:
                with coordinator._active_lock:
                    if directory in coordinator._active: continue
                lease=WorkLease(io.root,directory.name).acquire()
                _remove(directory,io.root/"work")
                report["removed_work"].append(directory.name)
            except (OSError,ValueError):
                report["uncertain"].append("Private work retained: busy, legacy or unverifiable worker ownership.")
            finally:
                if lease is not None:
                    path=lease.path; lease.close()
                    if directory.name in report["removed_work"]: _remove(path,io.root/"workleases")
        lease_root=io.root/"workleases"
        if lease_root.is_dir():
            leases=list(islice(lease_root.iterdir(),coordinator.limits.artifacts+1))
            if len(leases)>coordinator.limits.artifacts: raise ResourceLimit("work lease inventory exceeds quota")
            for directory in leases[:limit]:
                if (io.root/"work"/directory.name).exists(): continue
                lease=None
                try:
                    lease=WorkLease(io.root,directory.name).acquire()
                except (OSError,ValueError):
                    continue
                finally:
                    if lease is not None: lease.close()
                _remove(directory,lease_root)
    report["uncertain"]=report["uncertain"][:64]
    return report
