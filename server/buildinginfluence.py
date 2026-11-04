"""Bounded render influence and indivisible LOD components; no I/O or game actions.

Source rectangles are complete baseline/accepted parent+part contexts, not the
dependency cells of already expanded rendering targets. Legacy cell fences remain
conservative until their ownership can be proved by the existing recovery path.
"""
from dataclasses import dataclass
import hashlib
import json
import re

import numpy as np

from buildingoverlaystore import _bounds, _intersects, _union, ResourceLimit, CorruptStorage
import mapcells

MAX_REGIONS = 512
MAX_TARGETS = 512  # inspection only, reuses the generation-capacity bound


def render_bounds(frame, box, kind=None):
    """Conservative geographic read footprint, including genuine district reads.

    The small metric guard includes tangent-frame/centroid and boundary roundoff.
    Loading cells may be much larger; those are deliberately not render influence.
    """
    import buildings
    import surface
    x0, y0, x1, y1 = box
    margin = 5.0
    la, lo, _ = frame.enu_to_geodetic((x0+x1)/2, (y0+y1)/2, 0.)
    if kind not in ('skyline', 'skyline_geo', 'bldsky') and bool(surface.dry_west(float(la), float(lo))):
        margin += buildings.DISTRICT_M
    la, lo, _ = frame.enu_to_geodetic(np.array([x0-margin,x1+margin,x0-margin,x1+margin]),
                                     np.array([y0-margin,y0-margin,y1+margin,y1+margin]), 0.)
    return _bounds((float(lo.min()),float(la.min()),float(lo.max()),float(la.max())))


@dataclass(frozen=True)
class Coverage:
    regions: tuple = ()
    legacy_cells: tuple = ()

    def __post_init__(self):
        object.__setattr__(self,'regions',tuple(sorted(set(_bounds(r) for r in self.regions))))
        cells=tuple(sorted(set(self.legacy_cells)))
        if len(self.regions)>MAX_REGIONS or len(cells)>512:
            raise ResourceLimit('building influence coverage exceeds budget')
        for key in cells:
            if not isinstance(key,str) or not re.fullmatch(r'14/\d+/\d+',key) or any(
                    not 0<=int(v)<2**14 for v in key.split('/')[1:]):
                raise ValueError('invalid legacy influence cell')
        cells=tuple(sorted({str(mapcells.CellKey(*map(int,k.split('/')))) for k in cells}))
        object.__setattr__(self,'legacy_cells',cells)
        self.cells()  # validate aggregate cell/area bounds before using coverage

    def cells(self):
        cells=set(self.legacy_cells)
        for w,s,e,n in self.regions:
            try: cells.update(map(str,mapcells.cells_for_bbox(s,w,n,e,max_cells=512)))
            except ValueError as exc: raise ResourceLimit('building influence area exceeds cell budget') from exc
            if len(cells)>512: raise ResourceLimit('building influence area exceeds cell budget')
        return cells

    def wire(self):
        return dict(version=1,regions=[list(r) for r in self.regions],legacy_cells=list(self.legacy_cells))

    @classmethod
    def read(cls,value):
        try:
            if not isinstance(value,dict) or set(value)!={'version','regions','legacy_cells'} or \
                    type(value['version']) is not int or value['version']!=1 or \
                    not isinstance(value['regions'],list) or not isinstance(value['legacy_cells'],list) or \
                    len(value['regions'])>MAX_REGIONS or len(value['legacy_cells'])>512:
                raise ValueError('invalid influence document')
            return cls(tuple(value['regions']),tuple(value['legacy_cells']))
        except (ValueError,TypeError,KeyError,ResourceLimit) as exc:
            raise CorruptStorage('invalid bounded building influence coverage') from exc

    def union(self,other):
        return Coverage(self.regions+other.regions,self.legacy_cells+other.legacy_cells)

    def with_snapshot(self,snapshot):
        regions=[e.bounds for e in snapshot.entries] or [r['info']['bounds'] for r in snapshot.refreshes]
        changed=snapshot.affected_bounds
        if changed is not None and (not regions or _union(regions)!=tuple(changed)):
            previous=list(self.regions)+[mapcells.CellKey(*map(int,k.split('/'))).bounds() for k in self.legacy_cells]
            clipped=[(max(r[0],changed[0]),max(r[1],changed[1]),min(r[2],changed[2]),min(r[3],changed[3]))
                     for r in previous if _intersects(r,changed)]
            regions.extend([r for r in clipped if r[0]<r[2] and r[1]<r[3]] or [changed])
        return self.union(Coverage(tuple(regions)))

    def affects(self,request):
        if self.regions:
            bounds=render_bounds(*request.geometry(),request.kind)
            if any(_intersects(bounds,r) for r in self.regions): return True
        if self.legacy_cells:
            import blockworker
            return bool(set(self.legacy_cells).intersection(map(str,blockworker.building_cells(*request.geometry()))))
        return False


def priority(request,conn):
    tracker=getattr(conn,'roads',None)
    return (0 if request.kind.startswith('near') and (request.x,request.y) in getattr(tracker,'car',()) else
            1 if request.kind.startswith('near') else 2 if request.kind.startswith('far') else 3,request.slot)


def group(request):
    import geotiles
    import buildings
    if request.kind.endswith('_geo'):
        z=geotiles.SKY_Z if request.kind=='skyline_geo' else geotiles.Z
        if geotiles.FAR_Z!=geotiles.Z: return ('unaligned',)
        d=2**(z-geotiles.SKY_Z)
        return ('geo',request.x//d,request.y//d)
    u=1 if request.kind=='skyline' else buildings.SKY_UNIT
    return ('legacy',request.aid,request.lat,request.lon,request.x//u,request.y//u)


def group_bounds(key):
    import geotiles
    if key[0]=='geo':
        tile=geotiles.geobox(key[1],key[2],geotiles.SKY_Z)
        return render_bounds(tile.frame,tile.box,'skyline_geo')
    import geodesy
    import roads
    import buildings
    _,aid,lat,lon,x,y=key
    u=buildings.SKY_UNIT
    x0,y0,_,_=roads.tile_bbox(x*u,y*u)
    _,_,x1,y1=roads.tile_bbox(x*u+u-1,y*u+u-1)
    return render_bounds(geodesy.EnuFrame(lat,lon),(x0,y0,x1,y1),'skyline')


class Plan:
    def __init__(self,requests,coverage):
        self.requests=tuple(requests)
        self.coverage=coverage
        if len(self.requests)>MAX_TARGETS: raise ResourceLimit('building influence inspection exceeds generation capacity')
        groups={group(r) for r in self.requests}
        roots={g:g for g in groups}
        def root(g):
            while roots[g]!=g:
                roots[g]=roots[roots[g]]; g=roots[g]
            return g
        def join(keys):
            keys=sorted(keys)
            for key in keys[1:]: roots[root(key)]=root(keys[0])
        # Mixed ownership grids and unknown historical moves cannot be split safely.
        frames={(g[0],)+g[1:4] if g[0]=='legacy' else (g[0],) for g in groups}
        if len(frames)>1 or coverage.legacy_cells or ('unaligned',) in groups:
            join(groups)
        else:
            boxes={g:group_bounds(g) for g in groups}
            for region in coverage.regions:
                join(g for g,b in boxes.items() if _intersects(b,region))
        components={}
        for request in self.requests: components.setdefault(root(group(request)),[]).append(request)
        self.components=tuple(tuple(sorted(v,key=lambda r:r.slot)) for _,v in sorted(components.items()))

    @staticmethod
    def key(component):
        return hashlib.sha256(json.dumps(sorted(set(group(r) for r in component)),separators=(',',':')).encode()).hexdigest()

    def validate(self,selected):
        selected=set(selected)
        all_slots={r.slot for r in self.requests}
        if not selected or not selected<=all_slots: return False
        return all(not selected.intersection(r.slot for r in c) or {r.slot for r in c}<=selected for c in self.components)

    def choose(self,done,conn,queue=(),limit=128,prefer_near=None):
        pending={self.key(c):c for c in self.components if not all(r.slot in done for r in c)}
        waiting=[k for k in queue if k in pending]
        waiting.extend(k for k in sorted(pending,key=lambda k:min(priority(r,conn) for r in pending[k])) if k not in waiting)
        chosen=[]; blocked=0
        order=waiting
        if prefer_near is not None:
            order=sorted(waiting,key=lambda k:bool(any(r.kind.startswith('near') for r in pending[k]))!=prefer_near)
        for key in order:
            c=pending[key]
            ready=all(not r.kind.startswith('near') or any((r.x,r.y) in getattr(getattr(conn,'roads',None),n,())
                      for n in ('sent','empty')) for r in c)
            if len(c)>limit or not ready:
                blocked+=sum(r.slot not in done for r in c); continue
            # Near components can need several collision/LOD slots per target.
            # Do not enlarge those admissions with independent work. Far-only
            # components use <=1 shape per target; pack them to the existing
            # 32-target wire bound, avoiding a transaction per empty skyline tile.
            near=any(r.kind.startswith('near') for r in c)
            if not chosen or not near and not any(r.kind.startswith('near') for r in chosen) and len(chosen)+len(c)<=32:
                chosen.extend(c)
        progress=dict(required=len(self.requests),acknowledged=len(done),pending=len(self.requests)-len(done),
                      blocked=blocked,components=len(self.components),remaining_components=len(pending),
                      oversized=sum(sum(r.slot not in done for r in c) for c in pending.values() if len(c)>limit))
        return tuple(sorted(chosen,key=lambda r:priority(r,conn))),tuple(waiting),progress

    def document(self):
        return dict(version=1,coverage=self.coverage.wire())


def read_cohort(value):
    if not isinstance(value,dict) or set(value)!={'version','coverage'} or type(value['version']) is not int or value['version']!=1:
        raise CorruptStorage('invalid building cohort document')
    return Coverage.read(value['coverage'])


def discover(server,coverage):
    import buildingtargets
    return buildingtargets.discover(server,None,current_only=True,max_targets=MAX_TARGETS,influence=coverage)
