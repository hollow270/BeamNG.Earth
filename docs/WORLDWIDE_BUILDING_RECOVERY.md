# Worldwide first-visit recovery (BeamNG.drive 0.40.1.0)

This is a correction to `fca7cf4`, not a return to manually prepared cities.
The original Agadir collision guard and ordinary roads/terrain generation stay
unchanged. No live building/collision replacement is introduced.

## Diagnosis from November 2 retained evidence

The failed session pinned the November 1 Layercake file (46,201,191,041 bytes).
Nine requests transferred 36,434,500 bytes; the original worker stacks showed
one DuckDB acquisition and three workers waiting for its global storage mutex.
Those stacks prove a lock convoy, not a native deadlock. Standalone execution of
the retained real Parquet bytes completed the same SQL.

Agadir intersects row groups 875/876 (245,760 rows). The old reader reopened
DuckDB, planned and scanned the remote Parquet file for each ~400 m partition,
including dense-partition retries. The first historical dependency cell required
51 successful partitions / 56 scans: 66.3 seconds with **zero network requests**.
One bounded regional staging scan plus 56 local queries returned the same counts
in 4.1 seconds. Range filtering alone did not eliminate repeated execution cost.

Further real cold-source tests found substantial CPU work in overlap grouping
and repeated baseline conversion, so optimizing SQL alone was insufficient.
Grouping now caches footprint bounds and examines a pair only once across grid
buckets. Baseline warming selects original coordinate-array bounds before copying
full geometry/attributes. Duplicate equivalence uses a necessary, conservative
extrema test before exact comparisons. Existing matching tolerances, unsupported
geometry rules, full parent/part closure and final baseline checks remain.

## Acquisition/publication boundaries

`buildingworld.Provider.view()` uses one 45-second acquisition budget for an
entire generation read, including all density dependencies, not 45 seconds per
cell. It prioritizes cells nearest the actual render bounds. A supervised,
data-only spawned Python child runs source requests, DuckDB, conservative merging
and baseline validation. Its own parent/deadline watchdog and the supervising
worker's deadline timer terminate only that owned child. A hung query cannot tie
up a road worker until the existing 300-second road-pool watchdog.

The world publication mutex protects short claim, source-pin and immutable-choice
transactions. Network, SQL and candidate preparation run outside it. Render-view
conversion also runs outside it. Per-session/per-cell checksummed claims deduplicate
callers. Separate OS acquisition/network leases serialize source work; an orphaned
child still owns the network lease until its watchdog exits. OS locks release on
process death. Claim files are bounded and validated; uncertain/corrupt state
fails closed rather than guessing ownership. No PID guessing or application scan.
Cached immutable objects are decoded without the publication mutex. The metadata
lock has a separate 15-second admission timeout, allowing dense JSON publication
without reintroducing the previous 600-second wait behind network/SQL work.

Source selection atomically reloads/CAS-pins the latest session ledger so other
workers' choices cannot be lost. Publication rechecks claim token, session source,
baseline, persistent identities and earlier influence coverage. A fallback chosen
by another reader fences late success. Cached data is read without waiting for the
acquisition lease. Persistent request reservation, Retry-After and cooldown state
remain durable; a failed operation adds a 60-second cooldown. Immutable session
fallback is not retried by replacement road workers. A fresh session can retry;
active colliders are never changed to rescue a failed request.

The staged DuckDB table is limited to one ordinary data cell (~2 km), 50,000 rows,
256 MiB memory and no disk spill. Each output partition still admits only 512
records and subdivides on overflow. Network/range/geometry/cache quotas are not
raised. This is neither a planet mirror nor an unrestricted download backend.

`launcher.py` inserts its application directory before `apppaths`; `earthd.py`
inserts application and server directories before worker imports. This fixes the
embedded Windows Python invocation without kit-specific application path patches.
Frozen executables retain their bundle importer; the bootstrap does not add the
temporary bundle's parent directory to Python's search path.
The existing packagers enumerate the new `buildingacquire.py` normally.

## Verification and manual boundary

Detailed measurements and retained logs are under
`.venv/restart-osm/worldwide-recovery/` outside tracked source. Original failed
kit/evidence files were copied and hashed before investigation. Previous kits
and both historical worktrees are preserved. No game/Steam process was launched.

The real cold-network Agadir acquisition completed in 39.2 seconds, including
publication/view construction: five HTTPS ranges / 29,768,337 response bytes,
one remote scan / 36 local partitions, 1,789 source identities, 1,779 accepted
entries (62 additions / 1,717 replacements). Ten unsupported/ambiguous records
retained fallback. The reference identity `osm:way:1572086438` appeared once.
The render view contains 1,777 intersecting entries; two accepted full footprints
selected by source bbox do not actually intersect the requested cell influence.
This is not a claim that every unsupported building updated or that an OSM edit
is instantly published. Source publication time is not OSM replication time.

Slow native/network work was separately terminated at the deadline and returned
immutable Overture fallback in 45.2 seconds. Local publication/view work adds
bounded CPU/I/O overhead after acquisition, so 45 seconds is not an exact total
tile-delivery SLA. Original baseline/DEM/road loading has its own existing limits.

Tests include real four-process contention, forced owned-worker termination,
source pinning, late-result fencing, corrupt claims, mutex-free render conversion,
actual local Parquet execution, dense/empty/oversized staging, collision-hold
regressions and exact guarded embedded-Python entrypoints/worker/nested-child
startup. Simulated protocol replies are not physical engine collision evidence.

The 200-case targeted regression run passed (no skips), followed by all 68 cases
affected by the final publication-lock/read-path correction. Three LuaJIT suites,
all 67 packaged Lua files' syntax and the Vue/JS bridge/render/compilation checks
passed. The two established unrelated tests were deliberately run separately:
`test_roads.PaintRoadsTest.test_bare_verge_beside_roads` still fails `9 != 0`;
`test_protocol_agreement.CacheAgreementTest.test_cache_in_the_mod` still raises
the existing regex `NoneType.group` error. No new product failure remains.
An initial test command also named nonexistent `test_buildingbaseline`; the
corrected 200-test command passed. All intermediate logs are retained.
The final eight acquisition/bootstrap cases also passed after guarding the
frozen-executable import path; the exact final-kit command smoke passed again.

A final real-data four-process replay exercised the historical tile's four
density/dependency cells. All callers completed in 48.2--48.5 seconds, each saw
the reference once, and their four durable cell heads were identical. Neighbour
queries that could not fit the generation-read budget explicitly froze fallback.
This is successful bounded progress, not full OSM coverage of every dependency.
Earlier iterations exposed publication contention; immutable object decoding and
render conversion now run outside the mutex. No 300-second pool recycle occurred.
Exact Windows startup smokes used the final packaged scripts from outside their
directories, real spawned road-worker initialization, a nested acquisition child,
and a loopback protocol handshake. Python launch guards and loopback-only smoke
networking remained enabled; no application path patch was used by the commands.

The corrected isolated kit is
`.venv/restart-osm/worldwide-recovery/acceptance-kit/`; use its `README.md`,
`commands.ps1`, `hashes.json` and the sibling measurement results. Port 47735,
independent `profile/current` and `cache`, fresh OSM world choices/ranges.
No manually prepared city or automatic freshness check is required.

One final human acceptance must show ordinary roads/buildings loading, the
reference footprint/collision, one unrelated worldwide first visit and a cached
revisit/restart. Keep logs if fallback occurs; do not repeatedly retry inside a
session, delete the cache, force collision reload or start a long test marathon.
Only actual BeamNG testing can confirm loading and physical collision under game
CPU/I/O contention. Do not treat Python or Lua doubles as engine acceptance.
