# Stage 6A — isolated development build and first manual engine test

Prepared from clean `4d29161e84d331adc224af59744aa12a7317e9f0` on
`feature/live-osm-buildings`. Stage 6A prepares artifacts and exercises repository-local
server behavior. **BeamNG.drive has not been installed into, changed, or launched.
No real-engine result or live OSM retrieval is claimed.** No Rust, road-generation
source, production protocol, commit or push was changed.

The existing [Stage 5B2 architecture](LIVE_OSM_BUILDINGS.md) still applies. All fixture
providers, networking guards and restricted rendering ranges below belong to the
separate development companion, not the shipped production modules.

## Artifacts and reproducible preparation

Repository: `C:\Users\ULTRAPC\source\BeamNG.Earth`.

Final kit: `.venv/stage6a/development-kit/` beneath that repository:

| Artifact | Purpose |
| --- | --- |
| `build/BeamNG.Earth-1.7.2-beta.zip` | Unmodified source package built by the existing `tools/package.py`. |
| `source/BeamNG.Earth-1.7.2-beta/` | Verified extraction of that archive. The companion imports these server modules. |
| `beamng_earth_stage6a.zip` | Game mod subtree extracted from that package, plus one development observation helper. ZIP root contains `lua`, `ui`, `levels`, etc.; no extra wrapper directory. |
| `runtime/python.exe` | Copied pinned Windows x64 Python 3.12.10, NumPy 2.2.6 and Pillow 11.3.0 runtime. No system Python/install step is required. |
| `Start-Synthetic-Companion.ps1` | Explicit local development companion launcher. Does not find, install or start a game. |
| `serve.py`, `scenario.py` | Synthetic provider/transport and real server/worker/controller composition. |
| `diagnostics.lua` | Same helper also present at `/_stage6a/diagnostics.lua` in the mod ZIP. Observes objects/rays and writes development JSON evidence. Never reloads collision or moves objects. |
| `SCENARIO.json`, `synthetic-overpass.json` | Exact locations, footprints, expected counts, frozen source clock and complete synthetic API response. |
| `package-audit.json` | Archive hashes, per-file hashes, 38 required component checks, native/dependency audit and limitations. |
| `STAGE6A_WORKSPACE.json` | Required workspace marker and source checkpoint. |
| `docs/` | This procedure and the Stage 5B2 guide omitted by the ordinary release packager. |
| `smoke-results.json` | Packaged-runtime local-protocol smoke result. It explicitly reports `engine_tested: false` and `game_activated: false`. |

Create another fresh kit without overwriting any existing test state:

```powershell
Set-Location 'C:\Users\ULTRAPC\source\BeamNG.Earth'
& '.venv/overlay-tests/python/python.exe' -B tools/prepare_stage6a.py --output .venv/stage6a/another-development-kit
```

The preparer invokes the existing packager, validates every archive entry against
source, verifies CRC/path safety, extracts source, derives the mod ZIP, copies the
local runtime and adds the development-only supplement. It rejects an existing
output directory, paths outside this repository and links/junctions. It never calls
`install.py`, `start.py`, `launcher.py`, or a game executable.

## Packaging and startup audit

The existing package contains 346 files. All 38 explicitly required server, Lua,
Vue/JS, requirements, native binary and level-info components are present and match
the repository bytes. The full manifest is in `package-audit.json`. Settings live
in the packaged Lua/UI schemas; no endpoint, credential file or user-specific
`.beamng_earth.json` is required or included.

The existing packager intentionally excludes tests and includes only selected docs;
it does not include `LIVE_OSM_BUILDINGS.md`, this manual, development tools or a
Python runtime. The kit supplies these separately. The original source archive is
retained unchanged. The derived game ZIP adds only the observation helper.

The existing `earthfast.pyd` loads as version **114**, matching the server's minimum.
No native build occurred. The kit is Windows x64; the Linux binary in the source
package was not executed. The original server imports and `--help` can be checked
from the extracted package without invoking its installer or saved-path discovery.

The pinned local runtime lacks **DuckDB, fsspec, certifi and truststore**. These are
listed in `server/requirements.txt` and matter for normal online data/TLS operation.
They are unnecessary for this synthetic provider and are not silently downloaded.
The TLS warning about absent truststore is expected here: no HTTPS request occurs.
Do not treat this kit as a fully provisioned online/frozen application build.

The development companion uses real `earthd.Server`, `earthd.Connection`, protocol
framing, map-cell serialization, baseline/overlay views, merge/store, native mesh
writers, material slots and Stage 5B2 controller. It supplies flat elevation,
synthetic map inputs and thread-based existing worker entry points. It does not
exercise `main_async` process-pool/geotile setup or PyInstaller packaging; those
remain later integration checks.

`install.py` discovers/copies into game user folders; `start.py` updates the mod by
default and can install dependencies. **Do not use them for this test.** The dedicated
launcher bypasses both. App-path dialogs, updates, external search/routing and
server-list actions are disabled in the development connection. Paths are fixed
inside the marked kit. Non-loopback socket connections and external DNS are blocked
in that process; ordinary world fetches are offline. The OSM client receives an
in-memory response at a reserved `.invalid` identifier, not a network endpoint.
Its normal authorization, parser, request spacing and cooldown rules remain active.
The response/freshness clock is explicitly frozen at `2026-10-30T12:01:00Z` for this
fixture only. Production freshness validation is unchanged.

## Known blockers and limits

1. **Default draw-distance discovery exceeds 128 targets in this fixture.** The
   accepted revision persists, but local preparation reports `ResourceLimit:
   automatic generation target budget exceeded; no targets were silently omitted`.
   This was reproduced with actual protocol/pose streaming, not guessed from tests.
   Production code and quotas were not changed in Stage 6A. General default-distance
   use needs a separate implementation/review before broader release testing.
2. The initial kit uses the existing environment knobs, in its own process only:
   roads send/drop **384/704 m**, far buildings **384/896 m**, skyline **768/1568 m**
   (skyline drop is derived by the existing code as send + 800 m).
   Keep the game's Building draw distance at **Normal**. These affect streaming
   reach, not road footprints, widths or lanes. They allow a controlled first test;
   they do not resolve the production-default blocker.
3. Actual CDAE/TSStatic import, Vue host loading, collision contacts/raycasts, frame
   budgets and cached-shape lifetime require the user-operated engine test below.
   Backend `activation_scheduled` is not proof of an installed or colliding building.
4. Full VM ownership loss, uncertain/legacy journals and incomplete inventories
   retain resources. Do not delete held files while a game may reference them.
5. This first scenario is **legacy-frame/thread-worker** mode, one stationary car,
   plain rectangular buildings and no live services. Geographic tiles, multiple
   collision foci, process-worker startup and load/performance stress are later tests.

The optional `--default-ranges` flag reproduces blocker 1 only in a fresh smoke
sandbox. It must not be used for the first manual acceptance test.

## Deterministic scenario

Use `SCENARIO.json` as the exact numeric source; it is generated with the kit.
Reference origin is data cell `14/4706/6244`, near Baltimore, but **the world is a
synthetic flat-elevation test scene, not a model of the real location**.

Coordinates below are east/north metres from the reference origin. Heights are m.

| Building | Original baseline | Accepted fixture |
| --- | --- | --- |
| A | `(60,80)`, 12 × 16, height 8 | Same OSM identity, 24 × 16, height 24. Replacement retains baseline feature ID. |
| B | `(120,80)`, 12 × 16, height 8 | Unchanged. |
| C | Absent | New `osm:way:9003` at `(210,80)`, 12 × 16, height 14. |
| D | Overture-only at `(60,130)`, 12 × 16, height 8 | Retained. Omission is not deletion. |

The fixed baseline residential road runs from `(-80,20)` to `(280,20)`. No refresh
edits it. Baseline generation and refreshed generation use the same road input.
The fixture never reads a real Overture/DEM/OSM cache.

Park at the manifest's **viewpoint**, `(128,-190)` relative to the origin. This keeps
all test footprints beyond the stationary 200 m safety margin while within the
350 m near-collision residency range. Do not move the car or add vehicles during
the initial handoff. Camera inspection is useful after the first successful handoff.

Recommended selection: `[west,south,east,north]` from `SCENARIO.json`, approximately
**230 × 135 m**. Expected preview: **1 addition, 1 replacement, 2 unchanged records,
0 conflicts, 1 Overture-only building retained**. The two unchanged records are B
and retained D. No parts or multipolygons are included in this first scenario.

## User procedure — preparation and isolation

These steps are for the **user to execute later**. Codex has not performed them.

1. Close normal BeamNG.drive sessions. Preserve your normal user folder as usual;
   do not copy its mods/settings/saves/cache into the test. Do not run the normal
   installer, change `startup.ini`/registry, or change persistent Steam launch options.
2. Open PowerShell at the repository. Set:

   ```powershell
   $stage6Kit = (Resolve-Path '.venv/stage6a/development-kit').Path
   $stage6ProfileParent = Join-Path $stage6Kit 'run/profile'
   $stage6Profile = Join-Path $stage6ProfileParent 'current'
   $stage6Exe = Read-Host 'Full path to the existing BeamNG.drive executable (not its launcher)'
   ```

   Do not pass a normal game user folder to any development tool. No executable
   location was discovered or inspected by Codex.
3. Start the game yourself with a one-session override:

   ```powershell
   & $stage6Exe -userpath $stage6ProfileParent -bngeport 47623
   ```

   `-userpath` is documented as a session-specific user-folder override in the
   [official launch-argument documentation](https://documentation.beamng.com/modding/programming/launch_arguments/).
   The existing repository launcher passes the parent of `current`; this procedure
   uses that same layout. `-bngeport` is read by this mod's existing `net.lua`.
   There is no automatic game launch script in the kit.
4. On this first, mod-free startup, decline any migration/import prompt. Open the
   developer console in **GE Lua** context and run `print(FS:getFileRealPath("/"))`. It must
   resolve to exactly `$stage6Profile` (slash/case normalization is fine). Check the
   game log header as well. If it differs, **stop and close the game**; do not copy
   the mod or alter another folder. The real engine's handling of the flag has not
   yet been tested on this machine. Exit the game before the next step.
5. Verify and stage only the test mod ZIP into that new profile:

   ```powershell
   $stage6Audit = Get-Content -LiteralPath (Join-Path $stage6Kit 'package-audit.json') -Raw | ConvertFrom-Json
   $stage6Mod = Join-Path $stage6Kit 'beamng_earth_stage6a.zip'
   if ((Get-FileHash -LiteralPath $stage6Mod -Algorithm SHA256).Hash.ToLowerInvariant() -ne $stage6Audit.game_mod_sha256) { throw 'Mod hash mismatch' }
   $stage6Mods = Join-Path $stage6Profile 'mods'
   New-Item -ItemType Directory -Force -Path $stage6Mods | Out-Null
   $stage6Destination = Join-Path $stage6Mods 'beamng_earth_stage6a.zip'
   if (Test-Path -LiteralPath $stage6Destination) { throw 'Existing test mod retained; investigate before replacing it' }
   Copy-Item -LiteralPath $stage6Mod -Destination $stage6Destination
   ```

   This future user action installs only into the verified repository-local test
   profile. No normal installed mod is overwritten.
6. In a second PowerShell window, start the companion and keep the window open:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File "$stage6Kit/Start-Synthetic-Companion.ps1"
   ```

   Re-establish `$stage6Kit` in that window if needed. Confirm the banner says
   **STAGE 6A SYNTHETIC ONLY**, loopback **47623**, and the expected test profile.
   A port-in-use error is a blocker; do not connect to or kill an unrelated server.
   The normal `run/` tree is created only when this explicit launcher is run.
7. Launch the game again with the same one-session arguments. Verify `FS:getFileRealPath("/")`
   again. The test profile should contain only this mod; avoid BeamMP, multiplayer,
   repository downloads and external map/search actions during the test.

## User procedure — baseline, refresh and collision observations

1. Load **BeamNG.Earth** in Freeroam with one ordinary car. Set Building draw distance
   to **Normal**. Do not select the production-default range reproduction flag.
2. In the console's GE Lua context, use the exact manifest viewpoint:

   ```lua
   beamngEarth_core.teleport(39.29008564698664, -76.5842096724785)
   ```

   Alternatively use BeamNG.Earth Places → Go to coordinates with the same numeric
   latitude/longitude, which avoids external geocoding. Wait until baseline roads,
   buildings and ground have finished streaming; allow at least 20 s initially.
   The basemap can be blank because imagery is intentionally off.
3. Stop the car completely at the viewpoint. Keep physics running for collision
   checks. Take a baseline screenshot: three buildings A/B/D, no C. Load the helper:

   ```lua
   s6 = dofile('/_stage6a/diagnostics.lua')
   s6.capture('before')
   s6.rays()
   ```

   It writes only `stage6a-before.json` / `stage6a-rays.json` in the test user folder.
   Baseline rays: A east-wall distance roughly **18 m**; C ray has **no hit before
   35 m**. Roof/facade details can vary by small offsets. If the helper/API fails,
   record that as unverified and use the physical contact checks; do not invent a pass.
4. Open World Map → **OSM buildings**. The capability should be available. Choose the
   refresh-area tool, pan/zoom to the manifest bounds and drag a rectangle covering
   all four footprints. The dimensions must stay below 500 m per side. The exact
   coordinate list is printed by `SCENARIO.json`; do not use an enormous selection.
5. Click **Fetch and preview buildings** once. Verify the expected counts above and
   the frozen fixture replication timestamp. Expand change details: A footprint and
   height change, B unchanged, C addition, D retained. Before acceptance the scene
   must still show the original three buildings. Logs should contain one
   `fixture_request`; this is local data, not an HTTP request.
6. Click **Accept and apply these buildings**. Keep the car stationary and outside
   the region; close the pause menu or unpause if physics is stopped. Observe accepted,
   preparing, scheduled/deferred, activating/retiring and eventual **Activated in the
   current view**. The accepted revision alone is not success. If ordinary streaming
   was unfinished, let it settle and choose **Retry locally** once. If a quota error
   occurs with the controlled settings, stop and save logs rather than raising limits.
7. Confirm the visible result: A is wider and taller, B/D remain, C exists once. Record
   screenshots from the same camera position before moving. Run:

   ```lua
   s6.capture('after')
   s6.compare()
   s6.rays()
   ```

   `changed_unrelated_objects` should be empty while the player/camera remain fixed.
   The helper checks baseline road/sidewalk/deck/ground-visual paths and TerrainBlock
   identities. If a class has no captured objects, mark that comparison untested;
   do not claim it passed. Overlay rays should move A's east-wall hit to roughly
   **6 m** and add C's west-wall hit at roughly **5 m**. These are observations to
   collect in the actual engine, not results of Stage 6A.
8. Only after activation, approach slowly and verify real vehicle contact against C
   and A's newly extended exterior wall. Confirm visual walls and physical contacts
   agree, the car remains supported by terrain, and unrelated road/sidewalk geometry
   remains usable. Do not call `be:reloadCollision()` manually: that would mask a
   controller readiness problem. A UI success with missing/ghost collision is a failure.
9. Return to the original viewpoint and stop. Choose **Disable all overlays**, confirm,
   unpause and wait for current-view activation of the original baseline. A should
   return to 12 × 16 × 8, C should disappear, B/D should remain. Rays should return to
   baseline distances. Drive slowly through C's now-empty area to check for ghost
   collision. Stored OSM data must remain available.
10. Return to the viewpoint. **Enable saved overlays**, confirm, and verify A/C return
    without another `fixture_request`. Then use **Rollback last accepted revision**
    and confirm. With this initially empty store it restores the original baseline
    again. Verify scene and collisions after acknowledgement. No new OSM fixture
    request is needed for retry, disable, enable or rollback.

If exact drag selection is difficult, an optional **diagnostic-only** preview can
be requested with:

```lua
beamngEarth_settings.uiBuildingsCommand('preview', jsonEncode({bounds={-76.5854615264661,39.29215733555047,-76.58279539789308,39.29337328507307}}))
```

This does **not** pass the map-picker/UI test; report the
picker separately. Respect the normal 60 s spacing / 600 s same-area cooldown.
For a completely fresh test, close both processes and preserve/rename `run/` as below.

## Logs, verdict and reversible cleanup

Capture these files from the kit/test profile after the run:

- `companion-console.log` and `run/logs/events.jsonl` (state transitions, fixture
  request count, publication metadata and development errors; no write capability).
- `run/profile/current/beamng.log` and any engine import/collision errors.
- `run/profile/current/stage6a-before.json`, `stage6a-after.json`, `stage6a-compare.json`,
  `stage6a-rays.json`, `stage6a-baseline-roads.json`, plus screenshots.
- The store and journals in `run/cache/building-overlays/` and
  `run/cache/building-generations/`, preserved for diagnosis.

The first engine test is a pass only if UI selection/review/acceptance works,
activation is acknowledged for the current view, visible and colliding geometry
match, no duplicates/ghost colliders remain, baseline restoration works, unrelated
objects remain valid and local operations do not refetch. Record each check as
**pass / fail / not tested**. Automatic scheduled status is never an engine pass.

For cleanup: exit BeamNG.drive completely, stop only this companion with Ctrl+C,
and confirm both have exited. Do not delete held files while either may use them.
Preserve the entire development `run` directory rather than deleting individual
slots, journals, settings or overlays:

```powershell
$stage6Run = Join-Path $stage6Kit 'run'
if ((Resolve-Path -LiteralPath $stage6Run).Path -ne (Join-Path $stage6Kit 'run')) { throw 'Unexpected run path' }
Rename-Item -LiteralPath $stage6Run -NewName ('run-preserved-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
```

This retains development saves, settings, cache, logs and evidence. The next explicit
development launch starts a fresh `run/`. No normal user folder is removed. Close
the test launch and use the ordinary game shortcut without the one-session flags
to return to normal play; verify the normal user path before proceeding. No persistent
Steam/startup.ini/user-folder setting should have been edited. Existing installs,
mods, saves and normal caches were never the destination of these steps.

## Automated checks and responsibility boundary

Actual checkpoint results: **540 regression tests: 534 passed, 4 skipped, 1 failure,
1 error** across 22 suites. The established unrelated results are
`test_roads.PaintRoadsTest.test_bare_verge_beside_roads` (`9 != 0`) and
`test_protocol_agreement.CacheAgreementTest.test_cache_in_the_mod` (obsolete cache
enum regex: `None.group`). They are unchanged and were not fixed in this stage.
The four skips are two standalone LuaJIT CLI checks (mandatory Lupa/LuaJIT engine
doubles still ran) and two explicitly opted-out real-cache road-geometry tests.
**21 UI checks passed**, including four compiled Vue components, using the pinned
local Vue 3.5.22 compiler. Six new development-tool tests passed. Full logs are
`.venv/stage6a/regressions.log`, `ui.log` and `preparation-tests.log`.

The extracted portable runtime verified **346 file hashes, 38 required components,
128 Python parses, 68 LuaJIT compilations**, native 114, WebP, server imports and
production `--help` with saved-user-path discovery explicitly bypassed. The real
local-wire smoke prepared **80 targets and 6 building mesh files**, wrote actual
terrain output and reached `activation_scheduled` with one in-memory request.
The separate default-range run failed with the expected resource quota diagnostic.

Codex can execute archive/manifest/CRC/import/native checks, syntax/UI compilation,
regressions, synthetic parse/merge, external-network guard checks and the local
wire/terrain/mesh/preparation smoke test. The smoke creates disposable data only
under `smoke-sandboxes/`; it is never the future manual-test `run/` profile.

The user must verify the actual game user-path override, load the mod in BeamNG,
operate the map UI, inspect rendered objects, test physical collision, evaluate
performance and confirm restart/reload/cached-shape behavior. Those cannot be
established from Python, archive inspection or Lua engine doubles.

See `.venv/stage6a/verification-results.json` for actual Stage 6A execution results.
The baseline regression runner intentionally remains nonzero for the two known
unrelated failures. **Stage 6A ends at preparation; real in-game Stage 6 has not passed.**
