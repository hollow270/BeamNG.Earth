"""OSM-only immutable building objects; no legacy overlay/baseline migration.

Reuse checked paths, checksums, OS locks and fsync/replace from the existing store.
An object is source data, not a game generation or collision ownership record.
"""
from contextlib import contextmanager
from dataclasses import dataclass,replace
from pathlib import Path
import os,re,threading,time,logging

import buildingoverlay as geom
import buildingoverlaystore as fs
import layercakebuildings as lc
import osmbuildings as osm

MODE='osm-only-v1'
MAX_FEATURES=50000
MAX_OBJECTS=8192
MAX_CELLS=4096
MAX_SESSIONS=1024
LIMITS=fs.Limits(max_entries=MAX_FEATURES,max_file_bytes=96*1024**2,max_total_bytes=512*1024**2)


@dataclass(frozen=True)
class Prepared:
    head:str
    body:bytes


def signature(path):
    """Atomic-file identity, including replacement with equal-sized content."""
    try:
        s=path.stat()
        return s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns
    except FileNotFoundError:return None


def version(value):
    if not isinstance(value,dict) or set(value)!={'etag','size','last_modified','url'} or value['url']!=lc.URL or \
            not isinstance(value['etag'],str) or not re.fullmatch(r'"[!#-~]{1,200}"',value['etag']) or \
            type(value['size']) is not int or not 16<value['size']<1024**4:raise ValueError('invalid OSM source version')
    osm._time(value['last_modified'])


def empty():return fs.Snapshot(None,True,MODE,(),(),(),(),None)


def attributes(feature):
    values=geom._attrs(feature)
    # Reject implausible source values explicitly rather than create enormous
    # physics bounds or silently clamp the user's geometry/height.
    for name,limit in (('height',2000),('min_height',2000),('roof_height',500),('num_floors',600),('min_floor',600)):
        if values.get(name,0)>limit:raise ValueError('unsupported_attribute_extent:'+name)
    return values


def normalize(results):
    """Validate only OSM records. Partition duplicates must agree exactly."""
    records={};issues=set()
    for result in results:
        if result.complete is not True:raise ValueError('incomplete OSM response')
        for f in result.buildings+result.parts:
            if f.feature_id in records and fs._wire(records[f.feature_id])!=fs._wire(f):
                raise ValueError('OSM partition identities disagree')
            records[f.feature_id]=f
        issues.update(result.issues)
    if len(records)>MAX_FEATURES:raise fs.ResourceLimit('OSM cell feature capacity')
    valid={};vertices=0
    parts={f.feature_id for r in results for f in r.parts}
    for identity,f in sorted(records.items()):
        ring,error=geom._validated(f.geometry)
        if error:
            issues.add((identity,error));continue
        if geom.normalize_osm_id(identity)!=identity or geom._identities(f)!=(identity,) or f.status!='building':
            raise ValueError('OSM-only identity/status required')
        if not f.sources or any(s.get('dataset')!='OpenStreetMap' for s in f.sources):
            raise ValueError('non-OSM building source rejected')
        if any(s.get('dataset')!='OpenStreetMap' for values in f.attribute_sources.values() for s in values):
            raise ValueError('non-OSM attribute source rejected')
        try:attributes(f)
        except (TypeError,ValueError) as error:
            issues.add((identity,'invalid_or_unsupported_attributes:'+str(error)[:128]));continue
        vertices+=len(ring)
        if vertices>1000000:raise fs.ResourceLimit('OSM cell vertex capacity')
        if identity in parts and (f.parent_id not in records or f.parent_id in parts):
            issues.add((identity,'unsupported_part_parent'));continue
        valid[identity]=f
    children={}
    for identity in sorted(parts & valid.keys()):
        f=valid[identity];parent=valid.get(f.parent_id)
        if parent is None or not geom._contained(geom._validated(f.geometry)[0],geom._validated(parent.geometry)[0]):
            issues.add((identity,'unsupported_part_parent_or_extent'));continue
        children.setdefault(f.parent_id,[]).append(f)
    entries=[]
    for identity in sorted(valid.keys()-parts):
        f=valid[identity];ps=tuple(sorted(children.get(identity,()),key=lambda p:p.feature_id))
        f=replace(f,attrs=dict(f.attrs,has_parts=bool(ps)))
        entries.append(fs.Entry(f,ps,None,fs._union([geom._bbox(q.geometry) for q in (f,)+ps]),
                                f.sources[0]['dataset_timestamp']))
    # A parent returned in another partition resolves this partition-local note.
    issues={i for i in issues if not (i[1]=='parent_not_in_complete_partition' and i[0] in valid and valid[i[0]].parent_id in valid)}
    return replace(empty(),entries=tuple(entries),affected_bounds=fs._union([e.bounds for e in entries])),dict(
        source_features=len(records),supported_buildings=len(entries),supported_parts=sum(len(e.parts) for e in entries),
        issue_count=len(issues),issues=sorted(issues)[:128],coverage_complete=True,all_features_updated=not issues)


class Store:
    _no_link=staticmethod(fs.OverlayStore._no_link)
    _safe=fs.OverlayStore._safe
    _document=fs.OverlayStore._document
    _read=fs.OverlayStore._read

    @contextmanager
    def _io_gate(self,path):
        # Windows replacement cannot race open destination handles. Serialize
        # byte reads against rename only, independently of the metadata writer
        # mutex. JSON/geometry validation NEVER holds this gate. Fixed 32 stripes
        # bound lock files regardless of sessions, cells or objects retained.
        if os.name!='nt' or not hasattr(self,'directory'):
            yield;return
        stripe=int(fs._hash(str(path.relative_to(self.root)))[:4],16)%32
        directory=self.root/'io'/str(stripe)
        for p in (directory.parent,directory):self._no_link(p);p.mkdir(exist_ok=True)
        gate=object.__new__(fs.OverlayStore);gate.root=directory;gate.directory=self.directory
        gate.limits=self.limits;gate.lock_timeout=self.lock_timeout
        with gate._locked():yield

    @contextmanager
    def _open_read(self,path):
        with self._io_gate(path),path.open('rb') as f:yield f

    def _replace(self,temporary,path):
        with self._io_gate(path):os.replace(temporary,path)

    def _atomic_write(self,path,body):
        # Windows readers may briefly deny replacement while an immutable
        # metadata document is open. Retry the atomic operation, never truncate
        # the previous document or remove it to work around a sharing violation.
        end=time.monotonic()+.25
        while True:
            try:return fs.OverlayStore._atomic_write(self,path,body)
            except PermissionError as e:
                if os.name!='nt' or getattr(e,'winerror',None) not in (5,32,33) or time.monotonic()>=end:raise
                time.sleep(.005)

    def __init__(self,cache):
        self.directory=Path(cache).absolute();self.root=self.directory/'osm-buildings/v1'
        self.objects=self.root/'objects';self.limits=LIMITS;self.lock_timeout=15.
        self._index_memo=None;self._memo_lock=threading.Lock();self._index_load_lock=threading.Lock()
        self.lock_stats=dict(count=0,max_wait_s=0.,max_hold_s=0.);self._last_lock_note=0.
        for p in (self.root,*self.root.parents):self._no_link(p)
        for p in (self.root,self.objects,self.root/'sessions',self.root/'claims',self.root/'status'):
            self._no_link(p);p.mkdir(parents=True,exist_ok=True)

    @contextmanager
    def _locked(self):
        tick=time.monotonic()
        with fs.OverlayStore._locked(self):
            acquired=time.monotonic()
            try:yield
            finally:
                if hasattr(self,'lock_stats'):
                    wait,hold=acquired-tick,time.monotonic()-acquired
                    self.lock_stats['count']+=1
                    self.lock_stats['max_wait_s']=max(self.lock_stats['max_wait_s'],wait)
                    self.lock_stats['max_hold_s']=max(self.lock_stats['max_hold_s'],hold)
                    if max(wait,hold)>.5 and acquired-self._last_lock_note>10:
                        self._last_lock_note=acquired
                        logging.getLogger('earthd.osm_world').warning('OSM metadata lock waited %.3f s; held %.3f s',wait,hold)

    @contextmanager
    def session(self):
        with self.lease('session',1)._locked():yield

    def lease(self,name,timeout):
        if name not in ('session','query-lane','query-network'):raise ValueError('invalid OSM lease')
        value=object.__new__(Store);value.directory=self.directory;value.root=self.root/name
        value.limits=self.limits;value.lock_timeout=max(.001,timeout)
        self._no_link(value.root);value.root.mkdir(exist_ok=True)
        return value

    def index(self):
        # Concurrent planners share one checksum/schema validation for this
        # immutable file identity. Writers still use their ordinary CAS fence.
        with self._index_load_lock:return self._load_index()

    def _load_index(self):
        path=self.root/'index.json'
        self._safe(path)
        if not path.exists():
            if (self.root/'initialized').exists() or any(self.objects.iterdir()):raise fs.CorruptStorage('OSM index missing')
            return dict(mode=MODE,cells={},influence={},identities={})
        stamp=signature(path)
        with self._memo_lock:
            memo=self._index_memo
        if memo is not None and memo[0]==stamp:
            value=memo[1]
            return dict(value,cells=dict(value['cells']),identities=dict(value['identities']),
                        influence={k:list(v) for k,v in value['influence'].items()})
        value=self._read(path)
        if not isinstance(value,dict) or set(value) not in ({'mode','cells'},{'mode','cells','influence'},{'mode','cells','influence','identities'}) or value['mode']!=MODE or not isinstance(value['cells'],dict) or len(value['cells'])>MAX_CELLS:
            raise fs.CorruptStorage('invalid OSM index')
        import mapcells
        for key,head in value['cells'].items():mapcells.parse(key);self.digest(head)
        if 'influence' not in value:
            value['influence']={key:fs._union([mapcells.parse(key).bounds()]+[e.bounds for e in self.object(head)[0].entries]) for key,head in value['cells'].items()}
        if set(value['cells'])!=set(value['influence']):raise fs.CorruptStorage('OSM influence index mismatch')
        for bounds in value['influence'].values():fs._bounds(bounds)
        if 'identities' not in value:
            value['identities']={}
            for name in sorted(value['cells']):
                head=value['cells'][name]
                for entry in self.object(head)[0].entries:
                    for f in (entry.feature,)+entry.parts:value['identities'].setdefault(f.feature_id,head)
        if len(value['identities'])>100000:raise fs.CorruptStorage('OSM cached identity capacity')
        for identity,head in value['identities'].items():
            if geom.normalize_osm_id(identity)!=identity:raise fs.CorruptStorage('invalid cached OSM identity')
            self.digest(head)
        if signature(path)==stamp:
            with self._memo_lock:self._index_memo=(stamp,value)
        return dict(value,cells=dict(value['cells']),identities=dict(value['identities']),
                    influence={k:list(v) for k,v in value['influence'].items()})

    @staticmethod
    def digest(value):
        if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):raise fs.CorruptStorage('invalid OSM object identity')
        return value

    def object(self,head):
        raw=self._read(self.objects/(self.digest(head)+'.json'))
        return self._decode_object(head,raw)

    def _decode_object(self,head,raw):
        if fs._hash(raw)!=head or set(raw)!={'mode','entries','source','bounds','report'} or raw['mode']!=MODE:
            raise fs.CorruptStorage('OSM object checksum/schema mismatch')
        if raw['source'] is not None:version(raw['source'])
        if len(raw['entries'])>MAX_FEATURES:raise fs.CorruptStorage('OSM object entry capacity')
        fs._bounds(raw['bounds']);ids=set();entries=[]
        for row in raw['entries']:
            if row['baseline_digest'] is not None:raise fs.CorruptStorage('legacy matched building rejected')
            f=fs._feature(row['feature']);ps=tuple(fs._feature(p) for p in row['parts'])
            for q in (f,)+ps:
                if q.feature_id in ids or geom.normalize_osm_id(q.feature_id)!=q.feature_id or \
                        not q.sources or any(s.get('dataset')!='OpenStreetMap' for s in q.sources) or \
                        q.status!='building' or geom._validated(q.geometry)[1]:raise fs.CorruptStorage('invalid OSM-only feature')
                ids.add(q.feature_id);attributes(q)
                if len(ids)>MAX_FEATURES or any(s.get('dataset')!='OpenStreetMap' for values in q.attribute_sources.values() for s in values):
                    raise fs.CorruptStorage('OSM object feature/provenance capacity')
            if f.parent_id is not None or bool(f.attrs.get('has_parts'))!=bool(ps) or any(p.parent_id!=f.feature_id or
                    not geom._contained(geom._validated(p.geometry)[0],geom._validated(f.geometry)[0]) for p in ps):
                raise fs.CorruptStorage('invalid OSM part group')
            box=fs._bounds(row['bounds']);actual=fs._union([geom._bbox(q.geometry) for q in (f,)+ps])
            if not (box[0]<=actual[0]<=actual[2]<=box[2] and box[1]<=actual[1]<=actual[3]<=box[3]):raise fs.CorruptStorage('invalid OSM influence')
            osm._time(row['source_timestamp'])
            entries.append(fs.Entry(f,ps,None,box,row['source_timestamp']))
        if raw['entries'] and raw['source'] is None:raise fs.CorruptStorage('unattributed OSM data')
        return replace(empty(),entries=tuple(entries),affected_bounds=fs._union([e.bounds for e in entries])),raw

    def prepare(self,snapshot,source,bounds,report):
        """Serialize and fully validate outside the metadata transaction."""
        payload=dict(mode=MODE,entries=[fs._entry_wire(e) for e in snapshot.entries],source=source,bounds=list(bounds),report=report)
        head=fs._hash(payload);body=self._document(payload)
        self._decode_object(head,payload)
        return Prepared(head,body)

    def write_prepared(self,prepared):
        """Caller holds metadata lock: quota admission + atomic bytes only."""
        if not isinstance(prepared,Prepared):raise ValueError('validated OSM object required')
        path=self.objects/(self.digest(prepared.head)+'.json');body=prepared.body
        if not path.exists():
            files=list(self.objects.iterdir())
            if len(files)>=MAX_OBJECTS:raise fs.ResourceLimit('retained OSM object count capacity')
            size=0
            for p in files:self._no_link(p);size+=p.stat().st_size
            if size+len(body)>self.limits.max_total_bytes:raise fs.ResourceLimit('retained OSM bytes capacity')
            self._atomic_write(path,body)
        else:
            self._no_link(path)
            with path.open('rb') as f:existing=f.read(self.limits.max_file_bytes+1)
            if existing!=body:raise fs.CorruptStorage('OSM object bytes changed')
        return prepared.head

    def write(self,snapshot,source,bounds,report):
        # Compatibility for fixtures/tools; production prepares before locking.
        return self.write_prepared(self.prepare(snapshot,source,bounds,report))
