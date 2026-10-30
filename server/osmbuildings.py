"""Opt-in, manual Overpass retrieval. No default endpoint or live-world hooks.

All requests use netfetch.session(): its deny list, offline flag, budgets, tokens
and circuit breakers still apply. A separate bounded POST transport supplies the
streaming byte/deadline limits that netfetch's ordinary GET does not provide.
No redirects, retry loops, endpoint rotation, background work or disk writes.

parse() is pure. It requires our query's terminal count marker and every geometry
dependency before returning a snapshot. Unsupported *complete* geometry is retained
with diagnostics for the Stage 1 merge to reject, never flattened. Missing geometry
or an API remark discards the entire response. Absence never creates a deletion.
"""
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import email.utils
import http.client
import json
import math
import re
import threading
import time
from urllib.parse import urlencode, urlsplit

import buildingoverlay as bo
import netfetch


OSM_SOURCE = {"dataset": "OpenStreetMap", "license": "ODbL-1.0",
              "attribution": "© OpenStreetMap contributors", "url": "https://www.openstreetmap.org/copyright"}


class RetrievalError(IOError):
    pass


class ConfigurationError(ValueError):
    pass


class IncompleteResponse(RetrievalError):
    pass


class OverpassError(IncompleteResponse):
    pass


class ResourceLimit(RetrievalError):
    pass


class StaleSnapshot(RetrievalError):
    pass


class Throttled(RetrievalError):
    def __init__(self, reason, retry_after=0.0):
        super().__init__(reason)
        self.retry_after = max(0.0, retry_after)


class HTTPFailure(RetrievalError):
    def __init__(self, code, headers):
        super().__init__(f"configured OSM source returned HTTP {code}")
        self.code, self.headers = code, headers  # netfetch.session honours this protocol


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    endpoint: str | None = field(default=None, repr=False)
    headers: dict = field(default_factory=dict, repr=False)  # runtime credentials only
    allow_insecure_http: bool = False
    min_spacing_s: float = 60.0
    area_cooldown_s: float = 600.0
    timeout_s: float = 30.0
    total_timeout_s: float = 45.0
    query_timeout_s: int = 25
    query_memory_bytes: int = 64 * 1024 * 1024
    max_response_bytes: int = 8 * 1024 * 1024
    max_elements: int = 20000
    max_vertices: int = 50000
    max_features: int = 512
    max_known_ids: int = 64
    max_snapshot_age_s: float = 3600.0

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.allow_insecure_http) is not bool:
            raise ConfigurationError("opt-in flags must be boolean")
        for key in ("min_spacing_s", "area_cooldown_s", "timeout_s", "total_timeout_s", "max_snapshot_age_s"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ConfigurationError(f"invalid {key}")
        if self.timeout_s <= 0 or self.total_timeout_s <= 0:
            raise ConfigurationError("timeouts must be positive")
        for key in ("query_timeout_s", "query_memory_bytes", "max_response_bytes", "max_elements",
                    "max_vertices", "max_features", "max_known_ids"):
            if type(getattr(self, key)) is not int or getattr(self, key) <= 0:
                raise ConfigurationError(f"invalid {key}")
        if self.endpoint is not None:
            _endpoint(self.endpoint, self.allow_insecure_http)
        if not isinstance(self.headers, dict):
            raise ConfigurationError("headers must be a dictionary")
        for name, value in self.headers.items():
            # Auth headers may be supplied, but request framing and application identity are fixed.
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9-]+", name) or \
                    name.lower() in ("host", "connection", "content-length", "content-type", "transfer-encoding",
                                     "user-agent", "accept-encoding") or not isinstance(value, str) or \
                    any(ord(c) < 32 or ord(c) > 255 for c in value):
                raise ConfigurationError("invalid or reserved request header")
        if self.headers and self.endpoint and urlsplit(self.endpoint).scheme != "https":
            raise ConfigurationError("credential headers require HTTPS")


def _endpoint(value, allow_http):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise ConfigurationError("invalid endpoint")
    try:
        u = urlsplit(value)
        port = u.port
        if not u.hostname or u.username is not None or u.password is not None or u.query or u.fragment or \
                u.scheme not in (("https", "http") if allow_http else ("https",)) or \
                u.hostname.endswith(".") or "%" in u.netloc or (port is not None and not 0 < port < 65536):
            raise ValueError
        return u
    except ValueError:
        raise ConfigurationError("endpoint requires HTTPS, a hostname and no credentials/query/fragment") from None


def _area(bounds):
    if not isinstance(bounds, (tuple, list)) or len(bounds) != 4 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in bounds):
        raise ValueError("bounds must be west, south, east, north")
    w, s, e, n = map(float, bounds)
    if not (-180 <= w < e <= 180 and -85 <= s < n <= 85):
        raise ValueError("unsupported bounds")
    # Conservative geographic metres; use the largest longitude scale in the box.
    closest_lat = 0.0 if s <= 0 <= n else min(abs(s), abs(n))
    if (e - w) * 111320 * math.cos(math.radians(closest_lat)) > 505 or (n - s) * 111320 > 505:
        raise ValueError("manual OSM selections are limited to approximately 500 x 500 metres")
    return w, s, e, n


def _known(values, config):
    ids = set()
    for value in values:
        identity = bo.normalize_osm_id(value)
        if identity is None:
            raise ValueError("known IDs must be OSM ways or relations")
        digits = identity.rsplit(":", 1)[1]
        if len(digits) > 19 or int(digits) > 2**63 - 1:
            raise ValueError("OSM identity exceeds supported integer range")
        ids.add(identity)
        if len(ids) > config.max_known_ids:
            raise ResourceLimit("too many known identities")
    return tuple(sorted(ids))


def query(bounds, *, config=Config(), known_ids=()):
    """Complete dependencies, building associations and a completion sentinel; no cropped output."""
    w, s, e, n = _area(bounds)
    bbox = f"({s:.9f},{w:.9f},{n:.9f},{e:.9f})"
    selections = [f'way["building"]{bbox};', f'rel["type"="multipolygon"]["building"]{bbox};',
                  f'way["building:part"]{bbox};', f'rel["type"="multipolygon"]["building:part"]{bbox};',
                  f'rel["type"="building"]{bbox};']
    ids = _known(known_ids, config)
    for kind, selector in (("way", "way"), ("relation", "rel")):
        values = [i.rsplit(":", 1)[1] for i in ids if i.split(":")[1] == kind]
        if values:
            selections.append(f'{selector}(id:{",".join(values)});')
    return (f"[out:json][timeout:{config.query_timeout_s}][maxsize:{config.query_memory_bytes}];"
            f'({"".join(selections)})->.seed;'
            '(.seed;rel(bw.seed)["type"="building"];rel(br.seed)["type"="building"];)->.selected;'
            '(.selected;.selected >>;);out meta;out count;')


def _time(value):
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return dt.astimezone(timezone.utc)


def _stamp(dt):
    return dt.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Issue:
    feature_id: str | None
    code: str


@dataclass(frozen=True)
class Snapshot:
    bounds: tuple
    buildings: tuple
    parts: tuple
    issues: tuple
    replication_timestamp: str | None
    element_count: int
    stale: bool = False

    @property
    def empty(self):
        return not self.buildings and not self.parts and not any(
            i.code not in ("missing_replication_timestamp",) for i in self.issues)

    def refresh_info(self, baseline_release, *, metadata=None):
        """Bridge to Stage 2; never invent a source timestamp from the local fetch time."""
        from buildingoverlaystore import RefreshInfo
        if self.stale:
            raise StaleSnapshot("stale OSM snapshot cannot be stored")
        if self.replication_timestamp is None:
            raise IncompleteResponse("replication timestamp required before persistence")
        return RefreshInfo(self.bounds, self.replication_timestamp, (dict(OSM_SOURCE),), baseline_release,
                           dict(metadata or {}, retrieval_issues=[vars(i) for i in self.issues]))


def _json(body, config):
    if not isinstance(body, bytes) or len(body) > config.max_response_bytes:
        raise ResourceLimit("OSM response exceeds byte limit")
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError("duplicate JSON key")
            result[k] = v
        return result
    def constant(value):
        raise ValueError("nonfinite JSON constant")
    try:
        return json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise IncompleteResponse("malformed OSM JSON response") from None


def _positive(value):
    return type(value) is int and 0 < value <= 2**63 - 1


def _tagged(element, key):
    return element.get("tags", {}).get(key, "").lower() not in ("", "no", "false", "0", "demolished", "construction", "proposed")


def _rings(segments):
    """Stitch by node identity. Branches/open chains are unsupported, never guessed."""
    unused = {i: tuple(nodes) for i, nodes in segments}
    out = []
    while unused:
        chain = list(unused.pop(min(unused)))
        while chain[0] != chain[-1]:
            choices = [(i, nodes if nodes[0] == chain[-1] else tuple(reversed(nodes)))
                       for i, nodes in unused.items() if chain[-1] in (nodes[0], nodes[-1])]
            if len(choices) != 1:
                return None
            i, nodes = choices[0]
            del unused[i]
            chain.extend(nodes[1:])
        out.append(chain)
    return out


def _geometry(element, elements):
    def way_nodes(way):
        return [[elements[("node", i)]["lon"], elements[("node", i)]["lat"]] for i in way["nodes"]]
    if element["type"] == "way":
        return {"type": "Polygon", "coordinates": [way_nodes(element)]}
    members = element["members"]
    if element.get("tags", {}).get("type") != "multipolygon" or any(
            m["type"] != "way" or m.get("role", "") not in ("", "outer", "inner") for m in members):
        return {"type": "Unsupported", "coordinates": [way_nodes(elements[(m["type"], m["ref"])])
                for m in members if m["type"] == "way"]}
    grouped = {}
    for role in ("outer", "inner"):
        segments = [(m["ref"], elements[("way", m["ref"])]["nodes"]) for m in members
                    if (m.get("role") or "outer") == role]
        if len({i for i, _ in segments}) != len(segments):
            return {"type": "Unsupported", "coordinates": [way_nodes(elements[("way", i)]) for i, _ in segments]}
        stitched = _rings(segments)
        if stitched is None:
            return {"type": "Unsupported", "coordinates": [way_nodes(elements[("way", i)]) for i, _ in segments]}
        grouped[role] = [[[elements[("node", i)]["lon"], elements[("node", i)]["lat"]] for i in ring]
                         for ring in stitched]
    polygons = [[ring] for ring in grouped["outer"]]
    if not polygons:
        return {"type": "Unsupported", "coordinates": grouped["inner"]}
    for hole in grouped["inner"]:
        child, error = bo._validated({"type": "Polygon", "coordinates": [hole]})
        parents = [p for p in polygons if error is None and bo._validated({"type": "Polygon", "coordinates": [p[0]]})[1] is None
                   and bo._contained(child, bo._validated({"type": "Polygon", "coordinates": [p[0]]})[0])]
        if len(parents) != 1:
            return {"type": "Unsupported", "coordinates": grouped["outer"] + grouped["inner"]}
        parents[0].append(hole)
    return {"type": "Polygon", "coordinates": polygons[0]} if len(polygons) == 1 else \
        {"type": "MultiPolygon", "coordinates": polygons}


_NUMBER = r"([0-9]+(?:\.[0-9]+)?)"


def _number(value, length=False):
    value = value.strip().lower()
    m = re.fullmatch(_NUMBER + (r"\s*(m|metres?|meters?|ft|feet|foot|')?" if length else ""), value)
    if m:
        n = float(m[1])
        return n * 0.3048 if length and m[2] in ("ft", "feet", "foot", "'") else n
    if length:
        m = re.fullmatch(_NUMBER + r"\s*(?:'|ft)\s*" + _NUMBER + r'\s*(?:"|in)', value)
        if m and float(m[2]) < 12:
            return float(m[1]) * 0.3048 + float(m[2]) * 0.0254
    return None


def _attributes(element, issues, fid):
    tags, attrs = element.get("tags", {}), {}
    numeric = {"height": ("height", True), "building:levels": ("num_floors", False),
               "min_height": ("min_height", True), "building:min_level": ("min_floor", False),
               "roof:height": ("roof_height", True), "building:levels:underground": ("num_floors_underground", False)}
    for key, (target, length) in numeric.items():
        if key in tags:
            value = _number(tags[key], length)
            if value is None or not math.isfinite(value) or value < 0 or (target in ("height", "num_floors") and value == 0):
                issues.append(Issue(fid, f"invalid_attribute:{key}"))
            else:
                attrs[target] = value
    text = {"name": "name", "roof:shape": "roof_shape", "roof:material": "roof_material",
            "building:material": "facade_material"}
    for key, target in text.items():
        if tags.get(key):
            attrs[target] = tags[key]
    if tags.get("building", "yes") not in ("yes", "no"):
        attrs["class"] = tags["building"]
    if "roof:orientation" in tags:
        if tags["roof:orientation"] in ("along", "across"):
            attrs["roof_orientation"] = tags["roof:orientation"]
        else:
            issues.append(Issue(fid, "invalid_attribute:roof:orientation"))
    if "roof:direction" in tags:
        directions = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}
        direction = directions.get(tags["roof:direction"].upper(), _number(tags["roof:direction"]))
        if direction is not None and math.isfinite(direction) and 0 <= direction < 360:
            attrs["roof_direction"] = direction
        else:
            issues.append(Issue(fid, "invalid_attribute:roof:direction"))
    for keys, target in ((("roof:colour", "roof:color"), "roof_color"),
                         (("building:colour", "building:color"), "facade_color")):
        value = next((tags[k] for k in keys if k in tags), None)
        if value is not None:
            if re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", value):
                attrs[target] = value.lower()
            else:
                issues.append(Issue(fid, f"invalid_attribute:{keys[0]}"))
    return attrs


def parse(body, bounds, *, config=Config(), known_ids=(), not_before=None, now=None):
    """Parse this component's complete-dependency/count output into Stage 1 Feature records."""
    bounds, known = _area(bounds), _known(known_ids, config)
    doc = _json(body, config)
    if not isinstance(doc, dict) or doc.get("version") != 0.6 or not isinstance(doc.get("elements"), list):
        raise IncompleteResponse("unexpected Overpass JSON schema")
    if "remark" in doc:
        raise OverpassError("Overpass reported an error; the entire snapshot was discarded")
    rows = doc["elements"]
    if len(rows) > config.max_elements + 1:
        raise ResourceLimit("too many OSM elements")
    if not rows or not isinstance(rows[-1], dict) or rows[-1].get("type") != "count":
        raise IncompleteResponse("missing terminal completion count")
    elements, counts, vertices = {}, Counter(), 0
    try:
        for row in rows[:-1]:
            if not isinstance(row, dict) or row.get("type") not in ("node", "way", "relation") or not _positive(row.get("id")):
                raise ValueError
            kind, ident = row["type"], row["id"]
            if (kind, ident) in elements or not _positive(row.get("version")):
                raise ValueError
            _time(row["timestamp"])
            tags = row.get("tags", {})
            if not isinstance(tags, dict) or len(tags) > 256 or any(not isinstance(k, str) or not isinstance(v, str)
                    or len(k) > 256 or len(v) > 4096 for k, v in tags.items()):
                raise ValueError
            if kind == "node":
                if any(isinstance(row[k], bool) or not isinstance(row[k], (int, float)) or not math.isfinite(row[k])
                       for k in ("lon", "lat")) or not -180 <= row["lon"] <= 180 or not -90 <= row["lat"] <= 90:
                    raise ValueError
            elif kind == "way":
                if not isinstance(row.get("nodes"), list) or len(row["nodes"]) < 2 or not all(_positive(i) for i in row["nodes"]):
                    raise ValueError
                vertices += len(row["nodes"])
            else:
                if not isinstance(row.get("members"), list):
                    raise ValueError
                vertices += len(row["members"])
                for m in row["members"]:
                    if not isinstance(m, dict) or m.get("type") not in ("node", "way", "relation") or \
                            not _positive(m.get("ref")) or not isinstance(m.get("role", ""), str):
                        raise ValueError
            if vertices > config.max_vertices:
                raise ResourceLimit("too many OSM geometry references")
            elements[kind, ident] = row
            counts[kind] += 1
        count_tags = rows[-1]["tags"]
        expected = {"nodes": counts["node"], "ways": counts["way"], "relations": counts["relation"], "total": len(elements)}
        if any(not isinstance(count_tags[k], str) or not count_tags[k].isdigit() or int(count_tags[k]) != v
               for k, v in expected.items()):
            raise ValueError
        for (kind, _), row in elements.items():
            deps = [("node", n) for n in row["nodes"]] if kind == "way" else \
                [(m["type"], m["ref"]) for m in row["members"]] if kind == "relation" else []
            if any(d not in elements for d in deps):
                raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise IncompleteResponse("invalid element metadata, completion counts or missing geometry dependencies") from None
    issues = []
    replication = doc.get("osm3s", {}).get("timestamp_osm_base") if isinstance(doc.get("osm3s", {}), dict) else None
    try:
        replication = _stamp(_time(replication)) if replication is not None else None
    except ValueError:
        raise IncompleteResponse("invalid replication timestamp") from None
    stale = False
    if replication is None:
        issues.append(Issue(None, "missing_replication_timestamp"))
    else:
        stamp = _time(replication)
        if any(_time(row["timestamp"]) > stamp for row in elements.values()):
            raise IncompleteResponse("element metadata is newer than the replication snapshot")
        stale = not_before is not None and stamp < _time(not_before)
        if now is not None:
            age = (_time(now) - stamp).total_seconds()
            stale |= age > config.max_snapshot_age_s
            if age < -300:
                raise IncompleteResponse("replication timestamp is ahead of the local clock")
        if stale:
            issues.append(Issue(None, "stale_replication_timestamp"))
    relations = [r for (kind, _), r in sorted(elements.items()) if kind == "relation"]
    suppressed = set()
    for r in relations:
        if r.get("tags", {}).get("type") == "multipolygon" and (_tagged(r, "building") or _tagged(r, "building:part")):
            suppressed.update(("way", m["ref"]) for m in r["members"] if m["type"] == "way" and m.get("role", "") in ("", "outer"))
    features, part_keys, emitted_vertices = {}, set(), 0
    for key, row in sorted(elements.items()):
        if key[0] == "node" or key in suppressed:
            continue
        fid = f"osm:{key[0]}:{key[1]}"
        is_part = _tagged(row, "building:part")
        if not (_tagged(row, "building") or is_part or fid in known):
            continue
        if len(features) >= config.max_features:
            raise ResourceLimit("too many building/part features")
        geometry = _geometry(row, elements)
        pending_coords = [geometry["coordinates"]]
        while pending_coords:
            coords = pending_coords.pop()
            if len(coords) == 2 and all(isinstance(v, (int, float)) for v in coords):
                emitted_vertices += 1
            else:
                pending_coords.extend(coords)
            if emitted_vertices > config.max_vertices:
                raise ResourceLimit("expanded OSM geometry exceeds vertex limit")
        _, unsupported = bo._validated(geometry)
        if unsupported:
            issues.append(Issue(fid, unsupported))
        attrs = _attributes(row, issues, fid)
        source = dict(OSM_SOURCE, record_id=f'{"w" if key[0] == "way" else "r"}{key[1]}@{row["version"]}',
                      update_time=_stamp(_time(row["timestamp"])), snapshot_timestamp=replication, tags=dict(row.get("tags", {})))
        if key[0] == "relation":
            source["members"] = [dict(m) for m in row["members"]]
        else:
            source["nodes"] = list(row["nodes"])
        pending, visited, times = [key], set(), []
        while pending:
            dep = pending.pop()
            if dep in visited:
                continue
            visited.add(dep)
            item = elements[dep]
            times.append(_time(item["timestamp"]))
            if dep[0] == "way":
                pending.extend(("node", n) for n in item["nodes"])
            elif dep[0] == "relation":
                pending.extend((m["type"], m["ref"]) for m in item["members"])
        source["geometry_timestamp"] = _stamp(max(times))
        features[fid] = bo.Feature(fid, geometry, attrs, (source,), (fid,),
                                  status="building" if _tagged(row, "building") or is_part else "retagged",
                                  attribute_sources={k: (dict(source),) for k in attrs})
        if is_part:
            part_keys.add(fid)
    # Only explicit type=building relations can assert a complete parent/part set.
    parents = {k: set() for k in part_keys}
    complete, incomplete, declared = set(), set(), {}
    for r in relations:
        if r.get("tags", {}).get("type") != "building":
            continue
        outlines = [f'osm:{m["type"]}:{m["ref"]}' for m in r["members"] if m.get("role") == "outline"]
        parts = [f'osm:{m["type"]}:{m["ref"]}' for m in r["members"] if m.get("role") == "part"]
        valid = len(outlines) == 1 and outlines[0] in features and outlines[0] not in part_keys and \
            bool(parts) and all(p in part_keys for p in parts) and len(set(parts)) == len(parts)
        if valid:
            complete.add(outlines[0])
            if outlines[0] in declared and declared[outlines[0]] != set(parts):
                incomplete.add(outlines[0])
                issues.append(Issue(outlines[0], "conflicting_building_associations"))
            declared[outlines[0]] = set(parts)
            association = {"record_id": f'r{r["id"]}@{r["version"]}', "update_time": _stamp(_time(r["timestamp"])),
                           "tags": dict(r.get("tags", {})), "members": [dict(m) for m in r["members"]]}
            features[outlines[0]].sources[0].setdefault("building_associations", []).append(association)
            for p in parts:
                parents[p].add(outlines[0])
        else:
            incomplete.update(k for k in outlines if k in features)
            issues.append(Issue(f'osm:relation:{r["id"]}', "unsupported_building_association"))
    building_keys = set(features) - part_keys
    for p in sorted(part_keys):
        if not parents[p]:
            child, error = bo._validated(features[p].geometry)
            if not error:
                for k in sorted(building_keys):
                    parent, error = bo._validated(features[k].geometry)
                    if not error and bo._contained(child, parent):
                        parents[p].add(k)
        if len(parents[p]) == 1:
            parent = next(iter(parents[p]))
            features[p] = replace(features[p], parent_id=parent)
            features[parent].attrs["has_parts"] = True
            if p not in declared.get(parent, set()):
                incomplete.add(parent)
        else:
            complete.difference_update(parents[p])
            issues.append(Issue(p, "unknown_or_ambiguous_part_parent"))
            # An unsupported/orphan part is not proof that an enclosing parent's
            # part set is empty. Block speculative plain-footprint replacements.
            part_box = bo._bbox(features[p].geometry)
            for k in building_keys:
                parent_box = bo._bbox(features[k].geometry)
                if part_box is None or parent_box is None or (
                        part_box[0] < parent_box[2] and part_box[2] > parent_box[0] and
                        part_box[1] < parent_box[3] and part_box[3] > parent_box[1]):
                    incomplete.add(k)
                    features[k].attrs["has_parts"] = True
                    issues.append(Issue(k, "uncertain_part_set"))
    for k in building_keys:
        features[k] = replace(features[k], parts_complete=k in complete and k not in incomplete)
    for identity in sorted(set(known) - set(features)):
        issues.append(Issue(identity, "known_identity_not_returned_no_deletion_evidence"))
    return Snapshot(bounds, tuple(features[k] for k in sorted(building_keys)),
                    tuple(features[k] for k in sorted(part_keys)),
                    tuple(sorted(set(issues), key=lambda i: (i.feature_id or "", i.code))), replication, len(elements), bool(stale))


class BoundedPOST:
    """One fresh verified HTTP(S) connection; no redirects, compression or retries."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock

    def post(self, endpoint, body, headers, *, timeout, total_timeout, max_bytes):
        u = urlsplit(endpoint)
        end = self.clock() + total_timeout
        kwargs = {"timeout": min(timeout, total_timeout)}
        if u.scheme == "https":
            context = netfetch.windows_ssl_context()
            if context is not None:
                kwargs["context"] = context
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        connection = cls(u.hostname, port=u.port, **kwargs)
        response = None
        def remaining():
            left = end - self.clock()
            if left <= 0:
                raise TimeoutError("OSM response deadline exceeded")
            if connection.sock is not None:
                connection.sock.settimeout(min(timeout, left))
        try:
            connection.connect()
            remaining()
            connection.request("POST", u.path or "/", body=body, headers=dict(headers, Connection="close"))
            remaining()
            response = connection.getresponse()
            rh = {k.lower(): v for k, v in response.getheaders()}
            if response.status != 200:
                return response.status, rh, b""  # honour Retry-After without reading an error-page body
            if rh.get("content-encoding", "identity").lower() != "identity":
                raise IncompleteResponse("compressed OSM responses are unsupported")
            length = rh.get("content-length")
            if length is not None:
                if not length.isdigit():
                    raise IncompleteResponse("invalid response Content-Length")
                length = int(length)
                if length > max_bytes:
                    raise ResourceLimit("OSM response exceeds byte limit")
            data = bytearray()
            while True:
                remaining()
                chunk = response.read1(min(65536, max_bytes + 1 - len(data)))
                remaining()
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ResourceLimit("OSM response exceeds byte limit")
            if length is not None and len(data) != length:
                raise IncompleteResponse("truncated response body")
            return response.status, rh, bytes(data)
        finally:
            try:
                if response is not None:
                    response.close()
            finally:
                connection.close()


class Coordinator:
    """Process-local manual-request coordinator; share one in the future main-server caller."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.Lock()
        self.next_request = float("-inf")
        self.areas = []
        self.latest = {}

    @contextmanager
    def request(self, host, bounds, config):
        if not self.lock.acquire(blocking=False):
            raise Throttled("another manual OSM request is in progress")
        try:
            now = self.clock()
            if now < self.next_request:
                raise Throttled("manual OSM request spacing/Retry-After", self.next_request - now)
            self.areas = [(h, b, until) for h, b, until in self.areas if until > now]
            waits = [until - now for h, b, until in self.areas if h == host and
                     b[0] < bounds[2] and b[2] > bounds[0] and b[1] < bounds[3] and b[3] > bounds[1]]
            if waits:
                raise Throttled("same or overlapping OSM area is cooling down", max(waits))
            if len(self.areas) >= 256 or (host not in self.latest and len(self.latest) >= 32):
                raise ResourceLimit("manual OSM coordinator tracking limit reached")
            self.next_request = now + config.min_spacing_s
            self.areas.append((host, bounds, now + config.area_cooldown_s))
            yield
        finally:
            self.lock.release()


_COORDINATOR = Coordinator()


def _retry_after(value, wall):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = email.utils.parsedate_to_datetime(value).timestamp() - wall
        except (TypeError, ValueError, OverflowError, AttributeError):
            return 0.0
    return max(0.0, seconds) if math.isfinite(seconds) else 0.0


class Client:
    def __init__(self, config=Config(), *, fetcher=None, transport=None, coordinator=None, wall=time.time):
        self.config = replace(config, headers=dict(config.headers))
        self.fetcher = fetcher if fetcher is not None else netfetch.default_fetcher()
        self.transport = transport if transport is not None else BoundedPOST()
        self.coordinator = coordinator if coordinator is not None else _COORDINATOR
        self.wall = wall

    def fetch(self, bounds, *, manual, known_ids=(), not_before=None):
        config = self.config
        if not config.enabled or config.endpoint is None or manual is not True:
            raise ConfigurationError("OSM retrieval requires explicit enablement, endpoint and manual=True")
        u = _endpoint(config.endpoint, config.allow_insecure_http)
        bounds, known = _area(bounds), _known(known_ids, config)
        if not_before is not None:
            _time(not_before)
        policy = self.fetcher.policy(u.hostname)
        if policy.denied:
            raise netfetch.PolicyDenied("configured OSM host is denied by netfetch")
        if self.fetcher.offline_flag:
            raise netfetch.Offline("manual OSM retrieval is unavailable offline")
        # Respect the existing endpoint's socket timeout as well as the explicit caller's.
        timeout = min(config.timeout_s, policy.timeout)
        bounded_config = replace(config, query_timeout_s=max(1, min(config.query_timeout_s, int(timeout) - 1)))
        body = urlencode({"data": query(bounds, config=bounded_config, known_ids=known)}).encode("ascii")
        with self.coordinator.request(u.hostname, bounds, config):
            cutoff = self.coordinator.latest.get(u.hostname)
            if not_before is not None and (cutoff is None or _time(not_before) > _time(cutoff)):
                cutoff = not_before
            try:
                with self.fetcher.session(config.endpoint, kind="demand") as session:
                    headers = dict(session.headers, **config.headers)
                    headers.update({"User-Agent": netfetch.USER_AGENT, "Accept": "application/json",
                                    "Content-Type": "application/x-www-form-urlencoded", "Accept-Encoding": "identity"})
                    status, rh, data = self.transport.post(config.endpoint, body, headers, timeout=timeout,
                                                           total_timeout=config.total_timeout_s, max_bytes=config.max_response_bytes)
                    rh = {k.lower(): v for k, v in rh.items()}
                    session.status, session.nbytes = status, len(data)
                    if status != 200:
                        if status in (429, 503):
                            wait = _retry_after(rh.get("retry-after"), self.wall())
                            self.coordinator.next_request = max(self.coordinator.next_request, self.coordinator.clock() + wait)
                        raise HTTPFailure(status, rh)
                    result = parse(data, bounds, config=config, known_ids=known, not_before=cutoff,
                                   now=_stamp(datetime.fromtimestamp(self.wall(), timezone.utc)))
            except (IncompleteResponse, ResourceLimit, HTTPFailure, netfetch.FetchError):
                raise
            except (OSError, http.client.HTTPException):
                raise RetrievalError("configured OSM request failed or timed out") from None
            if result.replication_timestamp is not None and not result.stale:
                self.coordinator.latest[u.hostname] = result.replication_timestamp
            return result
