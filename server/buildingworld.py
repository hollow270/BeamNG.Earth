"""Worldwide cache-first OSM-only buildings, frozen before ordinary generation.

No baseline lookup, spatial merge, active-mesh transaction or collision handoff.
Every cell and every parent/part group has one immutable choice per session.
"""
from dataclasses import dataclass,replace
from collections import OrderedDict
import logging,math,os,re,time,uuid,threading
from pathlib import Path

import buildingoverlay as geom
import buildingoverlaystore as fs
import buildingregions as jobs
import layercakebuildings as lc
import mapcells
import osmbuildingcache as data
from osmbuildingview import View

log=logging.getLogger('earthd.osm_world')
DOWNLOAD_S=45.
PROCESS_S=30.
PUBLICATION_S=15.
READ_BUDGET_S=DOWNLOAD_S+PROCESS_S+PUBLICATION_S
MAX_OBJECTS=data.MAX_OBJECTS
MAX_CELLS=data.MAX_CELLS
PREPARE_CELLS=16  # bounded work queue; one existing-size cell in memory at a time


def configured(cache):
    store=data.Store(cache)
    path=store.root/'enabled.json'
    if path.exists():
        if store._read(path)!=dict(mode=data.MODE,source=lc.URL,policy_confirmed=True):raise fs.CorruptStorage('invalid OSM source permission')
        return True
    # Reuse authorization only. Never read/migrate the old merged geometry.
    old=Path(cache)/'osm-primary/v1/worldwide.json'
    if old.exists():
        for p in (old,*old.absolute().parents):data.Store._no_link(p)
        permission=object.__new__(fs.OverlayStore)
        permission.directory=Path(cache).absolute();permission.root=old.parent;permission.limits=data.LIMITS
        if permission._read(old)!=dict(version=1,source=lc.URL,policy_confirmed=True):raise fs.CorruptStorage('invalid legacy source permission')
        return True
    # This application's selected building backend is Layercake's published
    # public Parquet/Range service (not an arbitrary public Overpass endpoint).
    # Buildings-on first visits must work without a preparatory CLI action.
    # --offline and --buildings off remain the ordinary network/content controls.
    return True


def enable(cache,*,policy_confirmed=False):
    if policy_confirmed is not True:raise ValueError('confirm the Layercake source policy')
    store=data.Store(cache)
    with store.session(),store._locked():
        store._atomic_write(store.root/'enabled.json',store._document(dict(mode=data.MODE,source=lc.URL,policy_confirmed=True)))
    return dict(mode=data.MODE,worldwide_enabled=True,freshness='manual_only',effective='new session')


@dataclass(frozen=True)
class Session:
    identity:str
    enabled:bool
    refresh:bool=False


def _store(cache):return data.Store(cache)
def lease_store(store,name,timeout):return store.lease(name,timeout)
def _empty(release=None):return data.empty()


def start(cache,baseline=None,*,refresh=False):
    store=data.Store(cache);enabled=configured(cache)
    if refresh and not enabled:raise ValueError('OSM source permission required')
    with store._locked():
        index=store.index()  # detect missing/corrupt ownership before creating a session
        if not (store.root/'index.json').exists():
            store._atomic_write(store.root/'index.json',store._document(dict(mode=data.MODE,cells={},influence={},identities={})))
            store._atomic_write(store.root/'initialized',b'1\n')
        if len(list((store.root/'sessions').iterdir()))>=data.MAX_SESSIONS:
            raise fs.ResourceLimit('OSM session capacity')
        descriptor=Session(uuid.uuid4().hex,enabled,refresh)
        store._atomic_write(store.root/'sessions'/(descriptor.identity+'.json'),store._document(dict(
            mode=data.MODE,enabled=enabled,refresh=refresh,source=None,cells={},identities={},reports={})))
    return descriptor


class Provider:
    def __init__(self,md,session,*,reader_factory=None,runner=None,wall=time.time,sleep=time.sleep):
        if not isinstance(session,Session) or not re.fullmatch('[0-9a-f]{32}',session.identity):raise ValueError('invalid OSM session')
        self.md,self.session=md,session;self.store=data.Store(md.cache_dir)
        self.path=self.store.root/'sessions'/(session.identity+'.json')
        self.reader_factory=reader_factory;self.runner=runner;self.wall=wall;self.sleep=sleep
        self.decoded=OrderedDict()
        self._objects_lock=threading.RLock();self._memo_lock=threading.Lock();self._state_memo=None
        self._state_load_lock=threading.Lock()
        self.errors={}

    def _state(self):
        # One checked read per new file identity, not one per planning thread.
        # This is a process-local cache-fill lock, never the publication mutex.
        with self._state_load_lock:return self._load_state()

    def _load_state(self):
        self.store._safe(self.path)
        stamp=data.signature(self.path)
        with self._memo_lock:
            if self._state_memo is not None and self._state_memo[0]==stamp:return self._state_memo[1]
        value=self.store._read(self.path)
        if 'reports' not in value:value['reports']={}  # earlier local prototype session, geometry unchanged
        if set(value)!={'mode','enabled','refresh','source','cells','identities','reports'} or value['mode']!=data.MODE or \
                value['enabled']!=self.session.enabled or value['refresh']!=self.session.refresh or \
                len(value['cells'])>MAX_CELLS or len(value['identities'])>100000 or len(value['reports'])>MAX_CELLS:raise fs.CorruptStorage('invalid OSM session')
        if value['source'] is not None:data.version(value['source'])
        for key,head in value['cells'].items():mapcells.parse(key);self.store.digest(head)
        for identity,head in value['identities'].items():
            if geom.normalize_osm_id(identity)!=identity:raise fs.CorruptStorage('invalid OSM identity choice')
            self.store.digest(head)
        if data.signature(self.path)==stamp:
            with self._memo_lock:self._state_memo=(stamp,value)
        return value

    def object(self,head):
        with self._objects_lock:return self._object(head)

    def _object(self,head):
        self.store.digest(head);p=self.store.objects/(head+'.json');self.store._safe(p)
        stamp=p.stat();signature=(stamp.st_size,stamp.st_mtime_ns)
        got=self.decoded.get(head)
        if got is not None and got[0]==signature:self.decoded.move_to_end(head);return got[1]
        value=self.store.object(head);self.decoded[head]=(signature,value)
        while len(self.decoded)>1 and (len(self.decoded)>8 or sum(v[0][0] for v in self.decoded.values())>64*1024**2):
            self.decoded.popitem(last=False)
        return value

    def ready(self,keys):return all(str(k) in self._state()['cells'] for k in keys)

    def cached_revision(self,keys):
        """Read-only cache identity. Missing inputs defer, never start acquisition."""
        state=self._state();rows=[]
        for key in sorted(set(keys)):
            name=str(key)
            if name not in state['cells']:raise fs.LockTimeout('OSM inputs not prepared; defer generation')
            token=state['reports'].get(name,{}).get('dependency')
            if token is None:token=fs._hash(['legacy-session',self.session.identity,state['cells'][name]])
            self.store.digest(token);rows.append((name,token))
        return fs._hash([data.MODE,'prepared-cells-v1',rows])

    def prepare(self,keys,*,preferred_bounds=None,priority=0):
        if type(priority) is not int or not 0<=priority<=2:raise ValueError('invalid building preparation priority')
        keys=tuple(sorted(set(keys)))
        if any(k.z!=mapcells.ZOOM for k in keys):raise ValueError('OSM data-cell zoom required')
        if preferred_bounds is not None:
            w,s,e,n=fs._bounds(preferred_bounds);x,y=(w+e)/2,(s+n)/2
            keys=tuple(sorted(keys,key=lambda k:((k.center()[1]-x)*math.cos(math.radians(y)))**2+(k.center()[0]-y)**2))
        deadline=time.monotonic()+READ_BUDGET_S
        for key in keys:
            if priority:self._resolve(key,deadline,keys,priority=priority)
            else:self._resolve(key,deadline,keys)

    def _pin_source(self,version):
        data.version(version)
        with self.store._locked():
            state=dict(self._state())
            if state['source'] is not None and state['source']!=version:raise ValueError('session OSM source version changed')
            state['source']=version;self.store._atomic_write(self.path,self.store._document(state))
        return state

    def _claim_path(self,key):return self.store.root/'claims'/(fs._hash([self.session.identity,str(key)])+'.json')

    def _claim(self,key):
        value=self.store._read(self._claim_path(key))
        if set(value)!={'session','cell','token','status','owner','expires'} or value['session']!=self.session.identity or value['cell']!=str(key) or \
                not isinstance(value['token'],str) or not re.fullmatch('[0-9a-f]{32}',value['token']) or \
                value['status'] not in ('pending','chosen') or type(value['owner']) is not int or value['owner']<=0 or \
                type(value['expires']) not in (int,float) or not math.isfinite(value['expires']):raise fs.CorruptStorage('invalid OSM claim')
        return value

    def claim_valid(self,key,token):
        return self._claim_path(key).exists() and (v:=self._claim(key))['status']=='pending' and v['token']==token and self.wall()<=v['expires']

    def _publish(self,key,token,snapshot,source,report,prepared_callback=lambda:None):
        # Prepare/validate immutable bytes before a short compare-and-swap commit.
        end=time.monotonic()+PUBLICATION_S
        for _ in range(32):
            state_stamp=data.signature(self.path);state=self._state()
            if data.signature(self.path)!=state_stamp:continue
            stamp=data.signature(self.store.root/'index.json');index=self.store.index()
            if data.signature(self.store.root/'index.json')!=stamp:continue
            if str(key) in index['cells'] and not self.session.refresh:
                with self.store._locked():
                    if data.signature(self.path)!=state_stamp:state=self._state()
                    if not self.claim_valid(key,token) or str(key) in state['cells']:
                        raise fs.StalePreview('OSM acquisition claim fenced')
                    if state['source']!=source:raise fs.StalePreview('OSM source pin fenced')
                    if data.signature(self.store.root/'index.json')==stamp:return index['cells'][str(key)]
                continue
            if len(index['cells'])>=MAX_CELLS and str(key) not in index['cells']:raise fs.ResourceLimit('OSM cell capacity')
            old_head=index['cells'].get(str(key));reused=0;accepted=[];owners={};result_report=dict(report)
            for entry in snapshot.entries:
                owner=index['identities'].get(entry.feature.feature_id)
                if owner is not None and not self.session.refresh:
                    if owner not in owners:owners[owner]={e.feature.feature_id:e for e in self.object(owner)[0].entries}
                    prior=owners[owner].get(entry.feature.feature_id)
                    if prior is None:raise fs.CorruptStorage('cached OSM parent owner missing')
                    entry=prior;reused+=1
                elif not self.session.refresh and any(p.feature_id in index['identities'] for p in entry.parts):
                    result_report=dict(result_report,issue_count=result_report['issue_count']+1,all_features_updated=False,
                                       issues=(result_report['issues']+[(entry.feature.feature_id,'part_identity_in_other_cached_parent')])[:128])
                    continue
                accepted.append(entry)
            effective=replace(snapshot,entries=tuple(accepted))
            result_report.update(cached_osm_identities_reused=reused,
                all_features_updated=bool(result_report.get('all_features_updated')) and not reused)
            ids={f.feature_id for e in effective.entries for f in (e.feature,)+e.parts}
            if len(set(index['identities'])|ids)>100000:raise fs.ResourceLimit('persistent OSM identity capacity')
            prepared=self.store.prepare(effective,source,key.bounds(),result_report);head=prepared.head
            if self.session.refresh and old_head is not None:
                for entry in self.object(old_head)[0].entries:
                    for f in (entry.feature,)+entry.parts:
                        if f.feature_id not in ids and index['identities'].get(f.feature_id)==old_head:
                            index['identities'].pop(f.feature_id,None)
            for identity in ids:
                if self.session.refresh:index['identities'][identity]=head
                else:index['identities'].setdefault(identity,head)
            index['cells'][str(key)]=head
            index['influence'][str(key)]=fs._union([key.bounds()]+[e.bounds for e in effective.entries])
            body=self.store._document(index)
            prepared_callback()
            with self.store._locked():
                if not self.claim_valid(key,token):raise fs.StalePreview('OSM acquisition claim fenced')
                if data.signature(self.path)!=state_stamp:state=self._state()
                if str(key) in state['cells']:raise fs.StalePreview('OSM session choice already immutable')
                if state['source']!=source:raise fs.StalePreview('OSM source pin fenced')
                if data.signature(self.store.root/'index.json')==stamp:
                    self.store.write_prepared(prepared)
                    self.store._atomic_write(self.store.root/'index.json',body)
                    return head
            if time.monotonic()>=end:break
        raise fs.LockTimeout('OSM publication changed concurrently; defer generation')

    def _acquire(self,key,state,pinned=None,progress=lambda value:None,token=None,batch=(),deadline=None):
        if not self.session.enabled:raise OSError('OSM building retrieval not authorized')
        if self.md.fetcher.offline_flag:raise OSError('offline: no cached OSM cell')
        ledger=jobs.Jobs(self.store);request_state=ledger.read()
        if max(request_state['clock'],request_state['next_request'])-self.wall()>2:raise OSError('OSM source cooldown')
        reader=self.reader_factory() if self.reader_factory else lc.Reader(Path(self.md.cache_dir)/'mapdata/raw',self.md.fetcher,
            tolerant=True,raw_budget=jobs.RAW_BYTES,index_dir=self.store.root/'source-rows')
        try:
            if hasattr(reader,'ranges'):
                limits=jobs.Requests(ledger,request_state,Path(self.md.cache_dir)/'mapdata/raw',self.wall,lambda:False,self.sleep,
                    extra_raw=self.store.root/'source-rows')
                reader.ranges.admit=limits.admit;reader.ranges.failed=limits.failed
            source=state['source']
            if source is None:source=reader.select(explicit=True);self._pin_source(source)
            else:reader.attach(source)
            progress(dict(phase='source_selected',source=source))
            targets=((key,token),)+tuple(batch)
            if len(targets)>PREPARE_CELLS or len({k for k,t in targets})!=len(targets) or \
                    any(k.z!=key.z or max(abs(k.x-key.x),abs(k.y-key.y))>2 for k,t in targets):raise ValueError('invalid OSM acquisition batch')
            if any(not self.claim_valid(k,t) for k,t in targets):raise fs.StalePreview('OSM batch claim fenced')
            if hasattr(reader,'next_region'):
                return self._stream_cells(reader,ledger,source,targets,progress,deadline or time.monotonic()+READ_BUDGET_S)
            targets=tuple((k,t) for k,t in targets if (k.x//2,k.y//2)==(key.x//2,key.y//2))[:4]
            staged=False
            if len(targets)>1 and hasattr(reader,'prepare_cells'):
                selected=reader.prepare_cells(tuple(k for k,t in targets))
                targets=tuple((k,t) for k,t in targets if k in selected)
                staged=True
            else:
                targets=targets[:1]
                if hasattr(reader,'prepare_region'):
                    reader.prepare_region(key.bounds());staged=True
            progress(dict(phase='parquet_staged',diagnostics=getattr(reader,'diagnostics',{})))
            prepared=[];features=vertices=0
            for cell,claim in targets:
                # Complete local staging already proves a bounded source area.
                # Sparse/empty cells need ONE lookup, not dozens of 400 m jobs.
                # Dense cells still split until every reply fits the same cap.
                queue=[cell.bounds()] if staged else jobs.partition(cell.bounds())
                results=[];index=0
                while index<len(queue):
                    bounds=queue[index];result=ledger.load(source,bounds)
                    if result is None:
                        try:result=reader.query(bounds)
                        except fs.ResourceLimit as e:
                            if 'partition feature budget' not in str(e):raise
                            if len(queue)+3>jobs.MAX_JOBS:raise fs.ResourceLimit('OSM cell partition capacity')
                            queue[index:index+1]=jobs.split(bounds);continue
                        if tuple(result.bounds)!=tuple(bounds) or result.version!=source:raise ValueError('OSM response identity changed')
                        ledger.save_job(result)
                    results.append(result);index+=1
                snapshot,report=data.normalize(results)
                features+=sum(1+len(e.parts) for e in snapshot.entries)
                vertices+=sum(len(f.geometry['coordinates'][0]) for e in snapshot.entries for f in (e.feature,)+e.parts)
                if len(prepared) and (features>data.MAX_FEATURES or vertices>1000000):
                    progress(dict(phase='batch_reduced',reason='aggregate normalized feature/vertex budget'))
                    prepared=prepared[:1];break
                report.update(mode='osm_first_visit',partitions=len(queue),source=source,
                    source_diagnostics=getattr(reader,'diagnostics',{}),freshness_checked=self.session.refresh)
                prepared.append((cell,claim,snapshot,report))
                progress(dict(phase='normalized',cell=str(cell),entries=len(snapshot.entries),issues=report['issue_count']))
            primary=None
            for cell,claim,snapshot,report in prepared:
                if not self.claim_valid(cell,claim):raise fs.StalePreview('OSM batch claim fenced')
                head=self._publish(cell,claim,snapshot,source,report,prepared_callback=lambda:progress(
                    dict(phase='validated',entries=len(snapshot.entries),issues=report['issue_count'])))
                if cell==key:primary=head
                progress(dict(phase='published',cell=str(cell),head=head))
            if primary is None:raise fs.StalePreview('OSM primary result missing')
            return primary
        finally:reader.close()

    def _stream_cells(self,reader,ledger,source,targets,progress,deadline):
        """Publish each full cell before reading the next. Reuse one source index.

        The queue is larger than the former in-memory four-cell batch, but peak
        accepted features/vertices, native memory and the aggregate watchdog are
        unchanged. Unfinished extras are deferred and retain their claim fences.
        """
        primary=None
        for position,(cell,claim) in enumerate(targets):
            if position and deadline-time.monotonic()<PUBLICATION_S+5:
                progress(dict(phase='batch_deferred',remaining=len(targets)-position,reason='preparation budget'));break
            try:
                if not self.claim_valid(cell,claim):raise fs.StalePreview('OSM sequential claim fenced')
                if position:
                    progress(dict(phase='cell_processing',cell=str(cell)))
                    reader.next_region(cell.bounds())
                else:reader.prepare_region(cell.bounds())
                progress(dict(phase='parquet_staged',cell=str(cell),diagnostics=getattr(reader,'diagnostics',{})))
                queue=[cell.bounds()];results=[];index=0;records=0
                while index<len(queue):
                    bounds=queue[index];result=ledger.load(source,bounds)
                    if result is None:
                        try:result=reader.query(bounds)
                        except fs.ResourceLimit as error:
                            if 'partition feature budget' not in str(error):raise
                            if len(queue)+3>jobs.MAX_JOBS:raise fs.ResourceLimit('OSM cell partition capacity')
                            queue[index:index+1]=jobs.split(bounds);continue
                        if tuple(result.bounds)!=tuple(bounds) or result.version!=source:raise ValueError('OSM response identity changed')
                        ledger.save_job(result)
                    records+=len(result.buildings)+len(result.parts)
                    if records>jobs.MAX_SOURCE_FEATURES:raise fs.ResourceLimit('OSM cell accumulated reply capacity')
                    results.append(result);index+=1
                snapshot,report=data.normalize(results)
                report.update(mode='osm_first_visit',partitions=len(queue),source=source,
                    source_diagnostics=dict(getattr(reader,'diagnostics',{})),freshness_checked=self.session.refresh)
                progress(dict(phase='normalized',cell=str(cell),entries=len(snapshot.entries),issues=report['issue_count']))
                head=self._publish(cell,claim,snapshot,source,report,prepared_callback=lambda:progress(
                    dict(phase='validated',cell=str(cell),entries=len(snapshot.entries),issues=report['issue_count'])))
                if position==0:primary=head
                progress(dict(phase='published',cell=str(cell),head=head))
                del snapshot,results
            except (OSError,ValueError,RuntimeError) as error:
                if primary is None:raise
                progress(dict(phase='batch_deferred',remaining=len(targets)-position,reason=type(error).__name__+': '+str(error)[:256]))
                break
        if primary is None:raise fs.StalePreview('OSM primary result missing')
        return primary

    def _batch_claims(self,key,requested,deadline):
        """Caller owns metadata lock. Reserve only requested, unowned neighbours."""
        index=self.store.index();state=self._state();out=[]
        for other in requested:
            if other==key or other.z!=key.z or max(abs(other.x-key.x),abs(other.y-key.y))>2:continue
            if str(other) in index['cells'] or str(other) in state['cells']:continue
            path=self._claim_path(other);prior=self._claim(other) if path.exists() else None
            if prior is not None and (prior['status']!='pending' or prior['expires']>=self.wall()):continue
            if not path.exists() and len(list(path.parent.iterdir()))>=MAX_OBJECTS:break
            token=uuid.uuid4().hex
            self.store._atomic_write(path,self.store._document(dict(session=self.session.identity,cell=str(other),token=token,
                status='pending',owner=os.getpid(),expires=self.wall()+max(0,deadline-time.monotonic())+PUBLICATION_S)))
            out.append((other,token))
            if len(out)==PREPARE_CELLS-1:break
        return tuple(out)

    def _release_batch(self,batch):
        if not batch:return
        with self.store._locked():
            for key,token in batch:
                claim=self._claim(key)
                if claim['status']=='pending' and claim['token']==token:
                    claim['expires']=self.wall()-1
                    self.store._atomic_write(self._claim_path(key),self.store._document(claim))

    def _choose(self,key,error=None,available=()):
        end=time.monotonic()+PUBLICATION_S
        for _ in range(32):
            state_stamp=data.signature(self.path);state=self._state()
            if data.signature(self.path)!=state_stamp:continue
            if str(key) in state['cells']:return
            index_stamp=data.signature(self.store.root/'index.json');index=self.store.index()
            if data.signature(self.store.root/'index.json')!=index_stamp:continue
            head=index['cells'].get(str(key));prepared=None
            if head is None:
                known={};source=None
                for name in sorted(index['cells']):
                    if not fs._intersects(index['influence'][name],key.bounds()):continue
                    saved,raw=self.object(index['cells'][name])
                    for e in saved.entries:
                        if fs._intersects(e.bounds,key.bounds()):known.setdefault(e.feature.feature_id,e);source=source or raw['source']
                report=dict(mode='osm_cached_partial' if known else 'osm_unavailable',coverage_complete=False,
                            error=str(error or 'OSM data unavailable')[:512])
                snapshot=replace(data.empty(),entries=tuple(known[k] for k in sorted(known)))
                prepared=self.store.prepare(snapshot,source,key.bounds(),report);head=prepared.head
                raw=dict(report=report)
            else:snapshot,raw=self.object(head)
            state=dict(state,cells=dict(state['cells']),identities=dict(state['identities']),reports=dict(state['reports']))
            chosen=[]
            def add(cell,chosen_head,chosen_snapshot,chosen_raw):
                ids={f.feature_id for e in chosen_snapshot.entries for f in (e.feature,)+e.parts}
                if len(set(state['identities'])|ids)>100000 or len(state['cells'])>=MAX_CELLS:return False
                for entry in chosen_snapshot.entries:
                    owner=state['identities'].get(entry.feature.feature_id,chosen_head)
                    for f in (entry.feature,)+entry.parts:state['identities'].setdefault(f.feature_id,owner)
                state['cells'][str(cell)]=chosen_head
                dependency=fs._hash([chosen_head,sorted({state['identities'][e.feature.feature_id] for e in chosen_snapshot.entries})])
                state['reports'][str(cell)]=dict(mode=chosen_raw['report'].get('mode','osm_cached'),
                    issues=chosen_raw['report'].get('issue_count',0),dependency=dependency)
                chosen.append(cell);return True
            if not add(key,head,snapshot,raw):raise fs.ResourceLimit('OSM session capacity')
            raw=dict(report=raw['report']);del snapshot
            # These requested neighbours are ALREADY fully published. Pin them
            # in the same durable transaction instead of rewriting the growing
            # identity map once per cell. Refresh must still retrieve every cell.
            if not self.session.refresh:
                for cell in available:
                    if len(chosen)>=PREPARE_CELLS:break
                    if str(cell) in state['cells'] or str(cell) not in index['cells']:continue
                    saved,meta=self.object(index['cells'][str(cell)])
                    added=add(cell,index['cells'][str(cell)],saved,meta)
                    del saved,meta
                    if not added:break
            body=self.store._document(state);reports=state['reports'].values()
            summary=dict(mode=data.MODE,unavailable=sum(r['mode']=='osm_unavailable' for r in reports),
                partial=sum(r['mode']=='osm_cached_partial' for r in reports),unsupported=sum(bool(r['issues']) for r in reports))
            summary_body=self.store._document(summary)
            with self.store._locked():
                if data.signature(self.path)!=state_stamp or data.signature(self.store.root/'index.json')!=index_stamp:
                    if time.monotonic()>=end:break
                    continue
                if prepared is not None:self.store.write_prepared(prepared)
                self.store._atomic_write(self.path,body)
                for cell in chosen:
                    if self._claim_path(cell).exists():
                        claim=self._claim(cell);claim['status']='chosen'
                        self.store._atomic_write(self._claim_path(cell),self.store._document(claim))
                try:self.store._atomic_write(self.store.root/'status'/(self.session.identity+'.json'),summary_body)
                except OSError:log.warning('OSM status summary could not be written; geometry choice is already durable')
            if prepared is None:log.info('OSM cell %s: osm_cached head=%s',key,head)
            else:log.warning('OSM cell %s: %s',key,raw['report'])
            return
        raise fs.LockTimeout('OSM choices changed concurrently; defer generation')

    def note(self):
        """Tiny, read-only advisory for the existing status text; never fetches."""
        if not self.session.enabled:return 'OSM acquisition disabled; saved OSM only'
        path=self.store.root/'status'/(self.session.identity+'.json')
        if not path.exists():return ''
        try:
            value=self.store._read(path)
            if value['mode']!=data.MODE or any(type(value[k]) is not int or not 0<=value[k]<=MAX_CELLS for k in ('unavailable','partial','unsupported')):
                raise ValueError('invalid status counts')
            parts=[]
            if value['unavailable']:parts.append(f"{value['unavailable']} session cells unavailable")
            if value['partial']:parts.append(f"{value['partial']} session cells use partial cached OSM")
            if value['unsupported']:parts.append(f"unsupported records in {value['unsupported']} cells")
            return 'OSM buildings: '+ '; '.join(parts) if parts else ''
        except (OSError,ValueError,TypeError,KeyError):return 'OSM building status unavailable; inspect companion log'

    def _resolve(self,key,deadline,requested=(),priority=0):
        token=None;batch=();path=self._claim_path(key);poll=.05
        while not self.ready((key,)):
            state_stamp=data.signature(self.path);state=self._state()
            if data.signature(self.path)!=state_stamp:continue
            if str(key) in state['cells']:return
            index_stamp=data.signature(self.store.root/'index.json');index=self.store.index()
            if str(key) in index['cells'] and not self.session.refresh:break
            claim=self._claim(key) if path.exists() else None
            if claim is not None and claim['status']!='pending':
                if self.ready((key,)):return
                raise fs.CorruptStorage('OSM chosen claim missing session choice')
            if claim is not None and claim['expires']>=self.wall():
                if time.monotonic()>=deadline:raise fs.LockTimeout('OSM acquisition pending; defer ordinary generation')
                self.sleep(min(poll,max(0,deadline-time.monotonic())))
                poll=min(.5,poll*1.5)
                continue  # waiters never poll the global writer lock
            if time.monotonic()>=deadline:raise fs.LockTimeout('OSM acquisition queue deadline; defer generation')
            with self.store._locked():
                if data.signature(self.path)!=state_stamp:continue
                if data.signature(self.store.root/'index.json')!=index_stamp:continue
                fresh=self._claim(key) if path.exists() else None
                if fresh is not None and fresh['status']!='pending':
                    raise fs.CorruptStorage('OSM chosen claim missing session choice')
                if fresh is not None and fresh['expires']>=self.wall():continue
                if not path.exists() and len(list(path.parent.iterdir()))>=MAX_OBJECTS:raise fs.ResourceLimit('OSM claim capacity')
                token=uuid.uuid4().hex
                self.store._atomic_write(path,self.store._document(dict(session=self.session.identity,cell=str(key),token=token,
                    status='pending',owner=os.getpid(),expires=self.wall()+max(0,deadline-time.monotonic())+PUBLICATION_S)))
                if self.reader_factory is None and self.runner is None and not self.session.refresh:
                    batch=self._batch_claims(key,requested,deadline)
                break
        error=None
        if token is not None:
            admitted=False
            try:
                if not self.session.enabled:raise OSError('OSM acquisition is not authorized; buildings unavailable')
                if self.md.fetcher.offline_flag:raise OSError('offline: no complete cached OSM cell')
                from buildingpriority import Queue
                with Queue(self.store).enter(priority,deadline):
                    if self.ready((key,)):return
                    if time.monotonic()>=deadline:raise TimeoutError('OSM acquisition queue deadline')
                    admitted=True
                    if self.reader_factory is not None:
                        self._acquire(key,self._state(),progress=lambda p:log.info('OSM %s: %s',key,p),token=token)
                    else:
                        from buildingacquire import Runner
                        request=dict(cache=str(self.md.cache_dir),session=self.session,key=key,token=token,
                                     release=data.MODE,offline=self.md.fetcher.offline_flag,pinned=(),batch=batch)
                        (self.runner or Runner()).run(request,deadline,lambda p:log.info('OSM %s: %s',key,p))
            except (OSError,ValueError,RuntimeError) as e:
                if isinstance(e,fs.LockTimeout) or not admitted and isinstance(e,TimeoutError):
                    # Runner has already stopped its owned child. Retire only
                    # this token so a transient deferral can retry immediately.
                    try:
                        with self.store._locked():
                            claim=self._claim(key)
                            if claim['status']=='pending' and claim['token']==token:
                                claim['expires']=self.wall()-1
                                self.store._atomic_write(self._claim_path(key),self.store._document(claim))
                    except fs.LockTimeout:pass
                    raise fs.LockTimeout('OSM acquisition queued; defer ordinary generation and retry') from e
                error=e
                self.errors[str(key)]=str(e)[:512]
                # The request reservation already survives a killed query.
                # Don't hold the metadata mutex or retry this session's choice.
                log.warning('OSM acquisition %s: %s',key,str(e)[:512])
            finally:self._release_batch(batch)
        if token is None and not self.ready((key,)) and str(key) not in self.store.index()['cells']:
            raise fs.LockTimeout('OSM acquisition pending; defer ordinary generation and retry')
        self._choose(key,error,available=requested)

    def view(self,keys,*,preferred_bounds=None):
        keys=tuple(sorted(set(keys)))
        self.prepare(keys,preferred_bounds=preferred_bounds)
        return self.view_prepared(keys)

    def view_prepared(self,keys):
        keys=tuple(sorted(set(keys)));self.cached_revision(keys)
        state=self._state();entries={};reports=[];lookups={}
        for key in keys:
            snapshot,raw=self.object(state['cells'][str(key)]);reports.append(raw['report'])
            for entry in snapshot.entries:
                identity=entry.feature.feature_id;owner=state['identities'][identity]
                if owner not in lookups:lookups[owner]={e.feature.feature_id:e for e in self.object(owner)[0].entries}
                selected=lookups[owner].get(identity)
                if selected is None:raise fs.CorruptStorage('OSM identity owner missing')
                if any(fs._intersects(selected.bounds,k.bounds()) for k in keys):entries[identity]=selected
        if len(entries)+sum(len(e.parts) for e in entries.values())>data.MAX_FEATURES:raise fs.ResourceLimit('OSM render-read feature capacity')
        return View(replace(data.empty(),entries=tuple(entries[k] for k in sorted(entries))),dict(cells=reports,errors=dict(self.errors)))


def refresh(cache,keys,fetcher,*,reader_factory=None):
    """Explicit source replacement only while the companion session lease is free."""
    from types import SimpleNamespace
    store=data.Store(cache)
    with store.session():
        with store._locked():index=store.index()
        selected=set(keys);ids_by_cell={};inspected=0
        def identities(name):
            nonlocal inspected
            if name not in ids_by_cell:
                snapshot,_=store.object(index['cells'][name])
                values={f.feature_id for e in snapshot.entries for f in (e.feature,)+e.parts}
                inspected+=len(values)
                if inspected>100000:raise fs.ResourceLimit('manual OSM identity closure capacity; previous data retained')
                ids_by_cell[name]=values
            return ids_by_cell[name]
        # Refresh all cached copies of connected full footprints/parts together.
        # No geometry comparison, baseline lookup or active-session mutation.
        while True:
            if len(selected)>256:raise fs.ResourceLimit('manual OSM cell closure capacity; previous data retained')
            known=set().union(*(identities(str(k)) for k in selected if str(k) in index['cells']))
            owners={index['identities'][i] for i in known if i in index['identities']}
            extra={mapcells.parse(name) for name in index['cells'] if mapcells.parse(name) not in selected and
                   (index['cells'][name] in owners or any(fs._intersects(index['influence'][name],k.bounds()) for k in selected)) and identities(name)&known}
            if not extra:break
            selected|=extra
        with store._locked():store._atomic_write(store.root/'rollback.json',store._document(index))
        session=start(cache,refresh=True)
        provider=Provider(SimpleNamespace(cache_dir=cache,fetcher=fetcher),session,reader_factory=reader_factory)
        reports=[]
        for key in sorted(selected):reports.append(provider.view((key,)).report)
        return dict(mode=data.MODE,checked=not provider.errors,errors=dict(provider.errors),reports=reports,
                    cells=len(selected),effective='new session')


def status(cache):
    store=data.Store(cache)
    with store._locked():index=store.index()
    return dict(mode=data.MODE,worldwide_enabled=configured(cache),cached_cells=len(index['cells']),freshness='manual_only',
                note='No Overture buildings. Missing source data is reported unavailable; cached OSM data is reused.')


def rollback(cache):
    store=data.Store(cache)
    with store.session(),store._locked():
        previous=store._read(store.root/'rollback.json')
        if set(previous)!={'mode','cells','influence','identities'} or previous['mode']!=data.MODE or len(previous['cells'])>MAX_CELLS or set(previous['cells'])!=set(previous['influence']):
            raise fs.CorruptStorage('invalid OSM rollback index')
        for key,head in previous['cells'].items():mapcells.parse(key);store.object(head)
        store._atomic_write(store.root/'index.json',store._document(previous))
    return dict(mode=data.MODE,rolled_back=True,effective='new session')


def main(argv=None):
    import argparse,json,netfetch
    p=argparse.ArgumentParser(description='OSM-only buildings: status or explicit next-session regional freshness')
    p.add_argument('--cache-dir',required=True);p.add_argument('--status',action='store_true')
    p.add_argument('--center',nargs=2,type=float);p.add_argument('--radius-km',type=float,default=3.)
    p.add_argument('--user-dir');p.add_argument('--confirm-source-policy',action='store_true')
    args=p.parse_args(argv)
    if args.status:print(json.dumps(status(args.cache_dir),indent=2));return
    if not args.confirm_source_policy:p.error('explicit source-policy confirmation required for freshness')
    centre=args.center
    if centre is None and args.user_dir:
        path=Path(args.user_dir)/'settings/beamngEarth_lastpos.json'
        for q in (path,*path.absolute().parents):data.Store._no_link(q)
        with path.open('rb') as f:raw=f.read(16385)
        if len(raw)>16384:raise ValueError('saved position exceeds size limit')
        value=json.loads(raw);centre=(value['lat'],value['lon'])
    if centre is None:p.error('supply --center LAT LON or an isolated --user-dir for manual freshness')
    w,s,e,n=jobs.area(*centre,args.radius_km)
    keys=mapcells.cells_for_bbox(s,w,n,e,max_cells=256)
    enable(args.cache_dir,policy_confirmed=True)
    fetcher=netfetch.Fetcher(stats_dir=str(Path(args.cache_dir)/'netstats'))
    try:print(json.dumps(refresh(args.cache_dir,keys,fetcher),indent=2))
    finally:fetcher.close()


if __name__=='__main__':main()
