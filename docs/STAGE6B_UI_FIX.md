# Stage 6B — preview rendering and request safety fix

Checkpoint: `6ee0916c13293087002d8a2f6939f2a538682297`, branch
`feature/live-osm-buildings`. The working tree was clean when this fix began.
The user observed the preview render failure in **BeamNG.drive 0.40.1**. No game
was installed into or launched by Codex for this fix. No live OSM request, road or
terrain edit, Rust change, commit or push was made.

## Root cause and the exact data path

1. `server/buildingmanual.py`, `Workflow._changes()`, compares attributes. An
   unchanged building legitimately produces `attributes_changed={}`.
2. `server/buildinglive.py`, `preview_for_ui()`, projects that dictionary into
   `attributes=list(r['attributes_changed'])`: **an empty Python list `[]`**. The
   canonical proposal/token and stored geometry are separate from this projection.
3. `earthd.Connection.send()` uses `server/protocol.py:encode()`: four-byte
   little-endian length plus UTF-8 JSON. On the wire, the field is correctly `[]`.
4. `beamngEarth/net.lua` decodes the JSON with `jsonDecode()`. `core.lua` routes
   `building_status` to `buildingui.message()`, which stores the Lua table.
5. `settings.uiBuildingsState()` returns that table through `buildingui.state()`.
   `window.bngApi.engineLua()` returns it to the Vue callback. An empty Lua table
   does not intrinsically distinguish an array from an object: this boundary can
   return **`attributes: {}`**. Nonempty sequential tables can still become arrays.
6. The old Vue template called `row.attributes.join(', ')` unconditionally.
   `{}` has no `.join()`. Reassigning status every 500 ms retriggered the render
   error. Similar assumptions affected IDs, issue/change collections and diagnostics.

The repository does not contain BeamNG's native JSON/CEF bridge implementation;
it was not read from the installed game. The regression models that boundary with
real LuaJIT tables and the existing empty-table serializer, then executes the
**compiled component in real Vue**, rather than asserting compilation alone.
An actual backend preview with an unchanged building produces the empty attributes
list. After protocol framing, Lua decode/status storage and bridge conversion it
is `{}`. Running the checkpoint component on that payload reproduces the exact
error on three polls; the fixed component renders all three with zero errors.

Evidence is preserved in `.venv/stage6b/preview-roundtrip.json`,
`reproduced-before.json` and `rendered-after.json`. The DTO contains no private
write authorization. Actual BeamNG retesting remains the user's next step.

## Serialization and control contract

`normalizeBuildingStatus()` creates a bounded **public display model**. Array fields
accept arrays, missing/null values and empty objects from Lua; the latter become
empty arrays. Nonempty objects, scalars and malformed rows are not guessed into
lists. They are omitted with a warning and acceptance is disabled for a malformed
review. Strings, counts, revisions, selection values, errors and retry diagnostics
are checked before rendering. No original geometry, server proposal or token is
rewritten. No global JSON encoder or protocol-version change is necessary.

The audit covers `changes[].attributes`, `decisions[].baseline_ids`, `issues`,
`decisions`, `changes`, `overture_only`, `deferred`, and resource `uncertain`,
`removed_generations`, `reclaimed_slots` and `released_sessions`. Numeric counts
remain independent from the bounded detail arrays.

Vue locks commands immediately on click and coalesces overlapping polls. Lua also
holds one write request at a time, so closing/reopening the panel or bypassing a
disabled button cannot submit another request before the first completes. The
companion supplies optional public `command_id` / `command_pending` receipt fields.
An older status poll cannot acknowledge a newer command. A bridge receipt only
means the local command was submitted; completion comes from its matching companion
receipt. There is no timeout that silently unlocks a still-running operation.
Receipts are cleared at a new authorized handshake, so a reloaded Lua VM can restart
its request counter without accepting a receipt from the previous connection scope.

An explicitly repeated request for the **same still-valid selection** reuses the
existing review without another fetch, token replacement or TTL renewal. A failed
request for another selection preserves the earlier review only after read-only
revalidation of its TTL/token, accepted-store state (including ABA fencing), private
configuration, source freshness and baseline files. The connection/frame must still
match. Expired/configuration/baseline/store/frame changes cannot revive old reviews.
Acceptance still requires an explicit user click, `confirm=true`, the original
token and all existing writer-lock validation. Neither rendering nor polling accepts
anything. No automatic OSM retry was added.

**Use the matching companion and mod from this kit together.** The new Lua guard
expects completion receipts; an old Stage 6A companion does not provide them.
Failing to receive a receipt stays blocked rather than guessing that a request ended.

## Files changed

| File | Change |
| --- | --- |
| `mod/.../EarthBuildingRefresh.vue` | Normalize every status before rendering; display malformed-data warnings; immediate submission lock; coalesced polls and matching command receipts; confirm-button guard. |
| `mod/.../buildingRefresh.js` | Public status/collection normalization, strict selection/token checks and safe revision formatting. |
| `mod/.../buildingui.lua` | Shared Lua single-flight guard, receipt matching, failed-send/disconnect/revoke reset. Private capability stays inside Lua. |
| `server/buildinglive.py` | Completion receipts, validated reuse/retention of reviews and clearing reviews on frame changes. |
| `server/buildingmanual.py` | Read-only `validate_preview()`; full acceptance validation is retained. |
| `server/tests/test_buildinglive.py` | Real backend→protocol→Lua→Vue rendering, explicit acceptance, reuse/throttle/failure/expiry/store/frame guards and Lua receipt tests. |
| `server/tests/test_buildingmanual.py` | Revalidation preserves store bytes and rejects changed configuration/baseline. |
| `tools/building_ui_render.mjs` | Real Vue runtime renderer with an in-memory host and explicit UI event tests; no game dependencies. |
| `tools/test_building_ui.mjs` | Runtime rendering/malformed-data/event tests added to existing bridge and SFC checks. |
| `tools/ui-tests/package.json`, `package-lock.json` | Pinned reproducible test-only Vue compiler/runtime dependencies. Nothing is installed system-wide or into BeamNG. |
| `tools/prepare_stage6a.py` | Add `--stage 6B`, new mod name and dedicated port; existing no-overwrite behavior and normal packager remain. |
| `tools/stage6a/serve.py`, `verify.py` | Read stage label/mod filename from metadata; retain compatibility with old kit metadata. No existing kit is edited. |
| This document and `LIVE_OSM_BUILDINGS.md` | Failure analysis, evidence and safe continuation instructions. |

Here `mod/.../` means `mod/beamng_earth/ui/ui-vue/mods/beamngEarth/` for Vue/JS
and `mod/beamng_earth/lua/ge/extensions/beamngEarth/` for Lua.

## Reproducible checks and results

Use the existing project-local Python runtime, without `-Bootstrap`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/test_building_overlay.ps1
node tools/test_building_ui.mjs
& '.venv/overlay-tests/python/python.exe' -B -m unittest discover -s tools/stage6a -p test_preparation.py -v
```

The broad run covers **548 Python regression tests**: **542 passed, 4 skipped**,
with the two established unrelated results retained:

- `test_roads.PaintRoadsTest.test_bare_verge_beside_roads`: failure, `9 != 0`.
- `test_protocol_agreement.CacheAgreementTest.test_cache_in_the_mod`: error,
  obsolete cache enum regex returns `None.group`.

The regression runner remains nonzero for those two suites. The four skips are two
standalone LuaJIT CLI checks (mandatory embedded LuaJIT checks ran) and two opted-out
real-cache roadgeo checks. **34 UI checks pass**, including actual Vue render/event
execution, and **6 development preparation checks pass**. The live-control suite
contains 23 passing tests. No OSM endpoint or installed game is needed.
That live-control suite was rerun after the final handshake receipt-scope hardening.

For a fresh UI environment, copy `tools/ui-tests/package.json` and `package-lock.json`
into `.venv/overlay-ui/`, use `npm.cmd ci --prefix .venv/overlay-ui --ignore-scripts
--no-audit --no-fund`, and set npm's cache/user/global config and TEMP/TMP to
repository-local locations. The pinned compiler and Vue runtime are **3.5.22**.
This is a test-tool download from npm, not an OSM request. Existing local dependencies
can run offline; no download occurs during the regression commands above.

Actual logs and machine-readable results are under `.venv/stage6b/`. The final
archive/runtime audit and local synthetic smoke results are in the new kit.
The source package is checked against all 346 repository files, including all 38
required components. Native version 114, WebP, Python/Lua syntax and CLI import/help
checks are retained. The synthetic backend smoke reaches `activation_scheduled`;
this is not a game acknowledgement, rendering result or collision test.

## New isolated kit

Build command (refuses an existing destination):

```powershell
& '.venv/overlay-tests/python/python.exe' -B tools/prepare_stage6a.py --stage 6B --output .venv/stage6b/development-kit
```

Exact root: `C:\Users\ULTRAPC\source\BeamNG.Earth\.venv\stage6b\development-kit`.

- `build/BeamNG.Earth-1.7.2-beta.zip`: source package from the existing packager.
- `beamng_earth_stage6b.zip`: matching mod, including the fixed Lua/Vue/JS.
- `Start-Synthetic-Companion.ps1`: separate loopback companion on **47624**.
- `runtime/`, `source/`, `SCENARIO.json`, `synthetic-overpass.json`, `package-audit.json`,
  `runtime-audit.json`, `smoke-results.json`, `docs/`: portable runtime, complete
  synthetic scenario, source/hash audit and procedure.

The workspace marker remains named `STAGE6A_WORKSPACE.json` for compatibility with
the audited workspace validator; its metadata explicitly identifies **6B**, port
47624 and the current source/diff. Legacy `stage6a-*.json` observation filenames
also remain but are written into the **new profile**, never the old kit.

All Stage 6A artifacts, logs, accepted overlays, journals and caches stay in place.
No retained evidence or uncertain resources are removed. The manual `run/` profile
is not created by the automatic smoke, which uses separate randomized sandboxes.

## Safely continue the paused manual test

These are **user actions for later**. Codex has not performed them.

1. Save the old game console error/screenshots and companion logs. Exit the old
   BeamNG session completely, then stop its companion. Do not hot-replace a ZIP,
   clear old caches, reload collision or delete held files in the paused session.
   Leave the entire Stage 6A kit intact.
2. In PowerShell at the repository:

   ```powershell
   $stage6bKit = (Resolve-Path '.venv/stage6b/development-kit').Path
   $stage6bParent = Join-Path $stage6bKit 'run/profile'
   $stage6bProfile = Join-Path $stage6bParent 'current'
   $stage6bExe = Read-Host 'Full path to BeamNG.drive executable, not the launcher'
   & $stage6bExe -userpath $stage6bParent -bngeport 47624
   ```

   First launch this new profile **without a mod**, decline migration/import and
   verify `print(FS:getFileRealPath("/"))` in **GE Lua** resolves exactly to `$stage6bProfile`.
   If not, stop. Exit the game before copying anything. Use one-session flags; do
   not edit Steam options, `startup.ini`, the registry or normal user folders.
3. Copy only `beamng_earth_stage6b.zip` into that verified new profile's `mods/`
   directory. Check its SHA-256 against `package-audit.json:game_mod_sha256`. Do not
   copy old settings/mods/cache/journals or use old preview tokens. Refuse to
   overwrite an existing destination; choose another fresh kit/profile if necessary.

   ```powershell
   $stage6bAudit = Get-Content -LiteralPath (Join-Path $stage6bKit 'package-audit.json') -Raw | ConvertFrom-Json
   $stage6bMod = Join-Path $stage6bKit 'beamng_earth_stage6b.zip'
   if ((Get-FileHash -LiteralPath $stage6bMod -Algorithm SHA256).Hash.ToLowerInvariant() -ne $stage6bAudit.game_mod_sha256) { throw 'Mod hash mismatch' }
   $stage6bMods = Join-Path $stage6bProfile 'mods'
   New-Item -ItemType Directory -Force -Path $stage6bMods | Out-Null
   $stage6bDestination = Join-Path $stage6bMods 'beamng_earth_stage6b.zip'
   if (Test-Path -LiteralPath $stage6bDestination) { throw 'Existing evidence/profile retained; use a fresh destination' }
   Copy-Item -LiteralPath $stage6bMod -Destination $stage6bDestination
   ```
4. Start the new `Start-Synthetic-Companion.ps1` yourself in a separate terminal.
   Confirm **STAGE 6B SYNTHETIC ONLY**, **127.0.0.1:47624** and the new profile path.
   A busy port is a blocker; do not fall back to the old companion. Launch the game
   yourself with the same `-userpath` and `-bngeport` arguments, and verify the user
   path again. No real endpoint or credentials should be configured.
5. Load BeamNG.Earth, use Building draw distance **Normal**, and teleport via GE Lua:

   ```lua
   beamngEarth_core.teleport(39.29008564698664, -76.5842096724785)
   ```

   Wait for baseline streaming to settle, stop the car and keep physics running.
   The exact test bounds `[W,S,E,N]` are:
   `[-76.5854615264661,39.29215733555047,-76.58279539789308,39.29337328507307]`.
   Use the World Map's OSM buildings tool to select this approximately 230 × 135 m
   area. All source data is in memory; there is no live OSM backend.
6. Click **Fetch and preview buildings** once. Immediately double-clicking should
   not send a second in-flight command. Expect **1 addition, 1 replacement,
   2 unchanged, 0 conflicts, 1 Overture-only retained**. Expand the detail list:
   unchanged attributes display **unchanged**, empty lists display **none** where
   appropriate, counts render and the panel stays intact across at least ten polls
   (five seconds). Record any console exception as a failure.
7. Leave it on review first. There must be no accepted store revision, prepared
   activation or visible building change before explicit acceptance. Check
   `run/logs/events.jsonl`: one `fixture_request`. Re-requesting the same still-valid
   selection may reuse the existing review but must not fetch again or extend TTL.
   A failed request for a different area must not destroy the old valid review;
   return to its original selection before accepting it. Do not spam requests.
8. Only after the preview UI passes, click **Accept and apply these buildings**.
   Continue the baseline/overlay visual, raycast, actual vehicle-contact,
   disable/enable/rollback and unrelated-object checks in
   [the Stage 6A scenario procedure](STAGE6A_MANUAL_TEST.md). Substitute the **new
   kit/profile/port/mod** everywhere. A should widen/taller, C should appear, B/D
   should stay. Require current-view game acknowledgement; accepted/prepared/
   scheduled alone is not success. Do not manually reload collision to mask failures.
9. Keep new logs under the new kit: `companion-console.log`, `run/logs/events.jsonl`,
   the new profile's `beamng.log`, screenshots and `stage6a-*.json` observations.
   If a request never receives completion, preserve logs and stop; do not unlock
   it with repeated fetches. Cleanup only after both processes exit; preserve/rename
   this kit's `run/` rather than deleting retained resources. Ordinary game launch
   without these session flags returns to the normal installation/profile.

The known default-distance **128-target discovery quota** remains unchanged; use
the kit's controlled ranges. Optional normal-online dependencies and geographic/
process-worker/frozen-app startup remain outside this fix. **Real BeamNG UI
retesting and collision readiness are not claimed as passed. Stop at the prepared
fix/kit until the user resumes the manual game test.**
