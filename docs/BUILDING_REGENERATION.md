# Selective building generation preparation (Stage 4B)

The current opt-in UI, asynchronous scheduling, bounded activation and cleanup
are documented in [Stage 5B2: Live OSM buildings](LIVE_OSM_BUILDINGS.md). This
document retains the original Stage 4B preparation and cache contracts.

Stage 4B prepares complete, revision-safe server outputs using saved overlays. It
adds a bounded manual server API, selective output reuse, explicit worker snapshots
and an atomic catalog of prepared generations. The original Overture baseline and
road/terrain inputs retain the Stage 4A contracts.

Prepared generations live in the server cache, separately from session files and
pooled shapes held by the game. Publishing this catalog makes a batch ready for
activation; it does not send messages, reset connection trackers, replace active
collisions or change the game's installation. Those operations belong to Stage 5B.
No protocol, Lua, Rust or OSM retrieval changes are needed in this stage.

## Actual pipeline and cache contracts

The inspected paths are:

- Near legacy: `Server.build_roads_tile` -> `blockworker.roads_tile` ->
  `_tile_parts` -> `buildings.road_context_items` -> planner/mesher ->
  `roads.write_tile`. Buildings, building collision, roads, props and sidewalks
  share an output bundle. The renderer uses overlays; road context stays baseline.
- Near geotiles: `Server.build_roads_geo` -> `blockworker.roads_geo` ->
  `_geo_content` / `_write_geo_content`, plus anchor skin and exact variants.
  Revisit kind `roadsg` stores JSON, meshes and pickled internal skin inputs.
- Distant legacy: `build_far_bld_tile` / `far_buildings_tile`; revisit `bldfar`.
- Skyline legacy: `build_sky_tile` / `skyline_tile`; revisit `bldsky`.
- Distant geotiles: `build_far_geo` / `far_geo`, kinds `bldfar` and `bldsky`.
  Bridge kind `brfar` has baseline dependencies and is not refreshed here.

| State/cache | Stage 4B treatment |
| --- | --- |
| Raw Overture, processed NPZ cells, mmap mirrors and decoded baseline LRU | Retained; `cell()` and baseline version numbers are unchanged. |
| Rendering `BuildingView` | Explicit immutable snapshot per preparation job; fresh bounded view, no startup-view mutation. |
| Building density `_cell_stats` | Existing Stage 4A local dependency namespace retained, including parts/geometry/attributes and ownership. |
| Road widths, grading plans, bridge/terrain stores, `_geo_graders` | Retained on baseline inputs. |
| Revisit ZIPs: `roads`, `roadsg`, `bldfar`, `bldsky` | Existing writers and local building dependency keys reused. Old revisions remain valid under their own keys. |
| Worker `_geo_inputs` | Preparation jobs use `(tile, private output directory)` keys, including when disabling an overlay. Startup generation retains its old key format. |
| Geotile JSON `h` | A local overlay dependency enables hashing actual `.cdae` bytes as well as metadata. Unrelated/disabled output keeps the original hash algorithm. |
| Server `_far_geo_done` | Continues to describe active startup resources. The preparation catalog is separate; Stage 5 must activate replacements. |
| `roads.Tracker`, `roads.GeoTracker`, far/skyline `vegetation.Tracker` | Retain their active `sent`, `empty`, `futs`, skin/exact and placement state until Stage 5. |
| `matslots` holds, pooled file paths and serials | Retained. Preparation does not overwrite, release or reuse any active slot. |

The existing trackers fence anchors, but do not universally fence same-tile overlay
revisions. `GeoTracker._built`, for example, can process a completed content result
even after its future is no longer the current future for that tile. The normal
writers also use fixed tile filenames. Writing refresh jobs into those paths would
permit late jobs and held-resource overwrites. Private generations and a separate
publication point avoid that failure without prematurely adding live replacement.

## Snapshot and publication architecture

```mermaid
flowchart LR
    Store[Saved OverlayStore] --> Snapshot[Copied immutable job snapshot]
    Snapshot --> Plan[Local dependencies and registered tiles]
    Plan --> Worker[Existing road / far worker pools]
    Worker --> Private[Private work directory and receipt]
    Private --> Fence[Sequence and local-revision checks]
    Fence --> Catalog[Atomic prepared catalog]
    Catalog -. Stage 5 activation .-> Active[Session resources and connection trackers]
```

`MapData.building_snapshot(snapshot)` scopes a copied `BuildingView` with a
`ContextVar`. Existing `MapData` objects and already running worker processes can
therefore consume the supplied revision without being restarted. The context is
reset even on exceptions. Other threads, baseline `cell()` calls and the instance's
startup rendering snapshot remain unchanged. Baseline-only warm threads need no
overlay context propagation.

`buildingrefresh.generate` is the process-pool entry. It verifies the requested
input identity, runs one existing tile writer under that context, verifies inputs
again, and returns a receipt containing:

- The exact tile request/settings, baseline release and consumed store snapshot ID.
- The local effective-building dependency and complete input/cache identity.
- Exact file sizes and SHA-256 hashes, a VFS-independent content digest, and a
  separate digest of mesh bytes.
- The existing builder's return value, serialized as bounded JSON-compatible data.

The coordinator persists registered requests and the last complete catalog. On a
refresh it reloads the saved overlay once, compares each registered tile's local
input identity, verifies existing files, and builds only changed/missing outputs.
It uses `blockworker.building_cells` for the same 1 km density/context neighbourhood
as the rendering cache keys. Relevant overlay entries include old/new footprint
coverage and complete part sets. Changed-cell reporting is scoped to registered
dependencies, so unrelated edits elsewhere in a large store do not become a global
refresh or exhaust a local request's cell budget.

Each batch reserves a monotonically increasing catalog sequence under the existing
Stage 2 cross-process file-lock primitive. This is a publication fence, not a cache
version: a newer request supersedes older batches without changing unaffected tile
identities. Concurrent requests coalesce through the retained bounded registry;
the latest batch prepares the currently required outputs. Already published,
unchanged rows keep their exact generation directories and files.

Before publication the coordinator locks **catalog, then overlay store**, checks
the sequence, reloads the accepted overlay while retaining its writer lock, and
rechecks every requested local dependency. The writer lock remains held through
publication, preventing an accepted revision from slipping between the freshness
check and the catalog swap. Each ready directory is renamed to a unique generation
directory after fsync of its files, then one checksummed JSON catalog is atomically
replaced with fsync (directory fsync also applies on platforms that support it).
Every changed view in the batch becomes available through that one catalog update.

An older job cannot overwrite a newer directory: generation names are never reused.
An older batch cannot replace a newer catalog: its sequence fails. A store edit
without a second refresh also makes an affected pending batch stale. Unrelated
global store-history changes allow reuse because local input identities stay equal.
Reused rows may record an earlier global snapshot ID; their local effective data
and settings are identical to the requested state. Global head IDs are audit fields.

## Manual interface and scope

Configure `MapData` using Stage 4A's explicit store argument or
`BNGE_BUILDING_OVERLAY_DIR`, and enable accepted overlays through `OverlayStore`.
First register a bounded set of outputs; later calls recompute dependencies for that
set automatically:

```python
from buildingrefresh import TileRequest

# Example legacy tasks for an existing anchor. Supply the actual server settings.
requests = [
    TileRequest("near", tx, ty, aid=anchor_id, lat=lat, lon=lon, zoom=zoom,
                options={"sidewalks": True, "lift": 0.0, "q": height_quantum,
                         "left_hand": False, "mph": True}),
    TileRequest("far", tx, ty, aid=anchor_id, lat=lat, lon=lon, zoom=zoom),
    TileRequest("skyline", sx, sy, aid=anchor_id, lat=lat, lon=lon, zoom=zoom),
]
report = server.refresh_building_overlays(requests)

# After a saved edit, disable, enable or rollback:
report = server.refresh_building_overlays()
# report: snapshot, rebuilt slots, reused slots, affected_cells, publication
prepared = server._building_refresh.outputs()
```

Supported kinds are `near`, `far`, `skyline`, `near_geo`, `far_geo` and
`skyline_geo`. Geotile x/y must use `geotiles.Z`, `far_geo_zoom("bldfar")`, or
`far_geo_zoom("bldsky")` respectively. Near geotile defaults prepare content, skin
and automatic exact variants together. `skin=False, exact=None` explicitly prepares
content only. Settings are validated and included in the identity. A caller must
derive them from the actual server/anchor (`geo_rules`, `anchor_q`, road lift,
driving side, units, lamps, country and gas-station setting); the API does not infer
a live selection or read connection trackers.

The first call requires at least one target. Register every relevant near/far/skyline
output, including density neighbours whose prepared replacements will be needed.
The coordinator checks all retained targets, including those outside a newly edited
area, and reuses the unchanged ones. It deliberately does not scan every cached
world tile or automatically refresh while driving. Stage 5A's
[manual workflow](MANUAL_OSM_BUILDINGS.md) now discovers targets from active
anchors, legacy/geotile trackers and this retained registry using actual settings.
It forwards `expected_state` to fence the exact accepted store state at planning
and publication. `replace_requests=True` retires obsolete registry descriptors
only with a successful complete catalog publication; their files are retained.

For tests/tools, `Coordinator(directory, md, store, submit)` accepts a submitter
returning a `Future`; it uses no executor of its own. `Server` routes near tasks to
its existing road pool and far/skyline tasks to its existing far-building pool.
Missing pools produce an explicit error. Existing timeout/recreation support remains
in those pools; new jobs always receive an explicit snapshot after recreation.

## Persistence, failures and limits

The default server path is `<cache_dir>/building-generations/`:

```text
catalog.json                 checked JSON, atomic publication and request registry
writer.lock                  shared catalog sequence/publication lock
work/<uuid>/                 one running or completed private job
generations/<uuid>/          immutable prepared files referenced by catalog rows
```

The metadata layer reuses `OverlayStore`'s lock, safe JSON envelope/reader and atomic
write primitives through a small private adapter. It does not modify the overlay
store's manifests or use pickle for new persistent metadata. Existing trusted
internal geotile skin-input pickle files remain inside their generation bundles.
Paths, directory moves, cleanup targets and flat file manifests are checked;
symlink/reparse-point generation directories are rejected.

Default bounds: 128 registered requests, 512 changed cells, two concurrent refresh
callers, two outstanding worker jobs, 512 retained generations, 128 files/job,
64 MiB/file, 128 MiB/job, 512 MiB retained/work file bytes and a 120 s job deadline.
Catalog JSON is limited to 4 MiB. The original overlay storage limits still apply.
Budgets can be explicitly configured for direct `Coordinator` callers. A request
iterator is consumed only to its bound. No new worker processes or background pools
are created. Each active job also has the existing bounded rendering-view cache;
it is released with that context. Global density and skin-input memo limits remain.

Cancellation is cooperative at the coordinator. A queued job can be cancelled; a
running job retains its private directory and its concurrency permit until its
future actually finishes. Its abandoned files are then removed. Errors, timeouts,
receipt mismatches and incomplete baseline data leave the previous prepared entries
available. Partial batches are never published. Callers retry explicitly.

A crash before the final catalog swap leaves the prior catalog valid. A crash or
uncertain fsync after directory moves may leave unreferenced generation directories;
they are retained and counted against quotas. Published files are never deleted or
overwritten here. Missing/changed prepared files cause a rebuild; malformed catalog
metadata fails closed. Missing metadata alongside retained generations requires
explicit recovery rather than guessing. Automatic orphan/history collection and
physical collection of retired view targets require a lifecycle policy before
long-running Stage 5B use. Stage 5A can retire registry descriptors without deleting
files; the temporary old/new registry union must still fit the existing quota.
Capacity exhaustion rejects work while preserving existing output.

## Verification and Stage 5 prerequisites

Run the project-local pinned environment:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/test_building_overlay.ps1
```

The added deterministic tests cover actual native meshes, all six writer paths,
cache hits after recreation, a spawned/recreated worker, local/unrelated/part-only
changes, disable/enable/rollback, immutable road/terrain inputs, stale completions,
concurrent coordinators, cancellation, retained worker capacity after timeout,
worker/partial-batch failure, wrong-revision receipts, catalog write failure and the
store-writer/publication lock boundary. One real concave-footprint edit retains
identical JSON metadata (excluding `h`) while changing mesh bytes and `h`.
Sockets are prohibited in the fixtures and child worker. TMP/TEMP and artifacts stay
in the repository. No test failure is converted into a skip.

Actual final runner result: **358 tests, 356 passed, one known failure, one existing
skip**. All 135 Stage 1/2/3 tests, all 23 Stage 4A tests and all 23 new Stage 4B tests
passed. Existing geotile tests passed 13 of 14 with the Lua prerequisite skip;
process-pool (4), buildings (74), workers (5), road-width (4) and mapgeom (1) passed.
Roads passed 74 of 75. The runner correctly exits nonzero for that failure.
`git diff --check` passed. Full results are retained locally in
`.venv/overlay-tests/stage4b-regression.log`; the independent HEAD-source reproduction
is in `.venv/overlay-tests/stage4b-head-baseline.log`.

The known baseline failure remains
`test_roads.PaintRoadsTest.test_bare_verge_beside_roads`: `9 != 0` at the exact
painted edge. It was reproduced with unchanged HEAD Python sources, the same pinned
runtime and existing native binary. The pre-existing
`test_geotiles.LuaAgreementTest.test_agrees` skips when `luajit` is unavailable.
No road logic, Rust or Lua was changed to alter these outcomes.

Stage 5 must validate the chosen prepared catalog against current local dependencies,
resolve `@BUILDING_OVERLAY@/<uuid>` paths into immutable session/pool resources,
coordinate visual and collision activation, fence tracker callbacks by generation,
update active far/near/skyline memo entries selectively, and manage acknowledgements,
held slots and retirement. Stage 5A discovers active dependent targets with actual
server settings; Stage 5B must provide the manual geographic UI/action and carry source
attribution into the active experience. Prepared collision meshes are coherent with
their visual generation, but no live collision swap is attempted in Stage 4B.

## Changed files

The later [Stage 5B1 implementation](BUILDING_ACTIVATION.md) activates explicitly
selected prepared catalogs through existing slots and governed building-only
handoffs. It documents the observable collision criterion, safe future-LOD
deferral, recovery limits and remaining Stage 5B2 work.

| File | Change |
| --- | --- |
| [buildingrefresh.py](../server/buildingrefresh.py) | Requests, deterministic inputs, worker receipt, bounded orchestration and atomic prepared catalog. |
| [buildingrender.py](../server/buildingrender.py) | Construct a view from a supplied copied snapshot as well as a saved store. |
| [mapdata.py](../server/mapdata.py) | Retain the configured store; scope explicit snapshots per job with thread-local context. |
| [blockworker.py](../server/blockworker.py) | Shared dependency-cell calculation, mesh-aware local content hashes and generation-scoped skin-input memo keys. |
| [earthd.py](../server/earthd.py) | Bounded manual server method using existing worker pools and a separate cache catalog. |
| [test_buildingrefresh.py](../server/tests/test_buildingrefresh.py) | Real and controlled-concurrency integration tests, including worker process recreation. |
| [test_building_overlay.ps1](../tools/test_building_overlay.ps1) | Include Stage 4B, geotile and process-pool regression suites; keep nonzero failure reporting. |
| [BUILDING_RENDER_OVERLAYS.md](BUILDING_RENDER_OVERLAYS.md), [BUILDING_OVERLAYS.md](BUILDING_OVERLAYS.md), [server README](../server/README.md) | Link Stage 4B and update test/interface documentation. |
| This document | Architecture, API, cache inventory, failure behaviour and Stage 5 boundary. |
