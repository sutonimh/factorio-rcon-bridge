# Routing red science from the assembler module to the lab array

Everything here is measured on the live map, not assumed. Coordinates are tile positions.

---

## Why this is needed

The module makes **25 automation-science-packs/min** and delivers them nowhere. 157 packs are
standing on its internal belts, and research has not advanced all session. The module's output
line terminates at (37,4) pointing north into empty ground, so packs back up until the whole
block stalls on its own output — the same failure the bus had before it got a consumer.

The 10 real labs sit idle at `missing_science_packs`.

## The two endpoints, measured

### Output — where packs actually leave the module

Packs travel west along y=11, north up the x=37 column, and through an underground pair:

```
x=37 column      y=7,8,9   transport-belt d0 (north), 8 packs each
                 y=6       underground-belt d0  (input)
                 y=4       underground-belt d0  (output)   <- packs surface here
                 y=3       FREE                            <- the take-off tile
```

**Take-off point: (37,3).** The belt ends there and points north at nothing.

### Input — what the lab array expects

The labs are **chained by inserters**, not belt-fed. Every inserter picks from a tile that is
currently empty:

```
(2,33) pick(1,33) drop(3,33)      (2,35) pick(1,35) drop(3,35)
(6,33) pick(5,33) drop(7,33)      (6,35) pick(5,35) drop(7,35)
(10,33) pick(9,33) drop(11,33)    (10,35) pick(9,35) drop(11,35)
   ... (1,33) (5,33) (9,33) (1,35) (5,35) (9,35) are ALL FREE
```

And the 108-ghost reservation contains `lab:25  small-electric-pole:26  inserter:57` and
**no belt ghosts at all** — the reserved expansion does not include a feed belt either.

So the array has no input belt, and never had one planned. That is the real gap.

## The route

`belt_router` finds a legal path from the take-off to the array's east side:

```
(37,3) -> (26,33)   40 steps: 36 transport-belt + 4 underground halves
path: 37,3  36,3..36,9  35,9 34,9 33,9  [under 32]  30,9  30,10..30,15
      29,15 28,15 27,15 26,15  26,16 [under 17] 26,18 26,19 ... 26,33
```

It threads west of the module, tunnels under bus lane 32 at x=31–32, drops down the clear x=30
column, then runs south at x=26 just outside the reservation. Reservation tiles touched: **none**
— the router treats ghosts as hard by default.

Goals further west are refused: `(25,36)` and `(25,40)` both report *goal is blocked*, because
that is reservation ground.

## The open question — and it is yours, not mine

**How do the packs get from the array's east edge to the lab chain's head at x=1?**

The reservation occupies **x = −2..24, y = 30..50** and the feed tiles are at x=1,5,9 — deep
inside it. Three options, and they differ in ways only you can settle:

1. **Belt through the reservation.** Shortest and simplest, but lays belt across ~24 tiles of
   ground you reserved for the 36-lab expansion. It would need to be part of the array design
   rather than cut across it.
2. **Feed the chain from its east end instead.** If the inserter chain runs east-to-west, the
   head may be reachable from x=25 without entering the reservation at all. This depends on
   the chain's direction, which I can determine — but where the chain *should* be fed is a
   design decision about your array.
3. **Extend the array's own design to include a feed belt.** The reservation has 57 inserter
   ghosts and no belt ghosts; adding the missing belt column to the blueprint is arguably the
   correct fix, and then the delivery route just terminates at its head.

I have not guessed between these. The last three times I improvised on a layout of yours it
cost a rebuild.

## Build order, once the endpoint is settled

Each phase leaves the base working and is verified before the next.

1. **Take-off** — belt at (37,3) so packs leave the module. *Verify: the 157-pack backlog on
   the module's internal belts starts falling.*
2. **Trunk** — the 40-step route to (26,33). Laid with `belt_router`, not by hand.
   *Verify: continuity end to end (`area` query, not `position=` — splitters sit on tile edges),
   and packs arriving at (26,33).*
3. **Delivery** — whichever option above you pick, ending at the chain head.
   *Verify: labs leave `missing_science_packs` and research % advances.*

## Checks this plan already passes

- **Reservation**: route touches zero ghost tiles; `reserved_tiles()` clean over the corridor.
- **Ore patches**: none in the corridor.
- **Continuity**: the take-off tile is the module's genuine terminus, confirmed by walking the
  x=37 column tile by tile rather than inferring from contents.
- **Belt roles**: the take-off carries `automation-science-pack` and nothing else, so this is an
  output, not an input — the mistake that wired the bus to two smelter INPUT belts earlier.

## What this does not fix

The module is fed by a single input belt carrying iron on one lane and copper on the other, and
five of its ten science assemblers currently sit at `item_ingredient_shortage`. Draining the
output will help — a blocked output starves a machine as surely as a missing input — but the
input side is worth a separate look once packs are flowing.
