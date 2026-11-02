# Explicit pre-game OSM building preparation

This developer milestone uses the selected worldwide Layercake buildings GeoParquet
source. It prepares a small Agadir neighbourhood, then uses the existing ordinary
building generation pipeline. Roads, infrastructure, terrain, elevation and imagery
keep their existing sources. There is no live activation or graphical update button.

## Explicit operation and normal startup

Use the application's Python environment with `server/requirements.txt` installed.
Close the companion and game before preparation. This command only prepares data;
it does not install a mod or launch an application:

```powershell
$env:BNGE_OSM_PRIMARY = 'layercake'
$osmCache = Join-Path $PWD '.venv\layercake-agadir\cache'
python server/buildingprimary.py --cache-dir $osmCache --location agadir --confirm-source-policy
```

The confirmation flag records the caller's authorization to use the selected source
under its applicable policy; it is not a substitute for permission from a provider.
No public Overpass request is involved. The source is fixed to:
`https://data.openstreetmap.us/layercake/buildings.parquet`.

On a subsequent application launch, keep `BNGE_OSM_PRIMARY=layercake` and configure
the launcher with **the same cache directory**. Normal startup reads only saved
building data and validates its original baseline. It never checks Layercake's
current ETag, publication date, or data. This mode takes precedence over the earlier
`BNGE_OSM_STARTUP_CONFIG` saved-area workflow and bypasses live activation. A fresh
game process is required. Preparation is excluded by an OS lease while the companion
is using the selected snapshot. Existing general streaming/collision safety remains.

The fixed developer neighbourhood is west/south/east/north
`[-9.5552, 30.4119, -9.5516, 30.4136]`, including both supplied points. Internal z18
partition IDs are reported; coverage remains the exact requested bbox, not a claim
that every intersecting cell was fully fetched. This first bounded job is suitable
for a later city queue, not an implementation of city-wide scheduling or a final UI.

## Reader, ownership and persistence

- `layercakebuildings.py` reuses `osmtags.OsmTags` row-group metadata/candidate logic,
  `overture.Reader`'s configured DuckDB/fsspec connection and range prefetching, and
  `rangeread.RemoteFile`'s block cache and If-Match validation. DuckDB's HTTP and
  auto-extension paths remain disabled. Reads use existing netfetch admission,
  host policy, User-Agent, offline checks, budgets and circuit breakers.
- A separate bounded HTTPS range transport prevents a server ignoring Range from
  downloading the whole file. It closes responses on failure, rejects redirects
  and content encoding, and limits reads while receiving. Socket/operation limits
  are 30/45 seconds (OS DNS resolution has the usual platform limitations).
- Each operation reserves at most 96 MiB of HTTP ranges, with at most 16 MiB per
  range, a 32 MiB footer, 512 returned features, 128 KiB per geometry, the existing
  merge geometry limits, and a 256 MiB DuckDB memory limit without disk spill.
- Raw building blocks have conservative 256 MiB/4,096-entry admission and reserve
  a full operation before fetching. Accepted history uses the existing 64 MiB,
  128-object, 2,048-entry limits. Exhaustion is explicit; no evidence/history or raw
  generations are automatically deleted. Long-term compaction is a later milestone.
- `buildingprimary.py` reuses Stage 2 checksum, safe-path, OS-lock, fsync and replace
  primitives. It writes immutable content objects and validates them before atomic
  head publication at `<cache>/osm-primary/v1`. A previous manifest is retained;
  orphan objects are not committed. Legacy overlay stores/kits are never modified.
- The persistent request ledger reserves a ten-minute operation cooldown before
  HTTP and retains Retry-After. Missing/corrupt reserved ledgers block further
  refreshes; normal cached sessions remain usable.
- One validated snapshot reaches the main MapData and every local rendering worker
  and respawn. Newly prepared data applies on the next complete session only.
- Existing conservative merge matching prefers typed OSM identities. Proven spatial
  duplicates adopt the accepted OSM footprint. Independent Overture buildings are
  retained; ambiguous overlaps block publication instead of inventing a match.
- Building dependencies include accepted geometry/attributes, parents/parts and
  suppressions. Near/far/skyline read the normal building accessor. Baseline readers
  and road/terrain version inputs remain unchanged; no global cache version bump.

## Geometry, parts and deletion limits

The observed source includes typed IDs, version, edit timestamp, geometry, bbox,
building/part tags, floors, height, roof fields and material/colour fields. Only
published attributes can be used; omitted source columns are not invented.
The dataset Last-Modified is **publication time**, not OSM replication state.

Ways and relations retain their complete WKB geometry and identities. A multipolygon
relation with exactly one simple exterior is losslessly usable by the existing
generator. Multiple exteriors and holes remain complete in the source result with
unsupported diagnostics. They are not flattened or partially drawn.

The observed schema has no parent association column. Part rows are retrieved, but
unknown/orphan associations block acceptance. Complete externally validated parent/
part sets use the existing atomic part path; tests cover it. This reader cannot
claim that Layercake supplies all part-only objects or reconstruct missing relation
membership. Existing parts cannot be silently removed or downgraded. Unsupported
attributes/geometry or any merge conflict retain the previous accepted snapshot.

**Absence is not deletion.** Missing identities are reported only where their
original footprints intersect the queried coverage; neighbouring baseline context
outside that coverage is not falsely reported missing. No missing identity is
automatically removed. Absence can reflect moved, omitted, retagged or deleted
objects; the building-only file cannot distinguish those states reliably.

Positive identity-state evidence has a separate `Removal` interface (typed ID,
newer version, timestamp, deleted/retagged state and authoritative source). Complete
validated geographic context and an unambiguous original are required. Such evidence
persists a suppression, removes the owned baseline building/parts, changes local
cache dependencies and prevents an old source version resurrecting the footprint.
The unit deletion scenario uses an **explicit synthetic authoritative-history proof**;
no production history/deletion feed has been added or claimed. An eventual identity
adapter must obtain and verify that evidence; a caller cannot derive it from an
empty Layercake response. Positive proof handling does not establish physical
demolition or resolve arbitrary split/merge histories.

## Verification and next milestone

The explicit source read found 76 real OSM buildings at ETag
`"77b2a651e741fc3e3dc73b1de33bda93-8813"`, published `2026-11-01T14:35:59Z`.
The supplied way/1572086438 is version 2, edited `2026-10-28T00:57:56Z`, with three
floors and a flat roof. Its full real footprint is an attributed offline fixture.
The user's second edit is not expected in this snapshot; its absence is not an error.

A data-only integration against real pinned Overture produced 62 additions, one
replacement, zero conflicts and 79 ordinary native building specifications in the
test box. There were 76 prepared OSM entries. This is not a BeamNG collision or
engine validation. Focused tests exercise the actual Parquet reader using local
Parquet bytes/mock HTTP, native generation, duplicate suppression, cache keys,
two sessions, offline reuse, parts, invalid data, explicit suppression, atomic
failure, persistent cooldowns and session exclusion. Existing restart/render suites
are included; the full repository marathon is intentionally not run.

Final focused result: **61 tests passed, zero failures/errors/skips**, including ten
new primary-source cases, 23 render regressions and 28 restart regressions. The
non-removable test guards recorded zero blocked external launch/network attempts.
The final real-data replay passed with `offline=True`, no freshness check and the
same 79 ordinary building specifications. Source inclusion audit: 351 package files;
new modules/guide included, fixtures/development artifacts excluded. Diff checks pass.

Before an engine test: use an isolated profile/mod/companion and matching cache,
verify production dependencies and packaging, then validate actual placement,
appearance, LODs and collision in BeamNG. No kit is prepared by this milestone.
The next small UI milestone is a pre-game companion action that invokes the same
refresh operation, shows source publication/counts/conflicts, and enables play only
after successful preparation or an explicit choice to retain saved data. Disable
that action during gameplay. City scheduling, full part inventories, robust deletion
history and safe history compaction remain separate work.
