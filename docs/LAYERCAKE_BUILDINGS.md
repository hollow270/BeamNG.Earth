# Worldwide OSM-only buildings — BeamNG.drive 0.40.1.0

Building footprints, tags, heights, roofs and parts come exclusively from OpenStreetMap's published Layercake building data. Missing height tags use the existing deterministic visual defaults from the OSM footprint and independent terrain. No Overture building is substituted, spatially matched, deduplicated against OSM or retained in the effective building layer.

Overture roads, water, land use and other unrelated inputs remain. All building-dependent context uses the same pinned OSM footprints and attributes as rendered buildings, including road clearances, driveways, parking, street furniture, fuel stations, photo masks and vegetation exclusion masks. `MapData.cell()` and its warm/predownload/array entry points route building layers to OSM and ignore retained Overture building files. The independent elevation, imagery and vegetation sources are unchanged. OSM-only renderer inputs do not let that infrastructure invent building heights/garage classifications or subtract passages. No road replacement is included.

## Data and session flow

1. The ordinary generation job requests its building dependencies, prioritizing the cells nearest the rendering tile. The internal grid is the existing zoom-14 map-data grid, roughly 2 km cells, subdivided into bounded source queries. Users do not select small rectangles or prepare a city.
2. A successfully cached OSM cell is reused across visits and restarts without checking for a newer source version. If missing and online, a data-only Python child retrieves Layercake ranges, stages one cell, normalizes and validates supported OSM features, then atomically publishes a standalone OSM object and cache index.
3. The worker pins a cell head and whole OSM parent/part identity groups in a shared, durable session ledger before generating meshes. A worker crash reconstructs the same choices. Adjacent cells retain full footprints and deduplicate the same typed identity. There is no blanket exclusion of geometry touching previously chosen neighbours.
4. `MapData.building_cell()` reads those OSM features directly into the original near/far/skyline generator. It never reads baseline buildings, including when the OSM view is empty. A validated empty area renders no buildings. A failed first visit renders known cached OSM footprints if available, otherwise explicitly reports unavailable data; it never substitutes Overture.
5. Already chosen geometry stays fixed for the session. No live activation, collision handoff, cohort scheduling or restoration transaction is involved. Ordinary streaming, tile ownership, physical safety holds and the verified zero-radius building collision guard remain.

A new namespace, `CACHE/osm-buildings/v1/`, prevents importing legacy merged objects with GERS IDs or Overture-derived attributes. The old `osm-primary`, overlays, generation catalogs and evidence are untouched. Only the old Layercake source-configuration marker can be read; its geometry is not migrated. Layercake is the selected public Range backend by default: a new application cache acquires automatically without an enable action. Existing malformed configuration still fails closed. Use --offline or --buildings off to suppress network/content.

The store has checksummed atomic JSON objects, an index with full influence bounds, claims, session identity choices, persisted request reservations and immutable retrieval jobs. A bounded global typed-OSM identity index prevents adjacent first visits from implicitly refreshing cached identities; manual freshness updates those identities explicitly. Small advisory summaries report unavailable/unsupported cells in the existing game status line without repeatedly decoding geometry. The source ETag is pinned before ranges are read, every range proves it, and inconsistent partition identities reject publication. Supported records have typed OSM IDs and exclusively OSM provenance; legacy baseline fingerprints and non-OSM provenance are rejected.

No metadata mutex is held during network requests, DuckDB execution, candidate validation or render conversion. Separate OS leases serialize source requests. Pending claims deduplicate workers. A late result cannot replace an immutable session choice. A successful source object/index is durable before result IPC, so completed work survives an interrupted parent. Failed/unavailable choices are not inserted as successful persistent coverage.

### Streaming/context recovery after the Agadir void-world failure

The context-key helper previously walked every ground feature in a road cell. Agadir's retained road data contains water routes reaching Spain and the Canary Islands: two expanded to 992/998 building cells, including the exact remote cell `14/7452/6838` in the failed log. The width algorithm never measures those water routes. Building-context preparation now considers only the land-road classes/kinds eligible for measurement, conservatively including mapped-width roads. Full road measurement extents and the original width/grading algorithms remain intact. Legitimate neighbouring land-road dependencies still need preparation.

Preparation is an explicit worker operation before shared grading plans or near/far/skyline mesh keys are consulted. Cache identities use already-pinned per-cell dependency tokens; calculating a path or checking readiness cannot initiate acquisition. Wall reads use the same immutable session choices, not a launch-time subset or a different building source. Dependent plan and vegetation-mask keys include those choices. Source objects retain whole parent/part geometry.

Immutable object validation/serialization, identity-owner resolution and session-choice document preparation happen outside the metadata mutex. Short commits compare metadata file identities, source pins and claim tokens before quota admission and atomic publication. Pending readers poll immutable metadata without repeatedly taking the writer lock. Validated metadata is memoized by replacement-sensitive file signatures, and per-process decoded-object access is synchronized. Windows sharing violations during metadata replacement receive a bounded 250 ms retry; persistent errors retain the previous file. Read-only lock timing diagnostics warn about waits/holds over 500 ms at most once per ten seconds.

Aggregate-budget exhaustion or publication contention defers ordinary generation and expires only that caller's matching pending token, allowing a fenced retry. It does not turn temporary contention into a permanent unavailable session choice. Actual source failures still use the existing explicit cached-partial/unavailable policy. The child reports `validated` only after immutable object validation and serialization, so publication grace does not include unfinished heavy validation. No quota or acquisition deadline is increased, and no active geometry/collider replacement is introduced.

Focused combined regressions are in `server/tests/test_osmstreaming.py`; new real-byte replays, preservation copies and the fresh isolated acceptance kit are under `.venv/restart-osm/osm-streaming-recovery/`. Earlier kits and their evidence remain historical and must not be reused for this correction. Consult that kit's README for the short manual loading/contact checkpoint; automated generation is not real-engine collision verification.

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

`buildingworld` no longer calls `buildingregions.candidate`, `buildingprimary.prepare/cached`, `buildingbaseline.collect`, `OverlayStore.compose` or the OSM-versus-Overture merge. The old modules remain for recovery/pure legacy tests, not as rendering fallbacks. Low-level range/Parquet and filesystem primitives are reused. Original road records and elevation/imagery/vegetation source versions and algorithms are unchanged. The `building_context=osm-only-context-v1` input-contract token prevents reuse of artifacts derived from the former Overture building context. No files are deleted or source-release/global pipeline numbers bumped. Measured widths and their dependent grading/context can change where OSM footprints differ; this follows the exclusive building-source policy. Effective OSM-only dependency digests, including empty views, exclude old building-bearing mesh caches without a global world cache-version bump.

`launcher.py` routes building status/freshness into the OSM-only workflow. `earthd.py` pins the source-only session before workers/listener construction and disables live controls. Its embedded Python bootstrap and frozen-bundle guard remain. `osmbuildingcache.py` validates/persists source-only objects; `osmbuildingview.py` builds source-only generator arrays. `buildingacquire.py` supervises independent phases and passes small object references rather than whole city payloads. `mapdata.py` enforces OSM building access for every consumer while keeping unrelated layers on their original sources. `road_context_items()` returns the same specs used for rendering, including empty OSM areas. Revision-keyed wall, plan and vegetation-mask caches prevent reuse of old Overture context or stale next-session OSM derivatives; readiness probes never acquire OSM data. `buildings.py` removes the skyline's Overture-cache prerequisite and excludes non-OSM attribute/footprint overrides only for source-only inputs. Tests are in `server/tests/test_osmonly.py`.

Historical integration tests that require merged snapshots, Overture fallback or the removed OSM-primary publication methods are retained with explicit skip reasons (21 cases across `test_buildingprimary`, `test_buildingregions` and `test_buildingacquire`). Their current source-only replacements test immutable choices, claim fencing, source pins, request reservations, four-process stalls/termination, cross-cell groups and ordinary native generation. Reusable Parquet/range/normalization tests still run; the launcher test now mocks the actual OSM-only entry point. These intentional contract retirements are separate from the two previously established unrelated road/protocol test failures.

## Reproducible evidence and one engine acceptance

### Online streaming recovery (November 3)

The failed online Normal-distance run had successfully acquired OSM data. Its
ordinary terrain/far scheduler treated a temporary building-input `LockTimeout`
as a permanent tile failure, and abandoned refined ground. Loading could stay at
0/32 roads and 0/337 far tiles despite valid saved buildings. This was not proof
that more waiting would complete those abandoned jobs.

The recovery keeps source preparation explicit and cache-key/readiness probes
read-only. Temporary waits retry with bounded backoff, wake without player
movement, and cannot report far completion while inputs remain pending or have
exhausted their retry budget. Refined ground is fenced against superseded
anchors. Near-road progress excludes pending building-input retries. Speculative
warming uses prepared building cells only; it cannot take the acquisition lane
ahead of requested generation. These are dependency/scheduling changes, not new
road, terrain or collision geometry algorithms.

One killable source request may stage up to four requested z14 cells in the same
aligned 2x2 block, then validate/publish each complete cell separately. The native
table, reply, feature, vertex, byte, identity and source time budgets remain. A
dense group falls back to its primary cell and defers the others, never truncates
records. Source version, claim tokens, atomic index publication and immutable
session identities still fence every result. Actual source failures retain the
existing frozen session fallback; a temporarily busy input is not such evidence.

Windows metadata byte reads and atomic renames use 32 fixed filesystem-lock
stripes, separate from the publication mutex. Parsing/geometry validation stays
outside both critical sections. This prevents open readers from repeatedly
denying atomic replacement without truncating or deleting the previous document.
Converted OSM layers now share MapData's existing bounded decoded-layer LRU,
keyed by the immutable session dependency, rather than rebuilding a View for
every wall/width lookup. Existing public source and network-security policy,
quotas, ordinary mesh/collision pipeline and restart-only freshness remain.

New tests are in `server/tests/test_buildingcold.py`; the combined cold replay,
real Internet probes, isolated package and exact startup smoke are under
`.venv/restart-osm/worldwide-streaming-fix/`. Cold replay used retained real
Parquet bytes with no normalized building cache: all 32 required near road
targets, 337 far terrain tiles and refined ground completed, including three
injected transient waits, in 255.1 seconds. A new-session source-cache replay
completed all 32 content/skin pairs in 82.1 seconds. Neither is a cold Internet
load-time guarantee or evidence of engine collision. The unused manual kit has
its own profile, empty OSM cache and ports 47781/47782. Earlier kits/evidence stay
untouched. Unsupported geometry, source propagation and cold loading latency
remain explicit limitations.

The recoverable pre-change source archive and all new tests/evidence are under `.venv/restart-osm/osm-only-v1/`. Old worktrees, commits and worldwide-recovery kits/caches remain untouched. This task does not launch Steam or BeamNG.drive, modify the installed game, commit or push.

Pinned November 1 Layercake data contains 1,789 source identities in the main Agadir cell. Five unsupported polygons with holes are omitted; 1,784 supported building entries are stored, with 1,782 intersecting the requested cell. The reference `osm:way:1572086438` is preserved exactly once. The four historical dependency cells produce 20,808 unique OSM buildings across four real workers, all with identical durable choices and no acquisition errors. The generator proof includes the reference plus 20 actual nearby identities around 30.4125652,-9.5545864, each separately generating near, far and collision meshes; this is stronger than successful downloading alone. Two published footprints extend outside their retrieval cell and are excluded from that cell's rendering selection; they are not erased from storage.

The game-free cold-network checks measured Agadir at 15.5 seconds and Paris at 23.9 seconds, both using the same November 1 ETag. The final four-worker real-byte replay measured 43.0–45.4 seconds for all four Agadir cells. Cached restart selection measured about 0.55 seconds with zero source acquisition. These are measured examples, not a loading SLA under game contention. Source records not yet published by that version cannot be inferred from newer OSM edits.

The one isolated engine acceptance kit is `.venv/restart-osm/osm-only-v1/acceptance-kit/`. Its README has exact PowerShell commands, hashes, separate port/profile/cache, source/generator identity diagnostics and the minimal Agadir/Paris/revisit checks. GE Lua must use `FS:getFileRealPath("/")`, never unavailable user/game path helpers. No simulated ACK, mesh hash or static ray proves vehicle collision; actual appearance/contact are still the user's final engine acceptance.
