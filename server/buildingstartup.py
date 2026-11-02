"""Authorized refresh-before-streaming. No game operations or activation imports."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import buildingbaseline
import buildingoverlay as bo
from buildingoverlaystore import OverlayStore, Limits, CorruptStorage, ResourceLimit, StalePreview, _union, _intersects
import buildingrender
import mapcells
import osmbuildings as osm

log=logging.getLogger('earthd.osm_startup')


def _json(data):
    def pairs(items):
        out={}
        for key,value in items:
            if key in out: raise ValueError('duplicate configuration key')
            out[key]=value
        return out
    def constant(value): raise ValueError('non-finite JSON value')
    return json.loads(data,object_pairs_hook=pairs,parse_constant=constant)


@dataclass(frozen=True)
class Config:
    areas: tuple
    refresh_on_startup: bool = True

    def __post_init__(self):
        if type(self.refresh_on_startup) is not bool or not 1<=len(self.areas)<=8:
            raise osm.ConfigurationError('startup requires one to eight explicitly authorized areas')
        names=set(); areas=[]
        for name,bounds in self.areas:
            if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',name) or name in names:
                raise osm.ConfigurationError('invalid/duplicate saved area name')
            names.add(name); areas.append((name,osm._area(bounds)))
        object.__setattr__(self,'areas',tuple(areas))

    @classmethod
    def read(cls,path):
        path=Path(path).absolute()
        for p in (path,*path.parents): OverlayStore._no_link(p)
        with path.open('rb') as f: data=f.read(16385)
        if len(data)>16384: raise osm.ConfigurationError('startup configuration exceeds 16 KiB')
        value=_json(data)
        if not isinstance(value,dict) or set(value)!={'version','authorized','endpoint_policy_confirmed','refresh_on_startup','areas'} or \
                type(value['version']) is not int or value['version']!=1 or value['authorized'] is not True or \
                value['endpoint_policy_confirmed'] is not True or not isinstance(value['areas'],list):
            raise osm.ConfigurationError('explicit startup and endpoint-policy authorization required')
        areas=[]
        for row in value['areas']:
            if not isinstance(row,dict) or set(row)!={'name','bounds'}: raise osm.ConfigurationError('invalid saved area')
            areas.append((row['name'],row['bounds']))
        return cls(tuple(areas),value['refresh_on_startup'])


class State(OverlayStore):
    """Reuse Stage 2's checked paths, OS lock, checksums and fsync/replace primitives.

    This is an operational ledger, not an overlay revision store. Never calls the
    parent constructor or edits an overlay manifest/generation/ownership journal.
    """
    def __init__(self,cache_dir):
        self.directory=Path(cache_dir).absolute()
        self.root=self.directory/'osm-startup'
        if '..' in self.root.parts: raise ValueError('unsafe startup state path')
        for p in (self.root,*self.root.parents): self._no_link(p)
        self.root.mkdir(parents=True,exist_ok=True)
        self.limits=Limits(max_file_bytes=256*1024)
        self.lock_timeout=1.

    def read(self):
        path=self.root/'ledger.json'
        if not path.exists():
            # A prior reservation always creates the durable marker first.
            if (self.root/'initialized').exists(): raise CorruptStorage('startup ledger missing; network blocked')
            return dict(version=1,last_clock=0.,next_request=0.,areas=[],checks={},latest={},disabled=False)
        value=self._read(path)
        try:
            if set(value)!={'version','last_clock','next_request','areas','checks','latest','disabled'} or \
                    type(value['version']) is not int or value['version']!=1 or type(value['disabled']) is not bool: raise ValueError()
            for k in ('last_clock','next_request'):
                if type(value[k]) not in (int,float) or not math.isfinite(value[k]) or value[k]<0: raise ValueError()
            if not isinstance(value['areas'],list) or len(value['areas'])>256 or \
                    not isinstance(value['checks'],dict) or len(value['checks'])>64 or \
                    not isinstance(value['latest'],dict) or len(value['latest'])>32: raise ValueError()
            for row in value['areas']:
                if set(row)!={'bounds','until'} or osm._area(row['bounds'])!=tuple(row['bounds']) or \
                        type(row['until']) not in (int,float) or not math.isfinite(row['until']) or row['until']<0: raise ValueError()
            for key,row in value['checks'].items():
                if not re.fullmatch('[0-9a-f]{64}',key) or not isinstance(row,dict) or \
                        set(row)-{'attempted','checked','replication_timestamp','result'} or \
                        type(row.get('attempted')) not in (int,float) or not math.isfinite(row['attempted']) or row['attempted']<0:
                    raise ValueError()
                if 'checked' in row and (type(row['checked']) not in (int,float) or not math.isfinite(row['checked']) or row['checked']<0): raise ValueError()
                if 'replication_timestamp' in row: osm._time(row['replication_timestamp'])
                if 'result' in row and (not isinstance(row['result'],str) or len(row['result'])>64): raise ValueError()
            for host,stamp in value['latest'].items():
                if not isinstance(host,str) or not 0<len(host)<=253: raise ValueError()
                osm._time(stamp)
        except (ValueError,TypeError,KeyError): raise CorruptStorage('invalid startup ledger; network blocked') from None
        return value

    def save(self,value):
        # Marker first: a crash before initial reservation cannot reset request history.
        if not (self.root/'initialized').exists(): self._atomic_write(self.root/'initialized',b'1\n')
        self._atomic_write(self.root/'ledger.json',self._document(value))


class Requests(osm.Coordinator):
    def __init__(self,state,ledger,*,wall=time.time,clock=time.monotonic):
        super().__init__(clock)
        self.state,self.ledger,self.wall=state,ledger,wall
        self.latest=ledger['latest']

    @contextmanager
    def request(self,host,bounds,config):
        now=self.wall(); ledger=self.ledger
        if now<ledger['last_clock'] or now<ledger['next_request']:
            raise osm.Throttled('persistent request spacing/clock regression',max(ledger['last_clock'],ledger['next_request'])-now)
        ledger['areas']=[r for r in ledger['areas'] if r['until']>now]
        if any(_intersects(r['bounds'],bounds) for r in ledger['areas']):
            raise osm.Throttled('persistent overlapping-area cooldown')
        if len(ledger['areas'])>=256 or host not in self.latest and len(self.latest)>=32:
            raise ResourceLimit('startup request ledger limit reached')
        ledger['last_clock']=now
        ledger['next_request']=now+config.min_spacing_s
        ledger['areas'].append(dict(bounds=list(bounds),until=now+config.area_cooldown_s))
        self.state.save(ledger)  # durable permission reservation BEFORE any HTTP
        self.next_request=self.clock()+config.min_spacing_s
        try: yield
        finally:
            ledger['next_request']=max(ledger['next_request'],self.wall()+max(0.,self.next_request-self.clock()))
            self.state.save(ledger)  # persist Retry-After even on HTTP failure


def retrieval_config(environ=os.environ):
    raw=environ.get('BNGE_OSM_HEADERS','{}')
    if len(raw)>8192: raise osm.ConfigurationError('credential headers exceed limit')
    spacing=float(environ.get('BNGE_OSM_SPACING_SECONDS','60'))
    cooldown=float(environ.get('BNGE_OSM_COOLDOWN_SECONDS','600'))
    if spacing<60 or cooldown<600: raise osm.ConfigurationError('startup request limits cannot be lowered')
    endpoint=environ.get('BNGE_OSM_ENDPOINT')
    if not endpoint: raise osm.ConfigurationError('an explicit permitted HTTPS endpoint is required')
    return osm.Config(enabled=True,endpoint=endpoint,headers=_json(raw),min_spacing_s=spacing,area_cooldown_s=cooldown)


def fresh_connection(server,message):
    """A new companion cannot reuse another process's already loaded world."""
    sessions=message.get('sessions',[])
    if sessions=={}: sessions=[]  # Lua's empty-array representation
    return isinstance(sessions,list) and len(sessions)<=64 and all(s==server.sid for s in sessions) and \
        message.get('slotSession') in (None,'',server.sid)


def context(md,bounds,snapshot,incoming=(),parts=()):
    """Warm complete bounded baseline context; the Stage 5 validator stays read-only."""
    interest=_union([bounds]+[bo._bbox(f.geometry) for f in tuple(incoming)+tuple(parts)])
    scope=interest; loaded={}; forced=set()
    for _ in range(16):
        before=scope
        for e in snapshot.entries:
            if _intersects(e.bounds,scope):
                forced.add(e.feature.feature_id); scope=_union([scope,e.bounds])
        w,s,e,n=scope
        keys=mapcells.cells_for_bbox(s,w,n,e,max_cells=64)
        for key in keys:
            if key not in loaded: loaded[key]=md.cell(key,layers=('buildings','building_parts'))
        parents=set(forced)
        for cell in loaded.values():
            for f in bo.from_mapgeom(cell.layers['buildings']):
                box=bo._bbox(f.geometry)
                if f.feature_id in forced or box is None or _intersects(box,interest):
                    parents.add(f.feature_id); scope=_union([scope,box])
        for cell in loaded.values():
            for f in bo.from_mapgeom(cell.layers['building_parts']):
                if f.parent_id in parents: scope=_union([scope,bo._bbox(f.geometry)])
        if scope==before: return buildingbaseline.collect(md,bounds,snapshot,incoming,parts)
    raise ResourceLimit('startup baseline closure did not converge')


def _semantic(buildings,parts):
    return {f.feature_id:(bo.geometry_digest(f.geometry),f.attrs,f.parent_id,f.status)
            for f in tuple(buildings)+tuple(parts)}


def _validated_snapshot(md,snapshot):
    # Fail before streaming if saved baseline/parts cannot be proved compatible.
    view=buildingrender.BuildingView(None,md.release,snapshot=snapshot)
    for entry in snapshot.entries if view.active else ():
        # Each persisted entry already carries its entire original/part influence.
        # Do not scan all city entries for every entry during the startup barrier.
        context(md,entry.bounds,replace(snapshot,entries=(entry,)))
        view._validate(md,entry,'demand')
    return deepcopy(snapshot)


def prepare(md,cache_dir,config,*,client=None,store=None,wall=time.time,clock=time.monotonic,cancel=lambda:False):
    """Synchronous startup barrier. No listener/workers exist while this runs."""
    store=store or OverlayStore(os.environ.get('BNGE_BUILDING_OVERLAY_DIR') or Path(cache_dir)/'building-overlays')
    state=State(cache_dir)
    report=dict(mode='startup',areas=[],checked=False,edit_observed=False,accepted=False)
    with state._locked():
        snapshot,token=store.load_state()
        ledger=None
        try:
            ledger=state.read()
            if not config.refresh_on_startup: reason='cached'
            elif ledger['disabled'] or snapshot.revision and not snapshot.enabled: reason='disabled'
            elif md.fetcher.offline_flag: reason='offline'
            else: reason=None
            if reason:
                report['areas']=[dict(name=n,status=reason) for n,b in config.areas]
            else:
                if client is None: client=osm.Client(retrieval_config(),fetcher=md.fetcher,wall=wall)
                if client.config.min_spacing_s<60 or client.config.area_cooldown_s<600:
                    raise osm.ConfigurationError('startup request limits cannot be lowered')
                client.coordinator=Requests(state,ledger,wall=wall,clock=clock)
                candidates=[]
                for name,bounds in config.areas:
                    key=hashlib.sha256(bo._json([name,bounds,client.config.endpoint]).encode()).hexdigest()
                    candidates.append((ledger['checks'].get(key,{}).get('attempted',0),name,bounds,key))
                attempted=False
                for _,name,bounds,key in sorted(candidates):
                    row=dict(name=name,status='deferred_one_area_per_startup'); report['areas'].append(row)
                    if attempted: continue
                    if cancel(): raise InterruptedError('startup refresh cancelled')
                    if key not in ledger['checks'] and len(ledger['checks'])>=64: raise ResourceLimit('startup check history full')
                    # Preflight reservations before warming potentially expensive baseline context.
                    now=wall()
                    if now<max(ledger['last_clock'],ledger['next_request']) or any(
                            r['until']>now and _intersects(r['bounds'],bounds) for r in ledger['areas']):
                        row['status']='cooldown'; continue
                    attempted=True
                    check=ledger['checks'].setdefault(key,{})
                    check['attempted']=now
                    state.save(ledger)
                    try:
                        initial=context(md,bounds,snapshot)
                        records=initial.buildings+initial.parts+tuple(f for e in snapshot.entries
                            if _intersects(e.bounds,initial.bounds) for f in (e.feature,)+e.parts)
                        known=sorted({i for f in records for i in bo._identities(f)})
                        stamps=[r['info']['source_timestamp'] for r in snapshot.refreshes if _intersects(r['info']['bounds'],bounds)]
                        if check.get('replication_timestamp'): stamps.append(check['replication_timestamp'])
                        source=client.fetch(bounds,manual=True,known_ids=known,not_before=max(stamps,key=osm._time) if stamps else None)
                        source.refresh_info(md.release)  # fresh timestamp required even for empty data
                        if source.issues:
                            row['issues']=[i.code for i in source.issues[:64]]
                            raise osm.IncompleteResponse('OSM issues require manual review')
                        if cancel(): raise InterruptedError('startup refresh cancelled')
                        initial.validate(md)
                        full=context(md,bounds,snapshot,source.buildings,source.parts)
                        effective=store.compose(replace(snapshot,enabled=True),full.buildings,baseline_parts=full.parts,
                            baseline_release=md.release,bounds=full.bounds)
                        merged=bo.merge(effective.buildings,source.buildings,baseline_parts=effective.parts,incoming_parts=source.parts)
                        counts=merged.report()['counts']; row['counts']=counts
                        if counts['conflict']:
                            row['conflicts']=[d.reason for d in merged.decisions if d.action=='conflict'][:64]
                            raise StalePreview('conservative merge conflicts require manual review')
                        changed=_semantic(merged.buildings,merged.parts)!=_semantic(effective.buildings,effective.parts)
                        def validate():
                            if cancel(): raise InterruptedError('startup refresh cancelled')
                            full.validate(md)
                            age=wall()-osm._time(source.replication_timestamp).timestamp()
                            if age < -300 or age > client.config.max_snapshot_age_s:
                                raise osm.StaleSnapshot('OSM snapshot expired during startup validation')
                        if changed:
                            info=replace(source.refresh_info(md.release,metadata={'startup_area':name}),bounds=full.bounds)
                            commit=store.refresh(full.buildings,source.buildings,baseline_parts=full.parts,incoming_parts=source.parts,
                                info=info,enable=True,expected_state=token,validate=validate,
                                expected_merge=(merged.digest(),merged.report_json()))
                            row.update(status='accepted',revision=commit.revision); report['accepted']=True
                        else:
                            validate()
                            if store.load_state()[1]!=token: raise StalePreview('saved state changed during startup check')
                            row['status']='checked_unchanged'
                        check.update(checked=wall(),replication_timestamp=source.replication_timestamp,result=row['status'])
                        row['replication_timestamp']=source.replication_timestamp
                        report['checked']=True
                    except (OSError,ValueError,RuntimeError) as error:
                        row.update(status='retained_previous',error=type(error).__name__)
                        check['result']=type(error).__name__[:64]
                    state.save(ledger)
        except (OSError,ValueError,RuntimeError) as error:
            report.update(error=type(error).__name__,freshness='not_confirmed')
        snapshot=store.load()
        # An explicit disable of an empty store must also survive its first restart.
        if ledger is not None and ledger['disabled']: snapshot=replace(snapshot,enabled=False)
        frozen=_validated_snapshot(md,snapshot)
        all_checked=len(report['areas'])==len(config.areas) and all(
            row['status'] in ('accepted','checked_unchanged') for row in report['areas'])
        report.update(revision=frozen.revision,enabled=frozen.enabled,unresolved_saved_conflicts=len(frozen.conflicts),
            all_areas_checked=all_checked,
            freshness=('checked_endpoint_snapshot' if all_checked else 'partially_checked_endpoint_snapshot') if report['checked'] else 'not_confirmed',
            note='Replication time does not prove that a particular edit was included.')
        state._atomic_write(state.root/'report.json',state._document(report))
    log.info('OSM startup: %s',json.dumps(report,sort_keys=True))
    return frozen,report


def control(cache_dir,action,*,store=None):
    if action not in ('disable','enable','rollback'): raise ValueError('unsupported offline overlay action')
    state=State(cache_dir)
    with state._locked():
        ledger=state.read()
        store=store or OverlayStore(os.environ.get('BNGE_BUILDING_OVERLAY_DIR') or Path(cache_dir)/'building-overlays')
        if action=='disable':
            ledger['disabled']=True; state.save(ledger)
            store.disable()
        elif action=='enable':
            store.enable()
            ledger['disabled']=False; state.save(ledger)
        else: store.rollback()
        snapshot=store.load()
        return dict(revision=snapshot.revision,enabled=snapshot.enabled,effective='next full session')


def main():
    import argparse
    parser=argparse.ArgumentParser(description='Offline saved overlay control; close game and companion first.')
    parser.add_argument('--cache-dir',required=True)
    parser.add_argument('--action',choices=('disable','enable','rollback'),required=True)
    args=parser.parse_args()
    path=os.environ.get('BNGE_OSM_STARTUP_CONFIG')
    if not path: parser.error('BNGE_OSM_STARTUP_CONFIG authorization required')
    Config.read(path)
    print(json.dumps(control(args.cache_dir,args.action)))


if __name__=='__main__': main()
