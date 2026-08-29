# Autopilot v2 — Autonomous Nauvis Megabase (design)

> Written 2026-08-29 from three research sweeps (existing bots, blueprint resources, latest
> beta mechanics) + everything in GOTCHAS.md / FRESH-START.md / ROADMAP.md. This SUPERSEDES the
> ROADMAP's "fresh-map autonomy" section as the plan of record; GOTCHAS.md remains binding law.
>
> GOAL: a fresh Nauvis map drives itself, with zero user assistance, from crash site to a
> train-connected city-block megabase — using community blueprints wherever possible, a main
> bus mid-game, and explicit phase transitions that tear down or upgrade prior-phase
> infrastructure. Self-learning via a feedback loop on halo's local models. Web dashboard on
> charon.

## 1. Version target: Factorio 2.1 experimental

Current stable is **2.0.77**; latest experimental is **2.1.17** (2026-08-26). FFF-444: 2.1 is
the FINAL major update, stable promotion targeted "end of summer 2026". Our charon server
already runs the 2.1 line (2.1.8) → **pin the factoriotools image to 2.1.17 and track 2.1.x**.
We migrate once and we're on the terminal API.

What 2.1 changes for us (verified against lua-api.factorio.com/2.1.17):
- **`LuaEntity.fluidbox` / `LuaFluidBox` REMOVED.** All fluid ops move to LuaEntity methods
  (`get_fluid_count` still works — our GOTCHAS pattern survives; `extract_fluid`,
  `clear_fluids`, `get_fluid_capacity` are the new surface). Audit any fluidbox-era code.
- **`surface.create_entities_from_blueprint_string{string, position, force, direction?, ...}`**
  exists (also in 2.0.77): stamp a blueprint string headless, no player, no item stack. This
  replaces our import_stack + build_blueprint dance as the primary stamping path.
  `LuaItemCommon.build_blueprint{build_mode=defines.build_mode.superforced}` is the fallback
  when super-force semantics are needed (note: moved to LuaItemCommon).
- **Script teardown/upgrade is first-class**: `surface.deconstruct_area{area, force,
  super_forced=true}` + `surface.upgrade_area{area, force, item=<upgrade planner>}` — the
  supported path for phase transitions (marks for bots; bots do the physical work).
- **Trains**: base-2.0 groups + schedule interrupts + stop priority + train limits = native
  LTN. 2.1 adds upgrade-planner support for rolling stock and quality-scaled wagons. Scripted
  via `LuaTrain`/`LuaSchedule`; NEVER hand-roll dispatch.
- **Ghosts persist indefinitely** — no re-stamp-on-timeout logic needed.
- **Research**: `LuaForce.research_queue` is writable in 2.x (invalid entries silently
  dropped — VERIFY after write); `add_research` one-at-a-time remains the safe path; trigger
  techs (oil-processing = mine crude) still can't be queued.
- **One-way saves**: a save/blueprint touched by 2.1 can never go back to 2.0. Fine for us.
- New circuit-readable entities (pipes w/ fluid+temp, boilers, labs) = free telemetry if we
  ever want combinator sensors; RCON reads make this optional.

## 2. What we learned from other bots (and why our lane is open)

- **FLE (Factorio Learning Environment)** — github.com/JackHopkins/factorio-learning-environment.
  MIT, active (June 2026), requires 2.0.73+, and is architecturally US: Python → RCON → Lua
  against a headless server. **Lift its Lua tool layer** (`fle/env/tools/agent/*/server.lua`):
  `connect_entities` (auto belt/pipe/pole routing between two entities — replaces our snaking
  `build_belt` and the fragile pipe geometry), `nearest_buildable`, `place_entity_next_to`,
  `get_resource_patch`. Months of work we don't have to redo.
- **The sobering SOTA**: on FLE's benchmark the best frontier model (Opus 4.1) clears 16/24
  lab tasks and **no adaptive agent has ever reached a rocket unassisted in open play**.
  Failure analysis: ~98% of top-model failures are STATE-TRACKING errors, not game knowledge.
  Design law #1: **the world model lives in Python and is authoritative; no LLM is ever asked
  to remember the map.**
- **The only bot that ever finished the game** is the any% TAS (gotyoke/Factorio-AnyPct-TAS,
  1:21:20 rocket): a compiled, deterministic per-tick task list. Design law #2: **split
  planner from executor.** The planner (slow, occasionally LLM-advised) emits declarative
  build orders; the executor (dumb, deterministic, verified) compiles them to primitive ops
  with post-condition checks. Adaptivity lives in WHAT we build, never in HOW an order runs.
- **factorio-draftsman** (github.com/redruin1/factorio-draftsman, pushed 2026-08-28): Python
  blueprint manipulation with explicit 1.1↔2.0 format support. Our blueprint pipeline's
  transform stage.
- Everything else (Windfisch bot, AI Player mod, MCP wrappers) is dead, demo-grade, or
  interactive-only. An executor-heavy design that reaches a megabase is genuinely novel.

## 3. Architecture (revision of the current codebase)

Five layers. Current files map into them; nothing is thrown away wholesale, but autopilot.py's
hardcoded-coordinate servicers finally die (ROADMAP's "two parallel architectures" root cause).

```
L4  LEARNING   architect v2 on halo (Qwen 35B) + lap-triage (4B) + lesson store + Coder-30B codegen
L3  PLANNER    phase state machine · goal stack · blueprint selection · site planning
L2  EXECUTOR   build-order compiler → verified primitive ops · task queue · retries · teardown
L1  WORLD      authoritative state DB: entities/registry/patches/power/ratios · snapshot diffing
L0  GAME I/O   rcon.py (persistent socket) · FLE-derived Lua tool layer · chunked reads
```

> **2026-08-29 revision (the sweep):** runtime control is INVERTED into `controller.py` —
> a realtime issue loop (sense → detect → prioritize → fix → verify → escalate → learn,
> ~3s cadence) that owns L4's triage/architect/lessons plus all self-heals; the builder
> thread only advances phase programs and is preempted by severity-0/1 issues. The v1
> maintain loop is retired. Architect output is an executable command queue (shared
> validated actuator catalog with operator prompts), never advisory prose alone.


- **L0**: keep rcon.py + the chunked-read pattern; add the persistent authenticated socket
  (ROADMAP HIGH). Vendor the FLE Lua chunks we adopt into `lua/` with attribution (MIT).
- **L1 World model** (`world.py`, merges gamedb.py + state-db.json): every entity WE build is
  registered (id, name, pos, role, phase, build-order id). Refresh by snapshot diff each lap.
  Servicing coords derive from the registry, never literals. The registry is also what makes
  teardown SURGICAL (GOTCHAS law: never area-destroy blind) — phase teardown = "everything
  registered to phase N with role X", plus script-marked deconstruction once bots exist.
- **L2 Executor** (`executor.py`): consumes declarative orders — `stamp(bp, at, align)`,
  `connect(a, b, kind)`, `mine_outpost(patch, n)`, `decon(area|tags)`, `upgrade(area, map)`,
  `research(tech)`, `train_route(...)`. Compiles to primitive ops; every op has a
  post-condition (entity exists / fluid flows / belt delivers / ghost count) and a bounded
  retry, then escalates to L4 triage instead of looping. All the GOTCHAS invariants (clearspace,
  power-before-production, boiler-never-dies, guarded remove{count>0}) live HERE as code.
- **L3 Planner** (`planner.py`): the phase state machine below + a goal stack seeded from the
  tech DB (techdb.py stays). Chooses blueprints from the local library, sites them (resource
  scan + terrain cost), sequences transitions.
- **L4 Learning**: §6.

## 4. Blueprint pipeline (`bplib.py`)

1. **Fetch** (Mac-side tool, curated into the repo — the server never fetches):
   factorio.school REST (`/api/blueprint/<key>`, `/api/blueprintSummaries/filtered/page/1?title=…`),
   FactorioBin CDN perma-txt, factorioprints Firebase fallback
   (`facorio-blueprints.firebaseio.com/blueprints/<key>.json` — yes, the typo'd host; strip the
   `favorites` blob).
2. **Verify**: decode base64→zlib→JSON; require `version>>48 == 2` before a string is admitted
   to the library. 1.1 rail blueprints are DEAD in 2.x (new rail geometry, no converter);
   1.1 non-rail stamps degrade silently (filter inserters → plain fast inserters). Anything
   1.1 gets flagged and only used after a draftsman pass + manual review.
3. **Transform** (draftsman): strip `snap-to-grid`/`absolute-snapping` when placing at exact
   coords (GOTCHAS), retile city blocks at the book's snap period, parameterize (recipe/ore
   swaps), split books into per-module entries with metadata (footprint, tech prereqs from
   techdb, entity cost, roles for the registry).
4. **Stamp**: `create_entities_from_blueprint_string` at planner-chosen sites, after chunk
   generation + terrain clear (trees/rocks destroyed, CLIFFS = move the site; all existing
   GOTCHAS rules). Pre-bots: ghosts are built by the executor in verified increments.
   Post-bots: ghosts + logistics do the work; executor just feeds materials and watches
   ghost-count → 0.

**Curated library** (`blueprints/`, all verified 2.0-format; keys resolve on factorio.school):
- **Bootstrap**: Jumpstart base → science 3 (2.0.15) `-OEAvLn7GVfCLngIvBSj`; Nilaus SA
  Masterclass book `-OXRxN4v1U8dwIjSO4l4` → "Nauvis Starter Base", "Starter HUB", "Early Game
  Smelting", "Starter Science".
- **Bus/mall**: same Nilaus SA book ("Oil Processing", "Robots Rockets & Space HUB");
  bus plan = 4 iron / 4 copper / 2 green circuit / 1 each steel·plastic·coal·stone (community
  standard); Raynquist Fall-2025 balancer book (factoriobin `cgn0od`, exported 2.0.6);
  DocJade AutoMall (2.0); Xeinaemm Tileable-Factories (github, tracks 2.1+SA) as the
  SA-native tileable alternative.
- **Rails/city blocks**: Nilaus "City Blocks 2.0 v1.2" (100×100) from `-OXRxN4v1U8dwIjSO4l4`;
  Nilaus Space Age book `-OBAyDy9PnXey5SMeUra` → "Substation Aligned Rail Segments",
  "Elevated Rail Segments"; "Elevated rails train city system" `-OBMVXEGc7VfjNXQtso4`
  (interrupt-based smart depot, native-LTN pattern); "City block for v2.0 with elevated
  rails" `-OE19hywPpVuQWsFJexu`.
- **Science at scale**: 2.0 tileable lab/science arrays (`-OAA94aHsDaXcxqAAjKo`,
  `-OBHygPqkr3QwKbmk-Iq`); the existing megabase book (factoriobin `ftrgxd`) stays as
  DESTINATION modules, stamped as tech unlocks.
- **Ratios**: vendor factoriolab's static recipe JSON (github.com/factoriolab/factoriolab,
  MIT, full 2.x+SA data) for planner math — no live web dependency.
- Existing early-game-robot-factory BP (374 ents) stays: it's the hand-built (bot-built by
  executor) robot-rush target at the Phase 1→2 boundary.

## 5. Phase plan

The state machine. Each phase has an entry gate (checked against world model + techs), a build
program, an exit gate, and an explicit TRANSITION step (teardown/upgrade). "Bus base becomes
the mall; the megabase is stamped on fresh land" is the community-consensus transition and
ours too.

### Phase 0 — Bootstrap (crash site → automated red/green science)
Existing bootstrap.py, hardened per ROADMAP; this phase is deliberately blueprint-light
(hand-built by executor primitives) because nothing exists yet.
- clear_spaceship_debris → scout (iron/copper/coal/stone/water AND crude oil — record
  distance; it shaped the last map at 440 tiles) → richest-spot mine outposts →
  smelting → `build_power_plant` (Seth's verified column design) → stamp "Early Game
  Smelting"/"Starter Science" as soon as materials allow → red+green automated, ~8 labs.
- **Exit gate**: red+green ≥ 30 SPM sustained, power headroom >30%, oil patch located,
  automation-2 + logistics-2 researched.
- **Transition**: none — Phase 0 structures are registered `phase=0` and live until Phase 2
  eats them.

### Phase 1 — Bus base (oil → blue science → CONSTRUCTION ROBOTS)
- Stamp the Nilaus starter-HUB/bus skeleton: 4/4/2/1 lanes, smelting columns feeding the bus,
  science pulled off the bus (never script-fed labs — FRESH-START law).
- Oil: pumpjack at the scouted patch (fires the oil-processing trigger), refinery + chem
  plants ("Oil Processing" module), plastic → advanced circuits → blue science.
- Research to construction-robotics (add_research one at a time); executor-build the
  early-game-robot-factory BP; first roboports + ~50 construction bots + mall
  (AutoMall) producing: belts, inserters, assemblers, poles, rails, signals, roboports,
  train parts, bots.
- **Exit gate**: ≥100 construction bots idle in network, mall producing rail/train/roboport
  items, blue science sustained, logistics chests researched.
- **Transition (teardown/upgrade #1)**: `upgrade_area` the whole registered base — stone→steel
  furnaces, asm-1→asm-2, yellow→red belts where throughput-gated (Seth's justified-upgrade
  rule, now enforced by the planner's bottleneck metric). Phase-0 relics that the bus
  replaced (starter smelt columns, hand-rows) are marked via registry → `deconstruct_area`
  on their exact bounds → bots reclaim the materials. Character hauling ends here — belts
  and bots take over; haul_ore/fuel-shuttle code retires.

### Phase 2 — Robot mall era (bus base matures into the megabase's factory-factory)
- Scale power (solar/accumulator blocks or big steam per factoriolab math), expand mall to
  full construction kit incl. trains, elevated rails, city-block materials; artillery-free
  perimeter defense per fortify().
- Purple/yellow science on the bus (tileable modules) — enough to research elevated-rails,
  logistics-3, artillery-adjacent defense, and megabase techs while blocks come online.
- Site survey for the city grid: planner picks a region of fresh land (flat, resource-rich
  quadrant) and fixes the GLOBAL GRID ORIGIN aligned to the City Block snap period.
- **Exit gate**: mall stock covers ≥N blocks of materials (rails, roboports, big poles,
  substations), elevated-rails researched, ≥400 bots, first ore patch outside the bus base
  surveyed for train service.
- **Transition**: nothing torn down — the bus base is RE-ROLED: registry re-tags it
  `role=mall`; its science lines keep running until block-science exceeds them (Phase 3),
  then bus science is deconstructed and the bus base shrinks to mall+trainyard inside the
  grid's edge.

### Phase 3 — City blocks + trains (the megabase)
- Stamp City Blocks 2.0 grid block-by-block from the origin: roboport/substation/big-pole
  skeleton first, then rail blocks (substation-aligned + elevated segments) forming the
  4-lane grid arteries.
- Train network: generic trains in GROUPS with interrupts ("cargo empty → [item] pickup",
  "cargo full → [item] dropoff"), stop limits + priority set by circuit; depots from the
  elevated-rails city system book. Ore outposts get train stations (big mining drills once
  a single Vulcanus trip unlocks them — optional stretch; belt-era drills otherwise).
- Block build order (each block = stamp + train hookup + registry): smelting blocks →
  green/red circuit blocks → science module blocks (tileable 2.0 arrays) → mall block
  (relocating mall production INTO the grid) → repeat by throughput math.
- **Transition (teardown #2)**: once grid science > bus science, deconstruct bus science +
  redundant bus lanes (registry-scoped); the old base ends as one mall/trainyard block.
- **Exit gate / victory ladder**: 1k SPM sustained → grid ≥ 5×5 blocks → self-expanding
  steady state (planner keeps adding blocks + outposts as ore depletes, using the same
  relocate-to-richest logic that already exists for early mines).

## 6. Self-learning loop (halo local models)

All inference on halo's Lemonade server (LAN :13305, OpenAI-compatible; offline=true;
key in Keychain `lemonade-api-key`). Context budget 60k per GOTCHAS-of-halo (131k KV shared
across 4 slots). No cloud in the runtime loop.

- **Lap triage — Qwen 4B, every maintain lap (~seconds, cheap)**: input = compact state
  delta (status histogram, power %, science/min, executor failures); output = one of
  {healthy, watch, stall(class), anomaly} + a one-line reason. Drives the dashboard status
  light and decides whether to wake the architect early.
- **Architect v2 — Qwen 35B, every ~15 min or on triage escalation**: architect.py retargeted
  from the Claude API to halo, same snapshot→report pattern (chunked RCON read stays).
  Output = structured JSON: bottleneck ranking, phase-gate assessment, prioritized action
  list — every action validated against techdb + world model before the planner may enqueue
  it (schema-checked; invalid actions rejected and logged as a lesson).
- **Lesson store (`lessons.db`, SQLite on charon /mnt/cache per the shfs rule)**: every
  executor post-condition failure, triage anomaly, and architect finding lands as a
  structured lesson: {condition, mistake, rule, evidence, phase, count}. Top-K relevant
  lessons (by phase + entity types involved) are injected into every architect/triage
  prompt — the automated GOTCHAS.md. Lessons that fire repeatedly get PROMOTED:
- **Codegen — Qwen Coder-30B, offline, event-driven**: a promoted lesson + the relevant
  function source → a drafted patch (new guard, adjusted threshold, new post-condition) as a
  branch + PR via the normal WORKFLOW.md pipeline. NEVER auto-merged: Seth (or a Mac-side
  Claude session) reviews. This automates the standing "architect is a teacher — distill
  findings into code" directive.
- **Claude API**: demoted to occasional offline auditor (blind-spot hunts over the lesson
  store + snapshots), run manually from the Mac. Never in the loop.
- **Eval ladder (FLE-inspired)**: scripted scenario saves (burner-era, bus-era, robot-era,
  rail-era) + headless runs with score = production/SPM deltas; run after every deploy as
  regression CI. This is how we know a "learned" change helped.

## 7. Dashboard (charon, Tailscale-only)

Container `factorio-dash` next to the autopilot (compose service; restart:always; no public
ports). Stack: FastAPI + one WebSocket + static SPA (vanilla/preact, no build treadmill).
- **Reads**: status.json heartbeat, world-model registry, lessons.db, architect reports,
  eval-ladder history; live RCON via the autopilot's socket (read-only command allowlist).
- **Panels**: phase/gate progress (the §5 ladder as a stepper), production graphs
  (`force.get_item_production_statistics` — SPM, plates/min, power), research queue + ETA,
  mine patches (density/remaining, relocation events), executor queue + recent failures,
  triage light + latest architect report, lesson feed, map view (registry entities rendered
  to canvas by chunk; `game.take_screenshot` stills on demand), and a manual override lane
  (pause phase transitions, veto a teardown, enqueue an order) — auditable, logged as
  lessons.
- Auth: none beyond Tailscale (haus convention), read-only by default; overrides behind a
  single shared token.

## 8. What changes vs. the current codebase

| Current | Fate |
|---|---|
| rcon.py | keep; add persistent socket |
| bootstrap.py | becomes Phase 0 program under L3; coords → registry |
| autopilot.py primitives (place/walk/clear/lay_belt_path/power plant) | keep → L2 op library |
| autopilot.py hardcoded servicers (haul_ore, feed_smelter, fixed coords) | retire at Phase 1 exit; registry-driven replacements |
| build_belt (A* snaker) | replaced by FLE-derived connect_entities |
| gamedb.py + state-db.json | merged into world.py registry |
| techdb.py / tech-tree.json | keep (re-dump on 2.1.17) |
| architect.py (Claude API) | retarget to halo 35B; becomes the L4 core |
| patrol.py / tasks.py / status.py | fold into maintain strand + dashboard feed |
| blueprints/ | grows into the curated, versioned library (§4) |
| GOTCHAS.md | still law; new invariants get encoded in L2 post-conditions AND documented |
| deploy.sh / WORKFLOW.md | unchanged (PR → merge → deploy); dashboard gets its own compose service |

## 9. Build order (implementation roadmap)

1. **Server to 2.1.17** + tech-tree re-dump + fluid-API audit. (small)
2. **World registry + executor skeleton** (orders, post-conditions, retries) — port 3-4
   existing builds (mine outpost, power plant, smelter array) onto it. (the core investment)
3. **FLE Lua vendoring**: connect_entities + nearest_buildable wired as L2 ops. (medium)
4. **Blueprint pipeline**: fetcher/verifier/draftsman transforms + stamp-and-verify op;
   curate the §4 library into the repo. (medium)
5. **Phase 0-1 program** on the new stack; staged proving run on a fresh map (the ROADMAP's
   original plan, now on better rails) → to construction robots unassisted. (the grind)
6. **L4 loop**: triage + architect-on-halo + lessons.db (+ prompt injection). (medium)
7. **Dashboard v1** (status/phases/production/lessons). (parallelizable with 5-6)
8. **Phase 2-3 programs**: mall scale-out, grid origin, block stamper, train dispatcher;
   staged proving runs per phase gate. (the second grind)
9. **Coder-30B lesson→PR pipeline + eval ladder CI.** (last; needs stable everything)

## 10. Risks / open questions

- **2.1 experimental churn**: tracked releases may break API details mid-run; pin per-patch,
  re-dump tech tree on bumps, keep the fluid-audit list.
- **Blueprint licensing/availability**: strings are cached into the repo (curated, attributed);
  no runtime web dependency.
- **Space Age scope**: big mining drills/foundries need one Vulcanus trip. The design treats
  them as an OPTIONAL stretch (Phase 3 works belt-drill-era without them). If Seth wants a
  pure-Nauvis run, we skip; if not, a minimal scripted Vulcanus hop becomes Phase 2.5 —
  decide before Phase 2.
- **Defense**: biter pressure on a sprawling grid; Phase 2 must include walls/turrets per
  block edge + repair-bot coverage before the grid outruns the perimeter (evolution scales
  with pollution). fortify() logic generalizes to block edges.
- **Halo capacity**: 35B architect calls share the 131k KV pool with Hermes slots — batch the
  snapshot (already chunk-read), keep prompts ≤8k, and rate-limit to one architect call in
  flight.
- **The honest unknown**: nobody has done this. FLE SOTA stalls before rockets; the TAS was
  hand-compiled. Our edge is the executor-heavy split + blueprints + a persistent world
  model; expect the proving runs (steps 5, 8) to dominate the calendar, exactly like the
  first bootstrap did.
