#!/usr/bin/env python3
"""Mining-outpost LAYOUT PLANNER: a COMPLETE plan before anything is placed.

Modelled on GusevP/mineore-factorio-mod (MIT, tagged 2.0/2.1) - scripts/calculator.lua,
placer.lua, belt_placer.lua, pole_placer.lua. What is ported is the STRUCTURE: paired drill
rows facing each other with the collection belt in the gap (calculator.calculate_positions:
EW layout -> top row faces south, bottom row faces north, belt on the gap centreline), and
placer._filter_belt_lines' rule that the belt is rebuilt inside the SURVIVING drills' span
after unviable drills are dropped, so no orphan lane tile is ever planned.

Deliberately NOT ported: resource_scanner's grouping (it groups by entity name with no
flood fill and no minimum-coverage rule) and pole_placer's fixed-interval pole spacing.
Our patches are mixed - one probed 44x34 bbox here holds stone=101 AND coal=309 - so a
foreign-ore veto is mandatory, and a fixed interval is not minimal and is what once put a
pole line ON the mine's belt row (GOTCHAS "PLAN, then place"). Poles here are a minimum
interval point cover on the two rows immediately OUTSIDE the drill rows.

THE THING THIS EXISTS TO PREVENT (GOTCHAS "Swapping entity TIERS moves the drop tile"):
electrify_mines swapped 2x2 burner drills for 3x3 electric ones AT THE SAME POSITION; the
bigger footprint moved drop_position and six copper drills dumped ore onto bare ground while
every status read looked plausible. So: plan in TOP-LEFT TILES, re-derive the centre per
prototype, and never reuse a centre across a tier swap. replan() does exactly that.

Drop-position math, engine-probed on 2.1.17 (read-only) and confirmed against live drills:
    centre    = (tile_x + tw/2, tile_y + th/2)
    drop      = centre + rot(vector_to_place_result, direction)
    drop_tile = (floor(drop.x), floor(drop.y))
Live check: electric drill centre (-42.5,13.5) dir=8 -> drop (-42.5,15.348) -> tile (-43,15);
its partner at centre (-42.5,17.5) dir=0 -> drop (-42.5,15.652) -> tile (-43,15). Same lane
row. (The engine clamps the drop ~0.0023 tiles onto the collision-box edge vs the prototype
vector; that never changes floor() at these alignments, so the prototype vector is used.)

Row rule (tier-INDEPENDENT): top row top-left y = lane_y - th facing 8/south; bottom row
top-left y = lane_y + 1 facing 0/north. Both land the drop on lane_y for 2x2, 3x3 and 5x5.
Column rule is NOT tier-independent: the burner's vector has x=-0.35, so a north-facing
burner drops on its LEFT column and a south-facing burner on its RIGHT column, while
electric/big drop on their centre column. Even tw -> centre on an integer, odd tw -> centre
on .5; that half-tile is the other half of the copper failure.

RCON: pure except scan_patch() and probe_prototypes(), which are READ ONLY (find_entities
_filtered + prototype reads via the chunked storage._world protocol). Nothing here creates,
destroys, rotates or ghosts anything - to_orders() hands shapes to executor, which places.
"""
import math

import rcon

CHUNK = 3000                      # chars per RCON read slice (large responses truncate)

N, E, S, W = 0, 4, 8, 12          # 2.0/2.1 directions

# Engine-probed 2.1.17 (read-only, 2026-08-29). `vec` is vector_to_place_result at
# direction 0; `radius` is mining_drill_radius (mining area = a square of side 2*radius
# centred on the drill). Refresh with probe_prototypes(); do NOT read these from
# lua/fle_lib.lua, whose vendored table is wrong (see POLES).
DRILLS = {
    "burner-mining-drill":   {"tw": 2, "th": 2, "vec": (-0.35, -1.30), "radius": 0.99},
    "electric-mining-drill": {"tw": 3, "th": 3, "vec": (0.0, -1.85),   "radius": 2.49},
    "big-mining-drill":      {"tw": 5, "th": 5, "vec": (0.0, -2.85),   "radius": 6.49},
}

# Engine-probed 2.1.17. NOTE: on 2.1 the plain properties pole.supply_area_distance /
# pole.max_wire_distance THROW - only get_supply_area_distance() / get_max_wire_distance()
# work. lua/fle_lib.lua:32-33 hardcodes small=4 and big=30; both are wrong, which is why
# this module carries its own table and never consults fle.wire_reach.
POLES = {
    "small-electric-pole":  {"supply": 2.5, "wire": 7.5, "tw": 1, "th": 1},
    "medium-electric-pole": {"supply": 3.5, "wire": 9.0, "tw": 1, "th": 1},
    "big-electric-pole":    {"supply": 2.0, "wire": 32.0, "tw": 2, "th": 2},
    "substation":           {"supply": 9.0, "wire": 18.0, "tw": 2, "th": 2},
}

# Footprints for everything else the planner emits (all 1x1 on 2.1).
SIZES = {"transport-belt": (1, 1), "fast-transport-belt": (1, 1),
         "express-transport-belt": (1, 1), "inserter": (1, 1), "burner-inserter": (1, 1),
         "fast-inserter": (1, 1), "long-handed-inserter": (1, 1), "bulk-inserter": (1, 1),
         "wooden-chest": (1, 1), "iron-chest": (1, 1), "steel-chest": (1, 1)}


class LayoutError(Exception):
    """The patch cannot carry a viable outpost with the requested parts."""


def size_of(name):
    if name in DRILLS:
        return DRILLS[name]["tw"], DRILLS[name]["th"]
    if name in POLES:
        return POLES[name]["tw"], POLES[name]["th"]
    return SIZES.get(name, (1, 1))


# --------------------------------------------------------------------------- geometry
def rot(vec, direction):
    """Rotate an offset by a cardinal direction (screen coords, y down): 4/east is 90 deg
    clockwise. Confirmed live: electric vec (0,-1.85) at dir 8 -> (0,+1.85), and the drill
    at centre (-42.5,13.5) really drops at (-42.5,15.35)."""
    x, y = vec
    d = int(direction) % 16
    if d == N:
        return (x, y)
    if d == E:
        return (-y, x)
    if d == S:
        return (-x, -y)
    if d == W:
        return (y, -x)
    raise ValueError("drills only take cardinal directions 0/4/8/12, got %r" % (direction,))


def center(name, tile_x, tile_y):
    """Entity CENTRE from its top-left footprint tile. Even width -> integer centre,
    odd width -> .5. Never reuse a centre across a tier swap; re-derive it here."""
    tw, th = size_of(name)
    return (tile_x + tw / 2.0, tile_y + th / 2.0)


def drop_tile(drill, tile_x, tile_y, direction):
    """The tile a drill's output lands on, from its TOP-LEFT tile. This is the single
    number the whole plan turns on."""
    spec = DRILLS[drill]
    cx, cy = tile_x + spec["tw"] / 2.0, tile_y + spec["th"] / 2.0
    ox, oy = rot(spec["vec"], direction)
    return (math.floor(cx + ox), math.floor(cy + oy))


def mining_area(drill, tile_x, tile_y):
    """Inclusive tile rect (x1,y1,x2,y2) the drill can mine: a square of side 2*radius
    around its centre. electric -> 5x5, burner -> 2x2, big -> 13x13."""
    spec = DRILLS[drill]
    cx, cy = tile_x + spec["tw"] / 2.0, tile_y + spec["th"] / 2.0
    r = spec["radius"]
    return (math.floor(cx - r), math.floor(cy - r),
            math.ceil(cx + r) - 1, math.ceil(cy + r) - 1)


def footprint(name, tile_x, tile_y):
    tw, th = size_of(name)
    return {(tile_x + i, tile_y + j) for i in range(tw) for j in range(th)}


# --------------------------------------------------------------------------- patch scan
def _chunked(build_lua):
    """Store a string in storage._world, print its length, read it back in slices (the
    architect.py/world.py pattern - one large RCON response truncates)."""
    n = int((rcon.run("/sc " + build_lua) or "0").strip() or "0")
    if n <= 0:
        return ""
    parts, i = [], 1
    while i <= n:
        parts.append(rcon.run("/sc rcon.print(storage._world:sub(%d,%d))"
                              % (i, i + CHUNK - 1)).rstrip("\r\n"))
        i += CHUNK
    return "".join(parts)


def scan_patch(ore, cx, cy, radius=40):
    """READ-ONLY resource read around (cx,cy) -> {ore, tiles:{(x,y):amount}, bbox,
    foreign:{(x,y):name}}. world.scan_area filters force='player' and so never returns
    resources, hence this separate read. Every other resource name in the box lands in
    `foreign` - the veto that keeps a drill off a stone/coal boundary."""
    x1, y1 = int(cx - radius), int(cy - radius)
    x2, y2 = int(cx + radius), int(cy + radius)
    lua = ("local s=game.surfaces[1]; local acc={};"
           "for _,e in pairs(s.find_entities_filtered{area={{%d,%d},{%d,%d}},type='resource'}) do"
           "  local n=e.name; local b=acc[n]; if not b then b={} acc[n]=b end;"
           "  b[#b+1]=math.floor(e.position.x)..','..math.floor(e.position.y)..','..math.floor(e.amount)"
           " end;"
           "local out={}; for n,b in pairs(acc) do out[#out+1]=n..'|'..table.concat(b,' ') end;"
           "storage._world=table.concat(out,'\\n'); rcon.print(#storage._world)"
           % (x1, y1, x2, y2))
    return parse_patch(ore, _chunked(lua))


def parse_patch(ore, raw):
    """Parse scan_patch's wire format ("name|x,y,amt x,y,amt\\nname|...")."""
    tiles, foreign = {}, {}
    for line in (raw or "").split("\n"):
        name, sep, rest = line.partition("|")
        if not sep:
            continue
        for rec in rest.split():
            f = rec.split(",")
            if len(f) < 2:
                continue
            t = (int(f[0]), int(f[1]))
            if name == ore:
                tiles[t] = int(f[2]) if len(f) > 2 else 0
            else:
                foreign[t] = name
    xs = [t[0] for t in tiles]
    ys = [t[1] for t in tiles]
    bbox = (min(xs), min(ys), max(xs), max(ys)) if tiles else None
    return {"ore": ore, "tiles": tiles, "bbox": bbox, "foreign": foreign}


def probe_prototypes():
    """READ-ONLY refresh of DRILLS/POLES from the live server -> {drills, poles, notes}.
    2.1 caveats baked in: vector_to_place_result may be array-form, and the pole reach
    PROPERTIES throw (only the get_*() methods work), so both are pcall'd."""
    lua = ("local o={};"
           "for _,n in ipairs{" + ",".join("'%s'" % d for d in DRILLS) + "} do"
           "  local p=prototypes.entity[n];"
           "  if p then local vx,vy=0,0; pcall(function() local v=p.vector_to_place_result;"
           "      vx=v.x or v[1]; vy=v.y or v[2] end);"
           "    local r=0; pcall(function() r=p.mining_drill_radius end);"
           "    o[#o+1]='D|'..n..'|'..p.tile_width..'|'..p.tile_height..'|'..vx..'|'..vy..'|'..r"
           "  else o[#o+1]='D|'..n..'|MISSING' end end;"
           "for _,n in ipairs{" + ",".join("'%s'" % p for p in POLES) + "} do"
           "  local p=prototypes.entity[n];"
           "  if p then local s,w=0,0; pcall(function() s=p.get_supply_area_distance() end);"
           "    pcall(function() w=p.get_max_wire_distance() end);"
           "    o[#o+1]='P|'..n..'|'..p.tile_width..'|'..p.tile_height..'|'..s..'|'..w"
           "  else o[#o+1]='P|'..n..'|MISSING' end end;"
           "rcon.print(table.concat(o,';'))")
    out = {"drills": {}, "poles": {}, "notes": []}
    for rec in ((rcon.run("/sc " + lua) or "").strip().split(";")):
        f = rec.strip().split("|")
        if len(f) < 3:
            continue
        if f[2] == "MISSING":
            out["notes"].append("%s absent from this build" % f[1])
            continue
        if f[0] == "D" and len(f) >= 7:
            got = {"tw": int(f[2]), "th": int(f[3]),
                   "vec": (float(f[4]), float(f[5])), "radius": float(f[6])}
            out["drills"][f[1]] = got
            if DRILLS.get(f[1]) != got:
                out["notes"].append("baked %s %s != probed %s" % (f[1], DRILLS.get(f[1]), got))
        elif f[0] == "P" and len(f) >= 6:
            got = {"supply": float(f[4]), "wire": float(f[5]),
                   "tw": int(f[2]), "th": int(f[3])}
            out["poles"][f[1]] = got
            if POLES.get(f[1]) != got:
                out["notes"].append("baked %s %s != probed %s" % (f[1], POLES.get(f[1]), got))
    return out


# --------------------------------------------------------------------------- coverage
def _prefix(points, x0, y0, w, h):
    """2D prefix-sum over a boolean tile set so drill viability is O(1) per candidate
    (a 40-radius auto-lane sweep is otherwise ~16M tile lookups)."""
    g = [[0] * (w + 1) for _ in range(h + 1)]
    for (x, y) in points:
        i, j = x - x0, y - y0
        if 0 <= i < w and 0 <= j < h:
            g[j + 1][i + 1] = 1
    for j in range(1, h + 1):
        row, prev, acc = g[j], g[j - 1], 0
        for i in range(1, w + 1):
            acc += row[i]
            row[i] = acc + prev[i]
    return g


class _Field:
    """Prefix-summed tile set clipped to a window; rect() counts hits in an inclusive rect."""

    def __init__(self, points, x0, y0, w, h):
        self.x0, self.y0, self.w, self.h = x0, y0, w, h
        self.g = _prefix(points, x0, y0, w, h)

    def rect(self, x1, y1, x2, y2):
        a = max(0, x1 - self.x0)
        b = max(0, y1 - self.y0)
        c = min(self.w, x2 - self.x0 + 1)
        d = min(self.h, y2 - self.y0 + 1)
        if a >= c or b >= d:
            return 0
        g = self.g
        return g[d][c] - g[b][c] - g[d][a] + g[b][a]


# --------------------------------------------------------------------------- planner
def plan_outpost(patch, *, drill="electric-mining-drill", pole="small-electric-pole",
                 belt="transport-belt", lane_y=None, max_drills=None, min_ore_tiles=4,
                 output=("inserter", "wooden-chest"), grid_anchor=None):
    """Plan one horizontal (east-flowing) mining lane over `patch`.

    Returns a plan dict; nothing is placed. Coordinates in `entities` are TOP-LEFT TILES,
    the same convention as autopilot.place / executor's place order - centres are derived
    per prototype at emit time so a tier swap can never inherit a stale centre.

    lane_y=None sweeps every row of the patch and keeps the one carrying the most viable
    drills. Every drill is vetoed unless its mining area holds >= min_ore_tiles of `ore`
    and ZERO foreign ore. output=None skips the inserter+chest hookup.

    SCOPE: this plans against ORE, not against emptiness. It knows nothing about entities
    already standing on the patch, water, cliffs or operator-protected tiles. Before
    submitting to_orders(), diff the plan against world.query(role="mine") / scan_area and
    the protected-tile ledger (BUILD LAW 3: operator deletions are final), and let
    executor's _op_place do the clearspace + cliff-abort check per tile.
    """
    if drill not in DRILLS:
        raise LayoutError("unknown drill %r (have %s)" % (drill, ", ".join(sorted(DRILLS))))
    if pole is not None and pole not in POLES:
        raise LayoutError("unknown pole %r (have %s)" % (pole, ", ".join(sorted(POLES))))
    if not patch.get("tiles"):
        raise LayoutError("empty patch: nothing to mine")

    spec = DRILLS[drill]
    tw, th = spec["tw"], spec["th"]
    ore = patch["ore"]
    x1, y1, x2, y2 = patch["bbox"]
    pad = int(math.ceil(spec["radius"])) + max(tw, th) + 2
    fx, fy = x1 - pad, y1 - pad
    fw, fh = (x2 - x1 + 1) + 2 * pad, (y2 - y1 + 1) + 2 * pad
    ore_f = _Field(patch["tiles"], fx, fy, fw, fh)
    for_f = _Field(patch.get("foreign") or {}, fx, fy, fw, fh)

    def cover(tx, ty):
        """Ore tiles a drill at (tx,ty) would mine, or None if vetoed."""
        a, b, c, d = mining_area(drill, tx, ty)
        if for_f.rect(a, b, c, d):
            return None                      # foreign ore in the mining area: GOTCHAS veto
        n = ore_f.rect(a, b, c, d)
        return n if n >= min_ore_tiles else None

    lanes = [lane_y] if lane_y is not None else list(range(y1 - th, y2 + th + 1))
    best = None
    for ly in lanes:
        picked = _pick_rows(drill, ly, fx, fx + fw - 1, cover)
        if not picked:
            continue
        score = (len(picked), sum(p["ore"] for p in picked), -abs(ly * 2 - (y1 + y2)))
        if best is None or score > best[0]:
            best = (score, ly, picked)
    if best is None:
        raise LayoutError("no viable %s row over %s patch bbox %s (min_ore_tiles=%d, "
                          "foreign-ore veto on)" % (drill, ore, patch["bbox"], min_ore_tiles))
    _score, lane_y, drills = best

    if max_drills is not None and len(drills) > max_drills:
        drills = sorted(sorted(drills, key=lambda d: -d["ore"])[:max_drills],
                        key=lambda d: (d["x"], d["side"]))

    # mineore placer._filter_belt_lines: the lane is rebuilt inside the SURVIVING drills'
    # span, so a dropped drill never leaves an orphan belt tile hanging off the end.
    drops = [drop_tile(drill, d["x"], d["y"], d["dir"]) for d in drills]
    span_start = min(dx for dx, _dy in drops) - 1
    span_end = max(dx for dx, _dy in drops)

    ents = []
    for d, (dx, dy) in zip(drills, drops):
        ents.append({"entity": drill, "x": d["x"], "y": d["y"], "direction": d["dir"],
                     "role": "drill", "side": d["side"], "ore": d["ore"], "drop": (dx, dy)})
    lane_tiles = {(x, lane_y) for x in range(span_start, span_end + 1)}
    for x in range(span_start, span_end + 1):
        ents.append({"entity": belt, "x": x, "y": lane_y, "direction": E, "role": "lane"})

    hookup = []
    if output:
        ins, chest = output
        # Inserter direction is its PICKUP side (GOTCHAS, confirmed across 3 examples):
        # dir 12/west picks from the belt tile to its west and drops east into the chest.
        hookup = [{"entity": ins, "x": span_end + 1, "y": lane_y, "direction": W,
                   "role": "output"},
                  {"entity": chest, "x": span_end + 2, "y": lane_y, "direction": N,
                   "role": "output"}]
        ents.extend(hookup)

    blocked = set(lane_tiles)
    for e in ents:
        if e["role"] in ("drill", "output"):
            blocked |= footprint(e["entity"], e["x"], e["y"])
    # pole=None is the right call for a burner mine: burner drills take coal, not power,
    # and a pole line that powers nothing is BUILD LAW 1 ("never build anything that
    # doesn't do something"). The coal feed loop is a separate build (GOTCHAS captured
    # layout: coal belts outside both drill rows plus a fuel inserter per drill).
    warnings, poles = [], []
    if pole is not None:
        poles, warnings = _plan_poles(drills, drill, pole, lane_y, blocked, grid_anchor)
    ents.extend(poles)

    plan = {
        "ore": ore, "drill": drill, "pole": pole, "belt": belt,
        "lane_y": lane_y, "lane_span": (span_start, span_end),
        "lane_tiles": lane_tiles, "entities": ents, "warnings": warnings,
        "patch": patch,
        "params": {"pole": pole, "belt": belt, "max_drills": max_drills,
                   "min_ore_tiles": min_ore_tiles, "output": output,
                   "grid_anchor": grid_anchor},
    }
    plan["bom"] = bom(plan)
    return plan


def _pick_rows(drill, lane_y, xlo, xhi, cover):
    """Both drill rows for one lane. Row rule is tier-independent: top row top-left
    y = lane_y - th facing SOUTH, bottom row top-left y = lane_y + 1 facing NORTH - both
    put the drop on lane_y for 2x2, 3x3 and 5x5. Within a row the non-overlapping subset
    that maximises ore coverage is chosen by DP (greedy left-to-right misses the best
    phase offset on a ragged patch)."""
    th = DRILLS[drill]["th"]
    out = []
    for side, ty, d in (("top", lane_y - th, S), ("bottom", lane_y + 1, N)):
        for tx, n in _best_row(drill, ty, xlo, xhi, cover):
            out.append({"x": tx, "y": ty, "dir": d, "side": side, "ore": n})
    return sorted(out, key=lambda p: (p["x"], p["side"]))


def _best_row(drill, ty, xlo, xhi, cover):
    """Max-coverage set of non-overlapping drills along one row -> [(tx, ore_tiles)]."""
    tw = DRILLS[drill]["tw"]
    xs = list(range(xlo, xhi + 1))
    if not xs:
        return []
    cov = [cover(x, ty) for x in xs]
    best = [0] * (len(xs) + 1)
    take = [False] * len(xs)
    for i in range(len(xs) - 1, -1, -1):
        skip = best[i + 1]
        use = (cov[i] + best[min(len(xs), i + tw)]) if cov[i] else -1
        if use > skip:
            best[i], take[i] = use, True
        else:
            best[i] = skip
    res, i = [], 0
    while i < len(xs):
        if take[i]:
            res.append((xs[i], cov[i]))
            i += tw
        else:
            i += 1
    return res


# --------------------------------------------------------------------------- poles
def _supply_x_range(pole, py, drect):
    """Integer x range for a pole whose top-left row is `py` such that its supply square
    overlaps the drill rect (x, y, x+tw, y+th). None if that row cannot reach at all."""
    s = POLES[pole]["supply"]
    pw, ph = POLES[pole]["tw"], POLES[pole]["th"]
    dx, dy, dtw, dth = drect
    pcy = py + ph / 2.0
    if not (pcy - s < dy + dth and pcy + s > dy):
        return None
    lo = math.floor(dx - s - pw / 2.0) + 1
    hi = math.ceil(dx + dtw + s - pw / 2.0) - 1
    return (lo, hi) if lo <= hi else None


def _plan_poles(drills, drill, pole, lane_y, blocked, grid_anchor):
    """Minimum pole count that powers every drill, never on the lane.

    Pole rows sit immediately OUTSIDE the drill rows (a 2x2 pole is pushed out by its own
    height so it cannot clip the drill row). For a fixed row each drill yields the x
    interval of poles that reach it; the minimum stabbing set of intervals - sort by right
    endpoint, stab at the smallest uncovered right endpoint - is provably the minimum
    number of poles. mineore's fixed `interval = floor(effective_reach / drill_spacing)`
    is not minimal, and a fixed grid is what put a pole line on the belt row before."""
    tw, th = DRILLS[drill]["tw"], DRILLS[drill]["th"]
    wire, ph = POLES[pole]["wire"], POLES[pole]["th"]
    rows = (("top", lane_y - th - ph), ("bottom", lane_y + th + 1))
    out, warn = [], []
    for side, py in rows:
        ivs = []
        for d in [q for q in drills if q["side"] == side]:
            r = _supply_x_range(pole, py, (d["x"], d["y"], tw, th))
            if r is None:
                warn.append("no %s in row y=%d can power the %s drill at (%d,%d)"
                            % (pole, py, side, d["x"], d["y"]))
                continue
            ivs.append(r)
        for px in _stab(ivs):
            _emit_pole(out, pole, px, py, blocked, warn)
    # wire pass: nothing in a row may sit further than max_wire_distance from its neighbour
    for _side, py in rows:
        row = sorted(q["x"] for q in out if q["y"] == py)
        for a, b in zip(row, row[1:]):
            gap = b - a
            if gap > wire:
                for k in range(1, int(math.ceil(gap / wire))):
                    _emit_pole(out, pole, a + int(k * wire), py, blocked, warn)
    _connect(out, pole, blocked, warn, grid_anchor)
    return out, warn


def _emit_pole(out, pole, px, py, blocked, warn, radius=3):
    """Place one pole at (px,py), nudged to the nearest legal tile if that footprint is
    taken. Never on the lane and never on a drill - `blocked` holds both."""
    for cx, cy in _spiral(px, py, radius):
        if not (footprint(pole, cx, cy) & blocked):
            out.append({"entity": pole, "x": cx, "y": cy, "direction": N, "role": "pole"})
            blocked |= footprint(pole, cx, cy)
            return out[-1]
    warn.append("no legal tile for a %s near (%d,%d)" % (pole, px, py))
    return None


def _spiral(x, y, r):
    """Expanding Chebyshev rings around (x,y), nearest first."""
    yield (x, y)
    for d in range(1, r + 1):
        for i in range(-d, d + 1):
            yield (x + i, y - d)
            yield (x + i, y + d)
        for j in range(-d + 1, d):
            yield (x - d, y + j)
            yield (x + d, y + j)


def _dist(a, b):
    return math.hypot((a["x"] + 0.5) - (b["x"] + 0.5), (a["y"] + 0.5) - (b["y"] + 0.5))


def _connect(poles, pole, blocked, warn, grid_anchor):
    """One electric network: join pole components - and the caller's grid anchor, which is
    an EXISTING pole and so is a virtual node, never re-placed.

    A bridge is NOT a straight-line walk. Between two drill rows the straight line IS the
    belt lane, so the only legal bridge is around the end of the rows - a greedy step
    toward the other component just piles poles against the lane forever. So each bridge
    is a breadth-first search over legal pole tiles, hopping at most max_wire_distance per
    step, which finds the fewest-poles detour or proves there is none."""
    wire = POLES[pole]["wire"]
    nodes = list(poles)
    if grid_anchor:
        nodes.append({"entity": pole, "x": int(grid_anchor[0]), "y": int(grid_anchor[1]),
                      "direction": N, "role": "anchor"})
    if not nodes:
        return
    r = int(math.ceil(wire)) + 2
    region = (min(n["x"] for n in nodes) - r, min(n["y"] for n in nodes) - r,
              max(n["x"] for n in nodes) + r, max(n["y"] for n in nodes) + r)
    for _ in range(12):
        comps = _components(nodes, wire)
        if len(comps) <= 1:
            return
        comps.sort(key=len, reverse=True)
        path = _bridge_path(comps[0], comps[1], pole, blocked, region)
        if not path:
            a, b = comps[0][0], comps[1][0]
            warn.append("pole network stays split: no %s detour joins (%d,%d) to (%d,%d)"
                        % (pole, a["x"], a["y"], b["x"], b["y"]))
            return
        for (px, py) in path:
            p = _emit_pole(poles, pole, px, py, blocked, warn, radius=0)
            if p is None:
                return
            nodes.append(p)


def _bridge_path(src, dst, pole, blocked, region):
    """Fewest legal pole tiles that join node set `src` to node set `dst` -> [(x,y)] (may
    be empty if one hop already reaches). BFS over the legal tiles in `region`; every step
    is <= max_wire_distance. Same-type poles share a footprint, so centre distance is just
    the tile distance."""
    wire = POLES[pole]["wire"]
    x0, y0, x1, y1 = region
    if (x1 - x0) * (y1 - y0) > 200 * 200:
        return None                          # anchor is absurdly far; caller warns
    legal = {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)
             if not (footprint(pole, x, y) & blocked)}
    w2 = wire * wire
    disc = [(dx, dy) for dx in range(-int(wire), int(wire) + 1)
            for dy in range(-int(wire), int(wire) + 1) if dx * dx + dy * dy <= w2]

    def reaches(t, nodes):
        return any((t[0] - n["x"]) ** 2 + (t[1] - n["y"]) ** 2 <= w2 for n in nodes)

    if any(reaches((a["x"], a["y"]), dst) for a in src):
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
        for t in frontier:
            for dx, dy in disc:
                u = (t[0] + dx, t[1] + dy)
                if u in legal and u not in parent:
                    parent[u] = t
                    nxt.append(u)
        frontier = nxt
    return None


def _components(poles, wire):
    parent = list(range(len(poles)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(poles)):
        for j in range(i + 1, len(poles)):
            if _dist(poles[i], poles[j]) <= wire:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b
    groups = {}
    for i in range(len(poles)):
        groups.setdefault(find(i), []).append(poles[i])
    return list(groups.values())


def _stab(intervals):
    """Minimum stabbing set of integer intervals (greedy by right endpoint - optimal)."""
    pts, last = [], None
    for lo, hi in sorted(intervals, key=lambda t: t[1]):
        if last is None or last < lo:
            last = hi
            pts.append(hi)
    return pts


# --------------------------------------------------------------------------- output
_KEEP = object()      # "argument not given" - distinct from pole=None, which means no poles


def replan(plan, drill=_KEEP, pole=_KEEP, **kw):
    """Re-plan the SAME patch with a different tier. Every drop tile, centre and pole
    interval is recomputed from the new prototype - which is exactly what the 2026-08-30
    copper failure skipped when it reused the burner's position for a 3x3 electric drill.
    The lane row is kept unless the caller overrides it. pole=None really does mean "no
    poles" (a burner mine); omit the argument to keep the plan's current pole."""
    p = dict(plan["params"])
    p["pole"] = plan["pole"] if pole is _KEEP else pole
    p["lane_y"] = plan["lane_y"]
    p.update(kw)
    return plan_outpost(plan["patch"], drill=plan["drill"] if drill is _KEEP else drill, **p)


def bom(plan):
    """{item: count}. Every entity here is placed by an item of the same name on 2.1."""
    out = {}
    for e in plan["entities"]:
        out[e["entity"]] = out.get(e["entity"], 0) + 1
    return out


def to_orders(plan):
    """executor.submit shape: {"kind":"place","args":{name,tile_x,tile_y,direction}}.
    Drills first (the lane is sized off their drops), then the lane, then the hookup, then
    poles - so a partial run always leaves a mine that is closer to working, not further."""
    rank = {"drill": 0, "lane": 1, "output": 2, "pole": 3, "anchor": 4}
    ents = sorted(plan["entities"], key=lambda e: (rank.get(e["role"], 9), e["x"], e["y"]))
    return [{"kind": "place",
             "args": {"name": e["entity"], "tile_x": e["x"], "tile_y": e["y"],
                      "direction": e["direction"]}} for e in ents]


def to_ghosts(plan):
    """autopilot.stamp_blueprint shape [{name,x,y,dir}] with x,y the CENTRE (top-left tile
    + tile_width/2), re-derived per prototype."""
    out = []
    for e in plan["entities"]:
        cx, cy = center(e["entity"], e["x"], e["y"])
        out.append({"name": e["entity"], "x": cx, "y": cy, "dir": e["direction"]})
    return out


# --------------------------------------------------------------------------- validation
def validate(plan):
    """Pure invariant check on a plan -> {ok, errors}. These are the six things that have
    actually broken a mine on this map."""
    errs = []
    lane = plan["lane_tiles"]
    lane_y = plan["lane_y"]
    drill = plan["drill"]
    patch = plan.get("patch") or {}
    tiles, foreign = patch.get("tiles") or {}, patch.get("foreign") or {}
    ents = plan["entities"]

    # 1. every drill's drop tile is ON the lane (the ore-on-the-ground bug)
    for e in ents:
        if e["role"] != "drill":
            continue
        d = drop_tile(drill, e["x"], e["y"], e["direction"])
        if d not in lane:
            errs.append("drill %s at (%d,%d) dir %d drops on %s, not the lane row y=%d"
                        % (e["entity"], e["x"], e["y"], e["direction"], d, lane_y))

    # 2. no pole on the lane, no pole over a drill (the pole-line-on-the-belt-row bug)
    dfp = set()
    for e in ents:
        if e["role"] == "drill":
            dfp |= footprint(e["entity"], e["x"], e["y"])
    for e in ents:
        if e["role"] not in ("pole", "anchor"):
            continue
        fp = footprint(e["entity"], e["x"], e["y"])
        if fp & lane:
            errs.append("pole %s at (%d,%d) sits on the lane" % (e["entity"], e["x"], e["y"]))
        if fp & dfp:
            errs.append("pole %s at (%d,%d) overlaps a drill" % (e["entity"], e["x"], e["y"]))

    # 3. no two footprints overlap at all
    used = {}
    for e in ents:
        for t in footprint(e["entity"], e["x"], e["y"]):
            if t in used:
                errs.append("footprint collision at %s: %s and %s" % (t, used[t], e["entity"]))
            used[t] = e["entity"]

    # 4. the BOM accounts for every entity 1:1
    b = plan.get("bom") or bom(plan)
    if sum(b.values()) != len(ents):
        errs.append("bom totals %d but the plan has %d entities" % (sum(b.values()), len(ents)))

    # 5. every drill actually mines `ore` and touches no foreign ore
    if tiles:
        want = plan["params"]["min_ore_tiles"]
        for e in ents:
            if e["role"] != "drill":
                continue
            a, bb, c, d2 = mining_area(drill, e["x"], e["y"])
            area = [(x, y) for x in range(a, c + 1) for y in range(bb, d2 + 1)]
            n = sum(1 for t in area if t in tiles)
            bad = [t for t in area if t in foreign]
            if n < want:
                errs.append("drill at (%d,%d) mines only %d %s tiles (want %d)"
                            % (e["x"], e["y"], n, plan["ore"], want))
            if bad:
                errs.append("drill at (%d,%d) mining area touches foreign ore %s at %s"
                            % (e["x"], e["y"], foreign[bad[0]], bad[0]))

    # 6. the lane is contiguous end to end (a one-tile gap stops every item)
    s, en = plan["lane_span"]
    have = {e["x"] for e in ents if e["role"] == "lane" and e["y"] == lane_y}
    missing = [x for x in range(s, en + 1) if x not in have]
    if missing:
        errs.append("lane has %d gap tile(s): x=%s" % (len(missing), missing[:6]))
    orphan = [x for x in have if x < s or x > en]
    if orphan:
        errs.append("orphan lane tile(s) outside the drill span: x=%s" % orphan[:6])
    if plan["params"].get("output"):
        hook = sorted((e["x"], e["entity"]) for e in ents if e["role"] == "output")
        if not hook or hook[0][0] != en + 1:
            errs.append("output hookup does not start at the lane end x=%d" % (en + 1))

    # 7. every drill is inside some pole's supply area and the poles form ONE network
    #    (an unpowered electric drill mines nothing, and reads as "plausible" - GOTCHAS)
    poles = [e for e in ents if e["role"] in ("pole", "anchor")]
    if poles and plan["pole"]:
        pspec = POLES[plan["pole"]]
        s, pw, ph = pspec["supply"], pspec["tw"], pspec["th"]
        dtw, dth = DRILLS[drill]["tw"], DRILLS[drill]["th"]
        for e in ents:
            if e["role"] != "drill":
                continue
            if not any(p["x"] + pw / 2.0 - s < e["x"] + dtw and p["x"] + pw / 2.0 + s > e["x"]
                       and p["y"] + ph / 2.0 - s < e["y"] + dth and p["y"] + ph / 2.0 + s > e["y"]
                       for p in poles):
                errs.append("drill at (%d,%d) is outside every %s supply area"
                            % (e["x"], e["y"], plan["pole"]))
        if len(_components(poles, pspec["wire"])) > 1:
            errs.append("poles form %d separate electric networks"
                        % len(_components(poles, pspec["wire"])))
    return {"ok": not errs, "errors": errs}


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(probe_prototypes(), indent=2, default=str))
