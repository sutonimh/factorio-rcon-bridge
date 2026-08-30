#!/usr/bin/env python3
"""POLE DOCTRINE: regular lattices, straight trunks, explicit wiring, single network.

The operator relaid every pole in the base by hand (107 -> 69: 102 removed, 64 added, only
5 kept - a full relay, not a prune) and the result is not "fewer poles", it is a DIFFERENT
STRUCTURE: 3 straight trunks + 4 area lattices. 93% of his poles lie on a run of >=3
collinear poles; 72% of the bot's did. 36% of his poles (25/69) power nothing at all - they
are deliberate transmission spine - while 37% of the bot's powered nothing by ACCIDENT and
the network was still split in two (net 1 with the generators, net 405 with all six electric
drills and no generator at all, 8.06 tiles from the nearest pole against a 7.5 wire reach).

This module is the planner that produces the operator's structure instead of the bot's:

    plan_grid(area, consumers, anchor=None)   a REGULAR LATTICE that covers every consumer
    plan_trunk(from_xy, to_xy)                a straight axis-aligned run at spacing <= 7
    apply(plan, ...)                          place -> WIRE EXPLICITLY -> verify -> rollback
    audit(area)                               off-lattice / redundant / islanded findings

Four laws it exists to enforce, each measured off `snapshots/{before,after}.json` and the
live server (see OPERATOR-PRINCIPLES.md, GOTCHAS "THE OPERATOR'S DESIGN PRINCIPLES"):

  L1  A pole may NEVER sit on a belt tile, an inserter pickup/drop tile, a drill drop tile,
      or inside a machine footprint. 0 violations across all 95 live operator poles; the
      operator DELETED the two bot poles that were inside footprints ((-35,36) in a steam
      engine, (-3,15) in a copper furnace). The forbidden set is not re-derived here - it is
      exactly `belt_router.Obstacles`, whose scan already models functional reservations
      (inserter pickup/drop, drill drop, the tile a belt points into).
  L2  Every pole is a LATTICE MEMBER: one pitch and one phase per area, shared by every row,
      which is what makes the rows wire to each other for free. Poles are never placed
      "beside" a module, never interpolated by an error handler, never a diagonal staircase.
      P8: POLES MUST COME FROM A MODULE TEMPLATE, NEVER FROM AN ERROR HANDLER.
  L3  The plan is ONE connected network by construction. plan_grid never returns a split
      plan: it closes the graph while planning (on the lattice, or with a trunk run) and
      raises GridError if it cannot. apply() refuses a split plan before touching the world.
  L4  Script-placed poles do NOT reliably auto-connect (GOTCHAS 2026-08-30: two small poles
      4.0 apart, wire reach 7.5, sat on different electric_network_ids). So apply() wires
      every pair explicitly via
          p.get_wire_connector(defines.wire_connector_id.pole_copper, true).connect_to(q,false)
      and VERIFIES by comparing electric_network_id - never "placement implies connection".

Measured numbers encoded here (all from the operator's base, per-pole-tier derivations in
`pitch_for`, so nothing is hardcoded that a prototype can tell you):
    small-electric-pole  supply 2.5 (5x5 window)  wire 7.5  -> max axis hop 7
    trunk spacing        7 exactly, over 21 spans in 3 independent runs (91=13*7, 28=4*7,
                         21=3*7). Endpoints are HARD, pitch is nominal: a SHORTER final hop
                         still wires, so only EXCEEDING the pitch is a violation.
    smelter row lattice  pitch 4, pole rows = the inserter rows themselves (my-1, my+2)
    lab block lattice    pitch 4 in x and y, poles on the 1-tile seam intersections
    mine outpost         pole rows lane_y -/+ 4, pitch 6-7
    min pole separation  3.0 (operator's minimum; the bot had 8 pairs at 1.0 and 42 at 2.0)
    pole degree cap      4 of the 5 copper slots (a saturated pole cannot adopt a neighbour)

DEVIATIONS FROM THE MEASURED BASE, DELIBERATE AND DOCUMENTED:
  - The phase is chosen by fewest poles, not by the operator's anchor-at-x0-1 habit. On his
    own iron row that is 8 poles/row (cols x = 3 mod 4) instead of his 9 (x = 1 mod 4); both
    cover all 16 inserters, both are on-lattice, both stay connected. `anchor=` only breaks
    ties, it never buys extra poles.
  - "wire every pair within reach" is capped at MAX_POLE_DEGREE for REDUNDANT links. In a
    4-pitch 2-D lattice a pole has 8 neighbours within 7.5 (4 orthogonal at 4.0, 4 diagonal
    at 5.66), and a saturated pole cannot adopt a later one - which is how the bot stranded
    its lab block. So `wire_pairs` runs a SPANNING pass first (never refused, for any
    reason) and then a degree-capped redundancy pass. The goal - one electric_network_id -
    is unchanged, and it is verified, not assumed.

RCON: `plan_grid`/`plan_trunk`/`wire_pairs`/`validate`/every *_lua builder is PURE and
executes nothing. `scan(...)` and `audit(...)` are READ-ONLY (find_entities_filtered plus the
chunked `storage._pgrid` scratch string, cleared afterwards). The only writes live in
`apply()`, and they go through `buildplan.apply` so the truce / staleness / protected-tile /
rollback laws apply. NOTHING here registers a runtime event handler.
"""
import json
import math
from collections import Counter

import belt_router
import buildplan
import mine_layout
import principles
import rcon

# ----------------------------------------------------------------------------- constants
POLE_DEFAULT = "small-electric-pole"
GRID_KIND = "power_grid"                 # buildplan kind (resume() resolves verifiers by it)

TRUNK_SPACING = 7                        # measured: 21/21 operator trunk spans are exactly 7
MIN_POLE_SEP = principles.MIN_POLE_SEP   # 3.0 - the operator's measured nearest-neighbour min
MAX_POLE_DEGREE = principles.MAX_POLE_DEGREE   # 4; keep a slot free for a later neighbour
# A BUDGET, NOT AN ENGINE LIMIT - and GOTCHAS already says so: "Cap pole degree at 4 of the 5
# copper slots" is the rule, and the live-probe line right under it records the bot's own
# degree histogram "1:6 2:29 3:25 4:23 5:10 6:2". Degrees of 5 and 6 are measured there, so
# nothing in GOTCHAS needs correcting; a read-only probe of the base 2026-08-29 (95 poles,
# all on electric_network_id 535, 0 unpowered, max degree 6) only re-confirms it. The reason
# the cap exists is the one GOTCHAS gives - "a saturated pole cannot adopt a LATER neighbour"
# - so it is spent on REDUNDANT links only. The spanning pass never refuses a link for
# degree: a refused spanning link is precisely the islanded grid this module prevents.
POLE_DEGREE_SEEN = 6
CMD_LIMIT = belt_router.CMD_LIMIT        # 3500 bytes per /sc (a longer one truncates silently)
READ_CHUNK = 3000                        # chars per chunked storage read

# Statuses that mean "this consumer is not actually powered". A build that leaves one of
# these standing has not succeeded, whatever create_entity returned (Build Law 1).
UNPOWERED = ("no_power", "low_power", "not_plugged_in_electric_network")

MAX_BRIDGE_TILES = 20000                 # BFS guard: an absurd area is a caller bug, not a wait
# P4, and the single most load-bearing number here. A pole row must lie INSIDE the machine
# band or immediately beside it - never flanking the block. Every operator area lattice
# measures 0 or 1: smelter rows ARE the inserter rows (0), lab rows ARE the seam rows (0),
# mine rows are lane_y -/+ 4, one tile off the drill band (1), the plant spur pole sits in
# the engine stack itself (0). The bot flanked at rows 2/9/11/18 - 4 pole rows the operator
# deleted outright, 41 of his 102 removals. Flanking is CHEAPER (it has free ground to work
# with) so it must be forbidden outright, not merely penalised.
MAX_SERVICE_DIST = 1

LAST_WARNINGS = []       # non-fatal problems from the most recent plan_* call
LAST_INFO = {}           # {'pitch','phase','rows','poles','bridged','trunk'} of that plan


class GridError(Exception):
    """The requested grid cannot be planned legally (no covering lattice, or the plan cannot
    be made into one connected network inside `area`)."""


# ----------------------------------------------------------------------------- pole specs
def _spec(pole):
    """Prototype numbers for a pole tier. Reuses mine_layout.POLES, which is engine-probed -
    lua/fle_lib.lua's vendored table hardcodes small=4/big=30 and is simply wrong."""
    try:
        return mine_layout.POLES[pole]
    except KeyError:
        raise GridError("unknown pole %r (known: %s)"
                        % (pole, ", ".join(sorted(mine_layout.POLES))))


def centre(pole, x, y):
    """create_entity position for a pole whose top-left footprint tile is (x,y)."""
    s = _spec(pole)
    return (x + s["tw"] / 2.0, y + s["th"] / 2.0)


def wire_reach(pole, other=None):
    """Wire reach for a pole, or the LIMITING reach of a pair (mixed tiers wire at the
    shorter of the two)."""
    a = _spec(pole)["wire"]
    return a if other is None else min(a, _spec(other)["wire"])


def max_hop(pole=POLE_DEFAULT):
    """Largest INTEGER axis hop that still wires: 7 for small (reach 7.5), 9 for medium.
    The operator's largest measured hop on an axis is exactly 7."""
    return int(_spec(pole)["wire"])


def supply_span(pole, p, dim):
    """Inclusive tile range a pole at top-left tile `p` supplies along one axis.
    dim is 'tw' (x) or 'th' (y). small: (p-2, p+2) - the 5x5 window."""
    s = _spec(pole)
    c = p + s[dim] / 2.0
    return (math.floor(c - s["supply"]), math.ceil(c + s["supply"]) - 1)


def _offsets(pole, dim):
    """(lo, hi) offsets of the supply window relative to the pole's tile. Constant per tier."""
    lo, hi = supply_span(pole, 0, dim)
    return lo, hi


def covers(pole, px, py, box):
    """Does a pole at top-left tile (px,py) power a consumer whose inclusive tile box is
    (l,t,r,b)? The engine powers an entity whose bounding box OVERLAPS the supply area, so
    this is a tile-range intersection, not a centre-distance test."""
    l, t, r, b = box
    lox, hix = supply_span(pole, px, "tw")
    loy, hiy = supply_span(pole, py, "th")
    return not (r < lox or l > hix or b < loy or t > hiy)


def pitch_for(machine_pitch, machine_w=1, pole=POLE_DEFAULT, cap=None):
    """The operator's derivation: the largest multiple of the MACHINE pitch that still covers
    a continuous row, capped at the wire reach.

    A pole at tile px powers machines whose left tile falls in a window of
    `machine_w + (hi-lo)` consecutive values, so a row at machine pitch `mp` puts
    `floor(L / mp)` machines under one pole and the pole pitch is `mp * that`.

    Reproduces every measured lattice with a small pole (window 5):
        inserters   w=1 mp=2 -> L=5  -> 2 per pole -> pitch 4   (smelter rows: measured 4)
        labs        w=3 mp=4 -> L=7  -> 1 per pole -> pitch 4   (lab block:    measured 4)
        drills      w=3 mp=3 -> L=7  -> 2 per pole -> pitch 6   (mine rows:    measured 6-7)
    """
    lo, hi = _offsets(pole, "tw")
    cap = max_hop(pole) if cap is None else cap
    mp = max(1, int(machine_pitch))
    span = int(machine_w) + (hi - lo)
    return max(1, min(mp * max(1, span // mp), cap))


# ----------------------------------------------------------------------------- consumers
def consumer_box(c):
    """Normalize one consumer to an inclusive TILE box (l, t, r, b).

    Accepted forms (poles are owed to ELECTRIC CONSUMERS - a stone furnace is a burner and
    is NOT a consumer; passing one only wastes poles):
        (x, y)                         1x1 at tile (x,y)
        (l, t, r, b)                   explicit inclusive tile box
        {"bb": [l,t,r,b]}              principles.World / live-probe form (preferred)
        {"x":tx, "y":ty, "w":w,"h":h}  TOP-LEFT TILE + size
        {"x":tx, "y":ty, "name":n}     top-left tile, size from mine_layout.size_of
        {"cx":x, "cy":y, "name":n}     entity CENTRE (floats) + name
    """
    if isinstance(c, dict):
        bb = c.get("bb")
        if bb:
            l, t, r, b = (int(v) for v in bb)
            return (l, t, r, b)
        name = c.get("name") or c.get("n")
        if "cx" in c or "cy" in c:
            w, h = mine_layout.size_of(name) if name else (1, 1)
            l = math.floor(float(c["cx"]) - w / 2.0)
            t = math.floor(float(c["cy"]) - h / 2.0)
            return (l, t, l + w - 1, t + h - 1)
        w = c.get("w")
        h = c.get("h")
        if w is None or h is None:
            dw, dh = mine_layout.size_of(name) if name else (1, 1)
            w = dw if w is None else w
            h = dh if h is None else h
        l, t = int(c["x"]), int(c["y"])
        return (l, t, l + int(w) - 1, t + int(h) - 1)
    c = tuple(c)
    if len(c) == 2:
        x, y = int(c[0]), int(c[1])
        return (x, y, x, y)
    if len(c) == 4:
        return tuple(int(v) for v in c)
    raise ValueError("consumer must be (x,y), (l,t,r,b) or a dict, got %r" % (c,))


def from_entities(ents):
    """Consumer boxes from live/snapshot entity dicts (centre coords). Feeds `plan_grid`
    straight from `principles.World.powered` or from `scan(...)`."""
    out = []
    for e in ents:
        if e.get("bb"):
            out.append(consumer_box({"bb": e["bb"]}))
        else:
            out.append(consumer_box({"cx": e["x"], "cy": e["y"], "name": e.get("n")}))
    return out


# ----------------------------------------------------------------------------- geometry
def blocked_tiles(obstacles=None, consumers=(), extra=()):
    """Every tile a pole may not occupy (LAW 1).

    `obstacles` is a belt_router.Obstacles - `hard` (buildings, water, cliffs, ghosts),
    `reserved` (inserter pickup/drop, drill drop_position, the tile a belt points into) and
    `belts` (every belt/underground/splitter/pipe tile). We take the UNION: a pole is not a
    belt and may not adopt one, so belt tiles are as hard as walls here.

    Consumer footprints are added even when they are not in the scan: plan_grid runs BEFORE
    the machines exist, and a pole planned inside a footprint is exactly the mistake the
    operator deleted twice.
    """
    out = set()
    if obstacles is not None:
        out |= set(getattr(obstacles, "hard", ()) or ())
        out |= set(getattr(obstacles, "reserved", ()) or ())
        out |= set(getattr(obstacles, "belts", ()) or ())
    for c in consumers:
        l, t, r, b = consumer_box(c)
        for x in range(l, r + 1):
            for y in range(t, b + 1):
                out.add((x, y))
    out |= {(int(x), int(y)) for x, y in extra}
    return out


def obstacles_for(area, pad=6, res_pad=5):
    """READ-ONLY live scan of `area` -> belt_router.Obstacles, the input `plan_grid` wants.

    Not reimplemented here on purpose: `belt_router.scan_obstacles` already models the exact
    set LAW 1 needs - buildings/water/cliffs/ghosts as hard, plus the FUNCTIONAL
    RESERVATIONS (an inserter's pickup and drop tiles, a drill's drop_position, the tile a
    belt points into) that read free and jam the line if you build on them. The reservation
    scan is padded because the entity owning a reservation usually sits outside the box.
    """
    x1, y1, x2, y2 = (int(v) for v in area)
    return belt_router.scan_obstacles(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2),
                                      pad=pad, res_pad=res_pad)


def _fits(pole, x, y, area, blocked):
    """A pole's WHOLE footprint must be inside `area` and clear of `blocked`."""
    s = _spec(pole)
    x1, y1, x2, y2 = area
    if x < x1 or y < y1 or x + s["tw"] - 1 > x2 or y + s["th"] - 1 > y2:
        return False
    for i in range(s["tw"]):
        for j in range(s["th"]):
            if (x + i, y + j) in blocked:
                return False
    return True


def _dist(pole, a, b, other=None):
    ax, ay = centre(pole, a[0], a[1])
    bx, by = centre(other or pole, b[0], b[1])
    return math.hypot(ax - bx, ay - by)


def components(tiles, pole=POLE_DEFAULT, reach=None):
    """Connected components of the pole wire graph (adjacency = centre distance <= reach).
    Returns a list of lists of tiles, largest first."""
    tiles = [tuple(t) for t in tiles]
    reach = wire_reach(pole) if reach is None else reach
    parent = list(range(len(tiles)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(tiles)):
        for j in range(i + 1, len(tiles)):
            if _dist(pole, tiles[i], tiles[j]) <= reach + 1e-9:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b
    groups = {}
    for i, t in enumerate(tiles):
        groups.setdefault(find(i), []).append(t)
    return sorted(groups.values(), key=len, reverse=True)


def connected(tiles, pole=POLE_DEFAULT, reach=None):
    """True when every pole in the set can reach every other through wires (LAW 3)."""
    tiles = list(tiles)
    return len(tiles) <= 1 or len(components(tiles, pole, reach)) == 1


def plan_tiles(plan):
    return [(int(s["x"]), int(s["y"])) for s in plan or ()]


# ----------------------------------------------------------------------------- trunk
def plan_trunk(from_xy, to_xy, spacing=TRUNK_SPACING, pole=POLE_DEFAULT, blocked=(),
               area=None, corner=None, include_ends=True):
    """A straight, axis-aligned pole run from tile `from_xy` to tile `to_xy`.

    The operator's transmission spine: 25 of his 69 poles (36%) power nothing and exist only
    to carry the network. Every measured run is straight, axis-aligned and at spacing exactly
    7 - `x=-15` from y=-65 to 26 is 91 tiles / 14 poles (13 gaps of 7), `x=-36` 28 tiles / 5
    poles, `y=26` 21 tiles / 4 poles. Never a diagonal, never a staircase; the bot's
    equivalent was `fle_tools.connect` interpolating 2.72 tiles/hop and never arriving.

    Endpoints are HARD, the pitch is nominal: the run is anchored at both ends and filled at
    `spacing`, so the FINAL hop is short whenever the length is not a multiple of it. A
    shorter hop still wires - only EXCEEDING the spacing is a violation.

    Off-axis endpoints give TWO legs meeting at a corner (his N-S trunk x=-15 and E-W spine
    y=26 meet at (-15,26)); `corner` forces which one ('x' turns at from_x, 'y' at from_y).
    A blocked lattice point is nudged BACK along the axis (never sideways - the run stays in
    its column) so the hop only ever shrinks.

    Returns [{'x','y','entity'}]; executes nothing.
    """
    warn = []
    a = (int(from_xy[0]), int(from_xy[1]))
    b = (int(to_xy[0]), int(to_xy[1]))
    blocked = set(blocked)
    area = area or (min(a[0], b[0]) - 1, min(a[1], b[1]) - 1,
                    max(a[0], b[0]) + 1, max(a[1], b[1]) + 1)
    spacing = int(spacing)
    if spacing > max_hop(pole):
        raise GridError("trunk spacing %d exceeds the %s wire reach %.1f"
                        % (spacing, pole, wire_reach(pole)))

    if a[0] == b[0] or a[1] == b[1]:
        legs = [(a, b)]
    else:
        c1 = (a[0], b[1])          # turn at the end of the from-COLUMN
        c2 = (b[0], a[1])          # turn at the end of the from-ROW
        pick = corner
        if pick is None:
            pick = "x" if _leg_blocked(a, c1, b, blocked) <= _leg_blocked(a, c2, b, blocked) \
                   else "y"
        c = c1 if pick == "x" else c2
        legs = [(a, c), (c, b)]

    tiles = []
    for (p, q) in legs:
        run = _run(p, q, spacing, pole, blocked, area, warn)
        if run is None:
            LAST_WARNINGS[:] = warn
            raise GridError("no legal %s trunk from %s to %s: %s"
                            % (pole, p, q, "; ".join(warn) or "blocked"))
        for t in run:
            if t not in tiles:
                tiles.append(t)
    if not include_ends:
        tiles = [t for t in tiles if t != a and t != b]
    LAST_WARNINGS[:] = warn
    LAST_INFO.update({"trunk": {"legs": len(legs), "spacing": spacing, "poles": len(tiles)}})
    return [{"x": t[0], "y": t[1], "entity": pole} for t in tiles]


def _leg_blocked(a, c, b, blocked):
    """How many tiles of the two-leg route a->c->b are blocked (corner tie-break)."""
    n = 0
    for (p, q) in ((a, c), (c, b)):
        ax = 0 if p[1] == q[1] else 1
        step = 1 if q[ax] > p[ax] else -1
        for k in range(abs(q[ax] - p[ax]) + 1):
            t = list(p)
            t[ax] += step * k
            if tuple(t) in blocked:
                n += 1
    return n


def _run(a, b, spacing, pole, blocked, area, warn):
    """One straight leg: anchor both ends, fill at `spacing`, shorten the last hop."""
    if a == b:
        return [a]
    axis = 0 if a[1] == b[1] else 1
    step = 1 if b[axis] > a[axis] else -1
    out, cur = [a], a
    guard = 0
    while abs(b[axis] - cur[axis]) > spacing:
        guard += 1
        if guard > 10000:
            warn.append("trunk leg %s->%s did not converge" % (a, b))
            return None
        placed = None
        for back in range(0, spacing):        # nudge BACK toward cur: the hop only shrinks
            t = list(cur)
            t[axis] += step * (spacing - back)
            t = tuple(t)
            if t == cur:
                break
            if _fits(pole, t[0], t[1], area, blocked):
                placed = t
                break
        if placed is None:
            warn.append("trunk leg %s->%s blocked at %s+%d" % (a, b, cur, spacing))
            return None
        out.append(placed)
        cur = placed
    if b != cur:
        out.append(b)
    return out


# ----------------------------------------------------------------------------- area grid
def plan_grid(area, consumers, anchor=None, pole=POLE_DEFAULT, obstacles=None,
              pitch=None, phase=None, extra_blocked=(), trunk_spacing=TRUNK_SPACING,
              min_row_sep=None):
    """A REGULAR LATTICE of poles covering every consumer inside `area`.

    area       inclusive tile box (x1,y1,x2,y2) the poles may occupy.
    consumers  electric consumers to cover - any form `consumer_box` accepts. Burners
               (stone furnaces, burner inserters, boilers) are NOT consumers; passing them
               only buys poles that power nothing.
    anchor     an EXISTING pole's tile. Never re-placed (it is a virtual node), but the plan
               is required to REACH it: if the lattice lands out of wire range, a straight
               `plan_trunk` run joins them - the operator's own tie-in (his array lattice at
               x=-7 reaches the x=-15 trunk through 1-2 poles, not a chain).

    Structure (why this is a lattice and not a cover):
      * ONE pitch and ONE phase for the whole area, shared by every pole row. That single
        choice is what makes the rows wire to each other for nothing: poles in two rows sit
        in the same COLUMN, so the hop between them is just the row separation.
      * Rows are chosen by set cover, preferring rows that lie INSIDE or adjacent to the
        machine band (P4: service infrastructure rides inside the machine rows and doubles
        as the mesh). That reproduces the measured layouts exactly:
          - smelter: poles land in the INSERTER rows (my-1, my+2) in the machine tile the
            inserters do not use, not flanking the belts;
          - lab block: poles land on the 1-tile seam rows between lab rows;
          - mine: the drill band has no free tile, so the rows fall out to lane_y -/+ 4,
            which is exactly `mine_layout._plan_poles`' existing rule.
      * (pitch, phase) is SEARCHED, not assumed, and validated against real coverage: the
        blocked tiles decide it. On a smelter row the inserters occupy the even columns, so
        only an odd phase has free tiles, and only an even pitch keeps every pole odd - the
        search lands on pitch 4 by itself. `pitch_for` supplies the doctrine value as the
        tie-break so a tie always resolves the operator's way.

    Raises GridError when no lattice covers the consumers, or when the plan cannot be made
    into ONE connected network inside `area` (LAW 3 - never hand back a split grid).

    Returns [{'x','y','entity'}]; PURE, executes nothing.
    """
    area = tuple(int(v) for v in area)
    boxes = [consumer_box(c) for c in consumers]
    blocked = blocked_tiles(obstacles, boxes, extra_blocked)
    reach = wire_reach(pole)
    hop = max_hop(pole)
    sep = MIN_POLE_SEP if min_row_sep is None else min_row_sep
    warn = []

    if not boxes:
        LAST_WARNINGS[:] = ["no consumers given: nothing to cover"]
        LAST_INFO.clear()
        return []

    want_pitch = pitch_for(_modal(boxes, "pitch"), _modal(boxes, "width"), pole)
    pitches = [int(pitch)] if pitch else _pitch_candidates(hop, want_pitch)
    # Pass 1 allows ONLY service rows (P4). Pass 2 - flanking - is a documented fallback for
    # a block with no free interior tile at all, and it warns, because it is the shape the
    # operator deleted.
    best = None
    for limit in (MAX_SERVICE_DIST, None):
        for p in pitches:
            for ph in ([int(phase) % p] if phase is not None else range(p)):
                cand = _lay(area, boxes, blocked, pole, p, ph, anchor, sep, reach,
                            trunk_spacing, limit)
                if cand is None:
                    continue
                tiles, uncovered, bridged = cand
                key = (len(uncovered), len(tiles), 0 if p == want_pitch else 1, -p,
                       _anchor_dist(pole, tiles, anchor), ph)
                if best is None or key < best[0]:
                    best = (key, tiles, uncovered, bridged, p, ph)
        if best is not None and not best[2]:
            break
        if limit is not None:
            best = None
            warn.append("no service row (within %d of a machine band) can carry the lattice; "
                        "falling back to FLANKING rows - the shape the operator deleted"
                        % MAX_SERVICE_DIST)
    if best is None:
        raise GridError("no %s lattice fits inside area %s (every pitch/phase is blocked)"
                        % (pole, (area,)))

    _key, tiles, uncovered, bridged, p, ph = best
    if uncovered:
        warn.append("%d consumer(s) uncovered: %s%s"
                    % (len(uncovered), uncovered[:4], " ..." if len(uncovered) > 4 else ""))
    nodes = tiles + ([tuple(anchor)] if anchor else [])
    if not connected(nodes, pole, reach):
        raise GridError("lattice pitch %d phase %d is SPLIT into %d networks - refusing to "
                        "hand back a grid that cannot be one electric network"
                        % (p, ph, len(components(nodes, pole, reach))))

    LAST_WARNINGS[:] = warn
    LAST_INFO.clear()
    LAST_INFO.update({"pitch": p, "phase": ph, "poles": len(tiles),
                      "rows": sorted({t[1] for t in tiles}), "bridged": bridged,
                      "uncovered": len(uncovered), "derived_pitch": want_pitch})
    return [{"x": t[0], "y": t[1], "entity": pole} for t in tiles]


def _pitch_candidates(hop, want):
    """Pitches to try, doctrine value first, then wide-to-narrow. Below MIN_POLE_SEP a row
    would violate the operator's measured 3.0 minimum separation, so 1 and 2 are last
    resorts - offered only so a pathologically dense block still gets power."""
    lo = int(math.ceil(MIN_POLE_SEP))
    wide = [p for p in range(hop, lo - 1, -1)]
    narrow = [p for p in range(lo - 1, 0, -1)]
    order = ([want] if want in wide else []) + wide + narrow
    seen, out = set(), []
    for p in order:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _modal(boxes, what):
    """Modal machine pitch / width along x - the input to `pitch_for`."""
    if what == "width":
        c = Counter(b[2] - b[0] + 1 for b in boxes)
        return c.most_common(1)[0][0]
    xs = sorted({b[0] for b in boxes})
    gaps = [b - a for a, b in zip(xs, xs[1:]) if b > a]
    return Counter(gaps).most_common(1)[0][0] if gaps else 1


def _anchor_dist(pole, tiles, anchor):
    if not anchor or not tiles:
        return 0.0
    return round(min(_dist(pole, t, tuple(anchor)) for t in tiles), 3)


def _lay(area, boxes, blocked, pole, p, ph, anchor, sep, reach, trunk_spacing,
         service_limit=MAX_SERVICE_DIST):
    """Build one candidate lattice for a fixed (pitch, phase). -> (tiles, uncovered, bridged)."""
    xs = _lattice_xs(area, p, ph, pole)
    if not xs:
        return None
    rows = _row_options(area, boxes, blocked, pole, xs)
    if service_limit is not None:
        rows = {py: v for py, v in rows.items()
                if _row_service_dist(py, boxes) <= service_limit}
    if not rows:
        return None

    chosen, covered = _choose_rows(rows, boxes, sep)
    if len(covered) < len(boxes):
        # The separation rule is a preference, not a law: retry without it rather than leave
        # a consumer dark (an unpowered drill is the failure this module exists to prevent).
        chosen, covered = _choose_rows(rows, boxes, 0)
    uncovered = [i for i in range(len(boxes)) if i not in covered]

    tiles = []
    assigned = set()
    for py in chosen:
        free, cov = rows[py]
        mine = [i for i in cov if i not in assigned and i in covered]
        assigned |= set(mine)
        picked = _hit(mine, cov, free)
        picked = _fill_gaps(picked, free, pole, reach)
        for x in picked:
            if (x, py) not in tiles:
                tiles.append((x, py))

    bridged = _close(tiles, xs, area, blocked, pole, reach)
    if anchor:
        try:
            tiles = _reach_anchor(tiles, tuple(anchor), pole, blocked, area, reach,
                                  trunk_spacing, bridged)
        except GridError:
            return None          # this (pitch, phase) cannot reach the grid; try the next
    return tiles, uncovered, bridged


def _lattice_xs(area, pitch, phase, pole):
    x1, _y1, x2, _y2 = area
    pw = _spec(pole)["tw"]
    start = x1 + ((phase - x1) % pitch)
    return list(range(start, x2 - pw + 2, pitch))


def _row_options(area, boxes, blocked, pole, xs):
    """{row_y: (free lattice xs, {box index: [xs that cover it]})} for every row that can
    power something."""
    lo, hi = _offsets(pole, "th")
    _x1, y1, _x2, y2 = area
    ph = _spec(pole)["th"]
    cand = set()
    for (_l, t, _r, b) in boxes:
        for py in range(t - hi, b - lo + 1):
            if y1 <= py <= y2 - ph + 1:
                cand.add(py)
    out = {}
    for py in sorted(cand):
        free = [x for x in xs if _fits(pole, x, py, area, blocked)]
        if not free:
            continue
        cov = {}
        for i, box in enumerate(boxes):
            opts = [x for x in free if covers(pole, x, py, box)]
            if opts:
                cov[i] = opts
        if cov:
            out[py] = (free, cov)
    return out


def _row_service_dist(py, boxes):
    """0 when the row is INSIDE a machine band, 1 when adjacent, ... P4 in one number: the
    pole belongs in the machine rows, not flanking them."""
    best = 10 ** 6
    for (_l, t, _r, b) in boxes:
        best = min(best, 0 if t <= py <= b else min(abs(py - t), abs(py - b)))
    return best


def _choose_rows(rows, boxes, sep):
    """Greedy set cover over pole ROWS. Ties break toward rows inside/adjacent to the
    machine band, then toward rows near the ones already chosen (they wire for free)."""
    chosen, covered = [], set()
    remaining = set(range(len(boxes)))
    while remaining:
        best = None
        for py, (_free, cov) in rows.items():
            if py in chosen:
                continue
            if sep and any(abs(py - q) < sep for q in chosen):
                continue
            new = remaining & set(cov)
            if not new:
                continue
            near = min((abs(py - q) for q in chosen), default=0)
            key = (-len(new), _row_service_dist(py, boxes), near, py)
            if best is None or key < best[0]:
                best = (key, py, new)
        if best is None:
            break
        _k, py, new = best
        chosen.append(py)
        covered |= new
        remaining -= new
    return sorted(chosen), covered


def _hit(mine, cov, free):
    """Fewest lattice columns in this row that cover the boxes assigned to it. Greedy by
    rightmost option - the classic interval point cover, and exact while each box's option
    list is contiguous (it is, except where a lattice tile is blocked)."""
    picked = []
    for i in sorted(mine, key=lambda i: max(cov[i])):
        opts = cov[i]
        if any(x in picked for x in opts):
            continue
        picked.append(max(opts))
    return sorted(set(picked))


def _fill_gaps(picked, free, pole, reach):
    """Keep a row wired: where two kept poles are further apart than the wire reach (only
    possible when the lattice tiles between them were blocked) put back the lattice tiles
    that close the gap. Never adds a pole the geometry does not need."""
    if len(picked) < 2:
        return picked
    out = list(picked)
    for a, b in zip(picked, picked[1:]):
        if b - a <= reach:
            continue
        # walk the free lattice columns, taking the furthest that still wires
        cur = a
        while b - cur > reach:
            step = [x for x in free if cur < x <= cur + reach]
            if not step:
                break
            cur = max(step)
            out.append(cur)
    return sorted(set(out))


def _close(tiles, xs, area, blocked, pole, reach):
    """LAW 3 closure, ON THE LATTICE. Two rows at the same phase already wire through their
    shared columns, so this only fires where a blocked tile broke that - and then it may use
    ONLY lattice columns (x on the grid, any free row), never a free-form diagonal chain.
    Returns the tiles it had to add."""
    added = []
    for _ in range(12):
        comps = components(tiles, pole, reach)
        if len(comps) <= 1:
            return added
        path = _lattice_bridge(comps[0], comps[1], xs, area, blocked, pole, reach, tiles)
        if not path:
            return added                       # caller raises: the plan is split
        for t in path:
            if t not in tiles:
                tiles.append(t)
                added.append(t)
    return added


def _lattice_bridge(src, dst, xs, area, blocked, pole, reach, used):
    """Fewest LATTICE tiles joining two components. BFS, every hop <= wire reach.

    Neighbours are ENUMERATED from the lattice (columns within reach x the y band that the
    Pythagorean slack allows), never found by scanning the legal set - the scan is the
    quadratic that made mine_layout._bridge_path unusable on a wide area.
    """
    _x1, y1, _x2, y2 = area
    ph = _spec(pole)["th"]
    ys = range(y1, y2 - ph + 2)
    if len(xs) * len(ys) > MAX_BRIDGE_TILES:
        return None
    legal = {(x, y) for x in xs for y in ys
             if (x, y) not in used and _fits(pole, x, y, area, blocked)}
    if not legal:
        return None
    r2 = (reach + 1e-9) ** 2
    steps, seen = [], set()          # (dx, dy_max) hops that stay inside the wire circle
    for x in xs:
        for dx in (x - xs[0], xs[0] - x):
            if dx in seen or dx * dx > r2:
                continue
            seen.add(dx)
            steps.append((dx, int(math.floor(math.sqrt(max(0.0, r2 - dx * dx))))))

    def reaches(t, nodes):
        return any(_dist(pole, t, n) <= reach + 1e-9 for n in nodes)

    if any(reaches(a, dst) for a in src):
        return []
    frontier = [t for t in legal if reaches(t, src)]
    parent = dict.fromkeys(frontier)
    while frontier:
        for t in frontier:
            if reaches(t, dst):
                path = []
                while t is not None:
                    path.append(t)
                    t = parent[t]
                return list(reversed(path))
        nxt = []
        for (tx, ty) in frontier:
            for dx, dymax in steps:
                for dy in range(-dymax, dymax + 1):
                    u = (tx + dx, ty + dy)
                    if u in legal and u not in parent:
                        parent[u] = (tx, ty)
                        nxt.append(u)
        frontier = nxt
    return None


def _reach_anchor(tiles, anchor, pole, blocked, area, reach, spacing, bridged):
    """Join the lattice to an existing grid pole. Within reach: nothing to do. Otherwise a
    straight `plan_trunk` run - the operator's spine, not an opportunistic chain."""
    if not tiles:
        return tiles
    near = min(tiles, key=lambda t: _dist(pole, t, anchor))
    if _dist(pole, near, anchor) <= reach + 1e-9:
        return tiles
    box = (min(area[0], anchor[0] - 1), min(area[1], anchor[1] - 1),
           max(area[2], anchor[0] + 1), max(area[3], anchor[1] + 1))
    run = plan_trunk(near, anchor, spacing=spacing, pole=pole, blocked=blocked, area=box)
    for s in run:
        t = (s["x"], s["y"])
        if t != anchor and t not in tiles:
            tiles.append(t)
            bridged.append(t)
    return tiles


# ----------------------------------------------------------------------------- wiring
def wire_pairs(tiles, existing=(), pole=POLE_DEFAULT, reach=None,
               max_degree=MAX_POLE_DEGREE, existing_connected=True):
    """The pole pairs `apply` will wire EXPLICITLY (LAW 4).

    Two passes, because "wire every pair within reach" and the degree budget are in direct
    conflict on a 4-pitch 2-D lattice (8 neighbours within 7.5):
      1. SPANNING: shortest-first, take every pair that joins two components. These links are
         mandatory - they ARE the network - and are never refused for degree.
      2. REDUNDANCY: every remaining in-reach pair whose BOTH ends are still under
         `max_degree` (4), leaving a free slot so a later pole can still adopt a neighbour.
         The bot's lab block stranded itself exactly here: two poles 4.0 apart, both
         saturated, no slot left to bridge with.
    `existing` poles are assumed already wired to each other (they are a live network), so
    no pair between two of them is emitted.

    Pure: returns [((x1,y1),(x2,y2))], executes nothing.
    """
    tiles = [tuple(t) for t in tiles]
    existing = [tuple(t) for t in existing if tuple(t) not in tiles]
    nodes = tiles + existing
    n_new = len(tiles)
    reach = wire_reach(pole) if reach is None else reach

    cands = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if i >= n_new and j >= n_new and existing_connected:
                continue                    # both pre-existing: already one network
            d = _dist(pole, nodes[i], nodes[j])
            if d <= reach + 1e-9:
                cands.append((round(d, 6), i, j))
    cands.sort()

    parent = list(range(len(nodes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if existing_connected:
        for k in range(n_new + 1, len(nodes)):
            parent[find(k)] = find(n_new)

    deg = [0] * len(nodes)
    out = []
    for _d, i, j in cands:                  # pass 1: spanning - never refused for degree
        a, b = find(i), find(j)
        if a == b:
            continue
        parent[a] = b
        deg[i] += 1
        deg[j] += 1
        out.append((nodes[i], nodes[j]))
    taken = set(out)
    for _d, i, j in cands:                  # pass 2: redundancy, degree-capped
        if (nodes[i], nodes[j]) in taken:
            continue
        if deg[i] >= max_degree or deg[j] >= max_degree:
            continue
        deg[i] += 1
        deg[j] += 1
        out.append((nodes[i], nodes[j]))
    return out


# ----------------------------------------------------------------------------- validation
def validate(plan, consumers=(), obstacles=None, pole=POLE_DEFAULT, anchor=None,
             extra_blocked=()):
    """Pre-flight findings for a plan. Errors here are what `apply` refuses on."""
    tiles = plan_tiles(plan)
    boxes = [consumer_box(c) for c in consumers]
    blocked = blocked_tiles(obstacles, boxes, extra_blocked)
    reach = wire_reach(pole)
    out = []

    for t in tiles:
        if t in blocked:
            out.append(principles.finding(
                "pole_on_forbidden_tile", "L1",
                "pole tile %s is a belt / reservation / machine footprint" % (t,),
                centre(pole, *t)))
    seen = set()
    for t in tiles:
        if t in seen:
            out.append(principles.finding("duplicate_pole", "L2",
                                          "two poles planned on tile %s" % (t,),
                                          centre(pole, *t)))
        seen.add(t)

    for i, box in enumerate(boxes):
        if not any(covers(pole, t[0], t[1], box) for t in tiles):
            out.append(principles.finding("uncovered_consumer", "P4",
                                          "no pole powers consumer %s" % (box,),
                                          (box[0] + 0.5, box[1] + 0.5)))

    nodes = tiles + ([tuple(anchor)] if anchor else [])
    comps = components(nodes, pole, reach)
    if len(comps) > 1:
        out.append(principles.finding(
            "grid_split", "P2",
            "plan is %d electric networks, not one (largest %d poles, next %d)"
            % (len(comps), len(comps[0]), len(comps[1])),
            centre(pole, *comps[1][0])))

    for i in range(len(tiles)):
        for j in range(i + 1, len(tiles)):
            d = _dist(pole, tiles[i], tiles[j])
            if d < MIN_POLE_SEP - 1e-9:
                out.append(principles.finding(
                    "poles_too_close", "P5",
                    "poles %s and %s are %.2f apart (operator minimum %.1f)"
                    % (tiles[i], tiles[j], d, MIN_POLE_SEP),
                    centre(pole, *tiles[i]), severity="warn"))
    return out


# ----------------------------------------------------------------------------- lua builders
def place_lua(tiles, pole=POLE_DEFAULT, consume=True):
    """The /sc command(s) that WOULD place these poles. Returns strings; executes nothing.

    Mirrors belt_router.plan_to_lua: clear trees/rocks on the tile, consume the item from
    storage.derpface's inventory, `can_place_entity` as the ONLY gate (an occupied tile is
    SKIPPED, never destroyed - P5: create_entity does no collision check, so wrong geometry
    succeeds silently). Echoes one result per tile so the caller can report exactly which
    tiles were placed rather than a bare count:
        b built | a already there | c can_place refused | i no inventory | s create failed
    """
    tiles = [(int(x), int(y)) for x, y in tiles]
    if not tiles:
        return []
    s = _spec(pole)
    ox, oy = s["tw"] / 2.0, s["th"] / 2.0
    head = (
        "/sc local s=game.surfaces[1]; local f=game.forces.player;"
        "local inv=storage.derpface and storage.derpface.valid and storage.derpface.get_main_inventory();"
        "local NM='%s'; local OX,OY=%s,%s; local o={};"
        "for a,b in ([==[" % (pole, ox, oy))
    tail = (
        "]==]):gmatch('(-?%d+),(-?%d+)') do"
        " local x,y=tonumber(a),tonumber(b); local px,py=x+OX,y+OY;"
        " local e=s.find_entities_filtered{name=NM,position={px,py},radius=0.4}[1];"
        " if e then o[#o+1]=x..','..y..',a' else"
        "  for _,t in pairs(s.find_entities_filtered{area={{x+0.05,y+0.05},{x+0.95,y+0.95}},"
        "    type={'tree','simple-entity'}}) do if t.destroy then t.destroy() end end;"
        "  if s.can_place_entity{name=NM,position={px,py},force=f} then"
        + ("   local took=(inv and inv.get_item_count(NM)>0);"
           "   if took then inv.remove{name=NM,count=1} end;" if consume else
           "   local took=true;") +
        "   if took then local n=s.create_entity{name=NM,position={px,py},force=f};"
        + ("    if n then o[#o+1]=x..','..y..',b' else if inv then inv.insert{name=NM,count=1} end;"
           "     o[#o+1]=x..','..y..',s' end;" if consume else
           "    if n then o[#o+1]=x..','..y..',b' else o[#o+1]=x..','..y..',s' end;") +
        "   else o[#o+1]=x..','..y..',i' end"
        "  else o[#o+1]=x..','..y..',c' end end end;"
        "rcon.print(table.concat(o,';'))")
    return _batch(head, tail, ["%d,%d" % t for t in tiles])


def wire_lua(pairs, pole=POLE_DEFAULT):
    """The /sc command(s) that WOULD wire these pairs (LAW 4). Returns strings.

    `connect_to` returns false when the pair is ALREADY connected, which is a success, not a
    failure - so the echo separates made / already / missing rather than reporting a ratio
    that reads as broken on a re-run.
    """
    pairs = [((int(a[0]), int(a[1])), (int(b[0]), int(b[1]))) for a, b in pairs]
    if not pairs:
        return []
    s = _spec(pole)
    ox, oy = s["tw"] / 2.0, s["th"] / 2.0
    head = (
        "/sc local s=game.surfaces[1]; local W=defines.wire_connector_id.pole_copper;"
        "local OX,OY=%s,%s; local made,alr,miss=0,0,0;"
        "for a,b,c,d in ([==[" % (ox, oy))
    tail = (
        "]==]):gmatch('(-?%d+),(-?%d+),(-?%d+),(-?%d+)') do"
        " local p=s.find_entities_filtered{type='electric-pole',"
        "   position={tonumber(a)+OX,tonumber(b)+OY},radius=0.4}[1];"
        " local q=s.find_entities_filtered{type='electric-pole',"
        "   position={tonumber(c)+OX,tonumber(d)+OY},radius=0.4}[1];"
        " if p and q then"
        "  local cp=p.get_wire_connector(W,true); local cq=q.get_wire_connector(W,true);"
        "  if cp and cq then if cp.connect_to(cq,false) then made=made+1 else alr=alr+1 end"
        "  else miss=miss+1 end"
        " else miss=miss+1 end end;"
        "rcon.print(made..'/'..alr..'/'..miss)")
    return _batch(head, tail, ["%d,%d,%d,%d" % (a[0], a[1], b[0], b[1]) for a, b in pairs])


def verify_lua(area, pole=POLE_DEFAULT):
    """READ-ONLY: every pole in `area` with its electric_network_id, plus every entity whose
    status says it is not actually powered. One command; the payload comes back through the
    chunked storage._pgrid protocol."""
    x1, y1, x2, y2 = (int(v) for v in area)
    return (
        "/sc local s=game.surfaces[1]; local SN={} for k,v in pairs(defines.entity_status) do"
        " SN[v]=k end;"
        "local P,U={},{};"
        "for _,e in pairs(s.find_entities_filtered{area={{%d,%d},{%d,%d}},"
        " type='electric-pole'}) do"
        " local ok,id=pcall(function() return e.electric_network_id end);"
        " P[#P+1]={n=e.name,x=math.floor(e.position.x),y=math.floor(e.position.y),"
        "  e=(ok and id) or nil} end;"
        "for _,e in pairs(s.find_entities_filtered{area={{%d,%d},{%d,%d}},force='player'}) do"
        " local ok,st=pcall(function() return e.status end);"
        " if ok and st~=nil then local nm=SN[st];"
        "  if nm=='no_power' or nm=='low_power' or nm=='not_plugged_in_electric_network' then"
        "   U[#U+1]={n=e.name,x=math.floor(e.position.x),y=math.floor(e.position.y),s=nm}"
        "  end end end;"
        "storage._pgrid=helpers.table_to_json{poles=P,unpowered=U};"
        "rcon.print(#storage._pgrid)"
        % (x1, y1, x2 + 1, y2 + 1, x1, y1, x2 + 1, y2 + 1))


def probe_lua(area):
    """READ-ONLY area scan in `principles.World` shape - the input to `audit`."""
    x1, y1, x2, y2 = (int(v) for v in area)
    return (
        "/sc local s=game.surfaces[1]; local SN={} for k,v in pairs(defines.entity_status) do"
        " SN[v]=k end; local o={};"
        "for _,e in pairs(s.find_entities_filtered{area={{%d,%d},{%d,%d}},force='player'}) do"
        " if e.name~='character' and e.name~='entity-ghost' then"
        "  local bb=e.bounding_box;"
        "  local d={n=e.name,t=e.type,x=e.position.x,y=e.position.y,"
        "   bb={math.floor(bb.left_top.x+0.5),math.floor(bb.left_top.y+0.5),"
        "       math.ceil(bb.right_bottom.x-0.5)-1,math.ceil(bb.right_bottom.y-0.5)-1}};"
        "  local a,v;"
        "  a,v=pcall(function() return e.direction end) if a and v then d.d=v end;"
        "  a,v=pcall(function() return e.status end) if a and v~=nil then d.s=SN[v] or v end;"
        "  a,v=pcall(function() return e.electric_network_id end) if a and v then d.e=v end;"
        "  a,v=pcall(function() return e.drop_position end) if a and v then d.dp={v.x,v.y} end;"
        "  a,v=pcall(function() return e.pickup_position end) if a and v then d.pp={v.x,v.y} end;"
        "  o[#o+1]=d end end;"
        "storage._pgrid=helpers.table_to_json{ents=o}; rcon.print(#storage._pgrid)"
        % (x1, y1, x2 + 1, y2 + 1))


def _batch(head, tail, entries):
    """Split a gmatch-driven command by REAL byte length. A /sc past ~4KB truncates silently
    mid-entry, which for a placement means building something nobody planned."""
    cmds, buf = [], []
    overhead = len(head) + len(tail)
    for e in entries:
        if buf and overhead + len(";".join(buf)) + 1 + len(e) > CMD_LIMIT:
            cmds.append(head + ";".join(buf) + tail)
            buf = []
        buf.append(e)
    if buf:
        cmds.append(head + ";".join(buf) + tail)
    return cmds


def _chunked(build_lua, key="_pgrid"):
    """Run a builder command that stores JSON in storage.<key>, read it back in slices, then
    clear the scratch. (One large RCON response truncates, and rcon.print appends a newline
    to EACH response - every slice must be rstripped.)"""
    n = int((rcon.run(build_lua) or "0").strip() or "0")
    if n == 0:
        return "{}"
    parts, i = [], 1
    while i <= n:
        parts.append(rcon.run("/sc rcon.print(storage.%s:sub(%d,%d))"
                              % (key, i, i + READ_CHUNK - 1)).rstrip("\r\n"))
        i += READ_CHUNK
    rcon.run("/sc storage.%s=nil" % key)
    return "".join(parts)


def scan(area):
    """READ-ONLY live entity scan of `area` -> [entity dict] in principles.World shape."""
    cmd = probe_lua(area)
    if len(cmd) > CMD_LIMIT:
        raise ValueError("probe command is %d bytes (>%d)" % (len(cmd), CMD_LIMIT))
    return json.loads(_chunked(cmd)).get("ents", [])


def read_grid(area):
    """READ-ONLY: {'poles': [...], 'unpowered': [...]} for `area`."""
    cmd = verify_lua(area)
    if len(cmd) > CMD_LIMIT:
        raise ValueError("verify command is %d bytes (>%d)" % (len(cmd), CMD_LIMIT))
    return json.loads(_chunked(cmd))


# ----------------------------------------------------------------------------- apply
def bbox(tiles, boxes=(), pad=0):
    xs = [t[0] for t in tiles] + [b[0] for b in boxes] + [b[2] for b in boxes]
    ys = [t[1] for t in tiles] + [b[1] for b in boxes] + [b[3] for b in boxes]
    if not xs:
        raise ValueError("empty bbox")
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def make_place_fn(all_tiles, pole=POLE_DEFAULT, existing=(), consume=True, wire=True):
    """place_fn for buildplan.apply: place the missing poles, then WIRE THE WHOLE PLAN.

    Wiring covers every plan tile, not just the ones this pass placed: on a resumed or
    partial apply the poles already in the ground still have to be joined, and
    `connect_to` on an existing link is a no-op.
    """
    all_tiles = [(int(x), int(y)) for x, y in all_tiles]
    existing = [(int(x), int(y)) for x, y in existing]

    def place(_plan, tiles):
        placed, already, failed = [], [], []
        wired = {"made": 0, "already": 0, "missing": 0}
        for cmd in place_lua(tiles, pole=pole, consume=consume):
            out = (rcon.run(cmd) or "").strip()
            for part in out.replace("\n", ";").split(";"):
                part = part.strip()
                if not part:
                    continue
                bits = part.split(",")
                if len(bits) != 3:
                    continue
                t = (int(bits[0]), int(bits[1]))
                code = bits[2]
                if code == "b":
                    placed.append(t)
                elif code == "a":
                    already.append(t)
                else:
                    failed.append({"tile": t, "reason": {
                        "c": "can_place_entity refused (tile occupied)",
                        "i": "no %s in inventory" % pole,
                        "s": "create_entity returned nil"}.get(code, code)})
        if wire:
            pairs = wire_pairs(all_tiles, existing=existing, pole=pole)
            for cmd in wire_lua(pairs, pole=pole):
                # made/already/missing - `wire_lua` echoes it precisely so the caller can tell
                # "the link was already there" (a success) from "one end was not found" (a
                # silent islanding). Throwing it away leaves verify_fn as the only witness.
                out = (rcon.run(cmd) or "").strip().splitlines()
                bits = out[-1].split("/") if out else []
                if len(bits) == 3:
                    for k, b in zip(("made", "already", "missing"), bits):
                        try:
                            wired[k] += int(b)
                        except ValueError:
                            pass
        return {"placed": placed, "already": already, "failed": failed, "wired": wired}

    return place


def _inside(area, t):
    x1, y1, x2, y2 = area
    return (min(x1, x2) <= t[0] <= max(x1, x2)) and (min(y1, y2) <= t[1] <= max(y1, y2))


def tie_in(join, tiles):
    """The one EXISTING pole a plan must end up sharing a network with: the join pole nearest
    the plan. ONE is enough - the root's electric_network_id IS the whole grid's id - and a
    1x1 read-back of it is cheap, which is what makes the root check affordable at all.

    Returns None when the caller named no existing grid (a first, standalone grid).
    """
    own = {(int(x), int(y)) for x, y in tiles}
    join = [(int(x), int(y)) for x, y in (join or ()) if (int(x), int(y)) not in own]
    if not join or not own:
        return None
    return min(join, key=lambda j: min(abs(j[0] - x) + abs(j[1] - y) for x, y in own))


def make_verify_fn(all_tiles, area, pole=POLE_DEFAULT, require_powered=True, join=()):
    """verify_fn for buildplan.apply. Build Law 1: the check is functional, not "create_entity
    returned ok". Four conditions, all measured off the operator's base:
        every planned pole exists                          (69/69 in `after`)
        every pole in the area is on ONE electric_network_id (his 95 live poles: net 535)
        that id is the ROOT grid's id, when the caller named one (`join` = the anchor and any
            pre-existing poles the plan ties into)
        no entity in the area reads no_power/low_power      (his count: 0)

    The root check is not decoration - without it "one network" is trivially true of a grid
    that wired to NOTHING. A spine placed with an anchor 7 tiles past the plan's own bounding
    box comes back as N poles on one brand-new, generator-less network id and reads as a clean
    pass, which is net 405 all over again. GOTCHAS states the law directly: "After placing ANY
    electric entity, read electric_network_id and compare to the ROOT's; never 'get close' to
    a network." The anchor usually sits OUTSIDE `area` (that is why it is an anchor), so it
    gets its own 1x1 read rather than being assumed into the area scan.
    """
    want = {(int(x), int(y)) for x, y in all_tiles}
    root = tie_in(join, all_tiles)

    def verify(_plan):
        data = read_grid(area)
        have = {(int(p["x"]), int(p["y"])) for p in data.get("poles", ())}
        missing = sorted(want - have)
        nets = {p["e"] for p in data.get("poles", ()) if p.get("e") is not None}
        unpowered = data.get("unpowered", [])
        bits = []
        if missing:
            bits.append("%d planned pole(s) absent: %s" % (len(missing), missing[:6]))
        root_net = None
        if root is not None:
            if _inside(area, root):
                src = data.get("poles", ())
            else:
                src = read_grid((root[0], root[1], root[0], root[1])).get("poles", ())
            root_net = next((p["e"] for p in src
                             if (int(p["x"]), int(p["y"])) == root
                             and p.get("e") is not None), None)
            if root_net is None:
                bits.append("no powered pole at the tie-in %s: the plan is not joined to the "
                            "existing grid at all" % (root,))
            else:
                nets.add(root_net)
        snets = sorted(nets)
        if len(snets) > 1:
            bits.append("%d electric networks in the area (%s%s) - the grid is SPLIT"
                        % (len(snets), snets[:6],
                           ", root grid is %s" % root_net if root_net is not None else ""))
        if not snets and want:
            bits.append("no pole reports an electric_network_id (no generator on the grid?)")
        if require_powered and unpowered:
            bits.append("%d unpowered consumer(s): %s"
                        % (len(unpowered),
                           ["%s@%s,%s" % (u["n"], u["x"], u["y"]) for u in unpowered[:5]]))
        ok = not bits
        return ok, ("one network %s, %d poles%s" % (snets[:1], len(have),
                                                    " (root %s)" % (root,) if root else "")
                    if ok else "; ".join(bits))

    return verify


def apply(plan, consumers=(), area=None, pole=POLE_DEFAULT, existing=(), anchor=None,
          obstacles=None, tries=6, delay=5, force=False, scan_tick=None, consume=True,
          require_powered=True, rollback_on_fail=True, probe_fn=None):
    """Place a pole plan, WIRE IT EXPLICITLY, verify one network, roll back if it failed.

    Everything that touches the world goes through `buildplan.apply`, so the four laws it
    owns apply unchanged and in its order: truce (no construction while a human is
    connected) -> staleness -> operator-protected tiles -> "applying" on disk before the
    first placement. Rollback uses buildplan's default remover, which is registry-scoped to
    THIS plan's tiles and refunds the poles.

    Pre-flight: a plan that `validate` calls SPLIT or that has a pole on a forbidden tile is
    refused BEFORE anything is placed - a split grid is the exact failure this module exists
    to prevent, and finding out after 40 create_entity calls is worse than not starting.

    Returns the buildplan record (status verified | failed | planned-with-refusal).
    """
    tiles = plan_tiles(plan)
    if not tiles:
        raise ValueError("empty plan")
    boxes = [consumer_box(c) for c in consumers]
    bad = [f for f in validate(plan, consumers=consumers, obstacles=obstacles, pole=pole,
                               anchor=anchor) if f["severity"] == "error"]
    if bad:
        raise GridError("refusing to apply: %s" % "; ".join(f["msg"] for f in bad[:4]))
    if area is None:
        area = bbox(tiles, boxes, pad=int(math.ceil(_spec(pole)["supply"])) + 1)

    # The ANCHOR IS AN EXISTING POLE and must be wired to like any other (LAW 4: script-placed
    # poles do not auto-connect). Passing only `existing` here left `plan_grid(anchor=...)`
    # planning a tie-in that apply then never wired - the lattice reached the trunk and stayed
    # on its own network. `join` is the whole set the new grid has to end up sharing an id
    # with, and it drives the wire pass, the root check and the crash-resume record alike.
    join = [(int(x), int(y)) for x, y in list(existing or ())]
    if anchor and (int(anchor[0]), int(anchor[1])) not in join:
        join.append((int(anchor[0]), int(anchor[1])))

    args = {"pole": pole, "area": list(area), "anchor": list(anchor) if anchor else None,
            "join": [list(t) for t in join], "consume": bool(consume),
            "consumers": len(boxes), "pairs": len(wire_pairs(tiles, join, pole))}
    bp = buildplan.new_plan(GRID_KIND, args, tiles, names=[pole], scan_tick=scan_tick)
    return buildplan.apply(
        bp,
        place_fn=make_place_fn(tiles, pole=pole, existing=join, consume=consume),
        verify_fn=make_verify_fn(tiles, area, pole=pole, require_powered=require_powered,
                                 join=join),
        probe_fn=probe_fn,
        tries=tries, delay=delay, force=force, rollback_on_fail=rollback_on_fail)


# Registered so buildplan.resume() can re-verify a crashed grid with no caller context.
# The verifier is rebuilt from the record's own args - which is why `join` and `consume` are
# recorded there. Dropping them made a resumed pass wire the plan to ITSELF only and refund
# nothing, i.e. finish the crashed build into exactly the island the first pass avoided.
def _resume_join(a):
    return [(int(t[0]), int(t[1])) for t in (a.get("join") or ())]


def _resume_place(bp, tiles):
    a = bp.get("args") or {}
    return make_place_fn([tuple(t[:2]) for t in bp.get("tiles", ())],
                         pole=a.get("pole", POLE_DEFAULT), existing=_resume_join(a),
                         consume=bool(a.get("consume", True)))(bp, tiles)


def _resume_verify(bp):
    a = bp.get("args") or {}
    area = a.get("area")
    tiles = [tuple(t[:2]) for t in bp.get("tiles", ())]
    if not area:
        area = bbox(tiles, pad=4)
    return make_verify_fn(tiles, tuple(area), pole=a.get("pole", POLE_DEFAULT),
                          join=_resume_join(a))(bp)


buildplan.register(GRID_KIND, place=_resume_place, verify=_resume_verify)


# ----------------------------------------------------------------------------- audit
def audit(area, ents=None, pole=POLE_DEFAULT):
    """Findings for an EXISTING pole set: off-lattice, redundant, islanded.

    This is the cleanup half of the doctrine - the operator's relay is what it looks like
    when all three are zero. Measured contrast, bot -> operator:
        poles on a run of >=3 collinear poles   72% -> 93%      (off_lattice)
        poles whose coverage is fully duplicated 34% ->  0%      (redundant_pole)
        electric networks                          2 ->  1      (islanded_pole)

    `area` is an inclusive tile box; pass `ents` (principles.World-shape dicts) to audit
    offline. Live, the scan is READ-ONLY. Returns principles-style findings.
    """
    if ents is None:
        ents = scan(area)
    w = principles.World(ents)
    out = []
    out += _audit_lattice(w, pole)
    out += _audit_redundant(w, pole)
    out += _audit_islands(w, pole)
    order = {"error": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda f: (order.get(f["severity"], 3), f.get("pos") or []))


def _pole_name(p, pole=POLE_DEFAULT):
    """The tier of a live pole entity, falling back to the audit's default."""
    n = p.get("n")
    return n if n in mine_layout.POLES else pole


def _pole_tile(p, pole=POLE_DEFAULT):
    """Top-left footprint tile of a live pole. `principles._tile(centre)` is right for a 1x1
    but one short on an even footprint - a big pole centred on (10,10) occupies (9,9)..(10,10)
    - and `covers` is keyed on the top-left tile, so a big pole would be scored against a
    supply window shifted a whole tile."""
    s = _spec(_pole_name(p, pole))
    return (principles._tile(p["x"] - s["tw"] / 2.0),
            principles._tile(p["y"] - s["th"] / 2.0))


def _wire_components(poles, pole=POLE_DEFAULT):
    """Connected components of a MIXED-TIER pole set, on entity centres.

    A pair wires at the SHORTER of the two reaches, so scoring a whole base at one tier
    reports islands that do not exist: two medium poles 8.0 apart are one network (reach 9.0)
    and small-pole geometry calls them split - an ERROR that would invite a caller to "fix"
    a grid that is already correct. Returns lists of the pole dicts, largest first.
    """
    n = len(poles)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            r = wire_reach(_pole_name(poles[i], pole), _pole_name(poles[j], pole))
            d = math.hypot(poles[i]["x"] - poles[j]["x"], poles[i]["y"] - poles[j]["y"])
            if d <= r + 1e-9:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b
    groups = {}
    for i, p in enumerate(poles):
        groups.setdefault(find(i), []).append(p)
    return sorted(groups.values(), key=len, reverse=True)


def _audit_lattice(w, pole):
    """L2. A pole belongs to a straight run at a regular pitch, or it is off-lattice.

    For every row/column holding >=3 poles: take the MODAL gap as the pitch and the base that
    explains the most members as the phase; anything off that phase is an error (it is a
    one-off drop, the signature of `fle_tools.connect` interpolating a position and hoping).
    A pole on no run at all is a warning, not an error - the operator himself keeps two
    1-2 pole bridges where a lattice cannot reach.
    """
    out, on_run, viol = [], set(), {}
    for axis, _key, group in principles._collinear_groups(w.poles):
        vals = sorted((q["y"] if axis == "col" else q["x"]) for q in group)
        gaps = [round(b - a, 3) for a, b in zip(vals, vals[1:]) if b > a]
        if not gaps:
            continue
        m = sorted(Counter(gaps).most_common(), key=lambda kv: (-kv[1], kv[0]))
        pitch = m[0][0]
        if pitch <= 0:
            continue
        # Group by RESIDUE, not by a single best phase: one row may legitimately carry two
        # runs of the same pitch at different phases (two module lattices side by side).
        # A residue class needs >=3 members to count as a lattice; anything else is a
        # one-off drop - the signature of an interpolating error handler.
        res = Counter(round(v % pitch, 3) for v in vals)
        good = {r for r, c in res.items() if c >= 3}
        if not good:
            continue
        main = sorted(good, key=lambda r: (-res[r], r))[0]
        ends = (vals[0], vals[-1])
        for q in group:
            v = q["y"] if axis == "col" else q["x"]
            r = round(v % pitch, 3)
            if r in good:
                on_run.add(id(q))          # on-lattice on THIS axis: never a violation
                continue
            # P8, and it is ONE-SIDED: "pitch is nominal, endpoints are hard". A run's
            # terminal pole lands short whenever the length is not a multiple of the pitch
            # (the operator's own iron mine row is 6,7,7,7,6) and a shorter hop still wires.
            # A junction/bridge pole hangs off an end for the same reason. Only an INTERIOR
            # off-phase pole is a defect - that is the one-off drop an interpolating error
            # handler produces, and 4 of the bot's poles are exactly that.
            terminal = v in ends
            viol.setdefault(id(q), (q, principles.finding(
                "off_lattice_pole", "L2",
                ("pole hangs off the end of the %s lattice (pitch %g, phase %g) - a "
                 "shortened terminal hop or a junction pole" if terminal else
                 "pole is off the %s lattice (pitch %g, phase %g): %g sits %g past a "
                 "lattice point")
                % ((axis, pitch, main) if terminal
                   else (axis, pitch, main, v, round((r - main) % pitch, 3))),
                (q["x"], q["y"]), severity="warn" if terminal else "error")))
    for pid, (_q, f) in viol.items():
        if pid not in on_run:              # a pole may be a row outlier but a column member
            out.append(f)
    for q in w.poles:
        if id(q) not in on_run and id(q) not in viol:
            out.append(principles.finding(
                "off_lattice_pole", "L2",
                "pole lies on no run of 3+ collinear poles at a regular pitch",
                (q["x"], q["y"]), severity="warn"))
    return out


def _audit_redundant(w, pole):
    """P4. A pole that costs a slot and buys nothing.

    Two shapes, both measured on the bot's base and both absent from the operator's:
      * coverage duplicated - every consumer it powers is already powered by another pole
        (36 of the bot's 107, 34%);
      * powers nothing at all and is not carrying a trunk hop (40 of 107, 37%).
    Both are suppressed when the pole is a CUT VERTEX: inside a block the service poles ARE
    the network, so a pole that looks redundant for COVERAGE is often load-bearing for
    CONNECTIVITY - deleting those is exactly what browned out the base once
    (GOTCHAS "never delete connector poles").
    """
    out = []
    trunk = principles._trunk_poles(w)
    cover = {}
    for p in w.poles:
        nm = _pole_name(p, pole)
        t = _pole_tile(p, pole)
        cover[id(p)] = {id(m) for m in w.powered
                        if covers(nm, t[0], t[1], tuple(m["bb"]))}
    for p in w.poles:
        mine = cover[id(p)]
        if id(p) in trunk or principles._is_cut_vertex(w, p):
            continue
        if not mine:
            out.append(principles.finding(
                "redundant_pole", "P4",
                "pole powers nothing, is not on a trunk run, and removing it would not "
                "split the network", (p["x"], p["y"]), severity="warn"))
            continue
        others = set()
        for q in w.poles:
            if q is not p:
                others |= cover[id(q)]
        if mine <= others:
            out.append(principles.finding(
                "redundant_pole", "P4",
                "coverage duplicated: all %d consumer(s) this pole powers are already "
                "powered elsewhere" % len(mine), (p["x"], p["y"])))
    return out


def _audit_islands(w, pole):
    """P2. ONE electric network. The bot's base had two - net 1 with both engines and net
    405 with all six electric drills and no generator at all, 8.06 tiles from the nearest
    pole against a 7.5 wire reach, after a 19-pole chain had been laid ~52 tiles toward it.

    Two readings, and they are NOT equal in authority: the live electric_network_id is the
    engine's own answer and outranks our wire model. Geometry is the offline stand-in, used
    where no id was probed - and it is only ever allowed to raise an error about a pole the
    live ids do not already put on the main grid. Otherwise a pole linked through a neighbour
    just outside the scanned box, or through a tier this model mis-scored, reads as an island
    and invites a caller to "repair" a grid the engine says is fine.
    """
    out = []
    if not w.poles:
        return out
    ids = [p.get("e") for p in w.poles if p.get("e") is not None]
    main = Counter(ids).most_common(1)[0][0] if ids else None
    if main is not None:
        for p in w.poles:
            if p.get("e") is not None and p["e"] != main:
                out.append(principles.finding(
                    "islanded_pole", "P2",
                    "pole is on electric_network_id %s, the base grid is %s"
                    % (p["e"], main), (p["x"], p["y"])))
    comps = _wire_components(w.poles, pole)
    if len(comps) > 1:
        for c in comps[1:]:
            for p in c:
                if main is not None and p.get("e") == main:
                    continue                   # the engine says it is on the grid; it is
                out.append(principles.finding(
                    "islanded_pole", "P2",
                    "pole is wire-isolated from the main grid (%d poles in its island, "
                    "%d in the main one)" % (len(c), len(comps[0])),
                    (p["x"], p["y"])))
    return out


# ----------------------------------------------------------------------------- cli
def _fmt(plan):
    return "\n".join("  %4d,%-4d %s" % (s["x"], s["y"], s["entity"]) for s in plan)


def _main(argv):
    if len(argv) >= 6 and argv[1] == "trunk":
        a = (int(argv[2]), int(argv[3]))
        b = (int(argv[4]), int(argv[5]))
        plan = plan_trunk(a, b)
        print("%d poles, spacing<=%d\n%s" % (len(plan), TRUNK_SPACING, _fmt(plan)))
        print("\n-- %d command(s) that WOULD build it (NOT executed)"
              % len(place_lua(plan_tiles(plan))))
        return 0
    if len(argv) >= 6 and argv[1] == "audit":
        area = tuple(int(v) for v in argv[2:6])
        rep = audit(area)
        if not rep:
            print("no findings in %s" % (area,))
            return 0
        for f in rep:
            print("[%-5s] %-18s %-3s %s %s"
                  % (f["severity"], f["check"], f["principle"], f.get("pos"), f["msg"]))
        return 1
    print(__doc__.strip().splitlines()[0])
    print("usage: power_planner.py audit <x1> <y1> <x2> <y2>     (READ-ONLY)")
    print("       power_planner.py trunk <x1> <y1> <x2> <y2>     (plans only, no writes)")
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv))
