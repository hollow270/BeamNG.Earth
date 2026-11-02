"""Worldwide first-visit acquisition BEFORE ordinary building generation.

Each data cell's choice is frozen durably in a shared session before a cache key
or mesh can use it. New cells may only add influences that do not alter previously
chosen cells. Cross-cell conflicts keep the earlier choice or Overture fallback.
Workers share an OS-locked ledger, immutable objects and the existing regional
retrieval/merge primitives. No engine transactions, collider replacement or Lua RPC.
"""
from dataclasses import dataclass,replace
from collections import OrderedDict
import logging
import math
import os
from pathlib import Path
import re
import time
import uuid

import buildingoverlay as bo
import buildingoverlaystore as fs
import buildingprimary as primary
import buildingregions as regions
import buildingrender
import layercakebuildings as lc
import mapcells

log=logging.getLogger('earthd.osm_world')
MAX_CELLS=4096
MAX_OBJECTS=8192
MAX_SESSIONS=1024
WORLD_BYTES=512*1024**2
READ_BUDGET_S=45.0


def configured(cache):
    return (Path(cache)/'osm-primary/v1/worldwide.json').is_file()


def enable(cache,*,policy_confirmed=False):
    if policy_confirmed is not True: raise ValueError('confirm Layercake source policy before enabling automatic first visits')
    store=primary.Store(cache)
    with store.session(),store._locked():
        store._atomic_write(store.root/'worldwide.json',store._document(dict(version=1,source=lc.URL,policy_confirmed=True)))
    return dict(worldwide_enabled=True,freshness='manual_only',effective='next session')


@dataclass(frozen=True)
class Session:
    identity:str
    baseline:fs.Snapshot


def _store(cache):
    base=primary.Store(cache)
    store=object.__new__(primary.Store)
    store.directory=base.directory;store.root=base.root/'world';store.objects=store.root/'objects'
    # Bounded publication of two validated dense-cell JSON objects can take a
    # few seconds on Windows. This is independent of the 45 s query deadline.
    store.limits=primary.REGIONAL_LIMITS;store.lock_timeout=15.
    for path in (store.root,store.objects,store.root/'sessions',store.root/'claims'):
        store._no_link(path);path.mkdir(exist_ok=True)
    return store


def lease_store(store,name,timeout):
    """A separate OS lease, never the world publication mutex."""
    if name not in ('query-lane','query-network'):raise ValueError('unknown acquisition lease')
    lease=object.__new__(primary.Store)
    lease.directory=store.directory;lease.root=store.root/name;lease.limits=store.limits
    lease.lock_timeout=max(.001,timeout)
    store._no_link(lease.root);lease.root.mkdir(exist_ok=True)
    return lease


def start(cache,baseline):
    config=primary.Store(cache)
    value=config._read(config.root/'worldwide.json')
    if value!=dict(version=1,source=lc.URL,policy_confirmed=True): raise fs.CorruptStorage('invalid worldwide source permission')
    store=_store(cache)
    with store._locked():
        index=store.root/'index.json'
        if not index.exists():
            if (store.root/'initialized').exists() or any(store.objects.iterdir()):
                raise fs.CorruptStorage('world index missing; retained ownership must not be guessed')
            # Establish the empty valid index BEFORE its marker or any session
            # can publish objects. A killed first write cannot reserve an invalid
            # nonexistent index as though it had held prior data.
            store._atomic_write(index,store._document(dict(release=baseline.baseline_release,cells={},influence={},identities={})))
            store._atomic_write(store.root/'initialized',b'1\n')
        sessions=list((store.root/'sessions').iterdir())
        if len(sessions)>=MAX_SESSIONS: raise fs.ResourceLimit('retained session ledger capacity; archive explicitly')
        identity=uuid.uuid4().hex
        store._atomic_write(store.root/'sessions'/(identity+'.json'),store._document(dict(cells={},source=None,influence={})))
    return Session(identity,baseline)


def _empty(release):
    return fs.Snapshot(None,True,release,(),(),(),(),None)


def _write_object(store,snapshot,source,box,report):
    payload=dict(baseline_release=snapshot.baseline_release,entries=[fs._entry_wire(e) for e in snapshot.entries],
                 source=source,coverage=[list(box)],report=report)
    digest=fs._hash(payload);path=store.objects/(digest+'.json')
    if not path.exists():
        body=store._document(payload);size=count=0
        for p in store.objects.iterdir():
            store._no_link(p);count+=1;size+=p.stat().st_size
            if count>=MAX_OBJECTS: raise fs.ResourceLimit('world building object count capacity')
        if size+len(body)>WORLD_BYTES: raise fs.ResourceLimit('world building object byte capacity')
        store._atomic_write(path,body)
    # Validate both new objects and a possibly damaged existing same-name object
    # BEFORE any index/session publication can reference it.
    _load_object(store,digest,snapshot.baseline_release)
    return digest


def _load_object(store,digest,release):
    if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest): raise fs.CorruptStorage('invalid world object identity')
    value=store._read(store.objects/(digest+'.json'))
    if fs._hash(value)!=digest: raise fs.CorruptStorage('world object hash mismatch')
    if value['source'] is None:
        if value['entries']: raise fs.CorruptStorage('unattributed world entries')
        return _empty(release)
    snapshot=store._decode(digest)
    if snapshot.baseline_release!=release: raise fs.BaselineMismatch('world cache baseline changed')
    return snapshot


class Provider:
    def __init__(self,md,session,*,reader_factory=None,wall=time.time,sleep=time.sleep,runner=None):
        self.md,self.session=md,session
        self.store=_store(md.cache_dir)
        self._default_reader_factory=lambda:lc.Reader(Path(md.cache_dir)/'mapdata/raw',md.fetcher,
                                                      tolerant=True,raw_budget=regions.RAW_BYTES)
        self.reader_factory=reader_factory or self._default_reader_factory
        self.wall,self.sleep=wall,sleep
        self.inline_reader=reader_factory is not None  # deterministic fixture hook; production is supervised
        self.runner=runner
        self.decoded=OrderedDict()
        self.cell_views=OrderedDict()
        self.path=self.store.root/'sessions'/(session.identity+'.json')
        if not re.fullmatch('[0-9a-f]{32}',session.identity): raise ValueError('invalid session identity')

    def object(self,digest):
        if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):raise fs.CorruptStorage('invalid world object identity')
        path=self.store.objects/(digest+'.json')
        self.store._no_link(path)
        stamp=path.stat()
        signature=(stamp.st_size,stamp.st_mtime_ns)
        got=self.decoded.get(digest)
        if got is not None and got[0]==signature:
            self.decoded.move_to_end(digest);return got[1]
        snapshot=_load_object(self.store,digest,self.md.release)
        self.decoded[digest]=(signature,snapshot)
        while len(self.decoded)>8:self.decoded.popitem(last=False)
        return snapshot

    def _view(self,pinned,keys,heads):
        # Avoid copying the whole travelled world into every tile's render view.
        token=(keys[0],heads[str(keys[0])]) if len(keys)==1 else None
        while self.cell_views and (len(self.cell_views)>8 or sum(v.cache_bytes for v in self.cell_views.values())>buildingrender.CELL_CACHE_BYTES):
            self.cell_views.popitem(last=False)
        if token in self.cell_views:
            self.cell_views.move_to_end(token);return self.cell_views[token]
        entries=tuple(pinned[k] for k in sorted(pinned) if any(fs._intersects(pinned[k].bounds,key.bounds()) for key in keys))
        view=buildingrender.BuildingView(None,self.md.release,snapshot=replace(_empty(self.md.release),entries=entries))
        if token is not None:self.cell_views[token]=view
        return view

    def _index(self):
        path=self.store.root/'index.json'
        if path.exists():
            value=self.store._read(path)
            if set(value)!={'release','cells','influence','identities'} or value['release'] not in (None,self.md.release) or len(value['cells'])>MAX_CELLS or set(value['influence'])!=set(value['cells']) or len(value['identities'])>regions.MAX_SOURCE_FEATURES:
                raise fs.CorruptStorage('world index schema/baseline mismatch')
            for identity,head in value['identities'].items():
                if bo.normalize_osm_id(identity)!=identity or not isinstance(head,str) or not re.fullmatch('[0-9a-f]{64}',head):
                    raise fs.CorruptStorage('world identity index mismatch')
            if value['release'] is None:
                if value['cells']:raise fs.CorruptStorage('unversioned world cache')
                value['release']=self.md.release
            for key in value['cells']:
                mapcells.parse(key);fs._bounds(value['influence'][key])
            return value
        if (self.store.root/'initialized').exists(): raise fs.CorruptStorage('missing world index; acquisition blocked')
        return dict(release=self.md.release,cells={},influence={},identities={})

    def cached_entries(self,index,bounds):
        entries={};source=None
        for name,head in index['cells'].items():
            if not fs._intersects(index['influence'][name],bounds):continue
            snapshot=self.object(head)
            for e in snapshot.entries:
                if fs._intersects(e.bounds,bounds):entries.setdefault(e.feature.feature_id,e)
            if snapshot.entries and snapshot.refreshes:source=snapshot.refreshes[0]['source']
        return entries,source

    def ready(self,keys):
        """Non-fetching cache probe: never freeze a fallback just to inspect cache."""
        state=self._state()  # atomic checksummed replacement
        return all(str(k) in state['cells'] for k in keys)

    def _state(self):
        state=self.store._read(self.path)
        try:
            if set(state)!={'cells','source','influence'} or not isinstance(state['cells'],dict) or len(state['cells'])>MAX_CELLS or set(state['influence'])!=set(state['cells']):
                raise ValueError()
            if state['source'] is not None:primary.validate_version(state['source'])
            for key,head in state['cells'].items():
                mapcells.parse(key)
                fs._bounds(state['influence'][key])
                if not isinstance(head,str) or not re.fullmatch('[0-9a-f]{64}',head):raise ValueError()
        except (ValueError,TypeError):raise fs.CorruptStorage('invalid pinned session ledger') from None
        return state

    def _pin_source(self,version):
        primary.validate_version(version)
        with self.store._locked():
            latest=self._state()
            if latest['source'] is None:
                latest['source']=version
                self.store._atomic_write(self.path,self.store._document(latest))
            elif latest['source']!=version:raise ValueError('session source version changed during acquisition')
        return latest

    def _acquire(self,key,state,pinned,progress=lambda value:None):
        jobs=regions.Jobs(self.store);ledger=jobs.read()
        reader=None;box=key.bounds();results=[]
        # Previously acquired geometry in neighbouring cells is cache-first even
        # when those cells have not yet been visited in this particular session.
        cached_index=self._index()
        known,_=self.cached_entries(cached_index,box)
        known.update(pinned);pinned=known
        if self.md.fetcher.offline_flag: raise OSError('offline: Overture fallback frozen for this session')
        if self.wall()<max(ledger['clock'],ledger['next_request']):
            # Requests.admit waits for ordinary spacing; a long Retry-After must
            # not tie up the renderer or be circumvented by a new cell/worker.
            if max(ledger['clock'],ledger['next_request'])-self.wall()>2:
                raise OSError('source cooldown: fallback frozen; retry in a fresh session')
        try:
            reader=self.reader_factory()
            if hasattr(reader,'ranges'):
                requests=regions.Requests(jobs,ledger,Path(self.md.cache_dir)/'mapdata/raw',self.wall,lambda:False,self.sleep)
                reader.ranges.admit=requests.admit;reader.ranges.failed=requests.failed
            if state['source'] is None:
                version=reader.select(explicit=True)
                primary.validate_version(version)
                # Durable source pin BEFORE querying geometry, shared by workers.
                state=self._pin_source(version)
            else:
                version=state['source'];reader.attach(version)
            progress(dict(phase='source_selected',source=version))
            if hasattr(reader,'prepare_region'):reader.prepare_region(box)
            progress(dict(phase='parquet_staged',diagnostics=getattr(reader,'diagnostics',{})))
            queue=regions.partition(box);index=0
            while index<len(queue):
                bounds=queue[index];result=jobs.load(version,bounds)
                if result is None:
                    if hasattr(reader,'ranges'):reader.ranges.used=0
                    try:result=reader.query(bounds)
                    except fs.ResourceLimit as error:
                        if 'partition feature budget' not in str(error):raise
                        if len(queue)+3>regions.MAX_JOBS:raise fs.ResourceLimit('cell retrieval job capacity')
                        queue[index:index+1]=regions.split(bounds);continue
                    if tuple(result.bounds)!=tuple(bounds) or result.version!=version:raise ValueError('source job identity changed')
                    jobs.save_job(result)
                results.append(result);index+=1
            influence=fs._union([box]+[bo._bbox(f.geometry) for result in results for f in result.buildings+result.parts])
            # A returned full footprint can extend past its retrieval cell. Its
            # retained cached neighbours must participate in conflict checking.
            context,_=self.cached_entries(cached_index,influence)
            context.update(pinned);pinned=context
            pinned.update((e.feature.feature_id,e) for e in self.session.baseline.entries if fs._intersects(e.bounds,influence))
            # Known accepted buildings are never refreshed by a first visit to
            # adjoining uncached space. Only explicit pre-game update may do that.
            identities=set(cached_index['identities']) | {i for e in self.session.baseline.entries for i in bo._identities(e.feature)} | {i for e in pinned.values() for i in bo._identities(e.feature)}
            filtered=[]
            for result in results:
                buildings=tuple(f for f in result.buildings if not set(bo._identities(f)) & identities)
                ids={f.feature_id for f in buildings}
                filtered.append(replace(result,buildings=buildings,parts=tuple(p for p in result.parts if p.parent_id in ids)))
            before=replace(_empty(self.md.release),entries=tuple(e for e in pinned.values() if fs._intersects(e.bounds,influence)))
            observed={i for result in results for f in result.buildings+result.parts for i in bo._identities(f)}
            progress(dict(phase='merging',partitions=len(queue),observed=len(observed)))
            saved,report,checks=regions.candidate(self.md,before,filtered,box,version,known_returned=observed)
            for check in checks:check.validate(self.md)
            prior={e.feature.feature_id:e for e in before.entries}
            restored=[];entries=[]
            for e in saved.entries:
                old=prior.get(e.feature.feature_id)
                if old is not None and fs._entry_wire(e)!=fs._entry_wire(old):
                    restored.append((e.feature.feature_id,'cached_owner_requires_explicit_refresh'));e=old
                entries.append(e)
            if restored:
                saved=replace(saved,entries=tuple(entries))
                report.update(all_features_updated=False,deferred_count=report['deferred_count']+len(restored),
                              deferred=(report['deferred']+restored)[:128])
            counts=dict(addition=0,replacement=0,unchanged=0)
            for e in saved.entries:
                old=prior.get(e.feature.feature_id)
                action='unchanged' if old is not None and fs._entry_wire(e)==fs._entry_wire(old) else \
                       'addition' if e.baseline_digest is None else 'replacement'
                counts[action]+=1
            report['counts']=counts
            report.update(mode='world_first_visit',partitions=len(queue),coverage_complete=True,
                          cached_identities_reused=len(observed & identities),freshness_checked=False,
                          source_diagnostics=getattr(reader,'diagnostics',{}))
            progress(dict(phase='validated',entries=len(saved.entries)))
            return saved,version,report
        finally:
            if reader is not None:reader.close()

    def _claim_path(self,key):
        return self.store.root/'claims'/(fs._hash([self.session.identity,str(key)])+'.json')

    def _claim(self,key):
        value=self.store._read(self._claim_path(key))
        if set(value)!={'session','cell','token','status','owner','expires'} or \
                value['session']!=self.session.identity or value['cell']!=str(key) or \
                not isinstance(value['token'],str) or not re.fullmatch('[0-9a-f]{32}',value['token']) or \
                value['status'] not in ('pending','chosen') or type(value['owner']) is not int or value['owner']<=0 or \
                type(value['expires']) not in (int,float) or not math.isfinite(value['expires']) or value['expires']<0:
            raise fs.CorruptStorage('invalid acquisition claim')
        return value

    def claim_valid(self,key,token):
        path=self._claim_path(key)
        if not path.exists():return False
        value=self._claim(key)
        return value['token']==token and value['status']=='pending' and self.wall()<=value['expires']

    def _pinned(self,state,keys):
        manual_ids={i for e in self.session.baseline.entries for i in bo._identities(e.feature)}
        entries={e.feature.feature_id:e for e in self.session.baseline.entries
                 if any(fs._intersects(e.bounds,k.bounds()) for k in keys)}
        for name,head in state['cells'].items():
            if any(fs._intersects(state['influence'][name],k.bounds()) for k in keys):
                for e in self.object(head).entries:
                    if e.feature.feature_id not in entries and not set(bo._identities(e.feature)) & manual_ids:
                        entries[e.feature.feature_id]=e
        return entries

    def _cooldown(self):
        # Caller owns query-lane and has joined its child. No request-ledger
        # writer can still be alive; the publication mutex is not held here.
        with lease_store(self.store,'query-network',1.)._locked():
            jobs=regions.Jobs(self.store);ledger=jobs.read()
            ledger.update(clock=max(ledger['clock'],self.wall()),next_request=max(ledger['next_request'],self.wall()+60))
            jobs.save(ledger)

    def _resolve(self,key,deadline):
        token=None
        while True:
            with self.store._locked():
                state=self._state()
                if str(key) in state['cells']:return
                if str(key) in self._index()['cells']:
                    cached=True
                else:
                    cached=False;path=self._claim_path(key)
                    if not path.exists() and time.monotonic()<deadline:
                        if len(list(path.parent.iterdir()))>=MAX_OBJECTS:raise fs.ResourceLimit('acquisition claim capacity')
                        token=uuid.uuid4().hex
                        self.store._atomic_write(path,self.store._document(dict(session=self.session.identity,cell=str(key),
                            token=token,status='pending',owner=os.getpid(),expires=self.wall()+max(0,deadline-time.monotonic()))))
                    elif path.exists():
                        claim=self._claim(key)
                        if claim.get('status')!='pending':
                            raise fs.CorruptStorage('chosen acquisition claim has no session choice')
                        if self.wall()>claim['expires']:break
                pinned=self._pinned(state,(key,))
            if cached:
                self._choose((key,));return
            if token is not None:break
            if time.monotonic()>=deadline:break
            self.sleep(min(.05,max(0,deadline-time.monotonic())))
        if token is None:
            self._choose((key,),TimeoutError('acquisition read budget exhausted while awaiting owner'))
            return
        started=time.monotonic()
        try:
            with lease_store(self.store,'query-lane',deadline-time.monotonic())._locked():
                # Another reader may have chosen a safe fallback while this
                # acquisition was queued. Never overwrite that immutable choice.
                if self.ready((key,)):return
                if time.monotonic()>=deadline:raise TimeoutError('acquisition read budget exhausted in queue')
                try:
                    if self.inline_reader or self.reader_factory is not self._default_reader_factory:
                        acquired=self._acquire(key,state,pinned)
                    elif self.md.fetcher.offline_flag:
                        raise OSError('offline: Overture fallback frozen for this session')
                    else:
                        from buildingacquire import Runner
                        request=dict(cache=str(self.md.cache_dir),release=self.md.release,offline=False,
                            session=self.session,key=key,token=token,pinned=tuple(pinned.values()))
                        def progress(value):log.info('OSM acquisition %s %.2fs: %s',key,time.monotonic()-started,value)
                        acquired=(self.runner or Runner()).run(request,deadline,progress)
                    if time.monotonic()>=deadline:raise TimeoutError('acquisition deadline before publication')
                except (OSError,ValueError,RuntimeError) as error:
                    acquired=error
                    try:self._cooldown()
                    except (OSError,ValueError):pass
                self._choose((key,),acquired,token)
        except fs.LockTimeout as error:
            self._choose((key,),TimeoutError('acquisition lane deadline: '+str(error)))

    def view(self,keys,*,preferred_bounds=None):
        keys=tuple(sorted(set(keys)))
        if any(k.z!=mapcells.ZOOM for k in keys):raise ValueError('world acquisition uses the ordinary map-data cell grid')
        deadline=time.monotonic()+READ_BUDGET_S
        order=keys
        if preferred_bounds is not None:
            w,s,e,n=fs._bounds(preferred_bounds);cx,cy=(w+e)/2,(s+n)/2
            order=sorted(keys,key=lambda key:((key.center()[1]-cx)*math.cos(math.radians(cy)))**2+(key.center()[0]-cy)**2)
        for key in order:self._resolve(key,deadline)
        return self._commit(keys)

    def _commit(self,keys,prepared=None,claim_token=None):
        pinned,keys,heads=self._choose(keys,prepared,claim_token)
        if pinned is None:pinned=self._pinned(self._state(),keys)
        # Geometry conversion for rendering is private to this reader. It must
        # never extend the shared publication critical section.
        return self._view(pinned,keys,heads)

    def _choose(self,keys,prepared=None,claim_token=None):
        keys=tuple(sorted(set(keys)))
        if any(k.z!=mapcells.ZOOM for k in keys):raise ValueError('world acquisition uses the ordinary map-data cell grid')
        state=self._state()
        if all(str(k) in state['cells'] for k in keys):
            # Atomic ledger + immutable objects: ordinary cached reads need no
            # exclusive writer mutex, including potentially costly decoding.
            return None,keys,state['cells']
        with self.store._locked():
            state=self._state()
            manual_ids={i for e in self.session.baseline.entries for i in bo._identities(e.feature)}
            manual={e.feature.feature_id:e for e in self.session.baseline.entries
                    if any(fs._intersects(e.bounds,key.bounds()) for key in keys)}
            if all(str(k) in state['cells'] for k in keys):
                return None,keys,state['cells']
            index=self._index()
            pinned=dict(manual)
            frozen_keys={mapcells.parse(k) for k in state['cells']}
            # Keep all choices durably, but materialize only their relevant full
            # influences. Travelling elsewhere must not consume a cumulative
            # in-memory building quota or reload the whole travelled world.
            frozen={mapcells.parse(k):self.object(h) for k,h in state['cells'].items()
                    if any(fs._intersects(state['influence'][k],key.bounds()) for key in keys)}
            # Manual accepted snapshot always wins over older first-visit objects.
            for snapshot in frozen.values():
                for e in snapshot.entries:
                    if e.feature.feature_id not in pinned and not set(bo._identities(e.feature)) & manual_ids:
                        pinned[e.feature.feature_id]=e
            for key in keys:
                if key in frozen_keys:continue
                if len(state['cells'])>=MAX_CELLS:raise fs.ResourceLimit('session geographic choice capacity')
                name=str(key);digest=index['cells'].get(name)
                report=dict(mode='world_cached',coverage_complete=True)
                source=None
                if digest:
                    chosen=self.object(digest)
                    raw=self.store._read(self.store.objects/(digest+'.json'));source=raw['source']
                    report=dict(raw['report'],mode='world_cached',freshness_checked=False)
                else:
                    fallback,fallback_source=self.cached_entries(index,key.bounds())
                    try:
                        if len(index['cells'])>=MAX_CELLS:raise fs.ResourceLimit('persistent cell capacity')
                        if isinstance(prepared,BaseException):raise prepared
                        if prepared is None:raise TimeoutError('no acquisition result; fallback frozen')
                        if claim_token is None or not self.claim_valid(key,claim_token):raise TimeoutError('acquisition result fenced')
                        chosen,source,report=prepared
                        if state['source']!=source:raise ValueError('acquisition source pin changed')
                        identities={i for e in chosen.entries for f in (e.feature,)+e.parts for i in bo._identities(f)}
                        if len(set(index['identities']) | identities)>regions.MAX_SOURCE_FEATURES:
                            raise fs.ResourceLimit('persistent OSM identity capacity')
                        digest=_write_object(self.store,chosen,source,key.bounds(),report)
                        index['cells'][name]=digest
                        index['influence'][name]=fs._union([key.bounds()]+[e.bounds for e in chosen.entries])
                        for identity in identities:index['identities'].setdefault(identity,digest)
                        self.store._atomic_write(self.store.root/'initialized',b'1\n')
                        self.store._atomic_write(self.store.root/'index.json',self.store._document(index))
                    except (OSError,ValueError,RuntimeError) as error:
                        digest=None  # a failed index write must not select its uncommitted candidate
                        source=fallback_source
                        chosen=replace(_empty(self.md.release),entries=tuple(fallback[k] for k in sorted(fallback)))
                        report=dict(mode='world_fallback',coverage_complete=False,error=str(error)[:512])
                        log.warning('OSM first visit %s: %s',name,report)
                accepted=dict(pinned);deferred=0
                previous=regions.SpatialEntries(pinned.values())
                for e in chosen.entries:
                    fid=e.feature.feature_id
                    if fid in pinned or set(bo._identities(e.feature)) & manual_ids:continue
                    # This is the key no-live-replacement fence. An influence
                    # touching previously rendered cells cannot be introduced.
                    if any(fs._intersects(e.bounds,k.bounds()) for k in frozen_keys):
                        deferred+=1;continue
                    if any(fs._intersects(bo._bbox(e.feature.geometry),bo._bbox(old.feature.geometry)) and
                           bo._overlap(e.feature.geometry,bo._validated(e.feature.geometry)[0],
                                       old.feature.geometry,bo._validated(old.feature.geometry)[0])
                           for old in previous.near(bo._bbox(e.feature.geometry)) if old.feature.status=='building'):
                        deferred+=1;continue
                    accepted[fid]=e
                if len(accepted)+sum(len(e.parts) for e in accepted.values())>primary.REGIONAL_LIMITS.max_entries:
                    accepted=dict(pinned)
                    report=dict(mode='world_fallback',coverage_complete=False,error='building read entry capacity')
                    log.warning('OSM first visit %s: %s',name,report)
                local=replace(_empty(self.md.release),entries=tuple(accepted[fid] for fid in sorted(accepted) if fs._intersects(accepted[fid].bounds,key.bounds())))
                report=dict(report,session_boundary_deferred=deferred,
                            all_features_updated=bool(report.get('all_features_updated')) and deferred==0)
                # Session object includes the already pinned neighbour decisions,
                # so a respawned worker reconstructs the same cross-cell geometry.
                session_source=source or state['source']
                if local.entries and session_source is None:
                    session_source=self.session.baseline.refreshes[0]['source']
                if digest is not None and not deferred and \
                        [fs._entry_wire(e) for e in local.entries]==[fs._entry_wire(e) for e in chosen.entries]:
                    head=digest  # a session choice need not duplicate an immutable cache object
                else:
                    head=_write_object(self.store,local,session_source,key.bounds(),report)
                state['cells'][name]=head
                state['influence'][name]=fs._union([key.bounds()]+[e.bounds for e in local.entries])
                self.store._atomic_write(self.path,self.store._document(state))
                claim=self._claim_path(key)
                if claim.exists():
                    value=self.store._read(claim);value['status']='chosen'
                    self.store._atomic_write(claim,self.store._document(value))
                frozen[key]=local;frozen_keys.add(key);pinned=accepted
                log.info('OSM cell %s: %s',name,report)
            return pinned,keys,state['cells']


def saved_entries(cache,bounds,release,ignore=()):
    """Explicit offline refresh must also retain absent earlier first-visit data."""
    store=_store(cache);entries={}
    with store._locked():
        path=store.root/'index.json'
        if not path.exists():return ()
        index=store._read(path)
        if index['release'] is None and not index['cells']:return ()
        if index['release']!=release:raise fs.BaselineMismatch('world cache baseline changed')
        if len(index['cells'])>MAX_CELLS:raise fs.CorruptStorage('world index cell budget')
        for key,head in index['cells'].items():
            if not fs._intersects(mapcells.parse(key).bounds(),bounds):continue
            for entry in _load_object(store,head,release).entries:
                if entry.feature.feature_id in ignore:continue
                if fs._intersects(entry.bounds,bounds):
                    old=entries.get(entry.feature.feature_id)
                    if old is not None and fs._entry_wire(old)!=fs._entry_wire(entry):
                        raise fs.CorruptStorage('cached neighbouring cells disagree')
                    entries[entry.feature.feature_id]=entry
    return tuple(entries[k] for k in sorted(entries))
