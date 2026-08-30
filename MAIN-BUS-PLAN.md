# MAIN BUS + SCIENCE ARRAY — plan of record

Seth, 2026-08-30: *"plan out building a main bus for the iron and copper plates and an
assembler array that the bus feeds into. plan the assembler array to start by producing red
science but leave room to scale it up to make green science."*
And: *"we should directly route the outputs of the copper and iron smelter stacks to the main
bus but have an inserter that pulls from both belts and loads a chest with plates so derpface
can pull from the chest for manual building."*

Everything below is measured against the live map, not assumed. Coordinates are tile positions.

---

## 1. Why this exists

The base currently has no bus and no plate consumer worth the name. Both terminal chests fill
to 3200, all 28 furnaces jam at `full_output`, every drill upstream blocks, and plate flow
reads 0/min — while the triage loop reads those symptoms as "ore lane broken" and rewrites
belts. The single assembling-machine-1 built on 2026-08-30 caps at 6 packs/min and drains
~6 copper + ~12 iron per minute against 28 furnaces. It broke the deadlock; it does not
solve the throughput problem. **A bus with real consumers on it is the fix.**

## 2. Measured starting state

| thing | where |
|---|---|
| iron furnace row | 16 furnaces, **y = 6** |
| copper furnace row | 12 furnaces, **y = 15** |
| iron plate terminal chest | (28, 3), full |
| copper plate terminal chest | (20, 12), full |
| labs (9) | x = 0, 4, 8 × y = 36, 40, 44 |
| red-science pair (2026-08-30) | gear asm (28,6), science asm (28,10), output chest (31,10) |
| surplus depot | 6 iron chests, (2–4, 20–21) — see `depot-manifest.json` |
| power | 2 boiler columns, 3.6 MW installed, ~405 kW drawn, ONE network (id 535) |

Clear corridor width, x = −14..34 (`can_place_entity`, no ore, per row):

```
y=18..19  42 wide from x=-7
y=22..32  49 wide from x=-14      <- the open band; the array goes here
y=33..46  24 wide from x=11       <- labs occupy x=0..8, so the bus passes EAST of them
```

## 3. The bus — SITED BY `bus_planner`, not by eye

**x = 32..35, running y = 6 -> 44, four lanes, north to south.**

The first version of this section specified x = 14..17 and was wrong: it was chosen by
eyeballing the widest clear run per row, which cannot see that ground is CLAIMED and cannot
see whether a source can actually get there. Built, it destroyed 21 ghosts of the reserved
36-lab array. The site above is what `bus_planner.choose()` returns on the live world, and it
is reproducible - re-run it and it will say the same thing, with reasons:

```
CHOSE Corridor(v pos=32 6..44 lanes=4) score=-144.34
  feed: iron-plate 7 tiles, copper-plate 24 tiles | nearest sink 26 tiles
  margin 4.0 lanes | array room 627 tiles | reservation tiles touched: 0

REJECTED Corridor(v pos=14 18..46): crosses 53 RESERVED tiles
  (a blueprint ghost claims that ground, e.g. (14,30) (14,31) (14,32))
```

It sits east of the lab reservation (measured x = -2..25, y = 30..50), which is the constraint
that decides this map. Iron is 7 tiles from the head; copper is 24, which is the real cost of
the choice and is stated rather than hidden - the copper row ends at x = 17 and everything
nearer the reservation either collides with the standing base or cannot be reached.

Margin is 4 clear lanes, and there are 627 tiles of adjacent room for the assembler array.

### Feeding it — plates go to the BUS, not to a chest

Per the standing rule, the smelter rows output *directly* onto the bus:

- **Iron**, y = 6 → belt east along the existing output row, then south down x = 14..15
  from y ≈ 8 to the bus head at y = 18.
- **Copper**, y = 15 → belt east to x = 16..17, then south into the bus head.

The two existing terminal chests at (28,3) and (20,12) stay where they are as **overflow
buffers**, not as the path. They are already full; once the bus drains the rows they will
stop being the destination and become what they should have been — a cushion.

### The build-material tap (Seth's design)

One inserter per plate lane pulling into a **shared chest**, so derpface always has plates in
reach for manual building without ever standing in the bus's way:

```
iron lane  (15, 24) --inserter(15,25)--> chest (15,26) <--inserter(17,25)-- copper lane (17,24)
```

Placed at y ≈ 25, mid-corridor and next to the depot at (2–4, 20–21). This is the one
sanctioned chest in the plate path: it *takes from* the bus and never sits *in* it, so it
cannot back-pressure the lanes the way the terminal chests did.

## 4. The assembler array

East of the bus, in the open band, tiling **southward** so each new module is a copy placed
one module-height further down:

```
RED   (automation science)   20 x 13   modules at x = 19..38, y = 22, 35, 48, ...
GREEN (logistic science)     15 x 21   reserved  x = 19..33, y = 62 onward
```

Start with **one red module** and leave the rest as marked-out ground. Red is fed from its
WEST edge (9 belts + 2 undergrounds on that edge), which is the bus side — so the module
drops in with taps straight off the bus, no re-routing.

Green is deliberately *not* built yet: `logistic-science-pack` is researched, but green needs
a steady gear + inserter supply the base does not have while the smelters are still jammed.
The ground is reserved so adding it later is a stamp, not a rebuild.

**Packs to the labs.** The labs sit at x = 0..8, y = 36..44, west of the bus. Module output
(EAST edge) returns along y ≈ 34 and runs west into the lab array. Until that belt exists the
packs accumulate in a chest — which drains plates and unjams furnaces, but does **not**
advance research, so the lab feed is part of phase 3 below and not an optional extra.

## 5. Blueprints — saved to the library

| library name | source | notes |
|---|---|---|
| `tileable-science-early-mid` | [factorio.school](https://www.factorio.school/view/-KnQ865j-qQ21WoUPbd3) | whole book, migrated 0.17 → 2.0 |
| `tileable-science--automation-science-1-5-s` | ↑ child | red, 104 ent, 20×13 |
| `tileable-science--logistic-science-1-5-s` | ↑ child | green, 147 ent, 15×21 |
| **`bootstrap-red-science`** | ↑ tweaked | **build this** — `fast-inserter → inserter` ×3 |
| **`bootstrap-green-science`** | ↑ tweaked | later — `fast-inserter → inserter` ×10 |
| `mainbus-splitters` | [factorio.school](https://www.factorio.school/view/-Kzd-fbMeZaBtIuz7D7R) | book of 27 tap patterns, both directions |
| `mainbus-4lane-t-junction` | [factorio.school](https://www.factorio.school/view/-OC4gl7J2NQqgr0h1JDv) | 141 ent, 4-lane T with balancer |
| `raynquist-balancers-fall2025` | already in library | lane balancers |

### The tweaks, and why

Both science modules use `fast-inserter`, which is **not researched here** — 3 in red, 10 in
green. Stamped as-is they would leave inserter-shaped holes at exactly the points that move
ingredients, i.e. the module would look built and do nothing. `bplib.tier_downgrade` steps
them to plain `inserter`; everything else in both prints (`assembling-machine-2`,
`long-handed-inserter`, `underground-belt`, `splitter`) is already researched, so nothing
else moves. Verified: **zero un-buildable entities remain** in either variant.

Rate check: the print is labelled 1.5/s = 90 packs/min, and it is a *tileable* design — one
module is the unit, and we build one. Current output is 6/min from a single
assembling-machine-1, so one red module is ~15× the present rate and roughly matches what 28
furnaces can actually feed.

## 6. Build order

Each phase must leave the base working; none of them may be half-built.

1. **Bus spine** — 4 empty lanes, x = 14..17, y = 18..46. Belts only, 0 kW, nothing consumed.
2. **Smelter → bus** — iron row (y=6) and copper row (y=15) routed onto their lanes.
   *Verify: both terminal chests stop rising and furnaces leave `full_output`.*
3. **Build-material tap** — two inserters + chest at (15..17, 25).
4. **One red module** — stamp `bootstrap-red-science` at (19, 22), tap 4 lanes off the bus.
   *Verify: `automation-science-pack` flow > 0 and plate chests falling.*
5. **Lab feed** — pack belt from the module's east edge back west to the labs at y ≈ 34.
   *Verify: labs leave `missing_science_packs` and research advances.*
6. **Retire the 2026-08-30 stopgap** — the hand-built asm pair at (28,6)/(28,10) is superseded
   once the module runs. Remove it and return the parts to the depot.

## 7. Power — check before phase 4

One red module is 11 × assembling-machine-2. At 150 kW each that is **1.65 MW** on a plant with
3.6 MW installed and ~0.4 MW drawn. Nominal headroom is fine, but `power_headroom` gates on
*nominal* load and will refuse — see the open question in GOTCHAS about nominal-vs-measured
load. Expect to add a boiler column before or with phase 4, and the coal to fuel it.

## 8. Open items

- The bus corridor was measured to x = 34 only; x = 35..38 for the red module's east edge is
  unverified ground and must be probed before stamping.
- The exact belt route from the iron row (y=6) down to the bus head is not yet drawn — the
  area between y = 8 and y = 17 is congested with the existing lanes and the 2026-08-30 build.
- Whether `power_headroom` blocks phase 4 depends on the nominal-load question, still open.
