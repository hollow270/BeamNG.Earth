# Refresh before startup

The newer [explicit Layercake building preparation](LAYERCAKE_BUILDINGS.md) is the
current worldwide workflow. It selects freshness only on user request and reuses
saved data on normal startup. The saved-area/automatic workflow below is retained
as historical implementation. The worldwide session ledger adds immutable choices
for first-visit cells before generation; the invariants below describe the older
fixed-at-startup mode, not a requirement to prepare each new place manually.

This opt-in mode refreshes explicitly authorized saved areas before any world
workers, saved-area preparation or game listener start. It uses the existing OSM
parser, baseline lookup, conservative merge and atomic overlay store. It does not
use building activation transactions, catalogs, journals, cohorts or restoration.
Existing live-mode code and retained evidence are preserved.

Invariants:

1. Overture cells, road context, road widths and terrain inputs remain baseline.
2. A refresh accepts only complete, fresh, issue-free, conflict-free data. Missing
   features are never deletions. Failure preserves the previous accepted revision.
3. One selected snapshot is fixed for the whole process lifetime. Every local
   worker and respawn receives that snapshot, rather than rereading a mutable head.
4. Building-bearing cache keys depend on local effective geometry/attributes and
   parent parts. Unrelated tiles and baseline-only cache keys do not change.
5. The startup network reservation is durably written before fetching. Spacing,
   overlapping-area cooldowns and Retry-After survive crashes/restarts. A corrupt
   ledger blocks fetching; it is never silently reset.
6. Startup never waits for a cooldown or rotates endpoints. At most one due area
   is attempted per launch; oldest-attempted areas have priority. All deferred work
   is reported. There are no building-refresh OSM requests during play; unrelated
   existing data services retain their original behavior.
7. Overlay writes retain existing quotas and atomic publication. Unchanged data
   updates only the bounded check ledger. Exhausted changed-data history retains
   previous data and reports the limit; no automatic pruning or quota increase.
8. A baseline-incompatible/corrupt overlay cannot leave missing collision: startup
   fails before opening the game listener. Retrieval failure with a valid saved
   snapshot continues normally. No active engine resource is replaced.

Smallest defaults: one configured rectangle, one endpoint, at most eight saved
areas, 60 seconds between attempts, ten minutes for overlapping areas, existing
HTTP size/time/element/vertex limits. Saved overlays start disabled; the first
successful authorized refresh enables them. An explicitly disabled existing
revision stays disabled until enabled offline. Cached mode never fetches.

Set `BNGE_OSM_STARTUP_CONFIG` to a local JSON file (not committed):

```json
{
  "version": 1,
  "authorized": true,
  "endpoint_policy_confirmed": true,
  "refresh_on_startup": true,
  "areas": [{"name": "my-buildings", "bounds": [-76.585, 39.292, -76.583, 39.294]}]
}
```

Bounds are west/south/east/north, at most approximately 500 x 500 metres. Configure
`BNGE_OSM_ENDPOINT` with an explicitly permitted HTTPS interpreter URL and optional
`BNGE_OSM_HEADERS` for authentication. Existing `BNGE_OSM_SPACING_SECONDS` and
`BNGE_OSM_COOLDOWN_SECONDS` may increase the defaults. No public endpoint is supplied.
`refresh_on_startup: false` loads saved data without requiring an endpoint.
Startup mode takes precedence over `BNGE_OSM_BUILDINGS` and bypasses its live UI.

Close both BeamNG.drive and its companion before restarting this mode. A companion
restart while an existing game retains geometry is not a supported refresh boundary.
The launcher requires a fresh game process in this mode. Source hashes already
namespace deployment changes once; normal OSM checks do not change the global key.

Operational state/report: `<cache>/osm-startup/ledger.json` and `report.json`.
Both use the existing checksum envelope (`payload` contains the report). The
normal launcher echoes the startup summary, including failures and deferrals.
`checked` means at least one area was checked; `all_areas_checked` distinguishes
complete coverage from the explicitly reported per-area deferrals. Semantic no-op
checks preserve accepted provenance/history and advance only the check watermark.
Accepted data: `<cache>/building-overlays/building-overlays/v1` unless an explicit
`BNGE_BUILDING_OVERLAY_DIR` is configured. Credentials are never written into the
ledger/report. Replication time means the endpoint's observed snapshot time, not
proof that a particular user edit was included.

Offline control (same Python environment as the application; game/companion closed):

```powershell
python server/buildingstartup.py --cache-dir PATH --action disable
python server/buildingstartup.py --cache-dir PATH --action enable
python server/buildingstartup.py --cache-dir PATH --action rollback
```

Set cached mode before rollback to inspect it without a subsequent startup fetch
reapplying newer upstream data. Control commands only change the saved overlay;
they do not alter running geometry or remove history/resources. They serialize
with startup and require the explicit startup configuration. Existing live kits,
catalogs, journals and held files are never cleaned by this mode.

Remaining limits include unsupported polygons/holes, ambiguous splits/merges,
deletions, 64 known OSM IDs per fetch, baseline context bounds and 128 changed
revisions/refresh records. Those produce explicit deferrals. Long-term accepted
history compaction is a separate migration, not automatic evidence deletion.

`manual=True` remains required by the existing retrieval API. The startup caller
supplies it only after the saved configuration grants startup authorization;
there is no relaxed network API or interactive consent inferred from a restart.

The 128-target preparation/catalog limit does not apply: ordinary streaming
generates tiles individually from the pinned snapshot. There is no all-target
activation success state. Normal worker/mesh/cache limits remain in force.

## Implementation map

| File | Change |
| --- | --- |
| `server/buildingstartup.py` | Saved-area authorization, bounded pre-stream refresh, durable timing/check ledger, atomic acceptance, no-op detection, offline control and selected snapshot validation. |
| `server/earthd.py` | Startup barrier, snapshot arguments for every local pool, live-mode bypass, and rejection of an old retained game world. |
| `server/mapdata.py` | Explicit startup snapshot construction/pinning; baseline readers retain their original contracts. |
| `server/blockworker.py` | Initializer/respawn snapshot propagation and precise local building cache dependencies. |
| `server/buildingrender.py` | Filter dependency identities by the actual rendering read footprint. |
| `server/buildingrefresh.py` | Keep the preserved legacy preparation API's skyline identity consistent with the ordinary writer. |
| `launcher.py` | Require a fresh game process in startup mode and show the startup report. |
| `server/tests/test_buildingstartup.py` | Two-session native geometry/cache tests, real startup ordering, spawned workers, crash/atomicity/cancellation, rate limits, boundaries/parts, offline controls and production-distance inventories. |
| `server/tests/test_buildingrender.py` | Query the actual feature's geotile owner rather than a neighbouring boundary tile. |
| `server/tests/test_buildingrefresh.py` | Test density-neighbour invalidation where the renderer actually uses district density. |
| `server/tests/test_launcher_linux.py` | Guard external launch calls and skip Linux direct-start fixtures that lacked a Windows platform guard. |
| `tools/package.py` | Include this guide in the application archive; the new server module is included by the existing module collection. |
| `tools/test_building_overlay.ps1` | Include the new startup regression suite. |
| `docs/LIVE_OSM_BUILDINGS.md` | Link to this separate mode without removing the preserved live workflow. |
| This guide | Architecture, configuration, limits, migration and verification. |

No Rust, Lua, protocol schema, original Overture source or terrain/road generation
algorithm changes are required. The existing live implementation remains available
when this new opt-in configuration is absent.

## Development verification

The preserved base is `b54f601a7220ad30a97b98d609762e2d86bb10ad`; this implementation
is in the separate `feature/restart-osm-buildings` worktree. The original 23-file
production-distance redesign and Stage 6A--6E evidence are not modified.

- 28 restart-specific cases pass, including ordinary native generation of the
  historical Near/Normal 191/312 inventories and 194/332 expanded inventories.
- Broad regressions cover 672 cases. After the affected guarded reruns, the only
  unresolved failures are the established `PaintRoadsTest.test_bare_verge_beside_roads`
  and `CacheAgreementTest.test_cache_in_the_mod`; 18 platform/data tests are skipped.
- Final strict focused run: 105 cases, 91 passed and 14 platform skips. This covers
  startup, launcher isolation, subprocess store crash/concurrency and JS/Lua/Vue
  round trips. The guard permits only pinned Python workers and the specific Node
  test scripts; other launch APIs, shells and external networking are blocked.
- UI: 38 bridge, runtime-rendering and compilation checks pass. Preparation: eight
  checks pass. Package/source hashes and `git diff --check` are verified separately.

The sparse synthetic inventories verify on-demand scheduling, dependency coverage,
cache selection and snapshot consistency, not dense-city performance or actual
BeamNG collision. Real 0.40.1 loading, collision, LOD transitions and teleport/revisit
still require an isolated user-operated engine test. Existing online dependencies
and an explicitly permitted endpoint remain required for a later real-data test.
