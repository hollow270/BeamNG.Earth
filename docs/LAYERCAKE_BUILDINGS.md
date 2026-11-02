# OSM-primary buildings v1 — BeamNG.drive 0.40.1.0

This is the maintained user guide. Historical Stage 6 and saved-area documents
are evidence, not the current workflow. Roads, road widths, grading and terrain
continue to use the original Overture inputs.

## Enable once, then travel anywhere

Use the normal companion application's Python environment (or its packaged
executable) and your chosen download cache. Close the game and companion before
changing configuration. Read the Layercake distribution's current usage policy
and confirm that your intended usage is permitted. No public Overpass backend,
endpoint rotation, credentials or planet download is involved.

```powershell
python launcher.py --cache-dir 'D:\BeamNG-Earth-cache' --osm-buildings-enable --osm-source-policy-confirmed
python launcher.py --cache-dir 'D:\BeamNG-Earth-cache' --no-beammp
```

Enabling exits without installing or launching anything. It does NOT prepare a
city. On normal launches, saved building data is reused without a freshness check.
Teleport or drive using the existing map. Unknown geographic cells automatically
retrieve supported OSM buildings from the worldwide Layercake distribution at
`https://data.openstreetmap.us/layercake/buildings.parquet`, before their ordinary
building geometry is generated. Known cells, including empty completed cells,
are cache-first across restarts. The first unknown cell may select the source's
publication; it does not refresh already accepted building identities nearby.

The existing launcher still manages normal application startup. For a non-Steam
game, select its installation with the existing `--game-dir` option. Never use a
Steam URI for a non-Steam installation. No new game UI button is required.

## Explicit freshness update, before the next session

Close the game and companion. Select a place with the existing in-game map before
closing, then use its saved position; alternatively provide latitude/longitude:

```powershell
python launcher.py --cache-dir 'D:\BeamNG-Earth-cache' --osm-buildings-update --user-dir 'D:\isolated-profile\current' --osm-radius-km 3 --osm-source-policy-confirmed
# Or: --osm-center 30.412876 -9.5524592 --osm-radius-km 3
python launcher.py --cache-dir 'D:\BeamNG-Earth-cache' --osm-buildings-status
```

The radius describes a bounding square's approximate half-size, not an exact
circle. The default is 3 km; supported half-sizes are 0.1–10 km below 80 degrees
latitude without crossing the dateline. These selection restrictions apply to
manual regional updates; automatic acquisition uses the normal worldwide map
cell grid. Existing renderer restrictions still reject polar/dateline footprints.
No Agadir-specific production path or dozens of manually entered boxes remain.

Progress shows the target bounds, pinned source ETag/publication, completed and
total retrieval partitions, cache reuse, accepted revision, warnings and errors.
A partial retrieval is NEVER reported as complete. Complete coverage with deferred
features reports `complete_with_warnings`, `all_features_updated=false` and explicit
per-feature/group reasons. It does not claim every source building updated.

Failed/cancelled jobs retain the old published data. Run the SAME command after
the persisted cooldown to resume verified partitions without downloading them
again. A changed source publication starts a new pinned set; incompatible versions
are never mixed. Publication of a full regional update is atomic, after baseline
validation and cancellation checks. Accepted updates apply only in a fresh session.

## Consistency, ownership and fallback

Each geographic data cell's effective choice is durably fixed in a shared session
ledger BEFORE any near/far/skyline dependency or geometry uses it. All local workers
and respawns use that ledger. New areas extend the session's choices; they never
replace active meshes/colliders. A cross-boundary building already chosen by a
neighbour retains the same geometry. A new conflicting influence into a previously
chosen fallback area is deferred for the session rather than changing that area.
This can leave a boundary building on fallback until restart; the log reports
`session_boundary_deferred`. Density-neighbour dependencies participate too.

The normal building generator, corrected inside-vehicle collision hold and normal
collision reloads are reused. There are no live activation transactions, cohort
schedulers or special collision handoffs. Remote helper workers cannot safely share
these local session choices, so snapshot-mode jobs use local workers.

OSM identities take precedence. Proven duplicate Overture footprints are replaced;
independent Overture buildings remain. Floors, heights, supported roofs and ordinary
way/relation exteriors use the existing normalization. Multiple exteriors, holes,
unproven parent/part inventories and ambiguous matches are explicitly deferred.
Prior valid geometry or ordinary Overture is retained. Known complete supported
parent/part sets remain atomic; missing associations are never invented.

An absent Layercake row is NOT a verified deletion. Previously accepted buildings
are retained on absence. The existing positive identity-state suppression interface
remains, with version/timestamp and ownership checks. No production OSM history feed
has been added. Automatic deletion/retagging history is a remaining limitation.

An unavailable source, policy refusal, unsupported data, offline mode or failed
retrieval uses Overture/previous accepted fallback. A failed first visit stays on
that choice for the session, even if the network returns, preventing unsafe collider
replacement. An uncompleted area can retry in a fresh session. Corrupt ownership or
storage is not silently reset. Source publication time is not OSM replication time
and cannot prove that a particular recent edit has propagated.

## Storage, limits and diagnostics

Storage is separate from immutable Overture under `<cache>/osm-primary/v1`:

- `objects`, `state.json`, `previous.json`: accepted manual regional snapshots.
- `regions`: checksummed, resumable retrieval jobs and persistent request ledger.
- `world/index.json`, `world/objects`: completed first-visit cells and immutable data.
- `world/sessions`: bounded immutable choices for individual companion sessions.
- `world/regions`: shared automatic-source jobs and persistent range admission.

One network request runs at a time. Existing netfetch policies, offline controls,
User-Agent, budgets, range/ETag validation and circuit breakers remain. Range requests
are spaced at least one second apart, including across process restarts. Retry-After
is persisted; other failures impose a shared cooldown. Explicit source checks have
a ten-minute cooldown. Bodies are bounded while reading; each query retains the
96 MiB network, 512-feature, geometry and 256 MiB DuckDB memory safeguards. Dense
partitions subdivide; no target list is silently truncated.

Regional preparation admits at most 4,096 jobs, 100,000 unique source features,
50,000 accepted features including parts, 96 MiB per snapshot and 512 MiB retained
snapshot history. Automatic persistent choices are bounded by 4,096 cells, 8,192
objects/512 MiB and 1,024 retained session ledgers. A building generation read is
bounded at 50,000 entries; previously travelled geometry is not accumulated in
memory as a growing session-wide quota. Its immutable choices and full influence
bounds remain in the ledger. The persistent typed-identity index admits 100,000 identities.
That index prevents a newly visited area from implicitly moving/refreshing a
building previously acquired elsewhere. Raw building ranges have a 2 GiB bound; saved retrieval jobs
have a 256 MiB bound in each store. No history/evidence/object is silently deleted.
Capacity errors retain valid references and are reported; uncertain ownership is
never collected. Archive a closed disposable cache explicitly if these finite
budgets are reached. This is worldwide location support, not an unlimited planet
cache or a promise of instant loading.

Mesh cache keys use local effective geometry, attributes, parent/part relationships
and meaningful source identity. A publication timestamp/ETag change alone does not
rebuild unchanged buildings. Unrelated tiles and road/terrain versions are unchanged.
An Overture release change fails closed if original ownership can no longer validate;
keep the old cache for evidence and prepare a compatible separate cache.

Companion logs identify `earthd.osm_world`, cell keys, `world_first_visit`,
`world_cached`, `world_fallback`, coverage, warnings and boundary deferrals. The
checksummed world/session files retain exact choices for investigation. No status
claim substitutes for physical collision verification.

## One minimal engine acceptance (two places and a revisit)

Use the separately prepared `buildings-v1/manual` profile/cache and matching packaged
sources/mod, never your normal profile or historical Agadir kit. Exact commands and
hashes are in that build's `manual/commands.ps1` and `verification.json`. Commands
are human-operated; preparation does not launch the game or request external data.

1. Enable worldwide acquisition in the fresh test cache, start the matching companion,
   then launch `C:\270\games\BeamNG.drive\Bin64\BeamNG.drive.x64.exe` with that isolated
   userpath and companion port. Decline profile migration. In **GE Lua** check:
   `print(FS:getFileRealPath("/"))`. It must be the isolated profile's `current` folder.
2. Use the existing map to visit Agadir near `30.412918583438707, -9.552353024797444`.
   Wait for ordinary loading. Confirm `world_first_visit` or validated saved reuse,
   inspect way/1572086438's footprint and slowly check its wall collision. Move a short
   distance to check far appearance/LOD. Do not force a collision reload in the console.
3. Visit a safe road near Paris `48.8566, 2.3522`, without preparing that city. Wait for
   first-visit logs and supported buildings. If the selected spawn is inside a building,
   move to an open road before assessing collision. Confirm near/far appearance.
4. Return to Agadir. No new OSM freshness request should occur for completed cells;
   geometry/collision should remain consistent. Close both test processes normally.
   A restart at the same place must reuse saved cells without source selection/ranges.

Send the short companion log, whether both places loaded, and the collision/revisit
result. No repeated teleport marathon is needed. If there is a fallback or boundary
warning, report it rather than treating it as success. Keep the isolated evidence.
