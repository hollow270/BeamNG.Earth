"""Read-only, bounded original building context from existing processed cells.

Never invokes a fetch, creates an mmap mirror, or uses a possibly stale decoded LRU.
The caller explicitly warms missing baseline cells through the ordinary pipeline first.
"""
from dataclasses import dataclass
import hashlib
import io
import json
import math
from pathlib import Path
import zipfile

import numpy as np

import buildingoverlay as bo
from buildingoverlaystore import BaselineMismatch, ResourceLimit, StalePreview, _bounds, _intersects, _union
import buildingrender
import geodesy
import mapcells
import mapdata
import mapgeom


@dataclass(frozen=True)
class Context:
    buildings: tuple
    parts: tuple
    bounds: tuple
    files: tuple
    release: str

    def digest(self):
        return bo.MergeResult(self.buildings,self.parts,()).digest()

    def validate(self, md):
        if md.release != self.release:
            raise StalePreview("baseline release changed")
        for path, stamp in self.files:
            if _stamp(Path(path),max_bytes=stamp[0]) != stamp:
                raise StalePreview("original baseline cache changed; obtain a new preview")


def _stamp(path, data=None, max_bytes=64*1024*1024):
    try:
        before = path.stat()
        if data is None:
            if before.st_size > max_bytes:
                raise StalePreview("baseline file size changed")
            with path.open("rb") as f:
                data = f.read(max_bytes+1)
            if len(data) > max_bytes:
                raise StalePreview("baseline file grew while checking")
        after = path.stat()
        signature = lambda s: (s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_ino)
        if signature(before) != signature(after):
            raise StalePreview("baseline file changed while reading")
        return signature(after)+(hashlib.sha256(data).hexdigest(),)
    except OSError as e:
        raise StalePreview("original baseline file is missing or unreadable") from e


def _read(md, key, layer, budget):
    path = Path(md.path(key,layer))
    try:
        before = path.stat()
        if before.st_size > budget[0]:
            raise ResourceLimit("baseline file exceeds context byte budget")
        with path.open("rb") as f:
            data = f.read(budget[0]+1)
        if len(data) > budget[0]:
            raise ResourceLimit("baseline file exceeds context byte budget")
        stamp = _stamp(path,data)
        if (before.st_size,before.st_mtime_ns,before.st_ino) != (stamp[0],stamp[1],stamp[3]):
            raise StalePreview("baseline file changed while reading")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            if len(names) != len(set(names)) or len(names) > 256:
                raise BaselineMismatch("invalid original cell archive")
            size = sum(i.file_size for i in z.infolist())
            if size > budget[0]:
                raise ResourceLimit("uncompressed baseline context exceeds byte budget")
            # Check NPY shapes before NumPy can allocate according to an untrusted header.
            for info in z.infolist():
                with z.open(info) as f:
                    version = np.lib.format.read_magic(f)
                    reader = {(1,0): np.lib.format.read_array_header_1_0,
                              (2,0): np.lib.format.read_array_header_2_0}.get(version)
                    if reader is None:
                        raise BaselineMismatch("unsupported cached array format")
                    shape, _, dtype = reader(f)
                    if dtype.hasobject or dtype.itemsize<=0 or len(shape) > 3 or any(type(v) is not int or v<0 for v in shape) or \
                            math.prod(shape)*dtype.itemsize != info.file_size-f.tell():
                        raise BaselineMismatch("invalid cached baseline array size/type")
                    if info.filename=="ids.npy" and (len(shape)!=1 or shape[0]>budget[1]):
                        raise ResourceLimit("original context exceeds raw feature budget")
                    if info.filename=="lonlat.npy" and (len(shape)!=2 or shape[0]>budget[2]):
                        raise ResourceLimit("original context exceeds raw vertex budget")
        budget[0] -= size
        with np.load(io.BytesIO(data),allow_pickle=False) as z:
            arrays = {k:z[k] for k in z.files}
        if "missing" in arrays or "provenance" not in arrays:
            raise BaselineMismatch("baseline cell is incomplete, not an empty geographic area")
        f = mapgeom.Features.from_arrays(layer,arrays,"")
        n = len(f)
        provenance = json.loads(str(arrays["provenance"]))
        if (n and (not isinstance(provenance,dict) or not isinstance(provenance.get("id"),str))) or \
                f.lonlat.ndim != 2 or f.lonlat.shape[1] != 2 or not np.isfinite(f.lonlat).all() or \
                (np.abs(f.lonlat[:,0])>180).any() or (np.abs(f.lonlat[:,1])>90).any() or \
                f.ids.ndim!=1 or f.kind.ndim!=1 or len(f.kind)!=n:
            raise BaselineMismatch("invalid/incomplete original geometry or provenance")
        budget[1] -= n
        budget[2] -= len(f.lonlat)
        for offsets, end, length in ((f.part_off,len(f.ring_off)-1,n+1),
                                     (f.ring_off,len(f.coord_off)-1,None),
                                     (f.coord_off,len(f.lonlat),None)):
            if offsets.ndim!=1 or offsets.dtype.kind not in "iu" or not len(offsets) or offsets[0] != 0 or offsets[-1] != end \
                    or (offsets[1:]<offsets[:-1]).any() or (length is not None and len(offsets)!=length):
                raise BaselineMismatch("invalid baseline geometry offsets")
        if any(v.ndim!=1 or len(v)!=n for v in f.attrs.values()) or len(set(map(str,f.ids))) != n:
            raise BaselineMismatch("invalid baseline columns/identities")
        if any(v is not None and (v.ndim!=1 or len(v)!=n or v.dtype.kind!="b") for v in (f.own,f.clipped)):
            raise BaselineMismatch("invalid baseline ownership/clipping columns")
        f.localize(geodesy.EnuFrame(*key.center()))
        if layer == "buildings":
            f = mapgeom.drop_huge(f,mapdata.BUILDING_MAX_M2)
        return f,(str(path),stamp)
    except (BaselineMismatch,ResourceLimit,StalePreview):
        raise
    except (OSError,ValueError,KeyError,TypeError,IndexError,zipfile.BadZipFile,AttributeError) as e:
        raise BaselineMismatch(f"original {layer} cell {key} missing/invalid; warm or repair baseline context first") from e


def collect(md, bounds, snapshot, incoming=(), incoming_parts=(), *, max_cells=64, max_features=512,
            max_bytes=64*1024*1024):
    """Complete geometry/part lookup closure, without growing through unrelated baseline neighbours."""
    bounds = _bounds(bounds)
    interest = _union([bounds]+[bo._bbox(f.geometry) for f in tuple(incoming)+tuple(incoming_parts)])
    loaded, markers, entries, forced, selected, children = {}, {}, set(), set(), {}, {}
    budget = [max_bytes,65536,1000000]  # whole cached cells, before selecting the small interest area
    scope = interest
    for _ in range(max_cells+1):
        for entry in snapshot.entries:  # include disabled stored edits too: refresh merges over those deliberately
            if entry.feature.feature_id not in entries and _intersects(entry.bounds,scope):
                entries.add(entry.feature.feature_id)
                forced.add(entry.feature.feature_id)
                interest = _union([interest,bo._bbox(entry.feature.geometry)])
                scope = _union([scope,entry.bounds])
        w,s,e,n = scope
        try:
            keys = mapcells.cells_for_bbox(s,w,n,e,max_cells=max_cells)
        except ValueError as e:
            raise ResourceLimit("baseline neighbourhood exceeds cell budget") from e
        todo = [key for key in keys if key not in loaded]
        for key in todo:
            if len(loaded) >= max_cells:
                raise ResourceLimit("baseline neighbourhood exceeds cell budget")
            loaded[key] = {}
            for layer in ("buildings","building_parts"):
                f, marker = _read(md,key,layer,budget)
                loaded[key][layer] = f
                markers[marker[0]] = marker
        original_scope, count = scope, len(entries)
        for layers in loaded.values():
            f = layers["buildings"]
            indices = []
            for i,fid in enumerate(f.ids):
                xy = f.lonlat[f.coord_off[f.ring_off[f.part_off[i]]]:f.coord_off[f.ring_off[f.part_off[i+1]]]]
                box = (float(xy[:,0].min()),float(xy[:,1].min()),float(xy[:,0].max()),float(xy[:,1].max())) if len(xy) else None
                if str(fid) in forced or box is None or _intersects(box,interest):
                    if f.clipped is not None and f.clipped[i]:
                        raise BaselineMismatch("original building footprint is clipped; full context is required")
                    indices.append(i)
            for record in bo.from_mapgeom(buildingrender._take(f,indices)):
                if record.feature_id in selected and bo._json(vars(selected[record.feature_id])) != bo._json(vars(record)):
                    raise BaselineMismatch("neighbouring baseline cells disagree")
                selected[record.feature_id] = record
                scope = _union([scope,bo._bbox(record.geometry)])
        for layers in loaded.values():
            f = layers["building_parts"]
            parent = f.attrs.get("building_id",np.full(len(f),""))
            indices = [i for i,pid in enumerate(parent) if str(pid) in selected]
            if f.clipped is not None and any(f.clipped[i] for i in indices):
                raise BaselineMismatch("original building parts are clipped; full context is required")
            for part in bo.from_mapgeom(buildingrender._take(f,indices)):
                if part.feature_id in children and bo._json(vars(children[part.feature_id])) != bo._json(vars(part)):
                    raise BaselineMismatch("neighbouring baseline parts disagree")
                children[part.feature_id] = part
                scope = _union([scope,bo._bbox(part.geometry)])
        if len(selected)+len(children) > max_features:
            raise ResourceLimit("baseline neighbourhood exceeds feature budget")
        if scope == original_scope and not todo and count == len(entries):
            break
    else:
        raise ResourceLimit("baseline context closure did not converge")
    if any(f.attrs.get("has_parts") and not any(p.parent_id==f.feature_id for p in children.values()) for f in selected.values()):
        raise BaselineMismatch("baseline declares building parts but none are available")
    result = Context(tuple(selected[k] for k in sorted(selected)),tuple(children[k] for k in sorted(children)),
                     _bounds(scope),tuple(markers[k] for k in sorted(markers)),md.release)
    result.validate(md)
    return result
