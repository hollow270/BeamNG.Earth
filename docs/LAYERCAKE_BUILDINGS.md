# Worldwide OSM-only buildings — BeamNG.drive 0.40.1.0

Building footprints, tags, heights, roofs and parts come exclusively from OpenStreetMap's published Layercake building data. Missing height tags use the existing deterministic visual defaults from the OSM footprint and independent terrain. No Overture building is substituted, spatially matched, deduplicated against OSM or retained in the effective building layer.

Overture roads, water, land use and other unrelated inputs remain. Original road-width/grading/driveway context still reads its original baseline through `MapData.cell()`; it does not feed the rendered building layer. The independent elevation, imagery and vegetation sources are unchanged. OSM-only renderer inputs do not let that infrastructure invent building heights/garage classifications or subtract passages. No road replacement is included.

## Data and session flow

1. The ordinary generation job requests its building dependencies, prioritizing the cells nearest the rendering tile. The internal grid is the existing zoom-14 map-data grid, roughly 2 km cells, subdivided into bounded source queries. Users do not select small rectangles or prepare a city.
2. A successfully cached OSM cell is reused across visits and restarts without checking for a newer source version. If missing and online, a data-only Python child retrieves Layercake ranges, stages one cell, normalizes and validates supported OSM features, then atomically publishes a standalone OSM object and cache index.
3. The worker pins a cell head and whole OSM parent/part identity groups in a shared, durable session ledger before generating meshes. A worker crash reconstructs the same choices. Adjacent cells retain full footprints and deduplicate the same typed identity. There is no blanket exclusion of geometry touching previously chosen neighbours.
4. `MapData.building_cell()` reads those OSM features directly into the original near/far/skyline generator. It never reads baseline buildings, including when the OSM view is empty. A validated empty area renders no buildings. A failed first visit renders known cached OSM footprints if available, otherwise explicitly reports unavailable data; it never substitutes Overture.
5. Already chosen geometry stays fixed for the session. No live activation, collision handoff, cohort scheduling or restoration transaction is involved. Ordinary streaming, tile ownership, physical safety holds and the verified zero-radius building collision guard remain.

A new namespace, `CACHE/osm-buildings/v1/`, prevents importing legacy merged objects with GERS IDs or Overture-derived attributes. The old `osm-primary`, overlays, generation catalogs and evidence are untouched. Only the old Layercake source-configuration marker can be read; its geometry is not migrated. Layercake is the selected public Range backend by default: a new application cache acquires automatically without an enable action. Existing malformed configuration still fails closed. Use --offline or --buildings off to suppress network/content.

The store has checksummed atomic JSON objects, an index with full influence bounds, claims, session identity choices, persisted request reservations and immutable retrieval jobs. A bounded global typed-OSM identity index prevents adjacent first visits from implicitly refreshing cached identities; manual freshness updates those identities explicitly. Small advisory summaries report unavailable/unsupported cells in the existing game status line without repeatedly decoding geometry. The source ETag is pinned before ranges are read, every range proves it, and inconsistent partition identities reject publication. Supported records have typed OSM IDs and exclusively OSM provenance; legacy baseline fingerprints and non-OSM provenance are rejected.

No metadata mutex is held during network requests, DuckDB execution, candidate validation or render conversion. Separate OS leases serialize source requests. Pending claims deduplicate workers. A late result cannot replace an immutable session choice. A successful source object/index is durable before result IPC, so completed work survives an interrupted parent. Failed/unavailable choices are not inserted as successful persistent coverage.

## Deadlines, bounds and failure reports

The acquisition child has distinct bounded phases: up to 45 seconds for source selection/range download/staging; up to 30 seconds for partition processing/normalization; up to 15 seconds for publication/handoff. A generation read has a 90-second aggregate acquisition/queue ceiling across its cells, not 90 seconds per neighbour. Work validated before that ceiling receives up to 15 seconds of bounded publication grace (105 seconds total), rather than losing a valid result at the boundary. Requests still queued behind another owner are deferred for ordinary streaming retry instead of freezing missing geometry; expired claims can be renewed with a new fenced token. Local choice/render work adds CPU/I/O overhead. Native or network stalls terminate only that owned child. A validated result is not thrown away merely because the old single 45-second timer expired.

Persistent successful OSM cache entries are preferred even if an IPC error follows publication. Missing cells retry only in a fresh session; cached cells remain cache-first. Offline use reuses saved OSM, with explicit partial/unavailable diagnostics elsewhere. A dataset publication may lag edits; publication time is not OSM replication time. No promise of instant worldwide freshness is made.

Existing bounds remain: 512 output features per source partition with dense subdivision; one regional staging table, 50,000 rows, 256 MiB DuckDB memory, no disk spill; 96 MiB bounded range/JSON operation safeguards; 50,000 entries per cell/render read; 4,096 cached cells; 100,000 persistent typed identities; 8,192 retained objects/512 MiB; 1,024 retained session ledgers; 2 GiB raw building cache. No evidence or uncertain resource is automatically deleted. Exhaustion reports an error, retains valid OSM references and never enables another building source.

Unsupported holes, disjoint multipolygons, malformed data and uncertain part associations are reported. They produce no substitute building. Source heights/minimum heights above 2,000 m, roof heights above 500 m, or floor/minimum-floor counts above 600 are rejected and reported rather than clamped. The existing generator's minimum footprint area, very low-height filtering and skyline size/vertex limits still apply. Low houses appear through near/far meshes but need not qualify for the skyline. Source coverage is not a claim that every source record was rendered.

## Permission and manual freshness

Layercake documents its published Parquet data and direct HTTP Range access at https://layercake.openstreetmap.us/ . No public Overpass server, account or credential is assumed. All requests retain `netfetch` admission/security controls, application identification, bounded bodies, ETag/If-Match, request spacing and Retry-After persistence. Endpoints are not rotated.

First visits work automatically with buildings on; no enable or preparation command is required. The existing optional launcher actions run before install, process discovery, dialogs or game launch:

```powershell
# Optional explicit source-policy audit marker; not required for first visits:
& $python -B "$app/launcher.py" --cache-dir $cache --osm-buildings-enable --osm-source-policy-confirmed
# Read-only saved status:
& $python -B "$app/launcher.py" --cache-dir $cache --osm-buildings-status
# Explicit freshness, while game/companion are closed:
& $python -B "$app/launcher.py" --cache-dir $cache --osm-buildings-update --osm-source-policy-confirmed --osm-center 30.412876 -9.5524592 --osm-radius-km 3
```

Automatic acquisition needs no city or location argument. Manual freshness uses cell-sized coverage and expands cached copies of connected full-footprint/part identities together, bounded at 256 cells and 100,000 inspected identities. It excludes a running companion with the session lease. Successful cells publish atomically; a partial operation reports errors and retains previous valid OSM where retrieval failed. It never falsely claims all cells were checked. A complete newer OSM-only cell replaces its old contents, including absent buildings; there is no inference about deleting unrelated Overture records, because none are rendered. Rollback restores the pre-update index via `buildingworld.rollback(cache)` while the session lease is free. Updates and rollback affect a new session only.

## Removed from the active path

`buildingworld` no longer calls `buildingregions.candidate`, `buildingprimary.prepare/cached`, `buildingbaseline.collect`, `OverlayStore.compose` or the OSM-versus-Overture merge. The old modules remain for recovery/pure legacy tests, not as rendering fallbacks. Low-level range/Parquet and filesystem primitives are reused. The original map-data, road and terrain versions are unchanged. Effective OSM-only dependency digests, including empty views, exclude old building-bearing mesh caches without a global world cache-version bump.

`launcher.py` routes building status/freshness into the OSM-only workflow. `earthd.py` pins the source-only session before workers/listener construction and disables live controls. Its embedded Python bootstrap and frozen-bundle guard remain. `osmbuildingcache.py` validates/persists source-only objects; `osmbuildingview.py` builds source-only generator arrays. `buildingacquire.py` supervises independent phases and passes small object references rather than whole city payloads. `mapdata.py` separates original non-building consumers from the OSM accessor. `buildings.py` removes the skyline's Overture-cache prerequisite and excludes non-OSM attribute/footprint overrides only for source-only inputs. Tests are in `server/tests/test_osmonly.py`.

Historical integration tests that require merged snapshots, Overture fallback or the removed OSM-primary publication methods are retained with explicit skip reasons (21 cases across `test_buildingprimary`, `test_buildingregions` and `test_buildingacquire`). Their current source-only replacements test immutable choices, claim fencing, source pins, request reservations, four-process stalls/termination, cross-cell groups and ordinary native generation. Reusable Parquet/range/normalization tests still run; the launcher test now mocks the actual OSM-only entry point. These intentional contract retirements are separate from the two previously established unrelated road/protocol test failures.

## Reproducible evidence and one engine acceptance

The recoverable pre-change source archive and all new tests/evidence are under `.venv/restart-osm/osm-only-v1/`. Old worktrees, commits and worldwide-recovery kits/caches remain untouched. This task does not launch Steam or BeamNG.drive, modify the installed game, commit or push.

Pinned November 1 Layercake data contains 1,789 source identities in the main Agadir cell. Five unsupported polygons with holes are omitted; 1,784 supported building entries are stored, with 1,782 intersecting the requested cell. The reference `osm:way:1572086438` is preserved exactly once. The four historical dependency cells produce 20,808 unique OSM buildings across four real workers, all with identical durable choices and no acquisition errors. The generator proof includes the reference plus 20 actual nearby identities around 30.4125652,-9.5545864, each separately generating near, far and collision meshes; this is stronger than successful downloading alone. Two published footprints extend outside their retrieval cell and are excluded from that cell's rendering selection; they are not erased from storage.

The game-free cold-network checks measured Agadir at 15.5 seconds and Paris at 23.9 seconds, both using the same November 1 ETag. The final four-worker real-byte replay measured 43.0–45.4 seconds for all four Agadir cells. Cached restart selection measured about 0.55 seconds with zero source acquisition. These are measured examples, not a loading SLA under game contention. Source records not yet published by that version cannot be inferred from newer OSM edits.

The one isolated engine acceptance kit is `.venv/restart-osm/osm-only-v1/acceptance-kit/`. Its README has exact PowerShell commands, hashes, separate port/profile/cache, source/generator identity diagnostics and the minimal Agadir/Paris/revisit checks. GE Lua must use `FS:getFileRealPath("/")`, never unavailable user/game path helpers. No simulated ACK, mesh hash or static ray proves vehicle collision; actual appearance/contact are still the user's final engine acceptance.
