# Production-distance building refresh

Design checkpoint: `b54f601a7220ad30a97b98d609762e2d86bb10ad`.
This document specifies the coordinated correction before implementation. Engine
validation remains a separate manual step.

## Influence and identities

Cache-loading cells are not a rendering footprint. Keep the existing bounded cell
loading and baseline access. Add a shared influence model: geographic rectangles
cover the complete original/accepted parent and part context collected by
`buildingbaseline`; near/far targets also read district density within 1 km only
where `buildings.district_town` actually does. Skyline targets do not read that
district. The model is conservative about whole footprints, ownership changes,
parts, rectangular bounds and projection. It never treats absent OSM data as a
deletion. Uncertain historical coverage keeps the former cell-based fence.

Use this same predicate for target discovery, overlay dependency fingerprints,
ordinary-message suppression and activation closure. Cell-only dependency queries
used by density statistics retain their existing meaning. Feature-off inputs are
unchanged. No road, terrain, road-width or grading inputs are changed. Existing
baseline versions, accepted revision and real ABA state tokens remain required.

## Coherent cohorts

Enumerate wanted/live targets with the existing bounded tracker inspection. The
in-memory influence inspection is capped at the existing 512-generation capacity;
it is not permission to allocate 512 jobs or enlarge a catalog. Overflow is an
explicit error, never truncation. Each catalog still contains at most 128 targets;
wire packets still contain at most 32, assembled into at most one 128-target Lua
transaction. No incompatible network protocol change is required.

For aligned geographic grids, group each near/far child with its skyline parent.
For aligned legacy grids, use the corresponding four-by-four road-tile group.
All live representations in a group belong to the same atomic component. Join
groups intersected by a complete old/new source-context rectangle: this includes
parent centroid moves and parts crossing ownership boundaries. District-only
effects outside that rectangle can progress in separate groups because they
cannot move a parent between owners. Mixed grid/frame configurations and unknown
historical ownership are conservatively one component. Never split a component
to meet a limit. Oversized incompatible components remain explicitly deferred.

Pack only whole compatible components into bounded catalogs. Near components are
admitted individually; far-only components may share an admission up to the
existing 32-target wire bound (one indivisible larger component can use the full
128-target catalog). This bounds peak shape allocation without creating a
transaction for every empty skyline target. Initially prioritize
player-near collision groups. Keep a bounded FIFO of remaining component identities
for the same revision/frame/settings. Permit at most three near completions before
servicing an eligible distant turn, preserving FIFO within each class. Newly arriving
near collision targets are prioritized without indefinitely starving distant work.
A component whose near road targets
are not normally streamed is deferred; independent ready components can progress.
Keep all existing governor, pause, safe-region, mesh validation and collision
retirement behavior in Lua. Apply near-first order to preparation and staging.

## Publication, acknowledgement and progress

A new optional, versioned catalog field records the complete influence proof for
a cohort. Old catalogs retain their old conservative validation. Under the same
catalog/store locks, activation rediscovers the current influence plan, validates
all selected descriptors and requires every live member of every intersecting
component in the catalog. Other independent components may wait. The catalog must
also cover all authoritative accepted/historical source influence. Check this
again on activation acknowledgement. A catalog never advances while its prior
transaction awaits activation/retirement acknowledgement.

Count completion from acknowledged bindings whose identities, accepted ABA token,
overlay/baseline mode, connection epoch and anchor are current. Reuse may update
validation stamps but must not transfer pooled-slot ownership away from its real
transaction. If one member changes, include the complete component again. Partial
completion is not `game_activated`; report required, acknowledged, pending and
blocked target counts plus ready/deferred components. Rediscover as streaming
changes. Never claim completion from cached preparation or simulated ACKs.

## Durable guards, ownership and recovery

Persist versioned source rectangles alongside the existing activation journal's
cell guards, transactions and holds, atomically before dispatch. Cell guards remain
ownership roots. Validate their agreement with the new coverage document. A legacy
journal without the document retains every old cell fence. Invalid coverage fails
closed. Do not enlarge coverage by taking dependency cells of already expanded
targets; retain accepted and historical source coverage instead.

Keep the existing 512 generations, 512 dependency cells, 256 held shape slots,
32 transactions, all byte/time limits, job leases, count reservations, checksums,
source attribution and proof-based reclamation. Each bounded cohort passes the
same admission checks; aggregate held resources can still cause a real deferral.
Collection may remove only proven unreferenced generations. Catalog replacement
does not release previous cohorts: journals, bindings, holds and inventories keep
them rooted. No age-based or uncertain reclamation is introduced.

An acknowledged binding with no shape slots references inline data only; it does
not root generation files. Once such a transaction has no slot-bearing binding or
hold, it may be compacted while retaining its validation stamp and source coverage.
Unacknowledged transactions and all actual shape owners retain the existing roots.
This accounting is necessary for many empty distant targets and does not increase
any quota or infer a slot release.

Before creating a transaction, cost complete prepared components from their checked
shape/exact receipts and verified reuse bindings. Admit only whole components that
fit remaining held slots, per-transaction copy bytes and retained bytes. Keep the
existing allocation-time checks too. A packed distant batch can therefore advance
the components that fit without retrying an oversized aggregate forever. Explicitly
report the unadmitted prepared targets. A failed cohort invalidates its bindings'
completion claims while retaining every actual slot hold and resource proof.

Accepted source coverage, the last complete catalog and transaction ownership are
durable. Scheduling queues and completion claims are deliberately rebuilt after
restart; they are not evidence of engine objects. Reconcile using existing fenced
inventories, retain uncertain resources and revalidate/reprepare current cohorts.
Anchor/connection/settings/revision changes fence or cancel pending transactions;
they cannot release slots. Rollback/disable include historical source areas so
previously installed overlays are replaced by a validated baseline. Failed
restoration keeps the existing conservative baseline recovery, using the same
cohort machinery; report partial/blocked recovery without false global success.

## Verification

Reproduce the historical 191/312 discovery and 194/332 activation/guard expansions
using settled native geotile inventories. Test the full corrected path, persistent
guards and restart, not just a reduced discovery count. Also exercise genuinely
large independent cohorts, joined/oversized and mixed-grid cohorts, density effects,
parent/part moves, late streaming, FIFO fairness, partial ACKs, cancellation, ABA,
teleport/re-entry, disable/enable/rollback, held resources and true exhaustion.
Compare untouched native mesh arrays and baseline roads/terrain inputs. Run Python,
real Lua via Lupa, Vue runtime/compilation and development-kit preparation tests.
Preserve the two known unrelated roads/cache-agreement failures separately. Do not
modify or launch a game or earlier test kit, fetch OSM, commit or push.

## Implemented files

| Files | Purpose |
| --- | --- |
| `server/buildinginfluence.py` (new) | Pure coverage validation, shared render footprint, deterministic ownership components, bounded inspection and fair component selection. |
| `server/buildingtargets.py`, `server/buildingmanual.py` | Explicit influence discovery and preview/preparation integration; preserve the small legacy offline registry API. |
| `server/buildingrender.py`, `server/mapdata.py`, `server/blockworker.py` | Filter building-only dependency identities by actual influence; retain cell-level density queries and original baseline consumers. |
| `server/buildingrefresh.py`, `server/earthd.py` | Validate, atomically publish and forward optional cohort metadata using existing jobs, reservations and catalog interfaces. |
| `server/buildingactivation.py` | Check component closure and current inputs, prioritize staging, admit complete components by exact existing slot/byte budgets, preserve actual ownership, and stamp only acknowledged bindings. |
| `server/buildingretention.py` | Validate/reload precise source fences, preserve legacy fences and fail closed on missing catalogs with ownership journals. Collection proof rules remain in force. |
| `server/buildinglive.py` | Bounded cohort orchestration, three-near/one-distant scheduling, aggregate completion, current callback fences, partial recovery and unchanged-context cancellation without discarding independent acknowledgements. |
| `mod/beamng_earth/ui/ui-vue/mods/beamngEarth/EarthBuildingRefresh.vue`, `mod/beamng_earth/ui/ui-vue/mods/beamngEarth/buildingRefresh.js` | Render validated aggregate counts and explicit deferred-hint overflow; acceptance remains manual. |
| `server/tests/test_buildinginfluence.py` (new) | Historical counts, complete activation/guard path, genuine large cohorts, native meshes, resource admission, cancellation, restart and revision/renderer fences. |
| `server/tests/test_buildinglive.py` | Real Python/protocol/Lua/JavaScript round trip now includes partial progress. |
| `server/tests/test_buildingrender.py`, `server/tests/test_buildingrefresh.py` | Test the actual owning geotile and real arid-West density dependency instead of assuming every shared cell changes rendering. |
| `server/tests/test_buildingrestoration.py` | Serialize the immutable-store tree check with the authority worker to avoid racing Windows' locked coordination file. Keep all geometry/road/store assertions. |
| `tools/test_building_overlay.ps1`, `tools/test_building_ui.mjs` | Include the new suite and Vue runtime progress/malformed-data checks in reproducible testing. |
| `tools/prepare_stage6a.py` | Require the new runtime module when validating future isolated packages; existing kits are not rebuilt. |
| `docs/LIVE_OSM_BUILDINGS.md`, this document | Handoff, architecture, limits, file explanations and validation procedure. |

## Recorded results

Evidence is inside `.venv/production-influence-tests/`; it is ignored development
output and is not part of a production package.

- `full-final.log`: 27 suites, 616 tests, 610 passed, four skipped, one failure and
  one error. Only the established `PaintRoadsTest.test_bare_verge_beside_roads`
  (`9 != 0`) and `CacheAgreementTest.test_cache_in_the_mod` (obsolete regex returning
  `None`) fail.
- Subsequent `*-verified.log` runs: 24 influence tests, 23 live-control tests and
  14 restoration tests all pass with the final scheduling/callback code. An
  additional partial-restart and acknowledgement/streaming race tests pass in
  `restart-verified.log` and `ack-race-verified.log`; the influence suite now
  contains 26 tests. There are 618 distinct Python/Lua
  regression cases covered by the combined matrix.
- Vue: 43 checks pass, including real Vue rendering, explicit acceptance and the
  Python/protocol/Lua/JavaScript progress round trip. Preparation: eight pass.
- Actual package enumeration: 347 files, with the new server module and updated
  UI present, and no development artifacts/tests/logs. No release was built.
- All 12 retained Stage 6A--6E archive hashes and the recorded Stage 6D catalog and
  journal hashes match their historical audit values. No collection was run on
  those caches. `git diff --check` passes; nothing is staged or committed.

The Baltimore candidate inventories still reproduce 191 Near / 312 Normal and
the failed margin-only experiment still reproduces 194 / 332 activation/guard
dependencies. The shared precise model needs four coupled near/far targets for
that synthetic selection. Separate historical-source fixtures make at least
191 / 312 targets genuinely relevant and process them through real catalogs,
validation, persistent guards and the actual Lua controller with engine doubles.
Native tests separately prove mesh generation, disable/re-enable, rollback and
unchanged distant meshes/identities. Simulated acknowledgements and engine doubles
do not verify real collision contacts or resource-release timing.

## Engine continuation and remaining limits

Code is ready for isolated engine validation, not a declaration that engine testing
has passed. Build a fresh package/profile/cache without overwriting any Stage 6
kit. Use a permitted HTTPS endpoint only after its configuration and workload are
approved; no endpoint or credentials were introduced by this change.

1. Start the isolated companion and game using the established isolated-profile
   procedure. First reproduce the synthetic test at Near and Normal distances.
   Confirm the UI distinguishes saved/prepared data from acknowledged targets.
2. Observe partial progress and completion: all currently required targets must be
   acknowledged, pending must reach zero, and only then may the whole view report
   activated. A GE controller's single `activated` transaction is one cohort.
3. Verify native visual changes, player-near collision contacts/rays, old collider
   retirement, near/far/skyline seams, pause/unpause, and normal streaming alongside
   refresh. Confirm original road/terrain object identities remain intact.
4. Repeat normal, repeated, outside-and-return, rapid and paused teleports at
   Normal distance; revisit after renderer changes. Restart the companion with a
   partially completed plan and verify inventory/ownership fencing and restoration.
5. Test persistence, disable/re-enable and rollback, then inspect counts, held
   slots, pending components, generation usage and work directories. Under real
   pressure require an explicit deferral/fallback, not a success claim or deletion
   of uncertain resources.
6. Only after the isolated synthetic replay succeeds should the separately approved
   real OSM/Overture production smoke proceed. Preserve its logs and evidence too.

An indivisible component over 128, incompatible mixed ownership grids, large
unproven legacy cell coverage, or genuine aggregate resource exhaustion remains
explicitly blocked/deferred. The 512-target inspection bound is also enforced.
These conditions do not truncate geometry or relax ownership/collision checks.
Use a fresh cache for validation rather than deleting retained legacy evidence.
Restarts intentionally reset in-memory completion claims and can repeat bounded
preparation until current engine state is acknowledged. Original endpoint/runtime
configuration requirements from the production preflight remain applicable.
