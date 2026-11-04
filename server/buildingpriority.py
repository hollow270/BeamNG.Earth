"""Near-first admission to the existing single OSM acquisition lane.

Tickets are bounded, expiring scheduling hints, never ownership of data/meshes.
The existing OS query lease, claims and publication checks remain authoritative.
"""
from contextlib import contextmanager
from pathlib import Path
import math,re,time,uuid,threading,weakref
import buildingoverlaystore as fs

MAX_WAITERS=128
PROMOTE_S=20.
_locks=weakref.WeakValueDictionary()
_locks_guard=threading.Lock()


def rank(value,now):
    return max(0,value['priority']-int(max(0,now-value['created'])/PROMOTE_S)),value['created'],value['token']


class Queue:
    def __init__(self,store):
        self.store=store;root=store.root/'source-queue'
        store._no_link(root);root.mkdir(exist_ok=True)
        self.io=object.__new__(fs.OverlayStore);self.io.directory=store.directory;self.io.root=root
        self.io.limits=fs.Limits(max_file_bytes=2048);self.io.lock_timeout=.2
        with _locks_guard:
            name=str(root.absolute())
            self.local=_locks.get(name)
            if self.local is None:self.local=threading.Lock();_locks[name]=self.local

    def _read(self,path):
        v=self.io._read(path)
        if not isinstance(v,dict) or set(v)!={'token','priority','created','expires'} or v['token']!=path.stem or not re.fullmatch('[0-9a-f]{32}',path.stem) or type(v['priority']) is not int or not 0<=v['priority']<=2:
            raise fs.CorruptStorage('invalid OSM scheduling ticket')
        if any(type(v[k]) not in (int,float) or not math.isfinite(v[k]) for k in ('created','expires')) or v['expires']<v['created']:
            raise fs.CorruptStorage('invalid OSM scheduling deadline')
        return v

    def _waiting(self,now):
        paths=list(self.io.root.glob('*.json'))
        if len(paths)>MAX_WAITERS:raise fs.LockTimeout('OSM scheduling queue capacity')
        waiting=[]
        for path in paths:
            value=self._read(path)
            if value['expires']<=now:
                # Advisory ticket only. Removing it never unlocks the query OS
                # lease, changes a claim, deletes data or authorizes publication.
                self.io._safe(path).unlink()
            else:waiting.append(value)
        return waiting

    @contextmanager
    def enter(self,priority,deadline):
        if type(priority) is not int or not 0<=priority<=2:raise ValueError('invalid building preparation priority')
        token=uuid.uuid4().hex;path=self.io.root/(token+'.json');now=time.time()
        remaining=deadline-time.monotonic()
        if remaining<=0:raise fs.LockTimeout('OSM scheduling budget exhausted')
        value=dict(token=token,priority=priority,created=now,expires=now+remaining)
        with self.io._locked():
            if len(self._waiting(now))>=MAX_WAITERS:raise fs.LockTimeout('OSM scheduling queue full')
            self.io._atomic_write(path,self.io._document(value))
        try:
            while time.monotonic()<deadline:
                with self.io._locked():
                    waiting=self._waiting(time.time())
                    first=min(waiting,key=lambda v:rank(v,time.time())) if waiting else None
                if first is not None and first['token']==token and self.local.acquire(blocking=False):
                    entered=False
                    try:
                        with self.store.lease('query-lane',min(.02,max(.001,deadline-time.monotonic())))._locked():
                            with self.io._locked():
                                waiting=self._waiting(time.time())
                                current=min(waiting,key=lambda v:rank(v,time.time())) if waiting else None
                            if current is None or current['token']!=token:continue
                            entered=True
                            yield
                            return
                    except fs.LockTimeout:
                        if entered:raise
                    finally:self.local.release()
                time.sleep(min(.1,max(0,deadline-time.monotonic())))
            raise fs.LockTimeout('OSM preparation queued; retry after scheduling deadline')
        finally:
            with self.io._locked():
                if path.exists() and self._read(path)['token']==token:self.io._safe(path).unlink()
