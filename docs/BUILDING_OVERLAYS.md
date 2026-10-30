# Offline building overlay storage (Stage 2)

`server/buildingoverlaystore.py` persists the results of the Stage 1 merge module.
It has no network calls or server startup hooks, does not touch Overture files, and
does not invalidate caches or generate game objects. A caller must explicitly use
the store and supply the original baseline. Stored overlays start **disabled**.

## Interface

Run with `server` on Python's import path:

```python
from buildingoverlaystore import OverlayStore, RefreshInfo

# Explicit directory. For development, keep it inside this repository.
store = OverlayStore(".venv/overlay-data")
info = RefreshInfo(
    bounds=(-76.595, 39.282, -76.590, 39.286),  # west, south, east, north
    source_timestamp="2026-10-30T12:00:00Z",  # supplied source snapshot time
    provenance=({"dataset": "OpenStreetMap", "license": "ODbL-1.0"},),
    baseline_release="2026-09-23.1",
    metadata={"export": "local-fixture.json"},
)
commit = store.refresh(
    baseline_buildings, incoming_buildings, info=info,
    baseline_parts=baseline_parts, incoming_parts=incoming_parts,
)
print(commit.merge_result.report_json())
store.enable()
effective = store.apply(
    baseline_buildings, baseline_parts=baseline_parts,
    baseline_release=info.baseline_release, bounds=info.bounds,
)

reopened = OverlayStore(".venv/overlay-data")
snapshot = reopened.load()                     # persisted state after a restart
tokens = reopened.dependencies(info.bounds)     # content and geometry tokens
reopened.disable()                             # apply() now returns baseline copies
reopened.enable()                              # restores the same saved edits
reopened.rollback()                            # undo one saved refresh
# reopened.rollback(earlier_revision_id)        # undo back to an ancestor
```

Features use the Stage 1 `buildingoverlay.Feature` interface; `from_mapgeom()` can
copy existing building and part layers into it. Attributes must already be
normalized. `refresh()` composes saved edits even while the visible overlay is
disabled. Saving does not implicitly enable it. Methods return detached copies;
mutating a returned record does not change storage.

## Files and commit boundary

```text
<explicit-data-directory>/building-overlays/v1/
    state.json                 current revision, enabled flag, history, affected bounds
    previous.json              prior validated state manifest
    writer.lock                OS advisory lock; never broken based on elapsed age
    objects/<sha256>.json      immutable, checksummed overlay revision
```

Each revision separates overrides, additions, unresolved conflicts and refresh
metadata. Parent parts are stored with their parent as a complete set. Original
baseline geometry is not stored: overrides retain its fingerprint and bounding
coverage, while effective geometry keeps its source and attribute provenance.
Refresh metadata includes source timestamp, provenance, selection bounds, baseline
release and caller metadata, plus the supplied input records for conflict review.
There are no pickle files, executable payloads or user-supplied filenames.

Readers and writers take a bounded, exclusive OS lock (Windows `msvcrt`, POSIX
`flock`), then reread current state. The lock covers the entire merge/commit, so
two processes cannot publish changes based on the same stale snapshot. Failure
to acquire the lock raises `LockTimeout`; there is no unlocked fallback.

A commit writes a unique temporary file, flushes and fsyncs it, and atomically
renames it to the immutable revision path. It validates the resulting snapshot,
atomically writes the previous manifest, then atomically replaces `state.json`.
The last replacement is the commit point. POSIX also fsyncs the containing
directories; Python has no portable equivalent directory-fsync guarantee on
Windows. The filesystem must support local OS locks and atomic replacement.
Network filesystems and power-loss behavior have not been certified.

A failed/interrupted operation before the commit point leaves the old manifest
and its referenced object intact. A completed object not referenced by the
manifest is an orphan, never an automatically committed revision. Temporary files
from process termination are also ignored. An OS/storage error after the commit
point can leave the caller uncertain whether the commit succeeded: `load()` is
the authoritative check before retrying. Identical refreshes are idempotent.

## Overlapping selections and conflict handling

Entries are keyed by stable feature ID rather than by selection rectangle. Every
refresh runs the unchanged Stage 1 merge against baseline **plus saved edits**.
Omission from a later export does not delete earlier overrides/additions. New
conflicts preserve the last accepted geometry; old conflicts remain unresolved
until an explicit future resolution feature or a rollback removes their revision.
An older source timestamp cannot overwrite a newer accepted entry; it is recorded
as a `stale_source_timestamp` conflict. Equal timestamps still allow coordinate
changes, including moved OSM nodes under unchanged feature IDs/versions.
Saved entries are also selected by incoming OSM identity, so moving beyond the
previous selection cannot bypass part-completeness or source-timestamp checks.

Callers must supply sufficient baseline context, including neighbours touched by
full incoming footprints and original parents of relevant overrides. Missing or
changed originals raise `BaselineMismatch`; changing the Overture release also
requires explicit migration. A disabled overlay can return a different baseline
without applying the old edits. No migration tool is implemented here.

Rollback restores a whole earlier snapshot and its conflict/metadata state while
preserving the enabled flag. Rolling back the first revision restores an empty
overlay. Saving after rollback creates a new branch; abandoned objects remain on
disk but are not offered as redo targets. Per-area rollback is not implemented.

## Revisions for future selective invalidation

The global revision is SHA-256 of canonical serialized snapshot data, including
its parent and refresh metadata. Identical inputs/history produce identical IDs;
input sequence order and supported ring winding/start do not change them.

`dependencies(bounds)` returns content and geometry digests over only the entries
intersecting that area, plus their stable IDs. Coverage includes the original and
current footprint/parts, so moves affect both locations. Metadata-only refreshes
and changes elsewhere do not change these local digests. Disabling returns empty
dependency tokens; re-enabling restores them. Revision digests describe overlay
contributions, not the complete baseline/terrain/building-generator dependency.
Future consumers must combine them with their existing baseline/code keys.

`Commit.affected_bounds` and `Snapshot.affected_bounds` conservatively bound changed
entries for refresh/enable/disable/rollback operations. They are hints; dependency
digests are the authoritative local content comparison. No cache is touched here.

## Corruption, limits and remaining scope

Loading checks schemas, checksums, bounded document sizes, revision paths, parent
links, geometry/part consistency and metadata. Unknown schemas, missing/corrupt
objects, duplicate JSON keys, nonfinite JSON constants and links/reparse points
are errors. Storage never silently resets itself or treats corrupt data as an
empty overlay. `recover()` explicitly restores `previous.json` only when current
state is corrupt and the entire referenced backup snapshot validates. If both
copies or their objects are damaged, recovery stops; retain an external backup.
Checksums detect damage; they do not authenticate against a malicious writer.

Default hard limits: 8 MiB/document, 64 MiB total storage, 128 revision objects,
2,048 entries including parts, 512 features per input sequence, 2,048 conflicts,
128 distinct refresh records, and 10 km maximum refresh extent. Stage 1's geometry
limits still apply. `Limits` can be explicitly configured. No automatic pruning
deletes history, unresolved conflicts or orphan objects; reaching a limit rejects
the write while preserving prior state. Backup/export/garbage-collection tooling
is outside Stage 2. Reads validate the current revision, not every historical
object; a damaged historical target causes rollback to fail without publishing it.

Stage 1 limitations remain: simple polygons only, conservative spatial matching,
no automatic deletion/split/merge resolution, complete part sets for parent
changes, and no live rendering or cache invalidation. Storage scans snapshots
rather than maintaining a spatial index. Large-area workloads are unsupported.

## Reproducible tests

On Windows x64, bootstrap once from the repository:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/test_building_overlay.ps1 -Bootstrap
```

The script pins CPython 3.12.10 and NumPy 2.2.6 by SHA-256, downloads/extracts only
under the git-ignored `.venv/overlay-tests/`, and uses no pip or system Python.
Subsequent runs require no network:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/test_building_overlay.ps1
```

The execution policy option applies only to this PowerShell process; no machine
or user execution policy is changed. This runs all Stage 1, persistence and OSM retrieval tests
plus the existing `mapgeom` test.
Fixtures, temporary directories and subprocess working directories stay inside
the repository. Tests cover actual process interruption, cross-process/thread
writes, corruption/recovery, rollback, enable/disable, overlapping selections,
stale exports, per-area digests, baseline immutability and resource bounds.
