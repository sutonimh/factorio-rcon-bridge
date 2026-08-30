# OPERATOR PRINCIPLES — the reference spec

Reverse-engineered from Seth's hand-optimization of the bot's base, 2026-08-29.
Sources: `snapshots/before.json` (713 entities, bot-built) → `snapshots/after.json`
(619 entities, operator-optimized), plus read-only RCON probes of the live server.

**Every number in this file is measured, not assumed.** Where a claim came from a probe
rather than the snapshots, it says so. Machine-checkable forms of these rules live in
`principles.py`; the terse narrative version is the GOTCHAS section "THE OPERATOR'S
DESIGN PRINCIPLES". This file is the one planners should be written against.

> The operator did not clean up the bot's base. He **deleted the bot's system and rebuilt
> it as a different one**: 102 of 107 poles relaid, 121 belts removed and 80 added, the
> power plant moved, all 11 burner drills replaced. Five poles survived.

---

## 0. The one-line version

**The bot's success criterion is that a placement succeeded. The operator's is that an
item arrived.**

Everything below is a way of turning the second criterion into something a program can
assert. The cheapest complete version is two checks — `no_belt_without_consumer()` (P1)
and `grid_is_single_network()` (P2). Run those after every builder and the bot cannot
produce the base it produced.

---

## 1. Measured prototype constants

Read live from the running server, 2026-08-29. **Never hardcode a pitch a prototype can
tell you** — every spacing below is derived from one of these.

| Query | Value |
|---|---|
| `small-electric-pole.get_max_wire_distance()` | **7.5** |
| `small-electric-pole.get_supply_area_distance()` | **2.5** → a 5×5 supply box |
| pole copper connection limit | **5** (design cap: 4) |
| `electric-mining-drill.tile_width` | **3** |
| `electric-mining-drill.mining_drill_radius` | **2.49** |
| `underground-belt` max_distance (basic) | **5** |
| `electric-mining-drill` rate | 0.5 speed ÷ 1.0 mining_time = **30 ore/min** |
| `stone-furnace` rate | 1.0 speed ÷ 3.2 s = **18.75 plates/min** |
| yellow inserter | ~0.83 items/s ≈ **50/min** |
| boiler / 2 engines | 1.8 MW / 1.8 MW |

**Derived ratio:** 1 drill : 1.6 furnaces. A 16-furnace array wants 10 drills; the
operator has 6, so his arrays are knowingly drill-limited.

### Direction and position conventions (get these wrong and everything inverts)

- Directions are 16-way; belts, drills and inserters use only the cardinals:
  `0 = N (0,-1)`, `4 = E (1,0)`, `8 = S (0,1)`, `12 = W (-1,0)`.
- **An inserter's `direction` points at its PICKUP side; it drops on the opposite side.**
  Measured: inserter `(-5.5, 4.5)` dir 8 → `pickup (-5.5, 5.5)`, `drop (-5.5, 3.3)`.
  `principles.test_inserter_direction_convention` is the regression guard.
- A drill's drop position is `center + dir × (height/2 + 0.35)`. Measured: drill
  `(14.5,-42.5)` dir 8 → `drop_position (14.5, -40.652)` → tile `(14,-41)`.
- Tile of a coordinate is `math.floor(coord)`.
- Bounding boxes must be rounded to tile boundaries with **±0.5**, not ±0.01: the
  offshore pump's box is `-32.10 … -30.90`, which floors to a spurious 3-tile span.
- `LuaEntity.copper_connection_count` **does not exist in 2.0** — count connections via
  `get_wire_connector(defines.wire_connector_id.pole_copper, false).connections`.
- `LuaEntity.neighbours` reads nil for underground belts over RCON; pair them
  geometrically (nearest opposite mouth, same direction, within max_distance).
- `electric_network_id` is the reliable "does this need a pole?" discriminator: present
  on drills, inserters, labs, assemblers, engines and poles; **absent on stone furnaces
  (burners), boilers, chests and belts.**

---

## 2. The principles

### P1 — Flow is the only success metric. Entity count is not progress.

A build is complete only when material is measurably *moving* end to end. Measure
throughput at the **consumer** after every builder. Any sub-build producing zero flow is
torn down in the same pass.

**Metric.** Flood-fill the belt graph forward from every producer drop tile and backward
from every consumer pickup tile:

```
flow_coverage = |fwd(producers) ∩ bwd(consumers)| / |belt tiles|
```

| | bot | operator |
|---|---|---|
| entities | 713 | **619** |
| belt tiles on a producer→consumer path | 189 / 470 = **40%** | 408 / 431 = **95%** |
| no producer upstream | 229 (49%) | — |
| no consumer downstream | 80 (17%) | — |
| iron / copper / coal per min | **0 / 0 / 0** | 174 / 90 / 120 |
| stone furnaces with recipe `None` | **28 of 28** | 11 of 28 (still filling) |

**Check:** `no_belt_without_consumer()`, `production_is_moving()`.
**Bot code contradicted:** `connect_mine_to_array` (bootstrap.py L805) already contains
this law — `lane_moves_ore` → `teardown_lane` — and is the *only* builder that does.
`build_smelter_array` (L599), `build_belt_supply` (L~845), `coal_to_boiler` (L2231) and
`setup_science_io` all return success on placement.

---

### P2 — One electric network, energized, before any electric entity exists.

The generator array is placed first and is the root of the grid. After placing any
electric entity, read `electric_network_id` and compare it to the root's. Never "get
close" to a network.

**Evidence.** The bot had **two** networks: net 1 (105 poles, 2 engines) and **net 405 —
6 electric mining drills and 2 poles with no generator on them**. Minimum distance from
the main grid to that island: **8.06 tiles** (`(12.5,-37.5) → (16.5,-44.5)`) — **0.56
tiles past the 7.5 wire reach**, after a 19-pole chain had been laid ~52 tiles toward it.
Coal: 0/min. The operator: **69/69 poles on network 535, zero orphans, zero `no_power`**;
his replacement run is 20 poles over 124 tiles and it connects. Coal: 120/min.

**Slack is the point.** Cap pole degree at **4** of the available 5 so later poles can
auto-adopt a neighbour. Live probe 2026-08-29 (after the bot resumed building): degree
distribution `1:6 2:29 3:25 4:23 5:10 6:2` — **12 poles at or past the cap**, max wire in
use 7.28 of 7.50.

**Check:** `grid_is_single_network()`, `pole_degree_headroom()`, `wire_reach_respected()`.
**Bot code contradicted:** `fle_tools.connect` (L1425) and the bridge at L1710-1711 —
`steps = ceil(sqrt(bd)/6)` then `x = floor(ex + (tx-ex)*i/steps)` — *interpolates* pole
positions and hopes the spacing lands under reach; `can_place_entity` failures silently
drop poles, leaving holes.

> Related standing lesson (GOTCHAS): script-placed poles do **not** reliably auto-connect.
> After any scripted placement, wire every pair within reach explicitly and then verify by
> comparing `electric_network_id`. Placement never implies connection.

---

### P3 — Choose the geometry first; derive every entity from it.

Each module is a parameterised template with an anchor and fixed offsets. Pick the anchor
(lane row, zone origin, spine column), then compute every position. Never survey what got
built and rationalise a layout around it.

#### The mine template — 9 rows, zero waste

Because `mining_drill_radius` 2.49 > the lane offset 2.0, the belt row *and* both pole
rows are still inside the mined band. Nothing is wasted by putting them there.

```
lane − 4    POLE row
lane − 2    drill row, dir 8 (S)   drop = center + 1.85  → lands on lane
lane        BELT lane
lane + 2    drill row, dir 0 (N)   drop = center − 1.85  → lands on lane
lane + 4    POLE row
```

Verified at two patches:

| patch | lane | drill rows | pole rows | drill pitch |
|---|---|---|---|---|
| copper | −63.5 | −65.5 (S), −61.5 (N) | −67.5, −59.5 | 3 |
| coal | 15.5 | 13.5 (S), 17.5 (N) | 11.5, 19.5 | 3 |

#### The smelter stack — 6 rows, 9-tile pitch in y

`SMELT_ZONE`: iron `oy = 3`, copper `oy = 12` (pitch **9**).

```
oy + 0    BELT   (plate OUT)
oy + 1    inserter row + poles     (picks from furnace, drops on the out-belt)
oy + 2‥3  FURNACE row              (2×2)
oy + 4    inserter row + poles     (picks from the in-belt, drops in furnace)
oy + 5    BELT   (ore + coal IN)
```

Measured: iron block belts at y 3.5 / 8.5, inserters 4.5 / 7.5, furnaces y=6 (x −5…25,
16 of them). Copper block belts 12.5 / 17.5, inserters 13.5 / 16.5, furnaces y=15
(x −5…17, 12 of them).

#### The power plant — 2 free parameters

Free params: spine column `Sx` and boiler row `By`. **`By` is derived from the
shoreline**, not chosen.

```
boilers   (Sx − 2.0, By)   (Sx + 2.0, By)          column pitch 4
engines   (Sx ∓ 2.0, By − 3.5 − 5k)  for k = 0,1   engine stack pitch 5
pipe      (Sx, By + 0.5)                            ONE pipe feeds BOTH boilers
pump      (Sx, By + 5.5)
pole      (Sx, By − 5.5)                            centroid of the 2×2 engine array
```

Measured live: boilers `(-33.5,46)` `(-29.5,46)`; engines `(-33.5,37.5)` `(-29.5,37.5)`
`(-33.5,42.5)` `(-29.5,42.5)`; pipe `(-31.5,46.5)`; pump `(-31.5,51.5)`; pole
`(-31.5,40.5)`. So `Sx = -31.5`, `By = 46`.

**Check:** `mine_row_geometry_ok()`, `every_drill_drops_on_lane()`.
**Bot code contradicted:** `plan_mine_geometry` (L1776) sets
`lane_y = Counter(d["dy"]).most_common(1)` — it reads the lane off wherever the drills
happened to drop, then rotates and nudges drills to match. That ratifies a bad layout
instead of imposing a good one; its own docstring refers to "poles I dropped on it".

---

### P4 — Service infrastructure rides *inside* the machine rows and doubles as the mesh.

Poles go in the free tiles of rows that already exist, positioned so consecutive rows are
within wire reach of each other. **Never a dedicated pole row, a pole spine, or a pole
"beside" a module.**

**The phase trick.** In the smelter block, poles sit **on the inserter rows** at x-pitch
4, phase `x ≡ 1.5 (mod 4)`: x = −6.5, −2.5, 1.5, 5.5, 9.5, 13.5, 17.5, 21.5, 25.5.
Inserters sit at `x ≡ 0.5 (mod 2)` and 2×2 furnaces at `x ≡ 1.0 (mod 2)`, so **the pole
takes the free half-tile of every other furnace column and costs zero machine slots.**
Every block pole covers exactly 2 inserters.

Pole rows within a stack are **3.0** apart, between stacks **6.0** — both under 7.5, so
**the service poles ARE the network inside the block and zero distribution poles are
needed.** Smelter block: **58 → 34 poles, 17 distinct pole rows → 4**.

Same idea at the plant: **one pole at `(-31.5,40.5)`**, the exact centroid of the 2×2
engine array, powers **all four steam engines** (supply box x[−34,−29] y[38,43] clips all
four bounding boxes). The bot used 3 scattered poles for 2 engines.

| metric (measured by `principles.metrics`) | bot | operator |
|---|---|---|
| pole→consumer incidences per consumer | **2.171** | **1.014** |
| consumers per pole | 0.710 | **1.072** |
| poles | 107 | 69 |
| poles powering nothing | **40 (37%)** | 3 |
| fully redundant poles | 36 (34%) | 0 |

> Scoring subtlety: coverage is owed to **electric consumers**, not "machines". A stone
> furnace is a burner and needs no pole; an inserter does. Score against
> `electric_network_id`-bearing entities or the whole metric inverts.

**Check:** `poles_cover_machines()` (a pole must power something or be a trunk hop; a pole
that is redundant for coverage is spared if it is a cut vertex of the wire graph).
**Bot code contradicted:** `build_smelter_array` L633-634 —
`for x=ox-1,ox+n*2,3 do create pole at (x+0.5, oy-0.5); create pole at (x+0.5, oy+6.5) end`
— two dedicated pole rows straddling the block at pitch 3, plus a vertical spine, one tile
beyond the belt lanes, triple-covering the inserters. The four deleted pitch-2 rows
(`y=2.5` ×14, `y=9.5` ×14, `y=18.5` ×7, `y=11.5` ×6) are **41 of the 102 removed poles**.

---

### P5 — Every pitch is derived from a prototype constant; nothing is placed without `can_place_entity`.

`create_entity` performs **no collision check**, so wrong geometry *succeeds silently*.

| context | pitch | derivation |
|---|---|---|
| drills along a row | **3** | `= drill.tile_width`; `can_place` at 3 true, at 2 **false** |
| poles in a machine row | **4** | supply 2.5 → 5×5 box; tiles a 1×1 row with 1 tile overlap |
| poles on a trunk | **7.0** | 93% of wire 7.5 — a 0.5-tile safety margin |
| boiler columns | **4** | 3-wide boiler + **1-tile spine column** for everything shared |
| engine stack | **5** | engine is 3×5; abuts the boiler at `By − 3.5` |
| smelter stack | **9** in y | belt / ins / furnace / ins / belt |

Also invariant in `after`: **minimum pole separation 3.0** (bot: 8 pairs at 1.0, 4 at
1.41, 42 at 2.0), max wire 7.28, mean 4.96.

**Three burials, all from bare `create_entity`:**

1. `build_mine_outpost` (L941) steps `dx = rx - n + 2*k` — a pitch hardcoded for the 2×2
   burner drill and inherited by `electrify_mines` (L2282), which calls `create_entity`
   with no check. The 6 iron drills at x = 14.5…24.5 step 2 are 3×3: bounding boxes
   `[13,15]` and `[15,17]` **share a whole tile column**. Still live today.
2. `coal_to_boiler` (L2231) computes `bx = floor(boiler.position.x) = -34` and places the
   fuel inserter at `(-34.5,46.5)` — **inside the boiler's own footprint** — treating a
   3-wide entity's centre as its left edge. It then sets `pickup_position` onto the water
   riser pipe. The plant had no working coal feed at all.
3. A belt row laid at `lane ± 1` lands inside a 3×3 drill footprint: **13 belts survive at
   y = −41.5, x = 13.5…25.5 — exactly the drills' span — permanently unclickable.** The
   operator deleted every belt on that row he could reach (x 8.5–12.5 and 26.5–31.5) and
   left precisely the buried ones. *This is the only hard failure remaining in his base.*

Only `autopilot.place` (L683) collision-checks; the Lua fast paths do not.

**Check:** `no_entity_overlap()`, `drill_pitch_ok()`, `wire_reach_respected()`.
Add a cleanup pass for belts covered by a drill footprint — a human cannot remove them.

---

### P6 — Share every line from both sides. Separation is per *lane*, not per belt.

Before adding a second belt, pipe, pole or inserter, ask what the existing one's unused
side is doing.

- **Two drill rows feed one lane.** Ore per tile of row length: burner @pitch 2 =
  0.125/s; electric @pitch 3 single-sided = 0.167; electric @pitch 3 **double-sided =
  0.333 (+167%)**. Measured lane contents prove it: copper `L1=4 L2=4`, coal `L1=4 L2=4`,
  **iron (single-sided) `L1=0 L2=4` for all 115 tiles.**
- **Ore + fuel on one belt.** Stone furnaces are burners; they need coal *and* ore. Two
  opposing side-loads at one merge tile: `(-7.5,8.5)` E takes iron ore from the north
  (`(-7.5,7.5)` S) and coal from the south (`(-7.5,9.5)` N) → `{L1: iron-ore, L2: coal}`.
  Copper mirrors it at `(-7.5,17.5)`. The ratio being wildly off (a furnace wants ~13.9
  ore per coal; the splitters deliver ~1:1) is **harmless because the lanes back-pressure
  independently**.
- **One pipe, two boilers.** The boilers' water inputs are the two *end* tiles of the
  southern row — boiler W's east input and boiler E's west input are **the same tile
  `(-32,46)`** — so one pipe at `(-31.5,46.5)` feeds both.
- **The spine.** Column pitch 4 leaves free column `x = -32`, which carries the pole, the
  shared pipe, the water risers and the pump, leaving **both long faces of every boiler
  free**: north = engine, south = fuel inserter, `By+2` = coal belt.

**Check:** `lane_shared_from_both_sides()`, `one_lane_per_item_per_destination()`,
`plant_ratio_ok()` (pipes per boiler).
**Bot code contradicted:** `build_belt_supply` gives every commodity its own belt
*column* (`iron ox-2`, `copper ox-4`, `coal ox-6`) with the comment *"a shared column
MERGED the ore lanes (mixed-ore law violation)"*. The bot learned "don't mix ores" and
over-generalised to "don't share a belt". It also had **no fuel path to the copper array
in the design at all**, and `_build_boiler_engine` (L267) grows one boiler *longer* with
more engines instead of pairing.

---

### P7 — Direction is computed from the destination. Never a constant, never "keep the prior heading".

A collector's direction is `sign(destination − source)`. A merging lane's **final tile
points into the target belt**, not along its own travel.

**Evidence.** **13 of the 16 rotations in the diff are one row**: `y=-40.5, x=13.5…25.5,
E→W`. That is the only row the iron drills drop on (`drop_position y = -40.65`), and it
pointed **east, away from the base**, into a dead end at `(30.5,-40.5)`; every drill read
`waiting_for_space_in_destination`. Thirteen right-clicks took iron from **0 to 180
ore/min** (= 6 drills × 0.5/s × 60, the theoretical maximum).

Two more rotations are merge orientation: `(-7.5,9.5) E→N` (coal was ending pointing east
into bare ground, one row below the belt it was meant to join) and `(-9.5,17.5) E→S` (was
entering the copper feed head in-line from the west, occupying **both** lanes; rotated, it
feeds the dogleg and side-loads onto L2 only, leaving L1 for coal).

**The dogleg is deliberate lane engineering.** Copper arrives southbound on `x=-9.5`; to
land on lane 2 it must enter from the *south*, so the trunk runs 2 tiles past the feed row
to `y=19.5`, 2 east, then back **north** to `(-7.5,18.5)`. Cost 7 belts; benefit, L1 stays
free for coal.

**Check:** `no_belt_into_wall()`. For a merging lane, assert the last tile is orthogonally
adjacent to the target *and* its direction points at the target. Add a `merge_into=(x,y)`
argument to `lay_belt_path` and set the final direction from it.
**Bot code contradicted:** `build_smelter_array` (L599) hardcodes `direction=4` (east) for
both belt rows — correct for the array, and the bias leaked into the mine collector;
`mine_layout.plan_outpost` still hardcodes `"direction": E` for lane tiles. `lay_belt_path`
(L645) ends with `tiles.append((…, tiles[-1][2]))` — "last tile keeps prior direction".

---

### P8 — Trunks are straight, dedicated, parallel, and never cross. Turns are a budget.

Long runs are pure axis runs on clear ground at fixed pitch. Power trunks *parallel* the
belt corridors at fixed clearance — they do not follow them and do not hop machine to
machine. One column per commodity; collectors stop one tile short of the neighbouring
column so no crossing is ever needed.

**Belt.** 104-tile copper trunk = **1** direction change; 82-tile iron trunk = **2**; all
four smelter rows = **0**. Total 19 turns across 429 belts. Ore columns `x=-9.5` (copper,
82 tiles, y −63.5…19.5) and `x=-7.5` (iron, 61 tiles, y −40.5…19.5) run parallel 2 tiles
apart and never meet.

**Power.** N–S trunk `x=-14.5` from `y=-64.5` to `26.5`: **14 poles, 91 tiles, gaps
7,7,7,7,7,7,7,7,7,7,7,7,7**. Second column `x=-35.5`, 5 poles, gaps 7,7,7,7. Both share
phase `y ≡ 5.5 (mod 7)`. The trunk runs **5.0 tiles west** of the main belt corridor in
open ground (a 4-tile clear buffer — room to widen the corridor to 4 lanes without ever
moving the trunk) and **exactly 1 tile east** of the coal belt column where space is
tight. It T-junctions into modules at four points and terminates on module anchor poles
with a **shortened final hop** (6.0, 4.12): pitch is nominal, endpoints are hard.

> The invariant is **one-sided**: a *shorter* hop always still wires, so only *exceeding*
> the pitch is a violation. `trunk_pitch_ok()` enforces it that way.

**Check:** `trunk_pitch_ok()`, `no_pole_on_lane()`.
Perpendicular pole-to-lane offsets measured in `after`: −5.0 ×10, −1.0 ×17, +1.0 ×21,
−3.0 ×7, −4.0 ×4, tail at 9 — **never 0**. In `before`: the same mass **plus a junk tail
out to 25 and 5 poles at offset 0**.
**Bot code contradicted:** the bot's pole set is a staircase of one-off drops —
`(-3.5,-13.5) (0.5,-16.5) (0.5,-20.5) (4.5,-23.5) (2.5,-25.5) (3.5,-30.5) (8.5,-30.5)
(7.5,-35.5)` — 19 poles at 2.72 tiles/hop that never arrive, produced by `fle_tools.connect`
(L1425) plus the reactive self-heal at L1701-1711 (*on `status == no_power`, drop a pole
next to the entity and interpolate toward the nearest network*). That handler produced 39
zero-coverage scatter poles and put a pole at `(-9.5,20.5)` **inside the main N–S belt
column's own line**. **Poles must come from a module template, never from an error handler.**

---

### P9 — Cross underneath. Replan the corridor *before* an obstacle, never give up at it.

Belt meets belt → underground pair, entrance one tile before, exit one tile after,
span = obstacle width + 1. Belt meets *building* → replan the whole corridor from the last
free junction, **before laying anything**. Underground the line with fewer downstream
consumers. Same rule for fluid.

**Evidence.** 3 belt crossings, 3 underground pairs, **all exactly span 2**:
`(-10.5,10.5)→(-8.5,10.5)` and `(-10.5,11.5)→(-8.5,11.5)` (coal under the copper trunk),
and `(-9.5,14.5)→(-9.5,16.5)`. Fluid does the same: `pipe-to-ground (-31.5,49.5) →
(-31.5,47.5)` ducks the water riser under the coal belt (span 2 of a possible 10). He
ducked the **pipe**, not the belt, because the coal lane must stay unbroken so
back-pressure fills it to both boiler inserters, which sit on either side of the crossing.

**The corridor move is the sharper lesson.** The coal-to-plant run does **not** descend at
`x=-33.5` — that column is inside the steam engines' bounding boxes (x −34.75…−32.25). He
descends at **`x=-36.5`**, the first clear column west of the machine block, leaving
`x=-35.5` as the pole trunk. Measured spur: tap at the splitter `(-28.5,16)` → west along
`y=22.5` (x −36.5…−26.5) → south down `x=-36.5` (y 15.5…48.5, 28 belts) → east along
`y=48.5` (x −36.5…−29.5) to the boilers. **He moved the corridor clear of the obstacle
before committing.**

**Check:** `underground_pairs_complete()`, `no_belt_into_wall()`. On a missing partner,
**destroy the entrance**.
**Bot code contradicted:** `lay_belt_path` (L645): its bridge branch fires only when
`i>1 and j<=#T and (j-(i-1))<=5`; otherwise it does `gaps = gaps+1` and continues,
**leaving the already-laid belts in place**. The steam-engine block is 11 tiles tall, so
it gave up and left a **19-belt stub running south down `x=-33.5`, ending at `y=34.5`, one
tile short of the engine's top edge at 35.15** — 19 belts pointing at a wall, 12 tiles from
the boiler. Both `create_entity` calls in the underground branch are wrapped in bare
`pcall` with no partner verification, producing **3 unpaired N-facing underground inputs
stacked at `(-11.5, 10.5/11.5/12.5)` and 2 E inputs sharing 1 exit** — a sealed dead end
where coal arrived and stopped. `coal_to_boiler` still hardcodes the `x=-33.5` route;
**if it runs again it rebuilds the same dead stub.**

---

### P10 — Splitters allocate. Belts buffer. Chests only terminate.

Use a plain splitter (priority `none`, no filter) with a dead-end spur for allocation — it
self-prioritises via back-pressure and needs no servicing. A container is legal **only at
a true terminus**: finished good, end of belt, no downstream consumer built yet. Never a
relay, never a fuel buffer, never a haul target.

**The coal tree.** Two levels, both splitters pure 1-in/2-out with no priority:
**A `(-28.5,16.0)`** splits mine coal → power / smelters; **B `(-12.0,13.5)`** splits the
smelter half → iron feed / copper feed. So coal mine → ½ power → ¼ iron, ¼ copper.
*Placement trick:* a splitter is 2 tiles wide, so a half-tile offset **manufactures a
second output row out of one belt row**.

**The spur is the buffer.** The power spur is 50 tiles ending in a dead end and holds
**392 of a maximum 400 coal** — **7.3 minutes of full-plant autonomy at zero mining, 19.5
at current draw** — while the main line still reads 253 coal across 35 tiles. While the
spur is full, back-pressure sends ~100% east to the base; the instant the boilers eat, the
spur is the only side with space. **Absolute priority up to actual consumption, with no
circuit network, no priority setting and no filter.**

**Containers.** 9 wooden chests + 9 inserters removed; exactly **2 iron chests remain** —
`(28.5,3.5)` iron plate and `(20.5,12.5)` copper plate, both at the far end of a plate belt
with nothing downstream. The removed 18 were **4 complete `build_io_cell` shells with the
assembler never built**, at `y=-7.5`, pitch 8, x = 0.5 / 8.5 / 16.5 / 24.5, plus one orphan
half-cell at `(0.5,-22.5)`. Their statuses prove the hole: inserters aimed at a chest read
`waiting_for_space_in_destination`, inserters aimed at the empty assembler tile read
`waiting_for_source_items`.

```
belt_autonomy_minutes = spur_tiles * 8 / consumption_per_min
```

**Check:** `no_orphan_chest()` (also rejects a belt→chest→belt relay), `io_cell_is_atomic()`.
**Bot code contradicted:** `coal_buffer()` + `refill_buffers(0.2)` (L1072-1135) require the
**character to walk to the plant and hand-load a wooden chest** below 20%, and this is the
top `_gated()` priority in `maintain()` — the single largest source of maintenance walking.
`build_mine_outpost` (L941) always terminates a lane with `burner-inserter + wooden-chest`;
a chest is a hard stop where throughput becomes a human walking.
`mine_layout.plan_outpost` still defaults `output=("inserter","wooden-chest")`.
`coal_to_boiler` would place a *second* tee at `(-35.5,16)` — inside the ore field's south
drill band — after `destroy()`ing the belt under it.

---

### P11 — Ratios are exact. Scale by duplicating the module, never by lengthening it.

Machine ratios are hard gates — **refuse to place the orphan**. Growth adds a parallel copy
of the module at a fixed offset, keeping every run length constant.

**2 boilers : 4 engines = 3.6 MW : 3.6 MW, exact**, replicated identically in both columns,
no orphan engine and no starved boiler. Growth is `Sx' = Sx + 4` — a third column, same
table. He even **pre-laid the header for it**: 4 water-full pipes running east along
`(-30.5 … -27.5, 50.5)`, dead-ending cleanly one spine-pitch short of where a third riser
would go (the shoreline supports pumping from x=−30 to −23). Four pipes to make the next
expansion a drop-in.

**Check:** `plant_ratio_ok()` — `assert engines == 2 * boilers` as a hard gate that refuses
the placement. After any array build, assert `drills >= ceil(furnaces / 1.6)` or report the
deficit. Assert a growth step changed no existing run length.
**Bot code contradicted:** `_build_boiler_engine(n_engines=k)` (L267) stacks engines
northward on **one** boiler at 5-tile pitch. At k>2 a single 1.8 MW boiler cannot feed 3
engines, and the column walks further from its water with every addition, needing more poles.

---

### P12 — Site the plant at the fuel, not at the base — and size it to the fuel supply.

Electricity travels for the price of a pole every 7 tiles; **coal must be physically
belted**. Score candidate shore tiles by distance to the **fuel source**. Then cap plant
size so that `plant_burn + consumer_burn <= 0.8 × measured fuel/min`.

Nearest-water distances: smelter array 50.3, coal patch 34.0, **plant centre 8.2**, spawn
56.8. The lake's northernmost finger is tiny — water at y=49 exists only at x=−35,−34 — and
the plant sits directly on that tip, which is also the lake's closest approach to coal.
Distances from the plant: **coal ore 18.5**, splitter tap 25.2, smelter array 51.5, iron
mine 104.6. **He accepted a 104-tile electrical run to buy a 25-tile coal belt.**

**Sizing is a fuel budget, not a load budget.** Plant at full 3.6 MW ÷ 4 MJ/coal = 54
coal/min, plus 28 furnaces × 1.35 = 38 → **92/min demand against 120/min measured supply
(77%)**. Utilisation at snapshot time 37.3% (1342 kW of 3600) — **2.7× headroom ≈ 25 more
electric drills** — but a third boiler pair would put the system into fuel deficit. Note
the single boiler he inherited would already have been at **74.6%**: a brownout waiting for
the first assembler.

**Never route a service spur through an ore patch.** The fuel spur detours around it (east
2, south 6, west 10 ≈ 51 tiles vs a 30.4-tile straight line, 1.7×), hugging `y=22.5`, three
tiles clear of the south drill band at `y ∈ [16,18]`, and taps at `x=-28.5` — the far
downstream end of the bus, past the last drill, where the belt is fully loaded. A belt on
row 16 would have permanently blocked every future south-row drill from x=−36 to −29.

**Check:** `plant_sited_at_fuel()`. Also assert `network_capacity / peak_demand >= 2.0`
**and** `plant_burn + consumer_burn <= 0.8 * measured_fuel_per_min`; assert no service spur
enters `ore_row ± 3` of any drill band; assert the bus tap is downstream of the last drill.
**Bot code contradicted:** `power()` (L211-221) takes the **first tile** from
`find_tiles_filtered{radius=14}` with land to the north — no relation to the coal lane, the
boiler grid, or the base. It landed the pump on `(-35,48)`, exactly the tiles the coal lane
and inserter row now need, forcing the L-shaped pipe run along the boiler's **south face** —
the only face left for a fuel inserter — which is the root cause of the buried inserter in P5.

---

### P13 — Build order is supply → consumer, verified stage by stage. A consumer built early has negative value.

**Order: power → mine → trunk → smelter → buffer → consumer.** Each stage must be measured
moving material before the next starts. Never place a lab without a fed feed-chest *and* a
queued research; never an assembler without both inserters and both their sources in the
same pass.

The operator's finished base contains **0 labs, 0 assemblers**, produces only iron plate,
copper plate and coal, and **stops deliberately at "buffer"**. He deleted 2 labs and 1
assembling-machine-1. Lab 1 at `(-29.5,41.5)` (tiles x −31…−28, y 40…43) sat **exactly**
where the new steam engine at `(-29.5,42.5)` now sits — a measured spatial conflict, not an
inference. Lab 2 overlapped nothing and went anyway. Both read `missing_science_packs`;
neither had a feed chest or inserter anywhere near it. The lone assembler read `full_output`
on gears with **no inserters at all**. **Power capacity beat idle science.**

**The bot has not learned this.** Live probe 2026-08-29, after the `after` snapshot: **9
labs**, `research = NONE`, `no_research_in_progress ×9`, **21 entities
`waiting_for_target_to_be_built`**, 35 `waiting_for_source_items`, a feed-belt column at
x=−2.5…−5.5 connected to no plate source, and **109 entity ghosts**. It is re-committing the
exact mistake the operator just deleted, at scale.

**Check:** `no_consumer_ahead_of_supply()`, `io_cell_is_atomic()`. Gate a consumer placement
on `production_stats[input_item] > 0 AND a fed inserter exists within reach AND a
research/recipe is queued`. Before placing any non-producing machine, assert its footprint
is outside the expansion envelope of the power plant and every array. Treat a rising count
of `waiting_for_*` / `missing_science_packs` as a **build-order failure, not progress**.
`build_io_cell` must be **atomic**: chest, inserter, MACHINE, inserter, chest — or nothing.

---

### P14 — Measure the *binding* constraint end to end — including the drain — and tolerate ~5% residue.

After any array build, compute `min(mine, furnace, belt, drain)` and report which stage
binds. Do not report a transient fill-up number as throughput. And do not chase perfection.

**The whole factory has converged on one number: 56/min per array** — exactly one yellow
inserter (0.83 items/s ≈ 50/min). The chain:

| stage | rate | |
|---|---|---|
| drain (1 inserter → 1 chest) | **~56/min** | ← **binding** |
| mine (6 drills) | 180/min | |
| furnaces (16) | 300/min | |
| belt lane | 450/min | |

**He did not fix this.** It is the bot's original drain design, kept unexamined, and it is
now the base's only real bottleneck — 13 of 16 iron furnaces sit at `full_output`, and 14 of
16 drills read `waiting_for_space_in_destination`, which is *the definition of a correctly
saturated network*. The belts are no longer the constraint.

**Transient guard.** The `iron 174 / copper 90` in the `after` snapshot were **fill-up while
the output belts were still absorbing plates**. A live probe later the same session read
**iron 0/min, copper 53/min, coal 7/min**. Sample production over ≥2 windows and assert it
is not still rising before reporting it.

**Residue he left: 21–23 dead belts (~5%)** — 13 buried under the overlapping bot drills
(unclickable), 6 one-tile lead-ins, 2 orphan coal tiles at `(-10.5,15.5)` / `(-9.5,15.5)`;
plus one dropped `item-on-ground coal @(-35.7,46.5)`. **Even a careful human pass leaves
1–5% junk — the budget is a threshold, not zero.**

**Check:** `dead_belt_fraction_ok()` (budget 6%, set just outside the reference build's
own 5.3% so the reference passes), `metrics()['flow_coverage']`.
**Bot code contradicted:** `build_smelter_array` (L599) drains each plate belt with one
yellow inserter into one chest and nothing ever revisits that number. The bot's success
signal is entity count and `status` polling, neither of which surfaces a 5.4× drain deficit.

---

## 3. Why his base works and the bot's didn't

**The bot optimized locally and verified nothing. The operator optimized one global
objective — continuous end-to-end flow of ore, fuel and electricity through a single
connected system — and let every local decision fall out of it.**

The bot's loop is **place → check `status` → patch**. Every builder returns success on
*placement*; when something later reports `no_power` or `waiting_for_source_items`, a
self-heal drops another entity next to it and interpolates toward whatever is nearest. That
loop is locally sound at every step and globally divergent. It produces four signatures:

- **Coverage without connection.** 171 pole→machine incidences for 78 machines (mode: 3
  poles per machine), 40 poles powering nothing, 34% fully redundant — *and two electric
  networks*, with 6 drills stranded on a generator-less island. Every local check passed.
  The one global check — `electric_network_id == root` — was never run, and the failure
  margin was **0.56 tiles**.
- **Volume without a path.** 470 belts, 40% on a producer→consumer path. Three parallel iron
  lanes where one drop row exists; 19 belts pointing at a steam engine; four underground
  mouths with no exits. The bot never asked "can an item get from a drill to a furnace" — it
  asked "did `create_entity` return truthy".
- **Structure without function.** 9 chests and 9 inserters in four perfectly-formed I/O cells
  with no assembler; 2 labs with no feed; 1 assembler with no inserters. The base *looked*
  like a factory. Production was **0 / 0 / 0**.
- **Constants instead of geometry.** Pitch 2 for a 3×3 entity. `direction=4` for a mine east
  of the base. `floor(centre)` for a 3-wide boiler. Interpolated pole steps against a hard
  7.5 limit. Each is one line, each was correct where it was written, none was derived from
  the prototype it was applied to. And because `create_entity` doesn't collision-check,
  wrong geometry *succeeds silently*.

The operator's loop is **choose geometry → derive placements → assert the invariant**. He
never patched. Of 107 poles he kept **5** — because a pole layout is not a set of
independent decisions to be repaired one at a time; it is one object, and the right move on
a broken one is to replace it.

Three consequences the bot has no mechanism to reach:

1. **Compounding, not accumulating.** Every structure does two jobs. The inserter row is
   also the pole row is also the power mesh. One belt carries ore *and* fuel. One lane
   serves two drill rows. One pipe feeds two boilers. One pole collects four engines. One
   splitter allocates *and* prioritises *and* buffers. That is how the base got **13%
   smaller and infinitely more productive at the same time**.
2. **Slack designed in, in the right places.** Trunk pitch 7.0 against a 7.5 limit. Pole
   degree 4 against a 5 limit. A 5-tile buffer between the power trunk and the belt
   corridor. Four pre-laid pipes for a boiler column that doesn't exist yet. Each costs
   something today and removes a future rebuild. The bot has slack nowhere and lives
   permanently at the edge of every limit.
3. **Honest stopping.** He built producers, transport and buffers, then **stopped**. A
   consumer ahead of its supply is not progress banked, it is a liability: it occupies tiles
   the real infrastructure needs, demands pole coverage for nothing, burns plates that
   should have been drills, and emits a false green signal. The bot cannot stop, because its
   notion of progress is *things built*.

---

## 4. Using `principles.py`

```bash
python3 principles.py                     # live check, READ-ONLY RCON
python3 principles.py --snapshot after     # offline against snapshots/after.json
python3 principles.py --only no_belt_without_consumer,grid_is_single_network
python3 principles.py --json               # machine-readable
python3 test_principles.py                 # 44 offline tests (pytest not installed)
```

`check_all(world)` returns `{ok, errors, warnings, findings, by_check, by_principle,
metrics}`. Exit code is 0 only when there are no `error`-severity findings.

**For a builder, the minimum gate is:**

```python
w = principles.probe()
rep = principles.check_all(w, only={"no_belt_without_consumer", "grid_is_single_network"})
if not rep["ok"]:
    teardown_what_this_pass_built()
```

### Reference scores (2026-08-29)

| | before (bot) | after (operator) |
|---|---|---|
| entities | 713 | 619 |
| flow_coverage | **0.4021** | **0.9466** |
| dead belts | 281 | 23 |
| electric networks | **2** | **1** |
| incidences per consumer | 2.171 | 1.014 |
| consumers per pole | 0.710 | 1.072 |
| error-severity checks failing | **12** | **2** |

The operator's base fails exactly two checks — `drill_pitch_ok` and `no_entity_overlap` —
and both are **the bot's own pitch-2 iron drills and the 13 belts buried under them**, which
a human physically cannot click. That is the golden regression test in
`test_principles.py::test_golden_after_only_fails_on_inherited_drill_overlap`.
