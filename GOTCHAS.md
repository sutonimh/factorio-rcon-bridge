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
- **In-game notepad:** keep the task queue on-screen via `notepad()`/`now()` (rendering API),
  not just `game.print` (which scrolls away).
- **The notepad must use WORLD-SPACE rendering, NEVER a player GUI (2026-06-29).** `now()`/`notepad()`
  originally wrote to `storage.derpface.gui.screen`, but derpface is a PLAYER-LESS character (no
  `.gui` -> 'LuaEntity doesn't contain key gui') AND the autopilot runs 24/7 with NO connected
  player, so every `now()` call crashed silently (swallowed by the loop's except) and the on-screen
  note never appeared. FIX: `_render_notes()` draws a vertical panel via `rendering.draw_text` at a
  fixed world tile near the base (`NOTE_ANCHOR`), storing the LuaRenderObjects in
  `storage.autopilot_notes` so each update destroys exactly the prior panel (no leak; never
  `rendering.clear()`, which would wipe unrelated renders). Render objects persist in `storage`
  across saves + RCON calls. RULE: anything "on-screen" for the autopilot is world-space rendering,
  never a GUI - there is usually no player to own a GUI.

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

## A belt-fed mine must NOT keep build_mine_outpost's terminal chest+inserter (2026-06-29, Seth)
- `build_mine_outpost` ALWAYS ends the ore belt with `[belt][burner-inserter][wooden-chest]` - the
  inserter pulls ore OFF the belt into a terminal chest for CHARACTER HAULING. If that mine is then
  meant to BELT-FEED the base, leaving the chest+inserter in place is the bug Seth caught at the
  iron mine: the inserter drains the belt into the dead-end chest, so the belt segment continuing
  toward base stays EMPTY and no iron ever reaches the smelters. The drills look fine (belt full to
  the chest), but downstream is starved.
- FIX (what Seth did by hand): REMOVE the terminal inserter+chest and bridge the gap with a belt
  tile so the lane runs continuous mine -> base. `connect_mine_to_array(ore)` already does exactly
  this (refunds the burner-inserter/inserter/wooden-chest within radius 26 of STATE[ore], then
  `lay_belt_path` runs the belt through) - but it was NEVER applied to this mine: the relocation
  rebuilds (`build_mine_outpost`) keep re-placing the terminal chest, and the belt-connect step
  (`build_belt_supply`/`connect_mine_to_array`) never ran. So a relocated mine is left chest-capped.
- RULE: a mine is EITHER character-hauled (terminal chest, no belt to base) OR belt-fed (no terminal
  chest, continuous belt to base) - never both at the same belt end.
- GUARD (implemented): `build_mine_outpost` now bails at the top if the patch has ore belts but NO
  terminal wooden-chest (`nb>=4 and nc==0` within radius 30) - that signature == a human belt-fed
  the mine, so it returns a sentinel and leaves the through-belt intact instead of clean-slating +
  re-capping. A fresh patch (0 belts) and a normal char-haul outpost (has a chest) are unaffected.

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
- **`inv.remove{count=0}` THROWS "count must be positive" - guard EVERY remove whose count is an
  insert's return (2026-06-29, froze the whole base).** The servicers move items as
  `local g=dst.insert{...}; src.remove{count=g}`. When the destination is FULL, `insert` returns 0,
  so `remove{count=0}` errors and ABORTS THE ENTIRE `/sc` command. `trim_inventory`'s lab-feed loop
  hit this every lap (labs' input fills -> g=0 -> crash) BEFORE its `trim('copper-cable',200)` ran,
  so the cable clog (8400, over-produced by the green chain) was never trimmed -> derpface inventory
  hit free=0 -> `_collect_plates_all` could no longer pull plates from the furnaces (insert returns
  0) -> furnaces stuck `full_output` -> assemblers `item_ingredient_shortage` -> labs
  `missing_science_packs` -> research stalled. Power was fine the whole time; the symptom looked
  like a supply problem but was a 1-line crash. RULE: any `target.remove{count=X}` where X came from
  an `insert` MUST be `if X>0 then target.remove{...} end`. Fixed at all sites (trim_inventory,
  service_science, _collect_plates/_collect_plates_all, restock_coal, _sweep_iron_plates,
  harvest_array_plates). Diagnose a base-wide freeze by the entity STATUS histogram
  (`find_entities_filtered{type=...}` -> tally `e.status`): full_output + item_ingredient_shortage +
  missing_science_packs with power OK = a material-flow break, and check derpface `free` slots FIRST.
  (Follow-up: the green chain over-produces copper-cable; cap the cable assembler or it just gets
  deleted by trim every lap - wasteful but not fatal once trim runs.)

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

## Server container RECREATE breaks the autopilot's network namespace (2026-08-29)

The autopilot container runs with `--network container:factorio`, which docker resolves to a
CONTAINER ID at create time. A plain `docker restart factorio` keeps the ID (autopilot
survives), but a `docker compose up -d` that RECREATES the factorio container (image bump,
compose edit) gives it a new ID -> the autopilot's netns points at a dead container -> every
RCON connect = ConnectionRefused and the autopilot exits/flaps. FIX after any server
recreate: `docker rm -f factorio-autopilot` and re-run it with `--network container:factorio`
(re-resolves to the new ID). Hit during the 2.1.8 -> 2.1.17 bump (saves backed up first in
/mnt/user/appdata/factorio/saves-backup-pre2117). Eventual fix: put both containers on a
shared compose network and talk to `factorio:27015` by name instead of sharing netns.

## Factorio 2.1.17 bump notes (2026-08-29)

- Image pin: /mnt/user/appdata/factorio/docker-compose.yml -> factoriotools/factorio:2.1.17.
  DLC mods (space-age/elevated-rails/quality/recycler) ship inside the headless build; the
  mods dir only needs mod-list.json. Map 2.1.8-1 migrated cleanly (one-way: no going back).
- `game.active_mods` is GONE in 2.1 (LuaGameScript doesn't contain key active_mods) - use
  `script.active_mods` or prototypes if needed.
- Fluid-API audit: our code never touched `fluidbox` (blocked long ago); `get_fluid_count`
  verified working on 2.1.17. The removed `LuaFluidBox` class costs us nothing.
- Tech tree re-dumped via the new `dump_techtree.py` (repeatable; chunked storage read).
  2.1.8 -> 2.1.17 diff: transport-belt-capacity-2 gained prerequisite
  inserter-capacity-bonus-7; everything else identical (277 techs, 631 recipe mappings).

## Headless research TRIGGERS don't self-credit (2.1.17, cost the fresh-map run its research — 2026-08-29)

The engine only credits craft-item research triggers from PLAYER craft events. derpface
(player-less character) REALLY crafted a lab via `begin_crafting` (works fine on a character
entity: real ingredients, real crafting time, queue drains) — yet `automation-science-pack`
stayed locked, which deadlocked ALL research on the fresh map (nothing can queue while the
trigger tech is pending; the old map never hit this because Seth's hand-play had fired the
triggers long ago). Smelting-product triggers (steam-power=iron-plate, electronics=
copper-plate) DO fire headless; crafted-item ones do not.
RULE: `bootstrap.fire_craft_trigger(tech)` = verify the character genuinely performed the
trigger craft (begin_crafting + inventory delta), then set `technologies[t].researched=true`.
Trigger techs cost no science, materials + craft time were real -> faithful emulation, not a
cheat. `research_chain` calls it for every craft-item trigger in a chain; phase1_oil does the
same for `oil-processing` (mine-entity) once the pumpjack has DEMONSTRABLY produced crude.

## Blueprint stamping on 2.1.17: use import_stack + build_blueprint, not the new API (2026-08-29)

`surface.create_entities_from_blueprint_string` (2.1 API docs) returned NIL for a
known-valid string live (no ghosts, no error). The proven path works: temp inventory ->
`import_stack` (returns 0 on success) -> `st.build_blueprint{surface, force, position,
force_build=true}` -> entity-GHOSTS (no materials consumed; revive with build_ghosts*/bots).
`bplib._lua_stamp` implements it, with the storage._bp chunk pattern for >4KB strings.

## Vendored FLE placements must consume inventory (keep-it-legit)

Upstream FLE's connect_entities places from NOTHING. Our vendored lua/fle_lib.lua routes all
placements through `F.take(name)` (derpface inventory decrement, refund on failed create).
Any future vendored builder gets the same treatment before first use.

## Dead-end belt lanes from interrupted lays: verify continuity, self-heal (2026-08-29)

Three mine-output lanes on the v2 map ended mid-route (dead-end belts at -24,-65 / 18,17 /
26,8, all pointing east into empty tiles) -> ore piled behind the breaks and ALL 39 furnaces
read no_ingredients while 23/30 sampled belts carried items. Cause: lay_belt_path runs
interrupted (container restart mid-lay / out of belts) and NOTHING verified lane continuity
afterward. Fix: `repair_belt_gaps()` (science strand, every lap): finds dead-end belts, and
where the same lane resumes within 30 tiles in the belt's direction, bridges the span from
inventory (script-crafting belts from plates when short). No continuation = left alone (legit
terminus) for the architect to judge. RULE: any belt-laying routine must either verify its
lane end-to-end or rely on this repairer running.

## Lane law: verify SOURCE->DESTINATION by belt BFS; chests only where buffering is needed (Seth, 2026-08-29)

The copper lane looked laid but its two segments sat on ADJACENT ROWS - a misaligned junction
no same-row gap-bridging can fix; ore piled up and the arrays starved. Tile-local checks are
not enough: `_lane_connected(ore)` BFS-walks the mine's lane via belt_neighbours.outputs and
requires it to REACH the array intake; a broken lane is fully re-laid by
connect_mine_to_array (lay_belt_path corners join row offsets). Runs every 10th maintain lap
+ after phase-0 belt builds. RULE (Seth): raw ore belts DIRECTLY to smelters; plates belt
DIRECTLY onto the bus once it exists; a chest is only legitimate where something must buffer
(mine terminal for character-haul era, boiler coal buffer, array plate drain until the bus).

## AUTOMATION FIRST (Seth's standing directive, 2026-08-29)

The character must never do by hand what automation already provides: with drill outposts
live, ore comes from their chests/belts (ensure() pulls before it ever hand-mines) - derpface
hand-mining iron next to 20 working drills is a bug, not a bootstrap step. smelting_base's
direct A.mine top-ups are gone (ensure() everywhere). And the SELF-HEALS (keep_power,
fix_unpowered, repair_belt_gaps, ensure_lanes) + the operator inbox run at every planner
pass START, not just inside maintain bursts - a long build pass must never delay the
automatic systems that keep the base alive.

## Split-direction mine rows starve everything silently (2026-08-29)

The iron drop row was HALF west HALF east (x<=13 flowed to the lane; x>=14 carried ore east
to a dead end): drills fed both halves, the array got a trickle then nothing, and all lane
belts read EMPTY while drills sat waiting_for_space. A lane-connectivity BFS passes (the west
half IS connected) - flow DIRECTION was the break. `fix_mine_row_flow(ore)` (in ensure_lanes,
every pass start + 10th lap): find the drop row's EXIT belt (output leaves the row) and point
every row belt at it. Manual fix that session: reversed 16 belts by hand first.

## Bottleneck-first + heals-are-lessons (Seth's directive, 2026-08-29)

When triage says stall/anomaly, derpface STOPS (A.stop()) and the full heal battery runs
immediately - fixing the base always outranks whatever routine step was in progress. Every
heal that actually fixed something writes a lesson (lessons.jsonl), so recurring defects
surface for promotion into real code fixes instead of being silently re-repaired forever.
Also learned today: a single item-on-ground blocked a belt-gap bridge for an hour - the
repairer's obstruction handling must COLLECT items/trees/rocks and destroy stray ghosts,
never treat them as hard blockers; and the two smelt zones share ox, so per-ore lane columns
(ox-2, ox-4, ...) are mandatory or connect_mine_to_array merges the ore lanes.

## THE SWEEP (2026-08-29): control inversion — the maintain loop is retired

Seth's audit call ("problems prioritized, self-learning working, no legacy loop") + a
two-agent code/AI audit found the loop was detect -> log -> blind heal -> log: 42 LLM
verdicts + 4 correct architect diagnoses in 75 min produced ZERO fixes; architect
prioritized_actions were parsed by nothing; lesson dedup never accumulated (LLM prose never
repeats -> every lesson count=1 -> promotion unreachable); builder crashes outside try
caused 18 restarts/108 min, resetting every cooldown; and triage's schema couldn't express
the failures it watched (class=null 6/6 in replay, a fatal coal-death classed 'watch').

The fix is structural (controller.py): SENSE -> DETECT (rule battery owns known
signatures) -> PRIORITIZE (sev 0-2) -> FIX (one actuator) -> VERIFY (re-sense; repeat
failures escalate) -> LEARN (structured-key lessons that actually count up). The builder
thread only builds; sev<=1 issues preempt walks mid-leg. Architect reports now END in a
validated command queue (same catalog as operator prompts) - findings EXECUTE. Triage 4B is
demoted to residual anomalies with trend + actuator routing + verdict dedup. A 15-min
zero-flow progress watchdog is severity 0 - the detector the silent July-August dead month
needed. Controller state (fail counters, architect cooldown) persists across restarts.
RULES: no timer-driven blind healing; every finding must terminate in an actuator or a
rejection lesson; every fixer is verified by re-sensing, never by its own claim.

## Legacy-audit fixes round 2 (2026-08-29)

- gate0 was STRUCTURALLY UNSATISFIABLE: labs_working>=2 required, red_science built exactly
  one lab -> phase 0 looped forever, re-running every step (the root multiplier on all the
  obsolete work). red_science now builds two labs.
- fuel() was the literal "hand-mining beside working drills": unconditional walk+mine at the
  coal patch. Now ensure() first (chests -> belt-lift -> mine only as true last resort);
  ensure() itself gained a lane-belt LIFT fallback for belt-fed mines (no terminal chest).
- keep_power probed the RETIRED map's boiler buffer chest at (45,-2) -> silent no-op on v2
  (contributed to today's boiler=0). Now boiler-adjacent lookup.
- fix_unpowered searched radius 120 around DERPFACE - a runaway character made it no-op for
  an hour while triage screamed. Now scoped to the base (spawn radius 250).
- build_belt_supply re-ran connect_mine_to_array (destroys mine-head inserters/chests +
  re-lays the whole route) EVERY pass; now only when the lane BFS says broken. ensure_lanes
  got convergence accounting: 3 failed re-lays -> stop churning, write a lesson, escalate.
- rcon.run: bounded retry on refused/reset (safe: nothing executed) - one socket blip was
  process-fatal (18 crash-restarts today, each spawning a runaway walk).
- Controller triage moved to a worker thread (a 60s LLM call was blocking the actuator strand).
STILL OPEN (next batch): registry-first servicing for arrays/power/science, executor-orders
as the one queue, v1 dead-weight quarantine (autopilot.maintain servicers, patrol.py,
build_belt family, snapshot/rebuild), lay_belt_path inventory consumption, walk() callers
checking the success flag, orders stuck 'running' startup sweep.

## Native-pathfinder travel stack (2026-08-29): long walks go through surface.request_path

Long-distance walking (>40 tiles) is now the FLE-style travel stack (`travel.goto_far`,
wired into `walk()` as `walk_far`; lua in the fle_lib `travel*` chunks): corridor chunk
pre-generation -> async `surface.request_path` with the character's own collision
box/mask -> a server-side `on_nth_tick(2)` walker consumes the waypoints. Lessons:
- **`script.on_event`/`on_nth_tick` registration from /sc WORKS on 2.1.17** (verified
  live: a /sc-registered `on_script_path_request_finished` handler received a real path).
  The old "pure /sc has no event registration" belief was WRONG. Handlers die with the
  Lua state (save reload/restart) exactly like the `fle` global; the version-probe
  re-push re-registers them (`travel.ensure_handlers`).
- **Walker cadence must keep one step BELOW the arrival radius.** Step = speed x cadence
  (~0.15 x 5 = 0.75 tiles at FLE's nth_tick(5)) vs our 0.35-tile arrival: the character
  stepped ACROSS the circle and bounced over one waypoint for 200s. Cadence 2 (~0.3
  tiles/step) fixed it. Corollary: a stuck watchdog must measure BEST-DISTANCE-TO-TARGET
  improvement, never raw displacement - an oscillating character moves plenty.
- **`A.stop()` must clear `storage.fle_travel`**, or the server-side walker re-sets
  walking_state every 2 ticks and the stop doesn't stick (one-controller rule). Upside:
  a killed Python process can no longer cause a runaway walk - the walker stops itself
  at the last waypoint.
- **Ungenerated chunks silently block the pathfinder** (`not_found`): always pre-generate
  the corridor (every 32 tiles, radius 2 chunks + `force_generate_chunk_requests`) before
  requesting. Unreachable goals get the displaced-goal retry (8 tiles, rotated 90 deg per
  attempt, 5 goals).
- **Live-testing the character requires pausing the autopilot container** (`sudo docker
  stop factorio-autopilot`, restart after): its builder calls `A.stop()` before walks,
  which now also clears any in-flight travel queue - two controllers WILL fight. Also
  learned: another session deploy.sh'd this worktree's UNCOMMITTED code mid-test
  (shared-worktree hazard) - re-check what's actually deployed before blaming the code.

## OPERATOR TRUCE (Seth, 2026-08-30): layout self-heals suspend while a player is online

Seth hand-cleaned the belt mess and the self-heals kept UN-DOING his deletions: a deleted
belt is a "dead end" to repair_belt_gaps (re-bridged), a cleared route is "disconnected" to
ensure_lanes (re-laid), and build_belt_supply re-placed array belts every pass. Root rule:
the bot cannot distinguish "broken" from "being edited" - so while game.connected_players>0
(cached 10s), EVERY layout-modifying routine no-ops: repair_belt_gaps, ensure_lanes +
fix_mine_row_flow, fix_unpowered, ensure_grid_connected, build_belt_supply, coal_to_boiler.
Fuel/feed/research servicing continues. Heals resume when the operator disconnects.
Companion fixes same session: world notepad rendering retired (dashboard replaces it);
coal_to_boiler() splitter tap + boiler burner-inserter (self-sustaining power);
electrify_mines() burner->electric drill swap-in-place gated on the now-prioritized
electric-mining-drill research; research queue set via f.research_queue write.

## Operator deletions are INTENT, not damage: the protected-tile registry (2026-08-30)

The truce stopped the bot fighting Seth WHILE he was online, but the moment he logged off the
heals re-laid everything he had deliberately removed ("the belts I deleted seem to have
returned") - a deleted belt is indistinguishable from a broken lane to repair_belt_gaps /
ensure_lanes. FIX: the controller snapshots every belt tile when the operator connects; on
logoff it diffs and files every REMOVED tile into protected-tiles.json (persistent). Both
lay_belt_path and repair_belt_gaps skip protected tiles forever. RULE: any future auto-build
that places tiles must consult _protected_load() first.

## ROOT CAUSE of duplicate lanes: re-lay never tore down its predecessor (2026-08-30)

Seth: "those belts shouldn't be being placed in the first place, they're useless." Every
connect_mine_to_array call laid a FRESH route and left the previous one standing, so each
re-lay (architect command, ensure_lanes repair, phase pass) added another parallel lane -
"two belts coming from both the iron and copper patches". The protected-tile registry stops
resurrection of what the operator deletes; THIS is the creation-side fix: lay_belt_path now
returns the tiles it laid, connect_mine_to_array (and the coal lane) register them in
lanes.json, and teardown_lane(ore, keep=new_tiles) refund-removes the superseded lane in the
SAME pass. Registry-scoped, so it only ever removes belts the bot itself recorded.

## Learn from the OPERATOR's edits (Seth, 2026-08-30) + container timezone

The operator only touches what the bot got wrong, so his session is the strongest teaching
signal available. The controller now snapshots every player entity at login; at logoff it
diffs (removed / added / rotated, with example coordinates) and hands the summary to the
local 35B: "an expert changed the bot's base - infer WHY", which returns up to 3 durable
{condition, mistake, rule} lessons stored under operator:* keys and injected into future
triage/architect prompts. Verified live: a simulated stray-belt deletion produced the rule
"only place belts that directly connect a producer to a consumer... every segment has a
valid input and output". Pairs with the protected-tile registry (never rebuild what he
removed) and the lane supersede teardown (never create the duplicates in the first place).
CONTAINERS: both factorio-autopilot and factorio-dash must run with `-e TZ=America/Los_Angeles`
(python:3.12-slim ships zoneinfo, so this is all that's needed) or the log is UTC. Re-apply
on every `docker run` recreate - and remember the netns rule: `--network container:factorio`.

## NEVER register runtime event handlers: it locks human players out (2026-08-30)

The FLE-style travel stack registered `script.on_event`/`script.on_nth_tick` from `/sc`. It
works mechanically - but it mutates the LEVEL's event-handler set, and Factorio then REFUSES
every joining client: "Cannot join. The following mod event handlers are not identical
between you and the server ... level". Seth was locked out of his own game mid-session.
RULE: on a server a human joins, the autopilot may NEVER register runtime handlers. All
periodic behavior is driven from PYTHON (poll + act), which is what autopilot.walk() already
does. Recovery if it happens: `/sc script.on_nth_tick(nil); script.on_event(defines.events.
on_script_path_request_finished, nil)` then rejoin. The travel* lua chunks, travel.py and the
walk_far handoff were removed entirely; the salvageable idea (corridor chunk pre-generation
before long walks) can be added inside walk() without handlers.
Also fixed this pass: fix_mine_row_flow re-pointed belts by ROW Y within radius 42, so the
IRON row (y=-42) grabbed the COPPER column's crossing tile at (-10,-42) and flipped it east
every cycle - the invisible hand that kept breaking the copper lane all evening. It now only
touches belts within the mine's own DRILL X-SPAN (+/-6).

## ORE PATCHES ARE FOR MINING ONLY - now enforced in code (Seth, 2026-08-30)

"Never build anything except mining drills and supporting infrastructure on top of ore
patches." This was a documented BUILD CONVENTION that nothing enforced, so furnaces/
assemblers kept landing on ore. Now `autopilot.place()` refuses server-side: any entity NOT
in `autopilot.ORE_OK` (drills, pumpjacks, belts/undergrounds/splitters, inserters, poles/
substations, pipes, chests, turrets/walls/radar) whose FOOTPRINT touches a resource tile
returns `ON_ORE ... - ore patches are for mining only` and places nothing. `build_io_cell`
also refuses an ore site up front.

## The truce must pause the BUILDER too, not just the heals (2026-08-30)

Gating only the self-heals left the phase PROGRAM building while Seth played ("you are
rebuilding shit I've deleted while I'm logged in"). Now planner.play() skips the entire
program pass AND the operator BUILD_QUEUE while `game.connected_players>0`; the controller
keeps servicing fuel/feed/research (no construction). Zero construction while a human is
connected - full stop.

## THE BUILD LAWS (Seth, 2026-08-30 — after I rebuilt his deletions twice)

These are absolute. Violating them is what "shitting up the map" means.

1. **NEVER build anything that doesn't do something.** Every build is VERIFIED after the fact
   against a functional check - not "did create_entity return ok", but "does ore actually
   move / does the machine actually reach a live state". `build_worked(check)` polls it.
2. **If the result is nothing, REMOVE WHAT YOU BUILT, immediately, in the same pass.**
   connect_mine_to_array tears its own lane out when no ore flows; build_io_cell removes the
   whole cell when the assembler never goes live. No dead infrastructure is ever left standing.
3. **OPERATOR DELETIONS ARE FINAL.** If the bot built a tile, the tile is now empty, and the
   bot didn't remove it, a human removed it: `reconcile_removals()` (every 4th controller lap,
   independent of login/logoff edges and restarts) protects it forever. A planned route that
   is >=25% protected is OPERATOR-OWNED: the bot logs it and never lays it again.
4. **Guards belong at the PLACEMENT layer, not the control layer.** Every earlier version of
   this protection lived in the controller/planner and was bypassed by a restart, a manual
   call, or an architect command - which is exactly how his deletions came back twice. The
   ledger + reconcile + protected-tile checks now sit inside lay_belt_path/place().
5. **Ore patches are for mining only** (enforced in place(), see above).
6. **Zero construction while a human is connected** (builder AND heals, see the truce).

## Swapping entity TIERS moves the drop tile - verify it, or the mine dies silently (2026-08-30)

electrify_mines swapped burner (2x2) drills for electric (3x3) at the same position. The
larger footprint MOVES drop_position, so six copper drills dumped ore onto bare ground
("waiting_for_space_in_destination -> item-on-ground") and copper supply died while every
status read looked plausible. Now the swap checks the new drop_position lands on a
belt/underground/container and undoes itself if not, and separately verifies power (an
unpowered electric drill mines nothing) and reverts to a fuelled burner drill if it cannot be
powered. GENERAL RULE: any tier swap must re-verify the ENTIRE interface it participates in -
footprint, drop/pickup tiles, power, and fuel - not just that create_entity succeeded.

## PLAN, then place - and ADJUST rather than revert (Seth, 2026-08-30)

"Why are you reverting instead of adjusting the belts? Check space requirements and outputs
before placing anything; a plan should be in place to ensure routing before you place things."
`plan_mine_geometry(ore)` is that plan for a mine: it derives the lane row and span FROM THE
DRILLS, clears the lane row of anything that isn't lane (relocating my own poles instead of
deleting them, collecting spilled items), fills missing lane tiles, then makes every drill's
drop_position land on the lane by ROTATING it, and only nudging its position if rotation
can't work. Reverting an upgrade is the last resort, never the first move. electrify_mines
now calls this after a swap, so a footprint change repairs the routing instead of undoing the
upgrade. Two self-inflicted wounds this taught: my pole line was placed ON the mine's belt
row (blocking it), and a burner<->electric revert shifted drills onto the lane itself.

## Pre-2.0 blueprints are MIGRATABLE, not garbage; and script-placed poles don't auto-wire (2026-08-30)

Two import lessons from Seth's lab-array print:
1. bplib.verify_2x REFUSED every pre-2.0 blueprint outright ("game version 0.16, not 2.x").
   Wrong: 2.0 only doubled the direction space (8-way -> 16-way) and renamed a few entities
   (stack-inserter -> bulk-inserter, filter-inserter -> inserter, logistic-chest-* ->
   *-chest). `migrate_pre2()` does exactly that and the print imports fine. ONLY RAILS are
   genuinely dead (2.0 rail geometry, no converter), so a rail-bearing pre-2.0 print is still
   refused - with a message that says why. Separately, `tier_downgrade(bp, researched)` swaps
   un-researched tiers for buildable ones (fast-inserter -> inserter, fast belts -> belts) so
   a print can be built NOW and upgraded in place later.
2. create_entity-placed electric poles do NOT reliably auto-connect: two small poles 4.0 tiles
   apart (wire reach 7.5) sat on different electric_network_ids until wired explicitly via
   `p.get_wire_connector(defines.wire_connector_id.pole_copper, true).connect_to(other, false)`.
   This is almost certainly behind several "islanded grid" mysteries tonight. RULE: after any
   scripted pole placement, wire every pair within reach explicitly, then verify by comparing
   electric_network_id - never assume placement implies connection.

## Lab array build (2026-08-30): reserve the full footprint, build a fraction

Seth's 36-lab print was stamped whole as GHOSTS at (10,40) - 13 tiles of clear padding south
of the copper array - but only 9 labs were revived (the 3 westmost in the 3 rows nearest the
feed). The remaining 110 ghosts ARE the reservation: they hold the footprint so nothing else
gets built there, and expansion is just reviving them as materials allow. Poles follow the
print's own 4-tile lattice (x -6,-2,2,6,10..., y 30,34,38...) with ONE straight trunk column
at x=-6 spaced 7 (wire reach 7.5) back to the base grid - not the opportunistic pole chains
the bot had been laying, which Seth called out as a hack.

## THE OPERATOR'S DESIGN PRINCIPLES (learned from his 2026-08-29 hand-optimization)

Seth rebuilt the bot's base by hand: "the power poles have been laid out optimally, all
coal powered drills have been replaced by electric, all unneeded belts have been removed."
Measured (`snapshot_map.py diff before after`): entities 713 -> 619, poles 107 -> 69 (102
removed / 64 added = RELAID, only 5 survived), belts 462 -> 421, all 11 burner drills ->
16 electric, power plant rebuilt elsewhere, 2 labs + 1 assembler deleted, 9 wooden chests
+ 9 inserters deleted, 16 belts rotated. Production 0/0/0 -> iron 174, copper 90, coal 120.
He did not clean up the base; he DELETED THE BOT'S SYSTEM AND REBUILT IT AS A DIFFERENT ONE.

**The one line to encode: the bot's success criterion is that a placement SUCCEEDED; the
operator's is that an ITEM ARRIVED.** Full spec with every offset: `OPERATOR-PRINCIPLES.md`.
Machine-checkable form: `principles.py` (READ-ONLY RCON) + `test_principles.py` (44 tests).
The cheapest complete gate is two checks - P1 flow coverage + P2 single network - run after
every builder.

- **P1. FLOW is the only success metric; entity count is not progress.** Flood-fill the belt
  graph forward from producer drop tiles and backward from consumer pickup tiles: **bot
  189/470 belts (40%) on a producer->consumer path, operator 408/431 (95%)**. 229 of the
  bot's belts had no producer upstream, 80 no consumer downstream, and all 28 stone furnaces
  had recipe `None` - they had never received an item. 713 entities produced 0/0/0; 619
  produced all three. Measure at the CONSUMER after every builder; any sub-build with zero
  flow is torn down in the same pass. `connect_mine_to_array` (L805) already does this
  (`lane_moves_ore` -> `teardown_lane`) and is the ONLY builder that does -
  `build_smelter_array`, `build_belt_supply`, `coal_to_boiler`, `setup_science_io` all
  return success on placement.
- **P2. ONE electric network, energized, before any electric entity exists.** The bot had
  TWO: net 1 (105 poles, 2 engines) and **net 405 - 6 electric drills + 2 poles with NO
  generator**. The gap from the main grid to that island was **8.06 tiles - 0.56 past the
  7.5 wire reach** - after a 19-pole chain had been laid ~52 tiles toward it. Coal 0/min.
  Operator: 69/69 poles on net 535, zero orphans, zero `no_power`. After placing ANY electric
  entity, read `electric_network_id` and compare to the root's; never "get close" to a
  network. `fle_tools.connect` (L1425) + the self-heal at L1710 INTERPOLATE pole positions
  (`steps=ceil(sqrt(bd)/6)`) and hope the spacing lands under reach.
- **Cap pole degree at 4 of the 5 copper slots.** A saturated pole cannot adopt a later
  neighbour - that is exactly how the bot stranded its lab block on its own network with two
  poles 4.0 tiles apart and no free slot to bridge. Live probe 2026-08-29 after the bot
  resumed: degrees `1:6 2:29 3:25 4:23 5:10 6:2` - 12 poles at/past the cap.
- **P3. Choose the GEOMETRY first, derive every entity from it** - never survey what got
  built and rationalise a layout around it. Three exact templates, repeated verbatim:
  MINE (9 rows) `lane-4 POLE / lane-2 drills dir S / lane BELT / lane+2 drills dir N /
  lane+4 POLE` - drill radius 2.49 > the 2.0 offset so the belt row AND both pole rows are
  still inside the mined band (zero waste). Verified at copper (lane -63.5) and coal (15.5).
  SMELTER stack, 9-tile pitch in y (iron oy=3, copper oy=12): `belt / inserters+poles /
  furnaces / inserters+poles / belt` at oy+0,+1,+2..3,+4,+5. PLANT, 2 free params (spine
  column Sx, boiler row By): boilers `(Sx±2, By)`, engines `(Sx±2, By-3.5-5k)`, pipe
  `(Sx, By+.5)`, pump `(Sx, By+5.5)`, pole `(Sx, By-5.5)` - and **By is DERIVED FROM THE
  SHORELINE**. `plan_mine_geometry` (L1776) does the opposite: `lane_y =
  Counter(d["dy"]).most_common(1)` reads the lane off wherever the drills happened to drop,
  ratifying a bad layout instead of imposing a good one.
- **P4. Service infrastructure rides INSIDE the machine rows and doubles as the mesh.**
  Smelter poles sit ON the inserter rows at x-pitch 4, phase `x = 1.5 (mod 4)`; inserters are
  at `x = 0.5 (mod 2)` and 2x2 furnaces at `x = 1.0 (mod 2)`, so **the pole takes the free
  half-tile of every other furnace column and costs ZERO machine slots**. Pole rows within a
  stack are 3.0 apart, between stacks 6.0 - both under 7.5, so **the service poles ARE the
  network; zero distribution poles are needed** (smelter block 58 -> 34 poles, 17 pole rows
  -> 4). Same idea at the plant: ONE pole at `(-31.5,40.5)`, the centroid of the 2x2 engine
  array, powers ALL FOUR engines (the bot used 3 scattered poles for 2). Measured
  pole->consumer incidences per consumer **2.171 -> 1.014**; consumers per pole 0.71 -> 1.07;
  **40 of the bot's 107 poles (37%) powered nothing at all** and 34% were fully redundant -
  and the network was STILL broken. NEVER add a dedicated pole row, a pole spine, or a pole
  "beside" a module: `build_smelter_array` L633-634 straddles the block with two pitch-3 pole
  rows plus a vertical spine; those 4 deleted rows are 41 of the 102 removed poles.
  SCORING TRAP: coverage is owed to ELECTRIC CONSUMERS, not "machines" - a stone furnace is a
  burner and needs no pole, an inserter does. Score against `electric_network_id`-bearing
  entities or the metric inverts.
- **P5. Every pitch is DERIVED from a prototype constant, and nothing is placed without
  `can_place_entity`.** `create_entity` does NO collision check, so wrong geometry SUCCEEDS
  SILENTLY. Pitches: drills 3 (`= tile_width`; can_place at 3 true, at 2 FALSE), poles in a
  machine row 4 (supply 2.5 -> 5x5), trunk poles 7.0 (93% of wire 7.5), boiler columns 4
  (3-wide boiler + a 1-tile shared spine), engine stack 5, smelter stack 9. Min pole
  separation 3.0 (bot had 8 pairs at 1.0, 4 at 1.41, 42 at 2.0). Three burials, all from bare
  `create_entity`: (1) `build_mine_outpost` L941 steps `dx = rx-n+2*k`, a pitch hardcoded for
  the 2x2 burner drill and inherited by `electrify_mines` L2282 - the 6 iron drills at
  x=14.5..24.5 step 2 are 3x3 and **share a whole tile column**, still live today; (2)
  `coal_to_boiler` L2231 does `bx = floor(boiler.position.x)` and puts the fuel inserter
  INSIDE the boiler's own footprint, treating a 3-wide entity's centre as its left edge; (3)
  a belt row at `lane±1` lands inside the 3x3 drill footprint - **13 belts are buried at
  y=-41.5, x=13.5..25.5, permanently unclickable**. The operator deleted every belt on that
  row he could REACH and left precisely the buried ones. Add a cleanup pass for belts under a
  drill footprint; a human cannot remove them.
- **P6. SHARE every line from both sides - separation is per LANE, not per belt.** Two drill
  rows feed ONE lane: ore per tile of row length is 0.125/s burner@2, 0.167 electric@3
  single-sided, **0.333 electric@3 double-sided (+167%)**. Measured lane contents prove it -
  copper `L1=4 L2=4`, coal `L1=4 L2=4`, **iron (single-sided) `L1=0 L2=4` for all 115 tiles**.
  Ore AND fuel ride one belt: stone furnaces are burners, so two opposing side-loads at one
  merge tile give `{L1: iron-ore, L2: coal}` at `(-7.5,8.5)`, mirrored for copper at
  `(-7.5,17.5)`; the ratio being wildly off (a furnace wants ~13.9 ore per coal, the splitters
  deliver ~1:1) is HARMLESS because the lanes back-pressure independently. One pipe feeds two
  boilers (their water inputs are the SAME tile `(-32,46)`). The pitch-4 boiler columns leave
  free column x=-32 carrying pole + pipe + risers + pump, so **both long faces of every boiler
  stay free** (north engine, south fuel inserter, By+2 coal belt). `build_belt_supply` does
  the opposite - a separate belt COLUMN per commodity - because the bot learned "don't mix
  ores" and over-generalised to "don't share a belt"; it also had NO fuel path to the copper
  array in the design at all.
- **P7. DIRECTION IS COMPUTED FROM THE DESTINATION** - never a constant, never "keep the prior
  heading". **13 of the 16 rotations in the diff are one row**: `y=-40.5, x=13.5..25.5, E->W`
  - the only row the iron drills drop on, pointing EAST AWAY FROM THE BASE into a dead end,
  every drill reading `waiting_for_space_in_destination`. Thirteen right-clicks took iron from
  **0 to 180 ore/min** (the theoretical max for 6 drills). A merging lane's FINAL TILE POINTS
  INTO THE TARGET BELT, not along its own travel: `(-7.5,9.5) E->N` and `(-9.5,17.5) E->S`.
  The copper dogleg is deliberate lane engineering - to land on lane 2 the trunk runs 2 past
  the feed row, 2 east, then back NORTH to side-load L2 only, leaving L1 free for coal (cost 7
  belts). `build_smelter_array` hardcodes `direction=4` and the bias leaked into the mine
  collector; `lay_belt_path` L645 ends with `tiles[-1][2]` = "last tile keeps prior direction".
  Add a `merge_into=(x,y)` arg to `lay_belt_path` and set the final direction from it.
- **P8. Trunks are straight, dedicated, parallel, and never cross; turns are a BUDGET.**
  104-tile copper trunk = 1 direction change, 82-tile iron trunk = 2, all four smelter rows =
  0; 19 turns across 429 belts. Power trunk `x=-14.5`: **14 poles, 91 tiles, gaps
  7,7,7,7,7,7,7,7,7,7,7,7,7**; second column `x=-35.5`, gaps 7,7,7,7; both phase
  `y = 5.5 (mod 7)`. It PARALLELS the belt corridor at fixed clearance (5.0 tiles west in open
  ground = room to widen the corridor to 4 lanes without moving a pole; exactly 1 tile east
  where space is tight) and terminates on module anchor poles with a SHORTENED final hop
  (6.0, 4.12) - pitch is nominal, endpoints are hard. The invariant is ONE-SIDED: a shorter
  hop still wires, so only EXCEEDING the pitch is a violation. Perpendicular pole-to-lane
  offset is never 0 in `after`; the bot had 5 poles at offset 0, including one at `(-9.5,20.5)`
  inside the main N-S belt column's own line. The bot's trunk is a staircase of one-off drops
  at 2.72 tiles/hop that never arrives, produced by `fle_tools.connect` + the reactive
  self-heal at L1701-1711 (on `no_power`, drop a pole and interpolate toward the nearest
  network), which also produced 39 zero-coverage scatter poles.
  **POLES MUST COME FROM A MODULE TEMPLATE, NEVER FROM AN ERROR HANDLER.**
- **P9. CROSS UNDERNEATH; resolve an obstacle by replanning the corridor BEFORE it, never by
  giving up AT it.** 3 belt crossings, 3 underground pairs, all exactly **span 2**. Fluid does
  the same: `pipe-to-ground (-31.5,49.5)->(-31.5,47.5)` ducks the water riser under the coal
  belt - he ducked the PIPE, not the belt, because the coal lane must stay unbroken so
  back-pressure fills it to both boiler inserters on either side of the crossing. The sharper
  lesson is the corridor move: the coal run does NOT descend at `x=-33.5` (inside the steam
  engines' bounding boxes) but at **`x=-36.5`, the first clear column west of the machine
  block**, leaving `x=-35.5` for the pole trunk. `lay_belt_path` L645 instead does
  `gaps = gaps+1` and continues, **leaving the already-laid belts in place** - the 11-tile
  engine block beat it, so it left a **19-belt stub down `x=-33.5` ending one tile short of
  the engine's edge**, 19 belts pointing at a wall 12 tiles from the boiler. Its underground
  branch wraps both `create_entity` calls in bare `pcall` with no partner verification: 3
  unpaired N-facing inputs stacked at `(-11.5, 10.5/11.5/12.5)` and 2 E inputs sharing 1 exit
  = a sealed dead end where coal arrived and stopped. ON A MISSING PARTNER, DESTROY THE
  ENTRANCE. `coal_to_boiler` still hardcodes the x=-33.5 route - **if it runs again it
  rebuilds the same dead stub**.
- **P10. Splitters ALLOCATE, belts BUFFER, chests only TERMINATE.** A 2-level binary coal tree,
  both splitters pure 1-in/2-out with NO priority and NO filter: A `(-28.5,16.0)` mine coal ->
  power / smelters, B `(-12.0,13.5)` -> iron feed / copper feed. The power spur is 50 tiles
  ending in a DEAD END and holds **392 of a max 400 coal = 7.3 min of full-plant autonomy at
  zero mining**; while it is full, back-pressure sends ~100% east to the base, and the instant
  the boilers eat, the spur is the only side with space. **Absolute priority up to actual
  consumption, with no circuit network.** (Placement trick: a splitter is 2 tiles wide, so a
  half-tile offset MANUFACTURES a second output row out of one belt row.) He removed 9 wooden
  chests + 9 inserters - **4 complete `build_io_cell` shells whose assembler was never built**
  - and kept exactly 2 iron chests, both at the far end of a plate belt with nothing
  downstream. `coal_buffer()` + `refill_buffers(0.2)` (L1072-1135) make the CHARACTER WALK to
  the plant to hand-load a wooden chest below 20% and it is the top `_gated()` priority in
  `maintain()` - the single largest source of maintenance walking. A chest is a hard stop
  where throughput becomes a human walking; `build_mine_outpost` and
  `mine_layout.plan_outpost` still default to terminating a lane with `inserter + wooden-chest`.
  Cheap runtime detector for an unbuilt machine between two shells: alternating
  `waiting_for_space_in_destination` / `waiting_for_source_items` around a gap.
- **P11. Ratios are EXACT; scale by DUPLICATING the module, never by lengthening it.**
  **2 boilers : 4 engines = 3.6 MW : 3.6 MW**, replicated identically in both columns, no
  orphan engine and no starved boiler; growth is `Sx' = Sx + 4`, a third column with the same
  table. He even PRE-LAID the header for it: 4 water-full pipes east along `(-30.5..-27.5,
  50.5)`, dead-ending one spine-pitch short of where a third riser would go. Refuse to place
  the orphan (`assert engines == 2*boilers`). `_build_boiler_engine(n_engines=k)` (L267) stacks
  engines on ONE boiler at 5-tile pitch - at k>2 a 1.8 MW boiler cannot feed 3 engines and the
  column walks further from its water with every addition.
- **P12. Site the plant AT THE FUEL, not at the base - and size it to the FUEL SUPPLY, not
  today's load.** Electricity travels for the price of a pole every 7 tiles; coal must be
  physically BELTED. Nearest-water distance: smelter array 50.3, coal patch 34.0, **plant
  centre 8.2**. From the plant: coal ore 18.5, splitter tap 25.2, iron mine 104.6 - **he
  accepted a 104-tile electrical run to buy a 25-tile coal belt.** Sizing is a FUEL BUDGET:
  plant at full 3.6 MW = 54 coal/min + 28 furnaces x 1.35 = 38 -> 92/min against 120/min
  measured supply (77%); a third boiler pair would put the system into fuel deficit even
  though utilisation is only 37%. Also: **never route a service spur through an ore patch** -
  the fuel spur detours 1.7x around it, hugging `y=22.5` three tiles clear of the south drill
  band, and taps at the far downstream end of the bus past the last drill where the belt is
  fully loaded; a belt on row 16 would have permanently blocked every future south-row drill.
  `power()` (L211-221) takes the FIRST tile from `find_tiles_filtered{radius=14}` with land to
  the north - no relation to the coal lane, the boiler grid or the base - and landed the pump
  on exactly the tiles the coal lane now needs, forcing the pipe run along the boiler's south
  face, the only face left for a fuel inserter. That is the root cause of the buried inserter
  in P5.
- **P13. Build order is SUPPLY -> CONSUMER, verified stage by stage; a consumer built early has
  NEGATIVE value.** Order: power -> mine -> trunk -> smelter -> buffer -> consumer, each stage
  measured moving material before the next starts. His finished base has **0 labs, 0
  assemblers** and stops deliberately at "buffer". He deleted 2 labs and 1 assembler; **lab 1
  at `(-29.5,41.5)` sat EXACTLY where the new steam engine now sits** - a measured spatial
  conflict, not an inference - and both labs read `missing_science_packs` with no feed chest
  anywhere near them. **Power capacity beat idle science.** The bot has NOT learned this:
  live probe 2026-08-29 after the snapshot shows **9 labs with `research = NONE`**, 21
  entities `waiting_for_target_to_be_built`, 35 `waiting_for_source_items`, a feed-belt column
  connected to no plate source, and 109 ghosts. Gate a consumer on `production_stats[input] >
  0 AND a fed inserter within reach AND a research/recipe queued`; treat a rising count of
  `waiting_for_*` / `missing_science_packs` as a BUILD-ORDER FAILURE, not progress.
  `build_io_cell` must be ATOMIC: chest, inserter, MACHINE, inserter, chest - or nothing.
- **P14. Measure the BINDING constraint end to end - including the DRAIN - and tolerate ~5%
  residue, not 0%.** The whole factory has converged on ONE number, **56/min per array =
  exactly one yellow inserter** (0.83 items/s). The chain: drain (1 inserter -> 1 chest)
  ~56/min **<- binding**; mine 180; furnaces 300; belt lane 450. **He did not fix this** - it
  is the bot's original drain design, kept unexamined, and it is now the base's only real
  bottleneck (13 of 16 iron furnaces at `full_output`). 14 of 16 drills reading
  `waiting_for_space_in_destination` is not a fault: it is the DEFINITION of a correctly
  saturated network. **TRANSIENT GUARD: the `iron 174 / copper 90` in the after-snapshot were
  fill-up while the output belts were still absorbing plates** - a live probe later the same
  session read iron 0/min, copper 53, coal 7. Sample over >=2 windows and assert production is
  not still RISING before reporting it. Residue he left: ~21 dead belts (5%) - 13 buried under
  the bot's overlapping drills, 6 one-tile lead-ins, 2 orphan coal tiles. Even a careful human
  pass leaves 1-5% junk; the budget is a THRESHOLD, not zero.

**Why his base works and the bot's didn't.** The bot's loop is `place -> check status ->
patch`: locally sound at every step, globally divergent. It yields coverage without
connection (171 pole incidences for 78 machines, and still TWO networks), volume without a
path (470 belts, 40% connected), structure without function (4 I/O cells with no assembler,
production 0/0/0), and constants instead of geometry. The operator's loop is `choose geometry
-> derive placements -> assert the invariant`. **He never patched** - of 107 poles he kept 5,
because a pole layout is not a set of independent decisions to repair one at a time, it is ONE
OBJECT, and the right move on a broken one is to replace it. Three things follow that the bot
has no mechanism to reach: (1) **compounding, not accumulating** - every structure does two
jobs (the inserter row is also the pole row is also the power mesh; one belt carries ore AND
fuel; one splitter allocates AND prioritises AND buffers), which is how the base got 13%
SMALLER and infinitely more productive at once; (2) **slack designed in, in the right places**
- pitch 7.0 against a 7.5 limit, degree 4 against a 5 limit, a 5-tile corridor buffer, 4
pre-laid pipes for a boiler column that doesn't exist yet - each costs something today and
removes a future rebuild, and the bot has margin nowhere; (3) **honest stopping** - a consumer
ahead of its supply is a liability that occupies tiles, demands pole coverage for nothing, and
emits a false green signal. The bot cannot stop, because its notion of progress is THINGS BUILT.

## A BOILER BURNS FOR ITS LOAD, NOT FOR EXISTING (measured 2026-08-29) — the coal deadlock

The gates deadlocked the base for a day. The log line looked authoritative:

    gate BLOCK: power_capacity x1 [coal_at_boiler] - coal 120/min < 178/min - mine more coal

It was wrong. Measured against the running game with the game's own statistics:

    boilers=2 engines=4 gen_kW=405.2 coal_mined_pm=120.0 coal_consumed_pm=6.0

`boilers * BOILER_COAL_PER_MIN` predicted **54/min**. The truth was **6.0/min**, and it is
exactly `405.2 kW * 60 s / 4 MJ = 6.08`. A boiler is not a fire that burns whether you use it
or not: it converts fuel in proportion to the steam its engines are actually asked for. The
plant was running at 11% load while the gate demanded 178 coal/min to protect it.

Two consequences, both fatal:
  * **capacity became self-blocking** — every boiler column raised the demand it had to
    satisfy, so the base could never grow its own power;
  * the only relief the ladder could then find was **12 burner coal drills**, on a map where
    the operator had just converted every burner to electric and deleted their fuel belts.
    A wrong model does not fail politely; it proposes undoing the operator's work.

**THE RULE.** Coal demand is `min(load, capacity) * 60 / COAL_FUEL_MJ` plus fed furnaces. And
the gate's bound is FUELABILITY, not a multiplier: the plant AFTER the build, charged at full
tilt, must be something the mine can actually run. That keeps the real protection (four
columns behind one drill is still refused) without inventing a constraint.

Note what `COAL_HEADROOM_MIN = 1.5` was: "measured 120 supplied / 77 demanded = 1.56" — a
margin calibrated from the output of the very model it was multiplying. **A constant derived
from a model cannot validate that model.** If a threshold's justification cites a number the
code itself computed, it has not been measured; go and measure it against the game.

## NO TIER REGRESSION: never relieve a constraint by rebuilding what the operator removed

`relief_drill` returns `None` — not `burner-mining-drill` — once a single electric drill stands
on the base. If the grid cannot carry another electric drill, the honest relief is MORE POWER,
and if that is blocked too then the base is genuinely cornered and the deadlock detector says
so. Reporting "no move" is correct; smuggling in a tier the operator has already torn out and
calling it progress is not.

Same law for sizing: `_relief_mine` counts the drills that ACTUALLY STAND (RCON
`find_entities_filtered` at the patch) rather than trusting `phase.json`. The ledger said
`have=0` while four electric coal drills were standing on the patch — planning from it would
have laid a second outpost on top of a working one.

## THE TRUCE MISSED THE ONE PATH THAT WRITES (2026-08-30, third offence)

From the live log, in order:

    06:27:59  operator online - layout heals suspended
    06:28:11  triage -> actuator fix_lanes
    06:28:38  triage -> actuator fix_lanes

Belts relaid under his hands while he was repairing them. The truce was checked in exactly two
places — the invariant audit and the LAYOUT_ISSUES heals — and **both of those are read-only**.
The LLM triage worker, whose verdict routes straight into `_fix_lanes` / `fix_unpowered` /
`keep_power`, had no check at all. The guard was on the harmless paths and absent from the
harmful one.

**RULE: a truce is a property of the WRITE, not of the loop that happens to be fashionable.**
When adding any new actuator path, the question is not "is this like the other detectors" but
"can this call `create_entity` or `destroy`". If yes, it consults `operator_present()`. Gate the
ACTUATOR and not the classifier — reading the world while he works is useful and costs nothing.

## AND IT WAS REPAIRING A LANE THAT WAS NEVER BROKEN

`fix_lanes` ran every 15–20 seconds for hours. The model read "18 furnaces starved, 8 drills
blocked, iron_pm dropping" and classified it `stall/lane - ore lane broken`. Every one of those
symptoms was real. The diagnosis was still wrong: all 28 furnaces were jammed at `full_output`
with 3200 plates in each terminal chest and **nothing consuming plates**.

A blocked OUTPUT presents identically to a starved INPUT one step downstream — drills blocked,
furnaces idle, plate flow zero — and the two have opposite fixes. Relaying belt cannot drain a
full chest, so the repair could never clear the condition that triggered it, which is exactly
why it ran forever. `_backpressured()` now discriminates on `full_output` and `_fix_lanes`
declines with a reason.

**THE GENERAL LESSON, which cost this project three sessions: when an actuator repeats without
clearing its own trigger, the diagnosis is wrong. Repetition is the evidence.** `lessons.add`
already counts repeat verdicts at 5 and 20 — that counter firing is a signal to re-diagnose, not
to keep actuating.

## `create_entity{direction=}` on an inserter points at the PICKUP, not the drop (2026-08-30)

Building the red-science chain, every one of five inserters was placed backwards. The output
inserter, placed at (30,10) with `direction=defines.direction.east`, reported:

    pickup=31,10 (the chest)   drop=29,10 (the assembler)

It was moving finished packs OUT of the chest and back INTO the machine. So `direction` faces
the SOURCE, which is the opposite of the mental model most of us carry from the game ("the
inserter faces where it puts things").

**AND IT LOOKED FINE.** The assemblers reported `working` and the production statistics showed
33 gears/min and 3 packs/min — because the build had PRIMED both machines by hand. The chain
was running on its seed stock while the inserters drained it backwards. Status was green,
throughput was real, and the topology was wrong.

**RULE: never accept `status == working` as proof a chain is connected.** Read
`pickup_position` and `drop_position` off every inserter you place and assert they are the two
entities you intended, by tile. That is a cheap read and it is the only thing that actually
tests the topology - the operator's "check all results of building anything to make sure
results are as expected" applied to the one property that entity status cannot show you.

## A GHOST IS A RESERVATION. can_place_entity CANNOT SEE ONE (2026-08-30)

The phase-1 bus was pre-flighted tile by tile and reported:

    116 checked | ORE:none | BLOCKED:none

It then destroyed **21 ghosts of the operator's reserved 36-lab array** — the entire service
column at x=14 (5 poles + 12 inserters, y=30..46) and four labs at x=16. A ghost is not a
collision, so `can_place_entity` returns TRUE over one and `create_entity` silently consumes
it. **"Is this tile empty" and "is this tile UNCLAIMED" are different questions**, and only
the first was being asked.

`autopilot.place()` now refuses ground held by a ghost of a DIFFERENT entity
(`GHOST_RESERVED`), and `autopilot.reserved_tiles()` is the probe a planner should call.
Building a ghost's OWN entity is still allowed — that is how a blueprint gets fulfilled and
`build_ghosts` depends on it.

**AND THE ROUTER WAS ALREADY RIGHT.** `belt_router.scan_obstacles(..., ghosts_hard=True)` has
treated ghosts as buildings the whole time, with a comment saying why. The bus went through the
reservation because it was laid by a hand-rolled placement loop that called `can_place_entity`
directly instead of routing through `belt_router`. **The bug was not a missing feature; it was
bypassing the layer that already had it.** Route with the router. If a build needs its own
placement loop, it must call `reserved_tiles()` first, and say in a comment why the router
would not do.

## AND: A CONNECTED LANE IS NOT A FAILED LANE (2026-08-30)

Nine times in twelve minutes:

    connect_mine_to_array(copper-ore): lane produced NO ore flow - removing what I built
    teardown_lane(copper-ore): removed 83 superseded belt tiles (refunded)

83 belts, torn out and rebuilt, nine times, while every copper furnace sat at `full_output`
with nowhere to put a plate. The verify was
`_lane_connected(ore) and lane_moves_ore(ore)` — which conflates **"this route does not
connect"** (a real failure; remove it) with **"this route carries nothing"** (correct
infrastructure, starved from somewhere else). Removing the second guarantees a loop: the next
pass lays the same route, measures the same zero, and removes it again.

`_verify_lane_or_remove` now separates them, and `no_flow_reason()` names the real stall
(jammed array / blocked drills / dead patch) instead of blaming the belt.

**This is the THIRD instance of one bug.** `fix_lanes` had it, `connect_mine_to_array` had it,
and the deadlock detector's coal model had it. Every time, a downstream blockage was read as an
upstream fault, and the "fix" could not clear its own trigger. When you find one of these,
**grep for the others before shipping** — `lane_moves_ore`, `build_worked`, `flow(` and any
`if not <flow> : remove` are where this family lives.

## FOUR LAWS FROM 2026-08-30, all of them destructive, all of them mine

### 1. WHITELIST WHAT MAY BE DESTROYED. NEVER BLACKLIST WHAT MAY NOT.

`plan_mine_geometry` cleared its lane row with *"anything that is not a belt / drill /
resource / character"*. That silently includes **inserters** — and it removed every inserter
taking finished plates out of the iron smelters. An area clear that names what it SPARES will
eventually eat something nobody thought to name. Debris (trees, rocks, ground items) comes
out; **machinery never does**. Poles get relocated. Anything else is left standing and
reported, and the lane routes around it.

### 2. NEVER TURN A BELT THAT IS CARRYING SOMETHING.

Seth: *"you turned a belt for some reason, never do that without measuring the outcome."* A
loaded belt is evidence that something upstream feeds it and something downstream expects it,
and neither is visible from a row scan. `fix_mine_row_flow` now re-points only EMPTY tiles and
reports the loaded ones it left alone. Turning an empty tile is recoverable; turning a live one
silently re-routes a working line into the wrong destination.

### 3. A BUS FEED COMES FROM THE PLATE OUTPUT BELT. ASK THE INSERTERS WHICH ONE THAT IS.

The bus was wired to the smelter rows' **INPUT** belts — twice, iron and copper — draining the
furnaces' ore and fuel away instead of carrying plates. Lane 35 was measured carrying
`coal:112`. Both times the "verification" was counting items on the belt, which proves a belt
is *moving* and says nothing about what it is moving *for*.

A belt has no intrinsic purpose; the machines beside it decide. An inserter that PICKS UP from
it is feeding a machine → INPUT. One that DROPS onto it is draining a machine → OUTPUT.
`autopilot.belt_role(x, y)` reads exactly that, and `bus_planner.check_feed_source(x, y, item)`
refuses to wire anything that is not a matching output. **Call it before placing one belt.**

### 4. ASSEMBLERS PULL FROM THE BUS. THEY ARE NOT HAND-PLACED BESIDE A SMELTER.

Seth: *"assemblers should be planned to pull from the main bus, reference the Nilaus
blueprints."* The 2026-08-30 gear+science pair improvised next to the iron row is exactly what
not to do: it has no bus tap, it sits in the smelter block's working space, and it had to be
tunnelled under later. Assembler blocks come from a blueprint (`bootstrap-red-science`,
`bootstrap-green-science` in the library), sited beside the bus with taps off it.

### AND THE POLE RULE, RESTATED

*"Never place more power poles than absolutely needed, no overlap ever ... always use the
minimum amount of power poles needed to power anything at the base."* The smelter stack ran 50
poles where 23 cover every consumer; 27 came out on 2026-08-30 (`unpowered=0/125`,
`networks=1`). A pole is redundant only when every consumer in its supply area is covered by
another pole AND removing it does not split the network — and the connectivity test must
include the poles OUTSIDE the area being culled as anchors, or the cull fragments the grid
(that failure once left 8 networks and 57 dark machines).

## NEVER STOP THE BOT BEFORE THE OPERATOR LOGS IN. HIS LOGIN IS THE STOP.

Seth, 2026-08-30: *"never stop before i login just let my login trigger the stop, why are you
doing this kind of shit that circumvents the rules im giving you."*

`docker stop factorio-autopilot` before he plays is not a safety measure, it is a bypass of the
mechanism he asked for. The truce IS the design: `operator_present()` pauses the builder and
withholds every actuator while he is connected. Stopping the container instead does three bad
things at once:

  * it substitutes an ad-hoc guard for the one that is tested and codified;
  * it hides whether the truce actually works — every manual stop is a test not run;
  * **and it blinds the learning hook.** The login/logoff snapshot in `controller.py` can only
    see a transition it is RUNNING to observe. Stop the bot before he logs in and there is no
    snapshot on login, no diff on logout, and his repairs are invisible. On 2026-08-30 he
    rebuilt both smelter-array output belts while the container was down; the next session
    then rediscovered those same facts from scratch and reported them back to him as news.

**THE RULE: leave the bot running. His login stops the builder; his logout resumes it and
triggers the diff.** If the truce is not trustworthy enough to leave running, fix the truce —
do not paper over it with a manual stop.

The one exception is a deploy, which restarts the container by its nature.

BACKSTOP, because a hook can only observe what it is running for: `bootstrap.diff_since_baseline()`
keeps a durable on-disk baseline and `planner.play()` diffs it at STARTUP, so an edit made
while the bot was down is still caught, attributed to the operator, and his removals protected.
