#!/usr/bin/env python3
"""Machine-checkable INVARIANTS for the operator's design principles.

Companion to OPERATOR-PRINCIPLES.md (the prose spec) and the GOTCHAS section
"THE OPERATOR'S DESIGN PRINCIPLES". Every function here is one testable law
reverse-engineered from Seth's 2026-08-29 hand-optimization of the bot's base.

    python3 principles.py                    # live check (READ-ONLY RCON)
    python3 principles.py --snapshot after   # check snapshots/after.json offline
    python3 principles.py --json             # machine-readable report

STRICTLY READ-ONLY. `probe()` issues find_entities_filtered + property reads and one
`storage._principles` scratch string it clears afterwards. Nothing here creates,
destroys, rotates or moves anything, and nothing registers an event handler
(GOTCHAS: runtime handlers lock human players out of the server).

Design: RCON is confined to `probe()`. Every check is a pure function of a `World`,
so the whole rule set unit-tests offline against synthetic entity dicts
(see test_principles.py).
"""
import json
import math
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
SNAPDIR = HERE / "snapshots"
CHUNK = 3000

# --------------------------------------------------------------------------- constants
# Prototype-derived (verified live 2026-08-29 against the running server; see
# OPERATOR-PRINCIPLES.md section "Measured prototype constants"). P5: never hardcode a
# pitch that a prototype can tell you — these are the fallbacks when RCON isn't available.
WIRE_REACH = 7.5          # small-electric-pole get_max_wire_distance()
SUPPLY_DIST = 2.5         # small-electric-pole get_supply_area_distance() -> 5x5 box
PITCH_TRUNK = 7.0         # 93% of wire reach: 0.5-tile slack so a nudged pole still wires
PITCH_POLE_ROW = 4.0      # poles inside a machine row
PITCH_DRILL = 3.0         # == electric-mining-drill tile_width
MAX_POLE_DEGREE = 4       # cap is 5; keep one free slot so later poles can auto-connect
MIN_POLE_SEP = 3.0        # measured minimum in the operator's base
UG_MAX_DISTANCE = 5       # underground-belt max_distance (basic tier)
# P1/P14 budgets. The operator's own base measures flow_coverage 0.9466 / dead 0.0534 —
# its 13 remaining dead belts are buried UNDER the bot's overlapping drills where no human
# can click them, so a 0.95 gate is literally unreachable on this map. The budget sits just
# outside the reference build so the reference PASSES; tighten it once P5 stops burying belts.
FLOW_COVERAGE_MIN = 0.94
DEAD_BELT_MAX = 0.06      # P14: tolerate ~5% residue, not 0%
TRUNK_MIN_LEN = 8         # a belt run this long counts as a trunk for duplicate detection

# 16-way direction -> unit vector. Belts/inserters/drills only ever use the cardinals.
DIRS = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}

BELTISH = {"transport-belt", "underground-belt", "splitter"}
MACHINES = {"mining-drill", "furnace", "assembling-machine", "lab", "boiler",
            "generator", "electric-turret", "radar", "rocket-silo", "beacon"}
CONTAINERS = {"container", "logistic-container", "linked-container"}
# Pole coverage is owed to ELECTRIC CONSUMERS, not to "machines". A stone furnace is a
# burner and needs no pole; an inserter does. The live discriminator is
# electric_network_id (present on drills/inserters/labs/assemblers, absent on burner
# furnaces, boilers, chests and belts); the type list catches consumers not yet on any
# network — which is exactly the interesting case, an unpowered drill.
NON_CONSUMER = {"electric-pole", "generator", "solar-panel", "accumulator", "boiler"}
CONSUMER_TYPES = {"mining-drill", "assembling-machine", "lab", "inserter", "radar",
                  "beacon", "roboport", "electric-turret", "pump"}
BURNERS = {"burner-mining-drill", "burner-inserter", "stone-furnace", "steel-furnace"}
# Statuses that mean "this entity is waiting on infrastructure that was never built".
DEBT_STATUS = {"waiting_for_target_to_be_built", "missing_science_packs",
               "no_research_in_progress", "no_power"}

# Fallback footprints for snapshot mode (live probe reads real bounding boxes).
PROTO_SIZE = {
    "electric-mining-drill": (3, 3), "burner-mining-drill": (2, 2),
    "stone-furnace": (2, 2), "steel-furnace": (2, 2), "electric-furnace": (3, 3),
    "assembling-machine-1": (3, 3), "assembling-machine-2": (3, 3),
    "lab": (3, 3), "boiler": (3, 2), "steam-engine": (3, 5),
    "offshore-pump": (1, 1), "splitter": (2, 1), "big-electric-pole": (2, 2),
}

# An inserter's `direction` points at its PICKUP side; it drops on the opposite side.
# Measured live 2026-08-29: inserter (-5.5,4.5) dir=8 -> pickup (-5.5,5.5), drop (-5.5,3.3).
# Getting this backwards silently inverts every producer/consumer in the flow graph.
INSERTER_PICKUP_IS_DIRECTION = True


# --------------------------------------------------------------------------- findings
def finding(check, principle, msg, pos=None, severity="error", **extra):
    f = {"check": check, "principle": principle, "severity": severity, "msg": msg}
    if pos is not None:
        f["pos"] = [round(pos[0], 2), round(pos[1], 2)]
    f.update(extra)
    return f


# --------------------------------------------------------------------------- world model
def _tile(v):
    return int(math.floor(v + 1e-9))


class World:
    """Normalized, indexed view of a set of entities. Pure data — no RCON.

    Each entity dict: n(name) t(type) x y [d(direction) s(status) e(network id)
    bb(int tile box l,t,r,b inclusive) dp(drop pos) pp(pickup pos) bg(ug type) r(recipe)].
    Missing bb/dp are derived from PROTO_SIZE so snapshots taken by snapshot_map.py
    (which does not capture them) still check.
    """

    def __init__(self, ents, meta=None):
        self.meta = dict(meta or {})
        self.ents = [self._norm(e) for e in ents if e.get("n") != "character"]
        self.by_tile = {}
        for e in self.ents:
            for t in e["tiles"]:
                self.by_tile.setdefault(t, []).append(e)
        self.belts = [e for e in self.ents if e["t"] in BELTISH]
        self.belt_tiles = {t for e in self.belts for t in e["tiles"]}
        self.poles = [e for e in self.ents if e["t"] == "electric-pole"]
        self.drills = [e for e in self.ents if e["t"] == "mining-drill"]
        self.inserters = [e for e in self.ents if e["t"] in ("inserter", "burner-inserter")]
        self.chests = [e for e in self.ents if e["t"] in CONTAINERS]
        self.machines = [e for e in self.ents if e["t"] in MACHINES]
        self.powered = [e for e in self.ents if self.needs_power(e)]

    @staticmethod
    def needs_power(e):
        if e["t"] in NON_CONSUMER or e.get("n") in BURNERS:
            return False
        return e.get("e") is not None or e["t"] in CONSUMER_TYPES

    # -- normalization ------------------------------------------------------
    def _norm(self, e):
        e = dict(e)
        e.setdefault("t", e.get("n", ""))
        x, y = float(e["x"]), float(e["y"])
        if "bb" not in e:
            w, h = PROTO_SIZE.get(e.get("n", ""), (1, 1))
            if e.get("d") in (4, 12) and (w, h) != (w, w):
                w, h = h, w                      # rotated 90 degrees
            e["bb"] = [_tile(x - w / 2.0), _tile(y - h / 2.0),
                       _tile(x + w / 2.0 - 0.01), _tile(y + h / 2.0 - 0.01)]
        l, t, r, b = e["bb"]
        e["tiles"] = [(tx, ty) for tx in range(l, r + 1) for ty in range(t, b + 1)]
        e["tile"] = (_tile(x), _tile(y))
        if e["t"] == "mining-drill" and "dp" not in e and e.get("d") in DIRS:
            dx, dy = DIRS[e["d"]]
            off = (e["bb"][3] - e["bb"][1] + 1) / 2.0 + 0.35
            e["dp"] = [x + dx * off, y + dy * off]
        if e["t"] in ("inserter", "burner-inserter") and e.get("d") in DIRS:
            dx, dy = DIRS[e["d"]]
            e.setdefault("pp", [x + dx, y + dy])
            e.setdefault("dp", [x - dx, y - dy])
        return e

    # -- queries ------------------------------------------------------------
    def at(self, tile):
        return self.by_tile.get(tile, [])

    def beltish_at(self, tile):
        return [e for e in self.at(tile) if e["t"] in BELTISH]

    def drop_tile(self, e):
        return (_tile(e["dp"][0]), _tile(e["dp"][1])) if e.get("dp") else None

    def pickup_tile(self, e):
        return (_tile(e["pp"][0]), _tile(e["pp"][1])) if e.get("pp") else None

    # -- belt graph (P1 / P7 / P9) -----------------------------------------
    def belt_graph(self):
        """Directed tile graph of material flow. Edges: a belt tile points at the tile
        it moves items into; an underground input jumps to its geometric partner; a
        splitter feeds BOTH of its output tiles from either input tile."""
        g = {}

        def add(a, b):
            if b in self.belt_tiles:
                g.setdefault(a, set()).add(b)

        for e in self.belts:
            d = e.get("d")
            if d not in DIRS:
                continue
            dx, dy = DIRS[d]
            if e["t"] == "splitter":
                outs = [(tx + dx, ty + dy) for (tx, ty) in e["tiles"]]
                for t in e["tiles"]:
                    for o in outs:
                        add(t, o)
                continue
            t = e["tiles"][0]
            if e["t"] == "underground-belt" and e.get("bg") == "input":
                p = self.ug_partner(e)
                if p:
                    add(t, p["tiles"][0])
                continue
            add(t, (t[0] + dx, t[1] + dy))
        return g

    def ug_partner(self, e):
        """Geometric partner for an underground belt (LuaEntity.neighbours reads nil
        over RCON in 2.0, so pair by scanning along the direction — same rule the game
        uses: nearest opposite-type mouth, same direction, within max_distance)."""
        d = e.get("d")
        if d not in DIRS:
            return None
        dx, dy = DIRS[d]
        want = "output" if e.get("bg") == "input" else "input"
        step = 1 if e.get("bg") == "input" else -1
        tx, ty = e["tiles"][0]
        for k in range(1, UG_MAX_DISTANCE + 1):
            t = (tx + dx * k * step, ty + dy * k * step)
            for o in self.at(t):
                if o["t"] == "underground-belt" and o.get("bg") == want and o.get("d") == d:
                    return o
        return None

    def producers(self):
        """Tiles where material ENTERS the belt network: drill drop tiles, and inserter
        drop tiles whose pickup is a machine (a furnace draining onto a belt)."""
        out = set()
        for e in self.drills:
            t = self.drop_tile(e)
            if t and t in self.belt_tiles:
                out.add(t)
        for e in self.inserters:
            dt, pt = self.drop_tile(e), self.pickup_tile(e)
            if dt in self.belt_tiles and pt is not None:
                if any(o["t"] in MACHINES or o["t"] in CONTAINERS for o in self.at(pt)):
                    out.add(dt)
        return out

    def consumers(self):
        """Tiles where material LEAVES the belt network: inserter pickup tiles on belts."""
        out = set()
        for e in self.inserters:
            pt, dt = self.pickup_tile(e), self.drop_tile(e)
            if pt in self.belt_tiles and dt is not None and self.at(dt):
                out.add(pt)
        return out

    def reach(self, seeds, graph=None, reverse=False):
        g = graph if graph is not None else self.belt_graph()
        if reverse:
            r = {}
            for a, bs in g.items():
                for b in bs:
                    r.setdefault(b, set()).add(a)
            g = r
        seen, stack = set(), [s for s in seeds if s in self.belt_tiles]
        seen.update(stack)
        while stack:
            for nxt in g.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def runs(self, min_len=1):
        """Maximal straight contiguous belt runs: list of (dir, [tiles...])."""
        remaining = {}
        for e in self.belts:
            if e["t"] == "transport-belt" and e.get("d") in DIRS:
                remaining[e["tiles"][0]] = e["d"]
        out = []
        while remaining:
            t, d = next(iter(remaining.items()))
            dx, dy = DIRS[d]
            chain = [t]
            del remaining[t]
            for sgn in (1, -1):
                cur = t
                while True:
                    nxt = (cur[0] + dx * sgn, cur[1] + dy * sgn)
                    if remaining.get(nxt) != d:
                        break
                    del remaining[nxt]
                    chain.append(nxt) if sgn == 1 else chain.insert(0, nxt)
                    cur = nxt
            if len(chain) >= min_len:
                out.append((d, chain))
        return out


# --------------------------------------------------------------------------- P1: flow
def no_belt_without_consumer(w):
    """P1. A belt is only real if an item can reach it from a producer AND leave it into
    a consumer. Bot: 189/470 (40%). Operator: 408/429 (95%). This one check subsumes
    most of P6-P10."""
    if not w.belts:
        return []
    g = w.belt_graph()
    fwd = w.reach(w.producers(), g)
    bwd = w.reach(w.consumers(), g, reverse=True)
    live = fwd & bwd
    dead = sorted(w.belt_tiles - live)
    cov = len(live) / float(len(w.belt_tiles))
    out = []
    if cov < FLOW_COVERAGE_MIN:
        out.append(finding(
            "no_belt_without_consumer", "P1",
            "flow coverage %.0f%% (%d/%d belt tiles on a producer->consumer path); "
            "min %.0f%%" % (cov * 100, len(live), len(w.belt_tiles), FLOW_COVERAGE_MIN * 100),
            coverage=round(cov, 4), dead=len(dead)))
    orphan_src = sorted(w.belt_tiles - fwd)
    orphan_dst = sorted(w.belt_tiles - bwd)
    for t in orphan_src[:40]:
        out.append(finding("no_belt_without_consumer", "P1",
                           "belt has no producer upstream", t, severity="warn"))
    for t in orphan_dst[:40]:
        out.append(finding("no_belt_without_consumer", "P1",
                           "belt has no consumer downstream", t, severity="warn"))
    return out


def dead_belt_fraction_ok(w):
    """P14. Tolerate ~5% residue, not 0% — a careful human pass leaves lead-ins and
    unclickable buried tiles behind."""
    if not w.belts:
        return []
    g = w.belt_graph()
    live = w.reach(w.producers(), g) & w.reach(w.consumers(), g, reverse=True)
    frac = 1.0 - len(live) / float(len(w.belt_tiles))
    if frac > DEAD_BELT_MAX:
        return [finding("dead_belt_fraction_ok", "P14",
                        "dead belts %.1f%% > %.0f%% budget" % (frac * 100, DEAD_BELT_MAX * 100),
                        fraction=round(frac, 4))]
    return []


# --------------------------------------------------------------------------- P2: power
def grid_is_single_network(w):
    """P2. One electric network, energized. The bot's base had net 1 (105 poles, the
    generators) and net 405 (6 drills + 2 poles, NO generator) — 0.56 tiles past wire
    reach after a 19-pole chain. Coal: 0/min."""
    nets = {}
    for e in w.ents:
        if e.get("e"):
            nets.setdefault(e["e"], []).append(e)
    out = []
    if len(nets) > 1:
        ranked = sorted(nets.items(), key=lambda kv: -len(kv[1]))
        root = ranked[0][0]
        for nid, members in ranked[1:]:
            gen = [m for m in members if m["t"] in ("generator", "solar-panel", "reactor")]
            ex = members[0]
            out.append(finding(
                "grid_is_single_network", "P2",
                "island network %s: %d entities, %d generators (root net %s)"
                % (nid, len(members), len(gen), root), (ex["x"], ex["y"]),
                network=nid, size=len(members), generators=len(gen)))
    for e in w.ents:
        if e.get("s") == "no_power":
            out.append(finding("grid_is_single_network", "P2",
                               "%s reads no_power" % e["n"], (e["x"], e["y"])))
    return out


def pole_degree_headroom(w):
    """P2. Cap pole degree at 4 of the 5 available copper slots. A saturated pole cannot
    adopt a later neighbour — that is how the bot stranded a whole lab block on its own
    network with two poles 4.0 tiles apart and no free slot to bridge."""
    out = []
    for p in w.poles:
        deg = p.get("deg")
        if deg is None:
            continue        # only the live probe knows real wire count; never guess it
        if deg > MAX_POLE_DEGREE:
            out.append(finding("pole_degree_headroom", "P2",
                               "pole at degree %d (cap %d, hard limit 5) — no slot left to "
                               "adopt a later neighbour" % (deg, MAX_POLE_DEGREE),
                               (p["x"], p["y"]), severity="warn", degree=deg))
    return out


def wire_reach_respected(w):
    """P2/P5. Never 'get close' to a network. Consecutive poles on a run must be inside
    get_max_wire_distance(), and never closer than the measured 3.0-tile minimum."""
    out = []
    pts = [(p["x"], p["y"]) for p in w.poles]
    for i, a in enumerate(pts):
        for b in pts[i + 1:]:
            d = math.dist(a, b)
            if d < MIN_POLE_SEP - 1e-6:
                out.append(finding("wire_reach_respected", "P5",
                                   "poles %.2f tiles apart (min %.1f) — redundant coverage"
                                   % (d, MIN_POLE_SEP), a, severity="warn"))
    for p in w.poles:
        near = [q for q in w.poles
                if q is not p and math.dist((p["x"], p["y"]), (q["x"], q["y"])) <= WIRE_REACH]
        if not near and len(w.poles) > 1:
            out.append(finding("wire_reach_respected", "P2",
                               "isolated pole: nearest neighbour beyond %.1f wire reach"
                               % WIRE_REACH, (p["x"], p["y"])))
    return out


# --------------------------------------------------------------------------- P3/P5: geometry
def every_drill_drops_on_lane(w):
    """P3/P7. A drill's drop tile must land on a belt, underground or container. A tier
    swap changes the footprint and MOVES the drop tile — six copper drills once dumped
    onto bare ground while every status read looked plausible."""
    out = []
    for e in w.drills:
        t = w.drop_tile(e)
        if t is None:
            continue
        tgt = [o for o in w.at(t) if o["t"] in BELTISH or o["t"] in CONTAINERS]
        if not tgt:
            out.append(finding("every_drill_drops_on_lane", "P3",
                               "%s drops on bare ground at %s" % (e["n"], t), (e["x"], e["y"])))
    return out


def no_entity_overlap(w):
    """P5. create_entity performs NO collision check, so wrong geometry succeeds
    silently. The bot's 6 iron drills at pitch 2 are 3x3: they overlap by a full tile
    column each, and 13 belts sit buried under them where no human can click."""
    out = []
    seen = set()
    for tile, ents in w.by_tile.items():
        solid = [e for e in ents if e["t"] not in ("electric-pole",)]
        if len(solid) < 2:
            continue
        key = tuple(sorted((e["n"], e["tile"]) for e in solid))
        if key in seen:
            continue
        seen.add(key)
        names = ", ".join(sorted({e["n"] for e in solid}))
        buried = [e for e in solid if e["t"] in BELTISH]
        blockers = [e for e in solid if e["t"] in MACHINES]
        if buried and blockers:
            out.append(finding("no_entity_overlap", "P5",
                               "belt buried under %s at %s — unclickable, a human cannot "
                               "remove it" % (blockers[0]["n"], tile), tile))
        else:
            where = " + ".join("%s(%.1f,%.1f)" % (e["n"], e["x"], e["y"]) for e in solid[:3])
            out.append(finding("no_entity_overlap", "P5",
                               "footprint collision at %s: %s [%s]" % (tile, names, where),
                               tile))
    return out


def drill_pitch_ok(w):
    """P5. Drill pitch along a row must equal the drill's tile width. Pitch 2 was
    hardcoded for the 2x2 burner drill and inherited by the 3x3 electric swap."""
    out = []
    rows = {}
    for e in w.drills:
        rows.setdefault((round(e["y"], 1), e["n"]), []).append(e["x"])
    for (y, name), xs in sorted(rows.items()):
        width = PROTO_SIZE.get(name, (3, 3))[0]
        xs = sorted(xs)
        for a, b in zip(xs, xs[1:]):
            if b - a < width - 1e-6:
                out.append(finding("drill_pitch_ok", "P5",
                                   "%s pitch %.1f on row y=%.1f (tile width %d) — footprints "
                                   "overlap" % (name, b - a, y, width), (a, y),
                                   pitch=round(b - a, 2), width=width))
    return out


def mine_row_geometry_ok(w):
    """P3. The 9-row mine template: POLE at lane-4, drills at lane-2 facing S, BELT lane,
    drills at lane+2 facing N, POLE at lane+4. Every drill row is exactly 2 from its
    lane, which keeps both pole rows inside the 2.49-tile mining radius (zero waste)."""
    out = []
    lanes = {}
    for e in w.drills:
        t = w.drop_tile(e)
        if t:
            lanes.setdefault(t[1], []).append(e)
    for lane_y, drills in sorted(lanes.items()):
        for e in drills:
            off = abs(e["y"] - (lane_y + 0.5))
            if abs(off - 2.0) > 0.51:
                out.append(finding("mine_row_geometry_ok", "P3",
                                   "drill %.1f tiles off its lane row (template: exactly 2)"
                                   % off, (e["x"], e["y"]), severity="warn",
                                   offset=round(off, 2)))
    return out


# --------------------------------------------------------------------------- P4/P8: poles
def no_pole_on_lane(w):
    """P8. A pole must never sit in a belt lane's span or on a drill's drop tile. The
    bot's reactive self-heal put a pole at (-9.5,20.5) inside the main N-S belt column's
    own line, and its mine pole line landed on the mine's belt row."""
    out = []
    drops = {w.drop_tile(e) for e in w.drills} - {None}
    for p in w.poles:
        for t in p["tiles"]:
            if t in w.belt_tiles:
                out.append(finding("no_pole_on_lane", "P8",
                                   "pole occupies belt tile %s" % (t,), (p["x"], p["y"])))
            elif t in drops:
                out.append(finding("no_pole_on_lane", "P8",
                                   "pole sits on a drill drop tile %s" % (t,), (p["x"], p["y"])))
    return out


def poles_cover_machines(w):
    """P4. Service infrastructure rides INSIDE the machine rows. Every pole must either
    power something or be a trunk hop. Bot: 40 of 107 poles (37%) powered nothing, and
    the network was still broken. Operator: 1.05 pole->machine incidences per machine."""
    out = []
    trunk = _trunk_poles(w)
    for p in w.poles:
        box = (p["x"] - SUPPLY_DIST, p["y"] - SUPPLY_DIST,
               p["x"] + SUPPLY_DIST, p["y"] + SUPPLY_DIST)
        covered = [m for m in w.powered if _box_hits(box, m)]
        if not covered and id(p) not in trunk:
            out.append(finding("poles_cover_machines", "P4",
                               "pole powers nothing and is not on a trunk run",
                               (p["x"], p["y"]), severity="warn"))
    # A pole whose ENTIRE supply box is already covered by other poles is pure cost:
    # 36 of the bot's 107 poles (34%) were fully redundant, 0 of the operator's.
    for p in w.poles:
        box = (p["x"] - SUPPLY_DIST, p["y"] - SUPPLY_DIST,
               p["x"] + SUPPLY_DIST, p["y"] + SUPPLY_DIST)
        mine = {id(m) for m in w.powered if _box_hits(box, m)}
        if not mine:
            continue
        others = set()
        for q in w.poles:
            if q is p:
                continue
            qb = (q["x"] - SUPPLY_DIST, q["y"] - SUPPLY_DIST,
                  q["x"] + SUPPLY_DIST, q["y"] + SUPPLY_DIST)
            others |= {id(m) for m in w.powered if _box_hits(qb, m)}
        if mine <= others and id(p) not in trunk and not _is_cut_vertex(w, p):
            out.append(finding("poles_cover_machines", "P4",
                               "pole is fully redundant: every machine it covers is already "
                               "covered by another pole, and removing it would not split the "
                               "network", (p["x"], p["y"]), severity="warn"))
    return out


def _pole_components(poles):
    """Connected components of the pole wire graph (adjacency = within wire reach)."""
    idx = {id(p): i for i, p in enumerate(poles)}
    adj = {i: set() for i in idx.values()}
    for i, p in enumerate(poles):
        for j, q in enumerate(poles):
            if i < j and math.dist((p["x"], p["y"]), (q["x"], q["y"])) <= WIRE_REACH:
                adj[i].add(j)
                adj[j].add(i)
    seen, comps = set(), 0
    for i in adj:
        if i in seen:
            continue
        comps += 1
        stack = [i]
        seen.add(i)
        while stack:
            for k in adj[stack.pop()]:
                if k not in seen:
                    seen.add(k)
                    stack.append(k)
    return comps


def _is_cut_vertex(w, p):
    """True if deleting this pole would split the wire graph. P4's service poles ARE the
    network inside a block (rows 3.0 apart, stacks 6.0 apart — both under 7.5), so a pole
    that looks redundant for COVERAGE is often load-bearing for CONNECTIVITY."""
    if not hasattr(w, "_base_comps"):
        w._base_comps = _pole_components(w.poles)
    rest = [q for q in w.poles if q is not p]
    return _pole_components(rest) > w._base_comps


def _box_hits(box, e):
    l, t, r, b = box
    el, et, er, eb = e["bb"]
    return not (er + 1 <= l or el >= r or eb + 1 <= t or et >= b)


def _collinear_groups(poles):
    cols, rows = {}, {}
    for p in poles:
        cols.setdefault(round(p["x"], 1), []).append(p)
        rows.setdefault(round(p["y"], 1), []).append(p)
    for key, group in list(cols.items()):
        if len(group) >= 3:
            yield "col", key, sorted(group, key=lambda q: q["y"])
    for key, group in list(rows.items()):
        if len(group) >= 3:
            yield "row", key, sorted(group, key=lambda q: q["x"])


def _trunk_poles(w):
    ids = set()
    for axis, _key, group in _collinear_groups(w.poles):
        vals = [q["y"] if axis == "col" else q["x"] for q in group]
        for i in range(len(group) - 1):
            if abs(vals[i + 1] - vals[i] - PITCH_TRUNK) < 0.51:
                ids.add(id(group[i]))
                ids.add(id(group[i + 1]))
    return ids


def trunk_pitch_ok(w):
    """P8. Trunks are straight, dedicated, parallel and at a fixed pitch. The operator's
    N-S trunk at x=-14.5 is 14 poles over 91 tiles: gaps 7,7,7,7,7,7,7,7,7,7,7,7,7. The
    bot's equivalent was a 19-pole staircase at 2.72 tiles/hop that never arrived."""
    out = []
    for axis, key, group in _collinear_groups(w.poles):
        vals = [q["y"] if axis == "col" else q["x"] for q in group]
        gaps = [round(vals[i + 1] - vals[i], 2) for i in range(len(vals) - 1)]
        # Only gaps in the plausible trunk band are hops; a huge gap is two modules, and
        # a short gap is a machine row. A SHORTER hop is always safe (it still wires) —
        # the invariant is one-sided: never EXCEED the pitch.
        hops = [g for g in gaps if PITCH_POLE_ROW + 0.51 < g <= WIRE_REACH * 2]
        if len(hops) < 2:
            continue
        bad = [g for g in hops if g > PITCH_TRUNK + 0.51]
        if bad:
            out.append(finding("trunk_pitch_ok", "P8",
                               "%s %s trunk hops %s exceed pitch %.1f (wire reach %.1f — no "
                               "slack left for a nudged pole)" % (axis, key, bad,
                                                                  PITCH_TRUNK, WIRE_REACH),
                               severity="warn", gaps=gaps))
    return out


# --------------------------------------------------------------------------- P6/P8: lanes
def one_lane_per_item_per_destination(w):
    """P6/P8. One column per commodity, shared from both sides. The bot laid three
    parallel iron lanes where one drop row exists because every re-lay left its
    predecessor standing; and it gave every commodity its own column rather than sharing
    the two transport lines of one belt."""
    out = []
    long_runs = [(d, c) for d, c in w.runs() if len(c) >= TRUNK_MIN_LEN]
    for i, (d1, c1) in enumerate(long_runs):
        for d2, c2 in long_runs[i + 1:]:
            if d1 != d2:
                continue
            horiz = DIRS[d1][0] != 0
            a1 = {t[0] for t in c1} if horiz else {t[1] for t in c1}
            a2 = {t[0] for t in c2} if horiz else {t[1] for t in c2}
            overlap = len(a1 & a2)
            off = abs((c1[0][1] - c2[0][1]) if horiz else (c1[0][0] - c2[0][0]))
            if overlap >= TRUNK_MIN_LEN and 0 < off <= 3:
                out.append(finding(
                    "one_lane_per_item_per_destination", "P6",
                    "parallel duplicate lanes %d tiles apart, %d tiles of shared span — "
                    "one lane fed from both sides carries +167%%" % (off, overlap),
                    c1[0], severity="warn", offset=off, overlap=overlap))
    return out


def lane_shared_from_both_sides(w):
    """P6. Two drill rows feed one lane. A lane fed from one side only wastes half its
    throughput: electric drills at pitch 3, single-sided = 0.167 ore/s per tile of row;
    double-sided = 0.333."""
    out = []
    lanes = {}
    for e in w.drills:
        t = w.drop_tile(e)
        if t:
            lanes.setdefault(t[1], []).append(e)
    for lane_y, drills in sorted(lanes.items()):
        sides = {1 if e["y"] > lane_y else -1 for e in drills}
        if len(sides) == 1 and len(drills) >= 3:
            out.append(finding("lane_shared_from_both_sides", "P6",
                               "lane y=%d fed from ONE side only by %d drills — the "
                               "opposite drill row is free capacity" % (lane_y, len(drills)),
                               (drills[0]["x"], float(lane_y)), severity="warn"))
    return out


def no_belt_into_wall(w):
    """P7/P9. Direction is computed from the destination. A belt must not terminate
    pointing into a building's bounding box: the bot left a 19-belt stub running south
    into the steam-engine block, 12 tiles short of the boiler it was feeding."""
    out = []
    for e in w.belts:
        if e["t"] != "transport-belt" or e.get("d") not in DIRS:
            continue
        dx, dy = DIRS[e["d"]]
        t = e["tiles"][0]
        nxt = (t[0] + dx, t[1] + dy)
        if nxt in w.belt_tiles:
            continue
        occupants = w.at(nxt)
        blockers = [o for o in occupants if o["t"] in MACHINES]
        if blockers:
            out.append(finding("no_belt_into_wall", "P7",
                               "belt points into %s — replan the corridor before the "
                               "obstacle, do not stop at it" % blockers[0]["n"], t))
    return out


def underground_pairs_complete(w):
    """P9. Cross underneath: every underground input needs a matching output. The bot's
    bare-pcall underground branch left 3 unpaired N-facing inputs stacked at
    (-11.5, 10.5/11.5/12.5) and 2 E inputs sharing 1 exit — a sealed dead end."""
    out = []
    for e in w.belts:
        if e["t"] != "underground-belt":
            continue
        p = w.ug_partner(e)
        if p is None:
            out.append(finding("underground_pairs_complete", "P9",
                               "underground %s has no partner within %d tiles — destroy the "
                               "entrance" % (e.get("bg"), UG_MAX_DISTANCE), (e["x"], e["y"])))
            continue
        if e.get("bg") == "input":
            span = max(abs(p["tiles"][0][0] - e["tiles"][0][0]),
                       abs(p["tiles"][0][1] - e["tiles"][0][1]))
            if span > UG_MAX_DISTANCE:
                out.append(finding("underground_pairs_complete", "P9",
                                   "underground span %d exceeds max_distance %d"
                                   % (span, UG_MAX_DISTANCE), (e["x"], e["y"])))
    return out


# --------------------------------------------------------------------------- P10: chests
def no_orphan_chest(w):
    """P10. Containers only terminate. A chest is legal at a true terminus; never a
    relay, a fuel buffer or a haul target. The operator deleted 9 wooden chests and their
    9 inserters — four complete build_io_cell shells whose assembler was never built."""
    out = []
    for c in w.chests:
        tiles = set(c["tiles"])
        served = [i for i in w.inserters
                  if (w.drop_tile(i) in tiles) or (w.pickup_tile(i) in tiles)]
        if not served:
            out.append(finding("no_orphan_chest", "P10",
                               "%s has no inserter on either side — remove the chest AND "
                               "its inserters together" % c["n"], (c["x"], c["y"])))
            continue
        # A chest between two belt segments of the same commodity is a relay, not a
        # terminus: throughput becomes a human walking.
        feeders = [i for i in w.inserters if w.drop_tile(i) in tiles
                   and w.pickup_tile(i) in w.belt_tiles]
        drainers = [i for i in w.inserters if w.pickup_tile(i) in tiles
                    and w.drop_tile(i) in w.belt_tiles]
        if feeders and drainers:
            out.append(finding("no_orphan_chest", "P10",
                               "%s relays belt->chest->belt — use a splitter; belts buffer, "
                               "chests only terminate" % c["n"], (c["x"], c["y"])))
    return out


def io_cell_is_atomic(w):
    """P10/P13. build_io_cell must be atomic: chest, inserter, MACHINE, inserter, chest —
    or nothing. Alternating waiting_for_space_in_destination / waiting_for_source_items
    around a gap is the cheap runtime detector for the unbuilt machine in between."""
    out = []
    for i in w.inserters:
        dt = w.drop_tile(i)
        if dt is not None and not w.at(dt) and i.get("s") in (
                "waiting_for_target_to_be_built", "waiting_for_source_items"):
            out.append(finding("io_cell_is_atomic", "P13",
                               "inserter drops onto an empty tile %s (status %s) — the "
                               "machine in the cell was never built" % (dt, i.get("s")),
                               (i["x"], i["y"]), severity="warn"))
    return out


# --------------------------------------------------------------------------- P11/P12: plant
def plant_ratio_ok(w):
    """P11. 2 boilers : 4 engines = 3.6 MW : 3.6 MW, exact. Refuse the orphan. The bot's
    _build_boiler_engine stacked engines onto ONE boiler, so at 3 engines a single 1.8 MW
    boiler could not feed them and the column walked further from its water each time."""
    out = []
    boilers = [e for e in w.ents if e["n"] == "boiler"]
    engines = [e for e in w.ents if e["t"] == "generator"]
    pumps = [e for e in w.ents if e["n"] == "offshore-pump"]
    if not boilers and not engines:
        return out
    if len(engines) != 2 * len(boilers):
        out.append(finding("plant_ratio_ok", "P11",
                           "%d boilers : %d engines (must be exactly 1:2)"
                           % (len(boilers), len(engines)),
                           boilers=len(boilers), engines=len(engines)))
    if engines and not pumps:
        out.append(finding("plant_ratio_ok", "P11", "generators with no offshore pump"))
    pipes = [e for e in w.ents if e["t"] in ("pipe", "pipe-to-ground")]
    if boilers and len(pipes) / float(len(boilers)) > 4.0:
        out.append(finding("plant_ratio_ok", "P6",
                           "%.1f pipes per boiler — one pipe on the shared spine column "
                           "feeds two boilers" % (len(pipes) / float(len(boilers))),
                           severity="warn"))
    return out


def plant_sited_at_fuel(w):
    """P12. Site the plant at the FUEL, not at the base. Electricity travels for the price
    of a pole every 7 tiles; coal must be physically belted. The operator accepted a
    104-tile electrical run to buy a 25-tile coal belt."""
    out = []
    boilers = [e for e in w.ents if e["n"] == "boiler"]
    if not boilers:
        return out
    bx = sum(e["x"] for e in boilers) / len(boilers)
    by = sum(e["y"] for e in boilers) / len(boilers)
    coal = [e for e in w.drills if e.get("res") == "coal"]
    if not coal:
        return out
    d = min(math.dist((bx, by), (e["x"], e["y"])) for e in coal)
    smelt = [e for e in w.ents if e["t"] == "furnace"]
    ds = min((math.dist((bx, by), (e["x"], e["y"])) for e in smelt), default=None)
    if ds is not None and d > ds:
        out.append(finding("plant_sited_at_fuel", "P12",
                           "plant is %.0f tiles from coal but only %.0f from the smelters — "
                           "score shore tiles by distance to the FUEL source" % (d, ds),
                           (bx, by), severity="warn"))
    return out


# --------------------------------------------------------------------------- P13: order
def no_consumer_ahead_of_supply(w):
    """P13. Build order is supply -> consumer, verified stage by stage. A consumer built
    early has negative value: it occupies tiles the real infrastructure needs (a lab sat
    exactly where the new steam engine now sits), demands pole coverage for nothing, and
    emits a false green signal."""
    out = []
    labs = [e for e in w.ents if e["n"] == "lab"]
    research = (w.meta.get("research") or "").strip()
    if labs and not research:
        out.append(finding("no_consumer_ahead_of_supply", "P13",
                           "%d labs built with NO research queued" % len(labs),
                           (labs[0]["x"], labs[0]["y"])))
    for e in w.ents:
        if e["t"] == "assembling-machine":
            tiles = set(e["tiles"])
            served = [i for i in w.inserters
                      if w.drop_tile(i) in tiles or w.pickup_tile(i) in tiles]
            if len(served) < 2:
                out.append(finding("no_consumer_ahead_of_supply", "P13",
                                   "%s has %d inserters (needs both an in and an out in the "
                                   "same pass)" % (e["n"], len(served)), (e["x"], e["y"])))
    debt = [e for e in w.ents if e.get("s") in DEBT_STATUS]
    if debt:
        kinds = {}
        for e in debt:
            kinds[e["s"]] = kinds.get(e["s"], 0) + 1
        out.append(finding("no_consumer_ahead_of_supply", "P13",
                           "%d entities waiting on infrastructure that was never built: %s"
                           % (len(debt), kinds), severity="warn", statuses=kinds))
    return out


def production_is_moving(w):
    """P1. A build is complete only when material is measurably MOVING. Entity count is
    not progress: 713 entities produced 0/0/0; 619 produced iron, copper and coal."""
    out = []
    prod = w.meta.get("production") or {}
    if not prod:
        return out
    for item, rate in sorted(prod.items()):
        if rate <= 0:
            out.append(finding("production_is_moving", "P1",
                               "%s production is %s/min — a sub-build that produces zero "
                               "flow must be torn down in the same pass" % (item, rate),
                               item=item, rate=rate))
    return out


# --------------------------------------------------------------------------- metrics
def metrics(w):
    """Measured numbers, no thresholds. These are the quantities the principles are ABOUT;
    a planner reports them per build stage so a regression is visible before it compounds.
    (Deliberately not assertions: the incidence ratio in particular is design-dependent —
    the operator's own double-sided smelter rows cover each furnace from both inserter
    rows on purpose.)"""
    m = {"entities": len(w.ents), "belts": len(w.belt_tiles), "poles": len(w.poles),
         "machines": len(w.machines), "drills": len(w.drills)}
    if w.belts:
        g = w.belt_graph()
        live = w.reach(w.producers(), g) & w.reach(w.consumers(), g, reverse=True)
        m["flow_coverage"] = round(len(live) / float(len(w.belt_tiles)), 4)
        m["dead_belts"] = len(w.belt_tiles) - len(live)
        m["turns"] = sum(1 for _d, c in w.runs()) - 1 if w.runs() else 0
    m["powered"] = len(w.powered)
    if w.powered and w.poles:
        inc = sum(1 for mm in w.powered for p in w.poles
                  if _box_hits((p["x"] - SUPPLY_DIST, p["y"] - SUPPLY_DIST,
                                p["x"] + SUPPLY_DIST, p["y"] + SUPPLY_DIST), mm))
        m["incidences_per_consumer"] = round(inc / float(len(w.powered)), 3)
        m["consumers_per_pole"] = round(len(w.powered) / float(len(w.poles)), 3)
    nets = {e["e"] for e in w.ents if e.get("e")}
    m["networks"] = len(nets)
    degs = [p["deg"] for p in w.poles if p.get("deg") is not None]
    if degs:
        m["max_pole_degree"] = max(degs)
        m["poles_at_cap"] = sum(1 for d in degs if d >= 5)
    return m


# --------------------------------------------------------------------------- registry
CHECKS = [
    no_belt_without_consumer,
    production_is_moving,
    dead_belt_fraction_ok,
    grid_is_single_network,
    pole_degree_headroom,
    wire_reach_respected,
    every_drill_drops_on_lane,
    mine_row_geometry_ok,
    no_entity_overlap,
    drill_pitch_ok,
    no_pole_on_lane,
    poles_cover_machines,
    trunk_pitch_ok,
    one_lane_per_item_per_destination,
    lane_shared_from_both_sides,
    no_belt_into_wall,
    underground_pairs_complete,
    no_orphan_chest,
    io_cell_is_atomic,
    plant_ratio_ok,
    plant_sited_at_fuel,
    no_consumer_ahead_of_supply,
]


def check_all(w, only=None):
    """Run every invariant. Returns {ok, errors, warnings, findings, by_check, by_principle}."""
    findings, by_check = [], {}
    for fn in CHECKS:
        if only and fn.__name__ not in only:
            continue
        try:
            got = fn(w) or []
        except Exception as exc:                                   # a check must never
            got = [finding(fn.__name__, "?",                       # break the report
                           "check raised %s: %s" % (type(exc).__name__, exc))]
        by_check[fn.__name__] = len(got)
        findings.extend(got)
    errors = [f for f in findings if f["severity"] == "error"]
    warnings = [f for f in findings if f["severity"] != "error"]
    by_principle = {}
    for f in findings:
        by_principle[f["principle"]] = by_principle.get(f["principle"], 0) + 1
    return {"ok": not errors, "errors": len(errors), "warnings": len(warnings),
            "findings": findings, "by_check": by_check, "by_principle": by_principle,
            "entities": len(w.ents), "belts": len(w.belts), "meta": w.meta,
            "metrics": metrics(w)}


# --------------------------------------------------------------------------- RCON (READ-ONLY)
PROBE_LUA = r"""/sc
local s=game.surfaces[1]
local SN={} for k,v in pairs(defines.entity_status) do SN[v]=k end
local o={}
for _,e in pairs(s.find_entities_filtered{force='player'}) do
 if e.name~='character' and e.name~='entity-ghost' then
  local bb=e.bounding_box
  local d={n=e.name,t=e.type,x=e.position.x,y=e.position.y,
           bb={math.floor(bb.left_top.x+0.5),math.floor(bb.left_top.y+0.5),
               math.ceil(bb.right_bottom.x-0.5)-1,math.ceil(bb.right_bottom.y-0.5)-1}}
  local a,v
  a,v=pcall(function() return e.direction end) if a and v then d.d=v end
  a,v=pcall(function() return e.status end) if a and v~=nil then d.s=SN[v] or v end
  a,v=pcall(function() return e.electric_network_id end) if a and v then d.e=v end
  a,v=pcall(function() return e.drop_position end) if a and v then d.dp={v.x,v.y} end
  a,v=pcall(function() return e.pickup_position end) if a and v then d.pp={v.x,v.y} end
  if e.type=='underground-belt' then d.bg=e.belt_to_ground_type end
  if e.type=='mining-drill' then
    a,v=pcall(function() return e.mining_target end)
    if a and v then d.res=v.name end end
  if e.type=='electric-pole' then
    local n=0
    local c=e.get_wire_connector(defines.wire_connector_id.pole_copper,false)
    if c then for _ in pairs(c.connections) do n=n+1 end end
    d.deg=n end
  o[#o+1]=d
 end
end
local f=game.forces.player
local ps=f.get_item_production_statistics(s)
local function pm(n) return math.floor(ps.get_flow_count{name=n,category='input',
  precision_index=defines.flow_precision_index.one_minute}) end
local g={tick=game.tick,research=(f.current_research and f.current_research.name or ''),
         production={['iron-plate']=pm('iron-plate'),['copper-plate']=pm('copper-plate'),
                     ['coal']=pm('coal')}}
storage._principles=helpers.table_to_json({ents=o,meta=g})
rcon.print(#storage._principles)
""".replace("\n", " ")


def probe():
    """READ-ONLY live read of the whole player force. Never mutates the world."""
    import rcon
    n = int((rcon.run(PROBE_LUA) or "0").strip() or "0")
    if not n:
        raise RuntimeError("probe returned nothing")
    parts, i = [], 1
    while i <= n:
        parts.append(rcon.run("/sc rcon.print(storage._principles:sub(%d,%d))"
                              % (i, i + CHUNK - 1)).rstrip("\r\n"))
        i += CHUNK
    rcon.run("/sc storage._principles=nil")
    data = json.loads("".join(parts))
    return World(data["ents"], data.get("meta"))


def from_snapshot(name):
    """Offline World from a snapshot_map.py capture (snapshots/<name>.json)."""
    p = pathlib.Path(name)
    if not p.exists():
        p = SNAPDIR / ("%s.json" % name)
    data = json.loads(p.read_text())
    g = data.get("globals", {})
    meta = {"tick": g.get("tick"), "research": g.get("research", ""),
            "production": {"iron-plate": g.get("iron_pm"), "copper-plate": g.get("copper_pm"),
                           "coal": g.get("coal_pm")}}
    return World(data["ents"], meta)


# --------------------------------------------------------------------------- CLI
def format_report(rep, limit=12):
    lines = ["=== PRINCIPLES REPORT === %d entities, %d belts"
             % (rep["entities"], rep["belts"]),
             "meta: %s" % rep["meta"],
             "metrics: %s" % rep["metrics"],
             "%s — %d errors, %d warnings"
             % ("PASS" if rep["ok"] else "FAIL", rep["errors"], rep["warnings"]), ""]
    groups = {}
    for f in rep["findings"]:
        groups.setdefault(f["check"], []).append(f)
    for name, fs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        sev = "ERROR" if any(f["severity"] == "error" for f in fs) else "warn "
        lines.append("[%s] %-34s %s (%d)" % (sev, name, fs[0]["principle"], len(fs)))
        for f in fs[:limit]:
            lines.append("        %s%s" % (f["msg"],
                                           (" @%s" % f["pos"]) if "pos" in f else ""))
        if len(fs) > limit:
            lines.append("        ... %d more" % (len(fs) - limit))
    if not groups:
        lines.append("no findings — every invariant holds")
    return "\n".join(lines)


def main(argv):
    only = None
    snap = None
    as_json = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--snapshot":
            i += 1
            snap = argv[i]
        elif a == "--json":
            as_json = True
        elif a == "--only":
            i += 1
            only = set(argv[i].split(","))
        i += 1
    w = from_snapshot(snap) if snap else probe()
    rep = check_all(w, only=only)
    print(json.dumps(rep, indent=1) if as_json else format_report(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
