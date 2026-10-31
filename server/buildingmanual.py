"""Explicit fetch -> review -> accept -> discover -> prepare. Never activates game resources.

Preview capabilities are bounded and process-local. Accepted workflow markers are
durable inside OverlayStore refresh metadata; preparation is a separate durable step.
"""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import threading
import time
from urllib.parse import urlsplit

import buildingbaseline
import buildingoverlay as bo
from buildingoverlaystore import StalePreview, ResourceLimit, _fingerprint, _intersects
import buildingspatial
import buildingtargets
import netfetch
import osmbuildings as osm


KIND = "manual-osm-v1"


def _hash(value):
    return hashlib.sha256(bo._json(value).encode()).hexdigest()


class Busy(RuntimeError):
    pass


class Workflow:
    def __init__(self, server, client=None, *, preview_ttl=300.0, max_previews=4, max_preview_bytes=64*1024*1024):
        if not 0 < preview_ttl <= 3600 or type(max_previews) is not int or not 0 < max_previews <= 16 or \
                type(max_preview_bytes) is not int or not 0 < max_preview_bytes <= 128*1024*1024:
            raise ValueError("invalid manual preview resource limits")
        self.server, self.md, self.store = server, server.mapdata, server.mapdata._building_store
        if self.store is None or not server.buildings:
            raise osm.ConfigurationError("building overlays must be explicitly configured")
        self.client = client
        self.wall = client.wall if client is not None else time.time
        self.ttl, self.max_previews, self.max_bytes = preview_ttl,max_previews,max_preview_bytes
        self.previews, self.lock = OrderedDict(),threading.Lock()
        self.retired = False
        self.discovery_context = ContextVar("manual_building_discovery",default=None)

    def _discover(self, bounds):
        world=self.discovery_context.get() or self.server
        return buildingtargets.discover(world,bounds,current_only=bool(getattr(world,"building_current_targets",False)))

    @contextmanager
    def _operation(self, manual):
        if manual is not True:
            raise osm.ConfigurationError("manual=True is required")
        if not self.lock.acquire(blocking=False):
            raise Busy("another manual building operation is in progress")
        try:
            if self.retired:
                raise StalePreview("manual client was reconfigured; obtain a new preview")
            now = self.wall()
            for key in list(self.previews):
                if not 0 <= now-self.previews[key]["created"] <= self.ttl:
                    del self.previews[key]
            yield
        finally:
            self.lock.release()

    def _config(self, *, private=False):
        if self.client is None or not self.client.config.enabled or self.client.config.endpoint is None:
            raise osm.ConfigurationError("configure an enabled OSM client and an explicit permitted endpoint first")
        config = asdict(self.client.config)
        if not private:
            config["headers"] = sorted(config["headers"])  # credentials never enter tokens/reports/persistence
        return _hash(config)

    def _fresh(self, source):
        source.refresh_info(self.md.release)  # checks missing replication time and parser's stale flag
        age = self.wall()-osm._time(source.replication_timestamp).timestamp()
        if age < -300 or age > self.client.config.max_snapshot_age_s:
            raise osm.StaleSnapshot("reviewed OSM replication timestamp is no longer fresh")
        host = urlsplit(self.client.config.endpoint).hostname
        latest = self.client.coordinator.latest.get(host)
        if latest is not None and osm._time(latest) > osm._time(source.replication_timestamp):
            raise StalePreview("a newer OSM snapshot was observed; obtain a new preview")

    def _context(self, bounds, snapshot, incoming=(), parts=()):
        return buildingbaseline.collect(self.md,bounds,snapshot,incoming,parts,
                                        max_features=self.store.limits.max_input_features)

    def _diagnostics(self, effective, source):
        rows = []
        for incoming in source.buildings:
            for original in effective.buildings:
                a,b = bo._bbox(incoming.geometry),bo._bbox(original.geometry)
                if a is not None and b is not None and not _intersects(a,b) and \
                        not set(bo._identities(incoming)).intersection(bo._identities(original)):
                    continue
                if len(rows) >= 128:
                    raise ResourceLimit("spatial diagnostic pair budget exceeded")
                rows.append(dict(incoming_id=incoming.feature_id,baseline_id=original.feature_id,
                    incoming_osm_ids=list(bo._identities(incoming)),baseline_osm_ids=list(bo._identities(original)),
                    measurements=buildingspatial.measurements(bo._validated(incoming.geometry)[0],
                                                               bo._validated(original.geometry)[0])))
        return rows

    @staticmethod
    def _changes(before, result):
        originals = {f.feature_id:f for f in before.buildings}
        effective = {f.feature_id:f for f in result.buildings}
        rows = []
        for d in result.decisions:
            if d.subject!="building" or d.incoming_id is None or d.action=="conflict":
                continue
            fid = d.baseline_ids[0] if d.baseline_ids else bo.normalize_osm_id(d.incoming_id)
            after,old = effective[fid],originals.get(fid)
            attrs = old.attrs if old is not None else {}
            changed = {k:dict(before=attrs.get(k),after=after.attrs.get(k)) for k in sorted(set(attrs)|set(after.attrs))
                       if bo._json(attrs.get(k))!=bo._json(after.attrs.get(k))}
            rows.append(dict(feature_id=fid,incoming_id=d.incoming_id,action=d.action,
                geometry_before=old.geometry if old else None,geometry_after=after.geometry,attributes_changed=changed,
                parts_before=[asdict(p) for p in before.parts if p.parent_id==fid],
                parts_after=[asdict(p) for p in result.parts if p.parent_id==fid],sources_after=list(after.sources)))
        return rows

    def preview(self, bounds, *, manual=False, enable=False):
        with self._operation(manual):
            if type(enable) is not bool:
                raise ValueError("enable must be boolean")
            config = self._config()
            private_config = self._config(private=True)
            if self.client.fetcher.offline_flag:
                raise netfetch.Offline("manual OSM retrieval is unavailable offline")
            host = urlsplit(self.client.config.endpoint).hostname
            if self.client.fetcher.policy(host).denied:
                raise netfetch.PolicyDenied("configured OSM host is denied by netfetch")
            bounds = osm._area(bounds)
            before,state = self.store.load_state()
            initial = self._context(bounds,before)
            records = initial.buildings+initial.parts+tuple(
                f for e in before.entries if _intersects(e.bounds,initial.bounds) for f in (e.feature,)+e.parts)
            known = tuple(sorted({identity for f in records for identity in bo._identities(f)}))
            times = [r["info"]["source_timestamp"] for r in before.refreshes if _intersects(r["info"]["bounds"],bounds)]
            cutoff = max(times,key=osm._time) if times else None
            source = deepcopy(self.client.fetch(bounds,manual=True,known_ids=known,not_before=cutoff))
            if private_config != self._config(private=True):
                raise StalePreview("retrieval configuration changed during fetch")
            initial.validate(self.md)
            self._fresh(source)
            context = self._context(bounds,before,source.buildings,source.parts)
            token = _hash([KIND,state,config,context.digest(),context.files,asdict(source),enable])
            metadata = {"manual_refresh":dict(kind=KIND,token=token,requested_bounds=list(bounds),
                         endpoint_host=host,baseline_digest=context.digest())}
            info = replace(source.refresh_info(self.md.release,metadata=metadata),bounds=context.bounds)
            commit = self.store.refresh(context.buildings,source.buildings,baseline_parts=context.parts,
                incoming_parts=source.parts,info=info,dry_run=True,expected_state=state,enable=enable,
                validate=lambda:context.validate(self.md))
            referenced = {fid for d in commit.merge_result.decisions if d.incoming_id is not None for fid in d.baseline_ids}
            overture_only = [f.feature_id for f in context.buildings if f.feature_id not in referenced
                             and _intersects(bo._bbox(f.geometry) or bounds,bounds)]
            effective_before = self.store.compose(replace(before,enabled=True),context.buildings,
                baseline_parts=context.parts,baseline_release=self.md.release,bounds=context.bounds)
            report = dict(status="preview",token=token,selection=list(bounds),context_bounds=list(context.bounds),
                overlay_revision=before.revision,enabled_before=before.enabled,enabled_on_accept=before.enabled or enable,
                replication_timestamp=source.replication_timestamp,known_ids=list(known),
                issues=[asdict(i) for i in source.issues],merge=commit.merge_result.report(),
                overture_only=sorted(overture_only),omission_is_deletion=False,
                overture_only_details=[dict(feature=asdict(f),baseline_fingerprint=_fingerprint(f,
                    tuple(p for p in context.parts if p.parent_id==f.feature_id)))
                    for f in context.buildings if f.feature_id in overture_only],
                changes=self._changes(effective_before,commit.merge_result),
                incoming_buildings=[asdict(f) for f in source.buildings],incoming_parts=[asdict(f) for f in source.parts],
                spatial_diagnostics=self._diagnostics(effective_before,source),game_activated=False)
            try:
                report["generation_preview"] = self._discover(context.bounds).report()
            except Exception as e:
                report["generation_preview"] = dict(status="unavailable",error=self._error(e))
            report["generation_preview"]["advisory"] = True  # rediscovered from current server settings after acceptance
            row = dict(context=context,source=source,info=info,state=state,enable=enable,
                       expected=(commit.merge_result.digest(),commit.merge_result.report_json()),
                       config=private_config,created=self.wall())
            row["size"] = len(bo._json([asdict(source),[asdict(f) for f in context.buildings+context.parts],report]).encode())
            if row["size"] > self.max_bytes:
                raise ResourceLimit("manual preview exceeds memory budget")
            self.previews.pop(token,None)
            while self.previews and (len(self.previews)>=self.max_previews or
                    sum(p["size"] for p in self.previews.values())+row["size"] > self.max_bytes):
                self.previews.popitem(last=False)
            self.previews[token] = row
            return report

    @staticmethod
    def _error(error):
        return dict(type=type(error).__name__,message=str(error))

    def _prepare(self, bounds, expected_state):
        try:
            snapshot,state = self.store.load_state()
            if state != expected_state:
                raise StalePreview("accepted state changed before target discovery/preparation")
            context = self._context(bounds,snapshot)
            self.store.compose(snapshot,context.buildings,baseline_parts=context.parts,
                               baseline_release=self.md.release,bounds=context.bounds)
            targets = self._discover(context.bounds)
            if targets.status!="discovered":
                return dict(status=targets.status,discovery=targets.report(),prepared=False)
            result = self.server.refresh_building_overlays(targets.requests,expected_state=state,replace_requests=True)
            return dict(status="prepared" if snapshot.enabled else "prepared_baseline",prepared=True,
                        discovery=targets.report(),result=result)
        except Exception as e:
            return dict(status="pending",prepared=False,error=self._error(e))

    def validate_preview(self, token, *, manual=False):
        """Recheck a retained review without fetching, consuming it or committing data.

        Acceptance still performs its full validation under the writer lock.
        """
        with self._operation(manual):
            row = self.previews.get(token)
            if row is None or row["config"] != self._config(private=True):
                raise StalePreview("preview expired or retrieval configuration changed")
            if self.store.load_state()[1] != row["state"]:
                raise StalePreview("accepted state changed after preview")
            self._fresh(row["source"])
            row["context"].validate(self.md)

    def accept(self, token, *, manual=False, prepare=True):
        with self._operation(manual):
            if not isinstance(token,str) or len(token)!=64:
                raise StalePreview("invalid preview token")
            row = self.previews.get(token)
            if row is None:
                raise StalePreview("preview unknown/expired or server restarted; obtain a new preview")
            try:
                configured = self._config(private=True)
            except osm.ConfigurationError:
                configured = None
            if row["config"] != configured:
                self.previews.pop(token,None)
                raise StalePreview("retrieval configuration changed; obtain a new preview")
            # Keep observed replication freshness stable across commit. No network or
            # catalog lock is acquired while holding the overlay writer lock.
            if not self.client.coordinator.lock.acquire(blocking=False):
                raise Busy("OSM retrieval is in flight; retry explicit acceptance later")
            try:
                def validate():
                    if row["config"] != self._config(private=True):
                        raise StalePreview("retrieval configuration changed during acceptance")
                    self._fresh(row["source"])
                    row["context"].validate(self.md)
                commit = self.store.refresh(row["context"].buildings,row["source"].buildings,
                    baseline_parts=row["context"].parts,incoming_parts=row["source"].parts,info=row["info"],
                    expected_state=row["state"],expected_merge=row["expected"],validate=validate,enable=row["enable"])
            except (StalePreview,osm.StaleSnapshot):
                self.previews.pop(token,None)
                raise
            except OSError as e:
                # A manifest replacement followed by fsync failure has an uncertain
                # return value, not an atomic rollback. Durable metadata is the retry authority.
                self.previews.pop(token,None)
                return dict(status="acceptance_not_confirmed",workflow_token=token,error=self._error(e),
                            generation=dict(status="not_attempted",prepared=False),game_activated=False)
            finally:
                self.client.coordinator.lock.release()
            self.previews.pop(token,None)
            generation = self._prepare(row["info"].bounds,commit.state_token) if prepare else dict(status="not_attempted",prepared=False)
            return dict(status="accepted",workflow_token=token,accepted_revision=commit.revision,
                        accepted_state=commit.state_token,merge=commit.merge_result.report(),
                        generation=generation,game_activated=False)

    @staticmethod
    def _accepted(snapshot):
        rows = []
        for r in snapshot.refreshes:
            marker = r["info"].get("metadata",{}).get("manual_refresh",{})
            if marker.get("kind")==KIND:
                rows.append(dict(token=marker["token"],selection=marker["requested_bounds"],
                                 context_bounds=r["info"]["bounds"],source_timestamp=r["info"]["source_timestamp"]))
        return sorted(rows,key=lambda r:r["token"])

    def accepted(self):
        snapshot,_ = self.store.load_state()
        return self._accepted(snapshot)

    def retry(self, token=None, *, manual=False, prepare=True):
        with self._operation(manual):
            snapshot,state = self.store.load_state()
            operations = self._accepted(snapshot)
            if not prepare:
                if token is not None and not any(r["token"]==token for r in operations):
                    raise StalePreview("workflow is not present in current accepted history")
                return dict(status="retry",workflow_token=token,preparing_revision=snapshot.revision,
                            accepted_workflows=operations,game_activated=False)
            if token is None:
                if len(operations)!=1:
                    raise ValueError("specify an accepted workflow token (use accepted() to list them)")
                token = operations[0]["token"]
            operation = next((r for r in operations if r["token"]==token),None)
            if operation is None:
                raise StalePreview("workflow is not present in current accepted history; it may have been rolled back")
            return dict(status="retry",workflow_token=token,preparing_revision=snapshot.revision,
                        generation=self._prepare(operation["context_bounds"],state),game_activated=False)
