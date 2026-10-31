"""Bounded target discovery from live tracker state and the retained preparation registry.

No world-cache scan, file creation, tracker reset or activation. Settings come from
the same Server methods used by the normal writer dispatch paths.
"""
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

import blockworker
from buildingrefresh import TileRequest, registered_requests
from buildingoverlaystore import ResourceLimit, _bounds
import geotiles
import mapcells


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Discovery:
    requests: tuple
    anchors: tuple
    retired: tuple
    status: str
    relevant: tuple = ()

    def report(self):
        return dict(status=self.status, anchors=list(self.anchors), retired_registered_slots=list(self.retired),
                    count=len(self.requests), relevant_slots=list(self.relevant), targets=[r.wire() for r in self.requests])


def discover(server, bounds, *, max_targets=128, max_keys=16384, max_connections=8, max_cells=512, current_only=False):
    w,s,e,n = _bounds(bounds)
    try:
        area = set(mapcells.cells_for_bbox(s,w,n,e,max_cells=max_cells))
    except ValueError as e:
        raise ResourceLimit("target discovery area exceeds cell budget") from e
    connections = list(islice(iter(server.connections),max_connections+1))
    if len(connections) > max_connections:
        raise ResourceLimit("too many active connections for one manual refresh")
    anchors = {c.active:server.anchors[c.active] for c in connections
               if getattr(c,"active",None) in server.anchors}
    retained = registered_requests(Path(server.cache_dir)/"building-generations",limit=max_targets)
    candidates, retired, relevant, visited = {}, [], set(), 0
    lift = None
    if getattr(server,"near_visual",None) is not None:
        # Import the actual constant; no duplicated numeric rendering defaults.
        import earthd
        lift = earthd.DETAIL_ROAD_LIFT if server.near_visual=="detail" else 0.0

    def semantic(req):
        return (req.kind,req.x,req.y) if req.kind in ("far_geo","skyline_geo") else req.slot

    def current(kind, x, y, anchor=None, previous=None):
        if kind.endswith("_geo"):
            z = geotiles.Z if kind=="near_geo" else blockworker.far_geo_zoom("bldfar" if kind=="far_geo" else "bldsky")
            lh,mph,buried,lamps,country,gzoom = server.geo_rules(x,y,z)
        else:
            lamps = server.lamp_region(anchor)
            if kind=="near":
                lh,mph,buried,country = (server.left_hand(anchor),server.mph(anchor),server.buried(anchor),server.country(anchor))
            gzoom = anchor.zooms[0]
        options = {"lamp_region":lamps}
        if kind in ("near","near_geo"):
            if lift is None:
                raise DiscoveryError("server near rendering mode is unavailable")
            options.update(sidewalks=server.sidewalks,lift=lift,q=server.anchor_q(anchor),left_hand=bool(lh),
                           mph=bool(mph),gas_stations=server.gas_stations,buried=buried,country=country)
        # Distant geotiles have no anchor-dependent geometry. Preserve a retained
        # descriptor's unused frame fields to retain its existing Stage 4B slot.
        if kind in ("far_geo","skyline_geo"):
            return TileRequest(kind,x,y,aid=previous.aid if previous else 0,
                               lat=previous.lat if previous else 0.0,lon=previous.lon if previous else 0.0,
                               zoom=previous.zoom if previous else gzoom,gzoom=gzoom,options=options)
        return TileRequest(kind,x,y,aid=anchor.id,lat=anchor.lat,lon=anchor.lon,
                           zoom=anchor.zooms[0],gzoom=gzoom,options=options)

    def add(req, retained_target=False):
        if current_only:
            from buildingactivation import _live
            if not any(_live(c,req) for c in connections): return
        affected = bool(area.intersection(blockworker.building_cells(*req.geometry())))
        if current_only and not affected: return
        if affected:
            relevant.add(semantic(req))
        if retained_target or affected:
            candidates[semantic(req)] = req
            if len(candidates) > max_targets:
                raise ResourceLimit("automatic generation target budget exceeded; no targets were silently omitted")

    for req in retained:
        anchor = anchors.get(req.aid)
        if req.kind not in ("far_geo","skyline_geo") and (anchor is None or
                float(anchor.lat)!=req.lat or float(anchor.lon)!=req.lon):
            retired.append(req.slot)
            continue
        add(current(req.kind,req.x,req.y,anchor,req),True)

    try:
        for key in (() if current_only else getattr(server,"_far_geo_done",{})):
            visited += 1
            if visited > max_keys:
                raise ResourceLimit("server far-geotile inspection budget exceeded")
            kind,x,y = key
            if kind not in ("bldfar","bldsky"):
                continue
            kind = "far_geo" if kind=="bldfar" else "skyline_geo"
            add(current(kind,x,y,previous=candidates.get((kind,x,y))))
        for conn in connections:
            anchor = anchors.get(getattr(conn,"active",None))
            if anchor is None:
                continue
            for attr,legacy,geo in (("roads","near","near_geo"),("bldfar","far","far_geo"),
                                     ("bldsky","skyline","skyline_geo")):
                tracker = getattr(conn,attr,None)
                if tracker is None:
                    continue
                if getattr(tracker,"aid",None) not in (None,anchor.id):
                    raise DiscoveryError("tracker/active anchor changed; retry discovery")
                kind = geo if getattr(tracker,"geo",False) else legacy
                unique = set()
                for name in (("sent","empty","building","car","futs","retry","shown") if current_only else
                             ("sent","empty","building","car","futs","retry","info","skins","shown")):
                    for key in getattr(tracker,name,()):
                        if key in unique:
                            continue
                        unique.add(key)
                        visited += 1
                        if visited > max_keys:
                            raise ResourceLimit("live tracker inspection budget exceeded")
                        if not isinstance(key,tuple) or len(key)!=2 or any(type(k) is not int for k in key):
                            raise DiscoveryError("unsupported live tracker key")
                        existing = candidates.get((kind,key[0],key[1])) if kind in ("far_geo","skyline_geo") else None
                        add(current(kind,*key,anchor,existing))
    except RuntimeError as e:
        if isinstance(e,DiscoveryError):
            raise
        raise DiscoveryError("live tracker state changed while discovering targets; retry") from e
    status = "discovered" if relevant else "no_active_anchor" if not anchors else "no_corresponding_targets"
    slots = tuple(sorted(candidates[k].slot for k in relevant))
    return Discovery(tuple(sorted(candidates.values(),key=lambda r:r.slot)),tuple(sorted(anchors)),tuple(sorted(retired)),status,slots)
