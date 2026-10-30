"""Persistent local building overlays; no network, cache invalidation or game integration.

An explicit data directory contains building-overlays/v1/{state.json,previous.json,
writer.lock,objects/<sha256>.json}. Objects are immutable full *overlay* snapshots:
overrides/additions, their complete part sets, unresolved conflicts and refresh metadata.
Overture features are never written here; only their fingerprints/bounds are retained.

All readers/writers take an OS lock (also across processes). A commit fsyncs a new
object and backup manifest before atomically replacing state.json. Orphan objects
and temporary files are never treated as committed data. Corruption raises an
explicit error; recover() can restore the validated previous manifest. No eviction
or pruning: resource limits fail closed instead of deleting edits/history.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

import buildingoverlay as bo


SCHEMA = 1
_HEX = re.compile(r"[0-9a-f]{64}")


class StorageError(IOError):
    pass


class CorruptStorage(StorageError):
    pass


class ResourceLimit(StorageError):
    pass


class LockTimeout(StorageError):
    pass


class BaselineMismatch(StorageError):
    pass


class StalePreview(StorageError):
    """A reviewed state or decision set no longer matches; obtain a new preview."""
    pass


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_revisions: int = 128
    max_entries: int = 2048
    max_input_features: int = 512
    max_conflicts: int = 2048
    max_refreshes: int = 128


@dataclass(frozen=True)
class RefreshInfo:
    bounds: tuple                 # west, south, east, north; no antimeridian crossing
    source_timestamp: str         # source snapshot time, timezone required
    provenance: tuple
    baseline_release: str
    metadata: dict | None = None  # e.g. caller's export name; no automatic clock/network access


@dataclass(frozen=True)
class Entry:
    feature: bo.Feature
    parts: tuple
    baseline_digest: str | None  # None = addition; otherwise an override of that exact baseline
    bounds: tuple                # union of original and effective full geometry bounds
    source_timestamp: str


@dataclass(frozen=True)
class Snapshot:
    revision: str | None
    enabled: bool
    baseline_release: str | None
    entries: tuple
    conflicts: tuple
    refreshes: tuple
    history: tuple
    affected_bounds: tuple | None

    @property
    def overrides(self):
        return tuple(e for e in self.entries if e.baseline_digest is not None)

    @property
    def additions(self):
        return tuple(e for e in self.entries if e.baseline_digest is None)


@dataclass(frozen=True)
class Commit:
    revision: str
    merge_result: bo.MergeResult
    changed: bool
    affected_bounds: tuple | None
    state_token: str | None = field(default=None,compare=False)


@dataclass(frozen=True)
class AreaRevision:
    revision: str
    geometry_digest: str
    feature_ids: tuple


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _bounds(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4 or \
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
        raise ValueError("bounds must be four finite numbers: west, south, east, north")
    w, s, e, n = value
    if not (-180 <= w < e <= 180 and -85 <= s < n <= 85):
        raise ValueError("bounds outside supported geography")
    return tuple(float(v) for v in value)


def _intersects(a, b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def _union(boxes):
    boxes = [b for b in boxes if b is not None]
    return None if not boxes else (min(b[0] for b in boxes), min(b[1] for b in boxes),
                                   max(b[2] for b in boxes), max(b[3] for b in boxes))


def _wire(f):
    d = bo._clean(asdict(f))
    # Preserve unsupported/rejected geometry completely, canonicalize valid rings.
    ring, error = bo._validated(f.geometry)
    if error is None:
        d["geometry"] = {"type": "Polygon", "coordinates": [[list(p) for p in ring + ring[:1]]]}
    d["sources"] = list(bo._sources(f.sources))
    d["osm_ids"] = list(bo._identities(f))
    d["attribute_sources"] = {k: list(bo._sources(v)) for k, v in f.attribute_sources.items()}
    for k in ("cx", "cy", "has_parts", "building_id"):
        d["attribute_sources"].pop(k, None)  # derived fields acquire no new provenance on a repeat merge
    return d


def _feature(d):
    fields = {"feature_id", "geometry", "attrs", "sources", "osm_ids", "parent_id", "status",
              "parts_complete", "attribute_sources"}
    if not isinstance(d, dict) or set(d) != fields or not isinstance(d["feature_id"], str) or \
            not 0 < len(d["feature_id"]) <= 512 or not isinstance(d["attrs"], dict) or \
            not isinstance(d["attribute_sources"], dict) or type(d["parts_complete"]) is not bool or \
            d["status"] not in ("building", "deleted", "retagged") or \
            (d["geometry"] is not None and not isinstance(d["geometry"], dict)) or \
            (d["parent_id"] is not None and not isinstance(d["parent_id"], str)):
        raise ValueError("invalid serialized feature")
    if not isinstance(d["sources"], list) or any(not isinstance(s, dict) for s in d["sources"]) or \
            not isinstance(d["osm_ids"], list) or any(bo.normalize_osm_id(i) != i for i in d["osm_ids"]):
        raise ValueError("invalid serialized source identities")
    if any(not isinstance(v, list) or any(not isinstance(s, dict) for s in v)
           for v in d["attribute_sources"].values()):
        raise ValueError("invalid attribute provenance")
    return bo.Feature(**dict(d, sources=tuple(d["sources"]), osm_ids=tuple(d["osm_ids"]),
                             attribute_sources={k: tuple(v) for k, v in d["attribute_sources"].items()}))


def _fingerprint(f, parts):
    return _hash({"feature": _wire(f), "parts": [_wire(p) for p in sorted(parts, key=lambda p: p.feature_id)]})


def _entry_wire(e):
    return {"feature": _wire(e.feature), "parts": [_wire(p) for p in e.parts],
            "baseline_digest": e.baseline_digest, "bounds": list(e.bounds), "source_timestamp": e.source_timestamp}


def _dependency_wire(e):
    return {k: v for k, v in _entry_wire(e).items() if k != "source_timestamp"}


def _metadata(info):
    bounds = _bounds(info.bounds)
    if max((bounds[2] - bounds[0]) * 111320 * math.cos(math.radians((bounds[1] + bounds[3]) / 2)),
           (bounds[3] - bounds[1]) * 110540) > 10_000:
        raise ValueError("refresh extent exceeds 10 km")
    if not isinstance(info.baseline_release, str) or not 0 < len(info.baseline_release) <= 128:
        raise ValueError("baseline_release is required")
    if not isinstance(info.source_timestamp, str):
        raise ValueError("source_timestamp is required")
    dt = datetime.fromisoformat(info.source_timestamp.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("source_timestamp requires a timezone")
    if not info.provenance or any(not isinstance(p, dict) for p in info.provenance):
        raise ValueError("refresh provenance is required")
    if info.metadata is not None and not isinstance(info.metadata, dict):
        raise ValueError("metadata must be a JSON object")
    value = {"bounds": list(bounds), "source_timestamp": dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
             "provenance": list(bo._sources(info.provenance)), "baseline_release": info.baseline_release,
             "metadata": deepcopy(info.metadata or {})}
    _bytes(value)                  # do not silently sanitize invalid metadata
    return value


def _changed_bounds(old, new):
    a, b = ({e.feature.feature_id: e for e in entries} for entries in (old, new))
    changed = [k for k in a.keys() | b.keys()
               if k not in a or k not in b or _dependency_wire(a[k]) != _dependency_wire(b[k])]
    return _union([e.bounds for k in changed for e in (a.get(k), b.get(k)) if e])


class OverlayStore:
    """Explicit local storage directory; overlays start disabled. No baseline filesystem access."""

    def __init__(self, directory, *, limits=Limits(), lock_timeout=5.0):
        if any(type(v) is not int or v <= 0 for v in asdict(limits).values()):
            raise ValueError("storage limits must be positive integers")
        if not math.isfinite(lock_timeout) or lock_timeout <= 0:
            raise ValueError("lock_timeout must be finite and positive")
        directory = Path(directory).absolute()
        if ".." in directory.parts:
            raise ValueError("storage directory cannot contain '..'")
        self.root = directory / "building-overlays" / "v1"
        self.directory = directory
        self.objects = self.root / "objects"
        self.limits, self.lock_timeout = limits, lock_timeout
        for path in (directory, directory / "building-overlays", self.root, self.objects):
            self._no_link(path)
            path.mkdir(parents=True, exist_ok=True)
        with self._locked():
            if not (self.root / "state.json").exists():
                if not (self.root / "previous.json").exists() and not any(self.objects.iterdir()):
                    self._atomic_write(self.root / "state.json", self._document(
                        {"head": None, "enabled": False, "history": [], "affected_bounds": None}))
                # Existing incomplete stores stay incomplete so load() fails and recover() is possible.

    @staticmethod
    def _no_link(path):
        try:
            st = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise CorruptStorage(f"links/reparse points are unsupported in overlay storage: {path.name}")

    def _safe(self, path):
        if not path.is_relative_to(self.root):
            raise CorruptStorage("invalid storage path")
        for directory in (self.directory, self.root.parent, self.root):
            self._no_link(directory)
        for p in reversed((path,) + tuple(path.parents)[:len(path.relative_to(self.root).parts) - 1]):
            self._no_link(p)
        return path

    @contextmanager
    def _locked(self):
        path = self._safe(self.root / "writer.lock")
        with path.open("a+b") as f:
            if f.seek(0, os.SEEK_END) == 0:
                f.write(b"\0")
                f.flush()
            end = time.monotonic() + self.lock_timeout
            while True:
                try:
                    f.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= end:
                        raise LockTimeout("overlay storage is busy") from e
                    time.sleep(min(0.02, max(0, end - time.monotonic())))
            try:
                yield
            finally:
                f.seek(0)
                if os.name == "nt":
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _document(self, payload):
        data = _bytes({"schema": SCHEMA, "payload": payload, "checksum": _hash(payload)})
        if len(data) > self.limits.max_file_bytes:
            raise ResourceLimit("overlay document exceeds file limit")
        return data

    def _atomic_write(self, path, data):
        self._safe(path)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("xb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            if os.name != "nt":
                fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _read(self, path):
        def pairs(items):
            d = {}
            for k, v in items:
                if k in d:
                    raise ValueError("duplicate JSON key")
                d[k] = v
            return d
        def constant(value):
            raise ValueError(f"invalid JSON constant: {value}")
        try:
            with self._safe(path).open("rb") as f:
                data = f.read(self.limits.max_file_bytes + 1)
            if len(data) > self.limits.max_file_bytes:
                raise ValueError("document exceeds limit")
            doc = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
            if not isinstance(doc, dict) or set(doc) != {"schema", "payload", "checksum"} or \
                    type(doc["schema"]) is not int or doc["schema"] != SCHEMA or \
                    not isinstance(doc["payload"], dict) or _hash(doc["payload"]) != doc["checksum"]:
                raise ValueError("invalid schema/checksum")
            return doc["payload"]
        except (OSError, ValueError, TypeError, RecursionError) as e:
            if isinstance(e, CorruptStorage):
                raise
            raise CorruptStorage(f"cannot validate {path.name}: {e}") from e

    def _object(self, revision):
        if not isinstance(revision, str) or not _HEX.fullmatch(revision):
            raise CorruptStorage("invalid revision ID")
        payload = self._read(self.objects / f"{revision}.json")
        if _hash(payload) != revision:
            raise CorruptStorage("revision filename/content mismatch")
        return payload

    def _snapshot(self, manifest):
        try:
            required = {"head", "enabled", "history", "affected_bounds"}
            if set(manifest) not in (required,required|{"incarnation"}) or type(manifest["enabled"]) is not bool:
                raise ValueError("invalid state manifest")
            if "incarnation" in manifest and (not isinstance(manifest["incarnation"],str) or
                                               not re.fullmatch("[0-9a-f]{32}",manifest["incarnation"])):
                raise ValueError("invalid state incarnation")
            history, head = manifest["history"], manifest["head"]
            if not isinstance(history, list) or len(history) > self.limits.max_revisions or \
                    any(not isinstance(h, str) or not _HEX.fullmatch(h) for h in history) or len(set(history)) != len(history):
                raise ValueError("invalid revision history")
            affected = None if manifest["affected_bounds"] is None else _bounds(manifest["affected_bounds"])
            if head is None:
                if history:
                    raise ValueError("empty head with history")
                return Snapshot(None, manifest["enabled"], None, (), (), (), (), affected)
            if not history or head != history[-1]:
                raise ValueError("head must be the last history entry")
            payload = self._object(head)
            if set(payload) != {"parent", "baseline_release", "overrides", "additions", "conflicts", "refreshes"}:
                raise ValueError("invalid revision schema")
            if payload["parent"] is not None and (not isinstance(payload["parent"], str) or not _HEX.fullmatch(payload["parent"])):
                raise ValueError("invalid parent revision")
            if payload["parent"] != (history[-2] if len(history) > 1 else None):
                raise ValueError("revision parent does not match manifest history")
            if not isinstance(payload["baseline_release"], str) or not payload["baseline_release"]:
                raise ValueError("missing baseline release")
            entries, ids, part_ids = [], set(), set()
            if any(not isinstance(payload[k], list) for k in ("overrides", "additions")) or \
                    len(payload["overrides"]) + len(payload["additions"]) > self.limits.max_entries:
                raise ValueError("invalid entry collection/limit")
            for kind in ("overrides", "additions"):
                if not isinstance(payload[kind], list):
                    raise ValueError("invalid entry list")
                for row in payload[kind]:
                    if not isinstance(row, dict) or set(row) != {"feature", "parts", "baseline_digest", "bounds", "source_timestamp"}:
                        raise ValueError("invalid entry schema")
                    f = _feature(row["feature"])
                    parts = tuple(_feature(p) for p in row["parts"])
                    digest = row["baseline_digest"]
                    if (kind == "additions" and digest is not None) or (kind == "overrides" and
                            (not isinstance(digest, str) or not _HEX.fullmatch(digest))):
                        raise ValueError("invalid baseline fingerprint")
                    if f.feature_id in ids or f.parent_id is not None or bo._validated(f.geometry)[1] or f.status != "building":
                        raise ValueError("invalid persisted building")
                    for p in parts:
                        ring, error = bo._validated(p.geometry)
                        if p.feature_id in part_ids or p.parent_id != f.feature_id or error or p.status != "building" or \
                                not bo._contained(ring, bo._validated(f.geometry)[0]):
                            raise ValueError("invalid persisted parts")
                        part_ids.add(p.feature_id)
                    if bool(f.attrs.get("has_parts")) != bool(parts):
                        raise ValueError("inconsistent parent parts")
                    ids.add(f.feature_id)
                    coverage = _bounds(row["bounds"])
                    geometry_bounds = _union([bo._bbox(p.geometry) for p in (f,) + parts])
                    if not (coverage[0] <= geometry_bounds[0] and coverage[1] <= geometry_bounds[1] and
                            coverage[2] >= geometry_bounds[2] and coverage[3] >= geometry_bounds[3]):
                        raise ValueError("entry bounds do not contain its geometry")
                    if not isinstance(row["source_timestamp"], str) or \
                            datetime.fromisoformat(row["source_timestamp"].replace("Z", "+00:00")).tzinfo is None:
                        raise ValueError("invalid entry source timestamp")
                    entries.append(Entry(f, parts, digest, coverage, row["source_timestamp"]))
            if ids & part_ids or len(entries) + len(part_ids) > self.limits.max_entries:
                raise ValueError("entry collision/limit exceeded")
            conflicts, refreshes = payload["conflicts"], payload["refreshes"]
            if not isinstance(conflicts, dict) or len(conflicts) > self.limits.max_conflicts or \
                    not isinstance(refreshes, dict) or len(refreshes) > self.limits.max_refreshes:
                raise ValueError("invalid metadata collection/limit")
            for key, value in refreshes.items():
                if not isinstance(value, dict) or set(value) != {"info", "incoming", "incoming_parts"} or \
                        key != _hash(value) or _metadata(RefreshInfo(**value["info"])) != value["info"] or \
                        any(not isinstance(value[k], list) or len(value[k]) > self.limits.max_input_features
                            for k in ("incoming", "incoming_parts")):
                    raise ValueError("invalid refresh metadata")
            for key, value in conflicts.items():
                if not isinstance(value, dict) or set(value) != {"refresh_id", "bounds", "decision"} or \
                        key != _hash(value) or value["refresh_id"] not in refreshes:
                    raise ValueError("invalid conflict record")
                d = value["decision"]
                if not isinstance(d, dict) or set(d) != {"action", "incoming_id", "baseline_ids", "reason", "geometry_changed", "subject"} or \
                        d["action"] != "conflict" or d["subject"] not in ("building", "part") or \
                        not isinstance(d["incoming_id"], str) or not isinstance(d["reason"], str) or \
                        not isinstance(d["baseline_ids"], list) or any(not isinstance(v, str) for v in d["baseline_ids"]) or \
                        type(d["geometry_changed"]) is not bool or _bounds(value["bounds"]) != tuple(refreshes[value["refresh_id"]]["info"]["bounds"]):
                    raise ValueError("invalid conflict decision")
            return Snapshot(head, manifest["enabled"], payload["baseline_release"],
                            tuple(sorted(entries, key=lambda e: e.feature.feature_id)),
                            tuple(conflicts[k] for k in sorted(conflicts)), tuple(refreshes[k] for k in sorted(refreshes)),
                            tuple(history), affected)
        except (KeyError, TypeError, ValueError, RecursionError) as e:
            raise CorruptStorage(f"invalid overlay state: {e}") from e

    def _load(self):
        return self._snapshot(self._read(self.root / "state.json"))

    def load(self):
        """Read and validate a fresh snapshot, including changes made by other store instances."""
        with self._locked():
            return self._load()

    def _state_token(self):
        path = self._safe(self.root / "state.json")
        st = path.stat()
        return _hash([self._read(path), st.st_mtime_ns, st.st_ctime_ns, st.st_ino])

    def load_state(self):
        """Snapshot and manifest incarnation, read under one lock (also fences enable/disable ABA)."""
        with self._locked():
            return self._load(), self._state_token()

    def _quota(self, extra_bytes, *, new_object=False):
        total, count, visited = 0, 0, 0
        for directory in (self.root, self.objects):
            for path in directory.iterdir():
                visited += 1
                # Count zero-byte interrupted temporaries too; leave room for the
                # object and atomic manifest temporaries without deleting anything.
                if visited+int(new_object)+2 > 2*self.limits.max_revisions+16:
                    raise ResourceLimit("overlay filesystem entry budget reached; explicit safe cleanup is required")
                self._no_link(path)
                if path.is_file():
                    total += path.stat().st_size
                    count += directory == self.objects and path.suffix == ".json"
                    if total+extra_bytes > self.limits.max_total_bytes or count+int(new_object) > self.limits.max_revisions:
                        raise ResourceLimit("overlay storage quota reached; no history or edits were removed")
        if total + extra_bytes > self.limits.max_total_bytes or count + int(new_object) > self.limits.max_revisions:
            raise ResourceLimit("overlay storage quota reached; no history or edits were removed")

    def _publish(self, snapshot, head, history, enabled, affected):
        payload = {"head": head, "enabled": enabled, "history": list(history), "affected_bounds": affected}
        payload["incarnation"] = uuid.uuid4().hex
        data = self._document(payload)
        previous = self._document(self._read(self.root / "state.json"))
        self._quota(2 * (len(data) + len(previous)))
        self._atomic_write(self.root / "previous.json", previous)
        self._atomic_write(self.root / "state.json", data)

    @staticmethod
    def _apply(snapshot, baseline, baseline_parts, baseline_release, scope, enabled, identities=()):
        if enabled and snapshot.baseline_release is not None and snapshot.baseline_release != baseline_release:
            raise BaselineMismatch("overlay belongs to a different Overture release; explicit migration is required")
        buildings = {f.feature_id: deepcopy(f) for f in baseline}
        parts = {p.feature_id: deepcopy(p) for p in baseline_parts}
        if enabled:
            for entry in snapshot.entries:
                fid = entry.feature.feature_id
                if not _intersects(entry.bounds, scope) and fid not in buildings and \
                        not set(bo._identities(entry.feature)).intersection(identities):
                    continue
                if entry.baseline_digest is not None:
                    original = buildings.get(fid)
                    if original is None or _fingerprint(original, [p for p in parts.values() if p.parent_id == fid]) != entry.baseline_digest:
                        raise BaselineMismatch(f"original baseline building/parts missing or changed: {fid}")
                elif fid in buildings:
                    raise BaselineMismatch(f"addition now collides with baseline ID: {fid}")
                buildings[fid] = deepcopy(entry.feature)
                parts = {k: p for k, p in parts.items() if p.parent_id != fid}
                parts.update({p.feature_id: deepcopy(p) for p in entry.parts})
        return tuple(buildings[k] for k in sorted(buildings)), tuple(parts[k] for k in sorted(parts))

    def apply(self, baseline, *, baseline_release, bounds, baseline_parts=()):
        """Compose copies over a supplied original baseline; disabled storage returns that baseline."""
        scope = _bounds(bounds)
        baseline, baseline_parts = self._inputs(baseline), self._inputs(baseline_parts)
        with self._locked():
            snapshot = self._load()
            buildings, parts = self._apply(snapshot, baseline, baseline_parts, baseline_release, scope, snapshot.enabled)
            decisions = tuple(bo.Decision(**dict(c["decision"], baseline_ids=tuple(c["decision"]["baseline_ids"])))
                              for c in snapshot.conflicts if _intersects(c["bounds"], scope))
            return bo.MergeResult(buildings, parts, decisions)

    @staticmethod
    def compose(snapshot, baseline, *, baseline_release, bounds, baseline_parts=()):
        """Compose an already loaded snapshot without rereading disk (offline render jobs).

        Callers may select entries with dataclasses.replace; validation of each
        original fingerprint still uses exactly the same implementation as apply().
        """
        baseline, baseline_parts = tuple(baseline), tuple(baseline_parts)
        bo._unique(baseline, "snapshot baseline")
        bo._unique(baseline_parts, "snapshot baseline parts")
        scope = _bounds(bounds)
        buildings, parts = OverlayStore._apply(snapshot, baseline, baseline_parts, baseline_release,
                                               scope, snapshot.enabled)
        decisions = tuple(bo.Decision(**dict(c["decision"], baseline_ids=tuple(c["decision"]["baseline_ids"])))
                          for c in snapshot.conflicts if _intersects(c["bounds"], scope))
        return bo.MergeResult(buildings, parts, decisions)

    def _inputs(self, values):
        out = []
        for value in values:
            if len(out) >= self.limits.max_input_features:
                raise ResourceLimit("too many input features")
            if not isinstance(value, bo.Feature):
                raise ValueError("inputs must be buildingoverlay.Feature records")
            out.append(deepcopy(value))
        bo._unique(out, "storage input")
        if len(_bytes([_wire(f) for f in out])) > self.limits.max_file_bytes:
            raise ResourceLimit("input exceeds document limit")
        return tuple(sorted(out, key=lambda f: f.feature_id))

    def refresh(self, baseline, incoming, *, info, baseline_parts=(), incoming_parts=(),
                dry_run=False, expected_state=None, expected_merge=None, validate=None, enable=False):
        """Persist accepted Stage 1 edits and unresolved conflicts, even while disabled.

        Supply the original baseline and sufficient surrounding context on every call.
        A later snapshot's omissions never remove previously accepted overlays.
        """
        if type(dry_run) is not bool or type(enable) is not bool:
            raise ValueError("dry_run/enable must be boolean")
        metadata = _metadata(info)
        baseline, incoming, baseline_parts, incoming_parts = [self._inputs(v) for v in
                                                              (baseline, incoming, baseline_parts, incoming_parts)]
        for f in incoming:
            bbox = bo._bbox(f.geometry)
            if bbox is not None and not _intersects(bbox, metadata["bounds"]):
                raise ValueError("incoming building is outside refresh bounds")
        scope = _union([metadata["bounds"]] + [bo._bbox(f.geometry) for f in baseline + incoming])
        if any(p.parent_id not in {f.feature_id for f in baseline} for p in baseline_parts):
            raise ValueError("baseline part has no supplied parent")
        with self._locked():
            before = self._load()
            if expected_state is not None and self._state_token() != expected_state:
                raise StalePreview("overlay state changed; obtain a new preview")
            if validate is not None:
                validate()  # local validation only: callers must not acquire a catalog lock here
            identities = {identity for f in incoming for identity in bo._identities(f)}
            effective, parts = self._apply(before, baseline, baseline_parts, info.baseline_release, scope, True, identities)
            result = bo.merge(effective, incoming, baseline_parts=parts, incoming_parts=incoming_parts)
            entries = {e.feature.feature_id: e for e in before.entries}
            # Never let an older overlapping export overwrite a newer accepted edit.
            stale, decisions = set(), []
            source_time = datetime.fromisoformat(metadata["source_timestamp"].replace("Z", "+00:00"))
            for d in result.decisions:
                fid = d.baseline_ids[0] if d.baseline_ids else None
                entry = entries.get(fid)
                if d.incoming_id is not None and d.action != "conflict" and entry is not None and \
                        source_time < datetime.fromisoformat(entry.source_timestamp.replace("Z", "+00:00")):
                    stale.add(fid)
                    decisions.append(replace(d, action="conflict", reason="stale_source_timestamp", geometry_changed=False))
                else:
                    decisions.append(d)
            if stale:
                retained = {f.feature_id: f for f in effective if f.feature_id in stale}
                buildings = tuple(retained.get(f.feature_id, f) for f in result.buildings)
                kept_parts = tuple(p for p in result.parts if p.parent_id not in stale) + tuple(p for p in parts if p.parent_id in stale)
                result = bo.MergeResult(buildings, tuple(sorted(kept_parts, key=lambda p: p.feature_id)), tuple(decisions))
            if expected_merge is not None and expected_merge != (result.digest(), result.report_json()):
                raise StalePreview("reviewed merge decisions changed; obtain a new preview")
            originals, merged = ({f.feature_id: f for f in fs} for fs in (baseline, result.buildings))
            incoming_by_id = {f.feature_id: f for f in incoming}
            for d in result.decisions:
                if d.subject != "building" or d.incoming_id is None or d.action == "conflict":
                    continue
                fid = d.baseline_ids[0] if d.baseline_ids else bo._identities(incoming_by_id[d.incoming_id])[0]
                f = merged[fid]
                original = originals.get(fid)
                new_parts = tuple(p for p in result.parts if p.parent_id == fid)
                old_parts = tuple(p for p in baseline_parts if p.parent_id == fid)
                original_digest = _fingerprint(original, old_parts) if original else None
                coverage = _union([bo._bbox(f.geometry), bo._bbox(original.geometry) if original else None] +
                                  [bo._bbox(p.geometry) for p in new_parts + old_parts])
                entries[fid] = Entry(f, new_parts, original_digest, _bounds(coverage), metadata["source_timestamp"])
            refresh = {"info": metadata, "incoming": [_wire(f) for f in incoming],
                       "incoming_parts": [_wire(f) for f in incoming_parts]}
            refresh_id = _hash(refresh)
            refreshes = {_hash(r): r for r in before.refreshes}
            refreshes[refresh_id] = refresh
            conflicts = {_hash(c): c for c in before.conflicts}
            for d in result.decisions:
                if d.action == "conflict":
                    conflict = {"refresh_id": refresh_id, "bounds": metadata["bounds"], "decision": asdict(d)}
                    # asdict keeps tuples; normalize to the stored JSON representation.
                    conflict = json.loads(_bytes(conflict))
                    conflicts[_hash(conflict)] = conflict
            values = tuple(entries[k] for k in sorted(entries))
            if len(entries) + sum(len(e.parts) for e in values) > self.limits.max_entries or \
                    len(conflicts) > self.limits.max_conflicts or len(refreshes) > self.limits.max_refreshes:
                raise ResourceLimit("overlay record limit reached; prior edits remain intact")
            core = {"baseline_release": info.baseline_release,
                    "overrides": [_entry_wire(e) for e in values if e.baseline_digest is not None],
                    "additions": [_entry_wire(e) for e in values if e.baseline_digest is None],
                    "conflicts": conflicts, "refreshes": refreshes}
            if before.revision:
                previous = self._object(before.revision)
                if {k: v for k, v in previous.items() if k != "parent"} == core:
                    if validate is not None:
                        validate()
                    if enable and not before.enabled and not dry_run:
                        self._publish(before, before.revision, before.history, True, _union([e.bounds for e in before.entries]))
                    return Commit(before.revision, result, False, None, None if dry_run else self._state_token())
            payload = dict(core, parent=before.revision)
            revision = _hash(payload)
            document = self._document(payload)
            path = self.objects / f"{revision}.json"
            # Validate exactly what will be loaded before publishing its manifest.
            self._quota(len(document) * 2 + 16384, new_object=not path.exists())
            affected = _changed_bounds(before.entries, values)
            history = before.history + (revision,)
            if len(history) > self.limits.max_revisions:
                raise ResourceLimit("revision history limit reached")
            if dry_run:
                if validate is not None:
                    validate()
                return Commit(revision, result, True, affected)
            if path.exists():
                if self._object(revision) != payload:
                    raise CorruptStorage("immutable revision differs")
            else:
                self._atomic_write(path, document)
            manifest = {"head": revision, "history": list(history), "enabled": before.enabled or enable,
                        "affected_bounds": affected}
            self._snapshot(manifest)
            if validate is not None:
                validate()  # recheck after object I/O, immediately before manifest publication
            self._publish(before, revision, history, before.enabled or enable, affected)
            return Commit(revision, result, True, affected, self._state_token())

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._locked():
            before = self._load()
            if before.enabled != enabled:
                self._publish(before, before.revision, before.history, enabled,
                              _union([e.bounds for e in before.entries]))
            return self._load()

    def enable(self):
        return self.set_enabled(True)

    def disable(self):
        return self.set_enabled(False)

    def rollback(self, revision=None):
        """Restore an ancestor snapshot (default: one step, including back to no overlay)."""
        with self._locked():
            before = self._load()
            if not before.history:
                raise ValueError("no revision to roll back")
            if revision is None:
                history = before.history[:-1]
            else:
                if revision not in before.history[:-1]:
                    raise ValueError("rollback target must be an earlier revision in current history")
                history = before.history[:before.history.index(revision) + 1]
            target = history[-1] if history else None
            manifest = {"head": target, "enabled": before.enabled, "history": list(history), "affected_bounds": None}
            restored = self._snapshot(manifest)
            affected = _changed_bounds(before.entries, restored.entries)
            self._publish(before, target, history, before.enabled, affected)
            return self._load()

    def recover(self):
        """Explicitly restore a valid backup after corruption; never guess from orphan objects."""
        with self._locked():
            try:
                self._load()
            except CorruptStorage:
                previous = self._read(self.root / "previous.json")
                self._snapshot(previous)
                previous["incarnation"] = uuid.uuid4().hex
                self._atomic_write(self.root / "state.json", self._document(previous))
                return self._load()
            raise ValueError("current storage is valid; use rollback for a deliberate undo")

    def dependencies(self, bounds):
        """Local tokens for future consumers. No cache is inspected or invalidated here."""
        bounds = _bounds(bounds)
        with self._locked():
            snapshot = self._load()
            entries = [e for e in snapshot.entries if _intersects(e.bounds, bounds)] if snapshot.enabled else []
            full = [_dependency_wire(e) for e in entries]
            geometry = [{"id": e.feature.feature_id, "baseline_digest": e.baseline_digest, "bounds": e.bounds,
                         "geometry": bo._geometry_key(e.feature.geometry),
                         "parts": [(p.feature_id, bo._geometry_key(p.geometry)) for p in e.parts]} for e in entries]
            return AreaRevision(_hash(full), _hash(geometry), tuple(e.feature.feature_id for e in entries))

    def revision_for(self, bounds):
        return self.dependencies(bounds).revision
