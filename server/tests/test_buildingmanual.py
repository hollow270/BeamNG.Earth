"""Stage 5A integration with mocked HTTP, real processed cells and real mesh workers."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import multiprocessing
from pathlib import Path
import sys
import threading
from types import MethodType, SimpleNamespace
import unittest
from unittest import mock

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import test_buildingrender as fx
import test_buildingrefresh as regen
import test_osmbuildings as ofx
import buildingbaseline
import buildingmanual
import buildingoverlay as bo
from buildingoverlaystore import StalePreview, BaselineMismatch, ResourceLimit
import buildingrefresh
import buildingspatial
import buildingtargets
import blockworker as bw
import cdae
import earthd
import geodesy
import geotiles
import mapcells
import netfetch
import osmbuildings as osm
import roads
import vegetation


def way(i=1,x=60,y=60,w=10,d=10,*,geometry=None,tags=None):
    coordinates = (geometry or fx.polygon(x,y,w,d))["coordinates"][0]
    nodes = [dict(type="node",id=i*100+k+1,lon=lo,lat=la,version=2,timestamp=ofx.ELEMENT_STAMP)
             for k,(lo,la) in enumerate(coordinates[:-1])]
    return nodes+[dict(type="way",id=i,nodes=[n["id"] for n in nodes]+[nodes[0]["id"]],version=2,
                      timestamp=ofx.ELEMENT_STAMP,tags=tags or {"building":"retail","height":"12","roof:shape":"flat"})]


def bounds(x=20,y=20,w=200,d=200,frame=fx.FRAME):
    return bo._bbox(fx.polygon(x,y,w,d,frame))


def tree(directory):
    return {str(p.relative_to(directory)):(p.stat().st_mtime_ns,hashlib.sha256(p.read_bytes()).hexdigest())
            for p in Path(directory).rglob("*") if p.is_file()}


class ManualTest(unittest.TestCase):
    setUp = fx.RenderTest.setUp
    md = fx.RenderTest.md
    close = regen.RefreshTest.close

    def setup(self, originals=(), parts=(), *, payload=None, street=False):
        self.base = self.md(originals,parts,street=street)
        self.worker_md = self.md(originals,parts,street=street)
        self.base._building_store = self.store
        self.pool = ThreadPoolExecutor(2)
        self.addCleanup(self.pool.shutdown,wait=True,cancel_futures=True)
        mock.patch.object(bw,"_s",regen.state(self.worker_md,self.root/"worker-cache")).start()
        anchor = SimpleNamespace(id=1,lat=fx.LAT,lon=fx.LON,frame=fx.FRAME,zooms=(15,))
        tracker = roads.Tracker()
        tracker.aid = 1
        tracker.empty = {(0,0)}  # an originally empty tile is a regeneration target too
        conn = SimpleNamespace(active=1,roads=tracker,bldfar=None,bldsky=None)
        self.server = SimpleNamespace(mapdata=self.base,buildings=True,cache_dir=str(self.root/"server"),
            road_pool=self.pool,far_bld_pool=self.pool,near_visual="mesh",sidewalks=True,gas_stations=False,
            anchors={1:anchor},connections=[conn],_building_refresh_lock=threading.Lock(),
            anchor_q=lambda a:0.05,left_hand=lambda a:True,mph=lambda a:False,buried=lambda a:0.33,
            lamp_region=lambda a:"eu",country=lambda a:"MA",
            geo_rules=lambda x,y,z=None:(True,False,0.33,"eu","MA",15))
        for name in ("configure_osm_buildings","_manual_building_workflow","preview_osm_buildings",
                     "accept_osm_buildings","retry_osm_buildings","refresh_building_overlays"):
            setattr(self.server,name,MethodType(getattr(earthd.Server,name),self.server))
        self.clock = ofx.Clock()
        self.wall = ofx.WALL
        self.transport = ofx.FakeTransport(payload=ofx.response(way()) if payload is None else payload)
        self.base.fetcher = netfetch.Fetcher(transport=object(),clock=self.clock,sleep=self.clock.advance,wall=lambda:self.wall)
        self.server.configure_osm_buildings(ofx.CONFIG,transport=self.transport)
        self.workflow = self.server._building_manual
        self.workflow.client.coordinator = osm.Coordinator(self.clock)
        self.workflow.client.wall = self.workflow.wall = lambda:self.wall
        # Warm explicitly BEFORE preview. Preview itself never fetches/builds/mirrors baseline cells.
        box = bounds(-2000,-2000,4000,4000)
        w,s,e,n = box
        for key in mapcells.cells_for_bbox(s,w,n,e):
            self.base.cell(key,layers=("buildings","building_parts"))
        return conn

    def preview(self, **kw):
        return self.server.preview_osm_buildings(bounds(),manual=True,**kw)

    def accept(self, preview):
        return self.server.accept_osm_buildings(preview["token"],manual=True)

    def entry(self, kind="near"):
        coordinator = self.server._building_refresh
        row = next(r for r in coordinator.outputs()["entries"].values() if r["request"]["kind"]==kind)
        return row,coordinator.io.objects/row["generation"]

    def test_fetch_preview_accept_discover_store_real_prepared_asset(self):
        self.setup()
        preview = self.preview(enable=True)
        self.assertEqual(preview["merge"]["counts"]["addition"],1)
        self.assertIsNone(self.store.load().revision)
        result = self.accept(preview)
        self.assertTrue(self.store.load().enabled)
        self.assertEqual(result["accepted_revision"],self.store.load().revision)
        self.assertEqual(result["generation"]["status"],"prepared")
        self.assertFalse(result["game_activated"])
        row,directory = self.entry()
        self.assertEqual(row["snapshot"],result["accepted_revision"])
        self.assertTrue(cdae.read(directory/"bld_1_0_0.cdae")["meshes"])

    def test_preview_is_deterministic_and_has_zero_persistent_modifications(self):
        self.setup([fx.record()])
        before = tree(self.root)
        a = self.preview()
        self.assertEqual(tree(self.root),before)
        self.clock.advance(601)
        b = self.preview()
        self.assertEqual(a,b)
        self.assertEqual(tree(self.root),before)
        self.assertNotIn("_building_refresh",vars(self.server))

    def test_review_exposes_actual_geometry_attribute_and_original_fingerprint_changes(self):
        self.setup([fx.record(attrs={"height":30}),fx.record("gers-b",130,130,osm="w9@1")])
        p = self.preview(enable=True)
        change = next(c for c in p["changes"] if c["feature_id"]=="gers-a")
        self.assertEqual(change["attributes_changed"]["height"],{"before":30.0,"after":12.0})
        self.assertIsNotNone(change["geometry_before"])
        self.assertIsNotNone(change["geometry_after"])
        self.assertEqual(len(p["overture_only_details"][0]["baseline_fingerprint"]),64)
        self.assertTrue(p["generation_preview"]["advisory"])
        # Editing the returned report cannot rewrite the server's reviewed proposal.
        change["attributes_changed"]["height"]["after"] = 999
        self.accept(p)
        self.assertEqual(self.store.load().entries[0].feature.attrs["height"],12.0)

    def test_baseline_changed_during_object_write_rejects_publication(self):
        self.setup()
        p = self.preview(enable=True)
        path = Path(self.workflow.previews[p["token"]]["context"].files[0][0])
        write = self.store._atomic_write
        def changed(destination,data):
            write(destination,data)
            if destination.parent==self.store.objects:
                with path.open("ab") as f:
                    f.write(b"changed")
        with mock.patch.object(self.store,"_atomic_write",side_effect=changed):
            with self.assertRaises(StalePreview):
                self.accept(p)
        self.assertIsNone(self.store.load().revision)
        self.assertNotIn("_building_refresh",vars(self.server))

    def test_unsigned_nonmonotonic_offsets_fail_closed(self):
        self.setup([fx.record()])
        path = Path(self.base.path(fx.CELL,"buildings"))
        with np.load(path,allow_pickle=False) as z:
            arrays = {k:z[k] for k in z.files}
        arrays["coord_off"] = np.asarray([0,4,3,5],dtype=np.uint32)
        arrays["ring_off"] = np.asarray([0,3],dtype=np.uint32)
        np.savez(path,**arrays)
        with self.assertRaisesRegex(BaselineMismatch,"invalid baseline geometry offsets"):
            self.preview()
        self.assertFalse(self.transport.calls)

    def test_clipped_original_parts_reject_preview_before_network(self):
        self.setup([fx.record(attrs={"height":12,"has_parts":True})],
                   [fx.record("part-a",parent_id="gers-a",osm="w2@1")])
        path = Path(self.base.path(fx.CELL,"building_parts"))
        with np.load(path,allow_pickle=False) as z:
            arrays = {k:z[k] for k in z.files}
        arrays["clipped"] = np.ones(1,dtype=bool)
        np.savez(path,**arrays)
        with self.assertRaisesRegex(BaselineMismatch,"parts are clipped"):
            self.preview()
        self.assertFalse(self.transport.calls)

    def test_zero_itemsize_cache_array_is_rejected_before_numpy_allocation(self):
        self.setup()
        path = Path(self.base.path(fx.CELL,"buildings"))
        with np.load(path,allow_pickle=False) as z:
            arrays = {k:z[k] for k in z.files}
        arrays["ids"] = np.empty(2,dtype="V0")
        np.savez(path,**arrays)
        with self.assertRaisesRegex(BaselineMismatch,"invalid cached baseline array size/type"):
            self.preview()
        self.assertFalse(self.transport.calls)

    def test_zero_byte_orphan_files_are_bounded_without_deleting_referenced_data(self):
        self.setup()
        for i in range(300):
            (self.store.objects/f"interrupted-{i}.tmp").touch()
        with self.assertRaisesRegex(ResourceLimit,"filesystem entry budget"):
            self.preview()
        self.assertIsNone(self.store.load().revision)
        self.assertEqual(len(list(self.store.objects.glob("*.tmp"))),300)

    def test_known_identity_geometry_replacement_retains_gers_and_unrelated_building(self):
        old,unrelated = fx.record(),fx.record("gers-b",130,130,osm="w9@1")
        self.setup([old,unrelated],payload=ofx.response(way(w=18)))
        preview = self.preview(enable=True)
        self.assertEqual(preview["merge"]["counts"]["replacement"],1)
        self.assertIn("gers-b",preview["overture_only"])
        result = self.accept(preview)
        self.assertEqual(result["generation"]["status"],"prepared")
        with self.worker_md.building_snapshot(self.store.load()):
            f = self.worker_md.building_cell(fx.CELL).layers["buildings"]
            self.assertEqual(f.ids.tolist(),["gers-a","gers-b"])
            rows = bo.from_mapgeom(f)
            self.assertEqual(rows[1].geometry,bo.from_mapgeom(self.worker_md.cell(fx.CELL).layers["buildings"])[1].geometry)

    def test_overture_only_absence_and_known_id_omission_never_delete(self):
        old = fx.record()
        self.setup([old],payload=ofx.response([]))
        preview = self.preview(enable=True)
        self.assertEqual(preview["overture_only"],["gers-a"])
        self.assertTrue(any("not_returned" in i["code"] for i in preview["issues"]))
        self.assertFalse(preview["omission_is_deletion"])
        self.accept(preview)
        self.assertEqual(len(self.store.load().entries),0)
        row,directory = self.entry()
        self.assertTrue((directory/"bld_1_0_0.cdae").exists())

    def test_different_known_osm_identity_stays_conflict_and_keeps_baseline(self):
        self.setup([fx.record(osm="w99@1")])
        preview = self.preview(enable=True)
        self.assertEqual(preview["merge"]["counts"]["conflict"],1)
        self.assertTrue(any(d["reason"]=="conflicting_osm_identity" for d in preview["merge"]["decisions"]))
        self.accept(preview)
        self.assertFalse(self.store.load().entries)
        self.assertTrue(self.store.load().conflicts)

    def test_identityless_overture_exact_trace_is_associated_without_replacing_its_geometry(self):
        old = replace(fx.record(),sources=({"dataset":"Microsoft","record_id":"vendor-footprint"},))
        self.setup([old])
        original = bo.from_mapgeom(self.base.cell(fx.CELL,layers=("buildings",)).layers["buildings"])[0]
        p = self.preview(enable=True)
        self.assertFalse(p["merge"]["counts"]["conflict"])
        self.assertTrue(p["spatial_diagnostics"][0]["measurements"]["available"])
        result = self.accept(p)
        self.assertEqual(result["generation"]["status"],"prepared")
        entry = self.store.load().entries[0]
        self.assertEqual(entry.feature.feature_id,"gers-a")
        self.assertEqual(bo.geometry_digest(entry.feature.geometry),bo.geometry_digest(original.geometry))

    def test_missing_config_offline_denial_and_manual_requirement_are_explicit(self):
        self.setup()
        with self.assertRaises(osm.ConfigurationError):
            self.workflow.preview(bounds())
        self.workflow.client = None
        with self.assertRaises(osm.ConfigurationError):
            self.preview()
        self.server.configure_osm_buildings(ofx.CONFIG,transport=self.transport)
        self.workflow = self.server._building_manual
        self.base.fetcher.offline_flag = True
        with self.assertRaises(netfetch.Offline):
            self.preview()
        self.base.fetcher.offline_flag = False
        with mock.patch.object(self.base.fetcher,"policy",return_value=SimpleNamespace(denied=True)):
            with self.assertRaises(netfetch.PolicyDenied):
                self.preview()
        self.assertFalse(self.transport.calls)

    def test_reconfiguration_retires_resolved_old_workflow_before_it_can_accept(self):
        self.setup()
        p = self.preview(enable=True)
        old = self.workflow
        self.server.configure_osm_buildings(ofx.CONFIG,transport=self.transport)
        self.assertTrue(old.retired)
        self.assertFalse(old.previews)
        # Covers a caller that resolved the old workflow just before reconfiguration.
        with self.assertRaises(StalePreview):
            old.accept(p["token"],manual=True)
        self.assertIsNone(self.store.load().revision)

    def test_client_rate_limits_are_not_bypassed(self):
        self.setup()
        self.preview()
        with self.assertRaises(osm.Throttled):
            self.preview()
        self.assertEqual(len(self.transport.calls),1)

    def test_response_limits_and_newer_replication_observation_are_enforced(self):
        self.setup()
        self.workflow.client.config = replace(self.workflow.client.config,max_response_bytes=8)
        with self.assertRaises(osm.ResourceLimit):
            self.preview()
        self.workflow.client.config = ofx.CONFIG
        self.clock.advance(601)
        p = self.preview()
        self.workflow.client.coordinator.latest["osm-source.invalid"] = "2026-10-30T12:00:30Z"
        with self.assertRaises(StalePreview):
            self.accept(p)
        self.assertIsNone(self.store.load().revision)

    def test_stale_missing_timestamp_truncation_and_missing_dependencies_rejected(self):
        self.setup()
        for payload in (ofx.response(way(),replication=None),ofx.response(way(),sentinel=False),
                        ofx.response(way()[1:]),ofx.response(way(),replication="2026-10-29T12:00:00Z")):
            self.transport.payload = payload
            self.clock.advance(601)
            with self.assertRaises((osm.IncompleteResponse,osm.StaleSnapshot)):
                self.preview()
        self.assertIsNone(self.store.load().revision)

    def test_retagging_is_reported_and_keeps_original(self):
        self.setup([fx.record()],payload=ofx.response(way(tags={"building":"no"})))
        preview = self.preview(enable=True)
        self.assertTrue(any(d["reason"]=="deletion_or_retagging_unverified" for d in preview["merge"]["decisions"]))
        self.accept(preview)
        self.assertFalse(self.store.load().entries)

    def test_preview_stale_after_enable_disable_rollback_and_aba(self):
        self.setup()
        p = self.preview(enable=True)
        self.store.enable()
        self.store.disable()
        with self.assertRaises(StalePreview):
            self.accept(p)
        self.clock.advance(601)
        p = self.preview(enable=True)
        accepted = self.accept(p)
        self.transport.payload = ofx.response(way(w=18))
        self.clock.advance(601)
        p = self.preview()
        self.store.rollback()
        with self.assertRaises(StalePreview):
            self.accept(p)

    def test_retained_review_revalidation_is_read_only_and_checks_config_and_baseline(self):
        self.setup([fx.record('gers-a',60,60,osm='w1@1')])
        proposal=self.preview()
        before=tree(self.store.root)
        self.workflow.validate_preview(proposal['token'],manual=True)
        self.assertEqual(tree(self.store.root),before)
        self.assertIn(proposal['token'],self.workflow.previews)
        config=self.workflow.client.config
        self.workflow.client.config=replace(config,headers={'Authorization':'changed-private-value'})
        with self.assertRaises(StalePreview): self.workflow.validate_preview(proposal['token'],manual=True)
        self.workflow.client.config=config
        path=Path(self.workflow.previews[proposal['token']]['context'].files[0][0])
        path.write_bytes(path.read_bytes()+b'changed baseline')
        with self.assertRaises(StalePreview): self.workflow.validate_preview(proposal['token'],manual=True)
        self.assertIsNone(self.store.load().revision)

    def test_preview_stale_after_baseline_file_changes_even_with_a_warm_decoded_lru(self):
        self.setup([fx.record()])
        p = self.preview(enable=True)
        path = Path(self.base.path(fx.CELL,"buildings"))
        with np.load(path,allow_pickle=False) as z:
            arrays = {k:z[k] for k in z.files}
        arrays["attr.height"][0] = 99
        np.savez_compressed(path,**arrays)
        with self.assertRaises(StalePreview):
            self.accept(p)
        self.assertIsNone(self.store.load().revision)

    def test_expired_preview_changed_credentials_and_replay_are_rejected(self):
        self.setup()
        p = self.preview()
        self.wall += 301
        with self.assertRaises(StalePreview):
            self.accept(p)
        self.wall = ofx.WALL
        self.clock.advance(601)
        p = self.preview()
        self.workflow.client.config.headers["Authorization"] = "secret test token"
        with self.assertRaises(StalePreview):
            self.accept(p)
        self.workflow.client.config.headers.clear()
        with self.assertRaises(StalePreview):
            self.accept(p)
        self.clock.advance(601)
        p = self.preview()
        result = self.accept(p)
        with self.assertRaises(StalePreview):
            self.accept(p)
        for path in self.store.root.rglob("*.json"):
            self.assertNotIn(b"secret test token",path.read_bytes())

    def test_incomplete_original_neighbourhood_is_not_empty_and_fetch_is_not_started(self):
        self.setup()
        Path(self.base.path(fx.CELL,"building_parts")).unlink()
        with self.assertRaises(BaselineMismatch):
            self.preview()
        self.assertFalse(self.transport.calls)

    def test_known_object_moving_beyond_cached_neighbourhood_is_rejected_after_fetch(self):
        self.setup([fx.record()],payload=ofx.response(way(x=3500)))
        with self.assertRaises(BaselineMismatch):
            self.preview(enable=True)
        self.assertEqual(len(self.transport.calls),1)
        self.assertIsNone(self.store.load().revision)

    def test_unsupported_multipolygon_holes_and_incomplete_parts_remain_explicit_conflicts(self):
        self.setup([fx.record()])
        outer = way(10,x=60,y=60,w=30,d=30)
        for second,role in ((way(11,x=100,y=60),"outer"),(way(11,x=65,y=65),"inner")):
            relation = ofx.relation(1,[("way",10,"outer"),("way",11,role)],
                                     {"type":"multipolygon","building":"yes"})
            self.transport.payload = ofx.response(outer+second+[relation])
            p = self.preview(enable=True)
            self.assertTrue(p["merge"]["counts"]["conflict"])
            self.assertTrue(any("unsupported" in d["reason"] for d in p["merge"]["decisions"]))
            self.assertIsNone(self.store.load().revision)
            self.clock.advance(601)
        self.transport.payload = ofx.response(way(w=20,d=20)+way(10,x=61,y=61,w=5,d=5,
                                                       tags={"building:part":"yes","height":"20"}))
        p = self.preview(enable=True)
        self.assertTrue(any("parts" in d["reason"] for d in p["merge"]["decisions"] if d["action"]=="conflict"))
        self.accept(p)
        self.assertFalse(self.store.load().entries)

    def test_complete_building_parts_reach_prepared_mesh_with_preserved_links(self):
        parent = fx.record(attrs={"height":30,"has_parts":True})
        part = fx.record("gers-part",61,61,7,7,parent_id="gers-a",osm="w10@1",attrs={"height":14})
        association = ofx.relation(9,[("way",1,"outline"),("way",10,"part")],{"type":"building"})
        payload = ofx.response(way(tags={"building":"retail","height":"30"})+
            way(10,x=61,y=61,w=8,d=7,tags={"building:part":"yes","height":"22"})+[association])
        self.setup([parent],[part],payload=payload)
        p = self.preview(enable=True)
        self.assertFalse(p["merge"]["counts"]["conflict"])
        result = self.accept(p)
        self.assertEqual(result["generation"]["status"],"prepared")
        with self.worker_md.building_snapshot(self.store.load()):
            parts = self.worker_md.building_cell(fx.CELL).layers["building_parts"]
            self.assertEqual(parts.ids.tolist(),["gers-part"])
            self.assertEqual(parts.attrs["building_id"].tolist(),["gers-a"])
            self.assertAlmostEqual(float(parts.attrs["height"][0]),22)

    def test_cross_cell_cross_tile_and_density_neighbours_are_automatically_discovered(self):
        conn = self.setup()
        frame = geodesy.EnuFrame(fx.LAT,fx.CELL.bounds()[2])
        selection = bounds(-180,20,400,100,frame)
        geometry = fx.polygon(-150,60,300,10,frame)
        self.transport.payload = ofx.response(way(geometry=geometry))
        centre = fx.FRAME.geodetic_to_enu(frame.lat0,frame.lon0,0.0)
        tx = int(np.floor(float(centre[0])/roads.TILE))
        conn.roads.empty = {(i,0) for i in range(tx-2,tx+3)}
        p = self.server.preview_osm_buildings(selection,manual=True,enable=True)
        result = self.accept(p)
        self.assertEqual(result["generation"]["status"],"prepared")
        targets = result["generation"]["discovery"]["targets"]
        self.assertTrue({tx,tx+1} <= {r["x"] for r in targets})
        affected = result["generation"]["result"]["affected_cells"]
        self.assertIn(str(fx.CELL),affected)
        self.assertIn(str(mapcells.CellKey(fx.CELL.z,fx.CELL.x+1,fx.CELL.y)),affected)

    def test_concurrent_state_change_during_http_invalidates_preview_without_deadlock(self):
        self.setup()
        entered,leave = threading.Event(),threading.Event()
        post = self.transport.post
        def blocked(*a,**kw):
            entered.set()
            if not leave.wait(5):
                raise RuntimeError("test timed out")
            return post(*a,**kw)
        with mock.patch.object(self.transport,"post",side_effect=blocked):
            future = self.pool.submit(self.preview,enable=True)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(buildingmanual.Busy):
                    self.preview()
                self.store.enable()
            finally:
                leave.set()
            with self.assertRaises(StalePreview):
                future.result(5)
        self.assertIsNone(self.store.load().revision)

    def test_acceptance_survives_preparation_failure_and_restart_retry_without_network(self):
        self.setup()
        p = self.preview(enable=True)
        with mock.patch.object(self.server,"refresh_building_overlays",side_effect=RuntimeError("worker failed")):
            result = self.accept(p)
        self.assertEqual(result["generation"]["status"],"pending")
        self.assertEqual(self.store.load().revision,result["accepted_revision"])
        self.assertEqual(len(self.transport.calls),1)

    def test_second_acceptance_failure_preserves_previous_catalog_and_retry_prepares_latest(self):
        self.setup()
        first = self.accept(self.preview(enable=True))
        previous = self.server._building_refresh.outputs()["entries"]
        self.transport.payload = ofx.response(way(w=18))
        self.clock.advance(601)
        p = self.preview()
        with mock.patch.object(self.server,"refresh_building_overlays",side_effect=RuntimeError("worker died")):
            result = self.accept(p)
        self.assertEqual(result["generation"]["status"],"pending")
        self.assertNotEqual(self.store.load().revision,first["accepted_revision"])
        self.assertEqual(self.server._building_refresh.outputs()["entries"],previous)
        retry = self.server.retry_osm_buildings(p["token"],manual=True)
        self.assertEqual(retry["generation"]["status"],"prepared")
        self.assertNotEqual(self.server._building_refresh.outputs()["entries"],previous)

    def test_uncertain_acceptance_io_result_can_be_recovered_from_durable_marker(self):
        self.setup()
        p = self.preview(enable=True)
        publish = self.store._publish
        def uncertain(*a,**kw):
            publish(*a,**kw)
            raise OSError("simulated post-publication fsync failure")
        with mock.patch.object(self.store,"_publish",side_effect=uncertain):
            result = self.accept(p)
        self.assertEqual(result["status"],"acceptance_not_confirmed")
        self.assertTrue(self.store.load().entries)
        retry = self.server.retry_osm_buildings(p["token"],manual=True)
        self.assertEqual(retry["generation"]["status"],"prepared")
        del self.server._building_manual
        retry = self.server.retry_osm_buildings(p["token"],manual=True)
        self.assertEqual(retry["generation"]["status"],"prepared")
        self.assertIsNone(self.server._building_manual.client)
        self.assertEqual(len(self.transport.calls),1)

    def test_server_restart_retry_recreates_worker_and_needs_no_retrieval_endpoint(self):
        self.setup([fx.record()],street=True)
        p = self.preview(enable=True)
        with mock.patch.object(self.server,"refresh_building_overlays",side_effect=RuntimeError("stopped worker")):
            self.assertEqual(self.accept(p)["generation"]["status"],"pending")
        fields = {k:v for k,v in vars(self.server).items() if not isinstance(v,MethodType)
                  and k not in ("_building_manual","_building_refresh")}
        restarted = SimpleNamespace(**fields)
        restarted._building_refresh_lock = threading.Lock()
        for name in ("_manual_building_workflow","retry_osm_buildings","refresh_building_overlays"):
            setattr(restarted,name,MethodType(getattr(earthd.Server,name),restarted))
        with ProcessPoolExecutor(1,mp_context=multiprocessing.get_context("spawn"),initializer=regen.init_worker,
                                 initargs=(str(self.root/"restarted-worker"),self.base.originals,())) as process:
            restarted.road_pool = restarted.far_bld_pool = process
            result = restarted.retry_osm_buildings(p["token"],manual=True)
            self.assertEqual(result["generation"]["status"],"prepared")
            self.assertFalse(process.submit(regen.startup_overlay_active).result(10))
        self.assertIsNone(restarted._building_manual.client)
        self.assertEqual(len(self.transport.calls),1)

    def test_worker_failure_pending_and_acceptance_are_separate_durable_states(self):
        self.setup()
        p = self.preview(enable=True)
        result = self.accept(p)
        self.assertEqual(result["generation"]["status"],"prepared")
        snapshot = self.store.load()
        self.assertEqual(self.workflow.accepted()[0]["token"],p["token"])
        # An accepted marker is durable independently of the process-local preview capability.
        del self.server._building_manual
        self.assertIsNotNone(snapshot.revision)
        with self.assertRaises(osm.ConfigurationError):
            self.server.accept_osm_buildings(p["token"],manual=True)
        self.assertEqual(self.server.retry_osm_buildings(p["token"],manual=True)["generation"]["status"],"prepared")

    def test_rollback_between_acceptance_and_preparation_is_pending_not_silently_substituted(self):
        self.setup()
        p = self.preview(enable=True)
        prepare = self.workflow._prepare
        def raced(bounds,state):
            self.store.rollback()
            return prepare(bounds,state)
        with mock.patch.object(self.workflow,"_prepare",side_effect=raced):
            result = self.accept(p)
        self.assertIsNotNone(result["accepted_revision"])
        self.assertEqual(result["generation"]["status"],"pending")
        with self.assertRaises(StalePreview):
            self.server.retry_osm_buildings(p["token"],manual=True)

    def test_no_active_anchor_or_corresponding_target_is_not_asset_success(self):
        conn = self.setup()
        self.server.connections = []
        p = self.preview(enable=True)
        result = self.accept(p)
        self.assertEqual(result["generation"]["status"],"no_active_anchor")
        self.assertFalse(result["generation"]["prepared"])
        self.assertTrue(self.store.load().entries)
        self.server.connections = [conn]
        conn.roads.empty.clear()
        retry = self.server.retry_osm_buildings(p["token"],manual=True)
        self.assertEqual(retry["generation"]["status"],"no_corresponding_targets")
        conn.roads.empty.add((0,0))
        self.assertEqual(self.server.retry_osm_buildings(p["token"],manual=True)["generation"]["status"],"prepared")

    def test_feature_off_acceptance_preserves_baseline_mesh_road_and_terrain_inputs(self):
        self.setup([fx.record()],street=True)
        initial = buildingtargets.discover(self.server,bounds())
        self.server.refresh_building_overlays(initial.requests)
        before,old_dir = self.entry()
        digest = self.base.cell(fx.CELL).digest()
        p = self.preview(enable=False)
        result = self.accept(p)
        after,new_dir = self.entry()
        self.assertFalse(self.store.load().enabled)
        self.assertEqual(result["generation"]["status"],"prepared_baseline")
        self.assertEqual(before["content_digest"],after["content_digest"])
        self.assertEqual(self.base.cell(fx.CELL).digest(),digest)
        self.assertEqual((old_dir/"road_1_0_0.cdae").read_bytes(),(new_dir/"road_1_0_0.cdae").read_bytes())

    def test_target_settings_legacy_geo_and_retained_unrelated_output(self):
        conn = self.setup([fx.record()])
        req = buildingtargets.discover(self.server,bounds()).requests[0]
        options = json.loads(req.options)
        self.assertEqual((options["left_hand"],options["mph"],options["lift"],options["buried"],options["country"]),
                         (True,False,0.0,0.33,"MA"))
        distant = replace(req,x=12)
        self.server.refresh_building_overlays([req,distant])
        coordinator = self.server._building_refresh
        old = coordinator.outputs()["entries"][distant.slot]
        result = self.accept(self.preview(enable=True))
        self.assertIn(distant.slot,result["generation"]["result"]["reused"])
        self.assertEqual(coordinator.outputs()["entries"][distant.slot],old)
        conn.roads = roads.GeoTracker()
        conn.roads.aid = 1
        la,lo,_ = fx.FRAME.enu_to_geodetic(65,65,0)
        conn.roads.sent.add(geotiles.tile_of(float(la),float(lo)))
        for attr,z in (("bldfar",geotiles.FAR_Z),("bldsky",geotiles.SKY_Z)):
            tr = vegetation.Tracker(kind=attr,geo=True,z=z)
            tr.aid = 1
            tr.sent.add(geotiles.tile_of(float(la),float(lo),z))
            setattr(conn,attr,tr)
        discovered = buildingtargets.discover(self.server,bounds())
        self.assertTrue({"near_geo","far_geo","skyline_geo"} <= {r.kind for r in discovered.requests})
        for r in discovered.requests:
            if r.kind.endswith("_geo"):
                self.assertEqual(r.gzoom,15)

    def test_unrelated_retained_target_cannot_claim_preparation_for_selection(self):
        conn = self.setup()
        local = buildingtargets.discover(self.server,bounds()).requests[0]
        distant = replace(local,x=12)
        self.server.refresh_building_overlays([distant])
        previous = self.server._building_refresh.outputs()["entries"]
        conn.roads.empty.clear()
        p = self.preview(enable=True)
        result = self.accept(p)
        self.assertEqual(result["generation"]["status"],"no_corresponding_targets")
        self.assertFalse(result["generation"]["prepared"])
        self.assertEqual(result["generation"]["discovery"]["relevant_slots"],[])
        self.assertEqual(self.server._building_refresh.outputs()["entries"],previous)
        conn.roads.empty.add((0,0))
        retried = self.server.retry_osm_buildings(p["token"],manual=True)
        self.assertEqual(retried["generation"]["status"],"prepared")
        self.assertIn(distant.slot,retried["generation"]["result"]["reused"])

    def test_target_budget_error_is_explicit_and_retirement_does_not_delete_files(self):
        conn = self.setup()
        conn.roads.empty.add((1,0))
        with self.assertRaises(ResourceLimit):
            buildingtargets.discover(self.server,bounds(),max_targets=1)
        reqs = buildingtargets.discover(self.server,bounds()).requests
        self.server.refresh_building_overlays(reqs)
        old_files = list(self.server._building_refresh.io.objects.rglob("*.json"))
        self.server.connections = []
        result = buildingtargets.discover(self.server,bounds())
        self.assertEqual(len(result.retired),2)
        self.assertFalse(result.requests)
        self.assertTrue(all(p.exists() for p in old_files))


class SpatialTest(unittest.TestCase):
    def test_analytic_intersection_containment_and_winding_are_exact(self):
        a = ((0.,0.),(4.,0.),(4.,4.),(0.,4.))
        b = ((2.,0.),(6.,0.),(6.,4.),(2.,4.))
        self.assertEqual(buildingspatial.measurements(a,b)["iou_fraction"],"1/3")
        self.assertEqual(buildingspatial.measurements(a,b)["intersection_degrees2_fraction"],"8")
        contained = ((1.,1.),(3.,1.),(3.,3.),(1.,3.))
        m = buildingspatial.measurements(a,contained)
        self.assertTrue(m["a_contains_b"])
        self.assertEqual(m["iou_fraction"],"1/4")
        self.assertEqual(buildingspatial.measurements(tuple(reversed(a)),b)["iou_fraction"],"1/3")

    def test_strong_overlap_does_not_override_ambiguous_multiple_candidates(self):
        from test_buildingoverlay import baseline,incoming,box,outcomes
        a,b = baseline(osm=None),baseline("other",geometry=box(0.05),osm=None)
        merged = bo.merge([a,b],[incoming(geometry=box(0.025))])
        self.assertTrue(outcomes(merged,"conflict"))
        self.assertEqual(len(merged.buildings),2)
        self.assertEqual(merged.buildings,(a,b) if a.feature_id<b.feature_id else (b,a))

    def test_exact_convex_iou_and_containment_have_rational_evidence(self):
        a = bo._validated(fx.polygon(60,60,10,10))[0]
        b = bo._validated(fx.polygon(65,60,10,10))[0]
        m = buildingspatial.measurements(a,b)
        self.assertTrue(m["available"])
        self.assertAlmostEqual(m["iou"],1/3,places=5)
        self.assertAlmostEqual(m["relative_area"],1,places=5)
        self.assertFalse(m["a_contains_b"])
        self.assertIn("fraction", " ".join(m))

    def test_offset_different_vertex_counts_and_partial_overlap_keep_conservative_rules(self):
        from test_buildingoverlay import baseline,incoming,box,polygon,outcomes
        old = baseline(osm=None)
        for geometry in (box(0.05),polygon([(0,0),(5,-0.02),(10,0),(10,10),(0,10),(0,0)])):
            merged = bo.merge([old],[incoming(geometry=geometry)])
            self.assertFalse(outcomes(merged,"conflict"))
            self.assertEqual(merged.buildings[0].geometry,old.geometry)
        for geometry in (box(0.5),box(4)):
            merged = bo.merge([old],[incoming(geometry=geometry)])
            self.assertTrue(outcomes(merged,"conflict"))
            self.assertEqual(merged.buildings,(old,))

    def test_concave_measurements_are_unavailable_not_approximated(self):
        from test_buildingoverlay import polygon
        a = bo._validated(polygon([(0,0),(10,0),(10,10),(5,10),(5,5),(0,5),(0,0)]))[0]
        result = buildingspatial.measurements(a,a)
        self.assertFalse(result["available"])
        self.assertNotIn("iou",result)


if __name__ == "__main__":
    unittest.main()
