"""Pinned, rendering-only overlay view over immutable map-data cells.

Only MapData.building_cell uses this adapter. Baseline cell(), versions(), road
widths, grading, terrain and imagery remain unchanged. No fetching of OSM data,
storage writes, tile invalidation or live refresh. A caller may supply an explicit
snapshot; MapData scopes that view to a single generation job.
"""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import threading

import numpy as np

import buildingoverlay as bo
from buildingoverlaystore import OverlayStore, BaselineMismatch, ResourceLimit
import mapcells
import mapgeom as mg
import overture


LOOKUP_CELLS = 64
CELL_CACHE_BYTES = 64 * 1024 * 1024
CELL_CACHE_COUNT = 8


@dataclass(frozen=True)
class Dependency:
    revision: str | None = None
    geometry_digest: str | None = None
    feature_ids: tuple = ()


def _hash(value):
    return hashlib.sha256(bo._json(value).encode()).hexdigest()


def _intersects(a, b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def _take(f, indices):
    """Copy rows without widening/cleaning any original attributes or touching memo fields."""
    indices = np.asarray(indices, np.int64)
    out = mg.build(f.layer, f.ids[indices].copy(), [(int(f.kind[i]), f.parts(i)) for i in indices])
    out.attrs = {k: v[indices].copy() for k, v in f.attrs.items()}
    if f.own is not None:
        out.own = f.own[indices].copy()
    if f.clipped is not None:
        out.clipped = f.clipped[indices].copy()
    remap = {int(old): new for new, old in enumerate(indices)}
    for k, r in f.rules.items():
        mask = np.array([int(i) in remap for i in r.feature], bool)
        out.rules[k] = mg.RuleTable(np.array([remap[int(i)] for i in r.feature[mask]], np.int32),
                                   r.value[mask].copy(), r.start[mask].copy(), r.end[mask].copy())
    return out


def _attrs(record, layer):
    attrs = deepcopy(record.attrs)
    box = bo._bbox(record.geometry)
    attrs.update(cx=(box[0] + box[2]) / 2, cy=(box[1] + box[3]) / 2)
    if record.parent_id is not None:
        attrs["building_id"] = record.parent_id
    primary = next((s for s in record.sources if s.get("property") in (None, "", "/geometry") and s.get("dataset")), {})
    attrs.setdefault("src_dataset", primary.get("dataset", "OpenStreetMap"))
    attrs.setdefault("src_record", primary.get("record_id", record.feature_id))
    if layer == "buildings" and not attrs.get("height_src"):
        height = next(iter(record.attribute_sources.get("height", ())), primary)
        attrs["height_src"] = height.get("dataset", "")
    return attrs


def _geometry_source(feature):
    sources = [s for s in feature.sources if s.get("dataset", "").lower() == "openstreetmap"
               and s.get("property") in (None, "", "/geometry")]
    def order(s):
        version = str(s.get("record_id", "")).rpartition("@")[2]
        return (str(s.get("snapshot_timestamp") or ""), str(s.get("geometry_timestamp") or ""),
                str(s.get("update_time") or ""), int(version) if version.isdigit() else -1)
    return max(sources, key=order) if sources else {"dataset": "OpenStreetMap", "record_id": feature.feature_id}


def _combine(base, remove, records, key, frame, token, render_attrs):
    records = sorted(records, key=lambda f: f.feature_id)
    added = mg.build(base.layer, mg.text_column([f.feature_id for f in records]),
                     [(mg.POLYGON, [[np.asarray(r, np.float64) for r in f.geometry["coordinates"]]]) for f in records])
    attrs = [dict(_attrs(f, base.layer), **render_attrs.get(f.feature_id, {})) for f in records]
    spec = overture.LAYERS[base.layer]
    columns = (set(base.attrs) | set().union(*(set(a) for a in attrs)) | set(spec.get("text", ())) |
               set(spec.get("float", ())) | set(spec.get("bool", ())))
    for name in sorted(columns):
        values = [a.get(name) for a in attrs]
        dtype = base.attrs[name].dtype.kind if name in base.attrs else None
        if dtype == "b" or name in spec.get("bool", ()):
            added.attrs[name] = mg.bool_column(values)
        elif dtype in ("f", "i", "u") or name in spec.get("float", ()) or name in ("cx", "cy"):
            added.attrs[name] = mg.float_column(values, base.attrs[name].dtype if dtype == "f" else np.float64)
        else:
            added.attrs[name] = mg.text_column([bo._json(v) if isinstance(v, (dict, list, tuple)) else v for v in values])
    kept = _take(base, [i for i, fid in enumerate(base.ids) if str(fid) not in remove])
    # concat is intentionally strict about sorted IDs; assemble sorted single-row
    # chunks here so OSM IDs need not sort after every possible baseline ID.
    rows = sorted([(str(fid), kept, i) for i, fid in enumerate(kept.ids)] +
                  [(str(fid), added, i) for i, fid in enumerate(added.ids)])
    out = mg.build(base.layer, mg.text_column([fid for fid, _, _ in rows]),
                   [(int(f.kind[i]), f.parts(i)) for _, f, i in rows])
    for name in sorted(set(kept.attrs) | set(added.attrs)):
        template = kept.attrs.get(name, added.attrs.get(name))
        values = [f.attrs[name][i] if name in f.attrs else False if template.dtype.kind == "b" else
                  "" if template.dtype.kind == "U" else np.nan for _, f, i in rows]
        out.attrs[name] = mg.text_column(values) if template.dtype.kind == "U" else np.asarray(values, template.dtype)
    out.clipped = np.array([bool(f.clipped[i]) if f.clipped is not None else False for _, f, i in rows])
    ox, oy = mapcells.owner(out.attrs.get("cx", np.zeros(len(out))), out.attrs.get("cy", np.zeros(len(out))), key.z)
    computed_own = (ox == key.x) & (oy == key.y)
    out.own = np.array([bool(f.own[i]) if f.own is not None else bool(computed_own[j])
                        for j, (_, f, i) in enumerate(rows)])
    out.localize(frame)
    # Preserve baseline rule tables too (buildings normally have none).
    positions = {fid: i for i, (fid, _, _) in enumerate(rows)}
    for name, r in kept.rules.items():
        out.rules[name] = mg.RuleTable(np.array([positions[str(kept.ids[i])] for i in r.feature], np.int32),
                                      r.value.copy(), r.start.copy(), r.end.copy())
    out.__dict__["_bnge_building_dependency"] = token
    out.__dict__["overlay_provenance"] = {f.feature_id: {"sources": deepcopy(f.sources),
        "attribute_sources": deepcopy(f.attribute_sources), "parent_id": f.parent_id} for f in records}
    return out


class BuildingView:
    def __init__(self, store, release, *, snapshot=None):
        self.snapshot = deepcopy(snapshot) if snapshot is not None else store.load()
        self.active = bool(self.snapshot.enabled and self.snapshot.entries)
        if self.active and self.snapshot.baseline_release != release:
            raise BaselineMismatch("render overlay uses a different Overture release")
        self.release = release
        self.lock = threading.RLock()
        self.validated = set()
        self.render_attrs = {}
        self.cache = OrderedDict()
        self.cache_bytes = 0

    def relevant(self, key):
        return tuple(e for e in self.snapshot.entries if _intersects(e.bounds, key.bounds())) if self.active else ()

    def dependency(self, keys, *, render_bounds=None):
        keys = tuple(sorted(set(keys), key=str))
        entries = {e.feature.feature_id: e for k in keys for e in self.relevant(k)}
        if render_bounds is not None:
            entries = {fid:e for fid,e in entries.items() if _intersects(e.bounds,render_bounds)}
        if not entries:
            return Dependency()
        full, geo = [], []
        for fid, e in sorted(entries.items()):
            full.append([fid, bo.MergeResult((e.feature,), e.parts, ()).digest(), e.baseline_digest, e.bounds])
            geo.append([fid, bo.geometry_digest(e.feature.geometry),
                        [(p.feature_id, bo.geometry_digest(p.geometry)) for p in e.parts], e.bounds])
            if e.feature.status == "deleted":
                full[-1].append("suppressed")
                geo[-1].append("suppressed")
        return Dependency(_hash(full), _hash(geo), tuple(sorted(entries)))

    def _validate(self, md, entry, kind):
        fid = entry.feature.feature_id
        if fid in self.validated:
            return
        originals, parts = {}, {}
        w, s, e, n = entry.bounds
        try:
            keys = mapcells.cells_for_bbox(s, w, n, e, max_cells=LOOKUP_CELLS)
        except ValueError as e:
            raise ResourceLimit("overlay original lookup exceeds bounded cell context") from e
        new_box = bo._bbox(entry.feature.geometry)
        new_ring = bo._validated(entry.feature.geometry)[0]
        for key in keys:
            cd = md.cell(key, layers=("buildings", "building_parts"), kind=kind)
            for layer, target in (("buildings", originals), ("building_parts", parts)):
                f = cd.layers.get(layer)
                if f is None:
                    raise BaselineMismatch(f"baseline {layer} unavailable in overlay neighbourhood")
                matches = np.flatnonzero(f.ids == fid) if layer == "buildings" else \
                    np.flatnonzero(f.attrs.get("building_id", np.full(len(f), "")) == fid)
                for record in bo.from_mapgeom(_take(f, matches)):
                    if record.feature_id in target and bo._json(target[record.feature_id].__dict__) != bo._json(record.__dict__):
                        raise BaselineMismatch("neighbouring baseline cells disagree on an overlay original")
                    target[record.feature_id] = record
                if layer == "buildings" and entry.feature.status != "deleted":
                    candidates = []
                    for i, other_id in enumerate(f.ids):
                        if str(other_id) == fid:
                            continue
                        a = f.coord_off[f.ring_off[f.part_off[i]]]
                        b = f.coord_off[f.ring_off[f.part_off[i+1]]]
                        xy = f.lonlat[a:b]
                        if len(xy) and _intersects((xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max()), new_box):
                            candidates.append(i)
                    for other in bo.from_mapgeom(_take(f, candidates)):
                        ring = bo._validated(other.geometry)[0]
                        if bo._overlap(entry.feature.geometry, new_ring, other.geometry, ring) or bo._equivalent(new_ring, ring):
                            raise BaselineMismatch("stored overlay overlaps additional baseline context; recreate the overlay")
        one = replace(self.snapshot, entries=(entry,))
        OverlayStore.compose(one, tuple(originals.values()), baseline_parts=tuple(parts.values()),
                             baseline_release=self.release, bounds=entry.bounds)
        for record, old in [(entry.feature, originals.get(fid))] + [(p, parts.get(p.feature_id)) for p in entry.parts]:
            attrs = {}
            if old is None or bo.geometry_digest(old.geometry) != bo.geometry_digest(record.geometry):
                source = _geometry_source(record)
                attrs.update(src_dataset=source["dataset"], src_record=source.get("record_id", record.feature_id))
            if old is None or old.attrs.get("height") != record.attrs.get("height"):
                height_sources = record.attribute_sources.get("height", ())
                if height_sources:
                    attrs["height_src"] = height_sources[0].get("dataset", "")
            self.render_attrs[record.feature_id] = attrs
        self.validated.add(fid)

    def cell(self, md, base, kind):
        entries = self.relevant(base.key)
        if not entries or "buildings" not in base.layers:
            return base
        cache_key = (base.key, tuple(base.layers))
        with self.lock:
            got = self.cache.get(cache_key)
            if got is not None:
                self.cache.move_to_end(cache_key)
                return replace(base, layers=dict(base.layers, **got[0]), versions=dict(base.versions, building_overlay=got[1]),
                               provenance=base.provenance + [got[2]])
            for entry in entries:
                self._validate(md, entry, kind)
            f = base.layers["buildings"]
            base_ids = set(map(str, f.ids)) if f is not None else set()
            for entry in entries:
                if entry.baseline_digest is None and entry.feature.feature_id in base_ids:
                    raise BaselineMismatch("overlay addition collides with a baseline ID")
            if f is None:
                raise BaselineMismatch("baseline buildings unavailable for rendering")
            remove = {e.feature.feature_id for e in entries if e.baseline_digest is not None}
            present = [e for e in entries if e.feature.status == "building" and
                       _intersects(bo._bbox(e.feature.geometry), base.bounds)]
            token = self.dependency((base.key,)).revision
            layers = {"buildings": _combine(f, remove, [e.feature for e in present], base.key, base.frame, token,
                                           self.render_attrs)}
            # Parts travel with the full parent in every intersecting cell, rather
            # than disappearing when a parent centroid and a part use different cells.
            old_parts = base.layers.get("building_parts")
            if old_parts is None:
                old_parts = mg.empty("building_parts")
            old_parent = old_parts.attrs.get("building_id", np.full(len(old_parts), ""))
            part_remove = {str(old_parts.ids[i]) for i, pid in enumerate(old_parent) if str(pid) in remove}
            layers["building_parts"] = _combine(old_parts, part_remove,
                [p for e in present for p in e.parts], base.key, base.frame, token, self.render_attrs)
            provenance = {"id": "osm-building-overlay", "name": "OpenStreetMap building overlay",
                          "release": token, "attribution": "© OpenStreetMap contributors",
                          "licence": "ODbL-1.0", "url": "https://www.openstreetmap.org/copyright"}
            size = sum(a.nbytes for f in layers.values() for a in f.to_arrays("").values())
            if size <= CELL_CACHE_BYTES:
                self.cache[cache_key] = (layers, token, provenance, size)
                self.cache_bytes += size
                while self.cache_bytes > CELL_CACHE_BYTES or len(self.cache) > CELL_CACHE_COUNT:
                    _, old = self.cache.popitem(last=False)
                    self.cache_bytes -= old[3]
            return replace(base, layers=dict(base.layers, **layers), versions=dict(base.versions, building_overlay=token),
                           provenance=base.provenance + [provenance])
