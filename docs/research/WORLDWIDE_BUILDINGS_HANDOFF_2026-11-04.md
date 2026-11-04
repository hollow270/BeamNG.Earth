# Building work saved on 4 November 2026

This is an unfinished development checkpoint, not a production release.

## Branches

- `feature/live-osm-buildings`: preserves the previously uncommitted 23-file
  production-distance influence/cohort redesign based on b54f601. It was not
  engine-validated before work moved to restart-based loading. Keep it separate.
- `feature/restart-osm-buildings`: worldwide OSM-only building acquisition,
  immutable session choices, persistent accepted data, ordinary world streaming,
  the building collision fix, bounded acquisition/recovery and source reuse.
  Based on 648e86d; the latest recovery/performance changes are saved here.
  Overture roads remain; all active building footprints/context are OSM-only.

No main-branch merge, release or deployment is implied by saving these branches.

## Actual engine evidence: BeamNG.drive 0.40.1.0

Fresh online testing used an empty companion cache and a fresh isolated profile
on port 47811. Agadir downloaded supported OSM buildings automatically: six source
requests, 28.39 MiB, 43 saved cells, no unavailable/partial cells or pending claims.
Final GE status: connected, 31 road meshes, 29 near building tiles, 197 far building
objects, 337 far tiles, no failed far tiles/held building meshes/backlog.

Agadir first-visit performance is still poor. The 4 November log records finished
first terrain after 326.07 seconds and far scenery after 340.43 seconds measured
from provisional terrain. Initial source staging was 19.516 seconds. Worker
snapshots show roads/ground waiting on building preparation. Exact remaining
cost distribution has not been fully profiled. Do not claim this is fixed.

Iceland teleport at 00:54:54 UTC: provisional terrain about 4 seconds; finished
terrain after 87.74 seconds; far field complete at 00:57:01 (about 127 seconds
from teleport). Final status at 00:58:41: 93 road meshes, 80 near building tiles,
442 far building objects, 341 far tiles, no failed far tiles, held building
meshes or backlog. No critical errors or 300-second worker restarts were seen.
These tests establish two-region first-visit operation, not worldwide speed
or correctness for every geometry. Unsupported source records remain reported.

Earlier actual tests established Agadir static rays around 6 m plus physical
vehicle contact, and cached return/restart loading around or below 20 seconds.
The most recent fresh two-region logs do not independently prove every collider.

## Console correction

The earlier test-only wrapper synchronously drained a pipe and echoed to the
Windows console. Blocked screen output could fill the pipe and stall the
companion, causing timeouts. The corrected wrapper logs the child directly to a
regular file; a separate disposable viewer cannot block child logging. Its
source/tests/launchers are included in the development-tools archive. The
ordinary launcher already uses file-based companion logging.

Four focused wrapper tests passed with both Python runtimes, including four
workers completing with deliberately blocked screen output. The packaged
companion/road-worker/nested offline-acquisition smoke passed. The corrected
wrapper was used for the final empty-cache engine test.

## Automated checks and limitations

The prior focused suite ran 159 tests: 157 passed, two fixture-dependent tests
skipped. UI: 38 checks passed. Kit Lua helpers: eight checks plus four receipt
checks. Packaged Python paths, spawned road worker, nested acquisition and
SQLite RTree support passed. Two established unrelated failures remain:
`test_roads.PaintRoadsTest.test_bare_verge_beside_roads` (9 != 0) and
`test_protocol_agreement.CacheAgreementTest.test_cache_in_the_mod` (old source
regex returns None). Saving work did not rerun the broad test suites.

Local companion replays: previous cold 293.953 seconds, final cold 257.969,
saved-data fresh session 87.156. These are not actual game/collision timings.
No Rust module rebuild or resource-quota increase was made. Remaining limits
include first-visit waits, 100,000 session identities, existing byte/object caps,
unsupported geometries, source availability and conservative cache retention.

## Restoring work after local deletion

Clone the fork and check out the desired branch. All application source and
unit tests are in Git. `WORLDWIDE_BUILDING_DEV_TOOLS_2026-11-04.zip` additionally
preserves small historical harnesses, corrected console wrappers, launch
recipes and compact result summaries with an internal SHA-256 manifest. Its
archive contents retain original relative `.venv/restart-osm/...` paths. Review
paths and reinstall dependencies before running scripts on a new checkout.

The archive intentionally excludes downloaded map data, complete test kits,
Python/Node runtimes, dependency installations, active profiles, saves, raw logs
and bulky evidence. Historical scripts may depend on excluded fixtures. Their
presence is preservation, not a claim that a complete ready-to-run kit can be
reconstructed without reinstalling dependencies and reacquiring data.

GitHub is therefore a source/progress backup, not a full disk-image backup.
Deleting local caches makes later first visits download/rebuild data again.
Deleting local raw logs/evidence loses those exact originals; the written
results above remain. No local files were deleted as part of this save.

Suggested next engineering work: measure building preparation/ownership/session
publication waits using the retained traces, then combine genuinely shared
required-area preparation before releasing dependent road/terrain jobs. Maintain
all source, collision, revision and quota guarantees. Overture's monthly OSM
incorporation is an alternative product tradeoff if fresher data is unnecessary.
