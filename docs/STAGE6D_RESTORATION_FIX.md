# Stage 6D: building restoration after teleport

Starting checkpoint: `976a5f37cdee24d872ecaa4bc3d275904b90296f`, branch
`feature/live-osm-buildings`; tracked working tree initially clean.
The user established Stage 6C activation, raycast changes and physical contact
in BeamNG.drive 0.40.1, followed by failed restoration after teleport.
**Engine teleport restoration with this fix remains untested.** Codex did not
install into or launch the game, fetch live OSM, change road/terrain generation
or Rust, alter retention safeguards, commit or push.

## Cause and evidence

`earthd.Connection.commit_anchor()` changes `active` and calls the live service
before trackers reset. Roads and far buildings reset on their next pose;
skyline receives its new pose only after ordinary far-building streaming finishes.
The mixed-anchor interval is real. Discovery correctly rejects it.

The old retry signature contained only a union of tile keys. Tracker identity,
anchor, separate pending/ready memberships, futures, retries and rendering
settings were absent. A same-location anchor reset, or a pending tile becoming
ready without changing this union, could therefore suppress recovery forever.

The final historical rejection is reproduced from the retained catalog:
`bldfar_3_-2_1.json` references `bldfar_3_-2_1.cdae`, but the generation contains
only the JSON. `revisit.SHAPE_RE` required compact `"shape":"..."`; legacy
far/skyline writers use Python's `"shape": "..."`. Their revisit ZIPs therefore
kept the JSON without its CDAE. On returning to the same anchor location, a cache
hit restored a new-anchor JSON but no mesh. Catalog scanning hashed only files
present, so it considered this internally incomplete generation valid. Activation
then correctly rejected `mesh is not in the checked generation manifest`.
This explains why retrying while the area settled did not fix the final failure.

There is also a race: a new current target can appear between successful
preparation and activation. The actual Manager rejects it with
`prepared targets/settings do not match the current world`. The old service
called the legacy error-envelope API and replaced **any** rejected envelope with
`Rejected: current generation could not be activated`, discarding the real
validation. A deterministic regression reproduces that precise target-validation
failure using the real Service, store, Coordinator, native generation and Manager.
It now recovers by preparing the additional target, without weakening validation.

Accepted-area guards correctly suppress unprepared ordinary building geometry.
After a suppressed retry this also prevented baseline buildings from returning.
There was no safe fallback or explicit missing-collision warning. Additionally,
old-frame cancellation messages remained in an already-returned worker output
list; they now return through the real socket owner with their original fences.

The original generic log alone could not identify this guard. Read-only inspection
of the retained Stage 6C generation and activation of a **copy** with the old
Manager reproduce the exact missing-manifest-mesh rejection. Old `_validate()`
still passes that catalog because all *listed* files are intact; that does not
prove all referenced files exist. A different-frame old socket replay can succeed
on a cache miss, explaining the timing/location sensitivity. The new real socket
same-location replay reproduced the same missing far mesh before this cache fix.

Read-only investigation evidence: `.venv/stage6d/wire-replay.json`,
`wire-replay.log`, `saved-validation.json`, `saved-validation.log`,
`historical-mesh-references.jsonl`, `historical-activation.json` and
`historical-activation.log`. The first Stage 6D preflight build and its failed
replay remain in `development-kit/` and `restoration-socket.log`; use the corrected
**`development-kit-r2/`** below for manual testing.
Historical overlays/catalogs were copied into new sandboxes before revalidation.
Original Stage 6A/B/C data, held files and test evidence were not modified.

## Recovery contract

- Wait for tracker anchors to agree and near road streaming to finish. Include
  tracker identity, aid, separate tile sets/futures and rendering settings in
  the bounded retry signature. Drain context invalidations before capturing a
  new world. A completed tile wakes recovery even if its key was already present.
- Recognize JSON whitespace when recording cached shapes. Reject incomplete
  far/skyline cache entries as misses, without deleting them. Scan the full
  prepared JSON-to-mesh reference set before publishing or reusing a generation;
  an intact JSON without its mesh cannot be reused. Existing incomplete catalog
  entries rebuild through the normal generator, leaving held generations intact.
- Retry a stable transient failure at most three times, with 1/2-second spacing.
  A changed world can be reconsidered. Existing native generation reuse, one
  authority worker, 128 targets, 512 cells and byte/slot/transaction quotas remain.
- After a permanent failure or exhausted transient attempts, try one validated
  **original-baseline transaction** for this accepted state/world frame. A failed
  engine ACK also requests baseline recovery. Use the same Coordinator, Manager,
  mesh slots, collision retirement, safety checks and engine acknowledgement.
- A private read-only store lens supplies a disabled render snapshot but keeps
  the real durable ABA token, revision, entries, provenance and baseline checks.
  The durable overlay is never disabled or rewritten. Catalog access uses the
  existing lock; the lens is restored in `finally`, including ACK validation.
  Normal map, road and terrain consumers retain the original store.
- An acknowledged fallback explicitly reports `baseline_active`,
  `activated_enabled=false`, accepted data still enabled, and a cause code. It
  does not alternate back to the failed overlay automatically in the same frame.
  Explicit local retry or a new anchor/state may try that overlay again.
- If baseline validation or its engine handoff fails, stop retries for unchanged
  inputs and visibly report **blocked: stop before driving**. Never install
  unvalidated geometry to hide an error. Collision cannot be guaranteed if the
  engine or validated resources are unavailable; the user must stop and retain
  diagnostics. Storage/context scans also back off and stop after three errors.

Diagnostics contain fixed messages/codes, current anchor, attempt count, and
three trackers' anchor/counts. No capability, endpoint or arbitrary exception
text enters public status. Vue safely normalizes the added status. Lua notices
are deduplicated by mode/code/anchor, rather than emitted every status poll.
The development event log records exact local errors and validation codes without
logging command capabilities. No automatic fetching/acceptance, forced import,
collision reload, governor alteration or resource release shortcut was added.

## Files and automated evidence

| File | Change |
| --- | --- |
| `server/buildingactivation.py` | Fixed rejection-code mapping; validation predicates unchanged. |
| `server/revisit.py` | Whitespace-safe shape extraction and building-only cache completeness checks; no road/terrain generation change. |
| `server/buildingrefresh.py` | Check referenced meshes before generation publication/reuse; retain incomplete old generations while rebuilding. |
| `server/buildinglive.py` | Ready-state/signature checks, context/cancel ordering, precise errors, bounded retries, validated baseline lens and diagnostics. |
| `server/tests/test_buildingrestoration.py` | 14 orchestration/native/Lua tests: native near/far/skyline revisit, missing-mesh receipt, same/different location, re-entry, target race, incomplete streaming, repeated anchors, disconnect/cancellation, retry limits, fallback failures, engine ACK failure, ABA safety, unchanged roads and old collider removal. |
| `server/tests/test_revisit.py` | Two cache regression cases; close fixture sidecar descriptors before Windows temporary-directory cleanup. |
| `mod/beamng_earth/lua/ge/extensions/beamngEarth/buildingui.lua` | Bounded, deduplicated restoration notices; no world mutation. |
| `mod/beamng_earth/ui/ui-vue/mods/beamngEarth/buildingRefresh.js` | Normalize restoration diagnostic safely. |
| `mod/beamng_earth/ui/ui-vue/mods/beamngEarth/EarthBuildingRefresh.vue` | Visible waiting/baseline/blocked diagnostic. |
| `tools/test_building_ui.mjs` | Four extra real Vue renderer checks, including malformed diagnostics and no emitted commands. |
| `tools/test_building_overlay.ps1` | Include restoration regression suite. |
| `tools/stage6a/serve.py` | Whitelisted diagnostic logging and opt-in restoration replay. |
| `tools/stage6a/restoration_smoke.py` | Actual Server/socket anchor lifecycle; explicitly simulated ACKs, never engine evidence. |
| `tools/prepare_stage6a.py` | Separate 6D kit/port/mod; package new helper/guide. |
| This guide and `docs/LIVE_OSM_BUILDINGS.md` | Root cause, limits, evidence and manual continuation. |

Exact execution results are in `.venv/stage6d/verification-results.json` and
adjacent regression/UI/preparation logs. The established unrelated failures remain
`test_roads.PaintRoadsTest.test_bare_verge_beside_roads` (9 != 0) and
`test_protocol_agreement.CacheAgreementTest.test_cache_in_the_mod` (obsolete
regex returns None). No tests are weakened to conceal these failures.

Reproduce without network/bootstrap:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/test_building_overlay.ps1
node tools/test_building_ui.mjs
& '.venv/overlay-tests/python/python.exe' -B -m unittest discover -s tools/stage6a -p test_preparation.py -v
```

## Fresh isolated kit

Exact corrected root: `C:\Users\ULTRAPC\source\BeamNG.Earth\.venv\stage6d\development-kit-r2`.

- `build/BeamNG.Earth-1.7.2-beta.zip`: existing packager's complete source archive.
- `beamng_earth_stage6d.zip`: matching game mod, with restoration notices.
- `Start-Synthetic-Companion.ps1`: companion on **127.0.0.1:47626**.
- `source/`, `runtime/`, `SCENARIO.json`, `synthetic-overpass.json`, `docs/`:
  packaged source, portable pinned Python and unchanged four-building fixture.
- `package-audit.json`, `runtime-audit.json`, `smoke-results.json`,
  `restoration-smoke-results.json`: component/import/syntax/native/socket evidence.
  Restoration smoke ACKs are **simulated**; neither smoke proves engine collision.

```powershell
& '.venv/overlay-tests/python/python.exe' -B tools/prepare_stage6a.py --stage 6D --output .venv/stage6d/development-kit-r2
$stage6dKit = (Resolve-Path '.venv/stage6d/development-kit-r2').Path
& "$stage6dKit/runtime/python.exe" -B "$stage6dKit/verify.py" --workspace $stage6dKit
& "$stage6dKit/runtime/python.exe" -B "$stage6dKit/serve.py" --workspace $stage6dKit --synthetic-only --port 47626 --smoke
& "$stage6dKit/runtime/python.exe" -B "$stage6dKit/serve.py" --workspace $stage6dKit --synthetic-only --port 47626 --restoration-smoke
```

The preparer refuses an existing destination. Compatibility names
`STAGE6A_WORKSPACE.json` and `_stage6a/diagnostics.lua` remain; metadata says 6D.
Smokes use new random `smoke-sandboxes/`, ephemeral loopback ports and no manual
`run/` profile. Manual use has dedicated port 47626. Public HTTP/DNS is blocked.
All older profiles, caches, logs, archives and held resources remain retained.
Existing limits persist: default production ranges exceed 128 targets; this kit
uses the earlier reduced development ranges. Normal-online dependencies
duckdb/fsspec/certifi/truststore are absent. Frozen, geographic/process-worker
and normal online startup are outside this synthetic test.

## Manual continuation: user actions only

1. Retain the failed 6C console, game log, companion events, cache and journals.
   Exit the game completely and stop only its companion. Do not hot-reload Lua,
   clear caches, delete held resources or overwrite an older kit.
2. From PowerShell at the repository, launch a **mod-free isolated profile**:

   ```powershell
   $stage6dKit = (Resolve-Path '.venv/stage6d/development-kit-r2').Path
   $stage6dParent = Join-Path $stage6dKit 'run/profile'
   $stage6dProfile = Join-Path $stage6dParent 'current'
   $stage6dExe = Read-Host 'Full path to BeamNG.drive executable, not its launcher'
   & $stage6dExe -userpath $stage6dParent -bngeport 47626
   ```

   Decline migration/import. In **GE Lua**, `print(getUserPath())` must be exactly
   `$stage6dProfile` (allow slash/case normalization). Stop otherwise. Exit the
   game before copying the mod. Do not change Steam options or normal folders.
3. Copy only the new audited mod into the verified new profile:

   ```powershell
   $stage6dAudit = Get-Content -LiteralPath "$stage6dKit/package-audit.json" -Raw | ConvertFrom-Json
   $stage6dMod = Join-Path $stage6dKit 'beamng_earth_stage6d.zip'
   if ((Get-FileHash -LiteralPath $stage6dMod -Algorithm SHA256).Hash.ToLowerInvariant() -ne $stage6dAudit.game_mod_sha256) { throw 'Mod hash mismatch' }
   $stage6dMods = Join-Path $stage6dProfile 'mods'
   New-Item -ItemType Directory -Force -Path $stage6dMods | Out-Null
   $stage6dDestination = Join-Path $stage6dMods 'beamng_earth_stage6d.zip'
   if (Test-Path -LiteralPath $stage6dDestination) { throw 'Existing destination retained; use a fresh kit/profile' }
   Copy-Item -LiteralPath $stage6dMod -Destination $stage6dDestination
   ```

   Do not import old settings, overlays, caches or journals.
4. In a separate PowerShell window at the repository:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .venv/stage6d/development-kit-r2/Start-Synthetic-Companion.ps1
   ```

   Require **STAGE 6D SYNTHETIC ONLY**, port **47626**, and the new profile.
   Relaunch the game with step 2's arguments and recheck its user path. Load
   BeamNG.Earth with one car and Building draw distance **Normal**.
5. In GE Lua, return to the safe original viewpoint:

   ```lua
   beamngEarth_core.teleport(39.29008564698664, -76.5842096724785)
   ```

   Wait for ordinary streaming, park outside the building area, leave physics
   unpaused. Select bounds `[W,S,E,N]` in the OSM panel:
   `[-76.5854615264661,39.29215733555047,-76.58279539789308,39.29337328507307]`.
   Fetch **once**: 1 addition, 1 replacement, 2 unchanged, 0 conflicts,
   1 Overture-only retained. Review; then explicitly **Accept and apply** once.
6. Before acceptance load the observer/capture baseline; after acknowledgement
   compare while stationary:

   ```lua
   s6 = dofile('/_stage6a/diagnostics.lua')
   s6.capture('before') -- before acceptance
   -- After explicit acceptance and current activation/retirement acknowledgement:
   s6.progress()
   s6.capture('after')
   s6.compare()
   s6.rays()
   ```

   Expect A about 6 m and C about 5 m, A wider/taller, C once, B/D retained.
   Road/terrain identities must remain unchanged during the stationary handoff.
   Only after acknowledgement approach slowly for contact against all four.
7. From the building area teleport back with step 5's command, reproducing the
   failure trigger. **Do not disable, rollback, retry or refetch.** Automatic
   restoration should wait through `trackers_settling`/`near_streaming`, then
   prepare and activate the new anchor. Any transient validation must expose its
   code and settle within bounded retries. Wait for a new current acknowledgement.
8. Save a few observations after each teleport, without an unbounded console loop:

   ```lua
   dump(beamngEarth_settings.uiBuildingsState())
   s6.progress()
   s6.rays()
   jsonWriteFile('/stage6d-after-teleport-status.json', beamngEarth_settings.uiBuildingsState(), true)
   ```

   Require `game_activated=true`, `activated_enabled=true`, same accepted revision,
   correct A/C rays and visuals; then retest slow contact. Road/terrain object IDs
   naturally change on anchor replacement: compare IDs only within one frame.
9. Repeat same-viewpoint teleport, teleport outside the scene (for example
   `39.30008564698664,-76.5842096724785`) and return. Repeat two teleports quickly,
   then one while paused; unpause afterward and require only the latest frame to
   activate. Do not infer collision success from paused status. Do not refetch.
10. If fallback occurs, require `baseline_active`, `activated_enabled=false`,
    accepted data still enabled, A about 18 m, no C hit within 35 m, B/D retained.
    This is safe fallback evidence, **not an overlay-restoration pass**. Preserve
    the cause. If `blocked`, geometry is absent, collision uncertain or progress
    stops, stop driving. Do not force collision reload, budgets or extension
    reloads, or delete files. Capture status before any separately diagnosed retry.
11. Retain `$stage6dProfile/beamng.log`, screenshots/console, `run/logs/events.jsonl`,
    `companion-console.log` and the complete cache/journals. Capture teleport
    results before restart/disconnect experiments. For cleanup exit both processes
    and preserve only this kit's `run/` under an evidence name. Do not delete held
    files. Normal launching without session arguments returns to the normal
    profile; never put this development mod into that profile.

Codex can execute source, native, Lua, Vue and loopback checks. The user must
operate BeamNG.drive to verify its UI host, teleport lifecycle, visuals, collision
reloads, raycasts and contact. **Real-engine Stage 6D restoration remains pending.**
