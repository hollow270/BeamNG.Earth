"""Explicit pre-game regional preparation; ordinary streaming only after publication.

One ETag, one writer/session lease, bounded durable retrieval jobs. Complete jobs
are immutable and reusable after cancellation/crash. Coverage and the snapshot head
are published together only after ALL retrieval jobs and baseline checks succeed.
Unsupported/conflicting connected feature groups retain previous data and are
reported separately from coverage. No absence-derived deletion or live activation.
"""
from dataclasses import replace
import argparse
import json
import math
from pathlib import Path
import time

import buildingoverlay as bo
import buildingoverlaystore as fs
import buildingprimary as primary
import layercakebuildings as lc
import osmbuildings as osm

MAX_JOBS=4096
MAX_SOURCE_FEATURES=100000
RAW_BYTES=2*1024**3
JOB_BYTES=256*1024**2
OPERATION_BYTES=1024**3
CELL_M=400


class SpatialEntries:
    """Bounded coarse index for city-sized retained data; exact bounds still decide."""
    def __init__(self,entries):
        self.grid={}
        for entry in entries:
            for key in self.keys(entry.bounds):self.grid.setdefault(key,[]).append(entry)

    @staticmethod
    def keys(bounds):
        w,s,e,n=bounds
        x0,x1=math.floor(w*400),math.floor(e*400)
        y0,y1=math.floor(s*400),math.floor(n*400)
        if (x1-x0+1)*(y1-y0+1)>4096:raise fs.ResourceLimit('regional influence index budget')
        return ((x,y) for x in range(x0,x1+1) for y in range(y0,y1+1))

    def near(self,bounds):
        found={e.feature.feature_id:e for key in self.keys(bounds) for e in self.grid.get(key,()) if fs._intersects(e.bounds,bounds)}
        return tuple(found[k] for k in sorted(found))


def configured(cache):
    return (Path(cache)/'osm-primary/v1/regions/state.json').is_file()


def area(lat,lon,radius_km):
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in (lat,lon,radius_km)) or \
            not -80<lat<80 or not -180<lon<180 or not .1<=radius_km<=10:
        raise ValueError('centre must be below 80 degrees latitude; radius must be 0.1–10 km')
    dy=radius_km*1000/111320; dx=dy/math.cos(math.radians(lat))
    return fs._bounds((lon-dx,lat-dy,lon+dx,lat+dy))


def partition(bounds):
    w,s,e,n=fs._bounds(bounds)
    nx=max(1,math.ceil((e-w)*111320*math.cos(math.radians((s+n)/2))/CELL_M))
    ny=max(1,math.ceil((n-s)*111320/CELL_M))
    if nx*ny>MAX_JOBS: raise fs.ResourceLimit('region partition budget')
    return [[w+(e-w)*x/nx,s+(n-s)*y/ny,w+(e-w)*(x+1)/nx,s+(n-s)*(y+1)/ny]
            for y in range(ny) for x in range(nx)]


def split(bounds):
    w,s,e,n=bounds
    if min(e-w,n-s)*111320<12: raise fs.ResourceLimit('dense partition cannot be split safely further')
    x,y=(w+e)/2,(s+n)/2
    return [[w,s,x,y],[x,s,e,y],[w,y,x,n],[x,y,e,n]]


def empty():
    return dict(version=1,clock=0.,next_request=0.,next_check=0.,status='not_prepared',jobs=[],
                source=None,region=None,revision=None,completed=0,total=0)


class Jobs:
    def __init__(self,store):
        self.store=store
        self.root=store.root/'regions'
        store._no_link(self.root);self.root.mkdir(exist_ok=True)
        self.objects=self.root/'objects'
        store._no_link(self.objects);self.objects.mkdir(exist_ok=True)

    def read(self):
        path=self.root/'state.json'
        if not path.exists():
            if (self.root/'initialized').exists(): raise fs.CorruptStorage('regional ledger missing; requests blocked')
            return empty()
        value=self.store._read(path)
        try:
            if set(value)!=set(empty()) or value['version']!=1 or len(value['jobs'])>MAX_JOBS: raise ValueError()
            for k in ('clock','next_request','next_check'):
                if type(value[k]) not in (int,float) or not math.isfinite(value[k]) or value[k]<0: raise ValueError()
            if value['source'] is not None: primary.validate_version(value['source'])
            if value['region'] is not None: fs._bounds(value['region'])
            for job in value['jobs']: fs._bounds(job)
        except (TypeError,KeyError,ValueError): raise fs.CorruptStorage('invalid regional request ledger') from None
        return value

    def save(self,state):
        self.store._atomic_write(self.root/'initialized',b'1\n')
        self.store._atomic_write(self.root/'state.json',self.store._document(state))

    def key(self,source,bounds):
        return fs._hash([source,bounds])

    def load(self,source,bounds):
        path=self.objects/(self.key(source,bounds)+'.json')
        if not path.exists(): return None
        wire=self.store._read(path)
        if set(wire)!={'bounds','version','buildings','parts','issues','complete'} or \
                wire['bounds']!=list(bounds) or wire['version']!=source or wire['complete'] is not True or \
                len(wire['buildings'])+len(wire['parts'])>lc.MAX_FEATURES:
            raise fs.CorruptStorage('regional job identity or completion mismatch')
        return lc.Snapshot(tuple(bounds),tuple(fs._feature(f) for f in wire['buildings']),
                           tuple(fs._feature(f) for f in wire['parts']),source,tuple(tuple(i) for i in wire['issues']))

    def save_job(self,result):
        if result.complete is not True: raise ValueError('incomplete source partition')
        body=self.store._document(dict(bounds=list(result.bounds),version=result.version,
            buildings=[fs._wire(f) for f in result.buildings],parts=[fs._wire(f) for f in result.parts],
            issues=list(result.issues),complete=True))
        files=list(self.objects.iterdir())
        if len(files)>=MAX_JOBS*4: raise fs.ResourceLimit('retained retrieval job count; archive explicitly')
        size=0
        for p in files:
            self.store._no_link(p);size+=p.stat().st_size
        if size+len(body)>JOB_BYTES: raise fs.ResourceLimit('retained retrieval jobs full; previous data retained')
        self.store._atomic_write(self.objects/(self.key(result.version,result.bounds)+'.json'),body)


class Requests:
    """Reserve EVERY range before HTTP, persist Retry-After, honour cancellation.

    netfetch still applies endpoint policies, offline restrictions and host budgets.
    Exactly one request runs at a time; spacing also survives a process restart.
    """
    def __init__(self,jobs,state,raw,wall,cancel,sleep,extra_raw=None):
        self.jobs,self.state,self.raw=jobs,state,Path(raw)/'osm-buildings'
        self.wall,self.cancel,self.sleep=wall,cancel,sleep
        self.used=0
        self.extra_raw=Path(extra_raw) if extra_raw is not None else None
        if self.extra_raw is not None and not self.extra_raw.is_relative_to(jobs.store.root):raise ValueError('source index quota path outside store')

    def admit(self,amount):
        if self.cancel(): raise InterruptedError('cancelled before request')
        if self.wall()<self.state['clock']: raise osm.Throttled('request clock regressed')
        while self.wall()<self.state['next_request']:
            if self.cancel(): raise InterruptedError('cancelled while waiting')
            self.sleep(min(1,self.state['next_request']-self.wall()))
        self.used+=amount
        if self.used>OPERATION_BYTES: raise fs.ResourceLimit('regional operation network byte budget; resume later')
        size=count=0
        roots=[self.raw]+([self.extra_raw] if self.extra_raw is not None else [])
        for root in roots:
            self.jobs.store._no_link(root)
            for p in root.rglob('*'):
                self.jobs.store._no_link(p);count+=1
                if count>32768: raise fs.ResourceLimit('raw range cache entry budget')
                if p.is_file(): size+=p.stat().st_size
        if size+amount>RAW_BYTES: raise fs.ResourceLimit('raw range cache full; retained versions not deleted')
        now=self.wall();self.state.update(clock=now,next_request=now+1.)
        self.jobs.save(self.state)

    def failed(self,code,headers):
        if code in (429,503):
            delay=osm._retry_after(headers.get('retry-after'),self.wall())
            self.state['next_request']=max(self.state['next_request'],self.wall()+max(60,delay))
            self.jobs.save(self.state)


def _groups(features):
    """Connect overlapping footprints/parents across source job boundaries first.

    Independent components may share a bounded merge batch; competing footprints
    never go to different batches. Excessively large/complex components are deferred.
    """
    byid={f.feature_id:f for f in features};parent={i:i for i in byid};grid={}
    rings={f.feature_id:bo._validated(f.geometry)[0] for f in features}
    boxes={f.feature_id:bo._bbox(f.geometry) for f in features}
    def root(i):
        while parent[i]!=i: parent[i]=parent[parent[i]];i=parent[i]
        return i
    def join(a,b): parent[root(a)]=root(b)
    for f in features:
        b=boxes[f.feature_id]
        if b is None: continue
        x0,x1=math.floor(b[0]*400),math.floor(b[2]*400)
        y0,y1=math.floor(b[1]*400),math.floor(b[3]*400)
        if (x1-x0+1)*(y1-y0+1)>4096: raise fs.ResourceLimit('source influence index budget')
        seen=set()
        for x in range(x0,x1+1):
            for y in range(y0,y1+1):
                bucket=grid.setdefault((x,y),[])
                if len(bucket)>4096: raise fs.ResourceLimit('dense source influence index')
                for other in bucket:
                    if other in seen:continue
                    seen.add(other)
                    c=boxes[other]
                    if min(b[2],c[2])>max(b[0],c[0]) and min(b[3],c[3])>max(b[1],c[1]):
                        # AABBs of ordinary rotated neighbours overlap even when
                        # footprints do not. Do not turn a city of those into one
                        # artificial over-budget ambiguity group.
                        a,z=rings[f.feature_id],rings[other]
                        if a is None or z is None or bo._overlap(f.geometry,a,byid[other].geometry,z):join(f.feature_id,other)
                bucket.append(f.feature_id)
        if f.parent_id in byid: join(f.feature_id,f.parent_id)
    groups={}
    for f in features: groups.setdefault(root(f.feature_id),[]).append(f)
    return list(groups.values())


def candidate(md,before,results,region,source,cancel=lambda:False,known_returned=()):
    existing=SpatialEntries(before.entries)
    records={};issues=set()
    for result in results:
        for f in result.buildings+result.parts:
            if f.feature_id in records and fs._wire(records[f.feature_id])!=fs._wire(f):
                raise ValueError('boundary records disagree within the pinned source')
            records[f.feature_id]=f
        issues.update(result.issues)
    if len(records)>MAX_SOURCE_FEATURES: raise fs.ResourceLimit('regional source feature budget')
    observed=set(known_returned) | {i for f in records.values() for i in bo._identities(f)}
    observed.update(i[0] for i in issues if bo.normalize_osm_id(i[0])==i[0])
    missing=sorted({i for e in before.entries if fs._intersects(e.bounds,region)
                    for i in bo._identities(e.feature)}-observed)
    # A part's parent may have arrived in a neighbouring partition.
    issues={i for i in issues if not (i[1]=='parent_not_in_complete_partition' and
                i[0] in records and records[i[0]].parent_id in records)}
    bad={i[0] for i in issues};deferred=[];batches={}
    for group in _groups(tuple(records[k] for k in sorted(records))):
        ids={f.feature_id for f in group}
        bounds=fs._union([bo._bbox(f.geometry) for f in group])
        if ids & bad or len(group)>lc.MAX_FEATURES or bounds is None:
            deferred.extend((i,'unsupported_or_incomplete_group') for i in sorted(ids));continue
        key=(math.floor(bounds[0]*400),math.floor(bounds[1]*400))
        chunks=batches.setdefault(key,[[]])
        if sum(map(len,chunks[-1]))+len(group)>lc.MAX_FEATURES: chunks.append([])
        chunks[-1].append(group)
    proposals=[];checks=[];counts={k:0 for k in ('addition','replacement','unchanged')}
    for chunks in batches.values():
        pending=list(chunks)
        while pending:
            components=pending.pop(0)
            group=[f for component in components for f in component]
            if cancel(): raise InterruptedError('cancelled during baseline preparation')
            buildings=tuple(f for f in group if f.parent_id is None)
            parts=tuple(f for f in group if f.parent_id is not None)
            if not buildings:
                deferred.extend((f.feature_id,'unresolved_parts') for f in group);continue
            bounds=fs._union([bo._bbox(f.geometry) for f in group])
            local=replace(before,entries=existing.near(bounds))
            try:
                saved,report,context=primary.prepare(md,local,lc.Snapshot(bounds,buildings,parts,source))
            except (ValueError,fs.ResourceLimit) as error:
                if len(components)>1:
                    half=len(components)//2
                    pending[0:0]=[components[:half],components[half:]]
                    continue
                # Safe fallback is the unchanged previous entry or ordinary Overture.
                deferred.extend((f.feature_id,str(error)[:256]) for f in group);continue
            original={e.feature.feature_id:e for e in local.entries}
            updates={e.feature.feature_id:e for e in saved.entries if original.get(e.feature.feature_id)!=e}
            proposals.append((updates,group,report));checks.append(context)
    # Never choose a first winner if two batches claim the same original.
    owners={};blocked=set()
    for index,(updates,group,report) in enumerate(proposals):
        for fid in updates:
            if fid in owners: blocked.update((index,owners[fid]))
            owners[fid]=index
    # Absence retains prior additions/overrides. New accepted geometry must not
    # overlap those retained entries, even when the source no longer returns them.
    for index,(updates,group,report) in enumerate(proposals):
        for entry in updates.values():
            b=bo._bbox(entry.feature.geometry)
            # Check all old entries except the ones this atomic batch replaces.
            # Another proposal may itself be deferred, leaving its old geometry.
            for old in existing.near(b):
                if old.feature.feature_id in updates or old.feature.status!='building': continue
                c=bo._bbox(old.feature.geometry)
                if fs._intersects(b,c) and bo._overlap(entry.feature.geometry,bo._validated(entry.feature.geometry)[0],
                                                     old.feature.geometry,bo._validated(old.feature.geometry)[0]):
                    blocked.add(index)
    entries={e.feature.feature_id:e for e in before.entries}
    for index,(updates,group,report) in enumerate(proposals):
        if index in blocked:
            deferred.extend((f.feature_id,'competing_or_retained_overlay') for f in group);continue
        entries.update(updates)
        for k in counts: counts[k]+=report['counts'][k]
    if len(entries)+sum(len(e.parts) for e in entries.values())>primary.REGIONAL_LIMITS.max_entries:
        raise fs.ResourceLimit('accepted regional snapshot capacity; previous data retained')
    # Bound diagnostics in the published snapshot; all detailed source issues are
    # durably retained in the job objects. No claim that deferred records updated.
    report=dict(mode='regional_prepared',coverage_complete=True,all_features_updated=not deferred and not issues and not missing,
        source_version=source,source_features=len(records),counts=counts,deferred_count=len(deferred),
        deferred=deferred[:128],issues=sorted(issues)[:128],issue_count=len(issues),
        missing_unverified=missing[:128],missing_unverified_count=len(missing),
        note='Missing records are not verified deletions. Publication time is not OSM replication time.')
    return replace(before,enabled=True,baseline_release=md.release,entries=tuple(entries[k] for k in sorted(entries)),
                   affected_bounds=fs._union([e.bounds for e in entries.values()])),report,checks


def update(md,cache,bounds,*,policy_confirmed=False,reader=None,cancel=lambda:False,progress=lambda r:None,
           wall=time.time,sleep=time.sleep):
    if policy_confirmed is not True: raise ValueError('explicit source policy confirmation required')
    bounds=fs._bounds(bounds);initial_jobs=partition(bounds)
    store=primary.Store(cache)
    with store.session():
        before=store.load();jobs=Jobs(store);state=jobs.read()
        from buildingworld import saved_entries
        previous={e.feature.feature_id:e for e in saved_entries(cache,bounds,md.release,ignore={e.feature.feature_id for e in before.entries})}
        previous.update((e.feature.feature_id,e) for e in before.entries)
        before=replace(before,entries=tuple(previous[k] for k in sorted(previous)))
        if before.revision and before.baseline_release!=md.release: raise fs.BaselineMismatch('saved baseline changed')
        if md.fetcher.offline_flag: raise ValueError('offline: saved data remains reusable; no update attempted')
        if wall()<max(state['clock'],state['next_check'],state['next_request']):
            raise osm.Throttled('persistent source-check cooldown; saved data remains reusable')
        state.update(clock=wall(),next_check=wall()+600,status='checking_source')
        jobs.save(state)  # reserve before the first network request
        own=reader is None
        raw=Path(cache)/'mapdata/raw'
        reader=reader or lc.Reader(raw,md.fetcher,tolerant=True,raw_budget=RAW_BYTES)
        if hasattr(reader,'ranges'):
            requests=Requests(jobs,state,raw,wall,cancel,sleep)
            reader.ranges.admit=requests.admit;reader.ranges.failed=requests.failed
        try:
            version=reader.select(explicit=True);primary.validate_version(version)
            if before.refreshes and osm._time(version['last_modified'])<osm._time(before.refreshes[0]['source']['last_modified']):
                raise ValueError('source publication regressed; saved data retained')
            if state['source']!=version or state['region']!=list(bounds): state['jobs']=initial_jobs
            state.update(source=version,region=list(bounds),status='retrieving',completed=0,total=len(state['jobs']))
            jobs.save(state)
            records={};issues=set();index=0;source_count=0
            while index<len(state['jobs']):
                if cancel(): raise InterruptedError('cancelled between retrieval jobs')
                box=state['jobs'][index]
                result=jobs.load(version,box)
                reused=result is not None
                if result is None:
                    if hasattr(reader,'ranges'): reader.ranges.used=0
                    try: result=reader.query(box)
                    except fs.ResourceLimit as error:
                        if 'partition feature budget' not in str(error): raise
                        children=split(box)
                        if len(state['jobs'])+3>MAX_JOBS: raise fs.ResourceLimit('dense region job budget')
                        state['jobs'][index:index+1]=children;state['total']=len(state['jobs']);jobs.save(state);continue
                    if result.version!=version or tuple(result.bounds)!=tuple(box): raise ValueError('source job identity changed')
                    jobs.save_job(result)
                source_count+=len(result.buildings)+len(result.parts)
                if source_count>MAX_SOURCE_FEATURES*4: raise fs.ResourceLimit('regional boundary record budget')
                for f in result.buildings+result.parts:
                    old=records.get(f.feature_id)
                    if old is not None and fs._wire(old)!=fs._wire(f):
                        raise ValueError('boundary records disagree within the pinned source')
                    records[f.feature_id]=f
                if len(records)>MAX_SOURCE_FEATURES: raise fs.ResourceLimit('regional source feature budget')
                issues.update(result.issues);index+=1
                state.update(completed=index,total=len(state['jobs']))
                jobs.save(state)
                progress(dict(status='retrieving',completed=index,total=state['total'],reused=reused,source=version,
                              target=list(bounds),saved_revision=before.revision))
            state['status']='validating';jobs.save(state);progress(dict(state))
            results=[lc.Snapshot(tuple(bounds),tuple(f for f in records.values() if f.parent_id is None),
                                tuple(f for f in records.values() if f.parent_id is not None),version,tuple(issues))]
            saved,report,checks=candidate(md,before,results,bounds,version,cancel)
            coverage=list(before.refreshes[0]['coverage']) if before.refreshes else []
            coverage=[b for b in coverage if not primary.contains(bounds,b)]
            if not any(primary.contains(b,bounds) for b in coverage): coverage.append(list(bounds))
            if len(coverage)>64: raise fs.ResourceLimit('retained region coverage budget')
            def validate():
                if cancel(): raise InterruptedError('cancelled before atomic publication')
                for context in checks: context.validate(md)
            with store._locked():
                if store._load().revision!=before.revision: raise fs.StalePreview('accepted snapshot changed during preparation')
                published=store.publish(saved,version,coverage,report,validate)
            state.update(status='complete_with_warnings' if not report['all_features_updated'] else 'complete',revision=published.revision)
            jobs.save(state)
            report.update(revision=published.revision,completed=state['completed'],total=state['total'],status=state['status'])
            progress(report)
            return published,report
        except BaseException as error:
            state['status']='cancelled' if isinstance(error,(InterruptedError,KeyboardInterrupt)) else 'failed'
            jobs.save(state)
            progress(dict(status=state['status'],completed=state['completed'],total=state['total'],
                          error=str(error),saved_revision=store._load().revision,reusable=True))
            raise
        finally:
            if own: reader.close()


def status(cache):
    store=primary.Store(cache)
    with store._locked():
        snapshot=store._load();state=Jobs(store).read()
        from buildingworld import configured
        world=store.root/'world/index.json'
        cached=store._read(world) if world.exists() else {'cells':{}}
        return dict(state,saved_revision=snapshot.revision,saved_reusable=True,worldwide_enabled=configured(cache),
                    automatically_cached_cells=len(cached['cells']),
                    accepted_report=snapshot.refreshes[0]['report'] if snapshot.refreshes else None)


def main(argv=None):
    p=argparse.ArgumentParser(description='Prepare OSM buildings before play; never launches a game.')
    p.add_argument('--cache-dir',required=True)
    p.add_argument('--center',type=float,nargs=2,metavar=('LAT','LON'))
    p.add_argument('--user-dir',help='use the location saved by the existing in-game map')
    p.add_argument('--radius-km',type=float,default=3.)
    p.add_argument('--confirm-source-policy',action='store_true')
    p.add_argument('--status',action='store_true')
    args=p.parse_args(argv)
    if args.status:
        print(json.dumps(status(args.cache_dir),indent=2));return
    centre=args.center
    if centre is None and args.user_dir:
        path=Path(args.user_dir)/'settings/beamngEarth_lastpos.json'
        for q in (path,*path.absolute().parents): primary.Store._no_link(q)
        with path.open('rb') as f: raw=f.read(16385)
        if len(raw)>16384: raise ValueError('saved place exceeds size limit')
        pos=json.loads(raw);centre=[pos['lat'],pos['lon']]
    if centre is None: p.error('select a location in the existing map first, or supply --center LAT LON')
    bounds=area(*centre,args.radius_km)
    import mapdata,netfetch
    fetcher=netfetch.Fetcher(stats_dir=str(Path(args.cache_dir)/'netstats'))
    md=mapdata.MapData(args.cache_dir,fetcher=fetcher,building_overlays=False)
    try:
        print(json.dumps(dict(target=bounds,radius_km=args.radius_km,source=lc.URL)))
        update(md,args.cache_dir,bounds,policy_confirmed=args.confirm_source_policy,
               progress=lambda row:print(json.dumps(row,sort_keys=True),flush=True))
    finally: md.close();fetcher.close()


if __name__=='__main__': main()
