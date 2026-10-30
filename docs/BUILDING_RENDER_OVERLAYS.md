# Offline building renderer integration (Stage 4A)

Stage 4A connects accepted, enabled Stage 2 overlays to the existing server building
planner and mesh writers. It performs no OSM requests, tile invalidation, scheduling,
game installation, protocol changes or live game refresh. Stage 1 and Stage 2 retain
their merge and storage contracts; Stage 3 is unchanged.

## Opt-in and interfaces

`MapData(..., building_overlays=store_or_directory)` pins one validated store snapshot
at construction. `building_overlays=False` explicitly disables integration. When the
argument is omitted, `BNGE_BUILDING_OVERLAY_DIR` supplies the optional directory.
Without configuration, or with a disabled/empty store, the accessor returns the
original baseline layers. No storage directory is created by the default configuration.

For offline callers, with a populated original baseline cache:

```python
import mapcells
from mapdata import MapData
from netfetch import Fetcher

md = MapData("cache", fetcher=Fetcher(offline=True),
             building_overlays="cache/building-overlays")
try:
    keys = mapcells.cells_for_bbox(39.280, -76.600, 39.284, -76.596)
    effective = [md.building_cell(key) for key in keys]
    dependency = md.building_dependencies(keys)
    print(dependency.revision, dependency.geometry_digest)
finally:
    md.close()
```

The configured Overture release must match the saved overlay's baseline release.
Pass the appropriate `release=` when it differs from the project's default.
The store must already be enabled explicitly through `OverlayStore.enable()`.

For a generation process that spawns workers, set the environment variable before
starting it so every process receives the same directory:

```powershell
$env:BNGE_BUILDING_OVERLAY_DIR = (Resolve-Path './cache/building-overlays').Path
```

This prefix deliberately avoids `BNE_`, whose settings the existing global code-key
mechanism hashes. A constructor-only store argument configures that instance; it
does not configure independently spawned workers.

Keep the store stable for the entire offline generation run. Enable/disable,
rollback and edits are observed by a **new** `MapData` instance or newly started
workers. Existing instances neither poll the store nor mutate their pinned view.

## Data boundary and consumers

| Consumer | Data source |
| --- | --- |
| Near `road_tile_items` / `tile_buildings` / planner / mesh / collision | `MapData.building_cell()` |
| Far `far_items` / lite meshes | `MapData.building_cell()` |
| Skyline `sky_items` / skyline meshes | `MapData.building_cell()` |
| Building density/style neighbourhood | Effective buildings, with a separate local revision namespace |
| Roads, width constraints, grading, raster terrain and `MapData.versions()` | Original `MapData.cell()` / baseline versions |
| Road town context, structures, driveway cuts, parking, props and garage sound | Original baseline building plans |

Both `blockworker` and the in-process `earthd` near path split rendered building
items from baseline road-context items. A new OSM building goes through the same
`tile_buildings`, `plan`, `build_parts`, collision and `.cdae` writers as a GERS
building. It does not get new driveway/parking infrastructure in this stage.
Baseline skyline behaviour is retained: baseline parts are not newly enabled for
skyline rendering; accepted overlay parts accompany their parents.

`server/buildingrender.py` adapts immutable `buildingoverlay.Feature` records into
typed `mapgeom.Features`, rather than passing dictionaries into the mesher. IDs,
attributes, rules and untouched geometry rows are preserved. The adapter recomputes
centres/ownership and supplies full, unclipped overlay geometry. A changed geometry's
source columns and a changed height's source reflect their accepted OSM provenance;
the original provenance remains in the copied feature's `overlay_provenance` sidecar.
The sidecar preserves source records, tags, timestamps, attribute sources and parent
links without serializing all source metadata into wide NumPy string columns.
Effective `CellData` includes an OpenStreetMap provenance record and credit line.
Existing raw baseline attribution files remain unchanged; game UI attribution
plumbing has not been added.

## Changed files

| File | Change |
| --- | --- |
| [server/buildingrender.py](../server/buildingrender.py) | New pinned rendering adapter, validation, boundary/part handling, bounded cache and dependency digests. |
| [server/mapdata.py](../server/mapdata.py) | Opt-in construction, rendering accessor, dependency accessor and effective-cell credit wording. |
| [server/buildingoverlaystore.py](../server/buildingoverlaystore.py) | Read-only composition of an already loaded snapshot using existing fingerprint checks; conflicts remain in the result. |
| [server/buildings.py](../server/buildings.py) | Effective near/far/skyline inputs, baseline road-context accessor and separate density statistics. |
| [server/blockworker.py](../server/blockworker.py) | Render/context split and local dependencies in existing building-bearing cache keys. |
| [server/earthd.py](../server/earthd.py) | Matching in-process split and the same geotile content-key calculation as workers. |
| [server/tests/test_buildingrender.py](../server/tests/test_buildingrender.py) | Synthetic integration fixtures exercising actual map cells, planners, native meshes, collision and worker outputs. |
| [server/tests/test_blockworker.py](../server/tests/test_blockworker.py) | Close fixture revisit sidecar descriptors before Windows removes temporary directories. |
| [tools/test_building_overlay.ps1](../tools/test_building_overlay.ps1) | Pin Pillow, constrain TMP/TEMP to the repository and run all regression groups before reporting failures. |
| [docs/BUILDING_OVERLAYS.md](BUILDING_OVERLAYS.md) | Update test dependencies/scope and link the offline integration. |
| [server/README.md](../server/README.md) | Link the opt-in rendering documentation. |
| [docs/BUILDING_RENDER_OVERLAYS.md](BUILDING_RENDER_OVERLAYS.md) | Configuration, contracts, architecture, validation, dependencies and remaining integration work. |

## Conservative validation and boundaries

`OverlayStore.compose()` exposes the existing Stage 2 fingerprint/release checks for
an already loaded snapshot. The renderer uses it without reloading or writing the
store. Refresh callers must have saved the exact `buildingoverlay.from_mapgeom()`
baseline, including missing typed attributes and complete baseline part sets.
Handwritten sparse baseline records may fail the fingerprint check and must be
recreated from the actual cached baseline; no silent migration occurs.

Before projecting an accepted entry, the renderer reads baseline context across its
old/new coverage, deduplicates matching original rows, validates original fingerprints
and checks additional intersecting baseline buildings. Missing context, inconsistent
neighbour copies, baseline ID collisions and newly discovered spatial overlaps fail
closed. This also prevents a stored addition accepted against an incomplete baseline
selection from silently duplicating a real neighbouring baseline building.
Persisted conflicts stay unresolved and never become render records. Stage 1's
unsupported geometry and incomplete-parent/part decisions remain authoritative.

Full overlay polygons are supplied in every intersecting map cell. Overrides remove
the original across the stored old/new coverage, including when moved out of an old
cell. Complete accepted part sets travel with their parent, even when the part's
own centroid is in another cell. Existing ID-based render-tile deduplication and mesh
clipping handle tile edges. Overlay density statistics count the centroid-owner copy
once and cannot reuse baseline statistics. Nearby procedural building styles can
change through the existing 1 km density neighbourhood; their source rows remain
unchanged.

Context lookup is capped at 64 map cells per entry. The effective-cell LRU holds at
most eight cells and 64 MiB of NumPy arrays, in addition to the bounded store snapshot
and existing baseline cache. A cell larger than that byte budget is not retained.
Large stored selections that exceed the lookup cap require smaller refreshes.
Parent/source sidecars are also bounded by the existing Stage 2 input limits.

## Dependencies and cache identities

`MapData.building_dependencies(keys)` returns deterministic `Dependency` fields:

- `revision`: accepted features, attributes, source provenance, parts, baseline
  fingerprints and old/new coverage relevant to the supplied cells.
- `geometry_digest`: footprint geometry, IDs, parts and coverage. Height and other
  attributes are intentionally covered by **revision**, not this footprint digest.
- `feature_ids`: sorted relevant building IDs.

Disabled, empty, conflict-only and unrelated selections return empty dependencies.
Input cell ordering/duplicates do not affect results. A timestamp-only refresh that
does not change accepted records does not create a different rendering dependency.
Baseline version numbers and raw NPZ/mirror files are unchanged.

Existing building-bearing near/geotile, far and skyline revisit keys receive a local
dependency through the existing versions argument. The covered box includes the
1 km density neighbourhood. Bridge-only keys and unrelated areas retain their
original versions. This prevents an offline overlay build from loading a baseline
building artifact. It does not evict caches, update trackers or ask the game to reload.
No new global cache-version bump or overlay environment setting enters the global
code key. The pre-existing source-code hash mechanism still applies to code updates.

## Verification and next stage

Run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/test_building_overlay.ps1`.
The pinned project-local Python environment includes NumPy and Pillow and uses the
existing native binary, without rebuilding Rust. TMP/TEMP are constrained to the
repository. Synthetic integration tests prohibit sockets and exercise actual cached
cell serialization, planning, native meshes, collisions and worker `.cdae` output.
They verify restored byte-identical disabled output and byte-identical road/sidewalk
meshes alongside changed building meshes.

Stage 4A verification: **317 tests executed, 316 passed, one pre-existing failure**.
All 135 Stage 1/2/3 tests and all 23 new renderer integration tests passed. The
existing buildings (74), workers (5), road-width (4) and mapgeom (1) tests passed;
the roads suite passed 74 of 75. `git diff --check` passed.

In this checkout, the extended road regression suite has a pre-existing failure in
`test_roads.PaintRoadsTest.test_bare_verge_beside_roads` (`9 != 0` at the exact painted
edge). The same test fails with unchanged HEAD Python sources under the same pinned
environment/native binary. It has not been weakened, skipped or fixed in Stage 4A;
the runner continues through all suites and reports a nonzero result.

Stage 4B must coordinate a common revision across processes and in-flight work,
selectively invalidate/rebuild affected near/far/skyline outputs (including density
neighbours), and update server trackers/content hashes safely. It must address
existing cached geotile metadata hashes/shape slots before runtime replacement.
This stage does not make the existing game trackers revision-aware. Runtime game
refresh, collision swap ordering and user-facing attribution need their separately
authorized integration work. Simple-polygon and manual conflict-resolution limits
from Stage 1 remain in force.
