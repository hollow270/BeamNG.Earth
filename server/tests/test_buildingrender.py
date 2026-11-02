"""Offline renderer integration: synthetic cells, real native planner/mesher, no sockets/game."""
from copy import deepcopy
from dataclasses import replace
import gc
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import blockworker
import buildingoverlay as bo
from buildingoverlaystore import OverlayStore, RefreshInfo, BaselineMismatch
import buildingrender
import buildings
import cdae
import fast
import geodesy
import geotiles
import mapcells
import mapdata
import mapgeom as mg
import netfetch
import overture
import roads
import roadwidth
from test_buildingoverlay import outcomes


REPO = Path(__file__).resolve().parents[2]
CELL = mapcells.CellKey(14, 4706, 6244)
LAT, LON = CELL.center()
FRAME = geodesy.EnuFrame(LAT, LON)
RELEASE = "synthetic-overlay-render-1"


def polygon(x, y, w=10, d=10, frame=FRAME):
    xy = np.array([(x, y), (x+w, y), (x+w, y+d), (x, y+d), (x, y)], float)
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    return {"type": "Polygon", "coordinates": [np.c_[lo, la].tolist()]}


def record(fid="gers-a", x=60, y=60, w=10, d=10, *, geometry=None, parent_id=None, attrs=None, osm="w1@1"):
    return bo.Feature(fid, geometry or polygon(x, y, w, d), attrs or {"height": 12, "roof_shape": "flat", "class": "retail"},
                      ({"dataset": "OpenStreetMap", "record_id": osm},), parent_id=parent_id)


def incoming(fid="way/1", x=60, y=60, w=10, d=10, **kw):
    return record(fid, x, y, w, d, osm="w" + fid.rsplit("/", 1)[-1] + "@2", **kw)


def features(records, layer):
    records = sorted(records, key=lambda f: f.feature_id)
    geoms = [(mg.POLYGON, [[np.asarray(r) for r in f.geometry["coordinates"]]]) for f in records]
    f = mg.build(layer, mg.text_column([r.feature_id for r in records]), geoms)
    values = [buildingrender._attrs(r, layer) for r in records]
    spec = overture.LAYERS[layer]
    for name in spec.get("text", ()):
        f.attrs[name] = mg.text_column([a.get(name) for a in values])
    for name in spec.get("float", ()):
        f.attrs[name] = mg.float_column([a.get(name) for a in values])
    for name in spec.get("bool", ()):
        f.attrs[name] = mg.bool_column([a.get(name) for a in values])
    for name in ("cx", "cy"):
        f.attrs[name] = mg.float_column([a[name] for a in values], np.float64)
    return f


class FixtureMapData(mapdata.MapData):
    """Exercise real cell serialization/mirror/localization with a pure synthetic _build source."""
    def __init__(self, directory, originals=(), parts=(), overlay=False, frame=FRAME, street=False):
        self.originals, self.original_parts = tuple(originals), tuple(parts)
        self.test_frame = frame
        self.street = street
        super().__init__(str(directory), fetcher=netfetch.Fetcher(offline=True, transport=object()),
                         release=RELEASE, building_overlays=overlay)

    def _build(self, key, layer, kind):
        if layer in mapdata.RASTER_LAYERS:
            return {"missing": np.array(True), "provenance": np.array("null")}
        if layer in ("buildings", "building_parts"):
            rows = self.originals if layer == "buildings" else self.original_parts
            rows = [r for r in rows if buildingrender._intersects(bo._bbox(r.geometry), key.bounds())]
            f = features(rows, layer)
        else:
            f = overture.empty(layer)
        if layer == "roads" and self.street:
            la, lo, _ = self.test_frame.enu_to_geodetic(np.array([-80, 280]), np.array([20, 20]), 0.0)
            f = mg.build("roads", mg.text_column(["synthetic-street"]), [(mg.LINE, [[np.c_[lo, la]]])])
            f.attrs = {"class": mg.text_column(["residential"]), "subtype": mg.text_column(["road"]),
                       "cx": np.array([lo.mean()]), "cy": np.array([la.mean()])}
            f.rules = overture.empty("roads").rules
        ox, oy = mapcells.owner(f.attrs["cx"], f.attrs["cy"], key.z)
        f.own = (ox == key.x) & (oy == key.y)
        f.clipped = np.zeros(len(f), bool)
        arrays = mapdata._layer_arrays(f)
        arrays["provenance"] = np.array(json.dumps({"id": "overture:buildings", "name": "Synthetic Overture",
                                                   "release": RELEASE, "datasets": []} if len(f) else None))
        return arrays


def ground(la, lo):
    return np.zeros(np.shape(la))


def mesh_fingerprint(items):
    mesh = buildings.build(items)
    return None if mesh is None else cdae.encode(mesh[0], mesh[2], uvs=mesh[1], material=mesh[4],
                                                tri_materials=mesh[3], colors=mesh[5])


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".render-test-", dir=REPO)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = OverlayStore(self.root / "data")
        self.instances = []
        self.serial = 0
        self.addCleanup(self.close)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")).start()
        mock.patch.dict("os.environ", {}, clear=False).start()
        self.assertTrue(fast.AVAILABLE, "tests need the existing repository native binary; do not rebuild Rust")

    def close(self):
        for md in self.instances:
            md.close()
            md._mem.clear()
            if md._building_view:
                md._building_view.cache.clear()
        self.instances.clear()
        gc.collect()

    def md(self, rows=(), parts=(), overlay=False, frame=FRAME, street=False):
        self.serial += 1
        md = FixtureMapData(self.root / f"cache{self.serial}", rows, parts, overlay, frame, street)
        self.instances.append(md)
        return md

    def save(self, md, news, parts=()):
        # Save exactly the original mapgeom contracts (including typed missing attrs), not hand-waved sparse rows.
        bbox = [bo._bbox(r.geometry) for r in md.originals + tuple(news)]
        w = min(b[0] for b in bbox) - 1e-6
        s = min(b[1] for b in bbox) - 1e-6
        e = max(b[2] for b in bbox) + 1e-6
        n = max(b[3] for b in bbox) + 1e-6
        keys = mapcells.cells_for_bbox(s, w, n, e)
        originals, original_parts = {}, {}
        for key in keys:
            cd = md.cell(key, layers=("buildings", "building_parts"))
            originals.update({f.feature_id: f for f in bo.from_mapgeom(cd.layers["buildings"])})
            original_parts.update({f.feature_id: f for f in bo.from_mapgeom(cd.layers["building_parts"])})
        info = RefreshInfo((w, s, e, n), "2026-10-30T12:00:00Z", ({"dataset": "OpenStreetMap", "license": "ODbL-1.0"},), RELEASE)
        commit = self.store.refresh(tuple(originals.values()), news, baseline_parts=tuple(original_parts.values()),
                                    incoming_parts=parts, info=info)
        self.store.enable()
        return commit

    def items(self, md):
        return buildings.road_tile_items(FRAME, 0, 0, md, ground)

    def test_addition_enters_the_real_planner_mesh_and_collision_path(self):
        baseline = self.md()
        self.save(baseline, [incoming("way/2")])
        md = self.md(overlay=self.store)
        with mock.patch.object(buildings, "tile_buildings", wraps=buildings.tile_buildings) as planner:
            items = self.items(md)
        self.assertEqual(len(items), 1)
        self.assertTrue(planner.called)
        self.assertEqual(items[0][0].seed, buildings._hash01("osm:way:2", "plan"))
        mesh = buildings.road_tile(FRAME, 0, 0, md, ground, items=items)
        self.assertIsNotNone(mesh)
        self.assertGreater(len(mesh[0][0][0]), 0)
        self.assertIsNotNone(mesh[2])
        self.assertFalse(self.items(baseline))

    def test_replacement_geometry_reaches_real_mesh_without_duplicate(self):
        old = record()
        baseline = self.md([old])
        before = mesh_fingerprint(self.items(baseline))
        self.save(baseline, [incoming(w=18)])
        md = self.md([old], overlay=self.store)
        items = self.items(md)
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(float(np.ptp(items[0][0].ring[:, 0])), 18, delta=0.02)
        self.assertNotEqual(mesh_fingerprint(items), before)
        self.assertEqual(list(md.building_cell(CELL).layers["buildings"].ids), ["gers-a"])
        self.assertEqual(md.building_cell(CELL).layers["buildings"].attrs["src_record"].tolist(), ["w1@2"])

    def test_disable_restores_byte_identical_mesh_and_cell(self):
        old = record()
        baseline = self.md([old])
        self.save(baseline, [incoming(w=18)])
        enabled = self.md([old], overlay=self.store)
        self.assertNotEqual(mesh_fingerprint(self.items(enabled)), mesh_fingerprint(self.items(baseline)))
        self.store.disable()
        disabled = self.md([old], overlay=self.store)
        self.assertIs(disabled.building_cell(CELL).layers["buildings"], disabled.cell(CELL).layers["buildings"])
        self.assertEqual(mesh_fingerprint(self.items(disabled)), mesh_fingerprint(self.items(baseline)))
        self.assertEqual(disabled.cell(CELL).digest(), baseline.cell(CELL).digest())
        self.assertEqual(disabled.versions(), baseline.versions())

    def test_pinned_view_requires_new_offline_instance_for_storage_changes(self):
        base = self.md()
        self.save(base, [incoming("way/2")])
        md = self.md(overlay=self.store)
        token = md.building_dependencies((CELL,))
        self.store.disable()
        self.assertEqual(len(self.items(md)), 1)
        self.assertEqual(md.building_dependencies((CELL,)), token)
        self.assertFalse(self.items(self.md(overlay=self.store)))

    def test_unrelated_rows_and_baseline_disk_arrays_remain_unchanged(self):
        old, untouched = record(), record("gers-z", 160, 160, osm="w99@1")
        base = self.md([old, untouched])
        self.save(base, [incoming(w=18)])
        md = self.md([old, untouched], overlay=self.store)
        original = md.cell(CELL, layers=("buildings", "building_parts", "roads", "water"))
        original_digest = original.digest()
        arrays = {k: v.copy() for k, v in original.layers["buildings"].to_arrays("").items()}
        effective = md.building_cell(CELL, layers=tuple(original.layers))
        self.assertIs(effective.layers["roads"], original.layers["roads"])
        self.assertIs(effective.layers["water"], original.layers["water"])
        self.assertEqual(md.cell(CELL, layers=tuple(original.layers)).digest(), original_digest)
        for name, value in arrays.items():
            np.testing.assert_array_equal(original.layers["buildings"].to_arrays("")[name], value)
        f, b = effective.layers["buildings"], original.layers["buildings"]
        i, j = f.index("gers-z"), b.index("gers-z")
        for name in b.attrs:
            np.testing.assert_equal(f.attrs[name][i], b.attrs[name][j])
        np.testing.assert_array_equal(f.parts(i)[0][0], b.parts(j)[0][0])

    def test_road_width_terrain_versions_and_road_context_stay_on_baseline(self):
        old = record()
        base = self.md([old])
        self.save(base, [incoming(w=18), incoming("way/2", x=120)])
        md = self.md([old], overlay=self.store)
        rendered, context = buildings.road_context_items(FRAME, 0, 0, md, ground)
        self.assertEqual(len(rendered), 2)
        self.assertEqual(mesh_fingerprint(context), mesh_fingerprint(self.items(base)))
        self.assertEqual(md.versions(), base.versions())
        self.assertEqual(roadwidth.store_dir("cache", md), roadwidth.store_dir("cache", base))
        a = roadwidth.polygon_walls(md.cell(CELL, layers=("buildings",)).layers["buildings"], FRAME)
        b = roadwidth.polygon_walls(base.cell(CELL, layers=("buildings",)).layers["buildings"], FRAME)
        np.testing.assert_array_equal(a, b)

    def test_far_and_skyline_use_the_same_effective_features(self):
        base = self.md()
        self.save(base, [incoming("way/2", w=45, d=45, attrs={"height": 30, "class": "retail", "roof_shape": "flat"})])
        md = self.md(overlay=self.store)
        items, _, _ = buildings.far_items(FRAME, 0, 0, md, ground)
        self.assertEqual(len(items), 1)
        self.assertIsNotNone(buildings.build(items, lite=True))
        keys = roads.cells_of_tile(FRAME, 0, 0, margin=0)
        for key in keys:
            md.cell(key, layers=("buildings", "roads"))
        picked, _ = buildings.sky_items(FRAME, (0, 0, 256, 256), keys, md, ground)
        self.assertEqual(len(picked), 1)
        self.assertIsNotNone(buildings._sky_mesh(picked))

    def test_parent_parts_replace_as_a_complete_set_in_every_render_path(self):
        parent = record(attrs={"height": 30, "has_parts": True})
        part = record("gers-part", 61, 61, 7, 7, parent_id="gers-a", osm="w10@1", attrs={"height": 14})
        base = self.md([parent], [part])
        new = replace(incoming(), parts_complete=True)
        newpart = incoming("way/10", 61, 61, 8, 7, parent_id="way/1", attrs={"height": 22})
        commit = self.save(base, [new], [newpart])
        self.assertFalse(outcomes(commit.merge_result, "conflict"))
        md = self.md([parent], [part], overlay=self.store)
        cd = md.building_cell(CELL)
        self.assertEqual(list(cd.layers["building_parts"].ids), ["gers-part"])
        self.assertEqual(cd.layers["building_parts"].attrs["building_id"].tolist(), ["gers-a"])
        items = self.items(md)
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0][0].height, 22, delta=0.1)
        self.assertEqual(len(buildings.far_items(FRAME, 0, 0, md, ground)[0]), 1)

    def test_conflicts_and_incomplete_parts_are_not_promoted_to_rendering(self):
        old = record()
        base = self.md([old])
        commit = self.save(base, [incoming("way/99", x=65)])
        self.assertTrue(outcomes(commit.merge_result, "conflict"))
        md = self.md([old], overlay=self.store)
        self.assertFalse(md.building_overlays_active)
        self.assertEqual(mesh_fingerprint(self.items(md)), mesh_fingerprint(self.items(base)))
        snapshot = self.store.load()
        composed = self.store.compose(snapshot, bo.from_mapgeom(base.cell(CELL).layers["buildings"]),
                                      baseline_release=RELEASE, bounds=CELL.bounds())
        self.assertTrue(outcomes(composed, "conflict"))

    def test_baseline_mismatch_fails_instead_of_silently_rendering_wrong_geometry(self):
        base = self.md([record()])
        self.save(base, [incoming(w=18)])
        md = self.md([record(w=11)], overlay=self.store)
        with self.assertRaises(BaselineMismatch):
            md.building_cell(CELL)

    def test_overlay_saved_without_full_baseline_context_cannot_create_duplicates(self):
        empty = self.md()
        self.save(empty, [incoming("way/2")])
        # The same release actually contains this footprint, omitted by the storage caller.
        md = self.md([record()], overlay=self.store)
        with self.assertRaises(BaselineMismatch):
            md.building_cell(CELL)

    def test_worker_writes_normal_building_shapes_and_preserves_road_rows(self):
        old = record()
        base = self.md([old], street=True)
        self.save(base, [incoming(w=18), incoming("way/2", x=120)])
        md = self.md([old], overlay=self.store, street=True)
        class Dem:
            def sample(self, la, lo, zoom):
                return ground(la, lo)
        outputs = []
        for index, data in enumerate((base, md)):
            out = self.root / f"worker-{index}"
            out.mkdir()
            state = SimpleNamespace(md=data, dem=Dem(), widths=None, surface=None, airports=None, sea=None,
                                    fra=None, lock=threading.Lock(), cache_dir=str(self.root / "worker-cache"),
                                    bridges={}, anchor=lambda *args: (None, None))
            with mock.patch.object(blockworker, "_s", state):
                result = blockworker._roads_tile(1, LAT, LON, 15, 0, 0, str(out), "/fixture", True, 0.05,
                                                True, False, False, None, False)
            data_json = json.loads((out / "roads_1_0_0.json").read_text("utf-8"))
            self.assertIn("bld", data_json)
            self.assertIn("road", data_json)
            shape = out / Path(data_json["bld"]["shape"]).name
            self.assertTrue(cdae.read(shape)["meshes"])
            outputs.append((out, data_json, shape.read_bytes()))
        self.assertNotEqual(outputs[0][2], outputs[1][2])
        for field in ("chunks", "road", "props", "park", "wires", "fuel", "guardrails", "sound"):
            self.assertEqual(outputs[0][1].get(field), outputs[1][1].get(field), field)
        for field in ("road", "walk"):
            if field in outputs[0][1]:
                shape_name = Path(outputs[0][1][field]["shape"]).name
                self.assertEqual((outputs[0][0] / shape_name).read_bytes(),
                                 (outputs[1][0] / shape_name).read_bytes(), field)

    def test_per_area_dependencies_and_offline_cache_keys_are_local(self):
        base = self.md([record()])
        self.save(base, [incoming(w=18)])
        md = self.md([record()], overlay=self.store)
        self.assertNotEqual(md.building_dependencies((CELL,)).revision, None)
        self.assertEqual(md.building_dependencies((CELL, CELL)), md.building_dependencies((CELL,)))
        distant = mapcells.CellKey(CELL.z, CELL.x + 50, CELL.y)
        self.assertIsNone(md.building_dependencies((distant,)).revision)
        self.assertEqual(blockworker.building_versions(md, FRAME, (0, 0, 256, 256), False), base.versions())
        self.assertNotEqual(blockworker.building_versions(md, FRAME, (0, 0, 256, 256)), base.versions())
        farframe = geodesy.EnuFrame(*distant.center())
        self.assertEqual(blockworker.building_versions(md, farframe, (0, 0, 256, 256)), base.versions())
        rendered, context = buildings.road_context_items(farframe, 0, 0, md, ground)
        self.assertIs(rendered, context)
        # The cell centre lies on a geotile boundary; query this feature's owner.
        la,lo,_ = FRAME.enu_to_geodetic(65.,65.,0.)
        key = geotiles.key(*geotiles.tile_of(float(la),float(lo)))
        args = (key, 15, True, False, True, False, 0.0, None, None)
        off = blockworker._geo_content_key(*args, md=base)
        self.assertEqual(off, blockworker._geo_content_key(*args, md=base.versions()))
        on = blockworker._geo_content_key(*args, md=md)
        self.assertNotEqual(on, off)
        with mock.patch.object(blockworker, "_s", SimpleNamespace(md=md)):
            self.assertEqual(on, blockworker._geo_content_key(*args))

    def test_geometry_dependencies_change_after_offline_revision_and_rollback(self):
        base = self.md([record()])
        first = self.save(base, [incoming(w=18)])
        a = self.md([record()], overlay=self.store).building_dependencies((CELL,))
        self.save(base, [incoming(w=20)])
        b = self.md([record()], overlay=self.store).building_dependencies((CELL,))
        self.assertNotEqual(a.geometry_digest, b.geometry_digest)
        self.store.rollback(first.revision)
        self.assertEqual(a, self.md([record()], overlay=self.store).building_dependencies((CELL,)))

    def test_height_changes_update_render_revision_even_with_the_same_footprint(self):
        old = record(attrs={"height": 12, "height_src": "Microsoft"})
        base = self.md([old])
        self.save(base, [incoming(attrs={"height": 20})])
        a = self.md([old], overlay=self.store)
        self.save(base, [incoming(attrs={"height": 30})])
        b = self.md([old], overlay=self.store)
        da, db = a.building_dependencies((CELL,)), b.building_dependencies((CELL,))
        self.assertEqual(da.geometry_digest, db.geometry_digest)
        self.assertNotEqual(da.revision, db.revision)
        self.assertNotEqual(mesh_fingerprint(self.items(a)), mesh_fingerprint(self.items(b)))
        self.assertEqual(b.building_cell(CELL).layers["buildings"].attrs["height_src"].tolist(), ["OpenStreetMap"])

    def test_environment_config_is_inherited_by_worker_mapdata_without_global_versions(self):
        base = self.md()
        self.save(base, [incoming("way/2")])
        with mock.patch.dict("os.environ", {"BNGE_BUILDING_OVERLAY_DIR": str(self.root / "data")}):
            md = self.md(overlay=None)
            off = self.md(overlay=False)
        self.assertTrue(md.building_overlays_active)
        self.assertFalse(off.building_overlays_active)
        self.assertEqual(md.versions(), off.versions())
        self.assertEqual(len(self.items(md)), 1)

    def test_cell_boundary_crossing_is_whole_and_deduplicated_by_render_tile(self):
        west, _, east, _ = CELL.bounds()
        boundary_frame = geodesy.EnuFrame(LAT, east)
        geom = polygon(-5, 60, 10, 10, boundary_frame)
        added = incoming("way/2", geometry=geom)
        base = self.md()
        self.save(base, [added])
        md = self.md(overlay=self.store)
        neighbour = mapcells.CellKey(CELL.z, CELL.x + 1, CELL.y)
        a = md.building_cell(CELL).layers["buildings"]
        b = md.building_cell(neighbour).layers["buildings"]
        self.assertEqual(a.ids.tolist(), ["osm:way:2"])
        np.testing.assert_array_equal(a.lonlat, b.lonlat)
        self.assertEqual(int(a.own[0]) + int(b.own[0]), 1)
        left = buildings.road_tile_items(boundary_frame, -1, 0, md, ground)
        right = buildings.road_tile_items(boundary_frame, 0, 0, md, ground)
        self.assertEqual(len(left) + len(right), 1)

    def test_moved_replacement_disappears_from_old_cell_and_appears_in_new_cell(self):
        _, _, east, _ = CELL.bounds()
        boundary_frame = geodesy.EnuFrame(LAT, east)
        old = record(geometry=polygon(-15, 60, 10, 10, boundary_frame))
        new = incoming(geometry=polygon(15, 60, 10, 10, boundary_frame))
        base = self.md([old])
        self.save(base, [new])
        md = self.md([old], overlay=self.store)
        self.assertEqual(len(md.building_cell(CELL).layers["buildings"]), 0)
        other = mapcells.CellKey(CELL.z, CELL.x + 1, CELL.y)
        self.assertEqual(md.building_cell(other).layers["buildings"].ids.tolist(), ["gers-a"])

    def test_missing_neighbour_context_does_not_speculatively_render_an_addition(self):
        boundary_frame = geodesy.EnuFrame(LAT, CELL.bounds()[2])
        added = incoming("way/2", geometry=polygon(-5, 60, 10, 10, boundary_frame))
        self.save(self.md(), [added])
        md = self.md(overlay=self.store)
        neighbour = mapcells.CellKey(CELL.z, CELL.x + 1, CELL.y)
        cell = md.cell
        def incomplete(key, *args, **kwargs):
            original = cell(key, *args, **kwargs)
            return replace(original, layers=dict(original.layers, buildings=None)) if key == neighbour else original
        with mock.patch.object(md, "cell", side_effect=incomplete), self.assertRaisesRegex(BaselineMismatch, "neighbourhood"):
            md.building_cell(CELL)

    def test_cross_cell_parts_travel_with_the_full_parent(self):
        _, _, east, _ = CELL.bounds()
        frame = geodesy.EnuFrame(LAT, east)
        parent = replace(incoming("way/2", geometry=polygon(-20, 60, 40, 20, frame)), parts_complete=True)
        child = incoming("way/10", geometry=polygon(5, 62, 10, 10, frame), parent_id="way/2")
        base = self.md()
        self.save(base, [parent], [child])
        md = self.md(overlay=self.store)
        left = md.building_cell(CELL)
        self.assertEqual(left.layers["building_parts"].ids.tolist(), ["osm:way:10"])
        # The child itself is east of the cell edge, but the complete parent is present on both sides.
        right = md.building_cell(mapcells.CellKey(CELL.z, CELL.x + 1, CELL.y))
        np.testing.assert_array_equal(left.layers["building_parts"].lonlat, right.layers["building_parts"].lonlat)

    def test_overlay_density_stats_do_not_reuse_baseline_or_double_count_boundary_copies(self):
        _, _, east, _ = CELL.bounds()
        frame = geodesy.EnuFrame(LAT, east)
        base = self.md()
        self.save(base, [incoming("way/2", geometry=polygon(-5, 60, 10, 10, frame))])
        md = self.md(overlay=self.store)
        keys = (CELL, mapcells.CellKey(CELL.z, CELL.x + 1, CELL.y))
        buildings._cell_stats.clear()
        rows = []
        for key in keys:
            f = md.building_cell(key).layers["buildings"]
            rows.append(buildings.cell_footprints((key, md.building_dependencies((key,)).revision), f)[2])
        self.assertEqual(sum(len(r) for r in rows), 1)
        self.assertAlmostEqual(sum(float(r.sum()) for r in rows), 100, delta=1)

    def test_effective_provenance_and_part_links_survive_without_wide_json_columns(self):
        base = self.md()
        new = incoming("way/2")
        self.save(base, [new])
        cd = self.md(overlay=self.store).building_cell(CELL)
        f = cd.layers["buildings"]
        self.assertEqual(f.attrs["src_dataset"][0], "OpenStreetMap")
        self.assertEqual(f.overlay_provenance["osm:way:2"]["sources"][0]["dataset"], "OpenStreetMap")
        self.assertNotIn("attribute_sources", f.attrs)
        self.assertIn("osm-building-overlay", cd.credits())
        self.assertIn("OpenStreetMap", cd.credit_line())

    def test_feature_off_keeps_exact_cell_objects_and_generation_inputs(self):
        md = self.md([record()])
        a = md.cell(CELL)
        b = md.building_cell(CELL, layers=tuple(a.layers))
        self.assertEqual(a.digest(), b.digest())
        for name in a.layers:
            self.assertIs(a.layers[name], b.layers[name])
        rendered, baseline = buildings.road_context_items(FRAME, 0, 0, md, ground)
        self.assertIs(rendered, baseline)
        self.assertEqual(blockworker.building_versions(md, FRAME, (0, 0, 256, 256)), md.versions())


if __name__ == "__main__":
    unittest.main()
