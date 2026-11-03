"""OSM-only immutable building objects; no legacy overlay/baseline migration.

Reuse checked paths, checksums, OS locks and fsync/replace from the existing store.
An object is source data, not a game generation or collision ownership record.
"""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import re

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
    _locked=fs.OverlayStore._locked
    _document=fs.OverlayStore._document
    _atomic_write=fs.OverlayStore._atomic_write
    _read=fs.OverlayStore._read

    def __init__(self,cache):
        self.directory=Path(cache).absolute();self.root=self.directory/'osm-buildings/v1'
        self.objects=self.root/'objects';self.limits=LIMITS;self.lock_timeout=15.
        for p in (self.root,*self.root.parents):self._no_link(p)
        for p in (self.root,self.objects,self.root/'sessions',self.root/'claims',self.root/'status'):
            self._no_link(p);p.mkdir(parents=True,exist_ok=True)

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
        path=self.root/'index.json'
        if not path.exists():
            if (self.root/'initialized').exists() or any(self.objects.iterdir()):raise fs.CorruptStorage('OSM index missing')
            return dict(mode=MODE,cells={},influence={},identities={})
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
        return value

    @staticmethod
    def digest(value):
        if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):raise fs.CorruptStorage('invalid OSM object identity')
        return value

    def object(self,head):
        raw=self._read(self.objects/(self.digest(head)+'.json'))
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

    def write(self,snapshot,source,bounds,report):
        payload=dict(mode=MODE,entries=[fs._entry_wire(e) for e in snapshot.entries],source=source,bounds=list(bounds),report=report)
        head=fs._hash(payload);path=self.objects/(head+'.json')
        if not path.exists():
            body=self._document(payload);files=list(self.objects.iterdir())
            if len(files)>=MAX_OBJECTS:raise fs.ResourceLimit('retained OSM object count capacity')
            size=0
            for p in files:self._no_link(p);size+=p.stat().st_size
            if size+len(body)>self.limits.max_total_bytes:raise fs.ResourceLimit('retained OSM bytes capacity')
            self._atomic_write(path,body)
        self.object(head)  # fail before any pointer can select damaged data
        return head
