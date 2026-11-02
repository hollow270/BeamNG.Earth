"""Stage 4B: real fixture workers plus deterministic, explicitly controlled asynchronous completion."""
from concurrent.futures import CancelledError, Future, ProcessPoolExecutor, ThreadPoolExecutor, TimeoutError
from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import queue
import socket
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import test_buildingrender as fx
import blockworker as bw
import buildingrefresh as br
import buildingoverlay as bo
from buildingoverlaystore import CorruptStorage, ResourceLimit, OverlayStore, LockTimeout
import buildings
import cdae
import geodesy
import geotiles
import mapcells
import revisit
import roads


class Dem:
    def sample(self, la, lo, zoom):
        return fx.ground(la, lo)


def state(md, directory):
    return SimpleNamespace(md=md, dem=Dem(), widths=None, surface=None, airports=None, sea=None, fra=None,
                           grading=False,
                           lock=threading.Lock(), cache_dir=str(directory), bridges={},
                           anchor=lambda *args: (None, None))


def init_worker(directory, originals, parts):
    # A real spawned process, deliberately starting without the store's current overlay.
    socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network forbidden"))
    md = fx.FixtureMapData(Path(directory) / "cells", originals, parts, overlay=False, street=True)
    bw._s = state(md, Path(directory) / "revisit")


def startup_overlay_active():
    return bw._s.md.building_overlays_active


def fake_receipt(request, snapshot, directory, vfs, identity, limits):
    # Concurrency tests isolate the publication state machine from CPU scheduling.
    Path(directory, "building.cdae").write_bytes(br._json(br._index(snapshot)).encode())
    files, content, meshes = br.scan(directory, vfs, limits)
    return dict(request=request.wire(), identity=identity, snapshot=snapshot.revision,
                baseline_release=fx.RELEASE, dependency=None, files=files, content_digest=content, mesh_digest=meshes, result={})


class Controlled:
    def __init__(self, md):
        self.md, self.queue, self.jobs = md, queue.Queue(), []

    def submit(self, *args):
        future = Future()
        future.set_running_or_notify_cancel()  # cancellation cannot stop a running worker
        job = (future, args)
        self.jobs.append(job)
        self.queue.put(job)
        return future

    def next(self):
        return self.queue.get(timeout=5)

    def finish(self, job, error=None):
        future, args = job
        if error is not None:
            future.set_exception(error)
        else:
            row = fake_receipt(*args)
            with self.md.building_snapshot(args[1]):
                row["dependency"] = args[0].inputs(self.md)[1]
            future.set_result(row)

    def cleanup(self):
        for job in self.jobs:
            if not job[0].done():
                self.finish(job, RuntimeError("test cleanup"))


class RefreshTest(unittest.TestCase):
    setUp = fx.RenderTest.setUp
    md = fx.RenderTest.md
    save = fx.RenderTest.save

    def close(self):
        for directory, fd in list(revisit._where.items()):
            if Path(directory).is_relative_to(self.root):
                os.close(fd)
                revisit._where.pop(directory)
        bw._geo_inputs.clear()
        fx.RenderTest.close(self)

    def setup_worker(self, originals=(), parts=(), street=False):
        self.base = self.md(originals, parts, street=street)
        self.worker_md = self.md(originals, parts, street=street)
        self.workers = ThreadPoolExecutor(2)
        self.addCleanup(self.workers.shutdown, wait=True, cancel_futures=True)
        mock.patch.object(bw, "_s", state(self.worker_md, self.root / "worker-cache")).start()
        self.coordinator = br.Coordinator(self.root / "catalog", self.base, self.store,
            lambda *args: self.workers.submit(br.generate, *args))
        return br.TileRequest("near", 0, 0, lat=fx.LAT, lon=fx.LON)

    def entry(self, request, coordinator=None):
        coordinator = coordinator or self.coordinator
        row = coordinator.outputs()["entries"][request.slot]
        return row, coordinator.io.objects / row["generation"]

    def test_modified_building_regenerates_real_mesh_and_retains_road_and_terrain_inputs(self):
        request = self.setup_worker([fx.record()], street=True)
        versions = self.base.versions()
        baseline = self.base.cell(fx.CELL).digest()
        self.coordinator.refresh([request])
        before, old_dir = self.entry(request)
        self.save(self.base, [fx.incoming(w=18)])
        result = self.coordinator.refresh()
        after, new_dir = self.entry(request)
        self.assertEqual(result["rebuilt"], (request.slot,))
        self.assertNotEqual(before["mesh_digest"], after["mesh_digest"])
        self.assertTrue(cdae.read(new_dir / "bld_1_0_0.cdae")["meshes"])
        for name in ("road_1_0_0.cdae", "walks_1_0_0.cdae"):
            self.assertEqual((old_dir/name).exists(), (new_dir/name).exists())
            if (old_dir/name).exists():
                self.assertEqual((old_dir / name).read_bytes(), (new_dir / name).read_bytes())
        self.assertTrue((old_dir/"road_1_0_0.cdae").exists())
        self.assertEqual(self.base.versions(), versions)
        self.assertEqual(self.base.cell(fx.CELL).digest(), baseline)
        self.assertTrue(old_dir.exists(), "held generations must remain available")

    def test_new_addition_disable_reenable_and_rollback_restore_baseline(self):
        request = self.setup_worker([fx.record()])
        self.coordinator.refresh([request])
        original, old_dir = self.entry(request)
        first = self.save(self.base, [fx.incoming("way/2", x=120)])
        self.coordinator.refresh()
        added, _ = self.entry(request)
        self.assertNotEqual(added["mesh_digest"], original["mesh_digest"])
        self.store.disable()
        self.coordinator.refresh()
        disabled, disabled_dir = self.entry(request)
        self.assertEqual(disabled["content_digest"], original["content_digest"])
        self.assertEqual((disabled_dir/"bld_1_0_0.cdae").read_bytes(), (old_dir/"bld_1_0_0.cdae").read_bytes())
        self.store.enable()
        self.coordinator.refresh()
        self.assertEqual(self.entry(request)[0]["mesh_digest"], added["mesh_digest"])
        self.save(self.base, [fx.incoming("way/2", x=120, w=20)])
        self.coordinator.refresh()
        self.store.rollback(first.revision)
        self.coordinator.refresh()
        self.assertEqual(self.entry(request)[0]["mesh_digest"], added["mesh_digest"])

    def test_unaffected_outputs_retain_the_same_files_and_identity_and_density_neighbours_rebuild(self):
        # District density is a real render dependency only in an arid region.
        mock.patch('surface.dry_west',return_value=True).start()
        request = self.setup_worker()
        neighbour, distant = replace(request, x=3), replace(request, x=12)
        self.coordinator.refresh([request, neighbour, distant])
        old, directory = self.entry(distant)
        self.save(self.base, [fx.incoming("way/2")])
        result = self.coordinator.refresh()
        self.assertEqual(set(result["rebuilt"]), {request.slot, neighbour.slot})
        self.assertEqual(result["reused"], (distant.slot,))
        self.assertEqual(self.entry(distant), (old, directory))
        before = self.coordinator.outputs()["entries"]
        self.assertFalse(self.coordinator.refresh()["rebuilt"])
        self.assertEqual(self.coordinator.outputs()["entries"], before)

    def test_cross_cell_and_render_tile_dependencies_are_complete_without_duplicates(self):
        _, _, east, _ = fx.CELL.bounds()
        frame = geodesy.EnuFrame(fx.LAT, east)
        new = fx.incoming("way/2", geometry=fx.polygon(-5, 60, 10, 10, frame))
        self.setup_worker()
        left = br.TileRequest("near", -1, 0, lat=fx.LAT, lon=east)
        right = replace(left, x=0)
        self.coordinator.refresh([left, right])
        self.save(self.base, [new])
        result = self.coordinator.refresh()
        self.assertEqual(set(result["rebuilt"]), {left.slot, right.slot})
        self.assertIn(str(fx.CELL), result["affected_cells"])
        self.assertIn(str(mapcells.CellKey(fx.CELL.z, fx.CELL.x+1, fx.CELL.y)), result["affected_cells"])
        with self.worker_md.building_snapshot(self.store.load()):
            a = buildings.road_tile_items(frame, -1, 0, self.worker_md, fx.ground)
            b = buildings.road_tile_items(frame, 0, 0, self.worker_md, fx.ground)
            self.assertEqual(len(a)+len(b), 1)

    def test_real_near_far_skyline_geotiles_share_snapshot_and_revisit_hits_survive_recreation(self):
        self.setup_worker([fx.record(w=60, d=60, attrs={"height": 40, "class": "retail"})])
        self.save(self.base, [fx.incoming(w=70, d=60, attrs={"height": 40})])
        requests = []
        centre = fx.FRAME.enu_to_geodetic(95.0, 90.0, 0.0)
        for kind, z in (("near_geo", geotiles.Z), ("far_geo", bw.far_geo_zoom("bldfar")),
                        ("skyline_geo", bw.far_geo_zoom("bldsky"))):
            x, y = geotiles.tile_of(float(centre[0]), float(centre[1]), z)
            req = br.TileRequest(kind, x, y, lat=fx.LAT, lon=fx.LON)
            requests.append(req)
            frame, box = req.geometry()
            x0, y0, x1, y1 = box
            la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), 0.0)
            for key in mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max())):
                self.worker_md.cell(key, layers=("buildings", "building_parts", "roads"))
        self.coordinator.refresh(requests)
        head = self.store.load().revision
        for req in requests:
            row, directory = self.entry(req)
            self.assertEqual(row["snapshot"], head)
            self.assertTrue(any(n.endswith(".cdae") for n in row["files"]))
        hits = (bw.geo_stats()["content_hits"], bw.far_geo_stats()["hits"])
        recreated = br.Coordinator(self.root / "other-catalog", self.base, self.store,
                                   lambda *args: self.workers.submit(br.generate, *args))
        recreated.refresh(requests)
        self.assertGreater(bw.geo_stats()["content_hits"], hits[0])
        self.assertGreaterEqual(bw.far_geo_stats()["hits"]-hits[1], 2)
        for req in requests:
            self.assertEqual(self.entry(req)[0]["content_digest"], self.entry(req, recreated)[0]["content_digest"])

    def test_content_hash_includes_mesh_bytes_with_identical_json_and_feature_off_hash_is_original(self):
        self.setup_worker()
        self.save(self.base, [fx.incoming("way/2")])
        directory = self.root / "hash-test"
        directory.mkdir()
        mesh = directory / "building.cdae"
        mesh.write_bytes(b"first mesh")
        args = ('{"n":1,"pivot":6,"relief":6}', "/fixture", str(directory), [mesh.name], fx.FRAME, (0,0,256,256))
        with self.worker_md.building_snapshot(self.store.load()):
            before = bw._building_content_hash(*args)
            mesh.write_bytes(b"different geometry")
            self.assertNotEqual(bw._building_content_hash(*args), before)
        self.assertEqual(bw._building_content_hash(*args), bw._content_hash(args[0], args[1]))

    def test_real_geotile_metadata_can_stay_identical_while_mesh_and_content_hash_change(self):
        self.setup_worker([fx.record()])
        self.save(self.base, [fx.incoming()])
        centre = fx.FRAME.enu_to_geodetic(65.0, 65.0, 0.0)
        x, y = geotiles.tile_of(float(centre[0]), float(centre[1]))
        request = br.TileRequest("near_geo", x, y, lat=fx.LAT, lon=fx.LON, options={"skin": False, "exact": None})
        self.coordinator.refresh([request])
        before, old_dir = self.entry(request)
        # Change the polygon inside the same AABB; collider boxes, count, pivot and relief remain identical.
        xy = np.array([(60,60), (70,60), (70,70), (65,70), (65,68), (60,68), (60,60)], float)
        la, lo, _ = fx.FRAME.enu_to_geodetic(xy[:,0], xy[:,1], 0.0)
        geometry = {"type": "Polygon", "coordinates": [np.c_[lo,la].tolist()]}
        self.save(self.base, [fx.incoming(geometry=geometry)])
        self.coordinator.refresh()
        after, new_dir = self.entry(request)
        def metadata(row, directory):
            path = next(directory.glob("*.json"))
            text = path.read_text("utf-8").replace("@BUILDING_OVERLAY@/"+row["generation"], "@V@")
            data = json.loads(text)
            self.assertIn("bld", data)
            return data.pop("h"), data
        ah, a = metadata(before, old_dir)
        bh, b = metadata(after, new_dir)
        self.assertEqual(a, b)
        self.assertNotEqual(ah, bh)
        self.assertNotEqual(before["mesh_digest"], after["mesh_digest"])

    def test_part_only_changes_rebuild_parent_and_unresolved_conflicts_reuse_accepted_output(self):
        parent = fx.record(attrs={"height": 30, "has_parts": True})
        part = fx.record("gers-part", 61, 61, 7, 7, parent_id="gers-a", osm="w10@1", attrs={"height": 14})
        request = self.setup_worker([parent], [part])
        self.coordinator.refresh([request])
        before, _ = self.entry(request)
        new = replace(fx.incoming(attrs={"height": 30}), parts_complete=True)
        newpart = fx.incoming("way/10", 61, 61, 8, 7, parent_id="way/1", attrs={"height": 22})
        self.save(self.base, [new], [newpart])
        self.coordinator.refresh()
        after, _ = self.entry(request)
        self.assertNotEqual(before["dependency"], after["dependency"])
        self.assertNotEqual(before["mesh_digest"], after["mesh_digest"])
        with self.worker_md.building_snapshot(self.store.load()):
            parts = self.worker_md.building_cell(fx.CELL).layers["building_parts"]
            self.assertEqual(parts.ids.tolist(), ["gers-part"])
            self.assertEqual(parts.attrs["building_id"].tolist(), ["gers-a"])
        commit = self.save(self.base, [fx.incoming("way/99", x=65)])
        self.assertTrue(fx.outcomes(commit.merge_result, "conflict"))
        self.assertFalse(self.coordinator.refresh()["rebuilt"])
        self.assertEqual(self.entry(request)[0], after)

    def test_legacy_far_and_skyline_worker_paths_are_revision_pinned(self):
        self.setup_worker([fx.record(w=60, d=60, attrs={"height": 40, "class": "retail"})])
        self.save(self.base, [fx.incoming(w=70, d=60, attrs={"height": 40})])
        far = br.TileRequest("far", 0, 0, lat=fx.LAT, lon=fx.LON)
        sky = replace(far, kind="skyline")
        import buildings
        for key in roads.cells_of_tile(fx.FRAME, 0, 0, margin=buildings.SKY_UNIT*roads.TILE):
            self.worker_md.cell(key, layers=("roads", "buildings"))
        result = self.coordinator.refresh([far, sky])
        self.assertEqual(set(result["rebuilt"]), {far.slot, sky.slot})
        for req in (far, sky):
            row, _ = self.entry(req)
            self.assertEqual(row["snapshot"], self.store.load().revision)
            self.assertTrue(any(name.endswith(".cdae") for name in row["files"]))

    def test_settings_changes_replace_only_the_requested_slot_and_missing_files_rebuild(self):
        request = self.setup_worker([fx.record()], street=True)
        distant = replace(request, x=12)
        self.coordinator.refresh([request, distant])
        previous, _ = self.entry(distant)
        changed = replace(request, options={"mph": False})
        result = self.coordinator.refresh([changed])
        self.assertEqual(result["rebuilt"], (request.slot,))
        self.assertEqual(self.entry(distant)[0], previous)
        row, directory = self.entry(changed)
        (directory/"bld_1_0_0.cdae").unlink()
        self.assertEqual(self.coordinator.refresh()["rebuilt"], (request.slot,))
        new, new_dir = self.entry(changed)
        self.assertEqual(new["mesh_digest"], row["mesh_digest"])
        self.assertTrue((new_dir/"bld_1_0_0.cdae").exists())

    def test_unregistered_remote_store_edits_do_not_change_local_outputs_or_affected_cells(self):
        request = self.setup_worker()
        self.coordinator.refresh([request])
        before = self.entry(request)
        self.save(self.base, [fx.incoming("way/2", x=6000)])
        result = self.coordinator.refresh()
        self.assertFalse(result["rebuilt"])
        self.assertFalse(result["affected_cells"])
        self.assertEqual(self.entry(request), before)

    def test_existing_mapdata_contexts_are_thread_local_and_restore_after_errors(self):
        self.setup_worker()
        self.save(self.base, [fx.incoming("way/2")])
        snapshot = self.store.load()
        entered, leave = threading.Event(), threading.Event()
        def pinned():
            try:
                with self.worker_md.building_snapshot(snapshot):
                    entered.set()
                    self.assertTrue(leave.wait(5))
                    self.assertEqual(self.worker_md.building_cell(fx.CELL).layers["buildings"].ids.tolist(), ["osm:way:2"])
                    raise ValueError("controlled error")
            except ValueError:
                return self.worker_md.building_overlays_active
        future = self.workers.submit(pinned)
        self.assertTrue(entered.wait(5))
        try:
            self.assertFalse(self.worker_md.building_overlays_active)
            self.assertEqual(len(self.worker_md.building_cell(fx.CELL).layers["buildings"]), 0)
            self.store.disable()  # in-flight job must retain its explicitly supplied snapshot
        finally:
            leave.set()
        self.assertFalse(future.result(5))

    def setup_controlled(self, *, limits=br.Limits()):
        self.base = self.md()
        self.controller = Controlled(self.base)
        self.runners = ThreadPoolExecutor(2)
        self.addCleanup(self.runners.shutdown, wait=True, cancel_futures=True)
        self.addCleanup(self.controller.cleanup)
        self.coordinator = br.Coordinator(self.root / "catalog", self.base, self.store,
                                         self.controller.submit, limits=limits)
        self.request = br.TileRequest("near", 0, 0, lat=fx.LAT, lon=fx.LON)

    def run_controlled(self, requests=()):
        result = self.runners.submit(self.coordinator.refresh, requests)
        self.controller.finish(self.controller.next())
        return result.result(5)

    def test_older_completion_cannot_overwrite_a_newer_publication_across_coordinators(self):
        self.setup_controlled()
        self.save(self.base, [fx.incoming("way/2")])
        old = self.runners.submit(self.coordinator.refresh, [self.request])
        old_job = self.controller.next()
        self.save(self.base, [fx.incoming("way/2", w=20)])
        other = br.Coordinator(self.root / "catalog", self.base, self.store, self.controller.submit)
        new = self.runners.submit(other.refresh)
        new_job = self.controller.next()
        self.controller.finish(new_job)
        latest = new.result(5)
        published = other.outputs()["entries"]
        self.controller.finish(old_job)
        with self.assertRaises(br.StaleGeneration):
            old.result(5)
        self.assertEqual(other.outputs()["entries"], published)
        self.assertEqual(other.outputs()["snapshot"], latest["snapshot"])
        self.assertFalse(any((other.io.root/"work").iterdir()))

    def test_store_change_without_another_refresh_rejects_stale_job_and_retry_succeeds(self):
        self.setup_controlled()
        self.run_controlled([self.request])
        old = self.coordinator.outputs()["entries"]
        self.save(self.base, [fx.incoming("way/2")])
        future = self.runners.submit(self.coordinator.refresh)
        job = self.controller.next()
        self.save(self.base, [fx.incoming("way/2", w=20)])
        self.controller.finish(job)
        with self.assertRaises(br.StaleGeneration):
            future.result(5)
        self.assertEqual(self.coordinator.outputs()["entries"], old)
        self.run_controlled()
        self.assertNotEqual(self.coordinator.outputs()["entries"], old)

    def test_cancellation_waits_for_worker_before_cleaning_private_files(self):
        self.setup_controlled()
        self.run_controlled([self.request])
        previous = self.coordinator.outputs()["entries"]
        self.save(self.base, [fx.incoming("way/2")])
        cancel = threading.Event()
        result = self.runners.submit(self.coordinator.refresh, cancel=cancel)
        job = self.controller.next()
        directory = Path(job[1][2])
        cancel.set()
        with self.assertRaises(CancelledError):
            result.result(5)
        self.assertTrue(directory.exists(), "running worker still owns this directory")
        self.controller.finish(job)
        self.assertFalse(directory.exists())
        self.assertEqual(self.coordinator.outputs()["entries"], previous)
        self.run_controlled()

    def test_failure_and_partial_batch_leave_previous_catalog_and_files_available(self):
        self.setup_controlled()
        self.run_controlled([self.request])
        previous = self.coordinator.outputs()["entries"]
        self.save(self.base, [fx.incoming("way/2")])
        result = self.runners.submit(self.coordinator.refresh, [replace(self.request, x=1)])
        self.controller.finish(self.controller.next())
        self.controller.finish(self.controller.next(), RuntimeError("worker died"))
        with self.assertRaisesRegex(RuntimeError, "worker died"):
            result.result(5)
        self.assertEqual(self.coordinator.outputs()["entries"], previous)
        self.assertTrue(self.coordinator._valid(previous[self.request.slot]))
        self.assertFalse(any((self.coordinator.io.root/"work").iterdir()))

    def test_timeout_does_not_release_running_worker_capacity_or_previous_output(self):
        self.setup_controlled(limits=replace(br.Limits(), jobs=1, timeout=0.2))
        self.run_controlled([self.request])
        before = self.coordinator.outputs()["entries"]
        self.save(self.base, [fx.incoming("way/2")])
        result = self.runners.submit(self.coordinator.refresh)
        job = self.controller.next()
        with self.assertRaises(TimeoutError):
            result.result(5)
        directory = Path(job[1][2])
        self.assertTrue(directory.exists())
        count = len(self.controller.jobs)
        with self.assertRaises(TimeoutError):
            self.coordinator.refresh()
        self.assertEqual(len(self.controller.jobs), count)
        self.assertEqual(self.coordinator.outputs()["entries"], before)
        self.controller.finish(job)
        self.assertFalse(directory.exists())
        self.run_controlled()

    def test_receipt_for_a_different_revision_is_never_published(self):
        self.setup_controlled()
        self.save(self.base, [fx.incoming("way/2")])
        future = self.runners.submit(self.coordinator.refresh, [self.request])
        job = self.controller.next()
        receipt = fake_receipt(*job[1])
        with self.base.building_snapshot(job[1][1]):
            receipt["dependency"] = self.request.inputs(self.base)[1]
        receipt["snapshot"] = "0"*64
        job[0].set_result(receipt)
        with self.assertRaises(br.StaleGeneration):
            future.result(5)
        self.assertFalse(self.coordinator.outputs()["entries"])

    def test_overlay_writer_lock_covers_the_catalog_publication_boundary(self):
        request = self.setup_worker()
        self.save(self.base, [fx.incoming("way/2")])
        competitor = OverlayStore(self.store.directory, lock_timeout=0.02)
        save = self.coordinator._save
        checks = []
        def checked(state):
            if state["entries"]:
                with self.assertRaises(LockTimeout):
                    competitor.disable()
                checks.append(True)
            return save(state)
        with mock.patch.object(self.coordinator, "_save", side_effect=checked):
            self.coordinator.refresh([request])
        self.assertEqual(checks, [True])
        self.assertTrue(self.store.load().enabled)

    def test_atomic_catalog_write_failure_preserves_previous_and_restart_recovers(self):
        request = self.setup_worker([fx.record()])
        self.coordinator.refresh([request])
        old = self.coordinator.outputs()
        self.save(self.base, [fx.incoming(w=18)])
        write = self.coordinator.io._atomic_write
        def fail_publication(path, data):
            if json.loads(data)["payload"]["publication"] != old["publication"]:
                raise OSError("injected publication failure")
            return write(path, data)
        with mock.patch.object(self.coordinator.io, "_atomic_write", side_effect=fail_publication), self.assertRaises(OSError):
            self.coordinator.refresh()
        self.assertEqual(self.coordinator.outputs()["entries"], old["entries"])
        restarted = br.Coordinator(self.root/"catalog", self.base, self.store,
                                    lambda *args: self.workers.submit(br.generate, *args))
        self.assertEqual(restarted.refresh()["snapshot"], self.store.load().revision)
        self.assertNotEqual(restarted.outputs()["entries"], old["entries"])

    def test_real_spawned_worker_recreation_uses_requested_revision_and_restores_startup_state(self):
        self.base = self.md([fx.record()], street=True)
        self.save(self.base, [fx.incoming(w=18)])
        request = br.TileRequest("near", 0, 0, lat=fx.LAT, lon=fx.LON)
        ctx = multiprocessing.get_context("spawn")
        def run():
            with ProcessPoolExecutor(1, mp_context=ctx, initializer=init_worker,
                                     initargs=(str(self.root/"spawn"), self.base.originals, ())) as pool:
                coordinator = br.Coordinator(self.root/"catalog", self.base, self.store,
                    lambda *args: pool.submit(br.generate, *args))
                result = coordinator.refresh([request])
                self.assertFalse(pool.submit(startup_overlay_active).result(10))
                return result, coordinator.outputs()["entries"][request.slot]
        first, entry = run()
        self.assertEqual(entry["snapshot"], self.store.load().revision)
        self.save(self.base, [fx.incoming(w=22)])
        second, other = run()
        self.assertNotEqual(first["snapshot"], second["snapshot"])
        self.assertNotEqual(entry["mesh_digest"], other["mesh_digest"])

    def test_server_manual_interface_prepares_without_touching_live_files_or_trackers(self):
        request = self.setup_worker([fx.record()])
        self.base._building_store = self.store
        self.save(self.base, [fx.incoming(w=18)])
        live = self.root / "live"
        live.mkdir()
        held = live/"held.cdae"
        held.write_bytes(b"active resource")
        import earthd
        server = SimpleNamespace(mapdata=self.base, buildings=True, road_pool=self.workers, far_bld_pool=self.workers,
                                 cache_dir=str(self.root/"server-cache"), out_dir=str(live),
                                 _building_refresh_lock=threading.Lock(), _far_geo_done={"held": "original"})
        result = earthd.Server.refresh_building_overlays(server, [request])
        self.assertTrue(result["rebuilt"])
        self.assertEqual(held.read_bytes(), b"active resource")
        self.assertEqual(list(live.iterdir()), [held])
        self.assertEqual(server._far_geo_done, {"held": "original"})

    def test_invalid_requests_limits_and_corrupt_catalog_fail_closed(self):
        self.setup_controlled(limits=replace(br.Limits(), requests=1))
        with self.assertRaises(ResourceLimit):
            self.coordinator.refresh([self.request, replace(self.request, x=1)])
        with self.assertRaises(ValueError):
            br.TileRequest("near", 0, 0, options={"unknown": True})
        with self.assertRaises(ValueError):
            br.TileRequest("near_geo", -1, 0)
        self.coordinator.path.write_bytes(b'{"schema":1')
        with self.assertRaises(CorruptStorage):
            self.coordinator.refresh()


if __name__ == "__main__":
    unittest.main()
