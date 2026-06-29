# GOTCHAS — hard-won rules for driving Factorio over RCON

Every mistake below cost a real iteration. Read before changing autopilot behavior.

## STANDING PRACTICE: always be learning + the CODEBASE is the source of truth (WORKFLOW RULE)
Seth's directives (mandatory workflow):
1. **Codify every mistake.** After ANY mistake, surprise, or hard-won fix: add a lesson here
   AND fix it in code, before moving on. This file is the project memory (Factorio is non-Abyss;
   lessons live here, never in Abyss memory).
2. **The codebase auto-grows; don't rely on my memory.** Every feature/tweak/fix becomes a
   function in `autopilot.py` / `bootstrap.py` (not a one-off live RCON script I hand-drive), so
   the base builds + runs WITHOUT my per-step input. Prefer: add/extend a function -> call it ->
   it self-runs. The reusable pieces: `bootstrap.py` (fresh-world sequence + provisioner +
   maintain loop), `autopilot.py` (primitives), `techdb.py`+`tech-tree.json` (gating).
3. **On a new world, immediately run `bootstrap.bootstrap()`** then `bootstrap.maintain()` - no
   thinking time.

Recent lessons codified:
- **POWER MUST NEVER DIE - monitor + refuel the steam plant as the TOP priority (Seth).** If the
  boiler's coal runs out the engine stops and ALL electric machines (assemblers, labs, inserters)
  halt. The boiler buffer chest being empty = power death. The boiler-buffer gate must be checked
  EVERY lap and resolved before long builds; long build tasks must not run the plant dry.
- **When REBUILDING/replacing a setup, TEAR DOWN the old one (Seth).** Don't leave the superseded
  build standing (furnaces at mine patches, old scattered assemblers, dead poles). Refund + remove
  it in the same pass (`build_mine_outpost` clean-slates patches; `setup_science_io` removes old
  assemblers).
- **Build to MINIMIZE run distances (Seth).** Place new infrastructure near where it's used / near
  the character / near its inputs, so maintenance hauls and build trips are short. Don't scatter.
- **When SWAPPING an entity, recreate it at the OLD one's EXACT position - never a guessed offset
  (Seth).** I swapped the coal-mine burner inserter for electric but placed it a tile off, ON the
  belt instead of adjacent to the chest, so it didn't load the chest. Capture `old.position` (and
  `direction`) before destroying, and create the replacement at that exact position. A belt-side
  output inserter sits ADJACENT TO THE CHEST, picking off the belt and dropping into the chest -
  it must not sit on the belt.
- **PRIORITY MODEL (Seth): build pending tasks FIRST when able; only switch to refuel/refill
  when a GATE blocks; resolve the gate; resume building. Rinse + repeat.** `maintain()`:
  `if _gated(): clear it; elif BUILD_QUEUE: do next build; else: light upkeep`. A separate
  server-side SCIENCE strand (thread; RCON is thread-safe - fresh socket per call) always
  progresses research so the character's hauls never stall it. `_gated()` = boiler coal <20%,
  any drill low fuel, an outpost chest full enough to haul, or character low on coal.
- **SUPPLY ARCHITECTURE (Seth): scaled MINE outposts, base smelts EXCLUSIVELY.** Each patch =
  `build_mine_outpost(ore,n)`: a row of drills all dropping onto ONE belt -> inserter -> OUTPUT
  CHEST. No furnaces at patches (`build_mine_outpost` clean-slates any). The character HAULS ore
  from the output chest to the base smelter array (`haul_ore`), loading iron into the 8-furnace
  stack and copper into the 4-furnace stack (separate `FURNACE_AREA` per ore - mixing them = no
  copper plates). Build outposts for iron, copper, AND coal.
- **FUEL: drills mining ore don't self-fuel; refuel them PROACTIVELY (Seth).** A dry drill stops
  producing -> chest never fills -> no haul trip -> never refueled = deadlock. So `haul_ore`
  visits an outpost when it has ore OR its drills are low on fuel, and refuels all its burners.
  `restock_coal` keeps 6-12 stacks of coal in inventory from the COAL MINE chest and refuels the
  coal drills too. `ensure()` HAULS from a mine's output chest before ever hand-mining.
- **ALL LABS ALWAYS RUNNING is a priority (Seth).** Labs are fed by HARDWARE: a feed chest +
  powered inserter above each lab; `service_science` tops each feed chest EVENLY (not the first
  lab to 10). Power the feed inserters (they sit north of the labs, outside the lab-row poles).
- **ASSEMBLERS use INPUT/OUTPUT chests + inserters (Seth), not just a software shuffle.**
  `build_io_cell(recipe,x,y)` = [input chest][in inserter][assembler][out inserter][output chest]
  + pole; `setup_science_io()` rebuilds the chain spaced (7 wide/unit - the old 4-spacing was too
  tight for chests). `_service_assembler_chests()` fills input chests with each recipe's
  ingredients and empties output chests to inventory every science lap; the inserters do the
  machine I/O so the chain flows continuously.
- **PATHFINDING: string-pull to FEW waypoints via the L-PATH the walker takes, and CACHE it.**
  Collapsing A* steps staircased off-45 diagonals -> 68 jagged waypoints -> oscillation. Fix:
  `_clear_Lpath` string-pull -> a handful of glide-able legs (diagonal then cardinal). Routes are
  cached by start-region+goal in `_route_cache` and only recomputed when the character genuinely
  STALLS (deviates) - don't recompute every walk. Always `stop()` before re-pathing.
- **Automated science = assemblers (parallel production) + a software SHUFFLER, not belts.**
  `service_science()` (in `maintain()`) is GENERIC: for every assembling-machine it feeds each
  recipe ingredient from inventory and pulls the output back, so any chain (cable->circuit->
  inserter->belt->green-pack; gear->red-pack) self-runs with the inventory as the 'bus'. Place
  assemblers + `set_recipe`; the loop does the logistics. `automate_green_science()` builds the
  green chain; `_advance_research()` keeps research targeting the next fuelable tech.
- **`defines.inventory.assembling_machine_input` errored nil and silently broke feeding** (253
  iron plates sat unused, 0 assemblers worked, research stalled). Use the ROBUST API instead:
  `a.insert{name,count}` routes an ingredient to the input, `a.get_item_count(name)` reads an
  ingredient's input count, `a.get_output_inventory()` for products. Don't use inventory-index
  defines for machine I/O. (Diagnose stalls by reading assembler status + inputs, per Seth.)
- **The supply (iron) is the perennial bottleneck.** `build_outpost(ore,n)` builds burner
  drill->furnace rows for continuous plates; `maintain()` collects them. The provisioner's
  long iron-patch<->base smelting shuttle is slow - the TODO is smelt-at-nearest-furnace +
  automated coal delivery so supply builds are fast (don't hand-shuttle bulk).
- **NEVER blind-fire `begin_crafting` (Seth, repeated).** It spams "not enough ingredients".
  Use `bootstrap.make(recipe,count)`: it computes raw needs (`raw_cost`), GATHERS them
  (`ensure_plates` mines ore + smelts; `ensure` mines coal/stone/ore), THEN crafts. `_craft_wait`
  guards on `get_craftable_count` and diagnoses the missing ingredient (`missing_for`).
- **Coal buffer for boilers (Seth):** boilers must have a chest + burner inserter feeding them
  coal so they don't starve before auto-mining exists (`bootstrap.coal_buffer`). The inserter
  MUST sit on a tile ADJACENT to the boiler (drop lands IN it, not a gap) and REUSE an existing
  chest rather than dropping a new empty one. `refill_buffers()` tops any boiler-adjacent buffer
  chest that falls <20% (mining coal if short); run it every `maintain()` lap.
- **Burner-inserter status 36 = waiting_for_space (NORMAL when the boiler fuel slot is topped).**
  A lightly-loaded boiler burns slowly, so the inserter idles with the chest full behind it and
  feeds on demand. Don't "fix" a working buffer; verify power is up (engine.energy>0) instead.
- **Consolidate like buildings (Seth):** when adding labs, place them ADJACENT to existing ones
  (one cluster), don't scatter. Same for any repeated structure.

## TOP LESSONS (the expensive ones, read first)
- **WORK FROM THE TECH DB; don't discover gating via failed crafts (Seth's rule).**
  `tech-tree.json` (277 techs, 631 recipe->tech mappings, dumped live) + `techdb.py` give the
  prereq chain, science packs, and TRIGGER flags for any recipe: `python3 techdb.py <recipe>`
  or `techdb.report('roboport')`. Check it BEFORE crafting/building anything that might be
  gated. Re-dump after big version changes. Key revelations it surfaces:
  - Space Age `assembling-machine-1` is NOT free - it needs `automation` research (red
    science). You bootstrap with HAND-CRAFTED red science packs in a lab, not an assembler.
  - Many early "techs" are CRAFT-ITEM TRIGGERS that auto-complete from normal play:
    `steam-power` (craft iron-plate), `electronics` (craft copper-plate),
    `automation-science-pack` (craft a lab). So smelting your first plates + crafting a lab
    silently unlocks red science + electronics. `oil-processing` triggers on mining crude oil.
  - Full path to construction-robot = 21 techs needing red+green+blue science (the long pole
    is the oil economy for blue).
- **FRESH WORLD: always remove the crash-site spaceship debris first (Seth's rule).** A new
  Space Age Nauvis litters spawn with ~11 `crash-site-spaceship-wreck-*` pieces (+ ship/loot
  chests). `clear_spaceship_debris(radius=300)` collects any loot then destroys every
  `crash-site-*` entity. Run it as part of fresh-world setup before building at spawn.
- **Drill the RICHEST part of a deposit, not the nearest edge (Seth's rule, screenshot).**
  I anchored the first drill at the tile NEAREST spawn = the sparse eastern edge (5x5 ore
  density 213) when the thick field was ~19 tiles west (density 32,174, ~150x richer).
  `richest_spot(name, near_x, near_y)` returns the ore tile whose 5x5 neighbourhood holds the
  most ore; anchor drills there. Pick deposits by DENSITY, never by distance-to-spawn.
- **CLEARSPACE: >=10 tiles clear around EVERY building (Seth's rule, with screenshots).**
  Never build in/among trees, boulders, or cliffs. I planted the first drill+furnace in a
  dense cypress grove; Seth called it out. `clear_area(cx,cy,radius=10)` removes trees+rocks
  (and COLLECTS their wood/stone/coal - free bootstrap stone) and reports remaining CLIFFS;
  cliffs can't be mined without explosives, so if cliffs>0 you MOVE the build site, you don't
  build there. `build()`/`place()` now auto-clear a 10-tile radius before placing and ABORT
  with `CLIFF ...` if a cliff remains. For multi-entity builds, `clear_area` the whole site
  bbox+10 once up front.
- **ALWAYS `stop()` the character before killing/restarting any pathing driver.** A
  killed walk process leaves `walking_state={walking=true}` set in-game, so the character
  RUNS ENDLESSLY in the last direction (Seth saw it crab off to x=-116 after I killed a
  walk). `walk()` now halts in a `finally` (covers normal/timeout/exception exits), but a
  PROCESS KILL bypasses Python finally, so the operational rule stands: send
  `game.players[1].walking_state={walking=false}` (or `autopilot.stop()`) FIRST, every time,
  before re-running pathing/patrol code. Also: the character must NOT move unless there is a
  task to do on-site (Seth's rule) - no idle wandering; walk only to a build/work location.
- **Smooth walking = pure axis/diagonal LEGS, never a continuous off-axis heading.** Aiming
  the 16-way `heading()` continuously at an off-axis target makes it snap-oscillate between
  two neighbouring directions (each held ~0.3s over RCON) = visible crab/triangle zigzag.
  FIX (in `walk()`): move in 8-direction legs - hold a 45-degree diagonal while both axes
  have distance left, then the remaining cardinal once one axis is consumed. One diagonal
  leg + one straight leg = one continuous glide each, no oscillation. `DIR8` maps sign(dx,dy)
  -> the 8 holdable directions.
- **A "robot-rush" factory blueprint is HAND-BUILT, not bot-built. Don't conflate "needs
  bots to build" with "needs robotics tech to craft its parts."** Seth's "All-In-One
  Early-Game Robot Factory" BP (374 entities) is the thing you HAND-BUILD to GET your first
  robots; it is not a bot-built endgame base. Verified gating from the live tech tree:
  assembling-machine-2=automation-2 (red+green), fast-inserter/undergrounds/splitters=red,
  chemical-plant=oil-processing (trigger), medium-pole=electric-energy-distribution-1
  (red+green); ONLY the single roboport + the bots it outputs need construction-robotics
  (red+green+BLUE). "With only blue science" = research up to construction-robotics, hand-
  build the factory, let IT mass-produce bots/armor/equipment. I wrongly told Seth it "needs
  robotics first / can't be hand-built." The check that settles it: query `f.recipes[name].
  enabled` + the unlocking tech's `research_unit_ingredients`, never argue tech gates from
  memory.
- **Space Age TRIGGER techs + research-queue quirks.** `oil-processing` unlocks by
  MINING crude oil with a pumpjack (research_trigger = mine-entity crude-oil), NOT the
  science queue - check `tech.prototype.research_trigger` before trying to queue. The
  whole robot path is gated behind it and needs the oil economy (science packs need oil
  PRODUCTS as ingredients) - no shortcut. Also: `f.research_queue = {names}` silently
  emptied the queue; use `f.add_research(name)` ONE tech at a time (it works). Nearest
  crude oil here was 440 tiles from spawn - scout oil early on a fresh world.
- **A blueprint base needs CONSTRUCTION ROBOTS.** The Nilaus/megabase books are meant to
  be stamped + bot-built. Without bots, hand-placing 300+ entities via create_entity
  floundered (agents over-analyze and stall; I made messes). The right objective is:
  bootstrap -> reach construction-robotics -> stamp the book -> bots build it. Don't try
  to "follow blueprints" by hand without bots. See FRESH-START.md.
- **Don't hand a vague 300-entity build to one agent.** It reads docs, measures forever,
  and never places anything. Either get bots first (stamp), or build in SMALL verified
  increments from ONE session (and watch out for multi-session character conflict).
- **Route belts DIRECT + cross with undergrounds (learned from a before/after Seth made).**
  My `build_belt` avoided EVERY belt as a hard obstacle, so it A*-snaked a long convoluted
  mess out of the boxed-in mine. Seth's fix: a near-straight belt up a clear corridor that
  dips UNDER the existing distribution/ore belts with underground belts where they cross.
  CORRECT routing: treat only NON-belt buildings (turrets, machines, poles) as hard
  obstacles; go through/over existing belts by placing an underground-belt pair (entrance
  before the crossing, exit after). Prefer a short direct corridor over a detour. A
  convoluted belt that avoids everything is as bad as one through a building. (build_belt
  needs rework to this model; current version snakes - do NOT use it as-is for dense areas.)
- **NEVER area-destroy belts (or anything) to tear down YOUR build.** An area-based
  `find_entities_filtered{area=...,name='transport-belt'}; destroy()` will delete
  EXISTING infrastructure in that box too. I wiped the coal supply line + iron feeder
  this way. Teardown must be SURGICAL: track the exact tiles/entities you placed (e.g.
  build_belt should return its tile list) and destroy only those. Recovery: `rebuild()`
  from a fresh snapshot restores missing belts, but FIRST remove your conflicting new
  build (it blocks restore at the original positions), then rebuild.
- **A mine/area with burner inserters has NO power.** A new ELECTRIC inserter placed
  at the iron mine read no_power. Before swapping a feed to electric or adding electric
  inserters at a mine, confirm/extend power there (or use a burner inserter).
- **Patrol removes unneeded infrastructure (Seth's standing rule).** Every maintenance
  patrol must prune stray infra, not just fuel/feed: orphaned belts (stray stubs from
  abandoned builds) and redundant/island power poles. `cleanup_infra()` (in maintain)
  conservatively removes truly isolated belts + island poles every lap; the patrol runs
  the deeper `remove_redundant.py` (poles whose coverage is duplicated) every 10th lap.
  Keep it CONSERVATIVE: never remove a connected belt line or a connectivity-bridge pole.
- **ALWAYS clean up your messes (Seth's standing rule).** Never leave stray/half-built
  junk behind: failed builds, orphaned poles, abandoned ghosts, test entities. When a
  plan is reverted or abandoned, remove what it placed in the SAME pass. I left a whole
  dead pole grid (incl. a column running into empty desert) after reverting the inserter
  swap; Seth had to point it out.
- **But pole cleanup must PRESERVE connectivity bridges.** Removing "poles that power
  nothing within 3 tiles" also deletes the bridge poles that link two sections of one
  network, splitting it and browning out everything downstream (I disconnected the green
  factory this way). Before removing a pole, check it isn't the only link between a
  powered source and a consumer cluster (compare electric_network_id before/after, or
  keep poles whose removal raises the count of distinct networks).
- **VERIFY the real cause before building a fix.** I diagnosed "plant undersized" and
  nearly built more boilers/engines, but the definitive check (boiler steam 399/400 FULL
  + all 35 electric consumers at no_power=0) proved the plant had ample headroom. The
  brownout was entirely my own pole cleanup disconnecting the factory. Diagnosis signals
  for power: FULL boiler steam buffer = supply>=demand (adequate); DRAINED steam buffer =
  deficient (scale generation). Don't infer "undersized" from one unpowered consumer.
- **Watch power capacity ONLY when the signal says so.** Generation needs scaling when
  the boiler steam buffer runs low under load (it didn't here). The big electric load is
  still ahead (electric furnaces) - size the plant to that when it lands, with medium poles.
- **Don't swap powered-by-fuel for powered-by-electricity without VERIFIED power.**
  Replacing 73 burner inserters with electric ones cascaded: the smelter/mining/boiler
  areas have NO power grid (they were burner BY DESIGN), so the new electric inserters
  went dark, the boiler coal-feed inserters starved the steam plant, the whole network
  lost power, and the base stalled (furnaces 1/25). ALWAYS verify a powered pole covers
  an inserter before converting it; swap in small batches with power-checks; and KEEP
  the steam-plant/boiler coal inserters BURNER (electric there = power-deadlock: if power
  dips they can't restart the plant).
- **Source materials from where they ACCUMULATE, not a fixed spot.** Research stalled
  because the green factory + crafters pulled iron from empty furnace OUTPUTS while
  3,091 iron plates sat overflowing in the science feed chest (the feed belt outran the
  cluster's consumption). Feed/restock logic must drain the chests that actually fill up.
- **Hand-crafting can't sustain multiple labs.** ~10 science packs per 95s by hand; 4
  labs drain far faster, so they sit half-fed (red XOR green) and research = 0%. Labs
  need AUTOMATED assembler production (the green sub-factory + a red line, scaled), not
  crafting. Crafting is only a one-shot bootstrap.
- **Small poles (2.5 supply) can't power a dense hand-built smelter.** Need medium/big
  poles (research electric-energy-distribution-1) to cover the stacks from the perimeter.
- **A pole you place may be an unpowered ISLAND.** Always confirm new poles share a
  working consumer's electric_network_id before relying on them.

## BUILD CONVENTIONS (standing rules from Seth — follow on EVERY build)
- **Placement zoning:** ONLY mining infrastructure and defenses (turrets) go on/at
  ore patches. EVERYTHING else (smelting, assembly, labs, science, storage) goes at
  the BASE location (~10,-30). Never put a smelter/assembler on an ore patch.
- **Walk to the work site, ALWAYS (Seth's standing rule):** before doing work at a
  location (building, fueling, placing ghosts, mining), `walk()` the character there
  so Seth can SEE it happen. Never `player.teleport`. Don't operate remotely while the
  character stands somewhere else. He wants to watch everything, in real time.
- **Route belts AROUND everything (Seth's standing rule):** a belt must never run
  through a building of ANY kind (turret, assembler, pole, furnace, chest) and must
  never cross/overlap another belt. Use `build_belt(sx,sy,gx,gy)` which A*-routes the
  belt avoiding every entity (and walks the character to the start first). Where a
  crossing is truly unavoidable, use UNDERGROUND belts to pass under the existing belt.
  Never lay a straight belt line blindly through the base.
- **Maintain FREE inventory space (Seth's standing rule):** never let the player
  inventory clog - queued builds need room. The patrol runs `manage_inventory()` each
  pass: offload excess bulk (copper/iron plate >300, ore, ammo) to chests, keep build
  items + a working buffer. NEVER over-pull materials (a 400-plate/cycle restock buried
  the inventory under 5,497 copper plates and stalled all builds). Pull only when low.
- **One controller at a time / patrol stands still:** multiple processes (other sessions
  sharing this dir) each issuing walk commands yank the character around (looks like
  teleporting). The patrol stays stationary so it never competes for the character; do
  builds from ONE session. All movement is `walk()` - NEVER `player.teleport`.
- **Patrol STANDS STILL (Seth's standing rule):** the maintenance patrol must NOT wander
  a perimeter. Maintenance is all server-side (fuel/feed/craft/cleanup via RCON), so the
  character stays put and only moves when a specific task needs it on-site (a build or
  repair calls `goto`). No aimless walking.
- **Walk to the build site first (`goto`):** every build/teardown starts with
  `goto(cx,cy)` (or `build_belt`, which does it) so Seth watches it happen on-site.
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
- **Maintenance patrol = service EVERYTHING (standing rule):** every patrol lap must
  ensure all structures have both FUEL and COMPONENTS. That means: keep the coal stock
  chest itself supplied (pull from the coal mine), fuel every burner (furnaces, boilers,
  drills, burner inserters), top up assembler component inputs (cluster copper/iron,
  green-factory chain), refill the ore storage chests, and top up ALL labs with BOTH
  red+green packs so every lab keeps working (not just the first-fed one). `maintain()`
  now chains pickup + fill_ore_chests + science_factory + service_components +
  keep_fueled + feed_labs; the patrol also crafts a red+green buffer each lap.
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
- LARGE reads (>~4KB) get truncated/lost in a single response (the known gamedb.snapshot
  (0,0) bug). FIX (used by architect.py): build the payload server-side into a `storage`
  global as a JSON string, `rcon.print(#str)` its length, then read it back in fixed-size
  slices via `str:sub(i,i+CHUNK-1)`. CRUCIAL: `rcon.print` appends a trailing newline to
  EACH response, so `.rstrip("\r\n")` every slice before concatenating, or you inject a
  control char into the JSON at every chunk boundary (invalid-control-character at char N).
  Compact JSON (helpers.table_to_json) has no other trailing whitespace, so the strip is safe.

## Power grid: never delete connector poles; self-heal islanded generators (2026-06-28)

The single worst recurring failure was the electric grid fragmenting so the steam engine got
ISLANDED from the base (and the belt-fed smelter arrays lost power) every maintenance lap.

Root cause: `dedupe_poles` removed "orphan" poles (any pole with no machine within 3 tiles). But a
pole powering nothing is almost always a load-bearing CONNECTOR: the bridge tying the generator to
the base, or a spine linking an array to the grid. Deleting connectors split the network. The old
"power-verified" guard missed it: 0.3s was too short for the brownout to register, and it never
checked for a network SPLIT.

Rules now codified:
- `dedupe_poles` removes ONLY redundant poles (another pole within 2.0 tiles), NEVER orphans, and
  reverts any removal that raises the electric-network count (`_network_count`) or unpowers a
  consumer. Settle 0.6s before judging.
- `ensure_grid_connected()` (called from `keep_power`, top priority) self-heals: if any steam engine
  is on a different network than the main pole network, it auto-bridges with a pole line. The grid
  repairs itself instead of needing a human to re-bridge.
- To check fragmentation by hand: count distinct `electric_network_id` across poles; a healthy grid
  is 1 (plus maybe tiny dead stubs). Engine buffer ~95% while consumers read `no_power` = a SPLIT
  (engine islanded), NOT a generation shortage.

## Belt-fed smelter arrays: lay belts server-side, flank the poles (2026-06-28)

- `autopilot.build_belt` (A* walker) snaked and left GAPS over 70+ tile cross-base runs, so the
  iron/coal mine->array belts silently never connected (only the copper one, hand-laid, worked).
  Use `lay_belt_path(waypoints)` instead: server-side, exact tiles, auto-undergrounds blocked spans
  up to 5. Each tile's direction points to the NEXT tile, so a CORNER auto-takes the new direction
  (a corner left in the old segment's direction sends items straight past the turn - the bug that
  broke the iron belt; ore reached the corner then ran east instead of turning north).
- Poles CANNOT sit on the furnace row (oy+2..oy+3) - `can_place` refuses them silently and you get
  0 placed. FLANK the array: pole rows above the plate belt (oy-1) and below the ore belt (oy+6).
- Inserter `direction` semantics are error-prone (the drain inserter ended up picking from the
  chest and dropping on the belt). ALWAYS set `pickup_position` + `drop_position` EXPLICITLY.
- Furnaces stall `full_output` if the plate belt backs up: give each array a plate-DRAIN (chest +
  explicit-position inserter) at the plate-belt east end; the autopilot pulls plates from it.
- find_entities{position=p, radius=0.4} can MISS an entity whose center is >0.4 from p even if p is
  inside its bbox (e.g. a furnace center 0.58 from an inserter drop) - a query artifact, not a real
  misalignment. Use the bbox or a larger radius to confirm.

## Burner mine drills starve when derpface parks away (2026-06-29)

Symptom: the whole base froze - all furnaces no_ingredients, labs missing_science_packs, research
stuck - while power was fine (93% buffer) and status.json stayed fresh. Root cause: the iron/copper
MINE drills are burner-mining-drills (coal-fueled), and derpface had parked at the coal mine far
away, so the distant drills ran dry, the mines stopped, and the ore supply collapsed up the chain.

Fix: `fuel_drills()` tops every burner mining drill from derpface's carried coal SERVER-SIDE each
maintenance lap (wired into the science strand next to fuel_arrays). Same pattern as fueling the
furnaces: never rely on derpface WALKING to a distant consumer to fuel it; do it server-side.
Watch derpface's coal budget - it now fuels the boiler + ~12 furnaces + ~18 drills, so restock_coal
must keep it topped (derpface parks at the coal mine for this). Electrifying the drills is the
eventual upgrade.

## Coal death spiral + Seth's furnace-design rules (2026-06-29)

**Coal death spiral (froze the whole base):** the coal mine's drills are BURNER (need coal to mine
coal). After connect_mine removed the coal mine's output chest, coal went to a belt with no consumer
-> belt backed up -> derpface couldn't restock (restock_coal pulls from a CHEST, not a belt) ->
derpface hit 0 coal -> fuel_drills couldn't fuel the coal mine's own burner drills -> coal mine
stopped -> nothing could be fueled -> total deadlock. Fix: `ensure_coal_restock()` puts a self-
fueling BURNER inserter (NOT electric - there's no power that far north) moving coal belt -> chest,
so restock_coal always has a source. Wired into the science strand. The coal belt being backed up to
the mine is GOOD (= full supply); the bug was the missing belt->chest hop for restock.

**Furnace-stack design rules (Seth fixed these by hand; learn them):**
- Do NOT mix ores - keep the iron ore belt and copper ore belt strictly separate. A shared/crossed
  ore belt feeds copper into iron furnaces (wrong product).
- Coal goes on a SEPARATE LANE from ore, never the same lane. Coal + ore on one lane jams the belt
  ("iron block"). Two-lane belt: ore on one lane, coal on the other; the loader inserter grabs both.
- EVERY furnace stack needs coal, including the COPPER furnaces. Don't fuel only iron.
- Keep coal always flowing on the belts, or backed up all the way to the mine (= full supply).

(Server-side fuel_arrays is the current fueling mechanism and works WHEN derpface has coal; the
above are the belt-fed design Seth wants. Either way: never route coal onto an ore lane, never cross
ore belts, and keep the coal restock (burner inserter -> chest) alive so derpface never runs dry.)

## Scaling discipline + self-feeding coal mine (2026-06-29, learned from Seth's hands-on fixes)

I scaled smelting aggressively (iron 8->16 furnaces, copper 4->12) WITHOUT first scaling power or
hardening the coal supply. Result: cascade failures that froze the whole base, which Seth fixed by
hand. The hard lessons, codified:

**1. Scale POWER before production.** More furnaces = more electric inserters. The single
boiler+engine (~900 kW) was fine at 8+4 furnaces but the 16+12 scale-up pushed it to 0% buffer ->
every electric inserter browned out -> furnaces couldn't be loaded -> total stall. ALWAYS add
boiler+engine capacity to match new inserter load FIRST. Rule of thumb: ~1 boiler+engine pair per
~8-10 furnaces of inserter load; build them on a lake (offshore pump -> boilers -> engines).

**2. Self-feeding coal mine (Seth's design).** The coal mine's drills are BURNER (need coal to mine
coal). The robust design Seth built: inserters loop coal from the mine's own output belt back INTO
the drills (self-sustaining, never dies), AND the output belt is connected to deliver coal to the
base, AND a coal stock chest sits at the mine for derpface to restock from. My version left the
output dead-ended and relied on derpface server-side fueling -> death spiral when derpface hit 0.
Never leave the coal mine dependent on derpface; make it self-feed + deliver.

**3. Do NOT build power plants (or any multi-entity FLUID build) blind via RCON.** The
pump->boiler->engine water/steam connections and boiler-row water sharing are too finicky to place
reliably without seeing the fluid network; I failed 4 straight attempts. Seth placed a correct
boiler+engine column on the lake in seconds. Defer power-plant + pipe/fluid builds to a human with
eyes on the game, or only attempt with live supervision. Poles, belts, inserters, and server-side
logic ARE safe to build blind; fluids are not.

**4. Either power-loss OR coal-starvation cascades to a FULL base stall** (everything idle, looks
identical). When the base freezes, check BOTH: engine buffer (power_ok) AND the coal restock chain.
Harden both before scaling production again.

## create_entity{player=p} FAILS for the player-less derpface - it broke ALL autopilot builds (2026-06-29)

THE big one. `A.place` / `A.build` (and a few other builders) called
`s.create_entity{..., player=p}` where `p=storage.derpface`. derpface is a PLAYER-LESS character
(`derpface.player == nil`, since CHARON Phase 3 made it a 24/7 autonomous character, not a connected
player's body). `create_entity`'s `player=` field expects a LuaPlayer/index/name - a character entity
is not one - so the whole RCON command errored: `Invalid PlayerIdentification. Expected LuaPlayer,
index or name.` EVERY build placement silently failed (returned the error string, not 'BUILT'), so
`build_mine_outpost` placed nothing and returned None. The base only ever got built earlier, when
derpface WAS a connected player's character (player != nil); since going player-less, autonomous
building was dead and nobody noticed until a build was actually triggered (the relocation feature).
FIX: drop `player=` entirely (it only sets build attribution/undo, which we don't need). Verified:
`A.place` returns 'BUILT' again. This unblocks ALL autonomous building - the enabler for a fresh map
driving itself to robots. RULE: never pass `player=` a character entity in create_entity; omit it.

## Relocation must be SAFE: build-first, never strand the base (2026-06-29)

The first auto-relocation (`ensure_ore_supply`) tore down the failing iron outpost, then the rebuild
failed (the player=p bug above) -> 0 iron drills, WORSE than before. And with 0 live drills the next
trigger computed a (0,0) centroid and would have torn down at the ORIGIN/base. Lessons codified:
- BUILD FIRST, commit only on success. Don't tear the old outpost down before the new one verifies
  (`chest` not None). On failure REVERT `STATE[ore]` and set a cooldown so it doesn't retry-spam.
- PAUSE the reaper during a relocation build (`_REAP_PAUSE`): the science strand's `reap_dead_drills`
  runs concurrently and will kill freshly-placed drills (which momentarily read no_minable_resources)
  mid-build.
- SWEEP stranded iron-plate into the inventory before building (`_sweep_iron_plates`): the build
  needs to craft a burner-inserter, and the relocate-while-iron-starved trap is real (plates sit in
  base chests while the inventory has 0). Pull them first.
- Never teardown at a (0,0) centroid (the live==0 case): guard it.
- Trigger on per-tile ore UNDER the drills (thin) + a >=2x richer patch, not drill count - the iron
  outpost had 11 live drills on a 425/tile sparse edge while the dense 1071/tile field sat 14 tiles
  away. Healthy patches (copper ~1054/tile) must never relocate (no thrash).
- EMERGENCY RECOVERY pattern (autopilot stopped): drive the game from the Mac with
  `FACTORIO_RCON_HOST=charon python3 ...` (Tailscale RCON); gather wood by `clear_area`, craft via
  `A.craft` (script-craft, no player=), and build with `create_entity` WITHOUT `player=`.

## Relocation thrash: measure the on-patch density the SAME way as candidate patches (2026-06-29)

`ensure_ore_supply` relocated the iron outpost every 12th lap for 30+ min on a FALSE "thin" signal -
the drills were already on the richest patch (peak -75,17, 1055/tile) yet it kept reporting "patch
under drills thin (494/tile) ... a richer patch exists (1055/tile @ -75,17) -> relocating", then
build_mine_outpost's idempotency made each "rebuild" a no-op (existing belt within radius 22 -> it
returns the existing chest without building), so it never converged. Pure churn: log spam, a
`_sweep_iron_plates` + reaper-pause every cycle, a wasted maintain lap each time.

Root cause: APPLES-TO-ORANGES density measurement. `_ore_under_drills` summed each drill's single
actively-depleting `mining_target.amount` tile (reads low, ~494) while `richest_spot` sums ore over
a 5x5 neighbourhood and divides by 25 (~1055 on the SAME patch). So a freshly-relocated outpost ON
the best patch always read "thin + richer patch elsewhere". The 6 drills' true 5x5-average density
was 532/tile - just ABOVE the 500 thin_tile threshold - so it should never have triggered.

Fix: `_ore_under_drills` now measures the patch the SAME way `richest_spot` measures candidates -
ore summed over each drill's 5x5 footprint, averaged, /25 for per-tile - so on-patch vs best-patch
is apples-to-apples (same patch -> ratio ~1 -> no relocate). Verified live: now reads 532/tile (not
494); both gates (`thin` 532<500=False, `richer` 1055>=532*2=False) go False -> no relocation. A
GENUINE drought still fires: a sparse edge reads low (425/tile) vs a dense field (1071/tile), >2x.
RULE: any "is the patch we're on thin?" check must use the SAME metric as the candidate-patch check,
never the depleting single-tile `mining_target.amount`. (Latent follow-up: build_mine_outpost's
radius-22 idempotency makes an edge->dense-core relocation WITHIN one patch a no-op; not biting now.)

## Steam plant: Seth's SCALABLE design (verified from his hand-build 2026-06-29)

Fluid ratios (read from prototypes): boiler 1.8 MW = 60 water/s -> 60 steam/s; engine 900 kW =
30 steam/s; so 1 boiler : 2 engines. Offshore pump = 1200 water/s = 20 boilers = 40 engines.
Steam unit energy = (165-15)*200 = 30 kJ.

Layout = a repeating COLUMN (pitch 4 tiles in X) tapping two shared horizontal backbones, plus one
pump. All boilers dir0 (steam exits NORTH), engines chained north, character builds northward from
a water-south shore. Per-column entities (bx = boiler centre x; rows are FIXED relative to the
boiler row by = -18 in his build, i.e. offsets from by):
  - boiler        @ (bx, by)        d0          [A.place tile (bx-1.5, by-1)]
  - engine 1      @ (bx, by-3.5)    d0          [stacked north, 5 tall]
  - engine 2      @ (bx, by-8.5)    d0
  - burner-inserter @ (bx, by+1.5)  d8          picks coal off the belt (south), drops into boiler
  - water crossing: pipe-to-ground @ (bx+2, by+3.5) d8  +  (bx+2, by+1.5) d0, then pipe (bx+2, by+0.5)
                    -> ducks water UNDER the coal belt into the boiler's EAST input. The 4-tile
                       column pitch EXISTS so this crossing fits (Seth: intentional gap).
Shared backbones (extend by 4 tiles per added column):
  - WATER MANIFOLD: a pipe row at y = by+4.5 (boilers tap it via the crossing above).
  - COAL BELT:      a transport-belt row at y = by+2.5 (dir4, east), feeding every burner inserter;
                    coal enters from the WEST end (from the base coal supply).
Pump (1 per 20 columns / 40 engines): place in OPTIMAL CLEAR water space (not necessarily at the
manifold) and route a pipe from it into the manifold's intake. To scale past 40 engines, add ANOTHER
source pump and plumb it into the same manifold intake pipe (don't re-architect). The manifold is
the scalable backbone; columns and the two backbones just extend east.
`build_power_plant(n_engines)` replicates this: pump+route once, columns = ceil(n_engines/2)
stamped at bx0+4k, extend the manifold + coal belt, +1 pump per 20 columns. Verify with
get_fluid_count (pump 100, boiler 200/200) + engine energy > 0.
