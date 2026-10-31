# Prepared building activation (Stage 5B1)

[Stage 5B2: Live OSM buildings](LIVE_OSM_BUILDINGS.md) extends this mechanism with
manual map controls, bounded batches, future-tile preparation, recovery and cleanup.
The limitations below describe the original Stage 5B1 checkpoint; actual engine
collision readiness remains unverified pending Stage 6.

Stage 5B1 adds a development-only, explicit activation call for a catalog already
prepared by [Stage 5A](MANUAL_OSM_BUILDINGS.md). It does not fetch OSM, accept a
preview, install the mod, or add refresh UI. Activation uses the existing CDAE
material slots, road creation/deletion governor, collision reloads and session
ownership machinery. The immutable Overture baseline, road geometry, terrain,
protocol version 4 and explicit global cache-version constants remain unchanged.
The existing `revisit.code_key()` hashes all server Python files, so deploying
these code changes still changes the ordinary revisit-cache namespace once,
including with overlays off. This existing deployment fingerprint is preserved;
individual activation/revision changes do not bump it or invalidate the world.

## Invocation and states

Call on the server event-loop thread, with a connected, negotiated client:

```python
# `prepared` is the result of the existing preparation/retry operation.
# Obtain the connection from the running server, not a supplied filesystem path.
publication = prepared["publication"]
result = server.activate_building_overlays(connection, publication, manual=True)
if result["state"] != "rejected":
    transaction = result["transaction"]
    status = server.building_activation_status(transaction)
```

For Stage 5A accept results, the publication is under
`accepted["generation"]["result"]["publication"]`. Preparation remains a separate
operation: a retry of activation never re-fetches, re-accepts or rebuilds anything.
Passing `manual=False`, omitting the capability, or using a non-CDAE session is
rejected. There is no incoming network command that invokes this Python API.

The distinctions are deliberate:

| State | Meaning |
| --- | --- |
| Accepted | The overlay store contains the chosen local data and enable state. No game activation implied. |
| Prepared | The separate catalog atomically references checked, immutable generated assets. No game activation implied. |
| Scheduled | A bounded, fenced command and its resource ownership have been recorded and sent. |
| Deferred | The client is waiting for a safe target, files, residency or vehicle/focus clearance. |
| Installing | The client is staging new objects, observing collisions or retiring old objects. |
| Activated | The exact client transaction acknowledged the observable completion criterion, and the server revalidated its accepted state, inputs and catalog. |
| Superseded | The game reported completion but the accepted state or catalog changed; it is not reported as current success. |
| Failed/rejected/orphaned | No current activation success is claimed. Resource ownership remains conservative. |
| Retired | No active binding refers to the old transaction; its held slots still await exact release receipts. |

Status includes the publication, overlay revision/enable state, sequence,
`game_activated`, historical `game_acknowledged`, held-slot count, detail and a
bounded list of deferred future targets. An old acknowledged revision can remain
visible while a newer revision is merely accepted or prepared.

## Validation and protocol

Before scheduling, the server checks the complete catalog and file sizes/SHA-256,
accepted revision and store incarnation/state, original cached baseline context,
release and deterministic generation inputs. Reading the raw cached baseline
catches changes hidden by a warm MapData LRU. Active target discovery uses actual
near/far/skyline tracker modes, anchors and current server settings. Missing live
targets require another explicit preparation. Inactive retained catalog rows are
validated but do not cause unrelated objects or mesh slots to be materialized.

The client advertises `hello.buildingActivation = 1`. The server returns the same
capability and a new random `buildingEpoch`. The additive commands are:

```text
building_activate {
  version: 1, sessionId, epoch, anchorId, seq, transaction,
  publication, revision, osm, targets: [...]
}
building_cancel {sessionId, epoch, anchorId, seq, transaction}
building_ack {
  sessionId, epoch, anchorId, seq, transaction,
  status: deferred|installing|activated|failed|rejected, detail
}
```

Targets contain server-selected kind/key, generation/content digest, geometry
frame where needed, building collider boxes and existing slot/serial references.
Near targets contain only `bld`, `bld2`–`bld4`, `bldcol` and their exact variants.
Their ordinary base streaming must have completed (`sent` or explicitly `empty`)
before scheduling, so suppressing stale replay cannot prevent initial road
delivery. Explicitly empty legacy/geotile targets can receive new buildings even
without an existing road object or skin; no absent tile is guessed to be empty.
Far/skyline targets use their normal building shape slot. There is no client
directory, download URL or arbitrary path argument. The Lua validator only accepts
the current session's `/temp/beamngEarth/<sid>/slot_<n>.cdae` paths, known part
names, bounded arrays and finite frames. Wrong session, epoch, anchor, duplicate
routes, malformed messages and old/out-of-order transactions cannot activate.

The server rejects malformed acknowledgements and checks connection identity and
all transaction fences. Acknowledgement alone is insufficient: the server again
validates current catalog, source inputs and accepted state. Reconnect, hello,
rebase and disconnect orphan old transaction contexts. Tracker callbacks also
require the exact Future still registered for their tile, before altering state
or allocating/sending a shape. A late old callback cannot remove a new Future.

## Building and collision handoff

The controller and narrow roads adapter execute the actual existing placement
helpers for legacy and native geotiles. New shapes use standard MatSlots paths;
there is no second renderer or unpooled shape resource system. Normal road,
walkway, deck, road-collider and prop objects retain their IDs. Queue entries for
unrelated parts are retained and retargeted to the retained tile wrapper.

The operation first validates every target and journals old/new slot ownership.
It then stages new building visuals hidden, with new lean collision meshes or
full building boxes created through the existing road governor. Old buildings
and collision objects remain present. Current target references, files and
safety conditions are checked throughout. Collision-resident near tiles are
required; missing tiles, ghosts, unavailable skins and unfinished old building
parts defer instead of rebuilding the road bundle.

For changed collision geometry the strongest observable completion criterion is:

1. Every new required object exists.
2. An existing governed `be:reloadCollision(false)` cycle occurs after creation.
3. At least two positive simulation updates pass after that reload.
4. A same-frame building/LOD handoff reveals new and hides old visuals; only the
   old building IDs are queued for governed deletion.
5. Old object IDs are absent, another governed collision reload occurs after
   their deletion, and at least two further positive simulation updates pass.
6. The client sends the fenced `activated` acknowledgement.

Paused physics does not advance readiness. Render-only far/skyline updates do
not request a redundant collision rebuild. Terrain-swap quiet periods, collision
reload spacing, road creation/deletion charging and other existing governors
remain in force. No `be:reloadCollision` return value is treated as an asynchronous
engine-ready acknowledgement.

**This is an observable policy, not proof that BeamNG has published every collision
triangle to physics.** No documented collision-ready fence was found in this
checkout. The tests execute real Lua control/placement code with mocked engine
objects; actual physics, render visibility, CDAE engine caches and multi-vehicle
contact behavior have not been verified in BeamNG. Installation and game launch
were not performed.

Every vehicle is considered, including stationary/parked and non-player vehicles.
The old/new building bounds are excluded by a margin of 200 metres plus 20 seconds
of horizontal speed. All supplied streaming foci are excluded using at least
200 metres of focus radius plus a 2 km guard. Missing, nonfinite or failed safety
information defers. The controller does not move vehicles, reset them, zero their
speed or replace terrain/roads to make activation possible. Bounds use conservative
2D geometry; a vehicle high above the area can therefore also defer activation.

Revealing a new object can fail: the adapter restores old visibility and removes
uncommitted new objects. A failure after some parts have been committed retains
their ownership and reports failure rather than inventing a rollback or success.
Retry sends a fresh complete handoff without another fetch or acceptance.

## LODs, skins and future streaming

Live dependent near/far/skyline targets are one bounded activation batch. Exact
geotile variants use the existing placement/frame helpers; the road skin remains
the existing skin. Identical content/settings can retain acknowledged objects and
slots instead of recreating them. Reuse checks copied bytes and current target
identity. The final handoff refreshes affected LOD suppression in the same frame.

Once scheduled, geographic dependency-cell coverage is journaled **before**
delivery. Coverage includes existing building density/neighbor dependencies and
is recovered from checked prior journals on server restart. A lost acknowledgement
does not prove the game installed nothing.

An acknowledged binding filters ordinary replay messages so startup-worker data
cannot restore its older buildings. A newly streamed, unprepared near/far/skyline
target touching protected cells is marked `buildingPending`: its old building
shape/colliders are removed, while roads, walkways, props, chunks and other
colliders stay intact. An already displayed near road bundle is kept intact.
Pending near content does not suppress a valid far fallback, and pending far
content is not marked as a complete far-building tile. Unrelated areas pass
through unchanged. Pending routes are exposed in status.

This is intentionally a **safe deferral**, not automatic preparation of every
future tile. Stage 5B2 must coordinate future worker snapshots, prepare these
targets and explicitly activate the current revision. It must not restore the
stale startup view to fill the gap. Invalid recovery journals cause building
content to defer; unrelated road content continues where its bundle is readable.

## Ownership, recovery and bounded retention

The server writes checksummed atomic
`building-generations/activation-<session-hash>.json` journals using the existing
catalog I/O implementation. They record transaction fences/states, generation
references, accepted-state tokens, protected cells and slot/serial ownership.
They do not modify the overlay store, baseline or prepared mesh artifacts.

MatSlots owners and independent server holds keep copied shapes unavailable for
reuse across acknowledgement failure, disconnect and anchor changes. An exact
slot/serial release is required; a timer is not proof of deletion. Old resources
still referenced by a reused binding stay owned.

Lua uses the existing roads SimGroup's bounded `buildingHolds` field to journal
slot, serial, source session and collision-use information before mesh loading.
The group is created before the first ownership flush, even in an empty world.
Writes are read back; failure/truncation restores the previous field and fails
closed. Actual deletion and subsequent observed collision retirement control
release receipts. Session+slot identity prevents collisions between two servers'
equally numbered slots. Lua reload sweeps old group objects, observes collision
retirement and recovers release receipts instead of forgetting their ownership.
A full Lua VM reload still uses the mod's existing all-roads sweep; continuity of
road collision during that external reset is not proven by this building-only
activation work.

The existing hello/pose session union includes roads and activation holds as well
as farfield sessions. A `.building-activation-hold` marker in a source session
also prevents the existing age-based session collector from deleting its files
before a reconnect can claim them. This stage deliberately does **not** remove
that marker automatically: full proof-based session/generation GC is deferred.

Old legacy near bundles adopted after a server restart remain protected while a
vehicle/focus is nearby (conservative 4 km plus speed guard). Their whole road
bundle cannot safely be handed over through a building-only operation; an orphan
timeout therefore cannot remove a road under a vehicle. Current-world handoff of
such a session is a Stage 5B2 prerequisite, or the vehicle must travel clear.

The existing credit text appends OpenStreetMap contributors, ODbL-1.0 and the
copyright URL when an OSM activation is staged. The credit flag is conservative
while its roads group survives; failure/revert can leave the credit visible
longer than necessary. Raw OSM provenance remains in the overlay store.

| Resource | Limit / behavior |
| --- | --- |
| One activation | 32 targets, 256 shape references, 1 MiB command, 128 MiB copied bytes |
| Retained copied leases in the current server session | 512 MiB; refuse further work rather than evict potentially live shapes |
| In-memory transaction history | 32 transactions |
| Ownership journal | 1 MiB; 16 retained session journals; bounded control-directory entries |
| Protected dependency coverage | 512 geographic cells across retained journals |
| Deferred future target details | 128 routes |
| Lua ownership journal | 512 records, 128 KiB; refuse overflow |
| Prepared assets | Existing Stage 4B catalog limits still apply; no broad world-version bump |

Quotas are a deliberate blocker once conservative retention fills them. This
stage never deletes a published generation, silently reuses an uncertain lease,
or deletes a pinned old session to make space. It does not automatically resume
an old game transaction from a server journal. Corruption, a reused current-session
journal, changed source/settings, unavailable dependencies and exceeded budgets
require an explicit checked recovery/preparation path.
The byte budget is not a global disk cap across restarts: pinned session folders
also contain ordinary world assets. The 16-journal bound limits activation-session
history; reclaiming those folders and accounting for their full retained bytes
requires the later proof-based session GC.

## Verification

Run the reproducible project-local suite:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/test_building_overlay.ps1
```

The runner pins embedded CPython 3.12.10, NumPy 2.2.6, Pillow 11.3.0 and Lupa 2.6
(its LuaJIT 2.1 runtime) by SHA-256. Downloads/bootstrap are local tooling setup;
subsequent tests use the existing local archives and synthetic/mocked data. All
test temporary files are directed to `.venv/overlay-tests/tmp`; `-B` avoids bytecode
artifacts. No OSM request or installed-game access is part of these fixtures.

`test_buildingactivation.py` runs 40 tests covering real prepared native meshes,
server fences, checksums, missing assets, stale settings/baselines, enable/disable,
rollback, retry without fetch/accept, reused resources, all LODs and geotile exact
variants, real Lua collision observations, vehicle/focus guards, pause, failure
cleanup, roads/object identity, cross-session slots, Lua reload, future LOD
deferral, restart coverage and pinned-session retention. Actual controller,
adapter, roads, geodesy and geotile Lua execute under LuaJIT; engine APIs alone are
doubled. All four modified/new Lua modules also compile under that runtime,
including core and its local-variable limit.

The expanded runner retains Stage 1–5A suites and adds protocol, MatSlots, staging
and synthetic road-geotile checks. It explicitly opts out of the old real-data
road-geotile fixture with `BNGE_ROADGEO_REAL=0`, checking this opt-out before any
filesystem probe of its hardcoded user cache. An initial expanded probe, before
that correction, unexpectedly read that user cache and failed on missing DuckDB;
it was not retried and that external cache was not inspected or altered afterward.

Two pre-existing failures remain visible and make the runner return nonzero:

- `test_roads.PaintRoadsTest.test_bare_verge_beside_roads`: `9 != 0` at the painted
  road edge, reproduced previously with unchanged HEAD sources/runtime/native code.
- `test_protocol_agreement.CacheAgreementTest.test_cache_in_the_mod`: its enum
  regex does not match the existing numeric cache control and calls `.group()` on
  `None`. Reproduced using the original test, core and settings from HEAD in memory.

Existing external-`luajit` CLI agreement checks still skip when that CLI is absent;
the new activation Lua tests require Lupa and do not silently skip. The real-data
road-geotile cases are an explicit repository-only opt-out, not passing fixtures.
Final execution on 2026-10-31 with the project-local runner:

| Checks | Actual result |
| --- | --- |
| Stage 1–5A overlay/retrieval/render/preparation/manual suites | 224 passed |
| Stage 5B1 activation suite | 40 passed, including all real Lua execution checks |
| Entire expanded runner | 508 run: 502 passed, 1 known road failure, 1 pre-existing cache-test error, 4 explicit skips |
| Existing CLI agreement checks | 2 skips because the external `luajit` command is unavailable |
| Legacy real-data road-geotile checks | 2 explicit opt-out skips; no external cache probing in the final run |
| Runner exit | Nonzero (1), preserving both known failures |
| `git diff --check` | Passed |

The complete local log is
`.venv/overlay-tests/stage5b1-final.log`; the unchanged-HEAD cache-test reproduction
is `.venv/overlay-tests/stage5b1-baseline-cache.log`. No failure was converted to a
skip to produce a green result.

## Files changed in Stage 5B1

| File | Purpose |
| --- | --- |
| `server/buildingactivation.py` | Checked scheduling, acknowledgements, binding/replay fences, geographic guards and durable ownership. |
| `server/earthd.py` | Development API, negotiated commands/acks, contextual fencing, slot holds and pinned-session cleanup protection. |
| `server/roads.py`, `server/vegetation.py` | Exact-Future guards before old callbacks mutate any tracker state. |
| `mod/.../beamngEarth/buildingactivation.lua` | Bounded client transaction state machine and observable collision/retirement criterion. |
| `mod/.../beamngEarth/buildingadapter.lua` | Building-only adapter to actual placement, queues, safety, LOD and resource helpers. |
| `mod/.../beamngEarth/roads.lua` | Adapter host, targeted handoff support, session-aware leases/journal, safe orphan/drop guards, pending-building LOD and credit handling. |
| `mod/.../beamngEarth/core.lua` | Capability/epoch negotiation, dispatch, governed ticking, session union and existing attribution-line extension. |
| `server/tests/test_buildingactivation.py` | Synthetic end-to-end server/native/Lua tests. |
| `server/tests/lua/buildingactivation_trace.lua` | Deterministic mocked BeamNG engine harness for real Lua modules. |
| `server/tests/test_protocol_agreement.py` | Additive command/ack schema and session-union assertions. |
| `server/tests/test_roadgeo.py` | Honor existing explicit real-data opt-out before probing the user cache. Default real-data behavior unchanged. |
| `tools/test_building_overlay.ps1` | Pin/bootstrap required embedded Lua, expanded tests, repository-only opt-out and environment restoration. |
| This document, `MANUAL_OSM_BUILDINGS.md`, `BUILDING_REGENERATION.md`, `server/README.md` | Architecture, development use, verification and integration boundaries. |

The five already-untracked Stage 5A files were preserved; they were not rewritten
as part of Stage 5B1. No Rust or installed-game files changed, and no commit/push
was made.

## Remaining work before Stage 5B2 or distribution

An installed-game test is needed to verify actual collision contact/raycast
readiness before/after handoff, hidden visual behavior, CDAE/TSStatic engine cache
lifetime, heavy governor/terrain load, all vehicle/focus sources and session/level
reload. The two simulation-update criterion cannot prove these engine properties.

Stage 5B2 must provide manual UI/permissions, current-world handoff after a restart,
future streamed-target preparation with active snapshots, proof-based history and
session GC, and user-visible retry/deferred status. It must preserve accepted versus
active revision distinctions and avoid unbounded retention. No Stage 5B2 refresh
UI, automatic network work, broad tile invalidation or live-game installation was
implemented here.
