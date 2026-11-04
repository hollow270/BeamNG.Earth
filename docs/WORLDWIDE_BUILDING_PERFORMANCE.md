# Faster worldwide building loading

The worldwide OSM-only pipeline and immutable session choices remain unchanged.
No active mesh or collider replacement is introduced. Roads still come from the
existing road source; road and terrain calculations and the Lua collision fix
are unchanged by this performance work.

## Changes

- Decode each required source row group once per pinned source version. Save
  full source geometry and tags in a checked SQLite spatial lookup. Later local
  reads use that lookup instead of repeatedly scanning the same Parquet data.
- Use the building bounds each consumer actually needs. Keep the extra density
  neighborhood where the renderer uses it, plus boundary and parent/part rules.
- Process a bounded queue of already requested nearby cells with one reader,
  publishing each validated cell independently. Pin fully published requested
  cells together under the existing atomic session transaction.
- Prioritize the player's near requests. Promote waiting distant requests so
  they can also make progress. The source ownership lock remains authoritative.
- Share validated metadata reads between local threads. Readers still check
  file identity; publication still verifies the current revision under lock.

## Safety and limits

The lookup is only an acceleration cache, not an accepted building snapshot.
ETag/source identity, complete coverage, normal geometry validation, identity
ownership and existing immutable session choices remain required. The previous
accepted-store, identity, cell, request, timeout and raw-storage budgets remain.
Raw downloads and lookup files share the existing raw-storage quota. The lookup
has its own 128 MiB per-group ceiling and bounded native-memory/query limits.
Optional acceleration that cannot fit falls back to the original bounded scan.

Finished lookup databases have unique immutable filenames and checksummed
markers. Interrupted work is rebuilt under an OS writer lease. A finished file
with uncertain ownership is retained and counted, never blindly deleted.
Lookups live outside the general download-cache deletion tree. Connection caches
are bounded; no native module rebuild or new Python dependency is required.

At most 16 nearby requested cells share a reader, processed sequentially under
the existing aggregate acquisition deadline. A completed primary cell survives
an explicitly deferred extra cell; deferral is never reported as full success.
Refresh sessions retain their original per-cell freshness behavior.

## Verification and manual testing

Tests exercise real local Parquet data, SQLite RTree reads, four-process writers,
forced worker termination/restart, corrupt markers/data, full footprints and
attributes, resource limits, interrupted publication, request priority, and
cache-first session reuse. Full companion/socket replays use retained real map
bytes and spawned rendering workers, not a running game. Their result files
report received data separately from actual engine activation and collision.

The fresh kit and detailed measurements are retained outside source control at
`.venv/restart-osm/worldwide-performance/`. Its independent manual OSM cache starts
empty. Internet transfer and actual BeamNG loading still require the user test;
local replay times do not promise a worldwide loading-time maximum. Dense areas,
slow sources, the existing identity ceiling, and optional-cache capacity remain
limits. No Rust code, game installation, earlier kit or historical evidence is
modified. All source changes remain uncommitted for review.
