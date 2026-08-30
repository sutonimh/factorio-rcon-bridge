# Routing red science from the assembler module to the lab array

Everything here is measured on the live map. Coordinates are tile positions unless a bounding
box is quoted.

---

## Why this is needed

The module makes **25 automation-science-packs/min** and delivers them nowhere. Packs are
standing on its internal belts, and research has not advanced all session. The module's output
line terminates pointing north into empty ground, so it will eventually stall on its own output
— the same failure the bus had before it got a consumer.

The 10 real labs sit idle at `missing_science_packs`.

## What the lab array actually is

Not a belt-fed array. It is a **lab-to-lab distribution grid**: each lab hands packs to its east
neighbour and to the lab in the row below, via inserters between them.

```
y=30  pole row      -2  2  6  10  14  18  22        small-electric-pole
y=31  inserters          2  6  10  14  18  22
y=32  LAB row         0     4     8     12  16  20  24     (all ghosts)
y=33  inserters          2  6  10  14  18  22
y=34  pole row / lab-to-lab inserters
y=35  inserters          2  6  10  14  18  22
y=36  LAB row         0     4     8   built  |  12 16 20 24  ghosts
   ... repeating at y=40 and y=44
```

Verified facings: `(18,33)` picks lab (16,32) and drops into lab (20,32) — eastward.
`(14,33)` picks lab (16,32) and drops into lab (16,36) — downward. So packs flood east and
south from a single corner.

**This is why the reservation has no belt ghosts, and that is correct.** The array does not want
a distribution belt. It wants *one injection point*. My earlier note calling the missing feed
belt "the real gap" had that backwards.

**The injection point is the head of the built grid: the lab at position (0.5, 36.5)**
(box `x −0.7..1.7, y 35.3..37.7`, tile footprint x=−1..1, y=35..37). From there the existing
real inserters at (2,36) and (2,37) carry packs east to (4.5,36.5), and (−1,34)/(0,34)/(1,34)
carry them north into (0,32) once that ghost is built.

## The delivery geometry

The lab's west-adjacent tile column, x=−2 at y=35,36,37, is **entirely free**.

```
delivery inserter   tile (-2,35)   picks (-3,35)   drops (-0.5,35.5) = the head lab
                    can_place_entity -> true
source tile         (-3,35)        FREE
```

`(-2,36)` would work equally well geometrically, but its pickup tile `(-3,36)` is occupied by
the dead stub below, so `(-2,35)` is the one to use.

## The trunk

Take-off is **(36,3)**, not (37,3): x=37 is the module's own live output column, and a route
starting on it plans straight back down through the loaded belts.

```
(36,3) -> (-3,35)   70 steps: 66 transport-belt + 4 underground halves
36,3  36,4..36,9  35,9 34,9 33,9  [under 32]  30,9  30,10..30,15
29,15 28,15 27,15 26,15 25,15 24,15 23,15 22,15 21,15 20,15 19,15 18,15
18,16 [under 17] 18,18..18,29
17,29 16,29 ... 0,29 -1,29 -2,29 -3,29
-3,30 -3,31 -3,32 -3,33 -3,34 -3,35
```

It runs west of the module, tunnels under bus lane 32, drops down the clear x=30 column, west
along y=15, south down x=18, then west along **y=29 — the free row immediately north of the
array's y=30 pole row** — and finally south down x=−3 outside the array's west edge.

Checked against a real obstacle scan (734 hard, 49 reserved, 365 belt tiles):
**steps landing on hard tiles: none.**

### One detail that must not be missed

The router ends the last belt at (-3,35) facing **south (d8)**, which would feed the dead
underground at (-3,36) and packs would disappear into it. The terminal belt must be forced
**east (d4)** so packs pile at the end for the inserter to take. `plan_route` takes `goal_dir`
for exactly this.

## A correction to the route I reported earlier

The first route I gave — "(37,3) → (26,33), 40 steps, touches zero reservation tiles" — was
computed **without an obstacle set**. `plan_route(start, goal)` defaults to `obstacles=None`,
which plans over empty ground; the correct call is `scan_obstacles(...)` first, as the module's
own CLI does. That earlier path ran straight through the module's live output belts and through
the head lab's tiles. It was not validated. The 70-step route above is.

## Dead infrastructure found on the way

West of the array, x=−7..−2, y=35..48:

```
21 belt tiles holding 0 items
7 inserters, 7 of them dropping onto BARE GROUND
```

Connected to nothing at either end and moving nothing. This is the "never build anything unless
it actually does something" rule, and it should be removed — but it is 28 entities and removal
is a write, so it is listed here for a decision rather than done.

## Build order

Each phase leaves the base working and is verified before the next.

1. **Take-off** — belt at (36,3), fed from the module terminus at (37,3).
   *Verify: the pack backlog on the module's internal belts starts falling.*
2. **Trunk** — the 70-step route to (-3,35), laid via `plan_route` **with `scan_obstacles`** and
   `goal_dir=4`. *Verify: continuity end to end with an `area` query, not `position=` probes —
   splitters sit on tile edges and radius-0.4 probes miss them — then packs arriving at (-3,35).*
3. **Delivery** — inserter at (-2,35) picking west, dropping into the head lab.
   *Verify: labs leave `missing_science_packs` and research % advances.*

Continuity before contents, at every step: contents tell you about competition, continuity tells
you whether delivery is possible at all.

## What this does not fix

The module is fed by a single input belt carrying iron on one lane and copper on the other, and
several of its ten science assemblers have been sitting at `item_ingredient_shortage`. Draining
a blocked output starves a machine as surely as a missing input, so this will help — but the
input side deserves its own look once packs are flowing.
