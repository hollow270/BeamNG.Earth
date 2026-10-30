"""Synthetic lon/lat fixtures; no network, game, native module or disk caches."""
from copy import deepcopy
from dataclasses import replace
import itertools
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import buildingoverlay as bo


def polygon(points):
    # Synthetic local metres near the equator -> GeoJSON lon/lat.
    return {"type": "Polygon", "coordinates": [[[x / 111320.0, y / 110540.0] for x, y in points]]}


def box(x=0, y=0, w=10, h=10):
    return polygon([(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)])


def baseline(fid="gers-a", osm="w1@1", geometry=None, **kwargs):
    sources = ({"dataset": "OpenStreetMap", "record_id": osm, "license": "ODbL-1.0"},) if osm else \
        ({"dataset": "Microsoft", "license": "ODbL-1.0"},)
    return bo.Feature(fid, geometry or box(), sources=sources, **kwargs)


def incoming(fid="way/1", geometry=None, **kwargs):
    return bo.Feature(fid, geometry or box(), **kwargs)


def outcomes(result, action):
    return [d for d in result.decisions if d.action == action]


class IdentityTest(unittest.TestCase):
    def test_normalization(self):
        for value in ("w123@4", "way/123", "osm:way:123", "Way:123", "w123"):
            self.assertEqual(bo.normalize_osm_id(value), "osm:way:123")
        for value in ("r123@4", "relation/123", "osm:relation:123"):
            self.assertEqual(bo.normalize_osm_id(value), "osm:relation:123")
        for value in (None, "123", "node/123", "way/-1", "way/0", "way/123oops"):
            self.assertIsNone(bo.normalize_osm_id(value))

    def test_identity_from_nonprimary_source(self):
        old = baseline(osm=None)
        old = replace(old, sources=old.sources + ({"dataset": "OpenStreetMap", "record_id": "w1@8"},))
        result = bo.merge([old], [incoming(geometry=box(w=12))])
        self.assertEqual(result.buildings[0].feature_id, "gers-a")
        self.assertEqual(len(outcomes(result, "replacement")), 1)

    def test_incoming_identity_from_provenance(self):
        new = bo.Feature("export-row", box(), sources=({"dataset": "OpenStreetMap", "record_id": "r2@5"},))
        self.assertEqual(bo.merge([], [new]).buildings[0].feature_id, "osm:relation:2")

    def test_attribute_source_identity_is_not_a_footprint_match(self):
        old = baseline(osm=None)
        old = replace(old, sources=old.sources + ({"dataset": "OpenStreetMap", "record_id": "w1",
                                                 "property": "/properties/height"},))
        result = bo.merge([old], [incoming(geometry=box(30))])
        self.assertEqual(len(outcomes(result, "addition")), 1)
        self.assertFalse(outcomes(result, "replacement"))

    def test_missing_or_multiple_incoming_identity_conflicts(self):
        for f in (incoming("unknown"), incoming(osm_ids=("r2",))):
            result = bo.merge([], [f])
            self.assertFalse(result.buildings)
            self.assertEqual(outcomes(result, "conflict")[0].reason, "ambiguous_or_missing_osm_identity")


class MergeTest(unittest.TestCase):
    def test_existing_unchanged(self):
        old = baseline(attrs={"height": 12, "class": "house"})
        result = bo.merge([old], [incoming(attrs={"height": 12})])
        self.assertEqual(len(result.buildings), 1)
        self.assertEqual(result.buildings[0].feature_id, old.feature_id)
        self.assertEqual(result.buildings[0].attrs["class"], "house")
        self.assertEqual(result.report()["counts"], dict(addition=0, replacement=0, unchanged=1, conflict=0))
        self.assertFalse(result.decisions[0].geometry_changed)

    def test_addition_stable_namespaced_id(self):
        old = baseline()
        new = incoming("w99@7", box(30))
        result = bo.merge([old], [new])
        self.assertEqual([f.feature_id for f in result.buildings], ["gers-a", "osm:way:99"])
        self.assertEqual(len(outcomes(result, "addition")), 1)
        self.assertEqual(bo.merge([], [replace(new, feature_id="way/99")]).buildings[0].feature_id, "osm:way:99")

    def test_geometry_changes_without_identity_or_version_change(self):
        old = baseline()
        # The same way/version can have moved nodes.
        result = bo.merge([old], [incoming("w1@1", box(w=13))])
        event = outcomes(result, "replacement")[0]
        self.assertTrue(event.geometry_changed)
        self.assertNotEqual(bo.geometry_digest(old.geometry), bo.geometry_digest(result.buildings[0].geometry))

    def test_preserves_attributes_and_property_provenance(self):
        old = baseline(attrs={"height": 14, "roof_shape": "hipped", "name": "Original", "num_floors": 3})
        result = bo.merge([old], [incoming(attrs={"height": None, "roof_shape": "", "num_floors": float("nan"),
                                                "name": "Updated"})])
        merged = result.buildings[0]
        self.assertEqual(merged.attrs["height"], 14)
        self.assertEqual(merged.attrs["roof_shape"], "hipped")
        self.assertEqual(merged.attrs["num_floors"], 3)
        self.assertEqual(merged.attrs["name"], "Updated")
        self.assertIn(old.sources[0], merged.sources)
        self.assertEqual(merged.attribute_sources["height"], old.sources)
        self.assertEqual(merged.attribute_sources["name"][0]["dataset"], "OpenStreetMap")
        self.assertEqual(len(outcomes(result, "replacement")), 1)

    def test_exact_and_near_spatial_duplicates_retain_baseline_geometry(self):
        old = baseline(osm=None, attrs={"height": 14})
        for geometry in (box(), box(x=0.05)):
            result = bo.merge([old], [incoming(geometry=geometry)])
            self.assertEqual(len(result.buildings), 1)
            self.assertEqual(result.buildings[0].geometry, old.geometry)
            self.assertEqual(result.buildings[0].attrs["height"], 14)
            self.assertEqual(outcomes(result, "unchanged")[0].reason, "spatial_duplicate")

    def test_spatial_duplicate_does_not_claim_another_osm_identity(self):
        result = bo.merge([baseline()], [incoming("way/99")])
        self.assertEqual(outcomes(result, "conflict")[0].reason, "conflicting_osm_identity")
        self.assertEqual(result.buildings[0], baseline())

    def test_ambiguous_overlap_retains_baseline(self):
        old = baseline(osm=None)
        result = bo.merge([old], [incoming(geometry=box(4))])
        self.assertEqual(result.buildings, (old,))
        self.assertEqual(outcomes(result, "conflict")[0].reason, "ambiguous_spatial_overlap")

    def test_touching_is_not_overlap(self):
        result = bo.merge([baseline()], [incoming("way/2", box(10))])
        self.assertEqual(len(result.buildings), 2)
        self.assertFalse(outcomes(result, "conflict"))

    def test_duplicate_with_extra_collinear_vertices_is_not_added(self):
        outline = polygon([(0, 0), (5, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
        old = baseline(osm=None)
        result = bo.merge([old], [incoming(geometry=outline)])
        self.assertEqual(result.buildings, (old,))
        self.assertEqual(outcomes(result, "conflict")[0].reason, "ambiguous_spatial_overlap")

    def test_split_rejects_both_candidates_even_with_an_identity_match(self):
        old = baseline()
        result = bo.merge([old], [incoming(geometry=box(w=5)), incoming("way/2", box(5, w=5))])
        self.assertEqual(result.buildings, (old,))
        self.assertEqual(len(outcomes(result, "conflict")), 2)
        self.assertTrue(all(d.reason == "ambiguous_overlap_split_or_merge" for d in outcomes(result, "conflict")))

    def test_merge_rejects_incoming(self):
        olds = [baseline(), baseline("gers-b", "w2", box(10))]
        result = bo.merge(olds, [incoming(geometry=box(w=20))])
        self.assertEqual(result.buildings, tuple(olds))
        self.assertEqual(outcomes(result, "conflict")[0].baseline_ids, ("gers-a", "gers-b"))

    def test_overlapping_additions_and_duplicate_identity_are_conflicts(self):
        for pair in ([incoming(), incoming("way/2", box(4))],
                     [incoming(), incoming("w1@2", box(30))]):
            result = bo.merge([], pair)
            self.assertFalse(result.buildings)
            self.assertEqual(len(outcomes(result, "conflict")), 2)

    def test_identity_move_cannot_land_on_other_baseline(self):
        olds = [baseline(), baseline("gers-b", "w2", box(30))]
        result = bo.merge(olds, [incoming(geometry=box(30))])
        self.assertEqual(result.buildings, tuple(olds))
        self.assertEqual(len(outcomes(result, "conflict")), 1)

    def test_absent_buildings_are_not_deleted(self):
        old = baseline()
        result = bo.merge([old], [])
        self.assertEqual(result.buildings, (old,))
        self.assertEqual(result.decisions[0].reason, "baseline_retained")

    def test_deletion_and_retagging_are_unverified(self):
        old = baseline()
        for status in ("deleted", "retagged"):
            result = bo.merge([old], [replace(incoming(), status=status, geometry=None)])
            self.assertEqual(result.buildings, (old,))
            self.assertEqual(outcomes(result, "conflict")[0].reason, "deletion_or_retagging_unverified")

    def test_inputs_and_output_do_not_alias(self):
        old, new = baseline(attrs={"name": "Old"}), incoming(attrs={"name": "New"})
        before = deepcopy((old, new))
        result = bo.merge([old], [new])
        self.assertEqual((old, new), before)
        result.buildings[0].geometry["coordinates"][0][0][0] = 123
        result.buildings[0].sources[0]["dataset"] = "edited"
        result.buildings[0].attrs["name"] = "edited"
        self.assertEqual((old, new), before)

    def test_invalid_incoming_attributes_do_not_erase_existing(self):
        old = baseline(attrs={"height": 14})
        result = bo.merge([old], [incoming(attrs={"height": -5})])
        self.assertEqual(result.buildings, (old,))
        self.assertEqual(outcomes(result, "conflict")[0].reason, "invalid_attributes")


class ValidationTest(unittest.TestCase):
    def test_malformed_geometry_conflicts_and_retains_original(self):
        shapes = [None, {}, {"type": "Polygon", "coordinates": None},
                  {"type": "Point", "coordinates": [0, 0]},
                  polygon([(0, 0), (10, 0), (0, 10)]),                # open
                  polygon([(0, 0), (10, 10), (0, 10), (10, 0), (0, 0)]),  # crossing
                  polygon([(0, 0), (10, 0), (5, 0), (5, 10), (0, 0)]),    # backtracking
                  polygon([(0, 0), (0, 0), (10, 10), (0, 0)]),
                  {"type": "Polygon", "coordinates": [[[0, 0], [0, 0.1], [float("nan"), 0], [0, 0]]]},
                  {"type": "Polygon", "coordinates": [[[0, 0], [0, 0.1], [200, 0], [0, 0]]]},
                  box(w=0.01, h=0.01), box(w=2000, h=2000), box(w=6000)]
        old = baseline()
        for geometry in shapes:
            with self.subTest(geometry=geometry):
                result = bo.merge([old], [replace(incoming(), geometry=geometry)])
                self.assertEqual(result.buildings, (old,))
                self.assertEqual(len(outcomes(result, "conflict")), 1)

    def test_holes_and_multipolygons_never_flattened(self):
        holes = box()
        holes["coordinates"].append(box(2, 2, 2, 2)["coordinates"][0])
        multi = {"type": "MultiPolygon", "coordinates": [box()["coordinates"], box(30)["coordinates"]]}
        old = baseline()
        for geometry in (holes, multi):
            result = bo.merge([old], [incoming(geometry=geometry)])
            self.assertEqual(result.buildings, (old,))
            self.assertEqual(len(outcomes(result, "conflict")), 1)

    def test_unsupported_baseline_is_not_downgraded(self):
        multi = {"type": "MultiPolygon", "coordinates": [box()["coordinates"], box(30)["coordinates"]]}
        old = baseline(geometry=multi)
        result = bo.merge([old], [incoming()])
        self.assertEqual(result.buildings, (old,))
        self.assertEqual(outcomes(result, "conflict")[0].reason, "unsupported_baseline_geometry")

    def test_supported_concave_polygon_and_enclosed_addition(self):
        # The empty corner of an L has overlapping bboxes but no footprint overlap.
        shape = polygon([(0, 0), (10, 0), (10, 3), (3, 3), (3, 10), (0, 10), (0, 0)])
        result = bo.merge([baseline(geometry=shape)], [incoming("way/2", box(5, 5, 2, 2))])
        self.assertEqual(len(result.buildings), 2)
        self.assertFalse(outcomes(result, "conflict"))

    def test_ring_order_does_not_change_geometry_digest(self):
        geometry = box()
        points = geometry["coordinates"][0][:-1]
        for reverse in (False, True):
            for k in range(len(points)):
                ring = points[k:] + points[:k]
                if reverse:
                    ring = list(reversed(ring))
                rotated = {"type": "Polygon", "coordinates": [ring + [ring[0]]]}
                self.assertEqual(bo.geometry_digest(rotated), bo.geometry_digest(geometry))
                self.assertEqual(len(outcomes(bo.merge([baseline()], [incoming(geometry=rotated)]), "unchanged")), 1)


class PartsTest(unittest.TestCase):
    def setUp(self):
        self.old = baseline(attrs={"has_parts": True, "height": 15})
        self.part = baseline("gers-part", "w10@1", box(1, 1, 3, 3), parent_id="gers-a", attrs={"height": 10})

    def test_unchanged_parent_retains_parts_without_complete_snapshot(self):
        result = bo.merge([self.old], [incoming()], baseline_parts=[self.part])
        self.assertEqual(result.parts, (self.part,))
        self.assertEqual(len(outcomes(result, "unchanged")), 1)

    def test_parent_change_requires_complete_parts(self):
        result = bo.merge([self.old], [incoming(geometry=box(w=12))], baseline_parts=[self.part])
        self.assertEqual(result.buildings, (self.old,))
        self.assertEqual(result.parts, (self.part,))
        self.assertEqual(outcomes(result, "conflict")[0].reason, "complete_parts_required_for_parent_update")

    def test_part_update_is_atomic_and_preserves_parent_and_part_ids(self):
        newpart = incoming("w10@1", box(1, 1, 4, 3), parent_id="way/1", attrs={"height": 12})
        result = bo.merge([self.old], [incoming(parts_complete=True)], baseline_parts=[self.part], incoming_parts=[newpart])
        self.assertEqual(result.buildings[0].feature_id, "gers-a")
        self.assertTrue(result.buildings[0].attrs["has_parts"])
        self.assertEqual(result.parts[0].feature_id, "gers-part")
        self.assertEqual(result.parts[0].parent_id, "gers-a")
        self.assertEqual(result.parts[0].attrs["building_id"], "gers-a")
        self.assertTrue(outcomes(result, "replacement")[0].geometry_changed)

    def test_new_building_with_parts(self):
        p = incoming("w10", box(1, 1, 3, 3), parent_id="way/1")
        result = bo.merge([], [incoming(parts_complete=True)], incoming_parts=[p])
        self.assertEqual(result.parts[0].parent_id, "osm:way:1")
        self.assertEqual(result.parts[0].feature_id, "osm:way:10")
        self.assertTrue(result.buildings[0].attrs["has_parts"])

    def test_part_attribute_update_does_not_claim_geometry_changed(self):
        p = incoming("way/10", box(1, 1, 3, 3), parent_id="way/1", attrs={"height": 12})
        result = bo.merge([self.old], [incoming(parts_complete=True)], baseline_parts=[self.part], incoming_parts=[p])
        self.assertEqual(len(outcomes(result, "replacement")), 1)
        self.assertFalse(outcomes(result, "replacement")[0].geometry_changed)

    def test_new_declared_parts_cannot_be_silently_ignored(self):
        result = bo.merge([], [incoming(attrs={"has_parts": True})])
        self.assertFalse(result.buildings)
        self.assertEqual(outcomes(result, "conflict")[0].reason, "declared_parts_not_supplied")

    def test_invalid_or_outside_part_rejects_whole_transaction(self):
        multi = {"type": "MultiPolygon", "coordinates": [box()["coordinates"]]}
        for geometry in (box(50), multi):
            part = incoming("way/10", geometry, parent_id="way/1")
            result = bo.merge([self.old], [incoming(parts_complete=True)], baseline_parts=[self.part], incoming_parts=[part])
            self.assertEqual(result.buildings, (self.old,))
            self.assertEqual(result.parts, (self.part,))
            self.assertEqual(len(outcomes(result, "conflict")), 1)

    def test_parts_cannot_silently_disappear_or_change_identity(self):
        for supplied in ([], [incoming("way/99", box(1, 1, 3, 3), parent_id="way/1")]):
            result = bo.merge([self.old], [incoming(parts_complete=True)], baseline_parts=[self.part], incoming_parts=supplied)
            self.assertEqual(result.parts, (self.part,))
            self.assertEqual(result.buildings, (self.old,))
            self.assertEqual(len(outcomes(result, "conflict")), 1)

    def test_orphan_part_reported(self):
        result = bo.merge([], [], incoming_parts=[incoming("way/10", parent_id="way/999")])
        self.assertFalse(result.parts)
        self.assertEqual(outcomes(result, "conflict")[0].subject, "part")

    def test_parts_require_completeness_assertion(self):
        part = incoming("way/10", box(1, 1, 3, 3), parent_id="way/1")
        result = bo.merge([], [incoming()], incoming_parts=[part])
        self.assertFalse(result.buildings)
        self.assertFalse(result.parts)
        self.assertEqual(outcomes(result, "conflict")[0].reason, "incomplete_parts")

    def test_part_claimed_by_multiple_incoming_parents_blocks_both(self):
        parents = [incoming(parts_complete=True), incoming("way/2", box(30), parts_complete=True)]
        children = [incoming("way/10", box(1, 1, 3, 3), parent_id="way/1"),
                    incoming("w10@2", box(31, 1, 3, 3), parent_id="way/2")]
        result = bo.merge([], parents, incoming_parts=children)
        self.assertFalse(result.buildings)
        self.assertFalse(result.parts)
        self.assertEqual(len(outcomes(result, "conflict")), 2)
        self.assertEqual(result, bo.merge([], list(reversed(parents)), incoming_parts=list(reversed(children))))

    def test_part_cannot_be_taken_from_another_baseline_parent(self):
        newparent = incoming("way/2", box(30), parts_complete=True)
        child = incoming("way/10", box(31, 1, 3, 3), parent_id="way/2")
        result = bo.merge([self.old], [newparent], baseline_parts=[self.part], incoming_parts=[child])
        self.assertEqual(result.buildings, (self.old,))
        self.assertEqual(result.parts, (self.part,))
        self.assertEqual(outcomes(result, "conflict")[0].reason, "part_belongs_to_other_parent")

    def test_part_cannot_have_its_parents_identity(self):
        child = incoming("w1@2", box(1, 1, 3, 3), parent_id="way/1")
        result = bo.merge([], [incoming(parts_complete=True)], incoming_parts=[child])
        self.assertFalse(result.buildings)
        self.assertFalse(result.parts)
        self.assertEqual(outcomes(result, "conflict")[0].reason, "part_id_collision")

    def test_duplicate_part_footprints_are_conflicts(self):
        children = [incoming("way/10", box(1, 1, 3, 3), parent_id="way/1"),
                    incoming("way/11", box(1, 1, 3, 3), parent_id="way/1")]
        result = bo.merge([], [incoming(parts_complete=True)], incoming_parts=children)
        self.assertFalse(result.buildings)
        self.assertFalse(result.parts)
        self.assertEqual(outcomes(result, "conflict")[0].reason, "ambiguous_duplicate_parts")

    def test_has_parts_without_baseline_parts_is_not_guessed(self):
        result = bo.merge([self.old], [incoming()])
        self.assertEqual(result.buildings, (self.old,))
        self.assertEqual(outcomes(result, "conflict")[0].reason, "baseline_parts_not_supplied")


class DeterminismTest(unittest.TestCase):
    def test_input_permutations_produce_identical_output_and_report(self):
        old = [baseline(), baseline("gers-b", "w2", box(30))]
        new = [incoming(geometry=box(w=12)), incoming("way/2", box(30)), incoming("way/3", box(60))]
        reference = bo.merge(old, new)
        for aa, bb in itertools.product(itertools.permutations(old), itertools.permutations(new)):
            result = bo.merge(aa, bb)
            self.assertEqual(result, reference)
            self.assertEqual(result.digest(), reference.digest())
            self.assertEqual(result.report_json(), reference.report_json())
        report = json.loads(reference.report_json())
        self.assertEqual(report["counts"], dict(addition=1, replacement=1, unchanged=1, conflict=0))

    def test_conflicts_are_order_independent(self):
        fs = [incoming(), incoming("way/2", box(4)), incoming("way/3", box(40))]
        reference = bo.merge([baseline()], fs)
        for order in itertools.permutations(fs):
            self.assertEqual(bo.merge([baseline()], order), reference)

    def test_digest_includes_geometry_attributes_and_provenance(self):
        a = bo.merge([], [incoming()])
        b = bo.merge([], [incoming(geometry=box(w=12))])
        c = bo.merge([], [incoming(attrs={"height": 10})])
        d = bo.merge([], [incoming(sources=({"dataset": "OpenStreetMap", "record_id": "w1@2"},))])
        self.assertEqual(len({r.digest() for r in (a, b, c, d)}), 4)

    def test_structural_input_errors_raise(self):
        with self.assertRaises(ValueError):
            bo.merge([baseline(), baseline()], [])
        with self.assertRaises(ValueError):
            bo.merge([], [incoming(), incoming()])
        with self.assertRaises(ValueError):
            bo.merge([], [], baseline_parts=[baseline(parent_id="missing")])


class MapgeomAdapterTest(unittest.TestCase):
    def test_adapter_preserves_sources_parts_and_input_arrays(self):
        import numpy as np
        import mapgeom as mg
        geom = (mg.POLYGON, [[np.array(box()["coordinates"][0])]])
        f = mg.build("buildings", mg.text_column(["gers-a"]), [geom])
        f.attrs["height"] = np.array([12.0])
        f.attrs["sources"] = mg.text_column([json.dumps([
            {"dataset": "Microsoft"}, {"dataset": "OpenStreetMap", "record_id": "w1@4"}])])
        original = {k: v.copy() for k, v in f.to_arrays("").items()}
        converted = bo.from_mapgeom(f)
        self.assertEqual(converted[0].attrs["height"], 12)
        result = bo.merge(converted, [incoming(geometry=box(w=12))])
        self.assertEqual(len(outcomes(result, "replacement")), 1)
        for key, value in original.items():
            np.testing.assert_array_equal(f.to_arrays("")[key], value)
        part = mg.build("building_parts", mg.text_column(["part"]), [geom])
        part.attrs["building_id"] = mg.text_column(["gers-a"])
        self.assertEqual(bo.from_mapgeom(part)[0].parent_id, "gers-a")

    def test_adapter_preserves_multipolygons_and_holes(self):
        import numpy as np
        import mapgeom as mg
        outer, hole = np.array(box()["coordinates"][0]), np.array(box(2, 2, 2, 2)["coordinates"][0])
        f = mg.build("buildings", mg.text_column(["a", "b"]),
                     [(mg.POLYGON, [[outer, hole]]), (mg.POLYGON, [[outer], [outer + 0.01]])])
        converted = bo.from_mapgeom(f)
        self.assertEqual(len(converted[0].geometry["coordinates"]), 2)
        self.assertEqual(converted[1].geometry["type"], "MultiPolygon")
        self.assertEqual(len(converted[1].geometry["coordinates"]), 2)


if __name__ == "__main__":
    unittest.main()
