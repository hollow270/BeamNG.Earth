# Agadir building collision hold correction

Investigated from clean `9c5da45e316dd5004eb43c4c6e27826c28000093` on
`feature/restart-osm-buildings`, after the real BeamNG.drive 0.40.1 report.

## Confirmed mechanism

The committed way/1572086438 footprint and the actual emitted
`rg_g17_62058_53903.json` reproduce the problem with the real Lua placement inverse.
The spawn `30.412918583438707, -9.552353024797444` maps to tile coordinates
`(-107.0390185, 105.5883997, 90.9684392)`. The reference keep-out box is
`[-123.30, 90.45, 89.37, -107.95, 104.07, 106.32]`.

The car is **0.911 m east and 1.518 m north of the box**, and about 6 m outside
the actual diagonal wall. `PLAYER_R=3` expands that enclosing rectangle over the
spawn. The original `playerIn()` returns true. No unexpanded building box contains
the spawn; only the reference box triggers the expanded test.

The tile has **one bldcol mesh and 57 building boxes**. A positive test for one
box withholds the whole collision mesh, while the separate bld mesh remains
render-only and visible. `releaseHeld()` repeats the same predicate, so standing
at the safe spawn cannot release it. `noteNewColliders()` uses the same predicate
to remove a newly imported collider before rebuilding physics if the car moved
inside it. That guard must remain.

The retained ray outputs show 5.9999814 m in session `20261102-004149-a736` and
the full 7.99999998 m ray length (no hit) in session `20261102-013735-38fc`.
The initial queue-to-held path does not log a hold message or increment
`solidAround`; both recorded near snapshots have `solidAround=0`, no road error
and `walksQueued=0`. Their absence therefore does not rule out this hold. Old
status did not report the held queue. Collision metadata and mesh files exist,
and the server reported no merge conflict or missing source record.

The defect affects every building sharing this collider, explaining how a
neighbour can also lose collision while houses belonging to other tiles remain
solid. The second house was not identified by coordinates, so retained evidence
does not establish its exact tile. The source/queue failure is reproduced; the
individual physical contacts still require the user's retest.

## Small correction

`roads.lua` adds `BUILDING_PLAYER_R=0` for bld/bld2/bld3/bld4/bldcol only. Their
existing boxes already enclose complete footprints, including empty corners of
rotated or concave shapes. The centre-inside test and vertical margin remain;
only the extra horizontal expansion is removed. The generic 3 m radius, sidewalk
and deck rules, drop hold, queues, governor, ownership and collision-rebuild
timing are unchanged. No mesh, source, server protocol, cache version or Rust
change is needed. `server/buildings.py::collider_boxes()` was inspected and its
real rounded output is verified by the regression, but is unchanged.

The guard remains an AABB/vehicle-centre test, not a complete vehicle-volume or
exact footprint intersection test. Empty corners *inside* an unexpanded AABB can
still conservatively hold a tile. This correction does not redesign that policy.

Read-only status now includes `heldMeshes`, `heldBuildingMeshes`, and up to eight
`heldColliders` (key/kind), with `heldTruncated`. No timer or world mutation is added.

## Focused evidence

- Six Python/LuaJIT cases pass: exact committed footprint/box reproduction;
  complete shared collider created outside; actual polygon-inside vehicle held
  and released when clear; pre-rebuild inside removal; outside mesh survives
  rebuild; counterfactual old-padding whole-tile hold and corrected release.
- Existing standalone Lua suites pass: `test_roads` 92 checks, `test_roadgeo` 31.
  The harness supplies JSON encoding and a deterministic clock instead of engine
  globals/Windows clock granularity. No broad repository suite was run.
- Guarded tests permit no external application or network launch. Synthetic
  scene objects are not proof of actual BeamNG collision readiness.

Original logs, rays, emitted metadata and collider bytes are retained under
`.venv/restart-osm/agadir-collision-investigation/`. The original engine kit/profile
is not patched or cleared. The minimal follow-up kit is prepared separately at
`.venv/restart-osm/agadir-collision-test/`, sharing only the old immutable runtime
and application. Its cache/profile are separate copies and its port is 47732.
Follow that kit's README for a stationary-spawn ray/contact retest. Engine
confirmation is pending; no game was launched and no commit/push was made.
