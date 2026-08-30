#!/usr/bin/env python3
"""Steam-plant LAYOUT PLANNER: the operator's plant as a reusable, SCALABLE template.

Measured, not invented. Every offset here was read off the live 2.1.17 server (read-only)
and cross-checked against snapshots/{before,after}.json. The reference realisation is the
operator's own plant, N=2 columns, pump land tile (-32,51):

    boiler       (-33.5,46.0) (-29.5,46.0)  d0     3x2, steam exits NORTH
    steam-engine (-33.5,42.5) (-29.5,42.5)  d0     3x5, abuts the boiler
    steam-engine (-33.5,37.5) (-29.5,37.5)  d0     3x5, abuts engine 1
    burner-ins   (-33.5,47.5) (-29.5,47.5)  d8     picks coal S, drops N into the boiler
    pipe (tap)   (-31.5,46.5)               d0     ONE pipe feeds BOTH boilers
    pipe-to-grd  (-31.5,47.5) d0 / (-31.5,49.5) d8 ducks water UNDER the coal belt
    pipe (manif) (-31.5..-27.5, 50.5)       d0
    offshore-pmp (-31.5,51.5)               d8     d = the WATER side; output is opposite
    belt (coal)  (-36.5..-29.5, 48.5)       d4     dead-ends at the last boiler, no chest
    pole trunk   (-35.5, 40.5-7m)                  straight column, pitch 7
    pole spur    (-31.5, 40.5)                     one per GAP column, covers 4 engines

WHY THE 4-TILE COLUMN PITCH EXISTS (the load-bearing insight, §2 of the measured spec):
a 3-wide boiler at pitch 4 leaves exactly ONE free tile between columns, and that tile is
simultaneously boiler k's EAST water port and boiler k+1's WEST water port - so one pipe
feeds two boilers, and because the boiler's water fluidbox is a single through-flowing box
with an opening at each end, water then CHAINS boiler -> gap pipe -> boiler down the whole
row. ONE riser hydraulically supplies all N columns. That same gap column also carries the
pipe-to-ground pair that ducks the water under the coal belt, and the spur pole. Three
systems, one column: that is the whole design.

WHAT THIS SUPERSEDES: bootstrap._build_boiler_engine (a single non-scalable column with a
west-side surface pipe run that occupies the two rows the scalable design needs) and
bootstrap.coal_to_boiler (splitter ON the ore patch, spur descending INTO the engine
footprint, and a dead inserter whose pickup_position was a pipe tile). Nothing here edits
those; a caller migrates by planning here instead.

CONVENTIONS (identical to mine_layout, deliberately):
  - `entities` carry x,y = the entity's TOP-LEFT footprint TILE. Centres are re-derived per
    prototype at emit time, never carried, so a tier/size change can never inherit a stale
    centre (the 2026-08-30 drop-tile failure).
  - directions are 16-way: N=0, E=4, S=8, W=12.
  - to_orders() -> executor place orders; to_ghosts() -> autopilot.stamp_blueprint shape.

BUILDPLAN KEYING (subtle, and safety-critical): the tiles handed to buildplan are each
entity's KEY TILE = floor(centre), NOT its top-left tile and NOT its whole footprint.
world.scan_tiles reports a hit at floor(entity.position), so buildplan.probe() and
buildplan._default_remove() only line up with a plan whose tiles are floored centres. Key
by top-left instead and a 3x2 boiler probes 1.58 tiles off its own centre: every re-apply
would double-place it and every rollback would report it removed while it stood. One tile
per entity also keeps the built/protected ledger self-consistent (we record the same tile
we placed at), so reconcile_removals can still tell an operator deletion from our own.

POLE WIRING (2026-08-30 gotcha, and the reason this module places poles at all): script
placed poles do NOT reliably auto-connect - two small poles 4.0 tiles apart, well inside
the 7.5 wire reach, sat on different electric_network_ids. 4.0 is EXACTLY this template's
spur pitch, so the plant is the worst case for that bug. validate()'s _components() check
is a PLANNING-graph check (are the poles within reach of each other) and proves nothing
about the live grid. So place() calls wire_poles() after every placement pass, which issues
get_wire_connector(pole_copper).connect_to() for every planned pair and then VERIFIES by
reading electric_network_id back; verify() FAILS (not warns) if the plant's poles and
engines still span more than one network. build() re-wires before each verify attempt, so
the retry loop is a repair loop and only a persistent split rolls the plant back.

RCON: pure except scan_shore(), read_state() and pole_networks(), which are READ ONLY, and
the four lazily-imported WRITE wrappers at the bottom (_place_entity / _clear_area /
_fuel_boilers / _wire_poles) which are only ever reached through build() or place().
Offline tests monkeypatch those four.
"""
import math
import re

import buildplan
import mine_layout
import rcon
import world

N, E, S, W = mine_layout.N, mine_layout.E, mine_layout.S, mine_layout.W

KIND = "power_plant"

# --------------------------------------------------------------------------- prototypes
# Engine-probed 2.1.17 (read-only). Boilers and engines are placed at d0 ONLY (steam exits
# north, water enters on the two ENDS of the boiler's southern row) - a rotated boiler puts
# its water inputs on the N/S ends and the whole template stops meaning anything, so
# template() refuses any other direction rather than silently emitting a dry plant.
SIZES = {
    "boiler": (3, 2),
    "steam-engine": (3, 5),
    "offshore-pump": (1, 1),
    "pipe": (1, 1),
    "pipe-to-ground": (1, 1),
    "burner-inserter": (1, 1),
    "transport-belt": (1, 1),
    "small-electric-pole": (1, 1),
}

# Fluidbox CONNECTED-TILE offsets from the entity centre at direction 0, measured from
# prototypes.entity[n].fluidbox_prototypes (the connection tile is one step further out
# than the connection point itself).
FLUIDBOX = {
    "boiler": {"water": ((-2.0, 0.5), (2.0, 0.5)), "steam": (0.0, -1.5)},
    "steam-engine": {"steam": ((0.0, 2.5), (0.0, -2.5))},
}
# offshore-pump: output is on the pump's OWN tile, pointing OPPOSITE `direction`. d=8 means
# water is south, so the output tile is the one directly north.
PUMP_OUT_OFFSET = {N: (0.0, 1.0), E: (-1.0, 0.0), S: (0.0, -1.0), W: (1.0, 0.0)}
PIPE_TO_GROUND_MAX = 10          # max_underground_distance; the riser uses 2

# --------------------------------------------------------------------------- the template
# Every offset is in TILES from the ANCHOR = boiler-0's TOP-LEFT tile (bx0, by0).
# Reference: pump (-32,51) -> anchor (-35,45).
COLUMN_PITCH = 4                 # boiler/engine column pitch: 3-wide boiler + 1 gap column
ENGINE_STACK = (5, 10)           # engine k top-left row = by0 - ENGINE_STACK[k]
GAP_DX = 3                       # gap/spine column g at x = bx0 + GAP_DX + 4g
TAP_DY = 1                       # shared water tap pipe row
RISER_DY = (2, 4)                # pipe-to-ground pair (d0 north-facing, d8 south-facing)
COAL_ROW_DY = 3                  # coal belt row - the row the riser ducks under
MANIFOLD_DY = 5                  # water manifold row
PUMP_DY = 6                      # offshore-pump row
INSERTER_DX, INSERTER_DY = 1, 2  # burner-inserter: boiler's centre column, service row
FEEDER_DX = -2                   # coal feeder column (the belt row's west end / corner)
TRUNK_DX = -1                    # pole trunk column
POLE_ROW_DY = -5                 # pole row: engine-1 / engine-2 seam
TRUNK_PITCH = 7                  # straight orthogonal trunk runs, exactly 7 (wire 7.5)

# --------------------------------------------------------------------------- rates
BOILER_MW = 1.8                  # boiler.get_max_energy_usage() 30000 J/tick
ENGINE_MW = 0.9                  # steam-engine.get_max_energy_production() 15000 J/tick
ENGINES_PER_BOILER = 2           # 1.8 / 0.9 - exact, and enforced
BOILER_WATER_PER_S = 60.0
PUMP_WATER_PER_S = 1200.0        # -> 1 pump : 20 boilers : 40 engines = 36 MW
BOILER_COAL_PER_MIN = 27.0       # 1.8 MW / 4 MJ per coal
COAL_HEADROOM_MIN = 1.5          # operator's measured 120 supplied / 77 demanded = 1.56
SPLITTER_TAP_SHARE = 0.5         # a plain 50/50 splitter is the tap; it self-limits

# --------------------------------------------------------------------------- verification
PUMP_WATER_MIN = 100             # a CONNECTED offshore pump reads exactly 100
BOILER_WATER_MIN = 1             # > 0
BOILER_STEAM_WARN = 190          # healthy plant reads 199-200/200
ENGINE_ENERGY_MIN = 1            # > 0
INSERTER_OK_STATUS = (1, 36)     # 36 = waiting_for_space_in_destination = topped-up boiler
SPEC_BUDGET = 3800               # keep every generated /sc under the 4KB RCON cap
MAX_POLE_DEGREE = 4              # 4 of the 5 copper slots - keep one free for a later
#                                  neighbour (a saturated pole cannot adopt a bridge)

# --------------------------------------------------------------------------- clearance
# order-spec MIN_CLEARANCE, the rows that involve a power plant.
MIN_CLEARANCE = {
    "consumer_block": 21,
    "lab_array": 21,
    "smelter_array": 16,
    "mine_outpost": 14,
    "power_plant": 14,
    None: 12,                    # any_block <-> any_block floor
}
CLEARANCE_PENALTY = 1000.0       # per violated pair, in "belt tiles" - a hard sort key
POLE_COST_RATIO = 1.0 / TRUNK_PITCH   # power costs 1 pole per 7 tiles; coal costs 1 belt/tile


class PlantError(Exception):
    """The requested plant cannot be planned here with these parts."""


# --------------------------------------------------------------------------- geometry
def size_of(name):
    if name in SIZES:
        return SIZES[name]
    return mine_layout.size_of(name)


def center(name, tile_x, tile_y):
    """Entity CENTRE from its top-left footprint tile. Odd width -> .5, even -> integer."""
    tw, th = size_of(name)
    return (tile_x + tw / 2.0, tile_y + th / 2.0)


def footprint(name, tile_x, tile_y):
    tw, th = size_of(name)
    return {(tile_x + i, tile_y + j) for i in range(tw) for j in range(th)}


def key_tile(name, tile_x, tile_y):
    """floor(centre) - the tile buildplan/world.scan_tiles key this entity by. See the
    module docstring: probe() and _default_remove() both report floor(entity.position),
    so a plan keyed any other way silently double-places and falsely reports removal."""
    cx, cy = center(name, tile_x, tile_y)
    return (math.floor(cx), math.floor(cy))


def anchor_from_pump(px, py):
    """(bx0, by0) = boiler-0's TOP-LEFT tile from the pump's LAND tile.
    Measured: pump (-32,51) -> anchor (-35,45)."""
    return (int(px) - GAP_DX, int(py) - PUMP_DY)


def pump_tile(anchor):
    bx0, by0 = anchor
    return (bx0 + GAP_DX, by0 + PUMP_DY)


def gap_x(anchor, g=0):
    """The x of gap/spine column g. Gap g sits between column g and column g+1."""
    return anchor[0] + GAP_DX + COLUMN_PITCH * g


def columns_for(n_engines):
    """1 boiler : 2 engines, exactly. An odd request rounds UP to a whole column - a boiler
    running one engine is half a boiler wasted, and the bot's old _build_boiler_engine
    stacking a 3rd engine onto ONE boiler is exactly what principles.plant_ratio_ok flags."""
    if int(n_engines) < 1:
        raise PlantError("n_engines must be >= 1, got %r" % (n_engines,))
    return int(math.ceil(int(n_engines) / float(ENGINES_PER_BOILER)))


def reserved_rects(anchor, n_columns):
    """The three land rectangles the plant needs, as inclusive (x1,x2,y1,y2) tile rects.

    R1 machine block  - engines, boilers, service row, coal belt row
    R2 west utilities - coal feeder column + pole trunk column
    R3 south services - the duck's south end, the water manifold, the pump row
    Total footprint (4N+2) x 17.
    """
    bx0, by0 = anchor
    return (
        (bx0, bx0 + COLUMN_PITCH * n_columns - 2, by0 - 10, by0 + COAL_ROW_DY),
        (bx0 + FEEDER_DX, bx0 + TRUNK_DX, by0 - 10, by0 + COAL_ROW_DY),
        (bx0 + GAP_DX, bx0 + COLUMN_PITCH * n_columns - 1, by0 + 4, by0 + PUMP_DY),
    )


def bbox(anchor, n_columns):
    """Inclusive (x1,y1,x2,y2) of the whole plant footprint."""
    bx0, by0 = anchor
    return (bx0 + FEEDER_DX, by0 - 10,
            bx0 + COLUMN_PITCH * n_columns - 1, by0 + PUMP_DY)


def coal_intake(plan):
    """Where an EXTERNAL coal spur must hand off: the last tile of the feeder column,
    flowing SOUTH into the coal belt row's west corner. Route to it with belt_router
    (plan_route(start, goal, kind='belt', obstacles=...)); do not hand-lay it here - the
    bot's old spur descended straight into the engine footprint and dead-ended 11 tiles
    short of the boiler."""
    bx0, by0 = plan["anchor"]
    return {"tile": (bx0 + FEEDER_DX, by0 + COAL_ROW_DY - 1), "direction": S,
            "corner": (bx0 + FEEDER_DX, by0 + COAL_ROW_DY),
            "demand_per_min": BOILER_COAL_PER_MIN * plan["n_columns"]}


# --------------------------------------------------------------------------- terrain
class Terrain:
    """Read-only tile facts for siting. Sets of TILES; anything outside `bbox` is UNKNOWN
    and is refused rather than assumed clear (an unscanned tile is not a safe tile)."""

    def __init__(self, bbox=None, water=(), cliff=(), resource=(), blocked=()):
        self.bbox = tuple(bbox) if bbox else None
        self.water = set(map(tuple, water))
        self.cliff = set(map(tuple, cliff))
        self.resource = set(map(tuple, resource))
        self.blocked = set(map(tuple, blocked))

    def known(self, x, y):
        if self.bbox is None:
            return True
        x1, y1, x2, y2 = self.bbox
        return x1 <= x <= x2 and y1 <= y <= y2

    def is_water(self, x, y):
        return (x, y) in self.water

    def is_land(self, x, y):
        return self.known(x, y) and (x, y) not in self.water

    def is_clear(self, x, y):
        """Land, no cliff, no resource, nothing standing on it. Boilers and engines are NOT
        in autopilot.ORE_OK, so a resource tile is a hard refusal, not a preference."""
        t = (x, y)
        return (self.known(x, y) and t not in self.water and t not in self.cliff
                and t not in self.resource and t not in self.blocked)

    def rect_clear(self, x1, x2, y1, y2):
        for x in range(x1, x2 + 1):
            for y in range(y1, y2 + 1):
                if not self.is_clear(x, y):
                    return False
        return True

    def rect_problems(self, x1, x2, y1, y2, limit=4):
        out = []
        for x in range(x1, x2 + 1):
            for y in range(y1, y2 + 1):
                if self.is_clear(x, y):
                    continue
                t = (x, y)
                why = ("unscanned" if not self.known(x, y) else
                       "water" if t in self.water else
                       "cliff" if t in self.cliff else
                       "resource" if t in self.resource else "occupied")
                out.append((t, why))
                if len(out) >= limit:
                    return out
        return out


def scan_shore(cx, cy, radius=30):
    """RCON READ ONLY -> Terrain over the tile box centred on (cx,cy).

    Water is detected by name substring (there are many water variants - water,
    deepwater, water-shallow, water-mud...), matching bootstrap's own `iw()` test.
    Cliffs and resources come from find_entities_filtered by TYPE; every other player
    entity is `blocked`. Emits one compact string through the storage._world chunked
    read (world._chunked) rather than JSON - a 61x61 water box is thousands of tiles.
    """
    x1, y1 = int(cx) - int(radius), int(cy) - int(radius)
    x2, y2 = int(cx) + int(radius), int(cy) + int(radius)
    lua = (
        "local s=game.surfaces[1]; local W={}; local C={}; local R={}; local B={};"
        "for x=%d,%d do for y=%d,%d do" % (x1, x2, y1, y2) +
        "  if string.find(s.get_tile(x,y).name,'water') then W[#W+1]=x..','..y end end end;"
        "local A={{%d,%d},{%d,%d}};" % (x1, y1, x2 + 1, y2 + 1) +
        "for _,e in pairs(s.find_entities_filtered{area=A,type='cliff'}) do"
        "  C[#C+1]=math.floor(e.position.x)..','..math.floor(e.position.y) end;"
        "for _,e in pairs(s.find_entities_filtered{area=A,type='resource'}) do"
        "  R[#R+1]=math.floor(e.position.x)..','..math.floor(e.position.y) end;"
        "for _,e in pairs(s.find_entities_filtered{area=A,force='player'}) do"
        "  if e.name~='character' then"
        "    B[#B+1]=math.floor(e.position.x)..','..math.floor(e.position.y) end end;"
        "storage._world='W:'..table.concat(W,';')..'|C:'..table.concat(C,';')"
        "..'|R:'..table.concat(R,';')..'|B:'..table.concat(B,';');"
        "rcon.print(#storage._world)"
    )
    return parse_shore(world._chunked(lua), (x1, y1, x2, y2))


def parse_shore(raw, bbox_):
    """Split scan_shore's payload into a Terrain (pure; the offline half of scan_shore)."""
    sec = {"W": set(), "C": set(), "R": set(), "B": set()}
    for part in (raw or "").split("|"):
        if len(part) < 2 or part[1] != ":":
            continue
        key = part[0]
        if key not in sec:
            continue
        sec[key] = {(int(a), int(b)) for a, b in re.findall(r"(-?\d+),(-?\d+)", part[2:])}
    return Terrain(bbox_, water=sec["W"], cliff=sec["C"], resource=sec["R"],
                   blocked=sec["B"])


# --------------------------------------------------------------------------- siting
def site_valid(terrain, px, py, n_columns):
    """The measured siting predicate -> (ok, reasons).

    1. the pump tile is LAND and has a 3-wide WATER frontage to the SOUTH (the template is
       water-south only: `direction` points at the water and the output is opposite, so a
       d8 pump feeds the manifold from the north);
    2. the three reserved rectangles are land, cliff-free, RESOURCE-free and unoccupied.

    Live-validated: scanning y in [44,58], x in [-46,-18], 24 tiles accept an offshore pump
    at d8 but only 8 satisfy this whole predicate - and the operator's site is one of them.
    The naive alternatives fail readably (they put water tiles inside R1).
    """
    why = []
    if terrain is None:
        return (False, ["no terrain: scan_shore() first, or pass terrain="])
    if not terrain.is_land(px, py):
        why.append("pump tile (%d,%d) is not land" % (px, py))
    for dx in (-1, 0, 1):
        if not terrain.is_water(px + dx, py + 1):
            why.append("no water frontage at (%d,%d)" % (px + dx, py + 1))
    anchor = anchor_from_pump(px, py)
    for i, (x1, x2, y1, y2) in enumerate(reserved_rects(anchor, n_columns), start=1):
        bad = terrain.rect_problems(x1, x2, y1, y2)
        if bad:
            why.append("R%d (%d..%d, %d..%d) not clear: %s"
                       % (i, x1, x2, y1, y2,
                          ", ".join("%s@%s" % (w, t) for t, w in bad)))
    return (not why, why)


def _bbox_gap(a, b):
    """Clear tiles between two inclusive (x1,y1,x2,y2) bboxes; 0 if they touch or overlap."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    gx = max(0, bx1 - ax2 - 1, ax1 - bx2 - 1)
    gy = max(0, by1 - ay2 - 1, ay1 - by2 - 1)
    return max(gx, gy)


def clearance_violations(anchor, n_columns, avoid):
    """avoid: [{"kind": str, "bbox": (x1,y1,x2,y2)}] -> list of violations."""
    mine = bbox(anchor, n_columns)
    out = []
    for a in avoid or ():
        b = tuple(a["bbox"])
        need = MIN_CLEARANCE.get(a.get("kind"), MIN_CLEARANCE[None])
        got = _bbox_gap(mine, b)
        if got < need:
            out.append({"kind": a.get("kind"), "bbox": b, "need": need, "got": got})
    return out


def site_plant(near, terrain=None, n_engines=4, coal_tap=None, avoid=(), limit=8,
               radius=30):
    """Score candidate shore sites -> list of candidates, BEST FIRST (lowest `cost`).

    near      : (x,y) the plant must serve - the electrical destination (the base).
    coal_tap  : (x,y) where coal is available; the plant's only BELTED input.
    terrain   : a Terrain; if omitted, scan_shore(near, radius) is called (RCON READ ONLY).

    Cost model (spec §7 inference 4, labelled there as plausible-not-measured): coal must be
    physically belted at 1 belt per tile; power leaves over a pole trunk at 1 pole per 7
    tiles. So cost = coal_belt_tiles + trunk_tiles/7, plus CLEARANCE_PENALTY per area whose
    minimum separation this site would violate. The operator's own plant sits 31 tiles below
    the coal lane and ~57 from the smelters: sited at its FUEL, not at its LOAD.
    """
    n_columns = columns_for(n_engines)
    if terrain is None:
        terrain = scan_shore(near[0], near[1], radius)
    x1, y1, x2, y2 = terrain.bbox if terrain.bbox else (0, 0, 0, 0)
    out = []
    for px in range(x1, x2 + 1):
        for py in range(y1, y2 + 1):
            # cheap gate first: the shore signature. Full predicate only for real shores.
            if not terrain.is_land(px, py):
                continue
            if not all(terrain.is_water(px + dx, py + 1) for dx in (-1, 0, 1)):
                continue
            ok, why = site_valid(terrain, px, py, n_columns)
            if not ok:
                continue
            anchor = anchor_from_pump(px, py)
            intake = (anchor[0] + FEEDER_DX, anchor[1] + COAL_ROW_DY - 1)
            junction = (anchor[0] + TRUNK_DX, anchor[1] + POLE_ROW_DY)
            belt_len = (abs(intake[0] - coal_tap[0]) + abs(intake[1] - coal_tap[1])
                        if coal_tap else 0.0)
            trunk_len = abs(junction[0] - near[0]) + abs(junction[1] - near[1])
            viol = clearance_violations(anchor, n_columns, avoid)
            cost = (belt_len + POLE_COST_RATIO * trunk_len
                    + CLEARANCE_PENALTY * len(viol))
            out.append({"pump": (px, py), "anchor": anchor, "n_columns": n_columns,
                        "cost": round(cost, 3), "belt_tiles": belt_len,
                        "trunk_tiles": trunk_len, "clearance": viol,
                        "bbox": bbox(anchor, n_columns)})
    out.sort(key=lambda c: (c["cost"], c["pump"]))
    return out[:limit] if limit else out


# --------------------------------------------------------------------------- template
def template(anchor, n_columns, *, belt="transport-belt", pole="small-electric-pole",
             trunk_to_y=None):
    """The pure geometry: [{entity,x,y,direction,role,...}] with x,y = TOP-LEFT tile.

    Column k at x = bx0 + 4k. Gap column g at x = bx0 + 3 + 4g. There are max(1, N-1) gap
    columns' worth of shared hardware: N-1 real gaps, but a single-column plant still needs
    the one spine column east of it for the tap, the riser, the pump and the spur pole.
    """
    if n_columns < 1:
        raise PlantError("n_columns must be >= 1, got %r" % (n_columns,))
    if pole is not None and pole not in mine_layout.POLES:
        raise PlantError("unknown pole %r" % (pole,))
    bx0, by0 = int(anchor[0]), int(anchor[1])
    ents = []

    def add(name, x, y, direction, role, **kw):
        e = {"entity": name, "x": int(x), "y": int(y), "direction": int(direction),
             "role": role}
        e.update(kw)
        ents.append(e)

    # ---- per column: 1 boiler, 2 engines stacked north, 1 burner-inserter -----------
    for k in range(n_columns):
        cx = bx0 + COLUMN_PITCH * k
        add("boiler", cx, by0, N, "boiler", column=k)
        for j, dy in enumerate(ENGINE_STACK):
            add("steam-engine", cx, by0 - dy, N, "engine", column=k, stack=j)
        # d8 = the inserter's PICKUP side: picks coal from the belt row (south) and drops
        # NORTH into the boiler's southern row. Never a chest - the plant is belt-fed.
        add("burner-inserter", cx + INSERTER_DX, by0 + INSERTER_DY, S, "inserter", column=k)

    # ---- shared spine: one tap pipe per gap column ---------------------------------
    n_gaps = max(1, n_columns - 1)
    for g in range(n_gaps):
        add("pipe", gap_x((bx0, by0), g), by0 + TAP_DY, N, "tap", gap=g)

    # ---- riser: 2-tile underground ducking the coal belt row -----------------------
    gx0 = gap_x((bx0, by0), 0)
    add("pipe-to-ground", gx0, by0 + RISER_DY[0], N, "riser")   # opening N -> the tap
    add("pipe-to-ground", gx0, by0 + RISER_DY[1], S, "riser")   # opening S -> the manifold

    # ---- water manifold + pump ------------------------------------------------------
    # The manifold reaches the LAST gap column: 4(N-1)+1 pipes, which reproduces the
    # measured 5 at N=2. Hydraulically only ONE riser is required (water chains through the
    # boilers, module docstring), so the rest is buffer + the operator's deliberate
    # one-column pre-extension east. Note the consequence: total pipes = (N-1) taps +
    # (4N-3) manifold + 2 riser = 5N-2 (4 at N=1), so pipes-per-boiler is 5 - 2/N and
    # principles.plant_ratio_ok's "pipes per boiler > 4.0" P6 warning starts firing at
    # N>=3 (13/3 = 4.33). That is the measured design, not a defect - trim the manifold
    # only if you have a reason to depart from it.
    for x in range(gx0, gx0 + COLUMN_PITCH * (n_columns - 1) + 1):
        add("pipe", x, by0 + MANIFOLD_DY, N, "manifold")
    add("offshore-pump", gx0, by0 + PUMP_DY, S, "pump")         # d = the WATER side

    # ---- coal belt: enters WEST, runs east under every boiler, dead-ends ------------
    for x in range(bx0 + FEEDER_DX, bx0 + COLUMN_PITCH * (n_columns - 1) + INSERTER_DX + 1):
        add(belt, x, by0 + COAL_ROW_DY, E, "coal_belt")

    # ---- poles: one spur per gap column (pitch 4 - connectivity is then free) -------
    if pole is not None:
        for g in range(n_gaps):
            add(pole, gap_x((bx0, by0), g), by0 + POLE_ROW_DY, N, "pole_spur", gap=g)
        rows = [by0 + POLE_ROW_DY]
        if trunk_to_y is not None:
            y = by0 + POLE_ROW_DY
            step = -TRUNK_PITCH if trunk_to_y < y else TRUNK_PITCH
            while (y > trunk_to_y) if step < 0 else (y < trunk_to_y):
                y += step
                rows.append(y)
        for y in rows:
            add(pole, bx0 + TRUNK_DX, y, N, "pole_trunk")
    return ents


def plan_plant(n_engines, water_hint=None, *, terrain=None, near=None, coal_tap=None,
               avoid=(), belt="transport-belt", pole="small-electric-pole",
               trunk_to_y=None, coal_supply_per_min=None):
    """Plan a whole plant. Nothing is placed.

    n_engines  : rounded UP to a whole 1:2 column (see columns_for).
    water_hint : the offshore-pump's LAND tile (px,py). If omitted, site_plant() picks the
                 best candidate from `terrain` (which is scanned around `near` if absent).
    """
    n_columns = columns_for(n_engines)
    warnings = []
    engines = n_columns * ENGINES_PER_BOILER
    if engines != int(n_engines):
        warnings.append("rounded %s engines up to %d (1 boiler : 2 engines is exact; a "
                        "boiler running one engine is half a boiler wasted)"
                        % (n_engines, engines))

    if water_hint is not None:
        px, py = int(water_hint[0]), int(water_hint[1])
    else:
        if terrain is None and near is None:
            raise PlantError("plan_plant needs water_hint=(px,py), or terrain=, or near= "
                             "so a shore can be scanned and scored")
        cands = site_plant(near or (0, 0), terrain=terrain, n_engines=engines,
                           coal_tap=coal_tap, avoid=avoid)
        if not cands:
            raise PlantError("no shore site satisfies the siting predicate for %d columns "
                             "(need a 3-wide water frontage south of a (%d x 17) clear "
                             "block)" % (n_columns, 4 * n_columns + 2))
        px, py = cands[0]["pump"]
    anchor = anchor_from_pump(px, py)

    if terrain is not None:
        ok, why = site_valid(terrain, px, py, n_columns)
        if not ok:
            raise PlantError("site (%d,%d) fails the siting predicate: %s"
                             % (px, py, "; ".join(why)))
    else:
        warnings.append("no terrain supplied: shore geometry, cliffs and ore zoning are "
                        "UNVERIFIED - run scan_shore() before build()")

    viol = clearance_violations(anchor, n_columns, avoid)
    for v in viol:
        warnings.append("clearance to %s is %d tiles, needs %d"
                        % (v["kind"], v["got"], v["need"]))

    ents = template(anchor, n_columns, belt=belt, pole=pole, trunk_to_y=trunk_to_y)

    coal_demand = BOILER_COAL_PER_MIN * n_columns
    if coal_supply_per_min is not None:
        deliverable = coal_supply_per_min * SPLITTER_TAP_SHARE
        if deliverable < coal_demand * COAL_HEADROOM_MIN:
            warnings.append("coal: a 50/50 splitter tap delivers %.0f/min but %d columns "
                            "demand %.0f/min (headroom %.2f < %.2f) - raise coal production "
                            "or use a higher-throughput tap, not more boilers"
                            % (deliverable, n_columns, coal_demand,
                               deliverable / coal_demand if coal_demand else 0,
                               COAL_HEADROOM_MIN))
    if pole is None:
        warnings.append("pole=None: this plan emits NO poles, so the engines would generate "
                        "into nothing (Build Law 1). Only do this if power_planner is "
                        "laying the lattice; verify(require_single_network=) cannot check a "
                        "grid that is not in the plan")
    if n_columns > PUMP_WATER_PER_S / BOILER_WATER_PER_S:
        warnings.append("%d columns exceed one offshore pump (1200 water/s = 20 boilers); "
                        "add a SECOND pump plumbed into the same manifold - do not "
                        "re-architect" % n_columns)

    plan = {
        "kind": KIND,
        "anchor": (anchor[0], anchor[1]),
        "pump": (px, py),
        "spine_x": gap_x(anchor, 0),
        "n_columns": n_columns,
        "n_engines": engines,
        "n_engines_requested": int(n_engines),
        "n_boilers": n_columns,
        "entities": ents,
        "bbox": bbox(anchor, n_columns),
        "rects": reserved_rects(anchor, n_columns),
        "power_MW": round(ENGINE_MW * engines, 3),
        "boiler_MW": round(BOILER_MW * n_columns, 3),
        "coal_per_min": round(coal_demand, 2),
        "water_per_s": round(BOILER_WATER_PER_S * n_columns, 2),
        "warnings": warnings,
        "params": {"belt": belt, "pole": pole, "trunk_to_y": trunk_to_y,
                   "coal_tap": tuple(coal_tap) if coal_tap else None,
                   "coal_supply_per_min": coal_supply_per_min,
                   "avoid": [{"kind": a.get("kind"), "bbox": tuple(a["bbox"])}
                             for a in (avoid or ())]},
    }
    plan["intake"] = coal_intake(plan)
    plan["bom"] = bom(plan)
    return plan


def scale(existing, n_more, *, terrain=None, trunk_to_y=None):
    """EXTEND a plant east by ceil(n_more/2) columns instead of rebuilding it.

    Returns {"plan", "delta", "kept", "added_columns", "bom_delta", "warnings"}. Every
    entity of `existing` must reappear at an IDENTICAL (name, tile, direction) - that is
    checked, not assumed, and a mismatch raises. The pump, riser, coal feeder column and
    pole trunk are untouched by construction; the manifold and the coal belt each grow by
    exactly 4 tiles per column, and each new column brings 1 boiler + 2 engines + 1 burner
    inserter + 1 gap tap + 1 spur pole. Gain: +1.8 MW per column.

    Because buildplan.apply() probes the world first, build()ing the RETURNED PLAN is the
    correct way to realise this: the existing entities are found and only `delta` is placed.
    """
    if int(n_more) < 1:
        raise PlantError("n_more must be >= 1, got %r" % (n_more,))
    add_columns = int(math.ceil(int(n_more) / float(ENGINES_PER_BOILER)))
    n_columns = existing["n_columns"] + add_columns
    p = dict(existing.get("params") or {})
    plan = plan_plant(n_columns * ENGINES_PER_BOILER,
                      water_hint=existing["pump"],
                      terrain=terrain,
                      coal_tap=p.get("coal_tap"),
                      avoid=p.get("avoid") or (),
                      belt=p.get("belt", "transport-belt"),
                      pole=p.get("pole", "small-electric-pole"),
                      trunk_to_y=(p.get("trunk_to_y") if trunk_to_y is None else trunk_to_y),
                      coal_supply_per_min=p.get("coal_supply_per_min"))

    def sig(e):
        return (e["entity"], e["x"], e["y"], e["direction"])

    have = {sig(e) for e in plan["entities"]}
    moved = [e for e in existing["entities"] if sig(e) not in have]
    if moved:
        raise PlantError("scale() would MOVE %d existing entities (e.g. %s at (%d,%d)) - "
                         "that is a rebuild, not an extension; refusing"
                         % (len(moved), moved[0]["entity"], moved[0]["x"], moved[0]["y"]))
    old = {sig(e) for e in existing["entities"]}
    delta = [e for e in plan["entities"] if sig(e) not in old]
    kept = [e for e in plan["entities"] if sig(e) in old]
    warnings = list(plan["warnings"])
    if n_columns >= 3 and p.get("coal_supply_per_min") is None:
        warnings.append("N=%d: a 50/50 splitter tap ceilings at half the mine's output; "
                        "check coal before adding columns (pass coal_supply_per_min=)"
                        % n_columns)
    return {"plan": plan, "delta": delta, "kept": kept, "added_columns": add_columns,
            "bom_delta": _bom(delta), "warnings": warnings}


# --------------------------------------------------------------------------- output
def _bom(ents):
    out = {}
    for e in ents:
        out[e["entity"]] = out.get(e["entity"], 0) + 1
    return out


def bom(plan):
    """{item: count}. Every entity here is placed by an item of the same name on 2.1."""
    return _bom(plan["entities"])


ORDER_RANK = {"pump": 0, "manifold": 1, "riser": 2, "tap": 3, "boiler": 4, "engine": 5,
              "coal_belt": 6, "inserter": 7, "pole_spur": 8, "pole_trunk": 9}


def _ordered(ents):
    """Water first, then the machines, then fuel, then power export - so a partial run
    always leaves a plant that is CLOSER to working (water in a boiler is verifiable;
    engines with no water are not)."""
    return sorted(ents, key=lambda e: (ORDER_RANK.get(e["role"], 99), e["x"], e["y"]))


def to_orders(plan):
    """executor.submit shape: {"kind":"place","args":{name,tile_x,tile_y,direction}}."""
    return [{"kind": "place",
             "args": {"name": e["entity"], "tile_x": e["x"], "tile_y": e["y"],
                      "direction": e["direction"]}} for e in _ordered(plan["entities"])]


def to_ghosts(plan):
    """autopilot.stamp_blueprint shape [{name,x,y,dir}] with x,y the CENTRE."""
    out = []
    for e in _ordered(plan["entities"]):
        cx, cy = center(e["entity"], e["x"], e["y"])
        out.append({"name": e["entity"], "x": cx, "y": cy, "dir": e["direction"]})
    return out


# --------------------------------------------------------------------------- validation
def _supplies(p, pole_name, e):
    """Does pole `p` (top-left tile) cover entity `e`'s footprint?"""
    s = mine_layout.POLES[pole_name]["supply"]
    pw, ph = size_of(pole_name)
    pcx, pcy = p["x"] + pw / 2.0, p["y"] + ph / 2.0
    tw, th = size_of(e["entity"])
    return (pcx - s < e["x"] + tw and pcx + s > e["x"]
            and pcy - s < e["y"] + th and pcy + s > e["y"])


def validate(plan):
    """Pure invariant check -> {ok, errors}. These are the things that have actually
    produced a dry boiler, an unpowered engine or a plant with no fuel."""
    errs = []
    ents = plan["entities"]
    bx0, by0 = plan["anchor"]
    n = plan["n_columns"]
    by_role = {}
    for e in ents:
        by_role.setdefault(e["role"], []).append(e)

    # 1. the ratio. 1 boiler : 2 engines, exactly (principles P11).
    nb, ne = len(by_role.get("boiler", [])), len(by_role.get("engine", []))
    if ne != ENGINES_PER_BOILER * nb:
        errs.append("%d boilers : %d engines (must be exactly 1:%d)"
                    % (nb, ne, ENGINES_PER_BOILER))
    if nb != n:
        errs.append("plan says %d columns but carries %d boilers" % (n, nb))

    # 2. no footprint overlaps anywhere.
    used = {}
    for e in ents:
        for t in footprint(e["entity"], e["x"], e["y"]):
            if t in used:
                errs.append("footprint collision at %s: %s and %s"
                            % (t, used[t], e["entity"]))
            used[t] = e["entity"]

    # 3. every boiler has a tap pipe on one of its two water-port tiles. The whole design
    #    hangs on this: one gap pipe feeds two boilers and water chains along the row.
    taps = {(e["x"], e["y"]) for e in by_role.get("tap", [])}
    for e in by_role.get("boiler", []):
        if e["direction"] != N:
            errs.append("boiler at (%d,%d) is d%d - the template is d0 only (water on the "
                        "E/W ENDS, steam north)" % (e["x"], e["y"], e["direction"]))
        cx, cy = center("boiler", e["x"], e["y"])
        ports = {(math.floor(cx + dx), math.floor(cy + dy))
                 for dx, dy in FLUIDBOX["boiler"]["water"]}
        if not (ports & taps):
            errs.append("boiler at (%d,%d) has no water tap on either port tile %s"
                        % (e["x"], e["y"], sorted(ports)))

    # 4. the riser: a 2-tile underground pair in the spine column, ducking the coal row.
    risers = sorted(by_role.get("riser", []), key=lambda e: e["y"])
    if len(risers) != 2:
        errs.append("expected exactly 2 pipe-to-ground riser pieces, got %d" % len(risers))
    else:
        a, b = risers
        span = b["y"] - a["y"]
        if a["x"] != b["x"]:
            errs.append("riser pieces are not in the same column (%d vs %d)"
                        % (a["x"], b["x"]))
        if not (0 < span <= PIPE_TO_GROUND_MAX):
            errs.append("riser span %d is outside 1..%d" % (span, PIPE_TO_GROUND_MAX))
        if (a["direction"], b["direction"]) != (N, S):
            errs.append("riser openings are d%d/d%d, want d%d (north, to the tap) then "
                        "d%d (south, to the manifold)"
                        % (a["direction"], b["direction"], N, S))
        coal_rows = {e["y"] for e in by_role.get("coal_belt", [])}
        if not any(a["y"] < r < b["y"] for r in coal_rows):
            errs.append("the riser ducks under nothing: no coal belt row strictly between "
                        "y=%d and y=%d" % (a["y"], b["y"]))

    # 5. the pump: water-side direction, and its output tile holds a manifold pipe.
    manifold = {(e["x"], e["y"]) for e in by_role.get("manifold", [])}
    pumps = by_role.get("pump", [])
    if len(pumps) != 1:
        errs.append("expected exactly 1 offshore-pump, got %d" % len(pumps))
    for e in pumps:
        ox, oy = PUMP_OUT_OFFSET[e["direction"]]
        out_tile = (e["x"] + int(ox), e["y"] + int(oy))
        if out_tile not in manifold:
            errs.append("pump at (%d,%d) d%d outputs onto %s, which is not a manifold pipe"
                        % (e["x"], e["y"], e["direction"], out_tile))

    # 6. the manifold reaches every gap column, and the riser's south end sits on it.
    for g in range(max(1, n - 1)):
        gx = gap_x((bx0, by0), g)
        if (gx, by0 + MANIFOLD_DY) not in manifold:
            errs.append("manifold does not reach gap column %d (x=%d)" % (g, gx))
    if len(risers) == 2 and (risers[1]["x"], risers[1]["y"] + 1) not in manifold:
        errs.append("the riser's south end does not meet the manifold")

    # 7. the coal belt: contiguous, all east, and every inserter picks off it and drops
    #    INSIDE its own boiler (the bot's dead inserter picked from a PIPE and moved
    #    nothing, ever).
    belts = by_role.get("coal_belt", [])
    if belts:
        rows = {e["y"] for e in belts}
        if len(rows) != 1:
            errs.append("coal belt spans %d rows: %s" % (len(rows), sorted(rows)))
        bad_dir = [e for e in belts if e["direction"] != E]
        if bad_dir:
            errs.append("%d coal belt tiles do not flow east (first at (%d,%d))"
                        % (len(bad_dir), bad_dir[0]["x"], bad_dir[0]["y"]))
        xs = sorted(e["x"] for e in belts)
        gaps = [x for x in range(xs[0], xs[-1] + 1) if x not in set(xs)]
        if gaps:
            errs.append("coal belt has %d gap tile(s): x=%s" % (len(gaps), gaps[:6]))
        last_boiler_centre = bx0 + COLUMN_PITCH * (n - 1) + INSERTER_DX
        if xs[-1] != last_boiler_centre:
            errs.append("coal belt ends at x=%d, not the last boiler's centre column x=%d "
                        "(it must dead-end there - no terminal chest)"
                        % (xs[-1], last_boiler_centre))
    belt_tiles = {(e["x"], e["y"]) for e in belts}
    boiler_tiles = {}
    for e in by_role.get("boiler", []):
        for t in footprint("boiler", e["x"], e["y"]):
            boiler_tiles[t] = e
    for e in by_role.get("inserter", []):
        if e["direction"] != S:
            errs.append("coal inserter at (%d,%d) is d%d, want d%d (pickup side = the belt "
                        "row to its south)" % (e["x"], e["y"], e["direction"], S))
            continue
        pick = (e["x"], e["y"] + 1)
        drop = (e["x"], e["y"] - 1)
        if pick not in belt_tiles:
            errs.append("coal inserter at (%d,%d) picks up from %s, which is not the coal "
                        "belt" % (e["x"], e["y"], pick))
        if drop not in boiler_tiles:
            errs.append("coal inserter at (%d,%d) drops on %s, which is not a boiler"
                        % (e["x"], e["y"], drop))

    # 8. poles: every engine covered, and ONE wire-connected network.
    pole_name = (plan.get("params") or {}).get("pole")
    poles = by_role.get("pole_spur", []) + by_role.get("pole_trunk", [])
    if pole_name and poles:
        for e in by_role.get("engine", []):
            if not any(_supplies(p, pole_name, e) for p in poles):
                errs.append("steam engine at (%d,%d) is outside every %s supply area"
                            % (e["x"], e["y"], pole_name))
        comps = mine_layout._components(poles, mine_layout.POLES[pole_name]["wire"])
        if len(comps) > 1:
            errs.append("poles form %d separate electric networks - script-placed poles do "
                        "NOT auto-connect, so a split lattice never heals" % len(comps))
    elif pole_name:
        errs.append("no poles planned: the engines would generate into nothing")

    # 9. the BOM accounts for every entity 1:1.
    b = plan.get("bom") or bom(plan)
    if sum(b.values()) != len(ents):
        errs.append("bom totals %d but the plan has %d entities"
                    % (sum(b.values()), len(ents)))

    # 10. key tiles must be unique, or buildplan's probe/remove would alias two entities.
    keys = {}
    for e in ents:
        k = key_tile(e["entity"], e["x"], e["y"])
        if k in keys:
            errs.append("key-tile collision at %s: %s and %s" % (k, keys[k], e["entity"]))
        keys[k] = e["entity"]
    return {"ok": not errs, "errors": errs}


# --------------------------------------------------------------------------- verification
def _check_spec(plan):
    """The entities the functional check reads, as (name, centre_x, centre_y). Not every
    entity - the ones whose live state answers "is this plant actually making power".
    """
    by_role = {}
    for e in plan["entities"]:
        by_role.setdefault(e["role"], []).append(e)
    want = (by_role.get("pump", []) + by_role.get("boiler", [])
            + by_role.get("engine", []) + by_role.get("inserter", [])
            + by_role.get("pole_spur", []))
    belts = sorted(by_role.get("coal_belt", []), key=lambda e: e["x"])
    if belts:
        want = want + [belts[-1]]          # the dead-end tile: coal reached the last boiler
    trunk = sorted(by_role.get("pole_trunk", []), key=lambda e: e["y"])
    if trunk:
        want = want + [trunk[-1]]          # the junction pole: is the plant on the grid?
    out = []
    for e in want:
        cx, cy = center(e["entity"], e["x"], e["y"])
        out.append((e["entity"], cx, cy))
    return out


def verify_lua(spec):
    """One READ-ONLY /sc reading live state for `spec` -> 'name,x,y,v1,v2,v3|...'.

    v1/v2/v3 by entity TYPE, never by name: boiler=(water,steam,-1); generator=(energy,
    generated_last_tick,network_id); offshore-pump/pipe/pipe-to-ground=(water,-1,-1);
    transport-belt=(items,-1,-1); inserter=(status,coal,-1); electric-pole=
    (network_id,-1,-1). Dispatching on TYPE is what makes a tier swap (fast belt, medium
    pole, bulk inserter) keep its check instead of silently falling through - and the
    fall-through branch is pcall'd, because reading a property an entity does not carry
    ABORTS THE WHOLE /sc (GOTCHAS: 'LuaEntity doesn't contain key ...'), which would report
    every entity in the batch as missing and roll a healthy plant back.

    A missing entity reports (-2,-2,-2). Nothing here writes.
    """
    body = ";".join("%s,%.1f,%.1f" % s for s in spec)
    return (
        "/sc local s=game.surfaces[1]; local o={};"
        "for n,a,b in ([==[" + body + "]==]):gmatch('([%a%-]+),(-?[%d%.]+),(-?[%d%.]+)') do"
        "  local x,y=tonumber(a),tonumber(b);"
        "  local e=s.find_entities_filtered{name=n,position={x,y},radius=0.6}[1];"
        "  local v1,v2,v3=-2,-2,-2;"
        "  if e and e.valid then local t=e.type;"
        "    if t=='boiler' then v1=math.floor(e.get_fluid_count('water'));"
        "      v2=math.floor(e.get_fluid_count('steam')); v3=-1"
        "    elseif t=='generator' then v1=math.floor(e.energy);"
        "      v2=math.floor(e.energy_generated_last_tick or 0);"
        "      local okn,id=pcall(function() return e.electric_network_id end);"
        "      v3=(okn and id) or -1"
        "    elseif t=='offshore-pump' or t=='pipe' or t=='pipe-to-ground' then"
        "      v1=math.floor(e.get_fluid_count('water')); v2=-1; v3=-1"
        "    elseif t=='transport-belt' then local c=0;"
        "      for li=1,e.get_max_transport_line_index() do"
        "        c=c+#e.get_transport_line(li) end; v1=c; v2=-1; v3=-1"
        "    elseif t=='inserter' then v1=e.status or -1;"
        "      local fi=e.get_fuel_inventory(); v2=fi and fi.get_item_count('coal') or -1;"
        "      v3=-1"
        "    else local okn,id=pcall(function() return e.electric_network_id end);"
        "      v1=(okn and id) or -1; v2=-1; v3=-1 end end;"
        "  o[#o+1]=n..','..a..','..b..','..v1..','..v2..','..v3 end;"
        "rcon.print(table.concat(o,'|'))")


def parse_state(raw):
    """'name,x,y,v1,v2,v3|...' -> {(name, x, y): (v1, v2, v3)}."""
    out = {}
    for rec in (raw or "").strip().split("|"):
        m = re.match(r"^([a-z\-]+),(-?[\d.]+),(-?[\d.]+),(-?\d+),(-?\d+),(-?\d+)$",
                     rec.strip())
        if m:
            out[(m.group(1), float(m.group(2)), float(m.group(3)))] = (
                int(m.group(4)), int(m.group(5)), int(m.group(6)))
    return out


def read_state(plan):
    """RCON READ ONLY. Batched so no /sc ever passes the 4KB cap."""
    spec = _check_spec(plan)
    out = {}
    batch, size = [], 0
    overhead = len(verify_lua([]))
    for s in spec:
        piece = "%s,%.1f,%.1f" % s
        add = len(piece) + (1 if batch else 0)
        if batch and overhead + size + add > SPEC_BUDGET:
            out.update(parse_state(rcon.run(verify_lua(batch))))
            batch, size, add = [], 0, len(piece)
        batch.append(s)
        size += add
    if batch:
        out.update(parse_state(rcon.run(verify_lua(batch))))
    return out


def _categories(plan):
    """entity name -> verification category, derived from the plan's OWN params.

    Hardcoding the literals 'transport-belt' / 'small-electric-pole' here silently deleted
    two checks whenever a caller passed belt= or pole=: a medium-pole plant with its poles
    on one network and its engines on another verified clean, with no warning at all.
    """
    p = plan.get("params") or {}
    cats = {"boiler": "boiler", "steam-engine": "engine", "offshore-pump": "pump",
            "burner-inserter": "inserter", "transport-belt": "belt",
            "small-electric-pole": "pole"}
    if p.get("belt"):
        cats[p["belt"]] = "belt"
    if p.get("pole"):
        cats[p["pole"]] = "pole"
    return cats


def verify(plan, state=None, pump_water=PUMP_WATER_MIN, boiler_water=BOILER_WATER_MIN,
           engine_energy=ENGINE_ENERGY_MIN, require_single_network=True):
    """FUNCTIONAL check (Build Law 1) -> (ok, detail).

    The gate is fluid, energy and the GRID, never "create_entity returned ok":
        offshore-pump   get_fluid_count('water') >= 100     (an UNCONNECTED pump reads 0)
        every boiler    get_fluid_count('water')  > 0
        every engine    energy                    > 0
        poles+engines   exactly ONE electric_network_id     (P2)
    The network id is a FAILURE, not a warning: place() has already wired every pair
    explicitly and build() re-wires before each attempt, so a split that survives that is a
    plant with stranded generation - the single worst recurring failure in GOTCHAS - and
    Build Law 2 says remove what does nothing. Pass require_single_network=False to
    downgrade it (e.g. a plant deliberately built before its trunk exists).

    Everything else - steam level, generated_last_tick, coal on the belt's dead-end tile,
    inserter status in {1, 36} - is a WARNING, so a plant that is merely COLD is never torn
    down, but a plant that is dry or islanded is.
    """
    if state is None:
        state = read_state(plan)
    cats = _categories(plan)
    missing, fail, warn, notes = [], [], [], []
    nets = set()
    for name, cx, cy in _check_spec(plan):
        v = state.get((name, cx, cy))
        if v is None or v[0] == -2:
            missing.append("%s@%.1f,%.1f" % (name, cx, cy))
            continue
        v1, v2, v3 = v
        cat = cats.get(name)
        if cat == "pump":
            notes.append("pump w=%d" % v1)
            if v1 < pump_water:
                fail.append("pump@%.1f,%.1f water=%d < %d" % (cx, cy, v1, pump_water))
        elif cat == "boiler":
            if v1 < boiler_water:
                fail.append("boiler@%.1f,%.1f water=%d (dry)" % (cx, cy, v1))
            elif v2 < BOILER_STEAM_WARN:
                warn.append("boiler@%.1f,%.1f steam=%d/200 (cold or under-fuelled)"
                            % (cx, cy, v2))
        elif cat == "engine":
            if v1 < engine_energy:
                fail.append("engine@%.1f,%.1f energy=%d" % (cx, cy, v1))
            elif v2 <= 0:
                warn.append("engine@%.1f,%.1f generated_last_tick=0" % (cx, cy))
            if v3 > 0:
                nets.add(v3)
        elif cat == "inserter":
            if v1 not in INSERTER_OK_STATUS:
                warn.append("coal inserter@%.1f,%.1f status=%d (want 1 or 36)"
                            % (cx, cy, v1))
        elif cat == "belt":
            notes.append("coal dead-end items=%d" % v1)
            if v1 <= 0:
                warn.append("no coal on the belt's last tile - the spur is not delivering")
        elif cat == "pole":
            if v1 > 0:                              # poles report the network in v1
                nets.add(v1)
    if missing:
        fail.append("MISSING: %s" % ", ".join(missing[:6]))
    if len(nets) > 1:
        msg = ("engines/poles span %d electric networks %s - script-placed poles do "
               "NOT auto-connect; wire each pair within reach explicitly via "
               "get_wire_connector(defines.wire_connector_id.pole_copper,true)"
               ".connect_to(other,false) and re-read electric_network_id"
               % (len(nets), sorted(nets)))
        (fail if require_single_network else warn).append(msg)
    detail = " | ".join(([("FAIL: " + "; ".join(fail))] if fail else [])
                        + ([("warn: " + "; ".join(warn))] if warn else [])
                        + notes)
    return (not fail, detail or "ok")


def verify_record(rec):
    """buildplan verify_fn: the record carries its plant plan in args."""
    return verify(from_record(rec))


# --------------------------------------------------------------------------- pole wiring
def plan_poles(plan):
    """The plan's poles as (name, tile_x, tile_y), spurs then trunk."""
    return [(e["entity"], e["x"], e["y"]) for e in plan["entities"]
            if e["role"] in ("pole_spur", "pole_trunk")]


def wire_pairs(plan):
    """The pole pairs that MUST be wired EXPLICITLY after placement -> [((x1,y1),(x2,y2))].

    Pure. Script-placed poles do not reliably auto-connect (GOTCHAS 2026-08-30: two small
    poles 4.0 apart on different electric_network_ids - and 4.0 is this template's own spur
    pitch), so "within wire reach" is a planning fact, never a live one.

    Two passes, exactly as power_planner.wire_pairs does for a lattice:
      1. SPANNING, shortest first - every pair that joins two components. These links ARE
         the network and are never refused for degree.
      2. REDUNDANCY - any remaining in-reach pair whose BOTH ends are still under
         MAX_POLE_DEGREE, so a later neighbour can still be adopted.
    """
    poles = plan_poles(plan)
    if len(poles) < 2:
        return []
    reach = min(mine_layout.POLES[n]["wire"] for n, _x, _y in poles)
    cen = [center(n, x, y) for n, x, y in poles]
    cands = []
    for i in range(len(poles)):
        for j in range(i + 1, len(poles)):
            d = math.hypot(cen[i][0] - cen[j][0], cen[i][1] - cen[j][1])
            if d <= reach + 1e-9:
                cands.append((round(d, 6), i, j))
    cands.sort()
    parent = list(range(len(poles)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    deg = [0] * len(poles)
    out, taken = [], set()
    for _d, i, j in cands:                      # pass 1: spanning, degree never refuses
        a, b = find(i), find(j)
        if a == b:
            continue
        parent[a] = b
        deg[i] += 1
        deg[j] += 1
        taken.add((i, j))
        out.append(((poles[i][1], poles[i][2]), (poles[j][1], poles[j][2])))
    for _d, i, j in cands:                      # pass 2: redundancy, within the degree cap
        if (i, j) in taken:
            continue
        if deg[i] >= MAX_POLE_DEGREE or deg[j] >= MAX_POLE_DEGREE:
            continue
        deg[i] += 1
        deg[j] += 1
        out.append(((poles[i][1], poles[i][2]), (poles[j][1], poles[j][2])))
    return out


def wire_lua(pairs, pole="small-electric-pole"):
    """The /sc command(s) that wire `pairs` -> list of strings. RCON WRITE when run.

    Targets each pole by its CENTRE (correct for any pole footprint, unlike tile+0.5) and
    echoes made/already/missing: connect_to returns false when the pair is ALREADY
    connected, which is a success on a re-run, not a failure.
    """
    specs = []
    for a, b in pairs:
        ax, ay = center(pole, a[0], a[1])
        bx, by = center(pole, b[0], b[1])
        specs.append("%.1f,%.1f,%.1f,%.1f" % (ax, ay, bx, by))
    if not specs:
        return []
    head = ("/sc local s=game.surfaces[1]; local W=defines.wire_connector_id.pole_copper;"
            "local made,alr,miss=0,0,0;"
            "for a,b,c,d in ([==[")
    tail = ("]==]):gmatch('(-?[%d%.]+),(-?[%d%.]+),(-?[%d%.]+),(-?[%d%.]+)') do"
            "  local p=s.find_entities_filtered{type='electric-pole',"
            "    position={tonumber(a),tonumber(b)},radius=0.4}[1];"
            "  local q=s.find_entities_filtered{type='electric-pole',"
            "    position={tonumber(c),tonumber(d)},radius=0.4}[1];"
            "  if p and q then"
            "    local cp=p.get_wire_connector(W,true); local cq=q.get_wire_connector(W,true);"
            "    if cp and cq then"
            "      if cp.connect_to(cq,false) then made=made+1 else alr=alr+1 end"
            "    else miss=miss+1 end"
            "  else miss=miss+1 end end;"
            "rcon.print(made..'/'..alr..'/'..miss)")
    # batch by REAL byte length: a /sc past the cap truncates silently mid-entry.
    cmds, cur, size = [], [], 0
    overhead = len(head) + len(tail)
    for s in specs:
        add = len(s) + (1 if cur else 0)
        if cur and overhead + size + add > SPEC_BUDGET:
            cmds.append(head + ";".join(cur) + tail)
            cur, size, add = [], 0, len(s)
        cur.append(s)
        size += add
    if cur:
        cmds.append(head + ";".join(cur) + tail)
    return cmds


def pole_networks(plan, state=None):
    """RCON READ ONLY -> {"poles": sorted ids, "engines": sorted ids, "all": sorted ids}.

    THE verification the gotcha demands: never "placement implies connection", always read
    electric_network_id back and compare. Poles report their id in v1, engines in v3.
    """
    if state is None:
        state = read_state(plan)
    cats = _categories(plan)
    pole_ids, eng_ids = set(), set()
    for (name, _cx, _cy), v in state.items():
        cat = cats.get(name)
        if cat == "pole" and v[0] > 0:
            pole_ids.add(v[0])
        elif cat == "engine" and v[2] > 0:
            eng_ids.add(v[2])
    return {"poles": sorted(pole_ids), "engines": sorted(eng_ids),
            "all": sorted(pole_ids | eng_ids)}


def wire_poles(plan, check=True):
    """Wire every planned pole pair EXPLICITLY, then verify by electric_network_id.

    RCON WRITE (through _wire_poles) + one READ. Idempotent: an already-connected pair is
    reported as `already`, so this is safe to re-run every verify attempt - which is what
    build() does, so the retry loop repairs a missed wire instead of tearing the plant out.
    Returns {"pairs", "made", "already", "missing", "raw", "networks", "ok"}.
    """
    pairs = wire_pairs(plan)
    pole = (plan.get("params") or {}).get("pole")
    out = {"pairs": [list(a) + list(b) for a, b in pairs],
           "made": 0, "already": 0, "missing": 0, "raw": [], "networks": None}
    for cmd in wire_lua(pairs, pole or "small-electric-pole"):
        raw = str(_wire_poles(cmd) or "").strip()
        out["raw"].append(raw)
        m = re.match(r"^(\d+)/(\d+)/(\d+)$", raw)
        if m:
            out["made"] += int(m.group(1))
            out["already"] += int(m.group(2))
            out["missing"] += int(m.group(3))
    if check and pole:
        out["networks"] = pole_networks(plan)
        out["ok"] = len(out["networks"]["all"]) <= 1
    else:
        out["ok"] = out["missing"] == 0
    return out


# --------------------------------------------------------------------------- buildplan
def _jsonable(plan):
    """Tuples -> lists, and drop anything unserialisable, so the plan round-trips through
    the buildplan record file (which IS the audit trail)."""
    out = {}
    for k, v in plan.items():
        if isinstance(v, tuple):
            out[k] = list(v)
        elif k == "rects":
            out[k] = [list(r) for r in v]
        elif k == "intake":
            out[k] = {kk: (list(vv) if isinstance(vv, tuple) else vv)
                      for kk, vv in v.items()}
        elif k == "params":
            out[k] = {kk: (list(vv) if isinstance(vv, tuple) else vv)
                      for kk, vv in v.items()}
        else:
            out[k] = v
    return out


def from_record(rec):
    """The plant plan a buildplan record was built from (positions back to tuples)."""
    p = dict((rec.get("args") or {}).get("plant") or {})
    if not p:
        raise PlantError("buildplan record %s carries no plant plan in args"
                         % rec.get("id"))
    for k in ("anchor", "pump", "bbox"):
        if k in p:
            p[k] = tuple(p[k])
    return p


def plan_tiles(plan):
    """[[x, y, direction]] keyed by each entity's KEY TILE (floor of its centre) - the only
    keying buildplan.probe / _default_remove agree with. See the module docstring."""
    return [list(key_tile(e["entity"], e["x"], e["y"])) + [e["direction"]]
            for e in _ordered(plan["entities"])]


def to_buildplan(plan, scan_tick=None, plan_id=None):
    """Wrap a plant plan in a buildplan record (status "planned"; nothing is placed)."""
    v = validate(plan)
    if not v["ok"]:
        raise PlantError("refusing to submit an invalid plant plan: %s"
                         % "; ".join(v["errors"][:4]))
    args = {"plant": _jsonable(plan),
            "entities": [{"entity": e["entity"], "x": e["x"], "y": e["y"],
                          "direction": e["direction"], "role": e["role"],
                          "key": list(key_tile(e["entity"], e["x"], e["y"]))}
                         for e in _ordered(plan["entities"])]}
    return buildplan.new_plan(KIND, args, plan_tiles(plan), scan_tick=scan_tick,
                              names=sorted(bom(plan)), id=plan_id)


def _entity_map(rec):
    return {(e["key"][0], e["key"][1]): e
            for e in ((rec.get("args") or {}).get("entities") or [])}


def place(rec, tiles):
    """buildplan place_fn. RCON WRITE (through _place_entity).

    Clears trees/rocks over the whole plant zone ONCE, then places every tile with clear=0:
    autopilot.place's own clearspace pass ABORTS on a cliff anywhere in its radius even when
    the footprint itself is placeable, and at a shore that fires constantly (GOTCHAS, the
    bootstrap plant). A cliff inside the zone is a siting error site_valid() already refuses.
    """
    emap = _entity_map(rec)
    placed, already, failed = [], [], []
    if not tiles:
        return {"placed": placed, "already": already, "failed": failed}
    plant = from_record(rec)
    x1, y1, x2, y2 = plant["bbox"]
    _clear_area((x1 + x2) // 2, (y1 + y2) // 2,
                max(x2 - x1, y2 - y1) // 2 + 10)   # footprint + the standing 10-tile pad
    for t in tiles:
        key = (int(t[0]), int(t[1]))
        e = emap.get(key)
        if e is None:
            failed.append({"tile": key, "reason": "no entity planned for key tile"})
            continue
        out = str(_place_entity(e["entity"], e["x"], e["y"], e["direction"]) or "")
        if out.startswith("BUILT"):
            placed.append(key)
        elif out.startswith("ALREADY"):
            already.append(key)
        else:
            failed.append({"tile": key, "reason": "%s: %s" % (e["entity"], out[:100])})
    # Placement does NOT imply connection: wire every planned pole pair explicitly and read
    # electric_network_id back (GOTCHAS 2026-08-30 - two small poles 4.0 apart, which is
    # this template's spur pitch, sat on different networks). Extra keys are ignored by
    # buildplan.apply, which reads only placed/already/failed.
    wired = None
    if plant.get("params", {}).get("pole"):
        wired = wire_poles(plant)
    return {"placed": placed, "already": already, "failed": failed, "wired": wired}


def build(plan, scan_tick=None, tries=6, delay=5, fuel_coal=25, rollback_on_fail=True,
          force=False, place_fn=None, verify_fn=None):
    """plan -> apply -> functional fluid/energy check -> verified | rollback+failed.

    All four buildplan gates apply, in order: the operator truce, staleness, the protected
    tile ledger, then crash-safe "applying" on disk before the first placement. The check is
    verify() (pump 100 / boiler water > 0 / engine energy > 0); on failure buildplan rolls
    back exactly what THIS plan placed, refunding, and marks the record failed.

    fuel_coal>0 hand-seeds each boiler ONCE, before the first check, so a brand-new plant
    can reach energy > 0 without waiting on the coal spur. 25 coal = 100 MJ = ~55 s at
    1.8 MW, comfortably longer than tries*delay, and seeding once (not per poll) keeps a
    retry loop from draining the inventory. Set 0 once the spur is belted.
    """
    rec = to_buildplan(plan, scan_tick=scan_tick)
    seeded = {"done": False}

    def _verify(r):
        if fuel_coal and not seeded["done"]:
            seeded["done"] = True
            _fuel_boilers([center("boiler", e["x"], e["y"])
                           for e in plan["entities"] if e["role"] == "boiler"], fuel_coal)
        # RE-wire before every attempt: connect_to on an already-connected pair is a no-op,
        # so the retry loop REPAIRS a missed wire ("adjust rather than revert", 2026-08-30)
        # and only a split that survives every attempt rolls the plant back.
        if (plan.get("params") or {}).get("pole"):
            wire_poles(plan, check=False)
        return (verify_fn or verify_record)(r)

    return buildplan.apply(rec, place_fn=place_fn or place, verify_fn=_verify,
                           tries=tries, delay=delay, rollback_on_fail=rollback_on_fail,
                           force=force)


def register():
    """Register the kind so buildplan.resume() can re-verify a crashed plant build with no
    caller context. remove=None -> buildplan._default_remove, which is correct here BECAUSE
    the plan is keyed by floored centres (module docstring)."""
    return buildplan.register(KIND, place=place, verify=verify_record, remove=None)


register()


# --------------------------------------------------------------------------- RCON writes
# Lazily imported and monkeypatchable: offline tests replace these three and never import
# autopilot (which opens the ledger files and talks to the server).
def _place_entity(name, tile_x, tile_y, direction):
    """RCON WRITE. Returns autopilot.place's status string ('BUILT ...' on success)."""
    import autopilot
    return autopilot.place(name, tile_x, tile_y, direction=direction, clear=0)


def _clear_area(cx, cy, radius):
    """RCON WRITE. Trees/rocks only; returns (removed, cliffs)."""
    import autopilot
    return autopilot.clear_area(cx, cy, radius)


def _wire_poles(cmd):
    """RCON WRITE. Issue one pole-wiring /sc built by wire_lua; returns 'made/already/miss'.
    A wire is not a build - it places nothing and can only ever JOIN two poles this plan
    already owns - but it is a write, so it lives here with the other three."""
    return rcon.run(cmd)


def _fuel_boilers(centres, coal):
    """RCON WRITE. Hand-seed coal into the boilers so a cold plant can be verified."""
    if not centres:
        return ""
    spec = ";".join("%.1f,%.1f" % (cx, cy) for cx, cy in centres)
    return rcon.run(
        "/sc local p=storage.derpface; local s=p.surface;"
        "local inv=(p and p.valid) and p.get_main_inventory() or nil; local n=0;"
        "for a,b in ([==[" + spec + "]==]):gmatch('(-?[%d%.]+),(-?[%d%.]+)') do"
        "  local e=s.find_entities_filtered{name='boiler',"
        "    position={tonumber(a),tonumber(b)},radius=0.6}[1];"
        "  if e and inv then"
        "    local c=math.min(" + str(int(coal)) + ",inv.get_item_count('coal'));"
        "    if c>0 then e.insert{name='coal',count=c};"
        "      inv.remove{name='coal',count=c}; n=n+1 end end end;"
        "rcon.print(n)")


if __name__ == "__main__":
    import json as _json
    p = plan_plant(4, water_hint=(-32, 51))
    print(_json.dumps({"anchor": p["anchor"], "n_columns": p["n_columns"],
                       "power_MW": p["power_MW"], "bom": p["bom"],
                       "validate": validate(p), "warnings": p["warnings"]},
                      indent=2, default=str))
