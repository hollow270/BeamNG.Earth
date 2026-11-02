# Stage 6C — building staging governor starvation

Checkpoint: `5bbac4d3bad987c2f9ab91baa10a821a73bd0248` on
`feature/live-osm-buildings`; working tree clean at the start. The user confirmed
successful preview, explicit acceptance, mesh preparation and delivery of 80
activation targets in three batches in BeamNG.drive 0.40.1. The transaction then
remained `staging` / `deferred` despite unpaused simulation. This document covers
the focused fix and a new, separate kit. Codex did not install into or launch the
game, issue a live OSM request, change road/terrain generation, alter retention
safeguards, commit or push.

## Confirmed mechanism

`core.lua:onUpdate()` sets loading mode, opens the governor frame, charges the
previous engine tick, processes network messages and later updates the building
controller, inventory, roads and far-field in that existing order.

Previously the work predicate was only:

```lua
gov:beginFrame(farfield.hasWork() or roads.hasWork())
```

In `governor.lua`, no work makes `b` ramp down to zero. Tokens are replenished by
`b` per frame and clamped while idle. At `b=0`, an existing negative token balance
does not recover. Loading mode's target of 8 ms does not override the missing work
flag. The observed `b=0`, `tokens=-32.9647`, `target=8`, `loading=true` is therefore
a stable stall while both ordinary work queues are idle.

The building controller's `waiting` phase plans/validates targets, checks vehicle
safety and holds their existing resources. It then enters `staging`. Every mesh
creation still requires `gov:try('roadWalk')` and charges its measured cost. At the
observed approximately 1.15 ms estimate and negative tokens, the first import is
refused forever. The controller is updating, but the bucket never funds it.

## Minimal scheduling fix

The controller now exposes the pure query `hasWork(anchorId)`, true only for a
pending **staging** transaction matching the optional anchor. The existing caller
adds it to the work predicate for the active world and current unpaused step:

```lua
gov:beginFrame(farfield.hasWork() or roads.hasWork()
  or (st.active and dtSim > 0 and M._buildingActivation.hasWork(st.anchorId)))
```

Planning can enter staging during the first update with no budget; the following
frame sees staging and starts the existing budget ramp. This deliberate one-frame
delay does not bypass `try()`.

`governor.lua` is byte-for-byte unchanged. Token caps, debt, EMA estimates, target
budgets, ramp rates, heavy/quiet frames and engine-debt charges are unchanged.
The controller's import, safety, collision and pause logic are unchanged. No
unconditional mesh import, forced token reset, new import loop or extra module
import is introduced. The existing controller → inventory → roads → far-field
update order is preserved.

The new flag is false for incomplete batches, waiting on safety/target availability,
ready/collision fences, retirement and terminal transactions. The core excludes
paused, inactive and wrong-anchor staging. Ready/retirement do not need staging
tokens: `roads.hasWork()` already counts deletion queues and `collisionDirty`, and
the existing road update owns their governed cleanup/reloads. Therefore this fix
does not keep the budget permanently elevated after completion or failure, and
does not weaken collision-retirement or held-resource safeguards.

Sustained engine debt or a heavy-frame request can still defer work. That is
intentional; the fix restores ordinary budget replenishment, not guaranteed progress
under arbitrary overload.

## Read-only progress diagnostics

`buildingactivation.status()` retains its old fields and adds bounded scalar
information while pending: `target_cursor`, `object_cursor`, `planned_targets`,
`target_count` and `target_objects`. It does not inspect scene objects or modify
the controller. Terminal status retains its previous shape.

The development-only helper in the kit adds `s6.progress()`: one snapshot of those
fields, the existing governor snapshot, the `roadWalk` estimate and the three work
flags. It reads already-loaded modules; it does not import modules/assets, run a
timer, charge/replenish tokens, reload collision, write files or change objects.
Repeated-query tests check identical controller/governor/object state. The tested
snapshot is below 4 KiB.

## Files changed

| File | Change |
| --- | --- |
| `mod/beamng_earth/lua/ge/extensions/beamngEarth/core.lua` | Add eligible staging to the existing frame work predicate. |
| `.../buildingactivation.lua` | Add pure staging-work query and bounded scalar pending-status counters. |
| `server/tests/test_buildingscheduler.py` | Mandatory real-LuaJIT governor/controller/adapter/roads tests with deterministic engine/far-work doubles. |
| `tools/test_building_overlay.ps1` | Include the new scheduler tests in the existing pinned local runner. |
| `tools/stage6a/diagnostics.lua` | Add the opt-in read-only progress snapshot; existing observations remain. |
| `tools/prepare_stage6a.py` | Add stage 6C, port 47625, separate mod name, new guide and explicit governor packaging check. |
| This guide and `LIVE_OSM_BUILDINGS.md` | Analysis, test evidence and continuation procedure. |

No governor, road/terrain generation, native, protocol, store, activation-adapter,
retention or server orchestration implementation was changed.

## Test evidence

The tests execute the real `governor.lua`, `buildingactivation.lua`,
`buildingadapter.lua` and `roads.lua` under the pinned Lupa/LuaJIT runtime. They
extract and execute the actual `beginFrame(...)` statement from `core.lua`, so
forgetting the integration call fails the tests. The full engine-dependent
`core.onUpdate()` is not executed. Mesh construction, physics/collision reloads
and ordinary far-work consumption are deterministic doubles; they do not prove
real engine imports or contacts. Both the adapter's `os.clock` and the governor's
stopwatch use the same simulated clock. One import costs 1.15 ms.

`scheduler-reproduction.json` records an 80-target, three-batch case with the old
predicate for **18,000 updates / 300 simulated seconds**: zero imports and the
unchanged negative balance. Only the frame predicate is then switched. No tokens
are reset. The first import occurs after **17 updates**, and the simulated
controller reaches acknowledgement after **100 updates**. All 80 objects are
imported. After idle frames, the budget returns to zero. This is simulation evidence,
not a BeamNG activation or collision result. The trace is capped at 96 snapshots.

Nine scheduler tests cover:

- Idle ordinary queues, zero budget and the observed negative debt; multi-target
  staging, partial progress, recovery and return to idle.
- Normal and loading budgets, token caps and per-frame spending limits.
- Paused or inactive staging; incomplete batches and waiting vehicle-safety checks.
- Cancellation, anchor supersession, disconnect and partial import failure after
  staging has begun; existing road queues retire those temporary objects.
- Heavy/quiet frames and continuing engine debt: neither is bypassed.
- Paused collision-ready and retiring phases; no activation acknowledgement while
  paused, eventual governed cleanup/reload fences after unpausing, road identity kept.
- Ordinary queued road work and far-work coexistence, with the same budget/state
  trace as the legacy predicate whenever ordinary streaming keeps work available.
- Bounded read-only progress queries.

The broader existing regression runner and UI checks are also run. Actual final
counts, logs and artifact hashes are in `.venv/stage6c/verification-results.json`.
The run contains **557 tests across 23 suites: 551 passed, 4 skipped, 1 failure
and 1 error**. All **9 scheduler tests**, **34 UI checks** and **6 development
preparation tests** pass. The final scheduler recheck includes the stronger partial
staging and paused-retirement cases. Archive verification checks 346 file hashes,
39 required components, 128 Python parses and 68 LuaJIT compilations. The packaged
backend smoke prepares 80 targets and six meshes and reaches `activation_scheduled`
without claiming a game acknowledgement.
The established unrelated road-verge assertion (`9 != 0`) and obsolete cache enum
regex (`None.group`) remain separately reported; they were not changed here.

Reproduce without network/bootstrap or a game:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/test_building_overlay.ps1
node tools/test_building_ui.mjs
& '.venv/overlay-tests/python/python.exe' -B -m unittest discover -s server/tests -p test_buildingscheduler.py -v
& '.venv/overlay-tests/python/python.exe' -B -m unittest discover -s tools/stage6a -p test_preparation.py -v
```

## Fresh Stage 6C kit

Create only in a new destination; the preparer refuses existing directories:

```powershell
& '.venv/overlay-tests/python/python.exe' -B tools/prepare_stage6a.py --stage 6C --output .venv/stage6c/development-kit
```

Exact root: `C:\Users\ULTRAPC\source\BeamNG.Earth\.venv\stage6c\development-kit`.

- `build/BeamNG.Earth-1.7.2-beta.zip`: existing packager's complete source archive.
- `beamng_earth_stage6c.zip`: matching mod, including the fixed core/controller.
- `Start-Synthetic-Companion.ps1`: companion on **127.0.0.1:47625**.
- `source/`, `runtime/`, `SCENARIO.json`, `synthetic-overpass.json`, `docs/`:
  packaged source, pinned portable Python, the unchanged synthetic scene and guides.
- `package-audit.json`, `runtime-audit.json`, `smoke-results.json`: source/archive
  identity, 39 required component checks including governor, native/import/syntax
  checks and backend-only mesh preparation/scheduling evidence.

The compatibility marker is still named `STAGE6A_WORKSPACE.json`, but identifies
**6C**, the checkpoint/diff and port 47625. Legacy `_stage6a/diagnostics.lua` and
`stage6a-*.json` observation names are relative to the **new** game profile.
They never point into an older kit. The automatic smoke uses disposable random
`smoke-sandboxes/` folders; it does not create the future manual `run/` profile.

All Stage 6A/6B archives, profiles, logs, caches, journals and held resources remain
untouched. The original archive hashes are checked again after preparation.
The controlled draw ranges and synthetic providers are reused. The known default
draw-distance 128-target discovery limit, missing normal-online dependencies and
unverified geographic/process-worker/frozen startup remain outside this fix.

## Exact continuation procedure — user actions only

1. Save the stalled Stage 6B console output and companion logs. Exit BeamNG.drive
   completely, then stop only its companion. Do not hot-replace the mod, reload the
   extension/controller, clear caches, force collision reloads, change token values
   or delete held files. The controller singleton retains state across Lua reloads;
   this test requires a fresh game process and profile. Keep all older kits intact.
2. In PowerShell at the repository:

   ```powershell
   $stage6cKit = (Resolve-Path '.venv/stage6c/development-kit').Path
   $stage6cParent = Join-Path $stage6cKit 'run/profile'
   $stage6cProfile = Join-Path $stage6cParent 'current'
   $stage6cExe = Read-Host 'Full path to BeamNG.drive executable, not its launcher'
   & $stage6cExe -userpath $stage6cParent -bngeport 47625
   ```

   First launch without a mod, decline migration/import, and check
   `print(FS:getFileRealPath("/"))` in **GE Lua**. It must be exactly the new `$stage6cProfile`
   (allow slash/case normalization). Otherwise stop. Exit before copying the mod.
   Do not edit persistent Steam options, startup files or normal user folders.
3. After verification, stage the new mod only into that new profile:

   ```powershell
   $stage6cAudit = Get-Content -LiteralPath (Join-Path $stage6cKit 'package-audit.json') -Raw | ConvertFrom-Json
   $stage6cMod = Join-Path $stage6cKit 'beamng_earth_stage6c.zip'
   if ((Get-FileHash -LiteralPath $stage6cMod -Algorithm SHA256).Hash.ToLowerInvariant() -ne $stage6cAudit.game_mod_sha256) { throw 'Mod hash mismatch' }
   $stage6cMods = Join-Path $stage6cProfile 'mods'
   New-Item -ItemType Directory -Force -Path $stage6cMods | Out-Null
   $stage6cDestination = Join-Path $stage6cMods 'beamng_earth_stage6c.zip'
   if (Test-Path -LiteralPath $stage6cDestination) { throw 'Existing destination retained; use a fresh kit/profile' }
   Copy-Item -LiteralPath $stage6cMod -Destination $stage6cDestination
   ```

   Do not copy old settings, overlays, journals or caches. No installed mod is replaced.
4. In another PowerShell window, run the new kit's companion yourself:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File "$stage6cKit/Start-Synthetic-Companion.ps1"
   ```

   Set `$stage6cKit` again in that window if needed. Confirm **STAGE 6C SYNTHETIC
   ONLY**, port **47625** and the new profile. Stop on a port/path mismatch. Start
   the game yourself with the same one-session arguments and recheck its user path.
5. Load BeamNG.Earth with one car, Building draw distance **Normal**, and use GE Lua:

   ```lua
   beamngEarth_core.teleport(39.29008564698664, -76.5842096724785)
   ```

   Wait for initial streaming, park and leave physics unpaused. Select the same
   approximately 230 × 135 m OSM refresh bounds `[W,S,E,N]`:
   `[-76.5854615264661,39.29215733555047,-76.58279539789308,39.29337328507307]`.
   Fetch once: expect 1 addition, 1 replacement, 2 unchanged, 0 conflicts and
   1 Overture-only retained. Review without acceptance first; nothing should change.
   All data is injected locally; no endpoint/credentials or live OSM call is needed.
6. Before explicit acceptance, load the development observer and capture baseline:

   ```lua
   print(type(beamngEarth_core._buildingActivation.hasWork)) -- must print function
   s6 = dofile('/_stage6a/diagnostics.lua')
   s6.capture('before')
   s6.progress()
   ```

   Click **Accept and apply these buildings** once. Remain stationary, keep the
   player outside the selected area, and unpause simulation. Do not refetch.
7. Sample progress a few times, rather than running an unbounded console loop:

   ```lua
   s6.progress()
   -- Optional evidence file, inside the new verified user profile only:
   jsonWriteFile('/stage6c-progress.json', s6.progress(), true)
   ```

   During staging, `building_work` should be true. When ordinary work ends, `b`
   should recover while staging remains eligible and tokens should recover from
   initial debt; target/object cursors should advance. Check active anchor/unpaused
   simulation and heavy/engine-debt diagnostics if they do not. Do not force tokens,
   increase budgets, bypass imports or reload collision to make the test pass.
8. Require actual UI/controller acknowledgement for the current view, after the
   observed ready/retiring fences. Then verify visible A is wider/taller, C appears
   once, B/D stay, and compare unchanged road/terrain objects:

   ```lua
   s6.capture('after')
   s6.compare()
   s6.rays()
   s6.progress()
   ```

   Once complete, `building_work` must become false. After any ordinary cleanup
   drains, `roads_work`/`farfield_work` should be false and `b` should decay toward
   zero. The simulated 17/100 update counts are not an actual-engine deadline.
9. Only after acknowledgement, approach slowly to test real contact against C and
   A's extended wall. Confirm visual/physical agreement and no stale/duplicate
   collision. Repeat disable/enable/rollback from the original viewpoint as in
   [the earlier synthetic procedure](STAGE6A_MANUAL_TEST.md), substituting this
   **6C kit/profile/mod/port**. Collect raycast/contact evidence; a scheduled or
   simulated status is not a collision pass.
10. If progress stalls again, save several timestamped snapshots, the new profile's
    `beamng.log`, UI state/screenshots and `run/logs/events.jsonl` plus
    `companion-console.log`. Preserve the complete new cache/journals. On cleanup,
    exit both processes first and rename/preserve **only this kit's `run/`**;
    never delete uncertain live files or change older kits. Normal launching
    without these session overrides returns to the normal profile.

**Stop after preparing the fix and this kit. Actual engine activation and collision
have not been retested or established by Codex.**
