"""Explicit Layercake preparation, immutable cached sessions.

Uses the existing pure merge, baseline validator, atomic filesystem primitives,
render adapter and normal generators. Separate storage never edits legacy overlays.
Layercake absence is NOT a verified deletion. Positive identity-state proofs are
represented separately so an eventual history adapter cannot accidentally use absence.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import json
import logging
import math
import os
from pathlib import Path
import re
import time

import buildingoverlay as bo
import buildingoverlaystore as storage
import buildingstartup
import buildingrender
import layercakebuildings as lc
import mapcells
import osmbuildings as osm

log=logging.getLogger('earthd.osm_startup')

# Regional snapshots, not individual retrieval jobs. Request/geometry/context limits
# remain small. This separate bound permits a city while capping every worker's
# copied session snapshot and the retained immutable history. Legacy stores retain
# their original limits; no automatic history deletion is performed.
REGIONAL_LIMITS=storage.Limits(max_entries=50000,max_file_bytes=96*1024*1024,
                             max_total_bytes=512*1024*1024)


@dataclass(frozen=True)
class Removal:
    identity: str
    version: int
    timestamp: str
    state: str
    authority: str

    def __post_init__(self):
        if bo.normalize_osm_id(self.identity)!=self.identity or type(self.version) is not int or self.version<=0 or \
                self.state not in ('deleted','retagged') or not isinstance(self.authority,str) or \
                not self.authority.startswith('https://') or len(self.authority)>512:
            raise ValueError('invalid positive identity-state evidence')
        osm._time(self.timestamp)


class Store:
    """Bounded immutable objects plus atomic head; reuse Stage 2 locks/checksums/fsync.

    Custom schema is isolated from building-overlays/v1. No legacy object rewrite,
    evidence cleanup or automatic history eviction. Capacity exhaustion fails closed.
    """
    # Reuse filesystem primitives without exposing the incompatible legacy
    # refresh/rollback/recover methods on this independent manifest schema.
    _no_link=staticmethod(storage.OverlayStore._no_link)
    _safe=storage.OverlayStore._safe
    _locked=storage.OverlayStore._locked
    _document=storage.OverlayStore._document
    _atomic_write=storage.OverlayStore._atomic_write
    _read=storage.OverlayStore._read

    def __init__(self, cache_dir):
        self.directory=Path(cache_dir).absolute()
        self.root=self.directory/'osm-primary'/'v1'
        self.objects=self.root/'objects'
        self.limits=REGIONAL_LIMITS
        self.lock_timeout=1.
        if '..' in self.root.parts: raise ValueError('unsafe cache path')
        for p in (self.root,*self.root.parents): self._no_link(p)
        self.objects.mkdir(parents=True,exist_ok=True)
        with self._locked():
            if not (self.root/'state.json').exists() and not (self.root/'previous.json').exists() and not any(self.objects.iterdir()):
                self._atomic_write(self.root/'state.json',self._document({'head':None}))

    @contextmanager
    def session(self):
        # The same OS lease excludes manual refresh throughout a companion session.
        lease=object.__new__(Store)
        lease.directory=self.directory; lease.root=self.root/'session'; lease.lock_timeout=1.
        self._no_link(lease.root); lease.root.mkdir(exist_ok=True)
        with lease._locked(): yield

    def load(self):
        with self._locked(): return self._load()

    def _load(self):
        path=self.root/'state.json'
        if not path.exists():
            if (self.root/'previous.json').exists() or any(self.objects.iterdir()):
                raise storage.CorruptStorage('missing primary manifest; explicit recovery required')
            return storage.Snapshot(None,False,None,(),(),(),(),None)
        manifest=self._read(path)
        if set(manifest)=={'head'} and manifest['head'] is None:
            return storage.Snapshot(None,False,None,(),(),(),(),None)
        if set(manifest)!={'head'} or not isinstance(manifest['head'],str) or not re.fullmatch('[0-9a-f]{64}',manifest['head']):
            raise storage.CorruptStorage('invalid primary manifest')
        return self._decode(manifest['head'])

    def _decode(self, revision):
        payload=self._read(self.objects/(revision+'.json'))
        if storage._hash(payload)!=revision: raise storage.CorruptStorage('primary content hash mismatch')
        try:
            if set(payload)!={'baseline_release','entries','source','coverage','report'} or not isinstance(payload['entries'],list) or \
                    not isinstance(payload['baseline_release'],str) or len(payload['entries'])>self.limits.max_entries:
                raise ValueError('invalid primary payload')
            validate_version(payload['source'])
            if not isinstance(payload['coverage'],list) or len(payload['coverage'])>64: raise ValueError('coverage limit')
            for b in payload['coverage']: storage._bounds(b)
            entries=[]; ids=set()
            for row in payload['entries']:
                if set(row)!={'feature','parts','baseline_digest','bounds','source_timestamp'}: raise ValueError('bad entry schema')
                f=storage._feature(row['feature']); parts=tuple(storage._feature(p) for p in row['parts'])
                digest=row['baseline_digest']
                if digest is not None and not re.fullmatch('[0-9a-f]{64}',digest): raise ValueError('invalid baseline digest')
                if f.feature_id in ids or f.parent_id is not None or bo._validated(f.geometry)[1]: raise ValueError('invalid building')
                ids.add(f.feature_id)
                if f.status=='deleted':
                    proof=Removal(**f.attrs['_osm_suppression'])
                    if proof.identity not in bo._identities(f): raise ValueError('suppression identity mismatch')
                    if parts: raise ValueError('suppression cannot insert parts')
                elif f.status!='building': raise ValueError('unsupported persisted status')
                for p in parts:
                    ring,error=bo._validated(p.geometry)
                    if error or p.parent_id!=f.feature_id or p.feature_id in ids or p.status!='building' or \
                            not bo._contained(ring,bo._validated(f.geometry)[0]): raise ValueError('invalid parts')
                    ids.add(p.feature_id)
                if bool(f.attrs.get('has_parts'))!=bool(parts): raise ValueError('incomplete part set')
                bounds=storage._bounds(row['bounds'])
                if not contains(bounds,storage._union([bo._bbox(q.geometry) for q in (f,)+parts])): raise ValueError('bad influence bounds')
                osm._time(row['source_timestamp'])
                entries.append(storage.Entry(f,parts,digest,bounds,row['source_timestamp']))
            if len(ids)>self.limits.max_entries: raise ValueError('primary entry limit')
            return storage.Snapshot(revision,True,payload['baseline_release'],tuple(entries),(),
                                    ({'source':payload['source'],'coverage':payload['coverage'],'report':payload['report']},),(),
                                    storage._union([e.bounds for e in entries]))
        except (ValueError,TypeError,KeyError) as error:
            raise storage.CorruptStorage('invalid primary snapshot') from error

    def publish(self, snapshot, source, coverage, report, validate):
        validate_version(source)
        payload=dict(baseline_release=snapshot.baseline_release,entries=[storage._entry_wire(e) for e in snapshot.entries],
                     source=source,coverage=coverage,report=report)
        revision=storage._hash(payload)
        if self._load().revision==revision: return self._load()
        document=self._document(payload)
        # Include existing orphans and reserve metadata before writing a new object.
        files=[]
        for p in self.objects.iterdir():
            self._no_link(p)
            files.append(p)
            if len(files)>self.limits.max_revisions: raise storage.ResourceLimit('primary object count limit')
        if len(files)>=self.limits.max_revisions or sum(p.stat().st_size for p in files)+len(document)+16384>self.limits.max_total_bytes:
            raise storage.ResourceLimit('primary history capacity; previous data retained')
        target=self.objects/(revision+'.json')
        self._atomic_write(target,document)
        self._decode(revision)  # validate the candidate BEFORE publishing its head
        validate()
        manifest=self.root/'state.json'
        if manifest.exists(): self._atomic_write(self.root/'previous.json',self._document(self._read(manifest)))
        self._atomic_write(manifest,self._document({'head':revision}))
        return self._load()


def validate_version(version):
    if not isinstance(version,dict) or set(version)!={'etag','size','last_modified','url'} or version['url']!=lc.URL or \
            not isinstance(version['etag'],str) or not re.fullmatch(r'"[!#-~]{1,200}"',version['etag']) or \
            type(version['size']) is not int or not 16<version['size']<1024**4:
        raise ValueError('invalid source version')
    osm._time(version['last_modified'])


def contains(a,b):
    return b is not None and a[0]<=b[0] and a[1]<=b[1] and a[2]>=b[2] and a[3]>=b[3]


def prepare(md, before, source, *, limits=None):
    """Pure preparation over validated complete context; no network or publication."""
    validate_version(source.version)
    if source.complete is not True or source.issues:
        raise ValueError('incomplete/unsupported source partition; previous data retained: '+str(source.issues[:16]))
    if before.refreshes and osm._time(source.version['last_modified'])<osm._time(before.refreshes[0]['source']['last_modified']):
        raise ValueError('source publication regressed')
    full=buildingstartup.context(md,source.bounds,before,source.buildings,source.parts)
    full.validate(md)
    entries={e.feature.feature_id:e for e in before.entries}
    base={f.feature_id:f for f in full.buildings}
    removed={e.feature.feature_id for e in before.entries if e.feature.status=='deleted'}
    for f in source.buildings:
        for entry in before.entries:
            if not set(bo._identities(f)) & set(bo._identities(entry.feature)): continue
            versions=[int(s['record_id'].rsplit('@',1)[1]) for s in f.sources
                      if str(s.get('record_id','')).rsplit('@',1)[-1].isdigit()]
            prior=[int(s['record_id'].rsplit('@',1)[1]) for s in entry.feature.sources
                   if str(s.get('record_id','')).rsplit('@',1)[-1].isdigit()]
            if entry.feature.status=='deleted':
                prior.append(entry.feature.attrs['_osm_suppression']['version'])
                if not versions or max(versions)<=max(prior): raise ValueError('stale source cannot undo a suppression')
                removed.discard(entry.feature.feature_id)
            elif versions and prior and max(versions)<max(prior): raise ValueError('OSM element version regressed')
    for proof in source.removals:
        if not isinstance(proof,Removal): raise ValueError('positive removal evidence required')
        matches={f.feature_id:f for f in full.buildings if proof.identity in bo._identities(f)}
        for entry in before.entries:
            if proof.identity in bo._identities(entry.feature):
                # A previously accepted spatial association is evidence too;
                # unrelated Overture-only buildings have no such association.
                matches[entry.feature.feature_id]=base.get(entry.feature.feature_id,entry.feature)
        if len(matches)!=1: raise ValueError('removal identity is absent or ambiguous')
        original=next(iter(matches.values()))
        if bo._identities(original) and set(bo._identities(original))!={proof.identity}:
            raise ValueError('multiple geometry identities cannot justify suppression')
        record_versions=[int(s['record_id'].rsplit('@',1)[1]) for s in original.sources
                         if bo.normalize_osm_id(s.get('record_id'))==proof.identity and str(s.get('record_id')).rsplit('@',1)[-1].isdigit()]
        prior_entry=entries.get(original.feature_id)
        if prior_entry:
            record_versions.extend(int(s['record_id'].rsplit('@',1)[1]) for s in prior_entry.feature.sources
                if bo.normalize_osm_id(s.get('record_id'))==proof.identity and str(s.get('record_id')).rsplit('@',1)[-1].isdigit())
            if prior_entry.feature.status=='deleted': record_versions.append(prior_entry.feature.attrs['_osm_suppression']['version'])
        update_times=[osm._time(s['update_time']) for f in (original,prior_entry.feature if prior_entry else original)
                      for s in f.sources if s.get('update_time')]
        if osm._time(proof.timestamp)>osm._time(source.version['last_modified']) or \
                update_times and osm._time(proof.timestamp)<max(update_times):
            raise ValueError('removal timestamp contradicts the accepted source state')
        if not record_versions or proof.version<=max(record_versions) or not contains(source.bounds,bo._bbox(original.geometry)):
            raise ValueError('removal evidence older than original or lacks complete geographic coverage')
        if any(proof.identity in bo._identities(f) for f in source.buildings): raise ValueError('contradictory removal')
        parts=tuple(p for p in full.parts if p.parent_id==original.feature_id)
        digest=storage._fingerprint(original,parts) if original.feature_id in base else None
        suppressed=replace(original,status='deleted',parts_complete=True,
                           attrs=dict(original.attrs,has_parts=False,_osm_suppression=asdict(proof)))
        entries[original.feature_id]=storage.Entry(suppressed,(),digest,
            storage._union([bo._bbox(q.geometry) for q in (original,)+parts]),proof.timestamp)
        removed.add(original.feature_id)
    result=bo.merge(tuple(f for f in full.buildings if f.feature_id not in removed),source.buildings,
                    baseline_parts=tuple(p for p in full.parts if p.parent_id not in removed),incoming_parts=source.parts)
    if result.report()['counts']['conflict']:
        reasons=[(d.incoming_id,d.reason) for d in result.decisions if d.action=='conflict'][:16]
        raise ValueError('conservative building merge conflicts; previous data retained: '+str(reasons))
    final={f.feature_id:f for f in result.buildings}
    incoming={f.feature_id:f for f in source.buildings}
    for decision in result.decisions:
        if decision.incoming_id is None: continue
        fid=decision.baseline_ids[0] if decision.baseline_ids else bo._identities(incoming[decision.incoming_id])[0]
        f=final[fid]
        # Stage 1 proves equivalence conservatively; OSM owns the accepted geometry.
        if decision.reason=='spatial_duplicate': f=replace(f,geometry=deepcopy(incoming[decision.incoming_id].geometry))
        parts=tuple(p for p in result.parts if p.parent_id==fid)
        old=base.get(fid); originals=tuple(p for p in full.parts if p.parent_id==fid)
        bounds=storage._union([bo._bbox(q.geometry) for q in (f,)+parts+originals]+[bo._bbox(old.geometry) if old else None,
                              entries[fid].bounds if fid in entries else None])
        entries[fid]=storage.Entry(f,parts,storage._fingerprint(old,originals) if old else None,bounds,source.version['last_modified'])
    if len(entries)+sum(len(e.parts) for e in entries.values())>(limits or REGIONAL_LIMITS).max_entries:
        raise storage.ResourceLimit('regional entry limit')
    known={i for f in full.buildings if storage._intersects(bo._bbox(f.geometry),source.bounds)
            for i in bo._identities(f)} | {i for e in before.entries
            if storage._intersects(e.bounds,source.bounds) for i in bo._identities(e.feature)}
    returned={i for f in source.buildings for i in bo._identities(f)} | {p.identity for p in source.removals}
    report=dict(mode='layercake_prepared',accepted=True,source_version=source.version,
                counts=result.report()['counts'],suppressed=len(removed),
                missing_unverified=sorted(known-returned),
                partitions=[str(k) for k in mapcells.cells_for_bbox(source.bounds[1],source.bounds[0],
                            source.bounds[3],source.bounds[2],max_cells=64)],
                note='Missing identities are not verified deletions. Dataset publication time is not OSM replication time.')
    snapshot=replace(before,enabled=True,baseline_release=md.release,entries=tuple(entries[k] for k in sorted(entries)),
                     affected_bounds=storage._union([e.bounds for e in entries.values()]))
    return snapshot,report,full


def refresh(md, cache_dir, *, reader=None, bounds=None, wall=time.time):
    """One explicit developer request; no game operations and no startup caller."""
    if bounds is None: raise ValueError('an explicit geographical area is required')
    store=Store(cache_dir)
    with store.session(), store._locked():
        before=store._load()
        if before.revision and before.baseline_release!=md.release: raise storage.BaselineMismatch('primary baseline changed')
        ledger_path=store.root/'requests.json'
        if not ledger_path.exists() and (store.root/'requests-initialized').exists():
            raise storage.CorruptStorage('missing request ledger; refresh blocked')
        ledger=store._read(ledger_path) if ledger_path.exists() else {'next':0.,'clock':0.}
        if set(ledger)!={'next','clock'} or any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in ledger.values()):
            raise storage.CorruptStorage('invalid primary request ledger')
        now=wall()
        if now<max(ledger['next'],ledger['clock']): raise osm.Throttled('persistent manual refresh cooldown')
        store._atomic_write(store.root/'requests-initialized',b'1\n')
        store._atomic_write(ledger_path,store._document({'clock':now,'next':now+600}))
        own=reader is None
        reader=reader or lc.Reader(Path(cache_dir)/'mapdata/raw',md.fetcher)
        try:
            reader.select(explicit=True)
            source=reader.query(bounds)
            snapshot,report,full=prepare(md,before,source)
            # A changed-in-place file must not publish a mixed ETag response.
            coverage=list(before.refreshes[0]['coverage']) if before.refreshes else []
            coverage=sorted({tuple(b) for b in coverage+[list(source.bounds)]})
            if len(coverage)>64: raise storage.ResourceLimit('bounded pilot coverage limit')
            saved=store.publish(snapshot,source.version,[list(b) for b in coverage],report,lambda:full.validate(md))
            return saved,report
        except osm.HTTPFailure as error:
            wait=osm._retry_after(error.headers.get('retry-after'),wall()) if error.code in (429,503) else 0
            if wait: store._atomic_write(ledger_path,store._document({'clock':now,'next':max(now+600,wall()+wait)}))
            raise
        finally:
            if own: reader.close()


def cached(md,cache_dir):
    """No source reader or HTTP; fail before streaming if the saved baseline changed."""
    snapshot=Store(cache_dir).load()
    snapshot=buildingstartup._validated_snapshot(md,snapshot)
    report=dict(mode='layercake_cached',revision=snapshot.revision,enabled=snapshot.enabled,checked=False,
                note='Saved validated buildings reused; no freshness request. Updates apply only in a fresh session.')
    log.info('OSM startup: %s',json.dumps(report,sort_keys=True))
    return snapshot,report


def main():
    import argparse
    import mapdata
    import netfetch
    from buildingregions import main as regional_main
    regional_main()


if __name__=='__main__': main()
