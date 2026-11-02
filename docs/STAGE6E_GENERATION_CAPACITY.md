# Stage 6E: proof-based generation capacity for repeated re-anchoring

Checkpoint `368f6ed9885db140d401c0fd07a33b67089a8551`, branch
`feature/live-osm-buildings`; working tree clean before this work.
The user confirmed actual Stage 6D geometry/collision restoration through normal,
repeated, outside-area-return and rapid teleports. A later paused teleport hit
the generation count budget and correctly activated a validated original baseline.
Those are real user observations. The subsequent **Stage 6E user-operated run
completed 16 successful restoration cycles**, including paused teleport, in
BeamNG.drive 0.40.1. A remained about 6 m and C about 5 m in collision rays. At
512 generations, 80 old generations were reclaimed and 80 new ones created,
with zero work directories remaining. Transitional retiring states resolved
automatically. The user reports both the game and companion are closed.
These engine observations are supplied by the user; Codex did not launch the game.
All ACKs/releases in Codex's automated tests remain simulated.

## Confirmed cause

Read-only inspection of the retained Stage 6D cache confirmed:

| Quantity | Count |
| --- | ---: |
| Published generation directories | 510 |
| Private work directories | 0 |
| Distinct current catalog generations | 80 |
| Published generations absent from the catalog | 430 |
| Those 430 referenced by a protected journal transaction | 0 |
| Activation ownership journals | 1 |

The journal's one orphaned transaction references catalog roots, not the 430 old
generations. Metadata hashes were identical before/after inspection. No collector,
writer, cleanup or game process was run against that cache. Evidence and hashes:
`.venv/stage6e/retained-evidence.json`.

`Service.prepare()` called `buildingretention.collect(..., limit=8)` once before
discovering its new batch. A re-anchor can generate about 80 immutable targets.
After replacing the catalog, superseded unheld targets become collectible, but
reclaiming only eight cannot keep pace. Repeated batches accumulate despite
available proof that old generations are unused. This is the primary cause,
rather than an unbounded work queue, a corrupt journal or unexplained live holds.

There was also an admission gap: `Coordinator._usage()` checked the existing
published + private count before a batch and again at final publication. New
private directories were created between those checks. For example, admitting
80 jobs after removing only eight from 510 could temporarily create 582
directories before rejecting the batch. A quota check at publication is too late
to prevent that temporary excess. A local regression reproduces exhaustion under
the old eight-item policy and confirms the new admission guard fails before work.

## Small, bounded correction

1. Plan which target entries actually require rebuilding. Reused entries need
   zero new directories. No quota is increased: defaults remain **512 artifacts**,
   **128 registered targets**, existing byte budgets, slots and transaction limits.
2. The live service supplies an internal optional `reclaim(required)` callback
   to the existing preparation API, for both overlay and baseline preparation.
   It calls `buildingretention.make_room`, which measures demand and invokes the
   existing collector in passes of at most **64 per category**. It stops once the
   required count fits, no count-bearing removal made progress, or the bounded
   pass budget is exhausted (at most eight passes at the default artifact limit).
   The observed 510/80 fixture removes **78** old generations in two passes.
3. Every pass reuses the existing roots/proofs: current catalog, activation
   journals, active or held transactions, complete fenced inventory requirements,
   work leases, owner identity and corruption handling. Automatic admission passes
   **no inventory proof**; it cannot manufacture release evidence, retire an
   uncertain engine owner or remove an older-session journal on its own.
4. After collection, the Coordinator rechecks its sequence and capacity under the
   catalog OS lock. It creates the entire new batch's empty work directories with
   live OS work leases before starting any worker. These are count reservations
   visible to competing threads/processes. Count cannot exceed 512 while a second
   coordinator admits another batch. Collection never runs under a nested catalog
   lock, and a racing caller can fail safely rather than overbook capacity.
5. Reservations use the existing cancellation/publication lifecycle. Active jobs
   keep leases, canceled unstarted jobs release their private directories, and
   publication moves the same directories into immutable storage under the lock.
   Catalog CAS, overlay ABA token, checksums, identity and engine fences remain.

This guarantees admission/count capacity, not an operating-system disk quota.
Existing per-job, manifest and aggregate byte checks are unchanged; native writers
are not intercepted and no filesystem quota was introduced. Full leased batches
may be rejected under contention even if a later retry would fit. This conservative
behavior is intentional. Already over-budget or invalid inventories fail closed.

If enough ownership is genuinely uncertain, or a journal is corrupt, admission
still refuses the batch without deleting those resources. Stage 6D's validated
baseline fallback remains; if that too cannot be admitted/activated the existing
visible blocked warning requires the user to stop. Saved overlays remain enabled.
No OSM fetching, road/terrain generator, Rust, Lua, collision, governor, protocol,
quota constant or automatic inventory handshake was changed.

## Changes and regression evidence

| File | Change |
| --- | --- |
| `server/buildingretention.py` | Bounded demand-based `make_room`; existing `collect` proof/deletion rules untouched. |
| `server/buildingrefresh.py` | Return usage counts; plan reuse, admit and lease-reserve the full new batch before workers; existing publication/cleanup path. |
| `server/buildinglive.py` | Replace unconditional eight-item sweep with demand callback; keep per-call diagnostics without masking early errors/cancellation or publishing an obsolete frame's report. |
| `server/earthd.py` | Forward the optional preparation callback; existing callers retain default behavior. |
| `server/tests/test_buildingcapacity.py` | Thirteen capacity/lifecycle/diagnostic regressions with real catalog/lease logic and synthetic receipts; one case uses native preparation and real activation ownership. |
| `tools/test_building_overlay.ps1` | Include capacity suite alongside existing regressions. |
| `tools/stage6a/restoration_smoke.py` | Bounded 3..16 anchor replay, simulated exact slot releases and deferred ACKs; record capacity/counts. |
| `tools/stage6a/test_preparation.py` | Eight preparation/tool checks, including rejection of baseline fallback and unconfirmed revisions in the smoke test. |
| `tools/stage6a/serve.py` | Optional replay length and whitelisted capacity/removal event diagnostics. |
| `tools/prepare_stage6a.py` | Fresh 6E kit, mod, guide and port 47627. |
| This guide and `docs/LIVE_OSM_BUILDINGS.md` | Evidence, architecture, limitations and manual continuation. |

The capacity suite covers twelve 80-target transitions; 510 existing directories;
zero rebuilds and exact quota fit; catalog replacement; held terminal transactions;
incomplete release proof; genuine exhaustion; corruption; crash/restart with busy,
legacy and proven-abandoned work; cancellation of a fully reserved 80-job batch;
two competing Coordinators; and a deferred native activation whose holds survive
until exact complete release evidence. No test consults the Stage 6D run cache.
Existing Lua pause/controller, collision retirement, fencing and restoration tests
continue to run. Synthetic receipts test orchestration; they are not engine meshes.

Actual results and logs are recorded in `.venv/stage6e/verification-results.json`
and adjacent `regression-tests.log`, `capacity-tests.log`, `ui-tests.log`,
`preparation-tests.log`, package and socket logs. Established unrelated failures:
`test_roads.PaintRoadsTest.test_bare_verge_beside_roads` (9 != 0) and
`test_protocol_agreement.CacheAgreementTest.test_cache_in_the_mod` (obsolete regex).
No test is weakened to conceal them.

The final pre-merge review also reproduced and fixed an early-error diagnostic
masking bug: the authority can fail before its preparation callback initializes
a report. Reports now live in the individual call, preserve the original error
and cancellation, and are published only for the current connection/frame.
The smoke helper now rejects baseline packets, disabled activations and mismatched
or absent accepted revisions. Five new tests cover these cases. Review results
are under `.venv/stage6e/premerge-review/`; earlier kit/run artifacts are retained
unchanged. The reviewed source includes these narrow fixes beyond the previously
engine-tested kit; its successful activation, meshing and collection path is unchanged.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/test_building_overlay.ps1
node tools/test_building_ui.mjs
& '.venv/overlay-tests/python/python.exe' -B -m unittest discover -s tools/stage6a -p test_preparation.py -v
```

## Fresh Stage 6E kit

Root: `C:\Users\ULTRAPC\source\BeamNG.Earth\.venv\stage6e\development-kit`.
Manual companion: **127.0.0.1:47627**. Manual profile:
`run/profile/current`, cache `run/cache`, event log `run/logs/events.jsonl`.

The kit contains `beamng_earth_stage6e.zip`,
`build/BeamNG.Earth-1.7.2-beta.zip`, `Start-Synthetic-Companion.ps1`, portable pinned
`runtime/`, packaged `source/`, the unchanged `SCENARIO.json` and fixture, and guides.
`package-audit.json` records file hashes; `runtime-audit.json`, `smoke-results.json`
and `restoration-smoke-results.json` record automated checks. Compatibility names
`STAGE6A_WORKSPACE.json` and `/_stage6a/diagnostics.lua` remain; metadata says 6E.
Smokes use new random `smoke-sandboxes/` and ephemeral loopback ports, never the
manual `run/` profile. Long replay ACKs, pauses and slot releases are simulated.

```powershell
& '.venv/overlay-tests/python/python.exe' -B tools/prepare_stage6a.py --stage 6E --output .venv/stage6e/development-kit
$stage6eKit = (Resolve-Path '.venv/stage6e/development-kit').Path
& "$stage6eKit/runtime/python.exe" -B "$stage6eKit/verify.py" --workspace $stage6eKit
& "$stage6eKit/runtime/python.exe" -B "$stage6eKit/serve.py" --workspace $stage6eKit --synthetic-only --port 47627 --smoke
& "$stage6eKit/runtime/python.exe" -B "$stage6eKit/serve.py" --workspace $stage6eKit --synthetic-only --port 47627 --restoration-smoke
& "$stage6eKit/runtime/python.exe" -B "$stage6eKit/serve.py" --workspace $stage6eKit --synthetic-only --port 47627 --restoration-smoke --reanchor-cycles 12
```

The preparer refuses an existing destination. Stage 6A/B/C/D kits, logs, profiles,
catalogs and held files are retained. Earlier archive hashes are checked; Stage 6D
catalog/journal/inventory are checked again read-only. Public HTTP/DNS is blocked.
Known earlier limits remain: reduced development ranges avoid the production
128-target discovery limit; normal-online dependencies are absent; normal online,
frozen and geographic/process-worker startup are outside this synthetic test.

## Exact real-engine continuation: user actions only

1. Preserve the Stage 6D fallback evidence. Exit the game completely and stop only
   that companion. Do not run Cleanup, delete cached generations/held files, copy
   old caches, hot-reload Lua or modify the old profile. Use a fresh game process.
2. In PowerShell at this repository, start a **mod-free** isolated profile:

   ```powershell
   $stage6eKit = (Resolve-Path '.venv/stage6e/development-kit').Path
   $stage6eParent = Join-Path $stage6eKit 'run/profile'
   $stage6eProfile = Join-Path $stage6eParent 'current'
   $stage6eExe = Read-Host 'Full path to BeamNG.drive executable, not its launcher'
   & $stage6eExe -userpath $stage6eParent -bngeport 47627
   ```

   Decline migration/import. In GE Lua `print(FS:getFileRealPath("/"))` must exactly identify
   `$stage6eProfile` (slash/case normalization allowed). Stop otherwise. Exit.
3. Copy only the new audited mod into this verified new profile:

   ```powershell
   $stage6eAudit = Get-Content -LiteralPath "$stage6eKit/package-audit.json" -Raw | ConvertFrom-Json
   $stage6eMod = Join-Path $stage6eKit 'beamng_earth_stage6e.zip'
   if ((Get-FileHash -LiteralPath $stage6eMod -Algorithm SHA256).Hash.ToLowerInvariant() -ne $stage6eAudit.game_mod_sha256) { throw 'Mod hash mismatch' }
   $stage6eMods = Join-Path $stage6eProfile 'mods'
   New-Item -ItemType Directory -Force -Path $stage6eMods | Out-Null
   $stage6eDestination = Join-Path $stage6eMods 'beamng_earth_stage6e.zip'
   if (Test-Path -LiteralPath $stage6eDestination) { throw 'Existing destination retained; choose a fresh kit/profile' }
   Copy-Item -LiteralPath $stage6eMod -Destination $stage6eDestination
   ```

   Never copy it into the normal profile or change persistent Steam options.
4. In a second PowerShell window at the repository:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .venv/stage6e/development-kit/Start-Synthetic-Companion.ps1
   ```

   Require **STAGE 6E SYNTHETIC ONLY**, port **47627**, new profile. Relaunch the
   game with step 2's arguments, recheck the user path and load BeamNG.Earth with
   one car and Building draw distance **Normal**.
5. Teleport in GE Lua to the original safe viewpoint:

   ```lua
   beamngEarth_core.teleport(39.29008564698664, -76.5842096724785)
   s6 = dofile('/_stage6a/diagnostics.lua')
   ```

   Wait for ordinary streaming and park outside the building region, unpaused.
   Select `[W,S,E,N]` bounds
   `[-76.5854615264661,39.29215733555047,-76.58279539789308,39.29337328507307]`.
   Fetch once: 1 addition, 1 replacement, 2 unchanged, 0 conflicts, 1 Overture-only
   retained. Capture baseline with `s6.capture('before')` and `s6.rays()`, then
   explicitly **Accept and apply** once. Wait for current activation/retirement
   acknowledgement. Compare stationary road/terrain identities with
   `s6.capture('after'); s6.compare()`. Overlay rays: A about 6 m, C about 5 m.
6. Repeat the **entire** sequence below three times, targeting at least **16
   acknowledged restorations** after initial acceptance. Do not disable, rollback,
   refetch, retry or run Cleanup to make the test pass:
   - Approach the buildings only after acknowledgement; test slow contact with A,
     B, C and D. Teleport back to the original viewpoint and await restoration.
   - Repeat a same-viewpoint teleport and await the new current acknowledgement.
   - Teleport outside the area to `39.30008564698664,-76.5842096724785`, allow
     ordinary streaming to settle, then return to the original viewpoint and wait.
   - Issue two rapid teleports back to the viewpoint, then await only the newest
     anchor's activation. Superseded work must not activate later.
   - Pause simulation, teleport back to the viewpoint, inspect status while paused,
     then unpause and wait for current activation and collision retirement. Never
     infer collision success from a paused/deferred state. Do not accumulate an
     arbitrary number of paused anchors without allowing releases afterward.
   - Add acknowledged same-viewpoint teleports if fewer than 16 restorations were
     counted. New anchors must actually commit; merely polling is not a transition.
7. After each acknowledgement, require `game_activated=true`,
   `activated_enabled=true`, accepted `enabled=true`, stable accepted revision,
   no baseline warning, correct A/C rays and visible B/D. Save bounded snapshots
   under unique names in the new profile:

   ```lua
   dump(beamngEarth_settings.uiBuildingsState())
   s6.progress()
   s6.rays()
   jsonWriteFile('/stage6e-round1-after-pause.json', beamngEarth_settings.uiBuildingsState(), true)
   ```

   Use different round/step filenames. Compare road/terrain IDs only within a
   stationary handoff; anchor replacement naturally replaces their objects.
8. Inspect only the **new Stage 6E** generation store, read-only, in PowerShell:

   ```powershell
   $stage6eGenerations = Join-Path $stage6eKit 'run/cache/building-generations'
   $stage6ePublished = @(Get-ChildItem -LiteralPath (Join-Path $stage6eGenerations 'generations') -Directory).Count
   $stage6eWork = @(Get-ChildItem -LiteralPath (Join-Path $stage6eGenerations 'work') -Directory).Count
   [pscustomobject]@{ published=$stage6ePublished; work=$stage6eWork; total=$stage6ePublished+$stage6eWork }
   ```

   Sample after settled acknowledgement; during a rename, two separate directory
   enumerations are not an atomic snapshot. Expect total <=512 and zero work after
   completion. Published count can plateau at 512: collectible generations are
   removed on demand, rather than by age. In `run/logs/events.jsonl`, require at
   least one `generation_capacity.passes>0` and `removed_generations>8`. Capacity
   fields are the admission snapshot **before** reserving the new work batch.
   Confirm with post-teleport rays/contact that successful reclamation preserves
   current collision. Retain logs showing multiple collection cycles, not just one.
9. A ResourceLimit with true active/unknown holds remains legitimate; preserve its
   capacity/uncertainty diagnostic and do not delete anything. If fallback occurs,
   `baseline_active`, `activated_enabled=false`, accepted enabled, A about 18 m and
   C no hit within 35 m are baseline recovery, **not a Stage 6E success**. If blocked
   or collision is missing, stop driving. No forced reload, quota increase or
   resource deletion is an acceptable workaround. Capture status before any
   separately diagnosed local retry or reconciliation.
10. Retain the new profile's `beamng.log`, GE/UI captures, companion console,
    events, cache and journals. Exit both processes before cleanup. Preserve this
    kit's `run/` under an evidence name; never delete uncertain held files. Normal
    launching without the session arguments returns to the normal profile.

Codex executes repository/native/Lua/Vue and loopback regressions. Actual
pause/teleport, rendering, collision reloads, raycasts and contact are user-operated;
the completed 16-cycle Stage 6E engine result is recorded at the top of this guide.
No simulated acknowledgement is treated as engine collision evidence.
