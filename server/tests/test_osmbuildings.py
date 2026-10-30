"""Mocked OSM retrieval only. The network is forbidden, including transport tests."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import email.utils
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import buildingoverlay as bo
import buildingoverlaystore as storemod
import netfetch
import osmbuildings as osm
from test_buildingoverlay import baseline, box, outcomes


BOUNDS = (-0.001, -0.001, 0.002, 0.002)
OTHER = (0.01, 0.01, 0.011, 0.011)
STAMP = "2026-10-30T12:00:00Z"
ELEMENT_STAMP = "2026-10-30T11:59:00Z"
WALL = datetime(2026, 10, 30, 12, 1, tzinfo=timezone.utc).timestamp()
CONFIG = osm.Config(enabled=True, endpoint="https://osm-source.invalid/api/interpreter")


def node(i, x, y):
    return dict(type="node", id=i, lon=x / 111320, lat=y / 110540, version=1, timestamp=ELEMENT_STAMP)


def way(i=1, x=0, y=0, w=10, h=10, tags=None):
    nodes = [node(i * 100 + k + 1, a, b) for k, (a, b) in enumerate(((x, y), (x + w, y), (x + w, y + h), (x, y + h)))]
    return nodes + [dict(type="way", id=i, nodes=[n["id"] for n in nodes] + [nodes[0]["id"]],
                         tags={"building": "yes"} if tags is None else tags, version=1, timestamp=ELEMENT_STAMP)]


def relation(i, members, tags):
    return dict(type="relation", id=i, members=[dict(type=t, ref=r, role=role) for t, r, role in members],
                tags=tags, timestamp=ELEMENT_STAMP, version=2)


def response(rows=(), *, replication=STAMP, remark=None, sentinel=True):
    counts = Counter(r["type"] for r in rows)
    doc = {"version": 0.6, "generator": "Overpass API synthetic fixture", "elements": deepcopy(list(rows))}
    if replication is not None:
        doc["osm3s"] = {"timestamp_osm_base": replication}
    if remark is not None:
        doc["remark"] = remark
    if sentinel:
        doc["elements"].append({"type": "count", "id": 0, "tags": {"nodes": str(counts["node"]),
            "ways": str(counts["way"]), "relations": str(counts["relation"]), "total": str(len(rows))}})
    return json.dumps(doc).encode()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, value):
        self.now += value


class FakeTransport:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload = response(way()) if payload is None else payload
        self.status, self.headers, self.calls = status, dict(headers or {}), []

    def post(self, endpoint, body, headers, **limits):
        self.calls.append((endpoint, body, dict(headers), limits))
        return self.status, self.headers, self.payload


class NoNetworkTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(socket, "create_connection", side_effect=AssertionError("live networking forbidden")).start()


class ParseTest(NoNetworkTest):
    def parse(self, rows=None, **kw):
        return osm.parse(response(way() if rows is None else rows), BOUNDS, **kw)

    def test_building_identity_geometry_timestamp_tags_and_attribution(self):
        snapshot = self.parse()
        self.assertFalse(snapshot.empty)
        self.assertFalse(snapshot.stale)
        f = snapshot.buildings[0]
        self.assertEqual(f.feature_id, "osm:way:1")
        self.assertEqual(f.osm_ids, ("osm:way:1",))
        self.assertEqual(bo.geometry_digest(f.geometry), bo.geometry_digest(box()))
        self.assertEqual(f.sources[0]["record_id"], "w1@1")
        self.assertEqual(f.sources[0]["tags"], {"building": "yes"})
        self.assertEqual(f.sources[0]["update_time"], ELEMENT_STAMP)
        self.assertEqual(f.sources[0]["snapshot_timestamp"], STAMP)
        self.assertEqual(f.sources[0]["attribution"], "© OpenStreetMap contributors")
        self.assertEqual(snapshot.replication_timestamp, STAMP)

    def test_new_and_modified_geometry_under_the_same_way_version(self):
        modified = self.parse(way(w=12)).buildings[0]
        merged = bo.merge([baseline()], [modified])
        self.assertEqual(len(outcomes(merged, "replacement")), 1)
        self.assertTrue(outcomes(merged, "replacement")[0].geometry_changed)
        addition = self.parse(way(2, x=30)).buildings[0]
        self.assertEqual(bo.merge([baseline()], [addition]).buildings[-1].feature_id, "osm:way:2")

    def test_moved_nodes_are_reflected_in_geometry_timestamp(self):
        rows = way()
        rows[0]["timestamp"] = "2026-10-30T11:59:30Z"
        rows[0]["lon"] -= 0.00001
        f = self.parse(rows).buildings[0]
        self.assertEqual(f.sources[0]["geometry_timestamp"], "2026-10-30T11:59:30Z")
        self.assertEqual(f.sources[0]["record_id"], "w1@1")

    def test_attributes_and_unit_normalization(self):
        tags = {"building": "house", "name": "Synthetic", "height": "40 ft", "building:levels": "3",
                "min_height": '6\' 6"', "building:min_level": "1", "roof:height": "2 m", "roof:shape": "hipped",
                "roof:direction": "SW", "roof:orientation": "along", "roof:material": "tile",
                "roof:colour": "#ABC", "building:material": "brick", "building:colour": "#aabbcc"}
        snapshot = self.parse(way(tags=tags))
        attrs = snapshot.buildings[0].attrs
        self.assertAlmostEqual(attrs["height"], 12.192)
        self.assertAlmostEqual(attrs["min_height"], 1.9812)
        self.assertEqual(attrs["num_floors"], 3)
        self.assertEqual(attrs["min_floor"], 1)
        self.assertEqual(attrs["roof_height"], 2)
        self.assertEqual(attrs["roof_direction"], 225)
        self.assertEqual(attrs["roof_color"], "#abc")
        self.assertEqual(attrs["facade_color"], "#aabbcc")
        self.assertEqual(attrs["class"], "house")
        self.assertEqual(snapshot.buildings[0].sources[0]["tags"], tags)

    def test_invalid_attributes_are_reported_and_do_not_erase_existing(self):
        snapshot = self.parse(way(tags={"building": "yes", "height": "12;14", "building:levels": "-3",
                                              "roof:direction": "999", "roof:colour": "not-a-colour"}))
        self.assertNotIn("height", snapshot.buildings[0].attrs)
        self.assertEqual(len(snapshot.issues), 4)
        merged = bo.merge([baseline(attrs={"height": 14})], snapshot.buildings)
        self.assertEqual(merged.buildings[0].attrs["height"], 14)

    def test_complete_empty_response(self):
        snapshot = self.parse([])
        self.assertTrue(snapshot.empty)
        self.assertEqual(snapshot.element_count, 0)
        self.assertEqual(snapshot.refresh_info("baseline").source_timestamp, STAMP)

    def test_missing_count_truncated_json_or_wrong_count_is_not_empty(self):
        docs = [response([], sentinel=False), b'{"version":0.6,"elements":[', b"{}"]
        wrong = json.loads(response())
        wrong["elements"][-1]["tags"]["total"] = "1"
        docs.append(json.dumps(wrong).encode())
        for payload in docs:
            with self.subTest(payload=payload), self.assertRaises(osm.IncompleteResponse):
                osm.parse(payload, BOUNDS)

    def test_missing_node_or_way_dependency_rejects_whole_response(self):
        rows = way()
        with self.assertRaises(osm.IncompleteResponse):
            self.parse(rows[1:])
        rel = relation(9, [("way", 999, "outer")], {"type": "multipolygon", "building": "yes"})
        with self.assertRaises(osm.IncompleteResponse):
            self.parse([rel])

    def test_malformed_element_metadata_and_duplicate_ids(self):
        for field, value in (("timestamp", "bad"), ("version", 0), ("nodes", [999]), ("tags", {"height": 12})):
            rows = way()
            rows[-1][field] = value
            with self.subTest(field=field), self.assertRaises(osm.IncompleteResponse):
                self.parse(rows)
        with self.assertRaises(osm.IncompleteResponse):
            self.parse(way() + way())

    def test_http_200_overpass_remark_is_not_partial_success(self):
        with self.assertRaises(osm.OverpassError):
            osm.parse(response(way(), remark="runtime error: Query timed out"), BOUNDS)

    def test_stale_replication_and_missing_timestamp_cannot_be_persisted(self):
        snapshot = self.parse(not_before="2026-10-30T12:01:00Z")
        self.assertTrue(snapshot.stale)
        with self.assertRaises(osm.StaleSnapshot):
            snapshot.refresh_info("baseline")
        snapshot = osm.parse(response([], replication=None), BOUNDS)
        self.assertTrue(snapshot.empty)
        self.assertIsNone(snapshot.replication_timestamp)
        with self.assertRaises(osm.IncompleteResponse):
            snapshot.refresh_info("baseline")

    def test_snapshot_age_future_timestamp_and_inconsistent_element_time(self):
        snapshot = self.parse(now="2026-10-30T14:00:00Z")
        self.assertTrue(snapshot.stale)
        for replication in ("bad", "2026-10-30T11:00:00Z"):
            with self.subTest(replication=replication), self.assertRaises(osm.IncompleteResponse):
                osm.parse(response(way(), replication=replication), BOUNDS)
        with self.assertRaises(osm.IncompleteResponse):
            self.parse(now="2026-10-30T11:00:00Z")

    def test_known_missing_or_retagged_objects_never_create_deletions(self):
        missing = self.parse([], known_ids=["w1@1"])
        self.assertFalse(missing.empty)
        self.assertEqual(missing.issues[0].code, "known_identity_not_returned_no_deletion_evidence")
        snapshot = self.parse(way(tags={"building": "no"}), known_ids=["w1"])
        self.assertEqual(snapshot.buildings[0].status, "retagged")
        result = bo.merge([baseline()], snapshot.buildings)
        self.assertEqual(result.buildings, (baseline(),))
        self.assertEqual(len(outcomes(result, "conflict")), 1)

    def test_limits_elements_vertices_features_and_bytes(self):
        for config in (replace(CONFIG, max_elements=1), replace(CONFIG, max_vertices=4),
                       replace(CONFIG, max_response_bytes=20)):
            with self.subTest(config=config), self.assertRaises(osm.ResourceLimit):
                self.parse(config=config)
        with self.assertRaises(osm.ResourceLimit):
            self.parse(way() + way(2, x=30), config=replace(CONFIG, max_features=1))

    def test_malformed_json_constants_and_duplicate_keys(self):
        for payload in (b'{"version":0.6,"version":0.6}', b'{"value":NaN}', b"<html>error</html>", b"\xff"):
            with self.subTest(payload=payload), self.assertRaises(osm.IncompleteResponse):
                osm.parse(payload, BOUNDS)

    def test_reused_relation_geometry_is_included_in_vertex_budget(self):
        rows = way(tags={})
        rows += [relation(i, [("way", 1, "outer")], {"type": "multipolygon", "building": "yes"}) for i in range(10, 14)]
        # Input references = 5 way nodes + 4 relation members = 9; expanded vertices = 20.
        with self.assertRaises(osm.ResourceLimit):
            self.parse(rows, config=replace(CONFIG, max_vertices=10))

    def test_invalid_ring_is_retained_for_stage1_to_reject(self):
        rows = way()
        rows[-1]["nodes"] = rows[-1]["nodes"][:-1]
        snapshot = self.parse(rows)
        self.assertEqual(snapshot.issues[0].code, "unclosed_ring")
        self.assertEqual(len(outcomes(bo.merge([baseline()], snapshot.buildings), "conflict")), 1)

    def test_element_order_is_deterministic(self):
        rows = way() + way(2, x=30)
        self.assertEqual(self.parse(rows), self.parse(list(reversed(rows))))

    def test_round_trip_into_existing_persistent_store(self):
        snapshot = self.parse(way(w=12))
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2], prefix=".osm-test-") as d:
            store = storemod.OverlayStore(d)
            commit = store.refresh([baseline()], snapshot.buildings, incoming_parts=snapshot.parts,
                                   info=snapshot.refresh_info("fixture-baseline"))
            self.assertEqual(len(outcomes(commit.merge_result, "replacement")), 1)
            store.enable()
            reopened = storemod.OverlayStore(d)
            self.assertEqual(bo.geometry_digest(reopened.apply([baseline()], baseline_release="fixture-baseline",
                                                              bounds=BOUNDS).buildings[0].geometry),
                             bo.geometry_digest(box(w=12)))


class RelationsTest(NoNetworkTest):
    parse = ParseTest.parse
    def test_single_outer_relation_and_member_way_are_not_duplicated(self):
        rows = way() + [relation(9, [("way", 1, "outer")], {"type": "multipolygon", "building": "yes"})]
        snapshot = self.parse(rows)
        self.assertEqual([f.feature_id for f in snapshot.buildings], ["osm:relation:9"])
        self.assertEqual(snapshot.buildings[0].geometry["type"], "Polygon")

    def test_multiple_outer_rings_and_holes_preserved_as_unsupported(self):
        for role in ("outer", "inner"):
            rows = way(tags={}) + way(2, x=2 if role == "inner" else 30, y=2, w=2, h=2, tags={})
            rows.append(relation(9, [("way", 1, "outer"), ("way", 2, role)], {"type": "multipolygon", "building": "yes"}))
            snapshot = self.parse(rows)
            geom = snapshot.buildings[0].geometry
            self.assertEqual(geom["type"], "MultiPolygon" if role == "outer" else "Polygon")
            if role == "inner":
                self.assertEqual(len(geom["coordinates"]), 2)
            self.assertEqual(len(outcomes(bo.merge([], snapshot.buildings), "conflict")), 1)

    def test_open_member_ways_stitch_by_node_identity(self):
        rows = way(tags={})
        first = rows[-1]
        nodes = first["nodes"]
        first["nodes"] = nodes[:3]
        second = dict(first, id=2, nodes=nodes[2:])
        rows += [second, relation(9, [("way", 2, "outer"), ("way", 1, "outer")],
                                 {"type": "multipolygon", "building": "yes"})]
        snapshot = self.parse(rows)
        self.assertEqual(bo.geometry_digest(snapshot.buildings[0].geometry), bo.geometry_digest(box()))

    def test_complete_but_unstitchable_relation_is_reported(self):
        rows = way(tags={})
        rows[-1]["nodes"] = rows[-1]["nodes"][:-1]
        rows.append(relation(9, [("way", 1, "outer")], {"type": "multipolygon", "building": "yes"}))
        snapshot = self.parse(rows)
        self.assertEqual(snapshot.buildings[0].geometry["type"], "Unsupported")
        self.assertTrue(snapshot.issues)

    def test_explicit_building_parts_normalize_to_atomic_parent_set(self):
        rows = way() + way(2, 1, 1, 3, 3, {"building:part": "yes", "height": "15", "min_height": "3"})
        rows.append(relation(10, [("way", 1, "outline"), ("way", 2, "part")], {"type": "building"}))
        snapshot = self.parse(rows)
        self.assertTrue(snapshot.buildings[0].parts_complete)
        self.assertTrue(snapshot.buildings[0].attrs["has_parts"])
        self.assertEqual(snapshot.parts[0].parent_id, "osm:way:1")
        self.assertEqual(snapshot.parts[0].attrs["height"], 15)
        self.assertEqual(snapshot.buildings[0].sources[0]["building_associations"][0]["record_id"], "r10@2")
        result = bo.merge([], snapshot.buildings, incoming_parts=snapshot.parts)
        self.assertFalse(outcomes(result, "conflict"))
        self.assertEqual(len(result.parts), 1)

    def test_spatial_part_association_does_not_claim_completeness(self):
        snapshot = self.parse(way() + way(2, 1, 1, 3, 3, {"building:part": "yes"}))
        self.assertEqual(snapshot.parts[0].parent_id, "osm:way:1")
        self.assertFalse(snapshot.buildings[0].parts_complete)
        result = bo.merge([], snapshot.buildings, incoming_parts=snapshot.parts)
        self.assertEqual(outcomes(result, "conflict")[0].reason, "incomplete_parts")

    def test_extra_unlisted_part_invalidates_explicit_completeness(self):
        rows = way() + way(2, 1, 1, 3, 3, {"building:part": "yes"}) + way(3, 5, 5, 3, 3, {"building:part": "yes"})
        rows.append(relation(10, [("way", 1, "outline"), ("way", 2, "part")], {"type": "building"}))
        self.assertFalse(self.parse(rows).buildings[0].parts_complete)

    def test_orphan_parts_are_reported_and_not_fabricated_into_buildings(self):
        snapshot = self.parse(way(tags={"building:part": "yes"}))
        self.assertFalse(snapshot.buildings)
        self.assertIsNone(snapshot.parts[0].parent_id)
        self.assertEqual(snapshot.issues[0].code, "unknown_or_ambiguous_part_parent")

    def test_unsupported_orphan_part_cannot_silently_downgrade_a_parent(self):
        rows = way() + way(2, 1, 1, 2, 2, {}) + way(3, 5, 5, 2, 2, {})
        rows.append(relation(9, [("way", 2, "outer"), ("way", 3, "outer")],
                             {"type": "multipolygon", "building:part": "yes"}))
        snapshot = self.parse(rows)
        self.assertEqual(snapshot.parts[0].geometry["type"], "MultiPolygon")
        self.assertTrue(snapshot.buildings[0].attrs["has_parts"])
        self.assertFalse(snapshot.buildings[0].parts_complete)
        result = bo.merge([], snapshot.buildings, incoming_parts=snapshot.parts)
        self.assertFalse(result.buildings)
        self.assertTrue(outcomes(result, "conflict"))


class ClientTest(NoNetworkTest):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.coordinator = osm.Coordinator(self.clock)
        self.net = netfetch.Fetcher(transport=object(), clock=self.clock, sleep=self.clock.advance, wall=lambda: WALL)
        self.transport = FakeTransport()

    def client(self, config=CONFIG, **kw):
        return osm.Client(config, fetcher=self.net, transport=kw.pop("transport", self.transport),
                          coordinator=self.coordinator, wall=lambda: WALL, **kw)

    def test_opt_in_manual_and_endpoint_required(self):
        for config, manual in ((osm.Config(), True), (replace(CONFIG, enabled=False), True), (CONFIG, False)):
            with self.subTest(config=config, manual=manual), self.assertRaises(osm.ConfigurationError):
                self.client(config).fetch(BOUNDS, manual=manual)
        self.assertFalse(self.transport.calls)

    def test_success_uses_post_app_identity_and_bounded_transport(self):
        result = self.client().fetch(BOUNDS, manual=True)
        self.assertEqual(len(result.buildings), 1)
        endpoint, body, headers, limits = self.transport.calls[0]
        self.assertEqual(endpoint, CONFIG.endpoint)
        self.assertEqual(headers["User-Agent"], netfetch.USER_AGENT)
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertEqual(limits["max_bytes"], CONFIG.max_response_bytes)
        self.assertEqual(limits["timeout"], min(CONFIG.timeout_s, self.net.policy("osm-source.invalid").timeout))
        q = parse_qs(body.decode())["data"][0]
        self.assertIn("out meta;out count;", q)
        self.assertIn(".selected >>", q)
        self.assertIn('rel(bw.seed)["type"="building"]', q)
        self.assertIn('way["building:part"]', q)
        self.assertNotIn("out geom(", q)

    def test_query_bbox_order_and_known_identity_selection(self):
        q = osm.query(BOUNDS, known_ids=["w1@1", "relation/9"])
        self.assertIn("(-0.001000000,-0.001000000,0.002000000,0.002000000)", q)
        self.assertIn("way(id:1)", q)
        self.assertIn("rel(id:9)", q)
        self.assertEqual(q, osm.query(BOUNDS, known_ids=["r9", "way/1"]))

    def test_large_invalid_area_and_known_ids_rejected_before_network(self):
        for bounds in ((-1, -1, 1, 1), (1, 0, -1, 1), (0, 85, 0.001, 86), (True, 0, 1, 1)):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                self.client().fetch(bounds, manual=True)
        with self.assertRaises(osm.ResourceLimit):
            self.client(replace(CONFIG, max_known_ids=1)).fetch(BOUNDS, manual=True, known_ids=["w1", "w2"])
        self.assertFalse(self.transport.calls)

    def test_offline_and_existing_denied_hosts_remain_protected(self):
        policies = dict(netfetch.POLICIES)
        self.net.offline_flag = True
        with self.assertRaises(netfetch.Offline):
            self.client().fetch(BOUNDS, manual=True)
        self.net.offline_flag = False
        with self.assertRaises(netfetch.PolicyDenied):
            self.client(replace(CONFIG, endpoint="https://overpass-api.de/api/interpreter")).fetch(BOUNDS, manual=True)
        self.assertFalse(self.transport.calls)
        self.assertEqual(netfetch.POLICIES, policies)

    def test_endpoint_policies_budgets_and_breakers_apply(self):
        host = "osm-source.invalid"
        self.net.policies[host] = replace(netfetch.DEFAULT_POLICY, daily_hard=0)
        with self.assertRaises(netfetch.BudgetExceeded):
            self.client().fetch(BOUNDS, manual=True)
        self.assertFalse(self.transport.calls)

    def test_spacing_and_same_or_overlapping_area_cooldown(self):
        client = self.client()
        client.fetch(BOUNDS, manual=True)
        with self.assertRaises(osm.Throttled) as err:
            client.fetch(OTHER, manual=True)
        self.assertEqual(err.exception.retry_after, 60)
        self.clock.advance(60)
        with self.assertRaises(osm.Throttled) as err:
            client.fetch((-0.0005, -0.0005, 0.001, 0.001), manual=True)
        self.assertEqual(err.exception.retry_after, 540)
        self.clock.advance(540)
        client.fetch(BOUNDS, manual=True)
        self.assertEqual(len(self.transport.calls), 2)

    def test_clients_share_one_request_coordinator(self):
        entered, release = threading.Event(), threading.Event()
        original = self.transport.post
        def slow(*args, **kw):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test worker did not release request")
            return original(*args, **kw)
        self.transport.post = slow
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(self.client().fetch, BOUNDS, manual=True)
            self.assertTrue(entered.wait(2))
            try:
                with self.assertRaises(osm.Throttled):
                    self.client().fetch(OTHER, manual=True)
            finally:
                release.set()
            self.assertEqual(len(future.result().buildings), 1)

    def test_http_failures_no_retry_or_rotation(self):
        for status in (302, 400, 401, 429, 503):
            self.coordinator = osm.Coordinator(self.clock)
            self.net = netfetch.Fetcher(transport=object(), clock=self.clock, sleep=self.clock.advance, wall=lambda: WALL)
            transport = FakeTransport(status=status)
            with self.subTest(status=status), self.assertRaises(osm.HTTPFailure) as err:
                self.client(transport=transport).fetch(BOUNDS, manual=True)
            self.assertEqual(err.exception.code, status)
            self.assertEqual(len(transport.calls), 1)

    def test_full_retry_after_exceeding_netfetch_clamp_is_honoured(self):
        for status in (429, 503):
            self.coordinator = osm.Coordinator(self.clock)
            transport = FakeTransport(status=status, headers={"Retry-After": "3600"})
            with self.subTest(status=status), self.assertRaises(osm.HTTPFailure):
                self.client(transport=transport).fetch(BOUNDS, manual=True)
            self.clock.advance(901)
            with self.assertRaises(osm.Throttled) as err:
                self.client().fetch(OTHER, manual=True)
            self.assertGreater(err.exception.retry_after, 2600)

    def test_http_date_retry_after(self):
        date = email.utils.format_datetime(datetime.fromtimestamp(WALL + 1200, timezone.utc), usegmt=True)
        transport = FakeTransport(status=429, headers={"Retry-After": date})
        with self.assertRaises(osm.HTTPFailure):
            self.client(transport=transport).fetch(BOUNDS, manual=True)
        self.assertEqual(self.coordinator.next_request - self.clock(), 1200)

    def test_timeouts_errors_and_malformed_responses_consume_cooldown(self):
        self.transport.post = mock.Mock(side_effect=TimeoutError("private endpoint or credentials"))
        with self.assertRaises(osm.RetrievalError) as err:
            self.client().fetch(BOUNDS, manual=True)
        self.assertNotIn("private", str(err.exception))
        self.clock.advance(60)
        with self.assertRaises(osm.Throttled):
            self.client().fetch(BOUNDS, manual=True)
        self.assertEqual(self.transport.post.call_count, 1)

    def test_stale_endpoint_replication_is_detected_against_prior_observation(self):
        client = self.client(replace(CONFIG, min_spacing_s=0, area_cooldown_s=0))
        client.fetch(BOUNDS, manual=True)
        self.transport.payload = response(way(), replication="2026-10-30T11:59:30Z")
        result = client.fetch(BOUNDS, manual=True)
        self.assertTrue(result.stale)
        with self.assertRaises(osm.StaleSnapshot):
            result.refresh_info("baseline")

    def test_malformed_and_server_error_responses_never_return_partial_snapshots(self):
        for payload in (b"not JSON", response(way(), remark="runtime error: out of memory"), response(way(), sentinel=False)):
            self.coordinator = osm.Coordinator(self.clock)
            transport = FakeTransport(payload=payload)
            with self.subTest(payload=payload), self.assertRaises(osm.IncompleteResponse):
                self.client(transport=transport).fetch(BOUNDS, manual=True)
            self.assertEqual(len(transport.calls), 1)

    def test_endpoint_and_credentials_validation_and_redaction(self):
        bad = ("http://osm-source.invalid/api", "https://user:secret@osm-source.invalid/api",
               "https://osm-source.invalid/api?token=secret", "https://osm-source.invalid/api#fragment",
               "https://overpass-api.de./api", "file:///local", "https://osm-source.invalid/\r\n")
        for endpoint in bad:
            with self.subTest(endpoint=endpoint), self.assertRaises(osm.ConfigurationError):
                replace(CONFIG, endpoint=endpoint)
        for headers in ({"User-Agent": "hidden"}, {"Host": "elsewhere"}, {"Authorization": "bad\r\nvalue"}):
            with self.subTest(headers=headers), self.assertRaises(osm.ConfigurationError):
                replace(CONFIG, headers=headers)
        config = replace(CONFIG, headers={"Authorization": "Bearer synthetic-secret"})
        result = self.client(config).fetch(BOUNDS, manual=True)
        self.assertNotIn("synthetic-secret", repr(config) + repr(result))
        self.assertEqual(self.transport.calls[0][2]["Authorization"], "Bearer synthetic-secret")


class TransportTest(NoNetworkTest):
    def run_transport(self, payload=b"data", *, headers=None, status=200, limit=100, advance=0, total=45):
        clock = Clock()
        reads, connections = [], []
        class Response:
            def __init__(self):
                self.status, self.body = status, io.BytesIO(payload)
            def getheaders(self):
                return list((headers or {}).items())
            def read1(self, size):
                reads.append(size)
                clock.advance(advance)
                return self.body.read(min(size, 3))
            def close(self):
                self.body.close()
        class Connection:
            def __init__(self, *args, **kw):
                self.sock, self.closed, self.timeouts = self, False, []
                connections.append(self)
            def connect(self):
                pass
            def settimeout(self, value):
                self.timeouts.append(value)
            def request(self, *args, **kw):
                self.request_args = (args, kw)
            def getresponse(self):
                return Response()
            def close(self):
                self.closed = True
        with mock.patch.object(osm.http.client, "HTTPSConnection", Connection), \
                mock.patch.object(netfetch, "windows_ssl_context", return_value=None):
            try:
                result = osm.BoundedPOST(clock).post(CONFIG.endpoint, b"data=query", {"User-Agent": netfetch.USER_AGENT},
                                                    timeout=10, total_timeout=total, max_bytes=limit)
                return result, reads, connections
            finally:
                self.assertTrue(all(c.closed for c in connections))
                self.reads = reads

    def test_streaming_read_and_connection_closure(self):
        result, reads, connections = self.run_transport(headers={"Content-Length": "4"})
        self.assertEqual(result[2], b"data")
        self.assertTrue(reads)
        self.assertEqual(connections[0].request_args[0][0], "POST")
        self.assertTrue(all(0 < v <= 10 for v in connections[0].timeouts))

    def test_declared_size_rejected_before_read(self):
        with self.assertRaises(osm.ResourceLimit):
            self.run_transport(headers={"Content-Length": "101"})
        self.assertFalse(self.reads)

    def test_chunked_or_unknown_size_stops_at_limit_plus_one(self):
        with self.assertRaises(osm.ResourceLimit):
            self.run_transport(payload=b"a" * 10000, limit=10)
        self.assertEqual(sum(min(n, 3) for n in self.reads), 11)

    def test_body_read_deadline(self):
        with self.assertRaises(TimeoutError):
            self.run_transport(payload=b"a" * 100, advance=3, total=5)
        self.assertEqual(len(self.reads), 2)

    def test_incomplete_length_and_compressed_response_fail(self):
        with self.assertRaises(osm.IncompleteResponse):
            self.run_transport(headers={"Content-Length": "9"})
        with self.assertRaises(osm.IncompleteResponse):
            self.run_transport(headers={"Content-Encoding": "gzip"})

    def test_error_headers_honoured_without_reading_error_body(self):
        result, reads, _ = self.run_transport(status=429, headers={"Retry-After": "3000", "Content-Length": "999999"})
        self.assertEqual(result[:2], (429, {"retry-after": "3000", "content-length": "999999"}))
        self.assertFalse(reads)


if __name__ == "__main__":
    unittest.main()
