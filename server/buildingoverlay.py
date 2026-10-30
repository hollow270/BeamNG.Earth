"""Pure, offline building overlay prototype. No fetching, cache or renderer integration.

``merge(baseline, incoming, baseline_parts=(), incoming_parts=())`` returns copied
effective features and a deterministic dry-run report. Feature.geometry uses GeoJSON
lon/lat coordinates. Incoming features must have an unambiguous OSM way/relation
identity, supplied as feature_id, osm_ids or an OpenStreetMap sources record.
Attributes are *already normalized Overture-style values*, not raw OSM tags.

Only simple, closed Polygon exteriors are accepted from OSM. Holes, multipolygons,
antimeridian/polar geometry and invalid rings are conflicts, never repaired or
flattened. Unsupported baseline geometry is retained and its bbox blocks additions.
Exact identities take priority. Without one, equivalent rings within 15 cm count
as spatial duplicates. A bounded exact convex-area comparison also handles different
traces/vertex counts, with >=98% IoU/relative area and the same 15 cm distance bound.
Spatial associations retain the baseline geometry; other overlaps conflict.
All competing incoming candidates are considered together, never first-wins.

Absence, deleted and retagged records never delete baseline data in this prototype.
Parts are an atomic set with their parent: parts_complete explicitly asserts that
the caller supplied the whole set. Removing an existing part set is unsupported.
The dataclasses are frozen, but nested caller dictionaries are not: merge deep-copies
all inputs, and does not mutate mapgeom objects (including their memoized fields).
"""
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
import re


MAX_VERTICES = 512
MAX_AREA_M2 = 2_000_000.0
MAX_EXTENT_M = 5000.0
DUPLICATE_TOLERANCE_M = 0.15
_IDENTITY = re.compile(r"^(?:osm:)?(w|way|r|relation)[:/]?([1-9][0-9]*)(?:@[1-9][0-9]*)?$", re.I)


def normalize_osm_id(value):
    """w123@4 / way/123 / osm:way:123 -> osm:way:123; nodes/bare IDs -> None."""
    m = _IDENTITY.fullmatch(str(value).strip())
    if not m:
        return None
    kind = "way" if m[1].lower() in ("w", "way") else "relation"
    return f"osm:{kind}:{m[2]}"


@dataclass(frozen=True)
class Feature:
    feature_id: str
    geometry: dict | None
    attrs: dict = field(default_factory=dict)
    sources: tuple = ()
    osm_ids: tuple = ()
    parent_id: str | None = None
    status: str = "building"       # building, deleted or retagged (the last two only report conflicts)
    parts_complete: bool = False
    attribute_sources: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    action: str                    # addition, replacement, unchanged, conflict
    incoming_id: str | None
    baseline_ids: tuple = ()
    reason: str = ""
    geometry_changed: bool = False
    subject: str = "building"


@dataclass(frozen=True)
class MergeResult:
    buildings: tuple
    parts: tuple
    decisions: tuple

    def report(self):
        rows = [dict(action=d.action, incoming_id=d.incoming_id, baseline_ids=list(d.baseline_ids),
                     reason=d.reason, geometry_changed=d.geometry_changed, subject=d.subject)
                for d in self.decisions]
        counts = {a: sum(r["action"] == a for r in rows)
                  for a in ("addition", "replacement", "unchanged", "conflict")}
        return {"counts": counts, "buildings": len(self.buildings), "parts": len(self.parts), "decisions": rows}

    def report_json(self):
        return _json(self.report(), indent=2)

    def digest(self):
        """Effective content, including geometry, attrs and provenance; no clock or input-order dependency."""
        def row(f):
            return dict(id=f.feature_id, geometry=_geometry_key(f.geometry), attrs=f.attrs,
                        sources=_sources(f.sources), osm_ids=_identities(f), parent_id=f.parent_id,
                        attribute_sources={k: _sources(v) for k, v in f.attribute_sources.items()})
        return hashlib.sha256(_json([[row(f) for f in self.buildings],
                                    [row(f) for f in self.parts]]).encode()).hexdigest()


def _clean(value):
    # mapgeom attribute columns can contain numpy scalar values and NaN for missing values.
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _json(value, **kw):
    return json.dumps(_clean(value), sort_keys=True, ensure_ascii=False, allow_nan=False, **kw)


def _sources(sources):
    return tuple(json.loads(s) for s in sorted({_json(s) for s in sources}))


def _identities(f):
    ids = [normalize_osm_id(v) for v in f.osm_ids]
    ids.append(normalize_osm_id(f.feature_id))
    for s in f.sources:
        if s.get("dataset", "").lower() == "openstreetmap" and s.get("property") in (None, "", "/geometry"):
            ids.append(normalize_osm_id(s.get("record_id")))
    return tuple(sorted({v for v in ids if v}))


def _canonical_ring(ring):
    points = [tuple(float(v) for v in p) for p in ring[:-1]]
    # Validated rings have no repeated vertices, so their minimum vertex is unique.
    k = min(range(len(points)), key=points.__getitem__)
    forward = points[k:] + points[:k]
    backward = [forward[0]] + list(reversed(forward[1:]))
    return min(tuple(forward), tuple(backward))


def _geometry_key(geometry):
    # Baseline shapes may be unsupported: preserve their complete structure in the digest.
    try:
        if geometry["type"] == "Polygon" and len(geometry["coordinates"]) == 1:
            ring = geometry["coordinates"][0]
            if len(ring) >= 4 and ring[0] == ring[-1]:
                return {"type": "Polygon", "ring": _canonical_ring(ring)}
    except (KeyError, TypeError, ValueError, IndexError):
        pass
    return geometry


def geometry_digest(geometry):
    """Coordinate-based fingerprint, independent of ring start/winding, never of OSM version alone."""
    return hashlib.sha256(_json(_geometry_key(geometry)).encode()).hexdigest()


def _xy(ring, origin):
    lon, lat = origin
    return tuple(((p[0] - lon) * 111320.0 * math.cos(math.radians(lat)),
                  (p[1] - lat) * 110540.0) for p in ring)


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(p, a, b):
    return (abs(_cross(a, b, p)) <= 1e-8 and
            min(a[0], b[0]) - 1e-8 <= p[0] <= max(a[0], b[0]) + 1e-8 and
            min(a[1], b[1]) - 1e-8 <= p[1] <= max(a[1], b[1]) + 1e-8)


def _intersects(a, b, c, d, proper=False):
    x, y, z, w = _cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b)
    if ((x > 1e-8 and y < -1e-8) or (x < -1e-8 and y > 1e-8)) and \
            ((z > 1e-8 and w < -1e-8) or (z < -1e-8 and w > 1e-8)):
        return True
    return not proper and any((_on_segment(c, a, b), _on_segment(d, a, b),
                               _on_segment(a, c, d), _on_segment(b, c, d)))


def _inside(p, ring, boundary=False):
    inside = False
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if _on_segment(p, a, b):
            return boundary
        if (a[1] > p[1]) != (b[1] > p[1]):
            x = a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if p[0] < x:
                inside = not inside
    return inside


def _validated(geometry):
    """(unclosed canonical lon/lat ring, error). No geometry repairs."""
    try:
        if not isinstance(geometry, dict) or geometry.get("type") != "Polygon":
            return None, "unsupported_geometry_type"
        rings = geometry["coordinates"]
        if len(rings) != 1:
            return None, "unsupported_holes"
        raw = rings[0]
        if not 4 <= len(raw) <= MAX_VERTICES + 1:
            return None, "invalid_vertex_count"
        if any(len(p) != 2 or any(isinstance(v, (bool, str)) for v in p) for p in raw):
            return None, "invalid_coordinates"
        ring = tuple(tuple(float(v) for v in p) for p in raw)
        if any(not math.isfinite(x) or not math.isfinite(y) or abs(x) > 180 or abs(y) > 90 for x, y in ring):
            return None, "invalid_coordinates"
        if ring[0] != ring[-1]:
            return None, "unclosed_ring"
        if len(set(ring[:-1])) != len(ring) - 1:
            return None, "repeated_vertex"
        if any(abs(p[1]) > 85 for p in ring) or max(p[0] for p in ring) - min(p[0] for p in ring) > 180:
            return None, "unsupported_geographic_extent"
        points = _xy(ring[:-1], ring[0])
        if max(math.hypot(*p) for p in points) > MAX_EXTENT_M:
            return None, "unsupported_geographic_extent"
        edges = tuple(zip(points, points[1:] + points[:1]))
        for i, (a, b) in enumerate(edges):
            for j in range(i + 1, len(edges)):
                c, d = edges[j]
                if j == i + 1 or (i == 0 and j == len(edges) - 1):
                    # Adjacent collinear edges may continue straight, but cannot double back.
                    common, u, v = (b, a, d) if j == i + 1 else (a, b, c)
                    if abs(_cross(u, common, v)) <= 1e-8 and \
                            (u[0] - common[0]) * (v[0] - common[0]) + \
                            (u[1] - common[1]) * (v[1] - common[1]) > 0:
                        return None, "self_intersection"
                elif _intersects(a, b, c, d):
                    return None, "self_intersection"
        area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in edges)) / 2
        if not 0.5 <= area <= MAX_AREA_M2:
            return None, "invalid_area"
        return _canonical_ring(ring), None
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        return None, "malformed_geometry"


def _bbox(geometry):
    points = []
    def walk(value):
        if isinstance(value, (list, tuple)):
            if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
                if all(math.isfinite(v) for v in value[:2]):
                    points.append(value[:2])
            else:
                for child in value:
                    walk(child)
    if isinstance(geometry, dict):
        walk(geometry.get("coordinates"))
    if not points:
        return None
    return min(p[0] for p in points), min(p[1] for p in points), \
        max(p[0] for p in points), max(p[1] for p in points)


def _equivalent(a, b):
    if a is None or b is None:
        return False
    aa, bb = _xy(a, a[0]), _xy(b, a[0])
    if len(a) == len(b):
        for points in (bb, tuple(reversed(bb))):
            for k in range(len(points)):
                shifted = points[k:] + points[:k]
                if all(math.dist(p, q) <= DUPLICATE_TOLERANCE_M for p, q in zip(aa, shifted)):
                    return True
    from buildingspatial import equivalent
    return equivalent(a,b,DUPLICATE_TOLERANCE_M)


def _overlap(ga, a, gb, b):
    ba, bb = _bbox(ga), _bbox(gb)
    if ba is None or bb is None:
        return True                # unlocatable malformed input blocks speculative changes
    if ba[0] >= bb[2] or ba[2] <= bb[0] or ba[1] >= bb[3] or ba[3] <= bb[1]:
        return False               # boundary contact alone is allowed
    if a is None or b is None:
        return True                # unsupported shapes: conservative bbox test only
    if a == b:
        return True
    aa, bb = _xy(a, a[0]), _xy(b, a[0])
    ea, eb = tuple(zip(aa, aa[1:] + aa[:1])), tuple(zip(bb, bb[1:] + bb[:1]))
    def inner_samples(edges):
        # Coincident boundaries with different collinear vertices have no proper
        # crossings or strictly contained vertices. Probe just inside each edge.
        sign = 1 if sum(p[0] * q[1] - q[0] * p[1] for p, q in edges) > 0 else -1
        for p, q in edges:
            length = math.dist(p, q)
            step = min(1e-5, length * 1e-4) / length * sign
            yield ((p[0] + q[0]) / 2 - (q[1] - p[1]) * step,
                   (p[1] + q[1]) / 2 + (q[0] - p[0]) * step)
    return (any(_intersects(p, q, r, s, proper=True) for p, q in ea for r, s in eb) or
            any(_inside(p, bb) or _inside(((p[0] + q[0]) / 2, (p[1] + q[1]) / 2), bb) for p, q in ea) or
            any(_inside(p, aa) or _inside(((p[0] + q[0]) / 2, (p[1] + q[1]) / 2), aa) for p, q in eb) or
            any(_inside(p, aa) and _inside(p, bb) for p in inner_samples(ea)))


def _contained(child, parent):
    a, b = _xy(child, parent[0]), _xy(parent, parent[0])
    edges = tuple(zip(a, a[1:] + a[:1]))
    def within(p, q):
        # Split at parent vertices on the child's edge as well: passing through
        # a concave corner can leave the parent without a proper edge crossing.
        stops = [p, q] + [r for r in b if _on_segment(r, p, q)]
        stops.sort(key=lambda r: math.dist(p, r))
        return all(_inside(r, b, boundary=True) for r in stops) and all(
            _inside(((r[0] + s[0]) / 2, (r[1] + s[1]) / 2), b, boundary=True)
            for r, s in zip(stops, stops[1:]))
    return (all(within(p, q) for p, q in edges) and
            not any(_intersects(p, q, r, s, proper=True) for p, q in edges for r, s in zip(b, b[1:] + b[:1])))


def _attrs(f):
    out = {}
    for k, v in f.attrs.items():
        v = _clean(v)
        if v is None or v == "":
            continue
        if k in ("cx", "cy", "building_id", "has_parts"):
            continue               # recomputed structural metadata
        if k in ("height", "num_floors", "roof_height", "min_height", "min_floor"):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or (k == "height" and v == 0):
                raise ValueError("invalid_attributes")
        out[k] = v
    _json(out)                     # reject non-serializable normalized attributes
    return out


def _updated(old, new, fid, *, geometry=True, parent_id=None):
    sources = _sources(tuple(new.sources) + ({"dataset": "OpenStreetMap", "record_id": _identities(new)[0]},))
    attrs = deepcopy(old.attrs) if old else {}
    field_sources = deepcopy(old.attribute_sources) if old else {}
    if old:
        for k in attrs:
            field_sources.setdefault(k, _sources(old.sources))
    for k, v in _attrs(new).items():
        if old is None or _clean(attrs.get(k)) != v:
            attrs[k] = v
            field_sources[k] = sources
    geom = deepcopy(new.geometry if geometry or old is None else old.geometry)
    box = _bbox(geom)
    if box:
        attrs.update(cx=(box[0] + box[2]) / 2, cy=(box[1] + box[3]) / 2)
    if parent_id is not None:
        attrs["building_id"] = parent_id
    return Feature(fid, geom, attrs, _sources(tuple(old.sources if old else ()) + sources),
                   tuple(sorted(set(_identities(new)) | set(_identities(old) if old else ()))),
                   parent_id, attribute_sources=field_sources)


def _unique(features, label):
    ids = [f.feature_id for f in features]
    if any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != len(ids):
        raise ValueError(f"{label}: feature IDs must be unique nonempty strings")


def merge(baseline, incoming, *, baseline_parts=(), incoming_parts=()):
    """Merge local Feature sequences. Input absence is never deletion evidence.

    Structural caller errors (duplicate IDs, orphan baseline parts) raise ValueError.
    Bad/unsupported incoming buildings and parts become report conflicts. All spatial
    tests use the full supplied area; callers must supply neighbouring baseline buildings.
    """
    base, new, old_parts, new_parts = [tuple(sorted(deepcopy(tuple(fs)), key=lambda f: f.feature_id))
                                      for fs in (baseline, incoming, baseline_parts, incoming_parts)]
    for fs, label in ((base, "baseline"), (new, "incoming"), (old_parts, "baseline parts"), (new_parts, "incoming parts")):
        _unique(fs, label)
    base_by_id = {f.feature_id: f for f in base}
    if any(p.parent_id not in base_by_id for p in old_parts):
        raise ValueError("baseline parts: unknown parent")
    if set(base_by_id) & {p.feature_id for p in old_parts}:
        raise ValueError("baseline parts: building/part ID collision")
    rings_b = [_validated(f.geometry)[0] for f in base]
    validated = [_validated(f.geometry) for f in new]
    ids_b, ids_n = [_identities(f) for f in base], [_identities(f) for f in new]
    links = [set() for _ in new]
    errors = [err for _, err in validated]
    for i, f in enumerate(new):
        if len(ids_n[i]) != 1:
            errors[i] = "ambiguous_or_missing_osm_identity"
        if f.status != "building":
            errors[i] = "deletion_or_retagging_unverified"
        try:
            _attrs(f)
        except (TypeError, ValueError):
            errors[i] = "invalid_attributes"
        for j, old in enumerate(base):
            if (set(ids_n[i]) & set(ids_b[j]) or
                    _overlap(f.geometry, validated[i][0], old.geometry, rings_b[j]) or
                    _equivalent(validated[i][0], rings_b[j])):
                links[i].add(j)
    # Connected components include shared baseline targets, overlapping incoming
    # footprints and duplicate identities. A split/merge never depends on iteration order.
    neighbours = [set() for _ in new]
    for i, f in enumerate(new):
        for j in range(i):
            if (links[i] & links[j] or set(ids_n[i]) & set(ids_n[j]) or
                    _overlap(f.geometry, validated[i][0], new[j].geometry, validated[j][0]) or
                    _equivalent(validated[i][0], validated[j][0])):
                neighbours[i].add(j)
                neighbours[j].add(i)
    children = {i: [] for i in range(len(new))}
    decisions = []
    for part in new_parts:
        parent = normalize_osm_id(part.parent_id)
        matches = [i for i, f in enumerate(new) if part.parent_id == f.feature_id or parent in ids_n[i]]
        if len(matches) != 1:
            decisions.append(Decision("conflict", part.feature_id, reason="unknown_or_ambiguous_parent", subject="part"))
        else:
            children[matches[0]].append(part)
    # A part claimed by two incoming parents must block both transactions, rather
    # than letting the first sorted parent take it.
    owners = {}
    for i, supplied in children.items():
        for p in supplied:
            for identity in _identities(p):
                owners.setdefault(identity, set()).add(i)
    for group in owners.values():
        for i in group:
            neighbours[i].update(group - {i})
    effective, parts = dict(base_by_id), {p.feature_id: p for p in old_parts}
    touched, seen = set(), set()
    for start in range(len(new)):
        if start in seen:
            continue
        component, pending = set(), [start]
        while pending:
            i = pending.pop()
            if i not in component:
                component.add(i)
                pending.extend(neighbours[i] - component)
        seen.update(component)
        targets = set().union(*(links[i] for i in component))
        if len(component) > 1 or len(targets) > 1:
            for i in sorted(component):
                decisions.append(Decision("conflict", new[i].feature_id,
                                          tuple(base[j].feature_id for j in sorted(targets)),
                                          errors[i] or "ambiguous_overlap_split_or_merge"))
            continue
        i = start
        f, ring = new[i], validated[i][0]
        old_index = next(iter(targets)) if targets else None
        old = base[old_index] if old_index is not None else None
        reason = errors[i]
        identity_match = old is not None and bool(set(ids_n[i]) & set(ids_b[old_index]))
        if old and not reason:
            if rings_b[old_index] is None:
                reason = "unsupported_baseline_geometry"
            elif not identity_match and ids_b[old_index]:
                reason = "conflicting_osm_identity"
            elif not identity_match and not _equivalent(ring, rings_b[old_index]):
                reason = "ambiguous_spatial_overlap"
        previous = [p for p in old_parts if old and p.parent_id == old.feature_id]
        supplied = children[i]
        if old and bool(old.attrs.get("has_parts")) and not previous:
            reason = reason or "baseline_parts_not_supplied"
        if supplied and not f.parts_complete:
            reason = reason or "incomplete_parts"
        if f.attrs.get("has_parts") and not supplied and not previous:
            reason = reason or "declared_parts_not_supplied"
        part_rings = [_validated(p.geometry) for p in supplied]
        part_ids = [_identities(p) for p in supplied]
        for k, p in enumerate(supplied):
            pr, err = part_rings[k]
            if err or len(part_ids[k]) != 1 or p.status != "building":
                reason = reason or "invalid_or_unsupported_part"
            elif ring and not _contained(pr, ring):
                reason = reason or "part_outside_parent"
            try:
                _attrs(p)
            except (TypeError, ValueError):
                reason = reason or "invalid_part_attributes"
            if any(set(_identities(p)) & set(_identities(q)) and
                   (old is None or q.parent_id != old.feature_id) for q in old_parts):
                reason = reason or "part_belongs_to_other_parent"
            if any(_equivalent(pr, part_rings[j][0]) for j in range(k)):
                reason = reason or "ambiguous_duplicate_parts"
        if len({ids[0] for ids in part_ids if len(ids) == 1}) != len(supplied):
            reason = reason or "ambiguous_part_identity"
        if previous and f.parts_complete and not supplied:
            reason = reason or "parts_removal_unsupported"
        if previous and f.parts_complete and any(_validated(p.geometry)[1] for p in previous):
            reason = reason or "unsupported_baseline_part_geometry"
        if not reason:
            fid = old.feature_id if old else ids_n[i][0]
            candidate = _updated(old, f, fid, geometry=identity_match or old is None)
            changed = old is not None and geometry_digest(candidate.geometry) != geometry_digest(old.geometry)
            attr_changed = old is not None and _attrs(candidate) != _attrs(old)
            if previous and not f.parts_complete and (changed or attr_changed):
                reason = "complete_parts_required_for_parent_update"
            if supplied and old and not identity_match:
                reason = "parts_require_parent_identity_match"
            replacement_parts = []
            used_part_ids = set()
            for p in supplied:
                matched = [q for q in previous if set(_identities(q)) & set(_identities(p))]
                if len(matched) > 1 or (matched and matched[0].feature_id in used_part_ids):
                    reason = reason or "ambiguous_part_identity"
                    continue
                prior = matched[0] if matched else None
                pid = prior.feature_id if prior else _identities(p)[0]
                if pid == fid or pid in effective or (pid in parts and parts[pid].parent_id != fid):
                    reason = reason or "part_id_collision"
                used_part_ids.add(pid)
                replacement_parts.append(_updated(prior, p, pid, parent_id=fid))
            if f.parts_complete and any(p.feature_id not in used_part_ids for p in previous):
                reason = reason or "part_removal_or_reidentification_ambiguous"
            if not old and (fid in effective or fid in parts):
                reason = reason or "building_id_collision"
        if reason:
            decisions.append(Decision("conflict", f.feature_id, (old.feature_id,) if old else (), reason))
            continue
        parts_changed = False
        part_geometry_changed = False
        if f.parts_complete:
            replacement_parts.sort(key=lambda p: p.feature_id)
            parts_changed = _json([(p.feature_id, _geometry_key(p.geometry), _attrs(p)) for p in previous]) != \
                _json([(p.feature_id, _geometry_key(p.geometry), _attrs(p)) for p in replacement_parts])
            part_geometry_changed = _json([(p.feature_id, _geometry_key(p.geometry)) for p in previous]) != \
                _json([(p.feature_id, _geometry_key(p.geometry)) for p in replacement_parts])
            for p in previous:
                parts.pop(p.feature_id)
            for p in replacement_parts:
                parts[p.feature_id] = p
        candidate.attrs["has_parts"] = bool(supplied if f.parts_complete else previous)
        effective[fid] = candidate
        if old:
            touched.add(old.feature_id)
        action = "addition" if old is None else "replacement" if changed or attr_changed or parts_changed else "unchanged"
        decisions.append(Decision(action, f.feature_id, (old.feature_id,) if old else (),
                                  "osm_identity" if identity_match else "spatial_duplicate" if old else "new_osm_building",
                                  changed or part_geometry_changed))
    for old in base:
        if old.feature_id not in touched:
            decisions.append(Decision("unchanged", None, (old.feature_id,), "baseline_retained"))
    decisions.sort(key=lambda d: (d.subject, d.incoming_id or "", d.baseline_ids, d.action, d.reason))
    return MergeResult(tuple(effective[k] for k in sorted(effective)),
                       tuple(parts[k] for k in sorted(parts)), tuple(decisions))


def from_mapgeom(features, *, provenance=()):
    """Copy a mapgeom.Features building/part layer into offline Features.

    Full source records may be JSON in attrs['sources']; otherwise the reader's
    src_dataset/src_record pair is retained. No source identity is invented.
    """
    import mapgeom
    out = []
    for i, fid in enumerate(features.ids):
        if features.kind[i] != mapgeom.POLYGON:
            geometry = {"type": "Unsupported", "coordinates": []}
        else:
            polygons = [[r.tolist() for r in p] for p in features.parts(i)]
            geometry = {"type": "Polygon", "coordinates": polygons[0]} if len(polygons) == 1 else \
                {"type": "MultiPolygon", "coordinates": polygons}
        attrs = {k: _clean(v[i]) for k, v in features.attrs.items()}
        sources = list(deepcopy(provenance))
        full = attrs.pop("sources", None)
        if full:
            sources.extend(json.loads(full) if isinstance(full, str) else full)
        if attrs.get("src_dataset"):
            sources.append({"dataset": attrs["src_dataset"], "record_id": attrs.get("src_record", "")})
        out.append(Feature(str(fid), geometry, attrs, _sources(sources), parent_id=attrs.get("building_id")))
    return tuple(sorted(out, key=lambda f: f.feature_id))
