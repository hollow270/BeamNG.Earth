"""Bounded preparation of immutable building generations; activation belongs to Stage 5.

Workers never write into a session's live shape namespace. A durable catalog is the
only publication point. Its sequence is a CAS fence, never a world/cache version.
"""
from concurrent.futures import CancelledError, TimeoutError
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

import buildingoverlay as bo
from buildingoverlaystore import OverlayStore, Limits as StoreLimits, CorruptStorage, ResourceLimit


class StaleGeneration(RuntimeError):
    pass


class IncompleteGeneration(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    requests: int = 128
    changed_cells: int = 512
    jobs: int = 2
    refreshes: int = 2
    artifacts: int = 512
    files: int = 128
    file_bytes: int = 64 * 1024 * 1024
    job_bytes: int = 128 * 1024 * 1024
    total_bytes: int = 512 * 1024 * 1024
    timeout: float = 120.0


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


KINDS = ("near", "far", "skyline", "near_geo", "far_geo", "skyline_geo")
DEFAULTS = dict(sidewalks=True, lift=0.05, left_hand=False, mph=True, q=0.05,
                gas_stations=False, buried=0.0, lamp_region=None, country=None, skin=True, exact="auto")


@dataclass(frozen=True)
class TileRequest:
    kind: str
    x: int
    y: int
    aid: int = 1
    lat: float = 0.0
    lon: float = 0.0
    zoom: int = 15
    gzoom: int = 15
    options: object = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in KINDS or any(type(v) is not int for v in (self.x, self.y, self.aid, self.zoom, self.gzoom)):
            raise ValueError("unsupported building tile request")
        if not (0 <= self.aid < 2**31 and 0 <= self.zoom <= 22 and 0 <= self.gzoom <= 22) or \
                max(abs(self.x), abs(self.y)) > 2**22:
            raise ValueError("tile coordinates/settings exceed limits")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (self.lat, self.lon)) \
                or not (-85 <= self.lat <= 85 and -180 <= self.lon <= 180):
            raise ValueError("unsupported anchor")
        supplied = json.loads(self.options) if isinstance(self.options, str) else deepcopy(self.options)
        if not isinstance(supplied, dict) or set(supplied) - set(DEFAULTS):
            raise ValueError("unsupported rendering options")
        options = dict(DEFAULTS, **supplied)
        for name in ("sidewalks", "left_hand", "mph", "gas_stations", "skin"):
            if type(options[name]) is not bool:
                raise ValueError(f"{name} must be boolean")
        for name in ("lift", "buried", "q"):
            v = options[name]
            if name == "q" and v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1000:
                raise ValueError(f"invalid {name}")
        if options["q"] == 0 or options["exact"] not in (None, "auto"):
            raise ValueError("invalid height quantum/exact variant mode")
        for name in ("lamp_region", "country"):
            if options[name] is not None and (not isinstance(options[name], str) or len(options[name]) > 64):
                raise ValueError(f"invalid {name}")
        object.__setattr__(self, "options", _json(options))
        object.__setattr__(self, "lat", float(self.lat))
        object.__setattr__(self, "lon", float(self.lon))
        self.geometry()  # validate native geotile coordinates before any job is submitted

    def wire(self):
        return asdict(self)

    @property
    def slot(self):
        return _hash([self.kind, self.x, self.y, self.aid, self.lat, self.lon])

    def geometry(self):
        import blockworker
        import geodesy
        import geotiles
        import roads
        if self.kind.endswith("_geo"):
            kind = {"near_geo": None, "far_geo": "bldfar", "skyline_geo": "bldsky"}[self.kind]
            z = geotiles.Z if kind is None else blockworker.far_geo_zoom(kind)
            if not (0 <= self.x < 2**z and 0 <= self.y < 2**z):
                raise ValueError("geotile outside its grid")
            tile = geotiles.geobox(self.x, self.y, z)
            return tile.frame, tile.box
        frame = geodesy.EnuFrame(self.lat, self.lon)
        if self.kind == "skyline":
            import buildings
            u = buildings.SKY_UNIT
            x0, y0, _, _ = roads.tile_bbox(self.x*u, self.y*u)
            _, _, x1, y1 = roads.tile_bbox(self.x*u+u-1, self.y*u+u-1)
            return frame, (x0, y0, x1, y1)
        return frame, roads.tile_bbox(self.x, self.y)

    def inputs(self, md):
        import blockworker
        import revisit
        frame, box = self.geometry()
        versions = blockworker.building_versions(md, frame, box)
        return revisit.key("building-generation", self.wire(), versions), (versions or {}).get("building_overlay")

    def execute(self, out_dir, vfs_dir):
        import blockworker as bw
        o = json.loads(self.options)
        if self.kind == "near":
            return bw.roads_tile(self.aid, self.lat, self.lon, self.zoom, self.x, self.y, out_dir, vfs_dir,
                                 o["sidewalks"], o["lift"], True, o["left_hand"], o["mph"], o["q"],
                                 o["gas_stations"], o["buried"], o["lamp_region"], o["country"])
        if self.kind == "near_geo":
            return bw.roads_geo(self.aid, self.lat, self.lon, self.zoom, self.x, self.y, self.gzoom, out_dir, vfs_dir,
                                o["sidewalks"], o["lift"], True, o["left_hand"], o["mph"], o["q"],
                                o["gas_stations"], o["buried"], o["lamp_region"], o["country"],
                                content=True, skin=o["skin"], exact=o["exact"])
        if self.kind.endswith("_geo"):
            return bw.far_geo("bldfar" if self.kind == "far_geo" else "bldsky", self.x, self.y,
                              self.gzoom, out_dir, vfs_dir, o["lamp_region"])
        fn = bw.far_buildings_tile if self.kind == "far" else bw.skyline_tile
        return fn(self.aid, self.lat, self.lon, self.zoom, self.x, self.y, out_dir, vfs_dir, o["lamp_region"])


def _plain(value):
    # Only trusted worker results, never pickle in the persistent catalog.
    return json.loads(json.dumps(value, allow_nan=False, default=lambda v: v.tolist()))


def scan(directory, vfs, limits):
    """Bounded, flat artifact manifest, exact file hashes and path-independent content hashes."""
    files, semantic, meshes, total = {}, [], [], 0
    paths = list(islice(Path(directory).iterdir(), limits.files+1))
    if len(paths) > limits.files:
        raise ResourceLimit("generation contains too many files")
    for path in sorted(paths):
        OverlayStore._no_link(path)
        if not path.is_file() or len(files) >= limits.files:
            raise ResourceLimit("generation has unsupported entries or too many files")
        size = path.stat().st_size
        total += size
        if size > limits.file_bytes or total > limits.job_bytes:
            raise ResourceLimit("generation exceeds byte budget")
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
                if f.tell() > size:
                    raise ResourceLimit("generation changed during verification")
        digest = h.hexdigest()
        files[path.name] = dict(size=size, sha256=digest)
        normal = digest
        if path.suffix == ".json":
            with path.open("rb") as f:
                body = f.read(limits.file_bytes+1)
            if len(body) > limits.file_bytes:
                raise ResourceLimit("JSON artifact exceeds size budget")
            normal = hashlib.sha256(body.replace((vfs + "/").encode(), b"@V@/")).hexdigest()
        semantic.append((path.name, normal))
        if path.suffix == ".cdae":
            meshes.append((path.name, digest))
    if not files:
        raise IncompleteGeneration("worker produced no artifact files")
    return files, _hash(semantic), _hash(meshes)


def _sync_directory(directory):
    if os.name != "nt":
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _sync_files(directory, names):
    # Writers close their files before returning. Flush prepared data before publishing its durable pointer.
    for name in names:
        with (directory / name).open("r+b") as f:
            os.fsync(f.fileno())
    _sync_directory(directory)


def generate(request, snapshot, out_dir, vfs_dir, expected, limits=Limits()):
    """Process-pool entry: use the supplied snapshot, never a worker's startup/head revision."""
    import blockworker
    md = blockworker._s.md
    with md.building_snapshot(snapshot):
        identity, dependency = request.inputs(md)
        if identity != expected:
            raise StaleGeneration("worker baseline/settings differ from the requested generation")
        result = request.execute(out_dir, vfs_dir)
        if result is None:
            raise IncompleteGeneration("required baseline cells are not ready; retry later")
        if request.inputs(md)[0] != identity:
            raise StaleGeneration("generation inputs changed during the job")
        files, content, meshes = scan(out_dir, vfs_dir, limits)
        return dict(request=request.wire(), identity=identity, dependency=dependency, snapshot=snapshot.revision,
                    baseline_release=md.release, files=files, content_digest=content,
                    mesh_digest=meshes, result=_plain(result))


class _CatalogFiles(OverlayStore):
    """Reuse Stage 2's file lock, checked JSON and atomic-write primitives, without overlay manifests."""
    def __init__(self, directory, *, create=True):
        self.directory = self.root = Path(directory).absolute()
        if ".." in self.root.parts:
            raise ValueError("catalog path cannot contain '..'")
        self.objects = self.root / "generations"
        self.limits, self.lock_timeout = StoreLimits(max_file_bytes=4*1024*1024), 5.0
        for path in (self.root, self.objects, self.root / "work"):
            self._no_link(path)
            if create:
                path.mkdir(parents=True, exist_ok=True)
            elif not path.is_dir():
                raise CorruptStorage("prepared catalog directories are incomplete")


def registered_requests(directory, *, limit=128):
    """Read a retained registry without creating catalogs/directories (manual preview/discovery)."""
    path = Path(directory).absolute() / "catalog.json"
    if not path.exists():
        return ()
    io = _CatalogFiles(directory, create=False)
    state = io._read(path)
    rows = state.get("requests")
    if not isinstance(rows, dict) or len(rows) > limit:
        raise ResourceLimit("invalid/oversized retained target registry")
    requests = tuple(TileRequest(**row) for _,row in sorted(rows.items()))
    if any(r.slot != key for r,(key,_) in zip(requests,sorted(rows.items()))):
        raise CorruptStorage("invalid retained target slots")
    return requests


def _index(snapshot, feature_ids=None):
    return {e.feature.feature_id: [_hash([bo.MergeResult((e.feature,), e.parts, ()).digest(),
                                        e.baseline_digest, e.bounds]), list(e.bounds)]
            for e in snapshot.entries if feature_ids is None or e.feature.feature_id in feature_ids} if snapshot.enabled else {}


def changed_cells(before, after, limit):
    import mapcells
    keys = set()
    for fid in set(before) | set(after):
        if before.get(fid) == after.get(fid):
            continue
        for value in (before.get(fid), after.get(fid)):
            if value is not None:
                w, s, e, n = value[1]
                keys.update(mapcells.cells_for_bbox(s, w, n, e, max_cells=limit))
                if len(keys) > limit:
                    raise ResourceLimit("refresh affects too many geographical cells")
    return tuple(sorted(map(str, keys)))


class Coordinator:
    """Manual synchronous refresh API; callers may cancel or call concurrently, never from on_pose.

    submit(request, snapshot, directory, vfs, identity, limits) returns a Future.
    Disk sequence fencing also coordinates separate server instances sharing this catalog.
    """
    def __init__(self, directory, md, store, submit, *, limits=Limits()):
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
               for v in asdict(limits).values()):
            raise ValueError("generation limits must be positive")
        if any(type(v) is not int for k, v in asdict(limits).items() if k != "timeout"):
            raise ValueError("count/byte limits must be integers")
        self.io, self.md, self.store, self.submit, self.limits = _CatalogFiles(directory), md, store, submit, limits
        self.path = self.io.root / "catalog.json"
        self._requests = threading.BoundedSemaphore(limits.refreshes)
        self._jobs = threading.BoundedSemaphore(limits.jobs)
        self._active, self._active_lock = set(), threading.Lock()
        self._discard, self._cleanup_lock = set(), threading.Lock()
        with self.io._locked():
            if not self.path.exists():
                if any(self.io.objects.iterdir()):
                    raise CorruptStorage("generation catalog missing; existing artifacts require recovery")
                self._save(dict(sequence=0, publication=None, snapshot=None, overlay={}, requests={}, entries={}))
            self._load()

    def _save(self, state):
        self.io._atomic_write(self.path, self.io._document(state))

    def _load(self):
        state = self.io._read(self.path)
        try:
            if set(state) != {"sequence", "publication", "snapshot", "overlay", "requests", "entries"} or \
                    type(state["sequence"]) is not int or not 0 <= state["sequence"] < 2**63:
                raise ValueError("invalid catalog fields/sequence")
            if any(not isinstance(state[k], dict) for k in ("overlay", "requests", "entries")):
                raise ValueError("invalid catalog tables")
            for name, width in (("publication", 32), ("snapshot", 64)):
                value = state[name]
                if value is not None and (not isinstance(value, str) or not re.fullmatch("[0-9a-f]{"+str(width)+"}", value)):
                    raise ValueError("invalid catalog revision")
            if len(state["requests"]) > self.limits.requests or set(state["entries"]) - set(state["requests"]):
                raise ValueError("catalog exceeds request budget")
            for key, row in state["requests"].items():
                if TileRequest(**row).slot != key:
                    raise ValueError("invalid registered tile")
            for key, row in state["entries"].items():
                if set(row) != {"request", "generation", "identity", "dependency", "snapshot", "baseline_release", "files",
                               "content_digest", "mesh_digest", "result"} or not isinstance(row["files"], dict) \
                        or len(row["files"]) > self.limits.files:
                    raise ValueError("invalid artifact fields")
                if not isinstance(row["baseline_release"], str) or len(row["baseline_release"]) > 256:
                    raise ValueError("invalid baseline release")
                for name, width in (("identity", 40), ("dependency", 64), ("snapshot", 64),
                                    ("content_digest", 64), ("mesh_digest", 64)):
                    value = row[name]
                    if value is None and name in ("dependency", "snapshot"):
                        continue
                    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{"+str(width)+"}", value):
                        raise ValueError("invalid artifact identity")
                # A settings change retains the old valid entry until the new generation is complete.
                if not re.fullmatch("[0-9a-f]{32}", row["generation"]) or TileRequest(**row["request"]).slot != key:
                    raise ValueError("invalid artifact reference")
                for name, info in row["files"].items():
                    if Path(name).name != name or name in (".", "..") or "/" in name or "\\" in name or ":" in name:
                        raise ValueError("invalid artifact filename")
                    if set(info) != {"size", "sha256"} or type(info["size"]) is not int or \
                            not 0 <= info["size"] <= self.limits.file_bytes or \
                            not re.fullmatch("[0-9a-f]{64}", info["sha256"]):
                        raise ValueError("invalid artifact file manifest")
            if len(state["overlay"]) > self.store.limits.max_entries:
                raise ValueError("oversized overlay index")
            from buildingoverlaystore import _bounds
            for value in state["overlay"].values():
                if not isinstance(value, list) or len(value) != 2 or not re.fullmatch("[0-9a-f]{64}", value[0]):
                    raise ValueError("invalid overlay index")
                _bounds(value[1])
            return state
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            raise CorruptStorage(f"invalid generation catalog: {e}") from e

    def outputs(self):
        """Last completely prepared catalog. No connection/tracker activation is implied."""
        with self.io._locked():
            return deepcopy(self._load())

    def _valid(self, entry):
        if entry is None:
            return False
        directory = self.io.objects / entry["generation"]
        if not directory.exists():
            return False
        OverlayStore._no_link(directory)
        files, content, meshes = scan(directory, "@BUILDING_OVERLAY@/" + entry["generation"], self.limits)
        return files == entry["files"] and content == entry["content_digest"] and meshes == entry["mesh_digest"]

    def _usage(self):
        count, size = 0, 0
        for directory in (self.io.objects, self.io.root / "work"):
            OverlayStore._no_link(directory)
            for generation in directory.iterdir():
                OverlayStore._no_link(generation)
                if not generation.is_dir():
                    if directory.name == "work" and not generation.exists():
                        continue
                    raise CorruptStorage("unexpected generation entry")
                count += 1
                if count > self.limits.artifacts:
                    raise ResourceLimit("retained building generations exceed count budget")
                todo, visited = [(generation, 0)], 0
                while todo:
                    parent, depth = todo.pop()
                    try:
                        paths = list(islice(parent.iterdir(), self.limits.files*4+1))
                    except FileNotFoundError:
                        if directory.name == "work":
                            continue
                        raise
                    for path in paths:
                        visited += 1
                        if visited > self.limits.files * 4:
                            raise ResourceLimit("generation filesystem exceeds entry budget")
                        OverlayStore._no_link(path)
                        try:
                            if path.is_dir() and directory.name == "work" and depth < 2:
                                todo.append((path, depth+1))
                                continue
                            if not path.is_file():
                                raise CorruptStorage("unsupported nested generation")
                            size += path.stat().st_size
                        except FileNotFoundError:
                            continue  # a concurrent worker completed its atomic file/temporary-directory write
                        if size > self.limits.total_bytes:
                            raise ResourceLimit("retained building generations exceed disk budget")

    def _remove_work(self, directory):
        # Only private, unreferenced work. Never delete published generations or held resources.
        OverlayStore._no_link(self.io.root / "work")
        if directory.parent.resolve() != (self.io.root / "work").resolve():
            raise CorruptStorage("cleanup target outside private work directory")
        with self._cleanup_lock:
            OverlayStore._no_link(directory)
            if directory.exists():
                shutil.rmtree(directory)

    @staticmethod
    def _cancel(cancel):
        if cancel is not None and cancel.is_set():
            raise CancelledError("building refresh cancelled")

    def _build(self, request, snapshot, identity, cancel, directory, vfs):
        end = time.monotonic() + self.limits.timeout
        while not self._jobs.acquire(timeout=0.05):
            self._cancel(cancel)
            if time.monotonic() >= end:
                raise TimeoutError("building worker budget is busy")
        try:
            future = self.submit(request, snapshot, str(directory), vfs, identity, self.limits)
        except BaseException:
            self._jobs.release()
            raise
        abandoned = threading.Event()
        with self._active_lock:
            self._active.add(directory)
        def finished(_):
            self._jobs.release()
            with self._active_lock:
                self._active.discard(directory)
                discard = directory in self._discard
                self._discard.discard(directory)
            if abandoned.is_set() or discard:
                self._remove_work(directory)
        future.add_done_callback(finished)
        try:
            while True:
                self._cancel(cancel)
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("building generation timed out")
                try:
                    return future.result(timeout=min(0.05, remaining))
                except TimeoutError:
                    if future.done():
                        raise
        except BaseException:
            abandoned.set()
            future.cancel()
            if future.done():
                self._remove_work(directory)
            # A running cancelled/timed-out job owns its private directory until it exits.
            raise

    def refresh(self, requests=(), *, cancel=None, expected_state=None, replace_requests=False):
        if not self._requests.acquire(blocking=False):
            raise ResourceLimit("too many simultaneous building refreshes")
        work = []
        try:
            bounded = []
            for request in requests:
                if len(bounded) >= self.limits.requests or not isinstance(request, TileRequest):
                    raise ResourceLimit("invalid/oversized building refresh request")
                bounded.append(request)
            requests = bounded
            self._cancel(cancel)
            with self.io._locked():
                state = self._load()
                if state["sequence"] == 2**63-1:
                    raise ResourceLimit("building catalog sequence exhausted")
                registered = dict(state["requests"])
                registered.update({r.slot: r.wire() for r in requests})
                target_registry = {r.slot:r.wire() for r in requests} if replace_requests else registered
                if not target_registry:
                    raise ValueError("register at least one building tile before requesting regeneration")
                if len(registered) > self.limits.requests:
                    raise ResourceLimit("registered building tile budget exhausted")
                snapshot, state_token = self.store.load_state()
                if expected_state is not None and state_token != expected_state:
                    raise StaleGeneration("accepted workflow state changed before preparation")
                desired, feature_ids = {}, set()
                with self.md.building_snapshot(snapshot):
                    for key, row in sorted(target_registry.items()):
                        req = TileRequest(**row)
                        identity, dependency = req.inputs(self.md)
                        desired[key] = (req, identity, dependency)
                        import blockworker
                        feature_ids.update(self.md.building_dependencies(blockworker.building_cells(*req.geometry())).feature_ids)
                overlay = _index(snapshot, feature_ids)
                cells = changed_cells(state["overlay"], overlay, self.limits.changed_cells)
                state.update(sequence=state["sequence"]+1, requests=registered)
                ticket = state["sequence"]
                self._usage()
                self._save(state)
            entries, rebuilt, reused = {k:v for k,v in state["entries"].items() if k in target_registry}, [], []
            for key, (req, identity, dependency) in desired.items():
                self._cancel(cancel)
                old = entries.get(key)
                if old is not None and old["identity"] == identity and self._valid(old):
                    reused.append(key)
                    continue
                generation = uuid.uuid4().hex
                directory = self.io.root / "work" / generation
                directory.mkdir()
                work.append(directory)
                vfs = "@BUILDING_OVERLAY@/" + generation
                row = self._build(req, snapshot, identity, cancel, directory, vfs)
                if row["identity"] != identity or row["dependency"] != dependency or row["snapshot"] != snapshot.revision \
                        or row["baseline_release"] != self.md.release \
                        or row["request"] != req.wire():
                    raise StaleGeneration("worker receipt does not match the requested snapshot")
                files, content, meshes = scan(directory, vfs, self.limits)
                if (files, content, meshes) != (row["files"], row["content_digest"], row["mesh_digest"]):
                    raise CorruptStorage("worker files differ from their receipt")
                entries[key] = dict(row, generation=generation)
                rebuilt.append(key)
            self._cancel(cancel)
            with self.io._locked(), self.store._locked():
                current = self._load()
                if current["sequence"] != ticket:
                    raise StaleGeneration("a newer refresh superseded this batch")
                latest = self.store._load()  # hold the overlay writer lock through catalog publication
                if expected_state is not None and self.store._state_token() != expected_state:
                    raise StaleGeneration("accepted workflow state changed during preparation")
                with self.md.building_snapshot(latest):
                    if any(req.inputs(self.md)[0] != identity for req, identity, _ in desired.values()):
                        raise StaleGeneration("accepted local overlay data changed during generation")
                self._usage()
                self._cancel(cancel)
                for key in rebuilt:
                    source = self.io.root / "work" / entries[key]["generation"]
                    destination = self.io.objects / source.name
                    OverlayStore._no_link(source)
                    OverlayStore._no_link(self.io.objects)
                    if source.parent.resolve() != (self.io.root / "work").resolve() or \
                            destination.parent.resolve() != self.io.objects.resolve() or destination.exists():
                        raise CorruptStorage("invalid generation publication path")
                    _sync_files(source, entries[key]["files"])
                    source.rename(destination)
                _sync_directory(self.io.objects)
                # After this point retain all moved directories even on an uncertain fsync/rename failure.
                # They are either referenced by the new atomic catalog or harmless bounded orphans.
                current.update(entries=entries, requests=target_registry, publication=uuid.uuid4().hex,
                               snapshot=snapshot.revision, overlay=overlay)
                self._cancel(cancel)
                self._save(current)
            return dict(snapshot=snapshot.revision, rebuilt=tuple(rebuilt), reused=tuple(reused),
                        affected_cells=cells, publication=current["publication"])
        finally:
            try:
                for directory in work:
                    with self._active_lock:
                        active = directory in self._active
                        if active:
                            self._discard.add(directory)
                    if not active:
                        self._remove_work(directory)
            finally:
                self._requests.release()
