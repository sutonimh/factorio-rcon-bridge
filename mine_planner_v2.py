#!/usr/bin/env python3
"""The OPERATOR's all-electric mine outpost, planned whole before anything is placed.

This is `mine_layout.plan_outpost` corrected against the four deltas measured on the
operator's own copper and coal mines (before.json 713 ents -> after.json 619; he REBUILT
both from scratch, and left the bot's iron row alone as a counter-example):

  1. LANE DIRECTION IS COMPUTED FROM THE DESTINATION, never a constant. mine_layout.py:348
     emits direction=E for every lane tile; the iron mine needs W, and 13 of the operator's
     16 belt rotations are that one row flipped E->W. Here `lane_dir` comes from
     `trunk_x > mine_centre_x` and the lane's FINAL tile points into the trunk column
     (GOTCHAS P7).
  2. POLE PITCH IS REGULAR. `_plan_poles`' minimum-stabbing set plus a wire-gap filler put
     poles at -31.5,-24.5,-22.5 (spacings 7,2) where the operator laid 6,7. Here the row is
     an arithmetic run at pitch 7. That is not a cosmetic choice: a 3-wide drill starting at
     tile dx is supplied by a pole tile px anywhere in [dx-2, dx+tw+1], a window of exactly
     tw+4 = 7 integers, and a window of 7 consecutive integers contains exactly ONE member of
     every residue class mod 7 -- so ANY phase of a pitch-7 run covers EVERY drill, and the
     phase is free to be spent on avoiding belt tiles and landing on the trunk column.
  3. NO TERMINAL CHEST, NO TERMINAL INSERTER. mine_layout defaults to
     output=("inserter","wooden-chest"); the operator's mines contain 0 chests and 0
     inserters (the whole map has 2 chests, both plate-belt drains). A chest is a hard stop
     where throughput becomes a human walking (GOTCHAS P10).
  4. TRUNK HOOKUP. The plan used to stop at max(drop_x). Here the lane continues to the
     per-ore trunk column, turns 90 degrees onto it, and the flank pole row continues to the
     power trunk at pitch 7 -- the flank row IS the spur.

Everything else in mine_layout was already right and is REUSED, not reimplemented: the
prototype tables, `drop_tile` (centre = tile + tw/2, drop = centre + rot(vec, dir)),
`mining_area`, the foreign-ore veto, `_Field`'s prefix sums, `_connect`/`_bridge_path` for
the bridge poles, and `to_ghosts`.

THE GUARANTEE this module exists to make, checked by validate() before the plan is returned:
  * EVERY drill's drop tile lands on a planned belt tile, recomputed from the prototype's
    tile_width/height and direction (electric drills are 3x3; the in-place burner->electric
    swap moved the drop tile by half a tile and six copper drills mined onto bare ground);
  * NO pole sits on the lane, on the trunk column, on a drop tile or inside a drill;
  * drill pitch >= tile_width, so collision boxes cannot overlap (create_entity does no
    collision check -- the live iron row overlaps by 0.696 tiles because of that);
  * every drill is inside some pole's supply area and every pole is in ONE wire component.

build() runs the plan through buildplan (plan -> apply -> verify -> rollback), and the
functional check is `lane_lint.verify_supply(ore, head, terminus)`: connected AND moving. A
mine that does not move ore is torn down in the same pass (BUILD LAW 2).

upgrade_to_electric() RE-PLANS the whole outpost for the 3x3 footprint instead of swapping
tiers in place, supersedes the burner plan (which tears out only what the new layout does not
reuse), and removes the coal fuel belts, which are dead weight the moment the drills stop
burning coal.

RCON: pure except scan_patch and probe_placed (delegated, READ ONLY), and build(),
wire_poles(), remove_placed(), remove_tiles() (WRITES, and only from the build/teardown path).
Nothing at import time touches the server.

COORDINATE HAZARD, verified live and central to the removal/probe paths: every world lookup
in this codebase is find_entities_filtered{position=tile+0.5, radius<=0.8}, and that radius is
measured to the entity's POSITION (its centre), not its bounding box. A 3x3 drill's centre is
1.414 tiles from its TOP-LEFT tile - the coordinate this planner speaks in - so it is
INVISIBLE to both buildplan.probe and buildplan._default_remove. probe_placed/remove_placed
translate through probe_tile(); nothing else in this module may address an entity by its
top-left tile over RCON.

SCOPE, unfixed and inherited from mine_layout: the planner plans against ORE and against its
own entities. It has no model of what is already standing on the ground, so the hookup run
from the last drop tile to the trunk column is laid BLIND (a warning says so per plan).
OPERATOR_MINE_SPEC["crossing"]="underground-belt" records what the operator does there; it is
not implemented here. Route that leg with belt_router.plan_route before submitting.
"""
import math

import buildplan
import mine_layout as ML

N, E, S, W = ML.N, ML.E, ML.S, ML.W
LayoutError = ML.LayoutError

KIND = "mine_outpost_v2"
BELT_NAMES = ("transport-belt", "fast-transport-belt", "express-transport-belt",
              "underground-belt", "fast-underground-belt", "express-underground-belt")

# Order matters on a partial run: drills first (the lane is sized off their drops), then the
# lane, then the trunk column, then the lattice poles, then the bridges.
ROLE_RANK = {"drill": 0, "lane": 1, "trunk": 2, "pole": 3, "bridge": 4}

# ---------------------------------------------------------------------------------------
# MEASURED SPEC. Every number here was read off the operator's copper (lane y=-63.5) and
# coal (lane y=15.5) outposts on the live 2.1.17 server, or off the before/after snapshots.
# The iron row (pitch 2, single-sided, dropping east into a dead end) is the BOT's and is a
# counter-example, not a source.
# ---------------------------------------------------------------------------------------
OPERATOR_MINE_SPEC = {
    "drill":        "electric-mining-drill",   # all 11 burner drills removed, 16 electric added
    "pole":         "small-electric-pole",     # 95/95 poles on the map; medium not researched
    "belt":         "transport-belt",

    "drill_pitch":  3,        # == tile_width; drills flush, zero gap. HARD FLOOR: at pitch 2
                              # the 1.34765625 collision half-widths overlap by 0.696 tiles.
    "lane_offset":  2.0,      # drill centre -> lane centre, along the drill's facing.
                              # top row    ty = lane_y - th, dir 8/S
                              # bottom row ty = lane_y + 1,  dir 0/N   (tier-INDEPENDENT)
    "double_sided": True,     # both rows on the SAME x lattice; dir 8 fills the far belt
                              # lane, dir 0 the near one, so both lanes saturate.
    "pole_offset":  2.0,      # from the drill-row centreline, on the side AWAY from the lane
                              # top py = lane_y - th - ph (= lane_y-4), bottom py = lane_y+th+1
    "pole_pitch":   7,        # regular, co-linear, NEVER exceeded (wire reach 7.5). Measured
                              # on 21 trunk spans in 3 independent runs: exactly 7.0 every time.
    "lane_direction_rule": "toward_trunk",     # E(4) if trunk_x > mine_centre_x else W(12);
                              # the last lane tile turns 90 deg onto the per-ore trunk column.
    "output":       None,     # NO terminal chest, NO terminal inserter. Belt-fed only.
    "trunk_pole_pitch": 7,
    "array_pole_pitch": 4,    # base/lab lattice only, NOT mines
    "trunk_column_sep": 2,    # per-ore columns 2 apart, 1 empty tile between
    "crossing":     "underground-belt",        # span 2, never share a tile between two ores

    "min_pole_sep": 3.0,      # operator's measured minimum nearest-neighbour distance
    "max_axis_hop": 7,        # largest hop he ever used on an axis (0.5 tiles of slack)
    "wire_reach":   7.5,      # small-electric-pole get_max_wire_distance()
    "supply_radius": 2.5,     # get_supply_area_distance() -> a 5x5 supply square
    "electric_networks": 1,   # all 95 poles + 16 drills + 58 inserters on network id 535
}

# Engine-probed collision half-widths (2.1.17). The minimum non-overlapping pitch is
# ceil(2*half); for both drills that equals tile_width, which is why "pitch == tile_width" is
# the rule rather than a coincidence. big-mining-drill is deliberately absent: not measured.
COLLISION_HALFWIDTH = {
    "burner-mining-drill":   0.69921875,       # -> min pitch 2
    "electric-mining-drill": 1.34765625,       # -> min pitch 3
}

# Which drills die without a pole in range. A burner mine with no poles is a design; an
# ELECTRIC mine with no poles is 16 drills that never turn, and validate() must say so.
BURNER_DRILLS = ("burner-mining-drill",)


# --------------------------------------------------------------------------- small helpers
def _xy(t):
    return (int(t[0]), int(t[1]))


def needs_power(name):
    return name in ML.DRILLS and name not in BURNER_DRILLS


def probe_tile(name, tile_x, tile_y):
    """The tile an entity's CENTRE falls in, given its TOP-LEFT tile.

    Every world lookup in this codebase is `find_entities_filtered{position=tile+0.5,
    radius=0.6..0.8}`, and that radius is measured to the entity's POSITION (its centre), not
    to its bounding box - verified live 2026-08-29 on a 2x2 stone-furnace (centre 0.707 away:
    MISS at 0.6, HIT at 0.8) and re-verified 2026-08-29 on a live 3x3 electric-mining-drill at
    top-left (-33,-67): radius 0.6/0.8/1.0 all return 0, radius 1.5 returns 1, because its
    centre is 1.414 tiles from the top-left tile's centre.

    So probing or removing a 3x3 drill at its TOP-LEFT tile finds NOTHING - buildplan.probe
    reports it as never built and buildplan._default_remove leaves it standing while rollback
    reports done. Hand those two the centre tile instead; for 1x1 and 2x2 it is a no-op or a
    0.707 offset, both of which the existing radii already cover."""
    cx, cy = ML.center(name, tile_x, tile_y)
    return (int(math.floor(cx)), int(math.floor(cy)))


def drill_pitch(drill):
    """The x pitch for a drill row. tile_width, never less -- create_entity performs NO
    collision check, so nothing but the planner stops two drills sharing a tile column."""
    tw = ML.DRILLS[drill]["tw"]
    half = COLLISION_HALFWIDTH.get(drill)
    floor_ = int(math.ceil(2 * half)) if half else tw
    return max(tw, floor_)


def supply_window(drill, pole):
    """(lo_off, hi_off): a pole tile px supplies a drill whose left tile is dx iff
    dx + lo_off <= px <= dx + hi_off. Derived, not hardcoded, so a pole-tier change
    re-derives it: the pole's supply square spans px-s..px+s' and the drill spans dx..dx+tw-1.
    """
    s = ML.POLES[pole]["supply"]
    pw = ML.POLES[pole]["tw"]
    tw = ML.DRILLS[drill]["tw"]
    # supply square in tiles: floor(pcx - s) .. ceil(pcx + s) - 1 with pcx = px + pw/2
    lo = int(math.floor(pw / 2.0 - s))         # leftmost supplied tile, relative to px
    hi = int(math.ceil(pw / 2.0 + s)) - 1      # rightmost supplied tile, relative to px
    return (-hi, tw - 1 - lo)


def pole_pitch_for(drill, pole, nominal=None):
    """The largest regular pitch that still guarantees coverage at EVERY phase.

    The supply window above is `hi_off - lo_off + 1` integers wide; a run at pitch p contains
    exactly one member of each residue class mod p, so every window of >= p consecutive
    integers is hit. Cap at the wire reach so the run is also connected.
    """
    lo, hi = supply_window(drill, pole)
    width = hi - lo + 1
    nominal = OPERATOR_MINE_SPEC["pole_pitch"] if nominal is None else nominal
    return max(1, min(nominal, width, int(ML.POLES[pole]["wire"])))


def _drill_rows(drill, lane_y):
    """Top-left y and facing for both drill rows. TIER-INDEPENDENT: this same rule lands the
    drop on lane_y for 2x2, 3x3 and 5x5 (only the drop COLUMN is tier-dependent)."""
    th = ML.DRILLS[drill]["th"]
    return (("top", lane_y - th, S), ("bottom", lane_y + 1, N))


def pole_rows(drill, pole, lane_y):
    """The two flank rows, immediately OUTSIDE the drill rows, on the side away from the lane.
    For a 3x3 drill and a 1x1 pole that is exactly lane_y-4 / lane_y+4, which is what
    mine_layout._plan_poles already emits and what the operator built at copper and coal."""
    th = ML.DRILLS[drill]["th"]
    ph = ML.POLES[pole]["th"]
    return {"top": lane_y - th - ph, "bottom": lane_y + th + 1}


# --------------------------------------------------------------------------- slot picking
def _fields(patch, drill):
    spec = ML.DRILLS[drill]
    x1, y1, x2, y2 = patch["bbox"]
    pad = int(math.ceil(spec["radius"])) + max(spec["tw"], spec["th"]) + 2
    fx, fy = x1 - pad, y1 - pad
    fw, fh = (x2 - x1 + 1) + 2 * pad, (y2 - y1 + 1) + 2 * pad
    ore_f = ML._Field(patch["tiles"], fx, fy, fw, fh)
    for_f = ML._Field(patch.get("foreign") or {}, fx, fy, fw, fh)
    return ore_f, for_f, (fx, fw)


def _slots_for_lane(drill, lane_y, xlo, xhi, pitch, ore_f, for_f, min_ore_tiles):
    """Every viable (column, side) slot for one lane row, grouped by lattice phase.

    Both rows share ONE x lattice (the operator's copper south row sits on a subset of the
    north row's columns) -- interleaving them would put a south drill's footprint half a pitch
    off and break the "same tile, opposite lane" pairing that makes double-siding work.
    """
    out = {}
    rows = _drill_rows(drill, lane_y)
    for tx in range(xlo, xhi + 1):
        ph = tx % pitch
        for side, ty, d in rows:
            a, b, c, e = ML.mining_area(drill, tx, ty)
            if for_f.rect(a, b, c, e):
                continue                       # foreign ore in the mining area: hard veto
            n = ore_f.rect(a, b, c, e)
            if n < min_ore_tiles:
                continue
            out.setdefault(ph, []).append({"x": tx, "y": ty, "dir": d, "side": side, "ore": n})
    return out


def _take(slots, n_drills):
    """The n best slots, richest ore first, then laid out west-to-east / top-then-bottom."""
    ranked = sorted(slots, key=lambda s: (-s["ore"], s["x"], s["side"]))
    if n_drills is not None:
        ranked = ranked[:max(0, int(n_drills))]
    return sorted(ranked, key=lambda s: (s["x"], s["side"]))


# --------------------------------------------------------------------------- pole lattice
def _extension(core, target_x, pitch, min_sep):
    """The extra pole columns that walk the flank row out to the trunk, NEAREST-FIRST.

    GOTCHAS P8: pitch is nominal, ENDPOINTS ARE HARD. A shorter final hop still wires, so only
    EXCEEDING the pitch is a violation -- which is why the walk steps at `pitch` and then lands
    exactly on the trunk column, unless it is already closer than the minimum pole separation.
    """
    if target_x is None or not core:
        return []
    sign = 1 if target_x > core[-1] else -1
    anchor = core[-1] if sign > 0 else core[0]
    if (target_x - anchor) * sign <= 0:
        return []                                  # the trunk is already inside the run
    out, px = [], anchor
    while (px + sign * pitch - target_x) * sign <= 0:
        px += sign * pitch
        out.append(px)
    if abs(target_x - (out[-1] if out else anchor)) >= min_sep:
        out.append(target_x)
    return out


def _pole_run(row_slots, drill, pole, py, blocked, pitch, target_x, min_sep):
    """One flank row: a REGULAR arithmetic run at `pitch` that covers every drill in the row.

    Every phase covers every drill (see pole_pitch_for), so the phase is free to be spent on
    (a) clearing every belt and drill tile, (b) using the fewest poles, (c) landing the run ON
    the trunk column. The spur half is truncated at the first blocked tile rather than
    abandoning the phase - coverage comes from the core, connectivity from the bridge pass.
    """
    if not row_slots:
        return [], []
    lo_off, hi_off = supply_window(drill, pole)
    dxs = sorted(s["x"] for s in row_slots)
    win_lo, win_hi = dxs[0] + lo_off, dxs[-1] + hi_off
    best, notes = None, []
    for r in range(pitch):
        lo = win_lo + ((r - win_lo) % pitch)
        hi = win_hi - ((win_hi - r) % pitch)
        if hi < lo:
            continue
        core = list(range(lo, hi + 1, pitch))
        if any(ML.footprint(pole, px, py) & blocked for px in core):
            continue                               # a supply pole cannot be moved: skip phase
        run, cut = list(core), False
        for px in _extension(core, target_x, pitch, min_sep):
            if ML.footprint(pole, px, py) & blocked:
                cut = True
                break                              # stop the spur here; the bridge pass joins
            run.append(px)
        run.sort()
        aligned = 0 if (target_x is None or (target_x - r) % pitch == 0) else 1
        score = (int(cut), len(run), aligned, -run[0])
        if best is None or score < best[0]:
            best = (score, run, cut)
    if best is None:
        return None, ["no pitch-%d %s run on row y=%d clears the belt and drill tiles"
                      % (pitch, pole, py)]
    if best[2]:
        notes.append("the %s spur on row y=%d stops short of x=%s (a belt or drill is in the "
                     "way); the bridge pass must reach the grid" % (pole, py, target_x))
    return best[1], notes


# --------------------------------------------------------------------------- the planner
def plan_outpost(ore, n_drills=None, *, patch=None, center=None, scan_radius=40,
                 drill="electric-mining-drill", pole="small-electric-pole",
                 belt="transport-belt", lane_y=None, min_ore_tiles=4,
                 trunk=None, power_trunk_x=None, grid_anchor=None,
                 pole_pitch=None, spur_side=None, strict=True):
    """Plan ONE double-sided mining outpost. Nothing is placed; build() does that.

    ore         resource name ("copper-ore", "coal", ...).
    n_drills    how many drills to lay (None = every viable slot on the best lane row).
    patch       a mine_layout.scan_patch() result. If omitted, `center` is scanned live
                (READ ONLY). One of the two is required.
    trunk       (trunk_x, trunk_y): the per-ore trunk BELT column and the row it heads to.
                The lane runs to trunk_x, its last tile turns 90 degrees, and a single-tile
                column carries the ore away. None = no hookup (the lane ends at the last drop).
    power_trunk_x   x of the power trunk column; the spur flank row walks out to it at pitch 7.
    grid_anchor (x,y) of an EXISTING pole to join. It is a virtual node, never re-placed.
    spur_side   "top"/"bottom": which flank row doubles as the spur. Default: the row nearest
                the anchor (or the trunk), which is how the operator chose at copper.

    Coordinates in `entities` are TOP-LEFT TILES (autopilot.place / executor's convention);
    centres are re-derived per prototype at emit time so a tier swap can never inherit a
    stale centre.
    """
    if drill not in ML.DRILLS:
        raise LayoutError("unknown drill %r (have %s)" % (drill, ", ".join(sorted(ML.DRILLS))))
    if pole is not None and pole not in ML.POLES:
        raise LayoutError("unknown pole %r (have %s)" % (pole, ", ".join(sorted(ML.POLES))))
    if patch is None:
        if center is None:
            raise LayoutError("plan_outpost needs patch= or center=(x,y) to scan (READ ONLY)")
        patch = ML.scan_patch(ore, center[0], center[1], radius=scan_radius)
    if not patch.get("tiles"):
        raise LayoutError("empty %s patch: nothing to mine" % ore)
    if patch.get("ore") not in (None, ore):
        raise LayoutError("patch carries %r but the plan asked for %r" % (patch["ore"], ore))

    spec = ML.DRILLS[drill]
    th = spec["th"]
    pitch = drill_pitch(drill)
    ore_f, for_f, (fx, fw) = _fields(patch, drill)
    xlo, xhi = fx, fx + fw - 1
    y1, y2 = patch["bbox"][1], patch["bbox"][3]
    warnings = []

    # ---- lane row + lattice phase: one sweep, scored on (drills kept, ore, centrality)
    rows = [lane_y] if lane_y is not None else list(range(y1 - th, y2 + th + 1))
    best = None
    for ly in rows:
        by_phase = _slots_for_lane(drill, ly, xlo, xhi, pitch, ore_f, for_f, min_ore_tiles)
        for ph in sorted(by_phase):
            kept = _take(by_phase[ph], n_drills)
            if not kept:
                continue
            score = (len(kept), sum(s["ore"] for s in kept),
                     -abs(ly * 2 - (y1 + y2)), -ph)
            if best is None or score > best[0]:
                best = (score, ly, ph, kept)
    if best is None:
        raise LayoutError("no viable %s row over the %s patch bbox %s (min_ore_tiles=%d, "
                          "foreign-ore veto on)" % (drill, ore, patch["bbox"], min_ore_tiles))
    _score, lane_y, phase, slots = best
    if n_drills is not None and len(slots) < n_drills:
        warnings.append("asked for %d %s drills, the patch supports %d on the best row (y=%d)"
                        % (n_drills, drill, len(slots), lane_y))

    # ---- the lane IS the drop row (lane law L3): derive it from drop_position, never guess
    drops = [ML.drop_tile(drill, s["x"], s["y"], s["dir"]) for s in slots]
    off_row = [(s, d) for s, d in zip(slots, drops) if d[1] != lane_y]
    if off_row:
        raise LayoutError("drill at (%d,%d) dir %d drops on row %d, not the lane row %d"
                          % (off_row[0][0]["x"], off_row[0][0]["y"], off_row[0][0]["dir"],
                             off_row[0][1][1], lane_y))
    head_x, tail_x = min(d[0] for d in drops), max(d[0] for d in drops)

    # ---- flow direction is COMPUTED FROM THE DESTINATION (P7), never a constant
    centre_x = (head_x + tail_x) / 2.0
    trunk_x = trunk_y = None
    if trunk is not None:
        trunk_x, trunk_y = int(trunk[0]), (None if trunk[1] is None else int(trunk[1]))
        lane_dir = E if trunk_x > centre_x else W
        if lane_dir == E and trunk_x <= tail_x:
            raise LayoutError("trunk column x=%d is not past the last drop x=%d: an east-flowing"
                              " lane would divert mid-row" % (trunk_x, tail_x))
        if lane_dir == W and trunk_x >= head_x:
            raise LayoutError("trunk column x=%d is not past the last drop x=%d: a west-flowing"
                              " lane would divert mid-row" % (trunk_x, head_x))
        span = (head_x, trunk_x) if lane_dir == E else (trunk_x, tail_x)
    else:
        lane_dir = E
        span = (head_x, tail_x)
        warnings.append("no trunk= given: the lane has no destination, so its direction is a "
                        "DEFAULT (east), not a computation. Pass trunk=(x,y).")
    span_start, span_end = span
    lane_tiles = {(x, lane_y) for x in range(span_start, span_end + 1)}

    # HONEST SCOPE (inherited from mine_layout.plan_outpost and NOT fixed here): this planner
    # plans against ORE and against its own entities. It knows nothing about what is already
    # standing between the last drop tile and the trunk column - existing belts, poles,
    # machines, water, cliffs, operator-protected tiles. Inside the drill span that is
    # harmless (the drills' own footprints are the plan). The HOOKUP run is not: it is a blind
    # straight line, and OPERATOR_MINE_SPEC["crossing"]="underground-belt" is a recorded
    # measurement of what the operator does there, NOT something this module implements.
    # Route that leg with belt_router.plan_route (it does undergrounds + functional-reservation
    # obstacles from belt_router.scan_obstacles) before submitting, or check it with
    # lane_lint.trace afterwards.
    hookup = (tail_x + 1, span_end) if lane_dir == E else (span_start, head_x - 1)
    if hookup[1] >= hookup[0]:
        warnings.append("the %d-tile hookup run x=%d..%d on row y=%d is laid BLIND (no world "
                        "obstacles, no underground crossings): route it with "
                        "belt_router.plan_route or verify it with lane_lint.trace"
                        % (hookup[1] - hookup[0] + 1, hookup[0], hookup[1], lane_y))

    # ---- trunk column: the lane's LAST tile points into it; they are one continuous belt
    turn_dir = None
    trunk_tiles = set()
    if trunk_x is not None and trunk_y is not None and trunk_y != lane_y:
        step = 1 if trunk_y > lane_y else -1
        turn_dir = S if step > 0 else N
        trunk_tiles = {(trunk_x, y) for y in range(lane_y + step, trunk_y + step, step)}

    ents = []
    for s, d in zip(slots, drops):
        ents.append({"entity": drill, "x": s["x"], "y": s["y"], "direction": s["dir"],
                     "role": "drill", "side": s["side"], "ore": s["ore"], "drop": d})
    for x in range(span_start, span_end + 1):
        d = turn_dir if (turn_dir is not None and x == trunk_x) else lane_dir
        ents.append({"entity": belt, "x": x, "y": lane_y, "direction": d, "role": "lane"})
    for (x, y) in sorted(trunk_tiles, key=lambda t: (t[1], t[0])):
        ents.append({"entity": belt, "x": x, "y": y, "direction": turn_dir, "role": "trunk"})

    belt_tiles = lane_tiles | trunk_tiles
    blocked = set(belt_tiles)
    for e in ents:
        if e["role"] == "drill":
            blocked |= ML.footprint(e["entity"], e["x"], e["y"])

    # ---- poles: two regular flank runs + whatever bridges the rows and the grid need
    prows = pole_rows(drill, pole, lane_y) if pole else {}
    poles = []
    if pole is not None:
        pp = pole_pitch_for(drill, pole) if pole_pitch is None else int(pole_pitch)
        min_sep = OPERATOR_MINE_SPEC["min_pole_sep"]
        if spur_side is None:
            ref_y = (grid_anchor[1] if grid_anchor else
                     (trunk_y if trunk_y is not None else lane_y))
            spur_side = min(prows, key=lambda k: (abs(prows[k] - ref_y), k))
        for side in ("top", "bottom"):
            row_slots = [s for s in slots if s["side"] == side]
            target = power_trunk_x if side == spur_side else None
            run, warn = _pole_run(row_slots, drill, pole, prows[side], blocked, pp, target,
                                  min_sep)
            warnings.extend(warn)
            for px in (run or []):
                poles.append({"entity": pole, "x": px, "y": prows[side], "direction": N,
                              "role": "pole", "side": side})
                blocked |= ML.footprint(pole, px, prows[side])
        if power_trunk_x is not None and grid_anchor is None:
            warnings.append("power_trunk_x=%d given without grid_anchor: the join to the base "
                            "grid is asserted, not planned - pass the trunk pole's tile"
                            % power_trunk_x)
        # bridges: the two flank rows are th+2*ph apart (8 tiles for 3x3 + small), past the
        # 7.5 wire reach on purpose, so they are joined around the end of the rows - exactly
        # what mine_layout._connect's BFS does, and what the operator's (-23,-61) pole is.
        n_before = len(poles)
        ML._connect(poles, pole, blocked, warnings, grid_anchor)
        for p in poles[n_before:]:
            p["role"] = "bridge"
    ents.extend(poles)

    plan = {
        "ore": ore, "drill": drill, "pole": pole, "belt": belt,
        "lane_y": lane_y, "lane_dir": lane_dir, "lane_span": (span_start, span_end),
        "lane_tiles": lane_tiles, "trunk_tiles": trunk_tiles, "belt_tiles": belt_tiles,
        "turn_dir": turn_dir, "trunk": (trunk_x, trunk_y) if trunk_x is not None else None,
        "drill_pitch": pitch, "lattice_phase": phase,
        "pole_rows": prows, "pole_pitch": (pole_pitch_for(drill, pole) if pole and
                                           pole_pitch is None else pole_pitch),
        "entities": ents, "warnings": warnings, "patch": patch,
        "head": (head_x, lane_y), "tail": (tail_x, lane_y),
        "from_xy": (head_x if lane_dir == E else tail_x, lane_y),
        "to_xy": ((trunk_x, trunk_y) if trunk_tiles else
                  (span_end if lane_dir == E else span_start, lane_y)),
        "params": {"n_drills": n_drills, "min_ore_tiles": min_ore_tiles, "trunk": trunk,
                   "power_trunk_x": power_trunk_x, "grid_anchor": grid_anchor,
                   "pole_pitch": pole_pitch, "spur_side": spur_side, "output": None},
        "spec": dict(OPERATOR_MINE_SPEC),
    }
    plan["tiles_used"] = {t for e in ents for t in ML.footprint(e["entity"], e["x"], e["y"])}
    plan["bom"] = bom(plan)
    v = validate(plan)
    plan["validation"] = v
    if strict and not v["ok"]:
        raise LayoutError("plan failed its own invariants:\n  " + "\n  ".join(v["errors"]))
    return plan


# --------------------------------------------------------------------------- output shapes
def bom(plan):
    """{item: count}. Every entity this module emits is placed by an item of the same name."""
    out = {}
    for e in plan["entities"]:
        out[e["entity"]] = out.get(e["entity"], 0) + 1
    return out


def to_orders(plan):
    """executor.submit shape. Drills -> lane -> trunk -> poles -> bridges, so a partial run
    always leaves a mine that is CLOSER to working, never further."""
    ents = sorted(plan["entities"],
                  key=lambda e: (ROLE_RANK.get(e["role"], 9), e["x"], e["y"]))
    return [{"kind": "place",
             "args": {"name": e["entity"], "tile_x": e["x"], "tile_y": e["y"],
                      "direction": e["direction"]}} for e in ents]


def to_ghosts(plan):
    """autopilot.stamp_blueprint shape, centres re-derived per prototype (mine_layout)."""
    return ML.to_ghosts(plan)


def plan_tiles(plan):
    """buildplan tile list: [(x, y, direction)] in build order."""
    ents = sorted(plan["entities"],
                  key=lambda e: (ROLE_RANK.get(e["role"], 9), e["x"], e["y"]))
    return [(e["x"], e["y"], e["direction"]) for e in ents]


# --------------------------------------------------------------------------- validation
def validate(plan):
    """Pure invariant check -> {ok, errors, warnings}. Every one of these has actually broken
    a mine on this map."""
    errs, warns = [], []
    ents = plan["entities"]
    drill = plan["drill"]
    pole = plan["pole"]
    lane_y = plan["lane_y"]
    belt_tiles = plan["belt_tiles"]
    drill_ents = [e for e in ents if e["role"] == "drill"]
    pole_ents = [e for e in ents if e["role"] in ("pole", "bridge")]

    # 1. THE GUARANTEE: every drop tile lands on a planned belt tile, recomputed from the
    #    prototype. This is the check the in-place burner->electric swap did not have.
    drops = set()
    for e in drill_ents:
        d = ML.drop_tile(drill, e["x"], e["y"], e["direction"])
        drops.add(d)
        if d not in belt_tiles:
            errs.append("drill %s at (%d,%d) dir %d drops on %s - not a planned belt tile"
                        % (e["entity"], e["x"], e["y"], e["direction"], (d,)))
        elif d[1] != lane_y:
            errs.append("drill at (%d,%d) drops on row %d, not the lane row %d"
                        % (e["x"], e["y"], d[1], lane_y))

    # 2. no pole on a belt tile, on a drop tile, or inside a drill
    dfp = set()
    for e in drill_ents:
        dfp |= ML.footprint(e["entity"], e["x"], e["y"])
    for e in pole_ents:
        fp = ML.footprint(e["entity"], e["x"], e["y"])
        if fp & belt_tiles:
            errs.append("pole %s at (%d,%d) sits on a belt tile" % (e["entity"], e["x"], e["y"]))
        if fp & drops:
            errs.append("pole %s at (%d,%d) sits on a drill drop tile"
                        % (e["entity"], e["x"], e["y"]))
        if fp & dfp:
            errs.append("pole %s at (%d,%d) overlaps a drill" % (e["entity"], e["x"], e["y"]))

    # 3. no two footprints overlap at all (create_entity does NO collision check)
    used = {}
    for e in ents:
        for t in ML.footprint(e["entity"], e["x"], e["y"]):
            if t in used:
                errs.append("footprint collision at %s: %s and %s" % (t, used[t], e["entity"]))
            used[t] = e["entity"]

    # 4. drill pitch >= tile_width (the live iron row overlaps by 0.696 tiles)
    want = drill_pitch(drill)
    for side in ("top", "bottom"):
        xs = sorted(e["x"] for e in drill_ents if e.get("side") == side)
        for a, b in zip(xs, xs[1:]):
            if b - a < want:
                errs.append("%s-row drills at x=%d and x=%d are %d apart; %s needs >= %d"
                            % (side, a, b, b - a, drill, want))

    # 5. BOM accounts for every entity 1:1
    b = plan.get("bom") or bom(plan)
    if sum(b.values()) != len(ents):
        errs.append("bom totals %d but the plan has %d entities" % (sum(b.values()), len(ents)))
    recount = {}
    for e in ents:
        recount[e["entity"]] = recount.get(e["entity"], 0) + 1
    if recount != b:
        errs.append("bom %s does not match the plan's entities %s" % (b, recount))

    # 6. the lane is contiguous, has no head overshoot, and its last tile turns into the trunk
    s, en = plan["lane_span"]
    have = {e["x"] for e in ents if e["role"] == "lane"}
    missing = [x for x in range(s, en + 1) if x not in have]
    if missing:
        errs.append("lane has %d gap tile(s): x=%s" % (len(missing), missing[:6]))
    if have and (min(have) != s or max(have) != en):
        errs.append("lane tiles span x=%d..%d but the plan's span is %d..%d"
                    % (min(have), max(have), s, en))
    head_x, tail_x = plan["head"][0], plan["tail"][0]
    if plan["lane_dir"] == E and s != head_x:
        errs.append("east-flowing lane starts at x=%d, %d tiles upstream of the first drop x=%d"
                    % (s, head_x - s, head_x))
    if plan["lane_dir"] == W and en != tail_x:
        errs.append("west-flowing lane starts at x=%d, %d tiles upstream of the first drop x=%d"
                    % (en, en - tail_x, tail_x))
    if plan["trunk_tiles"]:
        tx, ty = plan["trunk"]
        turn = [e for e in ents if e["role"] == "lane" and e["x"] == tx]
        if not turn or turn[0]["direction"] != plan["turn_dir"]:
            errs.append("the lane's tile at the trunk column x=%d does not turn into it" % tx)
        col = sorted(t[1] for t in plan["trunk_tiles"])
        step = 1 if ty > lane_y else -1
        want_col = list(range(lane_y + step, ty + step, step))
        if sorted(want_col) != col:
            errs.append("trunk column is not contiguous from the lane to y=%d" % ty)

    # 7. the operator's mines have ZERO chests and ZERO inserters
    junk = [e["entity"] for e in ents
            if e["entity"].endswith("-chest") or "inserter" in e["entity"]]
    if junk:
        errs.append("mine plan contains %s - the operator's mines are belt-fed and terminate "
                    "in NO chest and NO inserter" % sorted(set(junk)))

    # 8. power: every drill supplied, one wire component, hops within the pitch rule.
    #    The no-pole case is checked FIRST and is an error, not a skip: `pole=None`, or two
    #    flank rows that both failed to find a clear phase, used to fall straight through this
    #    block and return ok=True for an all-electric mine with zero poles - 16 drills that
    #    never turn. Only a burner mine may legally have none.
    unpowered = [e for e in drill_ents if needs_power(e["entity"])]
    if unpowered and not pole_ents:
        errs.append("%d %s need power and the plan has NO poles (pole=%r): an electric mine "
                    "with no supply is %d drills that never turn"
                    % (len(unpowered), drill, pole, len(unpowered)))
    if pole and pole_ents:
        lo_off, hi_off = supply_window(drill, pole)
        for e in drill_ents:
            # supply_window is stated pole-relative-to-drill: px in [dx+lo_off, dx+hi_off]
            if not any(e["x"] + lo_off <= p["x"] <= e["x"] + hi_off
                       and _row_covers(pole, p["y"], e["y"], ML.DRILLS[drill]["th"])
                       for p in pole_ents):
                errs.append("drill at (%d,%d) is outside every %s supply area"
                            % (e["x"], e["y"], pole))
        nodes = list(pole_ents)
        anchor = (plan.get("params") or {}).get("grid_anchor")
        if anchor:
            nodes = nodes + [{"entity": pole, "x": int(anchor[0]), "y": int(anchor[1])}]
        comps = ML._components(nodes, ML.POLES[pole]["wire"])
        if len(comps) > 1:
            errs.append("poles form %d separate electric networks (the operator's base has "
                        "exactly 1)" % len(comps))
        pp = plan.get("pole_pitch") or pole_pitch_for(drill, pole)
        for side, py in (plan.get("pole_rows") or {}).items():
            xs = sorted(p["x"] for p in pole_ents if p["y"] == py and p["role"] == "pole")
            for a, c in zip(xs, xs[1:]):
                if c - a > pp:
                    errs.append("%s flank row hop x=%d->%d is %d, past the pitch %d"
                                % (side, a, c, c - a, pp))
        for i in range(len(pole_ents)):
            for j in range(i + 1, len(pole_ents)):
                d = ML._dist(pole_ents[i], pole_ents[j])
                if d < OPERATOR_MINE_SPEC["min_pole_sep"]:
                    warns.append("poles at (%d,%d) and (%d,%d) are %.2f apart; the operator's "
                                 "minimum is %.1f"
                                 % (pole_ents[i]["x"], pole_ents[i]["y"], pole_ents[j]["x"],
                                    pole_ents[j]["y"], d, OPERATOR_MINE_SPEC["min_pole_sep"]))

    # 9. ore veto: every drill really mines `ore` and touches no foreign ore
    patch = plan.get("patch") or {}
    tiles, foreign = patch.get("tiles") or {}, patch.get("foreign") or {}
    if tiles:
        want_n = (plan.get("params") or {}).get("min_ore_tiles", 0)
        for e in drill_ents:
            a, bb, c, d2 = ML.mining_area(drill, e["x"], e["y"])
            area = [(x, y) for x in range(a, c + 1) for y in range(bb, d2 + 1)]
            n = sum(1 for t in area if t in tiles)
            bad = [t for t in area if t in foreign]
            if n < want_n:
                errs.append("drill at (%d,%d) mines only %d %s tiles (want %d)"
                            % (e["x"], e["y"], n, plan["ore"], want_n))
            if bad:
                errs.append("drill at (%d,%d) mining area touches foreign ore %s at %s"
                            % (e["x"], e["y"], foreign[bad[0]], bad[0]))
    return {"ok": not errs, "errors": errs, "warnings": warns}


def _row_covers(pole, py, dy, dth):
    """Does a pole row at py reach a drill row starting at dy and dth tall?"""
    s = ML.POLES[pole]["supply"]
    ph = ML.POLES[pole]["th"]
    pcy = py + ph / 2.0
    return (pcy - s < dy + dth) and (pcy + s > dy)


# --------------------------------------------------------------------------- build (WRITES)
def _register():
    buildplan.register(KIND, place=place_tiles, verify=verify_lane, remove=remove_placed)
    return buildplan.KINDS[KIND]


def _plan_args(plan):
    """The slice of the plan that the buildplan record has to carry (JSON-safe)."""
    return {
        "ore": plan["ore"], "drill": plan["drill"], "pole": plan["pole"],
        "belt": plan["belt"], "lane_y": plan["lane_y"], "lane_dir": plan["lane_dir"],
        "lane_span": list(plan["lane_span"]),
        "from_xy": list(plan["from_xy"]), "to_xy": list(plan["to_xy"]),
        "entities": [{k: (list(v) if isinstance(v, tuple) else v)
                      for k, v in e.items()} for e in plan["entities"]],
        "poles": [[p["x"], p["y"]] for p in plan["entities"]
                  if p["role"] in ("pole", "bridge")],
        "grid_anchor": (plan.get("params") or {}).get("grid_anchor"),
    }


def place_tiles(rec, tiles):
    """buildplan place_fn. Places each tile with the entity the PLAN assigned to it, in role
    order, then wires the poles explicitly (script-placed poles do NOT auto-connect)."""
    import autopilot as A
    idx = {(int(e["x"]), int(e["y"])): e for e in rec["args"]["entities"]}
    todo = sorted((_xy(t) for t in tiles),
                  key=lambda t: (ROLE_RANK.get((idx.get(t) or {}).get("role"), 9), t[0], t[1]))
    out = {"placed": [], "already": [], "failed": []}
    for t in todo:
        e = idx.get(t)
        if e is None:
            out["failed"].append({"tile": t, "reason": "tile %s is not in the plan" % (t,)})
            continue
        res = (A.place(e["entity"], t[0], t[1], direction=int(e["direction"])) or "").strip()
        if res.startswith("BUILT"):
            out["placed"].append(t)
        else:
            out["failed"].append({"tile": t, "reason": res or "place returned nothing"})
    poles = [tuple(p) for p in (rec["args"].get("poles") or ())]
    if poles:
        w = wire_poles(poles, pole=rec["args"].get("pole") or "small-electric-pole",
                       anchor=rec["args"].get("grid_anchor"))
        out["wiring"] = w
        if not w.get("ok"):
            # buildplan.apply reads ONLY placed/already/failed off this dict - an extra
            # "wiring" key is dropped on the floor, so a wiring failure was invisible in the
            # audit record and only surfaced ~30s later as "the lane moves no ore". Say it
            # here, on a pole tile, while the reason is still known. The tile stays in
            # `placed` (it IS in the ground), so rollback's scope is unchanged.
            out["failed"].append({"tile": poles[0],
                                  "reason": "poles placed but NOT wired into one network: %s"
                                            % (w.get("detail") or w)})
    return out


def probe_placed(rec, tiles):
    """buildplan probe_fn: which of `tiles` already hold THE PLANNED ENTITY. RCON READ ONLY.

    buildplan.probe cannot be used here for two independent reasons, both of which this fixes:
      * it looks at tile+0.5 with radius 0.6, which never sees a 3x3 drill (probe_tile above),
        so every re-apply hands place_fn all 16 drills again;
      * it accepts a hit under ANY of the plan's names, so a leftover transport-belt sitting
        on a tile the plan wants a DRILL on reads as "already built" and the drill is silently
        never placed. Match the name the plan assigned to that tile.
    """
    idx = {(int(e["x"]), int(e["y"])): e["entity"] for e in rec["args"]["entities"]}
    want, back = [], {}
    for t in tiles:
        t = _xy(t)
        name = idx.get(t)
        if name is None:
            continue
        p = probe_tile(name, t[0], t[1])
        back[(p, name)] = t
        want.append(p)
    if not want:
        return set()
    names = sorted({n for (_p, n) in back})
    return {back[((e["x"], e["y"]), e["n"])]
            for e in buildplan._scan_tiles(want, names)
            if ((e["x"], e["y"]), e["n"]) in back}


def remove_placed(rec, tiles):
    """buildplan remove_fn (rollback + supersede). RCON WRITE, registry-scoped.

    Same defect, the destructive half: buildplan._default_remove looks for our entity at
    tile+0.5 within radius 0.8, and a 3x3 electric drill's centre is 1.414 away, so rollback
    tore out the belts and poles and left every drill standing while reporting them
    "not_found" - the exact litter BUILD LAW 2 exists to forbid, and what this module's own
    docstring promises never happens. Translate to centre tiles, delegate the actual removal
    to _default_remove (never reimplemented - it holds the refund, the belt-line drain, the
    guarded remove{count=0} and the 4KB batching), then translate the removed tiles BACK so
    rollback forgets the top-left tiles it recorded, not the centres.
    """
    idx = {(int(e["x"]), int(e["y"])): e["entity"] for e in (rec.get("args") or {})
           .get("entities", [])}
    fwd, unknown = {}, []
    for t in tiles:
        t = _xy(t)
        name = idx.get(t)
        if name is None:
            unknown.append(t)                  # not ours to name: leave it to the name list
            continue
        fwd.setdefault(probe_tile(name, t[0], t[1]), t)
    scope = sorted(fwd) + unknown
    out = buildplan._default_remove(rec, scope)
    gone = [fwd.get(_xy(t), _xy(t)) for t in (out.get("removed_tiles") or ())]
    return {"removed": len(gone), "not_found": len(tiles) - len(gone),
            "removed_tiles": gone}


def wire_poles(tiles, pole="small-electric-pole", anchor=None):
    """RCON WRITE. Wire every pole pair within reach EXPLICITLY, then verify by comparing
    electric_network_id.

    GOTCHAS 2026-08-30: two small poles 4.0 tiles apart (reach 7.5) sat on DIFFERENT
    electric_network_ids until wired by hand. Placement never implies connection.
    """
    import rcon
    pts = [_xy(t) for t in tiles] + ([_xy(anchor)] if anchor else [])
    if len(pts) < 1:
        return {"wired": 0, "networks": 0, "poles": 0, "ok": False,
                "detail": "no poles to wire"}
    reach = ML.POLES[pole]["wire"]
    spec = ";".join("%d,%d" % p for p in pts)
    lua = (
        "/sc local s=game.surfaces[1]; local P={};"
        "for a,b in ([==[" + spec + "]==]):gmatch('(-?%d+),(-?%d+)') do"
        "  local x,y=tonumber(a),tonumber(b);"
        "  local e=s.find_entities_filtered{name='" + pole + "',position={x+0.5,y+0.5},"
        "radius=0.8}[1];"
        "  if e and e.valid then P[#P+1]=e end end;"
        "local R=" + repr(float(reach)) + "; local n=0;"
        "for i=1,#P do for j=i+1,#P do local a,b=P[i],P[j];"
        "  local dx=a.position.x-b.position.x; local dy=a.position.y-b.position.y;"
        "  if dx*dx+dy*dy <= R*R then"
        "    local ca=a.get_wire_connector(defines.wire_connector_id.pole_copper,true);"
        "    local cb=b.get_wire_connector(defines.wire_connector_id.pole_copper,true);"
        "    if ca.connect_to(cb,false) then n=n+1 end end end end;"
        "local ids={}; local k=0;"
        "for _,e in ipairs(P) do local i=tostring(e.electric_network_id);"
        "  if not ids[i] then ids[i]=true; k=k+1 end end;"
        "rcon.print('WIRED '..n..' NETS '..k..' POLES '..#P)")
    out = (rcon.run(lua) or "").strip()
    got = {"wired": 0, "networks": 0, "poles": 0, "ok": False, "detail": out}
    parts = out.split()
    if len(parts) >= 6 and parts[0] == "WIRED":
        got.update(wired=int(parts[1]), networks=int(parts[3]), poles=int(parts[5]))
        got["ok"] = got["networks"] == 1 and got["poles"] == len(pts)
    return got


def verify_lane(rec):
    """buildplan verify_fn: does ore ACTUALLY move from the first drop tile to the terminus?

    Not "did create_entity return ok" and not "a BFS got close" -- lane_lint.verify_supply
    re-samples the tail after a settle and reports a moved item, so a full-but-frozen lane
    reads False (BUILD LAW 1).
    """
    import lane_lint
    a = rec["args"]
    r = lane_lint.verify_supply(a["ore"], tuple(a["from_xy"]), tuple(a["to_xy"]),
                                settle=a.get("settle", 3.0))
    sev = [f for f in (r.get("findings") or []) if f.get("severity") != "info"]
    detail = ("connected=%s moving=%s arrived=%s path=%d findings=%s"
              % (r.get("connected"), r.get("moving"), r.get("arrived"), r.get("path_len", 0),
                 [f.get("code") for f in sev][:6]))
    # CONNECTED IS THIS BUILD'S OWN RESULT; MOVING IS THE WORLD'S. `ok` gates
    # rollback_on_fail, so returning `connected and moving` tears out a CORRECT lane whenever
    # something downstream is stalled - a jammed array, blocked drills, an exhausted patch -
    # none of which a belt can fix, and all of which return next pass. That is the misreading
    # that had connect_mine_to_array remove 83 copper belts nine times in twelve minutes on
    # 2026-08-30 while every copper furnace sat at full_output.
    #
    # A lane that does not CONNECT genuinely failed and still rolls back. A connected one stays,
    # and the detail carries the real stall so the log accuses the right thing.
    ok = bool(r.get("connected"))
    if ok and not r.get("moving"):
        import bootstrap
        detail += " | KEPT: connected but idle - %s" % bootstrap.no_flow_reason(a["ore"])
    return {"ok": ok, "detail": detail}


def build(plan, *, tries=6, delay=5, rollback_on_fail=True, place_fn=None, verify_fn=None,
          record=None, force=False):
    """plan -> apply -> verify -> rollback. RCON WRITES (via buildplan/autopilot).

    The plan is refused before anything is placed if it fails its own invariants, if a human
    is connected (truce), if the ground changed since the scan (staleness), or if the route is
    operator-owned. If the lane does not actually move ore, everything this pass placed is
    removed in the SAME pass.
    """
    v = plan.get("validation") or validate(plan)
    if not v["ok"]:
        raise LayoutError("refusing to build an invalid plan:\n  " + "\n  ".join(v["errors"]))
    _register()
    rec = record or buildplan.new_plan(KIND, args=_plan_args(plan), tiles=plan_tiles(plan),
                                       names=sorted(plan["bom"]))
    return buildplan.apply(rec, place_fn=place_fn or place_tiles,
                           verify_fn=verify_fn or verify_lane, probe_fn=probe_placed,
                           tries=tries, delay=delay,
                           rollback_on_fail=rollback_on_fail, force=force)


def remove_tiles(tiles, names, reason="", scan_tick=None):
    """Refunding, registry-scoped teardown of exactly these tiles. RCON WRITE.

    Goes through a buildplan record so the removal is audited and the built-tile ledger stays
    honest (a tile we cannot find stays recorded as ours, so reconcile_removals can still
    protect an operator deletion).
    """
    tiles = [_xy(t) for t in tiles]
    if not tiles:
        return {"removed": 0, "not_found": 0}
    # TRUCE (BUILD LAW 6). This is a raw WRITE that does NOT go through buildplan.apply, so
    # none of apply's four gates run on it. Teardown is construction: a demolition crew tearing
    # belts out from under a connected human is exactly what the truce forbids.
    if buildplan._operator_present():
        return {"removed": 0, "not_found": len(tiles),
                "refused": "OPERATOR PRESENT: a human is connected; zero construction "
                           "(teardown included) until he logs off (truce)."}
    big = sorted({n for n in names if ML.size_of(n) != (1, 1)})
    if big:
        # _default_remove looks at tile+0.5 radius 0.8, which is blind to any entity whose
        # centre is further than that - a 3x3 drill's is 1.414 away. Silently leaving it
        # standing is the failure this module exists to prevent; say so instead.
        raise LayoutError(
            "remove_tiles is tile-addressed and can only find 1x1 entities; %s are larger. "
            "Tear those out through a buildplan record whose remove_fn is remove_placed "
            "(build()/rollback already does), not through this helper." % big)
    rec = buildplan.new_plan(KIND + "_teardown", args={"reason": reason}, tiles=tiles,
                             names=list(names), scan_tick=scan_tick)
    rec["verify"] = {"placed": [[x, y] for (x, y) in tiles]}
    buildplan.save(rec)
    out = buildplan.rollback(rec, tiles=tiles)
    rec = buildplan.load(rec["id"])
    rec["status"] = "superseded"
    rec["verify"]["superseded"] = {"reason": reason or "obsolete", "kept": 0,
                                   "removed": out["removed"],
                                   "not_found": out["not_found"]}
    buildplan.save(rec)
    return out


# --------------------------------------------------------------------------- tier upgrade
def replan_electric(old_plan, *, n_drills=None, patch=None, keep_lane=True, **kw):
    """RE-PLAN the outpost for the 3x3 electric footprint. NOT an in-place tier swap.

    electrify_mines swapped 2x2 burner drills for 3x3 electric ones AT THE SAME POSITION; the
    bigger footprint moved drop_position by half a tile and six copper drills dumped ore onto
    bare ground while every status read looked plausible. The lane ROW rule is
    tier-independent so it is kept by default, but the column lattice (pitch 2 -> 3), every
    drop tile, the lane span and the whole pole lattice are recomputed from the prototype.
    """
    p = dict(old_plan.get("params") or {})
    p.pop("output", None)
    p.update(kw)
    if n_drills is None:
        n_drills = p.get("n_drills")
    p["n_drills"] = n_drills
    if keep_lane:
        p["lane_y"] = old_plan["lane_y"]
    p.setdefault("pole", None)
    pole = p.pop("pole", None) or OPERATOR_MINE_SPEC["pole"]
    return plan_outpost(old_plan["ore"], p.pop("n_drills"),
                        patch=patch if patch is not None else old_plan.get("patch"),
                        drill=OPERATOR_MINE_SPEC["drill"], pole=pole,
                        belt=old_plan.get("belt") or OPERATOR_MINE_SPEC["belt"], **p)


def obsolete_fuel_tiles(new_plan, fuel_tiles):
    """The coal fuel belts that fed burner drills, minus any tile the electric layout needs.

    Belts cannot fuel a drill directly -- these are the coal lanes plus fuel-inserter feeds
    that existed only to keep burners alive. Once the mine is electric they carry nothing and
    BUILD LAW 2 applies: remove what does nothing, in the same pass.
    """
    keep = new_plan["tiles_used"]
    return sorted({_xy(t) for t in (fuel_tiles or ())} - keep)


def reusable_tiles(old_rec, new_plan):
    """The tiles the replacement genuinely REUSES -> supersede(keep=...).

    NOT `new_plan["tiles_used"]`. tiles_used is FOOTPRINTS, and a footprint tile is not reuse:
    a 3x3 electric drill cannot inherit the ground a 2x2 burner drill is standing on, so
    keeping the burner there does not save a placement, it BLOCKS one - the new drill fails to
    place and the mine comes up short while the teardown reports "kept". Reuse means the same
    entity NAME on the same TOP-LEFT tile; anything else is torn out and rebuilt.
    """
    new_by_tile = {(int(e["x"]), int(e["y"])): e["entity"] for e in new_plan["entities"]}
    old_ents = ((old_rec or {}).get("args") or {}).get("entities") or []
    if old_ents:
        old_by_tile = {(int(e["x"]), int(e["y"])): e["entity"] for e in old_ents}
        return sorted(t for t, n in old_by_tile.items() if new_by_tile.get(t) == n)
    # an older record with no per-tile entity map: fall back to its declared names. Still
    # narrower than footprints, and it never keeps a tile the new plan does not build on.
    names = set((old_rec or {}).get("names") or ())
    return sorted(t for t, n in new_by_tile.items() if n in names)


def upgrade_to_electric(ore, old_plan=None, *, patch=None, n_drills=None, fuel_tiles=(),
                        old_record_id=None, apply=True, **kw):
    """Re-plan a burner outpost as an all-electric one and (optionally) execute it.

    Returns {"plan", "superseded", "build", "fuel_removed", "obsolete_fuel"}. With apply=False
    nothing touches the server -- that is the reviewable, offline half.
    """
    if old_plan is not None:
        new = replan_electric(old_plan, n_drills=n_drills, patch=patch, **kw)
    else:
        # no prior plan record: plan the outpost from the patch alone (a burner mine the bot
        # built before buildplan existed still has to be re-planned, never swapped in place).
        new = plan_outpost(ore, n_drills, patch=patch,
                           drill=OPERATOR_MINE_SPEC["drill"],
                           pole=OPERATOR_MINE_SPEC["pole"], **kw)
    dead = obsolete_fuel_tiles(new, fuel_tiles)
    out = {"plan": new, "superseded": None, "build": None, "fuel_removed": None,
           "obsolete_fuel": dead}
    if not apply:
        return out
    if old_record_id:
        old_rec = buildplan.load(old_record_id)
        out["superseded"] = buildplan.supersede(
            old_rec, keep=reusable_tiles(old_rec, new),
            reason="re-planned as an all-electric outpost (3x3 footprint moves every drop tile)")
    out["build"] = build(new)
    if dead and (out["build"] or {}).get("status") == "verified":
        out["fuel_removed"] = remove_tiles(
            dead, BELT_NAMES,
            "coal fuel belts: dead weight once the drills stopped burning coal")
    return out


_register()

if __name__ == "__main__":
    import json as _json
    print(_json.dumps({"spec": OPERATOR_MINE_SPEC,
                       "derived": {
                           "drill_pitch": drill_pitch("electric-mining-drill"),
                           "supply_window": supply_window("electric-mining-drill",
                                                          "small-electric-pole"),
                           "pole_pitch": pole_pitch_for("electric-mining-drill",
                                                        "small-electric-pole"),
                           "pole_rows(lane_y=0)": pole_rows("electric-mining-drill",
                                                            "small-electric-pole", 0),
                       }}, indent=2))
