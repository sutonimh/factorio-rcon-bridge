# GOTCHAS — hard-won rules for driving Factorio over RCON

Every mistake below cost a real iteration. Read before changing autopilot behavior.

## BUILD CONVENTIONS (standing rules from Seth — follow on EVERY build)
- **Placement zoning:** ONLY mining infrastructure and defenses (turrets) go on/at
  ore patches. EVERYTHING else (smelting, assembly, labs, science, storage) goes at
  the BASE location (~10,-30). Never put a smelter/assembler on an ore patch.
- **Walk to the work site, ALWAYS (Seth's standing rule):** before doing work at a
  location (building, fueling, placing ghosts, mining), `walk()` the character there
  so Seth can SEE it happen. Never `player.teleport`. Don't operate remotely while the
  character stands somewhere else. He wants to watch everything, in real time.
- **Keep it legit (no cheats):** build/fuel/move via the character + `create_entity`/
  inventory ops like a player would; never force-spawn finished items, instant-research,
  or hand-set progress. Progress the tech tree legitimately.
- **NON-STOP work, never stand idle (Seth's standing rule):** the character must always
  be doing something visible. Don't stop between actions. Chain walks continuously
  (don't set walking_state=false then sit while running RCON). ALL idle/wait time
  (research finishing, crafting, builds settling) MUST be spent on refueling +
  restocking maintenance, walking a patrol of the base.
- **Keep EVERYTHING fueled, especially the smelter stack (standing rule):** every
  maintenance pass tops up all stone furnaces (the iron + copper smelter stacks),
  boilers, and burner drills from the coal stock chest (20.5,-1.5). Never let the
  smelter starve. `keep_fueled()` does this; run it constantly.
- **Make material handling VISIBLE (standing rule):** when taking from a chest or
  inserting into a machine, do it AT the chest/machine with the character present and,
  where possible, route through the character's own inventory (walk to chest -> take
  coal into inventory -> walk to furnace -> insert) rather than a silent chest->machine
  script transfer, so Seth can see the material move.
- **Proper pathfinding (standing rule):** walking must be smooth, no stutter/stopping.
  Pre-route around obstacles and follow waypoints, re-sending direction only on a real
  turn; keep walking_state=true through the whole route.
- **Blueprint-first, then cadence:** to build anything, (1) `stamp_blueprint()` the
  ghosts, (2) ASK Seth to check/approve, (3) only then `build_ghosts()` which builds
  in a realistic player-like cadence (a couple at a time with delay).
- **Snapshot after every placement:** run `snapshot()` + commit after any build op,
  so `rebuild()` can restore it.
- **Storage:** the overflow chest array lives in its own CLEAR zone (-20,-36), never
  adjacent to other builds.
- **Defenses:** turrets start full (100 mags) on deploy when ammo allows, refill at
  <50%, and `produce_ammo` ramps when low. `fortify` auto-scales the ring to nearby
  nest count and weights toward the nearest nest (even ring if none).
- **In-game notepad:** keep the task queue on-screen via `notepad()` (rendering API),
  not just `game.print` (which scrolls away).

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

## Fluid verification (the unlock)
- `entity.get_fluid_count([name])` is a METHOD and WORKS even though `.fluidbox`
  and `.neighbours` are blocked. This is THE tool for verifying fluid hookups:
  place a pipe, wait a few ticks, check `pipe.get_fluid_count('water')`. Probe
  connections tile by tile instead of guessing geometry.
- An offshore pump reads `get_fluid_count('water')==100` once drawing. A read in
  the SAME tick as placement shows 0 (buffer fills next tick) — settle ~2-3s
  before trusting a 0.

## Steam plant geometry (cost ~15 iterations, then solved by eye + get_fluid_count)
- Boiler water connections are on its two ENDS, perpendicular to the steam output.
  Face the boiler NORTH/SOUTH so water comes from its EAST/WEST ends and steam
  exits N/S. Facing it E/W puts the water inputs on the N/S ends (wrong if the
  pump is to the side).
- The pump's output sits on ONE specific tile-row east of its body. The pipe line
  AND the boiler's water-input row must match that exact row — a ONE-tile vertical
  mismatch = zero flow. Keep the pipe run a straight single line on that row.
- Steam engines chain steam through both ends: boiler steam-out -> engine -> engine.
  Place them in a line off the boiler's steam side.
- A small power pole within wire reach (7.5) of a steam engine injects its power
  into the grid; verify the chain by reading a consumer's `status` (`lab.status==1`
  = working/powered, not 58=no_power).
- Ratio: ~1 boiler : 2 engines; 1 pump feeds ~20 boilers.

## Belts / inserters / drills (positions ARE readable — use them)
- `drill.drop_position`, `inserter.pickup_position`, `inserter.drop_position` are
  readable (unlike fluidbox). Place a drill, read `drop_position`, put the furnace
  exactly there. Verified pattern: burner drill facing south at (x,-8) drops to
  ~(x,-7); a stone furnace centered at (x,-6) catches it -> smelts -> plates.
- Inserter `direction` here behaves as the PICKUP side: dir=8 (south) picks from
  the SOUTH tile and drops NORTH (opposite the "faces its drop" intuition). Always
  confirm with pickup_position/drop_position rather than assuming.
- CENTERING (this was the real bug, not "snapping"): an entity's position is its
  CENTER = top-left footprint tile + (tile_width/2, tile_height/2). So a 1x1
  (belt/inserter/chest) on tile (x,y) goes at (x+0.5, y+0.5); a 2x2 (drill/furnace)
  on top-left tile (x,y) goes at (x+1, y+1). Passing integer coords for 1x1 entities
  put them a tile off. `autopilot.place(name, tile_x, tile_y, dir)` does this right.
- A transport belt lane must be CONTINUOUS (no gaps) or items stop. Lay belt on
  every tile of the lane, then have inserters drop onto it.
- Burner drill status 36 = no drop target (needs a furnace/belt at its drop_position).

## Captured layout: double-sided mining belt -> chest (Seth's design, verified)
- Belt lane runs EAST (dir=4) along one tile row Y; centers at (x+0.5, Y+0.5).
- TOP drills at y=Y-1 facing SOUTH (dir=8) drop ore onto the belt from above.
- BOTTOM drills at y=Y+2 facing NORTH (dir=0) drop onto the belt from below.
  (A south drill at center (x,Y-1) drops to belt tile x; a north drill at (x,Y+2)
  drops to belt tile x-1 - they interleave to fill the lane densely.)
- East end: a burner inserter facing WEST (dir=12, so it picks from the belt to
  its west) drops into an iron-chest one tile further east.
- All burners (drills + burner inserter) need coal. Verified ore flow:
  drills -> belt -> inserter -> chest.
- Inserter direction = pickup side, CONFIRMED across 3 examples: d0 picks N, d4 E,
  d8 S, d12 W; drops the opposite tile.

## Captured layout: coal auto-fueling loop (Seth's design)
- A coal belt is routed PARALLEL to the drill rows so a per-drill inserter can pull
  coal off it into each burner drill. Layout around the mining block:
  - top coal belt one row above the top drills (here row Y-4), bottom coal belt one
    row below the bottom drills (row Y+4), joined by a vertical belt on the west side
    that turns the incoming coal feed down both sides.
  - each TOP drill: a burner inserter between coal belt and drill, dir=N (picks coal
    from the belt to its north, drops into the drill to its south).
  - each BOTTOM drill: inserter dir=S (picks from the south coal belt, drops north
    into the drill).
- So a self-fueling mining block = ore belt (middle) + coal belt loop (outside) +
  one fuel inserter per drill. Coal feed comes in from the coal patch end.

## Power is the first thing to check when MANY electric inserters read 58
- status 58 = no_power. When a whole region of inserters (taps, feeders) reads 58
  AND assemblers/labs read 0/idle while BURNER furnaces still have fuel, the steam
  plant is DOWN, not a per-inserter problem. Diagnose top-down: steam-engine
  `energy` (0 across all = dead plant) -> boiler `get_fuel_inventory().get_item_count('coal')`
  (0 = starved) -> refuel + reheat ~40s. Don't chase individual inserters first.
- A newly placed pole that reads buffer 0 may just be an ISLAND (no powered pole
  within the 7.5 wire reach). Confirm by comparing `electric_network_id` against a
  known-working consumer (a `lab` with status==1); bridge islands with intermediate
  poles <=7.5 apart. Small-pole SUPPLY area is only ~2.5 (powers a 5x5), separate
  from the 7.5 wire reach (connects poles).

## Belt FLOW DIRECTION must point at the consumer (cost a "wrong way" report)
- An output belt (e.g. smelter plate belt) must carry items TOWARD the base/consumer.
  A plate belt laid all-East (dir=4) carried plates AWAY from the westside science
  cluster: the west tap starved while plates piled at the dead east end. Always
  verify the belt row's direction histogram points the right way before wiring a
  tap. Reverse with `belt.direction = 12` (W) per tile.

## Don't tap a collinear belt with an inserter - just connect the belts
- If the source belt and the destination belt are on the SAME row flowing the same
  way with only a gap between them, DO NOT drop an inserter to lift-and-redrop. One
  inserter throttles the whole feed to ~0.8 items/s (the original science bottleneck)
  and is pointless. Place a belt in the gap tile so it's one continuous lane. Only
  use an inserter where you must cross OFF a belt into a machine/chest (belt->chest
  load still needs an inserter).

## Smelter ore feed: two storage chests (Seth's layout)
- Seth set up ONE storage chest per smelter stack, each with a loader inserter that
  drops onto that stack's distribution belt (iron belt y=-28 runs E; copper belt
  y=-40 runs E); 12 ore-loader inserters pull off the belt into the 12 furnaces.
  - iron storage chest @(-1.5,-25.5) -> iron stack ; copper storage chest @(-1.5,-37.5) -> copper stack.
  - mine chests (drill output): iron @(17.5,0.5), copper @(1.5,6.5). Drills sit at
    status 36 (waiting_for_space_in_destination) once their mine chest fills (1600).
- `fill_ore_chests()` tops the two storage chests from the mine chests on the maintain
  loop; draining the mine chests also un-sticks the drills. NOT offline-proof (needs a
  physical mine->storage belt for that); it's the software feed while I'm active.
- Single loader inserter per stack caps throughput (~6/12 furnaces run in steady
  state). More furnaces working needs a 2nd/faster loader, not more chest fill.

## Inventory contents API (2.1)
- `inventory.get_contents()` returns a LIST of {name,count,quality} entries, NOT a
  name->count map. Iterating `for n,c in pairs(...)` gives c as a TABLE and crashes
  on concat. Use `inv.get_item_count('name')` for specific items, or index the
  entry fields.

## Megabase ghost placement (Nilaus/factoriobin blueprints over RCON)
- Endgame blueprints can be placed as GHOSTS regardless of tech (no research/bots
  needed to lay the plan); they auto-build later once entities + construction robots
  exist. This is the legit way to "place" a megabase early (Seth's choice).
- `build_blueprint{surface,force,position,force_build=true}` returns the ghost list;
  it returns 0 (places nothing) when:
  1. Target chunks aren't generated -> `request_to_generate_chunks` + `force_generate_chunk_requests` first.
  2. Terrain obstacles (trees/rocks/cliffs) collide -> CLEAR TERRAIN FIRST: destroy
     `find_entities_filtered{type={'tree','simple-entity','cliff'}}` in the footprint
     (Seth's standing rule: clear terrain before placing).
  3. The blueprint has `absolute-snapping=true` + `snap-to-grid` -> it snaps to a fixed
     world grid and can collide with already-placed blocks; pop snap-to-grid/
     absolute-snapping/position-relative-to-grid from the BP json to place at an exact spot.
  4. Overlaps existing entity-ghosts (e.g. two modules whose footprints intersect).
- Tile the City Block grid at the blueprint's snap period, aligned to that grid
  (origins at exact multiples), or blocks collapse onto each other.
- MOVE THE CHARACTER to the placement area first (Seth wants to watch it happen):
  `walk(tx,ty)` to the site, then place. Don't place ghosts remotely while the
  character stands elsewhere.
- Parallel placement via subagents is safe if each agent's import_stack+build_blueprint
  +clear is ONE atomic /sc command (no interleave) and uses a unique temp file.

## RCON client protocol
- Don't use the empty-RESPONSE_VALUE end-marker trick — Factorio doesn't echo it,
  so the read hangs. Read one response packet, then drain with a short timeout.
