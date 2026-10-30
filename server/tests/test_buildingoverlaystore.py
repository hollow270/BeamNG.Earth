"""Persistent overlay tests. Every file, subprocess and fixture stays in the repository."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import buildingoverlay as bo
import buildingoverlaystore as storage
from test_buildingoverlay import baseline, incoming, box, outcomes


REPO = Path(__file__).resolve().parents[2]
REGION = (-0.001, -0.001, 0.002, 0.002)
OTHER_REGION = (1.0, 1.0, 1.001, 1.001)
INFO = storage.RefreshInfo(REGION, "2026-10-29T12:00:00Z",
                           ({"dataset": "OpenStreetMap", "license": "ODbL-1.0", "export": "synthetic"},),
                           "synthetic-overture-1", {"fixture": True})


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".overlay-test-", dir=REPO)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = storage.OverlayStore(self.directory)

    def save(self, *, old=None, new=None, info=INFO, store=None, **kw):
        return (store or self.store).refresh([baseline()] if old is None else old,
                                             [incoming(geometry=box(w=12))] if new is None else new,
                                             info=info, **kw)

    def apply(self, *, old=None, store=None, bounds=REGION, release=INFO.baseline_release, **kw):
        return (store or self.store).apply([baseline()] if old is None else old,
                                           baseline_release=release, bounds=bounds, **kw)

    def test_new_store_disabled_and_empty(self):
        state = self.store.load()
        self.assertIsNone(state.revision)
        self.assertFalse(state.enabled)
        self.assertFalse(state.entries)
        self.assertEqual(self.apply().buildings, (baseline(),))

    def test_save_load_and_restart(self):
        commit = self.save()
        self.assertTrue(commit.changed)
        self.assertEqual(len(commit.revision), 64)
        state = self.store.load()
        self.assertEqual(state.revision, commit.revision)
        self.assertEqual(len(state.overrides), 1)
        self.assertFalse(state.additions)
        self.assertEqual(state.overrides[0].feature.feature_id, "gers-a")
        self.assertEqual(state.refreshes[0]["info"]["source_timestamp"], INFO.source_timestamp)
        self.assertEqual(state.refreshes[0]["info"]["bounds"], list(REGION))
        self.assertEqual(state.refreshes[0]["info"]["provenance"], list(INFO.provenance))
        reopened = storage.OverlayStore(self.directory)
        self.assertEqual(reopened.load(), state)
        reopened.enable()
        self.assertEqual(bo.geometry_digest(self.apply(store=reopened).buildings[0].geometry),
                         bo.geometry_digest(box(w=12)))

    def test_additions_are_separate_from_overrides(self):
        self.save(new=[incoming(geometry=box(w=12)), incoming("way/2", box(30))])
        state = self.store.load()
        self.assertEqual(len(state.overrides), 1)
        self.assertEqual(state.additions[0].feature.feature_id, "osm:way:2")
        payload = json.loads((self.store.objects / f"{state.revision}.json").read_text("utf-8"))["payload"]
        self.assertIn("overrides", payload)
        self.assertIn("additions", payload)
        self.assertNotIn("baseline", payload)
        self.assertEqual(len(payload["overrides"][0]["baseline_digest"]), 64)

    def test_deterministic_revision_across_stores_and_input_order(self):
        olds = [baseline(), baseline("gers-b", "w2", box(30))]
        news = [incoming(geometry=box(w=12)), incoming("way/2", box(30)), incoming("way/3", box(60))]
        a = self.save(old=olds, new=news)
        other = storage.OverlayStore(self.directory / "second")
        b = self.save(old=list(reversed(olds)), new=list(reversed(news)), store=other)
        self.assertEqual(a.revision, b.revision)
        self.store.enable()
        other.enable()
        self.assertEqual(self.store.dependencies(REGION), other.dependencies(REGION))

    def test_repeat_identical_refresh_is_noop(self):
        a = self.save()
        state_bytes = (self.store.root / "state.json").read_bytes()
        b = self.save()
        self.assertEqual(a.revision, b.revision)
        self.assertFalse(b.changed)
        self.assertIsNone(b.affected_bounds)
        self.assertEqual((self.store.root / "state.json").read_bytes(), state_bytes)
        self.assertEqual(len(self.store.load().history), 1)

    def test_enable_disable_reenable_survives_restart(self):
        self.save()
        baseline_digest = bo.MergeResult((baseline(),), (), ()).digest()
        self.assertEqual(self.apply().digest(), baseline_digest)
        self.store.enable()
        overlay_digest = self.apply().digest()
        token = self.store.revision_for(REGION)
        self.assertNotEqual(overlay_digest, baseline_digest)
        self.store.disable()
        reopened = storage.OverlayStore(self.directory)
        self.assertFalse(reopened.load().enabled)
        self.assertEqual(self.apply(store=reopened).digest(), baseline_digest)
        self.assertFalse(reopened.dependencies(REGION).feature_ids)
        reopened.enable()
        self.assertEqual(self.apply(store=reopened).digest(), overlay_digest)
        self.assertEqual(reopened.revision_for(REGION), token)

    def test_disabled_store_allows_a_different_baseline_release(self):
        self.save()
        self.assertEqual(self.apply(release="another-release").buildings, (baseline(),))
        self.store.enable()
        with self.assertRaises(storage.BaselineMismatch):
            self.apply(release="another-release")

    def test_rollback_and_branch(self):
        a = self.save()
        b = self.save(new=[incoming(geometry=box(w=14))], info=replace(INFO, source_timestamp="2026-10-30T12:00:00Z"))
        self.store.enable()
        before = self.store.revision_for(REGION)
        restored = self.store.rollback()
        self.assertEqual(restored.revision, a.revision)
        self.assertTrue(restored.enabled)
        self.assertNotEqual(self.store.revision_for(REGION), before)
        self.assertEqual(bo.geometry_digest(self.apply().buildings[0].geometry), bo.geometry_digest(box(w=12)))
        c = self.save(new=[incoming(geometry=box(w=13))])
        self.assertNotIn(b.revision, self.store.load().history)
        self.assertEqual(self.store.load().history, (a.revision, c.revision))
        with self.assertRaises(ValueError):
            self.store.rollback(b.revision)

    def test_rollback_first_revision_to_baseline(self):
        self.save()
        self.store.enable()
        restored = self.store.rollback()
        self.assertIsNone(restored.revision)
        self.assertTrue(restored.enabled)
        self.assertFalse(restored.entries)
        self.assertEqual(self.apply().buildings, (baseline(),))
        with self.assertRaises(ValueError):
            self.store.rollback()

    def test_targeted_rollback(self):
        a = self.save()
        self.save(new=[incoming(geometry=box(w=14))])
        self.save(new=[incoming(geometry=box(w=16))])
        self.assertEqual(self.store.rollback(a.revision).revision, a.revision)

    def test_overlapping_regions_preserve_omitted_edits(self):
        a = self.save(new=[incoming(geometry=box(w=12)), incoming("way/2", box(30))])
        self.store.enable()
        b = self.save(new=[incoming("way/3", box(60))],
                      info=replace(INFO, bounds=(-0.0005, -0.0005, 0.0015, 0.0015)))
        self.assertNotEqual(a.revision, b.revision)
        state = self.store.load()
        self.assertEqual([e.feature.feature_id for e in state.entries], ["gers-a", "osm:way:2", "osm:way:3"])
        self.assertEqual(len(self.apply().buildings), 3)
        # Omitting every incoming building still removes nothing.
        self.save(new=[])
        self.assertEqual(len(self.apply().buildings), 3)

    def test_previously_added_building_updates_in_place(self):
        self.save(old=[], new=[incoming("way/2", box(30))])
        self.save(old=[], new=[incoming("w2@2", box(30, w=12))])
        state = self.store.load()
        self.assertEqual(len(state.additions), 1)
        self.assertFalse(state.overrides)
        self.assertEqual(state.additions[0].feature.feature_id, "osm:way:2")

    def test_older_overlapping_snapshot_is_an_unresolved_conflict(self):
        self.save(info=replace(INFO, source_timestamp="2026-10-30T12:00:00Z"))
        prior = self.store.load().entries
        commit = self.save(new=[incoming(geometry=box(w=14))])
        self.assertEqual(self.store.load().entries, prior)
        self.assertEqual(outcomes(commit.merge_result, "conflict")[0].reason, "stale_source_timestamp")
        self.assertEqual(len(self.store.load().conflicts), 1)

    def test_unrelated_area_revision_is_unchanged(self):
        self.store.enable()
        before = self.store.dependencies(OTHER_REGION)
        self.save()
        self.assertEqual(self.store.dependencies(OTHER_REGION), before)
        local_before = self.store.dependencies(REGION)
        distant = incoming("way/90")
        distant = replace(distant, geometry={"type": "Polygon", "coordinates": [
            [[x + 1.0002, y + 1.0002] for x, y in box()["coordinates"][0]]]})
        self.save(old=[], new=[distant], info=replace(INFO, bounds=OTHER_REGION))
        self.assertEqual(self.store.dependencies(REGION), local_before)
        self.assertNotEqual(self.store.dependencies(OTHER_REGION), before)

    def test_timestamp_only_refresh_changes_history_without_changing_dependencies(self):
        self.save()
        self.store.enable()
        before = self.store.dependencies(REGION)
        commit = self.save(info=replace(INFO, source_timestamp="2026-10-30T12:00:00Z"))
        self.assertTrue(commit.changed)
        self.assertIsNone(commit.affected_bounds)
        self.assertEqual(self.store.dependencies(REGION), before)

    def test_moved_addition_changes_old_and_new_area_tokens(self):
        west = (-0.0001, -0.0001, 0.00015, 0.00015)
        east = (0.0003, -0.0001, 0.0006, 0.00015)
        self.store.enable()
        self.save(old=[], new=[incoming()])
        old_west, old_east = self.store.revision_for(west), self.store.revision_for(east)
        commit = self.save(old=[], new=[incoming(geometry=box(40))])
        self.assertNotEqual(self.store.revision_for(west), old_west)
        self.assertNotEqual(self.store.revision_for(east), old_east)
        self.assertLessEqual(commit.affected_bounds[0], 0)
        self.assertGreaterEqual(commit.affected_bounds[2], 50 / 111320)

    def test_move_outside_prior_selection_cannot_bypass_stored_parts_or_timestamp(self):
        parent = incoming(parts_complete=True)
        child = incoming("way/10", box(1, 1, 3, 3), parent_id="way/1")
        newer = replace(INFO, source_timestamp="2026-10-30T12:00:00Z")
        self.save(old=[], new=[parent], incoming_parts=[child], info=newer)
        prior = self.store.load().entries
        moved_info = replace(newer, bounds=(0.004, -0.001, 0.006, 0.002))
        moved = incoming(geometry=box(500))
        result = self.save(old=[], new=[moved], info=moved_info)
        self.assertEqual(outcomes(result.merge_result, "conflict")[0].reason,
                         "complete_parts_required_for_parent_update")
        self.assertEqual(self.store.load().entries, prior)
        moved_child = incoming("way/10", box(501, 1, 3, 3), parent_id="way/1")
        result = self.save(old=[], new=[replace(moved, parts_complete=True)], incoming_parts=[moved_child],
                           info=replace(moved_info, source_timestamp=INFO.source_timestamp))
        self.assertEqual(outcomes(result.merge_result, "conflict")[0].reason, "stale_source_timestamp")
        self.assertEqual(self.store.load().entries, prior)
        result = self.save(old=[], new=[replace(moved, parts_complete=True)], incoming_parts=[moved_child], info=moved_info)
        self.assertFalse(outcomes(result.merge_result, "conflict"))
        state = self.store.load()
        self.assertEqual(len(state.entries), 1)
        self.assertEqual(state.entries[0].parts[0].parent_id, "osm:way:1")
        self.assertEqual(bo.geometry_digest(state.entries[0].feature.geometry), bo.geometry_digest(box(500)))

    def test_conflicts_persist_without_changing_effective_overlay(self):
        self.save()
        prior = self.store.load().entries
        commit = self.save(new=[incoming("way/99", box(4))])
        self.assertEqual(self.store.load().entries, prior)
        self.assertEqual(len(outcomes(commit.merge_result, "conflict")), 1)
        reopened = storage.OverlayStore(self.directory)
        self.assertEqual(len(reopened.load().conflicts), 1)
        # A later valid edit does not silently mark the earlier conflict resolved.
        self.save(new=[incoming(geometry=box(w=13))])
        self.assertEqual(len(self.store.load().conflicts), 1)
        self.store.enable()
        self.assertEqual(len(outcomes(self.apply(), "conflict")), 1)

    def test_deletion_and_unsupported_geometry_stay_conflicts(self):
        prior = self.save()
        for f in (replace(incoming(), status="deleted", geometry=None),
                  incoming(geometry={"type": "MultiPolygon", "coordinates": [box()["coordinates"]]})):
            self.save(new=[f])
        self.assertEqual(len(self.store.load().conflicts), 2)
        self.assertEqual(len(self.store.load().entries), 1)
        self.store.rollback(prior.revision)
        self.assertFalse(self.store.load().conflicts)

    def test_parts_survive_restart_and_update_atomically(self):
        parent = baseline(attrs={"height": 15, "has_parts": True})
        part = baseline("gers-part", "w10", box(1, 1, 3, 3), parent_id="gers-a", attrs={"height": 10})
        child = incoming("way/10", box(1, 1, 4, 3), parent_id="way/1", attrs={"height": 12})
        self.save(old=[parent], new=[incoming(parts_complete=True)], baseline_parts=[part], incoming_parts=[child])
        reopened = storage.OverlayStore(self.directory)
        reopened.enable()
        result = self.apply(old=[parent], baseline_parts=[part], store=reopened)
        self.assertEqual(result.parts[0].feature_id, "gers-part")
        self.assertEqual(result.parts[0].parent_id, "gers-a")
        self.assertEqual(result.parts[0].attrs["height"], 12)
        previous = reopened.load().entries
        invalid = replace(child, geometry=box(50))
        commit = self.save(old=[parent], new=[incoming(parts_complete=True)], baseline_parts=[part],
                           incoming_parts=[invalid], store=reopened)
        self.assertEqual(reopened.load().entries, previous)
        self.assertEqual(len(outcomes(commit.merge_result, "conflict")), 1)

    def test_baseline_and_caller_data_are_immutable(self):
        old, new = baseline(attrs={"height": 12}), incoming(geometry=box(w=12))
        original = deepcopy((old, new))
        self.save(old=[old], new=[new])
        self.store.enable()
        self.apply(old=[old])
        loaded = self.store.load()
        loaded.entries[0].feature.attrs["height"] = 999
        loaded.refreshes[0]["info"]["metadata"]["fixture"] = False
        self.assertEqual((old, new), original)
        self.assertEqual(self.store.load().entries[0].feature.attrs["height"], 12)
        self.assertTrue(self.store.load().refreshes[0]["info"]["metadata"]["fixture"])

    def test_changed_or_missing_baseline_requires_explicit_migration(self):
        self.save()
        before = self.store.load()
        with self.assertRaises(storage.BaselineMismatch):
            self.save(old=[baseline(attrs={"height": 999})])
        with self.assertRaises(storage.BaselineMismatch):
            self.save(old=[])
        with self.assertRaises(storage.BaselineMismatch):
            self.save(info=replace(INFO, baseline_release="new-release"))
        self.assertEqual(self.store.load(), before)

    def test_untrusted_area_and_metadata_rejected_before_commit(self):
        before = self.store.load()
        cases = [replace(INFO, bounds=(1, 0, -1, 1)), replace(INFO, bounds=(-1, -1, 1, 1)),
                 replace(INFO, bounds=(float("nan"), 0, 1, 1)), replace(INFO, source_timestamp="2026-10-30"),
                 replace(INFO, provenance=()), replace(INFO, metadata={"bad": float("inf")})]
        for info in cases:
            with self.subTest(info=info), self.assertRaises((ValueError, TypeError)):
                self.save(info=info)
        with self.assertRaises(ValueError):
            self.save(info=replace(INFO, bounds=OTHER_REGION))
        self.assertEqual(self.store.load(), before)

    def test_write_failures_leave_previous_revision_and_retry_recovers(self):
        self.save()
        for stage in ("object", "backup", "state"):
            before = self.store.load()
            replace_file = os.replace
            def fail(src, dst):
                dst = Path(dst)
                if (stage == "object" and dst.parent == self.store.objects) or \
                        (stage == "backup" and dst.name == "previous.json") or \
                        (stage == "state" and dst.name == "state.json"):
                    raise OSError("injected write failure")
                return replace_file(src, dst)
            new = incoming(geometry=box(w=13 + len(before.history)))
            with self.subTest(stage=stage), mock.patch.object(storage.os, "replace", side_effect=fail):
                with self.assertRaises(OSError):
                    self.save(new=[new])
            self.assertEqual(self.store.load(), before)
            self.assertFalse(list(self.store.root.rglob("*.tmp")))
            self.save(new=[new])
            self.assertNotEqual(self.store.load().revision, before.revision)

    def test_fsync_failure_does_not_publish(self):
        self.save()
        before = self.store.load()
        with mock.patch.object(storage.os, "fsync", side_effect=OSError("disk sync failed")):
            with self.assertRaises(OSError):
                self.save(new=[incoming(geometry=box(w=13))])
        self.assertEqual(self.store.load(), before)

    def test_corrupt_manifest_requires_explicit_recovery(self):
        a = self.save()
        self.save(new=[incoming(geometry=box(w=14))])
        (self.store.root / "state.json").write_bytes(b'{"truncated":')
        reopened = storage.OverlayStore(self.directory)
        with self.assertRaises(storage.CorruptStorage):
            reopened.load()
        restored = reopened.recover()
        self.assertEqual(restored.revision, a.revision)
        with self.assertRaises(ValueError):
            reopened.recover()

    def test_missing_manifest_is_not_reset_and_can_recover(self):
        a = self.save()
        self.save(new=[incoming(geometry=box(w=14))])
        (self.store.root / "state.json").unlink()
        reopened = storage.OverlayStore(self.directory)
        with self.assertRaises(storage.CorruptStorage):
            reopened.load()
        self.assertEqual(reopened.recover().revision, a.revision)

    def test_corrupt_or_missing_head_object_can_recover_prior_revision(self):
        a = self.save()
        b = self.save(new=[incoming(geometry=box(w=14))])
        path = self.store.objects / f"{b.revision}.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(storage.CorruptStorage):
            self.store.load()
        self.assertEqual(self.store.recover().revision, a.revision)
        path.unlink()
        self.assertEqual(self.store.load().revision, a.revision)

    def test_corrupt_backup_is_not_guessed_from_orphans(self):
        self.save()
        (self.store.root / "state.json").write_bytes(b"bad")
        (self.store.root / "previous.json").write_bytes(b"bad")
        with self.assertRaises(storage.CorruptStorage):
            self.store.recover()
        with self.assertRaises(storage.CorruptStorage):
            self.store.load()

    def test_corrupt_historical_object_does_not_publish_failed_rollback(self):
        a = self.save()
        b = self.save(new=[incoming(geometry=box(w=14))])
        (self.store.objects / f"{a.revision}.json").unlink()
        with self.assertRaises(storage.CorruptStorage):
            self.store.rollback(a.revision)
        self.assertEqual(self.store.load().revision, b.revision)

    def test_duplicate_json_keys_unknown_schema_and_path_traversal_fail_closed(self):
        self.save()
        state = self.store.root / "state.json"
        original = state.read_bytes()
        for content in (b'{"schema":1,"schema":1}',
                        self.store._document({"head": "../escape", "enabled": True, "history": [], "affected_bounds": None}),
                        b'{"schema":99,"payload":{},"checksum":"x"}'):
            state.write_bytes(content)
            with self.subTest(content=content), self.assertRaises(storage.CorruptStorage):
                self.store.load()
        state.write_bytes(original)
        self.assertIsNotNone(self.store.load().revision)

    def test_oversized_document_is_not_read_unbounded(self):
        self.save()
        (self.store.root / "state.json").write_bytes(b"x" * 2000)
        constrained = storage.OverlayStore(self.directory, limits=replace(storage.Limits(), max_file_bytes=1024))
        with self.assertRaises(storage.CorruptStorage):
            constrained.load()

    def test_checksumming_does_not_bypass_schema_or_geometry_validation(self):
        commit = self.save()
        state_path = self.store.root / "state.json"
        original_manifest = self.store._read(state_path)
        original_payload = self.store._object(commit.revision)
        for mutation in ("geometry", "bounds", "parts", "parent", "metadata"):
            payload = deepcopy(original_payload)
            if mutation == "geometry":
                payload["overrides"][0]["feature"]["geometry"] = {"type": "Point", "coordinates": [0, 0]}
            elif mutation == "bounds":
                payload["overrides"][0]["bounds"] = [1, 1, 2, 2]
            elif mutation == "parts":
                payload["overrides"][0]["feature"]["attrs"]["has_parts"] = True
            elif mutation == "parent":
                payload["parent"] = "f" * 64
            else:
                next(iter(payload["refreshes"].values()))["info"]["source_timestamp"] = "invalid"
            revision = storage._hash(payload)
            (self.store.objects / f"{revision}.json").write_bytes(self.store._document(payload))
            manifest = dict(original_manifest, head=revision, history=[revision])
            state_path.write_bytes(self.store._document(manifest))
            with self.subTest(mutation=mutation), self.assertRaises(storage.CorruptStorage):
                self.store.load()
        state_path.write_bytes(self.store._document(original_manifest))
        self.assertEqual(self.store.load().revision, commit.revision)

    def test_limits_preserve_existing_data(self):
        self.save()
        before = self.store.load()
        for limits in (replace(storage.Limits(), max_revisions=1),
                       replace(storage.Limits(), max_total_bytes=1024),
                       replace(storage.Limits(), max_refreshes=1)):
            limited = storage.OverlayStore(self.directory, limits=limits)
            with self.subTest(limits=limits), self.assertRaises(storage.ResourceLimit):
                self.save(new=[incoming(geometry=box(w=14))], store=limited)
            self.assertEqual(self.store.load(), before)
        too_many = (incoming(f"way/{i}", box(i * 20)) for i in range(1, 4))
        limited = storage.OverlayStore(self.directory, limits=replace(storage.Limits(), max_input_features=2))
        with self.assertRaises(storage.ResourceLimit):
            self.save(new=too_many, store=limited)
        self.assertEqual(self.store.load(), before)

    def test_lock_timeout_never_writes_without_lock(self):
        self.save()
        before = self.store.load()
        other = storage.OverlayStore(self.directory, lock_timeout=0.05)
        with self.store._locked():
            with self.assertRaises(storage.LockTimeout):
                self.save(store=other)
        self.assertEqual(self.store.load(), before)

    def test_concurrent_store_instances_do_not_lose_edits(self):
        stores = [storage.OverlayStore(self.directory) for _ in range(4)]
        def write(i):
            return self.save(old=[], new=[incoming(f"way/{i + 1}", box(i * 30))], store=stores[i])
        with ThreadPoolExecutor(4) as executor:
            commits = list(executor.map(write, range(4)))
        self.assertEqual(len({c.revision for c in commits}), 4)
        state = self.store.load()
        self.assertEqual(len(state.additions), 4)
        self.assertEqual(len(state.history), 4)

    def process(self, code, *args):
        header = f"import sys; sys.path[:0] = {[str(REPO / 'server'), str(REPO / 'server/tests')]!r}\n"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMP=str(self.directory), TEMP=str(self.directory))
        return subprocess.Popen([sys.executable, "-B", "-c", header + code, str(self.directory), *map(str, args)],
                                cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_concurrent_processes_do_not_lose_edits(self):
        code = '''
from test_buildingoverlaystore import INFO
from test_buildingoverlay import incoming, box
from buildingoverlaystore import OverlayStore
s = OverlayStore(sys.argv[1])
i = int(sys.argv[2])
s.refresh([], [incoming(f"way/{i}", box(i * 30))], info=INFO)
'''
        workers = [self.process(code, i) for i in (1, 2)]
        for worker in workers:
            out, err = worker.communicate(timeout=20)
            self.assertEqual(worker.returncode, 0, out + err)
        self.assertEqual(len(self.store.load().additions), 2)
        self.assertEqual(len(self.store.load().history), 2)

    def test_process_interruption_before_manifest_commit_preserves_old_state(self):
        a = self.save()
        code = '''
import os
from pathlib import Path
from test_buildingoverlaystore import INFO
from test_buildingoverlay import incoming, baseline, box
from buildingoverlaystore import OverlayStore
s = OverlayStore(sys.argv[1])
original = s._atomic_write
def interrupted(path, data):
    if path.name == "state.json":
        os._exit(23)
    original(path, data)
s._atomic_write = interrupted
s.refresh([baseline()], [incoming(geometry=box(w=14))], info=INFO)
'''
        worker = self.process(code)
        out, err = worker.communicate(timeout=20)
        self.assertEqual(worker.returncode, 23, out + err)
        reopened = storage.OverlayStore(self.directory)
        self.assertEqual(reopened.load().revision, a.revision)
        # The OS releases the lock on process exit; retry can reuse the orphan object.
        self.save(store=reopened, new=[incoming(geometry=box(w=14))])
        self.assertNotEqual(reopened.load().revision, a.revision)


if __name__ == "__main__":
    unittest.main()
