# GOTCHAS — hard-won rules for driving Factorio over RCON

Every mistake below cost a real iteration. Read before changing autopilot behavior.

## Achievements
- Hosting a save as multiplayer (required for RCON) disables Steam achievements.
  Running ANY `/c` or `/sc` console command also disables them. The bridge IS
  multiplayer + console, so achievements and the bridge are mutually exclusive.
  Never tell the user "achievements intact."

## Hosting / data dir
- A running Steam GUI client holds the DEFAULT data-dir lock. The headless server
  must run on its OWN data dir (`~/factorio-server-data`, via `--config`) or the
  GUI client can't launch ("Couldn't acquire exclusive lock ... /factorio/.lock").
- Have the user save in-game and quit before hosting; load the save by absolute path.

## Driving a CONNECTED player (these are client-authoritative)
- `player.walking_state` WORKS server-side → real, visible walking. Set
  `{walking=true,direction=D}`, poll `player.position`, then `{walking=false}`.
- `cursor_stack` / `build_from_cursor` do NOT work for a connected player. The
  cursor is client-owned; `can_build_from_cursor` returns false even in reach.
  You CANNOT animate hand-builds. Use `surface.create_entity` + `inv.remove`
  (conservative). The building appears with no place-animation, then runs/animates.
- `player.mine_entity` returns nothing for the connected player → no scripted
  hand-mining animation. To "mine": deplete-and-insert — reduce the resource
  entity's `amount` by N and `inv.insert` N of its product. Conservative (patch
  loses exactly what inventory gains), but instant (no animation).

## Placement
- `surface.can_place_entity{...}` with the DEFAULT build_check_type works.
  Do NOT pass `build_check_type=defines.build_check_type.manual` — it includes
  player collision and fails when the character is nearby.
- Direction constants (2.0/2.1 are 16-direction): N=0, E=4, S=8, W=12.
- Walk the character NEAR a build site (tol ~3), never onto water or the footprint.

## Offshore pump (cost several iterations)
- Only specific shore tiles validate for a given direction; the engine enforces
  the water/land geometry. Brute-force: loop candidate water tiles × the 4
  directions with `can_place_entity`.
- Setting `entity.direction` on a PLACED offshore pump does NOT stick (reverts).
  To reorient: `destroy()` (refund to inventory) + `create_entity` with the wanted
  direction at a tile that validates it.
- DIRECTION SEMANTICS (empirical): placing with `direction=4` (East) made the
  OUTPUT face WEST. So `direction` points at the WATER/intake side; output is the
  OPPOSITE side. To get output facing EAST (toward land/base), place with
  `direction=12` (West) on a tile with water to the WEST and land to the EAST.
  Always confirm against neighbor land/water tiles via `surface.get_tile`.

## Fluidbox
- `entity.fluidbox` is NOT accessible in this build ("LuaEntity doesn't contain
  key fluidbox"). You cannot read live pipe-connection tiles. Compute fluid
  geometry from `prototypes.entity[name]` collision_box + fluidbox_prototypes
  connection offsets, rotated by direction.

## RCON client protocol
- Don't use the empty-RESPONSE_VALUE end-marker trick — Factorio doesn't echo it,
  so the read hangs. Read one response packet, then drain with a short timeout.
