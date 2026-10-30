# Controlled OSM building retrieval (Stage 3)

`server/osmbuildings.py` provides an opt-in manual Overpass JSON client, a bounded
POST transport and a pure parser. It returns `buildingoverlay.Feature` objects for
the existing merge/store APIs. It has no driving hooks, UI, Lua, tile regeneration,
cache invalidation, automatic persistence or background jobs. No live API requests
were used to develop or test this stage.

## Configuration and caller example

There is no default backend. Supply a self-hosted or explicitly permitted
Overpass-compatible endpoint and enable the component. Configuration is passed in
memory; the module does not read or write configuration files. This example reads
credentials from the caller's environment. It was not executed during testing:

```python
import os
import netfetch
from osmbuildings import Client, Config

headers = {}
if os.environ.get("BNE_OSM_BUILDINGS_TOKEN"):
    headers["Authorization"] = "Bearer " + os.environ["BNE_OSM_BUILDINGS_TOKEN"]

config = Config(
    enabled=True,
    endpoint=os.environ["BNE_OSM_BUILDINGS_ENDPOINT"],
    headers=headers,
)
# Future integration should pass the main server's existing Fetcher instead.
client = Client(config, fetcher=netfetch.default_fetcher())
snapshot = client.fetch(
    (-76.595, 39.282, -76.590, 39.286),  # west, south, east, north; about 430 x 445 m
    manual=True,
    known_ids=(),                      # relevant prior OSM way/relation identities
    not_before=None,                   # prior source timestamp for this area, when known
)
print(snapshot.replication_timestamp, snapshot.empty, snapshot.issues)

# Separate, explicit storage action after checking the snapshot:
# info = snapshot.refresh_info(baseline_release)
# commit = overlay_store.refresh(
#     original_baseline, snapshot.buildings, baseline_parts=original_parts,
#     incoming_parts=snapshot.parts, info=info,
# )
```

Endpoint and credential fields are omitted from `Config`'s repr and never included
in returned provenance. Put secrets in credential headers, not URL paths, user-info,
query strings or committed files. HTTPS is required by default. Insecure HTTP needs
`allow_insecure_http=True` and cannot carry custom headers. User-info, query/fragment
parameters, trailing-dot hosts, control characters and header injection are rejected.
Framing/identity headers cannot be overridden. Redirects are not followed and
credentials are never forwarded to another endpoint.

## Network policy and resource controls

Every POST runs inside `netfetch.Fetcher.session(kind="demand")`, preserving the
offline flag, host deny list, token buckets, concurrency controls, daily budgets
and circuit breaker accounting. `netfetch.py` and its policy table are unchanged.
The existing denied public Overpass host stays denied. A configured endpoint must
already be permitted by the supplied fetcher; no scoped or global deny override is
introduced. Its socket timeout also limits requests if stricter than configuration.

The application sends `netfetch.USER_AGENT` and form-encoded POST queries. The query
sets server timeout/memory limits, obtains uncropped dependencies, and requests a
terminal `out count` completion marker. `Accept-Encoding: identity` is sent;
compressed responses are rejected rather than decompressed without a bound.

| Control | Default |
| --- | --- |
| Selection width and height | approximately 500 m each (505 m tolerance) |
| Concurrent manual requests | one per shared process coordinator |
| Minimum start-to-start spacing | 60 seconds |
| Same/overlapping-area cooldown | 600 seconds, including failed attempts |
| Socket timeout | 30 seconds or stricter endpoint policy |
| Body/operation deadline | 45 seconds, checked between operations/reads |
| Server query timeout/memory | 25 seconds, reduced for stricter socket policy; 64 MiB |
| Response body | 8 MiB |
| Parsed OSM elements | 20,000 |
| Geometry references / expanded output vertices | 50,000 each |
| Buildings plus parts | 512 |
| Explicit known identities | 64 |
| Maximum source age | one hour |

The transport checks Content-Length before reading and limits each body read to
remaining capacity plus one detection byte. Unknown-length/chunked bodies stop at
the same bound. Premature EOF, invalid lengths and deadline expiry discard the
response. Responses/connections close on success and failure. Error statuses are
handled from headers without downloading large error pages.

`HTTPFailure.code` exposes status. `Throttled.retry_after` indicates when another
explicit attempt may be useful. HTTP 429/503 Retry-After, including HTTP dates,
pauses subsequent manual retrieval. The coordinator honours the full value even
beyond netfetch's existing 15-minute breaker clamp. There are no automatic retries,
source rotations, query splitting or rate-limit evasions. Errors never return a
partial snapshot.

The default coordinator is shared within one process and tracks at most 256 active
cooldown areas and 32 timestamp-bearing hosts. It fails closed at those limits
rather than forgetting cooldowns. Custom coordinators are injectable for tests;
production must use a single main-server owner instead of independent worker
clients. Cooldowns are in memory, not durable across process restarts. Durable,
cross-process coordination is a remaining integration requirement. Standard Python
socket timeouts cannot preempt every OS DNS resolution or slow header parse; the
total deadline is checked after these operations and throughout body streaming.
Endpoint operators' policies still determine suitable configured limits.

## Parsing and geometry

`parse(body, bounds, ...)` makes no network or filesystem calls. It checks JSON
schema, duplicate keys/OSM identities, finite coordinates, versions/timestamps,
terminal counts, every referenced node/way/relation, and resource limits. A missing
dependency or any Overpass `remark` rejects the whole response. Valid zero-count
output is distinguishable from missing output, truncation and runtime errors.

Closed building ways become Polygon features. Multipolygon relations are assembled
by node identity with deterministic stitching and explicit outer/inner roles.
Constituent outer ways are not emitted again as duplicate buildings. Holes and
multiple outer rings retain their geometry with diagnostics; the unchanged Stage 1
merge rejects them. Open/branching/unusable relation topology becomes unsupported
geometry, preserving member identities in provenance. Nested relation geometry is
not flattened.

The query includes parts and `type=building` associations, including parents of
selected ways/relations. Referenced dependencies outside the selection arrive
whole. Explicit outline/part associations preserve parent links, provenance and
part completeness. Containment can associate a part with a unique parent but cannot
prove the entire part set was fetched; those sets remain incomplete. Orphans,
conflicting associations and unsupported shapes are reported.
An unsupported orphan part also marks overlapping potential parents as having an
uncertain part set, preventing a speculative plain-footprint replacement.

Normalized features preserve OSM identity, element version/timestamp, replication
timestamp, latest dependency geometry timestamp, tags, way node IDs, relation
members and ODbL attribution. Editor usernames, UIDs and changeset fields are not
copied. Attributes include metric/feet/feet-and-inches heights, floor counts, minimum
height/level, roof height/shape/direction/orientation/material, facade material,
name, class and hex colours. Ambiguous values such as `height=12;14` are omitted
with an issue. Raw tags remain available; existing valid Overture attributes are
not erased. Named colours and free-form measurements are not normalized yet.

## Freshness, persistence and remaining integration

`osm3s.timestamp_osm_base` supplies the source replication time. It is compared with
`not_before`, prior endpoint observations and the age limit. Stale snapshots are
marked and `refresh_info()` refuses them. Missing replication timestamps are
reported and cannot become persistence metadata. Local fetch time is never used
as a substitute source timestamp.

Missing known IDs produce diagnostics, never deletion records. Returned known IDs
without building tags become retagging conflicts for Stage 1. A bbox query cannot
guarantee discovery of a polygon enclosing the selection with no vertices inside.
Future callers must supply relevant known identities and neighbouring original
baseline geometry. Absence never proves demolition, deletion or a complete inventory.

Production integration still needs endpoint provisioning/policy review, durable
request coordination, baseline/known-ID selection, user-visible diagnostics and
explicit storage decisions. Existing Stage 1/2 limits and migration rules apply.
Live endpoint compatibility was not tested here. Fetching is not connected to
driving, tile generation or game UI.

Protocol references: the official Overpass manual covers [complete geometry
dependencies](https://dev.overpass-api.de/overpass-doc/en/full_data/osm_types.html),
[selection versus cropped output](https://dev.overpass-api.de/overpass-doc/en/full_data/bbox.html)
and [metadata output](https://dev.overpass-api.de/overpass-doc/en/targets/formats.html).

## Tests

Use the existing project-local environment:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/test_building_overlay.ps1
```

The runner executes Stage 1, Stage 2, retrieval and mapgeom regressions. Retrieval
tests block socket connection creation and use synthetic Overpass data, fake clocks,
fake HTTP connections and mocked responses. No live OSM requests or game operations
are made. Temporary storage fixtures stay inside the repository.
