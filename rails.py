#!/usr/bin/env python3
"""Factorio 2.x rail geometry: the engine-probed adjacency table + an A* that emits a
ready-to-place ghost chain between two rail poses.

Ported from chebykinn/factorio-planning-agent `agent-workspace/lib/rails.js` (63 lines).
The ADJ table below is that file's table VERBATIM (transcribed programmatically, not by
hand): for every rail piece+direction, the exact set of (piece, direction, center-offset)
the engine reports as connected via get_connected_rail. Do not hand-edit. This table is
the hard-won part - 1.1 rail blueprints are dead in 2.x (MEGABASE-V2-DESIGN.md:114), so
2.0 rail geometry has to be rebuilt from probes.

Usage (mirrors the JS):
    from rails import route, to_ghosts
    chain = route({"piece": "straight-rail", "dir": 0, "x": 601, "y": 601},
                  {"piece": "straight-rail", "dir": 4, "x": 625, "y": 577})
    autopilot.stamp_blueprint(to_ghosts(chain))     # rail centers ARE integers

Operational rule that goes with the table (factorio-tool SKILL.md:138-147): start from an
EXISTING rail, or place the FIRST piece alone and scan back its snapped center, then route
from there. Never hand-compose curved-rail-a/b chains.

Facts: rails snap to a 2-tile grid; positions are entity CENTERS (integers), never top-left
tiles - do NOT derive them from prototype tile_width/height, which is orientation-dependent
(curved-rail-a reports 2x4). dir 0 = N-S straight, 4 = E-W. curved-rail-a joins
straight<->half-diagonal, curved-rail-b joins half-diagonal<->diagonal-straight; a 90 deg
corner is the chain a-b-...-b-a. Every edge is reversible, so routing works from either end.

TWO UPSTREAM DEFECTS, carried knowingly and then fenced (see route()):
  1. h = manhattan/2 is INADMISSIBLE. One edge can displace 8 tiles (curved-rail-a|8 ->
     curved-rail-a|0 is dx=2,dy=6), so h overestimates by up to 4x. Paths are not optimal
     and nodes get re-expanded.
  2. Because the graph is undirected and nodes re-expand, the raw search can emit a chain
     that visits the SAME CENTER TWICE. Measured against the upstream node implementation:
     3 of 5 sample goals from (straight-rail,0,601,601) came back with a duplicate center
     (e.g. goal (straight-rail,4,611,591) -> 12 pieces with curved-rail-a twice at
     (606,589)). Every consecutive pair is a legal ADJ edge - the chain is graph-valid but
     doubles back, and placing both pieces at one center collides.
  route(strict=True) (the default) therefore validates and repairs; route(strict=False) is
  the bit-for-bit upstream behaviour, kept only for porting checks.

RCON: this module is PURE except verify_against_engine(), which does READ-ONLY prototype
probes (/sc rcon.print). It never creates, destroys or ghosts anything.
"""
import heapq

import rcon

DIRS = (0, 2, 4, 6, 8, 10, 12, 14)
PIECES = ("straight-rail", "half-diagonal-rail", "curved-rail-a", "curved-rail-b")

# Engine-probed 2.1.17 (read-only, 2026-08-29): prototypes.entity[n].items_to_place_this.
# All four pieces are built from the single item "rail" at these counts.
RAIL_ITEM_COST = {"straight-rail": 1, "half-diagonal-rail": 2,
                  "curved-rail-a": 3, "curved-rail-b": 3}

# Legacy 1.1 rail prototypes still exist on 2.1.17 and must NEVER be emitted - a legacy
# rail does not connect to a 2.0 rail. Probed sizes are recorded so verify_against_engine
# can notice if a save was migrated to something unexpected.
LEGACY = {"legacy-straight-rail": (2, 2), "legacy-curved-rail": (4, 8)}

# Probed geometry, used only by verify_against_engine (never to compute a center).
EXPECTED = {
    "straight-rail": {"type": "straight-rail", "size": (2, 2), "items": {"rail": 1}},
    "half-diagonal-rail": {"type": "half-diagonal-rail", "size": (2, 2), "items": {"rail": 2}},
    "curved-rail-a": {"type": "curved-rail-a", "size": (2, 4), "items": {"rail": 3}},
    "curved-rail-b": {"type": "curved-rail-b", "size": (2, 2), "items": {"rail": 3}},
}


class RailRouteError(Exception):
    """A route was found but is not placeable (self-overlapping chain, illegal edge)."""


# --------------------------------------------------------------------------- the table
ADJ = {
    "straight-rail|0": [
        {"piece": "curved-rail-a", "dir": 0, "dx": 0.0, "dy": -3.0},
        {"piece": "curved-rail-a", "dir": 10, "dx": 0.0, "dy": 3.0},
        {"piece": "curved-rail-a", "dir": 2, "dx": 0.0, "dy": -3.0},
        {"piece": "curved-rail-a", "dir": 8, "dx": 0.0, "dy": 3.0},
        {"piece": "straight-rail", "dir": 0, "dx": 0.0, "dy": -2.0},
        {"piece": "straight-rail", "dir": 0, "dx": 0.0, "dy": 2.0},
    ],
    "straight-rail|2": [
        {"piece": "curved-rail-b", "dir": 10, "dx": 3.0, "dy": -3.0},
        {"piece": "curved-rail-b", "dir": 12, "dx": 3.0, "dy": -3.0},
        {"piece": "curved-rail-b", "dir": 2, "dx": -3.0, "dy": 3.0},
        {"piece": "curved-rail-b", "dir": 4, "dx": -3.0, "dy": 3.0},
        {"piece": "straight-rail", "dir": 2, "dx": -2.0, "dy": 2.0},
        {"piece": "straight-rail", "dir": 2, "dx": 2.0, "dy": -2.0},
    ],
    "straight-rail|4": [
        {"piece": "curved-rail-a", "dir": 12, "dx": -3.0, "dy": 0.0},
        {"piece": "curved-rail-a", "dir": 14, "dx": -3.0, "dy": 0.0},
        {"piece": "curved-rail-a", "dir": 4, "dx": 3.0, "dy": 0.0},
        {"piece": "curved-rail-a", "dir": 6, "dx": 3.0, "dy": 0.0},
        {"piece": "straight-rail", "dir": 4, "dx": -2.0, "dy": 0.0},
        {"piece": "straight-rail", "dir": 4, "dx": 2.0, "dy": 0.0},
    ],
    "straight-rail|6": [
        {"piece": "curved-rail-b", "dir": 0, "dx": 3.0, "dy": 3.0},
        {"piece": "curved-rail-b", "dir": 14, "dx": 3.0, "dy": 3.0},
        {"piece": "curved-rail-b", "dir": 6, "dx": -3.0, "dy": -3.0},
        {"piece": "curved-rail-b", "dir": 8, "dx": -3.0, "dy": -3.0},
        {"piece": "straight-rail", "dir": 6, "dx": -2.0, "dy": -2.0},
        {"piece": "straight-rail", "dir": 6, "dx": 2.0, "dy": 2.0},
    ],
    "half-diagonal-rail|0": [
        {"piece": "curved-rail-a", "dir": 0, "dx": 2.0, "dy": 5.0},
        {"piece": "curved-rail-a", "dir": 8, "dx": -2.0, "dy": -5.0},
        {"piece": "curved-rail-b", "dir": 0, "dx": -2.0, "dy": -4.0},
        {"piece": "curved-rail-b", "dir": 8, "dx": 2.0, "dy": 4.0},
        {"piece": "half-diagonal-rail", "dir": 0, "dx": -2.0, "dy": -4.0},
        {"piece": "half-diagonal-rail", "dir": 0, "dx": 2.0, "dy": 4.0},
    ],
    "half-diagonal-rail|2": [
        {"piece": "curved-rail-a", "dir": 10, "dx": 2.0, "dy": -5.0},
        {"piece": "curved-rail-a", "dir": 2, "dx": -2.0, "dy": 5.0},
        {"piece": "curved-rail-b", "dir": 10, "dx": -2.0, "dy": 4.0},
        {"piece": "curved-rail-b", "dir": 2, "dx": 2.0, "dy": -4.0},
        {"piece": "half-diagonal-rail", "dir": 2, "dx": -2.0, "dy": 4.0},
        {"piece": "half-diagonal-rail", "dir": 2, "dx": 2.0, "dy": -4.0},
    ],
    "half-diagonal-rail|4": [
        {"piece": "curved-rail-a", "dir": 12, "dx": 5.0, "dy": -2.0},
        {"piece": "curved-rail-a", "dir": 4, "dx": -5.0, "dy": 2.0},
        {"piece": "curved-rail-b", "dir": 12, "dx": -4.0, "dy": 2.0},
        {"piece": "curved-rail-b", "dir": 4, "dx": 4.0, "dy": -2.0},
        {"piece": "half-diagonal-rail", "dir": 4, "dx": -4.0, "dy": 2.0},
        {"piece": "half-diagonal-rail", "dir": 4, "dx": 4.0, "dy": -2.0},
    ],
    "half-diagonal-rail|6": [
        {"piece": "curved-rail-a", "dir": 14, "dx": 5.0, "dy": 2.0},
        {"piece": "curved-rail-a", "dir": 6, "dx": -5.0, "dy": -2.0},
        {"piece": "curved-rail-b", "dir": 14, "dx": -4.0, "dy": -2.0},
        {"piece": "curved-rail-b", "dir": 6, "dx": 4.0, "dy": 2.0},
        {"piece": "half-diagonal-rail", "dir": 6, "dx": -4.0, "dy": -2.0},
        {"piece": "half-diagonal-rail", "dir": 6, "dx": 4.0, "dy": 2.0},
    ],
    "curved-rail-a|0": [
        {"piece": "curved-rail-a", "dir": 10, "dx": 0.0, "dy": 4.0},
        {"piece": "curved-rail-a", "dir": 8, "dx": -2.0, "dy": -6.0},
        {"piece": "curved-rail-a", "dir": 8, "dx": 0.0, "dy": 4.0},
        {"piece": "curved-rail-b", "dir": 0, "dx": -2.0, "dy": -5.0},
        {"piece": "half-diagonal-rail", "dir": 0, "dx": -2.0, "dy": -5.0},
        {"piece": "straight-rail", "dir": 0, "dx": 0.0, "dy": 3.0},
    ],
    "curved-rail-a|2": [
        {"piece": "curved-rail-a", "dir": 10, "dx": 0.0, "dy": 4.0},
        {"piece": "curved-rail-a", "dir": 10, "dx": 2.0, "dy": -6.0},
        {"piece": "curved-rail-a", "dir": 8, "dx": 0.0, "dy": 4.0},
        {"piece": "curved-rail-b", "dir": 2, "dx": 2.0, "dy": -5.0},
        {"piece": "half-diagonal-rail", "dir": 2, "dx": 2.0, "dy": -5.0},
        {"piece": "straight-rail", "dir": 0, "dx": 0.0, "dy": 3.0},
    ],
    "curved-rail-a|4": [
        {"piece": "curved-rail-a", "dir": 12, "dx": -4.0, "dy": 0.0},
        {"piece": "curved-rail-a", "dir": 12, "dx": 6.0, "dy": -2.0},
        {"piece": "curved-rail-a", "dir": 14, "dx": -4.0, "dy": 0.0},
        {"piece": "curved-rail-b", "dir": 4, "dx": 5.0, "dy": -2.0},
        {"piece": "half-diagonal-rail", "dir": 4, "dx": 5.0, "dy": -2.0},
        {"piece": "straight-rail", "dir": 4, "dx": -3.0, "dy": 0.0},
    ],
    "curved-rail-a|6": [
        {"piece": "curved-rail-a", "dir": 12, "dx": -4.0, "dy": 0.0},
        {"piece": "curved-rail-a", "dir": 14, "dx": -4.0, "dy": 0.0},
        {"piece": "curved-rail-a", "dir": 14, "dx": 6.0, "dy": 2.0},
        {"piece": "curved-rail-b", "dir": 6, "dx": 5.0, "dy": 2.0},
        {"piece": "half-diagonal-rail", "dir": 6, "dx": 5.0, "dy": 2.0},
        {"piece": "straight-rail", "dir": 4, "dx": -3.0, "dy": 0.0},
    ],
    "curved-rail-a|8": [
        {"piece": "curved-rail-a", "dir": 0, "dx": 0.0, "dy": -4.0},
        {"piece": "curved-rail-a", "dir": 0, "dx": 2.0, "dy": 6.0},
        {"piece": "curved-rail-a", "dir": 2, "dx": 0.0, "dy": -4.0},
        {"piece": "curved-rail-b", "dir": 8, "dx": 2.0, "dy": 5.0},
        {"piece": "half-diagonal-rail", "dir": 0, "dx": 2.0, "dy": 5.0},
        {"piece": "straight-rail", "dir": 0, "dx": 0.0, "dy": -3.0},
    ],
    "curved-rail-a|10": [
        {"piece": "curved-rail-a", "dir": 0, "dx": 0.0, "dy": -4.0},
        {"piece": "curved-rail-a", "dir": 2, "dx": -2.0, "dy": 6.0},
        {"piece": "curved-rail-a", "dir": 2, "dx": 0.0, "dy": -4.0},
        {"piece": "curved-rail-b", "dir": 10, "dx": -2.0, "dy": 5.0},
        {"piece": "half-diagonal-rail", "dir": 2, "dx": -2.0, "dy": 5.0},
        {"piece": "straight-rail", "dir": 0, "dx": 0.0, "dy": -3.0},
    ],
    "curved-rail-a|12": [
        {"piece": "curved-rail-a", "dir": 4, "dx": -6.0, "dy": 2.0},
        {"piece": "curved-rail-a", "dir": 4, "dx": 4.0, "dy": 0.0},
        {"piece": "curved-rail-a", "dir": 6, "dx": 4.0, "dy": 0.0},
        {"piece": "curved-rail-b", "dir": 12, "dx": -5.0, "dy": 2.0},
        {"piece": "half-diagonal-rail", "dir": 4, "dx": -5.0, "dy": 2.0},
        {"piece": "straight-rail", "dir": 4, "dx": 3.0, "dy": 0.0},
    ],
    "curved-rail-a|14": [
        {"piece": "curved-rail-a", "dir": 4, "dx": 4.0, "dy": 0.0},
        {"piece": "curved-rail-a", "dir": 6, "dx": -6.0, "dy": -2.0},
        {"piece": "curved-rail-a", "dir": 6, "dx": 4.0, "dy": 0.0},
        {"piece": "curved-rail-b", "dir": 14, "dx": -5.0, "dy": -2.0},
        {"piece": "half-diagonal-rail", "dir": 6, "dx": -5.0, "dy": -2.0},
        {"piece": "straight-rail", "dir": 4, "dx": 3.0, "dy": 0.0},
    ],
    "curved-rail-b|0": [
        {"piece": "curved-rail-a", "dir": 0, "dx": 2.0, "dy": 5.0},
        {"piece": "curved-rail-b", "dir": 6, "dx": -4.0, "dy": -4.0},
        {"piece": "curved-rail-b", "dir": 8, "dx": -4.0, "dy": -4.0},
        {"piece": "curved-rail-b", "dir": 8, "dx": 2.0, "dy": 4.0},
        {"piece": "half-diagonal-rail", "dir": 0, "dx": 2.0, "dy": 4.0},
        {"piece": "straight-rail", "dir": 6, "dx": -3.0, "dy": -3.0},
    ],
    "curved-rail-b|2": [
        {"piece": "curved-rail-a", "dir": 2, "dx": -2.0, "dy": 5.0},
        {"piece": "curved-rail-b", "dir": 10, "dx": -2.0, "dy": 4.0},
        {"piece": "curved-rail-b", "dir": 10, "dx": 4.0, "dy": -4.0},
        {"piece": "curved-rail-b", "dir": 12, "dx": 4.0, "dy": -4.0},
        {"piece": "half-diagonal-rail", "dir": 2, "dx": -2.0, "dy": 4.0},
        {"piece": "straight-rail", "dir": 2, "dx": 3.0, "dy": -3.0},
    ],
    "curved-rail-b|4": [
        {"piece": "curved-rail-a", "dir": 4, "dx": -5.0, "dy": 2.0},
        {"piece": "curved-rail-b", "dir": 10, "dx": 4.0, "dy": -4.0},
        {"piece": "curved-rail-b", "dir": 12, "dx": -4.0, "dy": 2.0},
        {"piece": "curved-rail-b", "dir": 12, "dx": 4.0, "dy": -4.0},
        {"piece": "half-diagonal-rail", "dir": 4, "dx": -4.0, "dy": 2.0},
        {"piece": "straight-rail", "dir": 2, "dx": 3.0, "dy": -3.0},
    ],
    "curved-rail-b|6": [
        {"piece": "curved-rail-a", "dir": 6, "dx": -5.0, "dy": -2.0},
        {"piece": "curved-rail-b", "dir": 0, "dx": 4.0, "dy": 4.0},
        {"piece": "curved-rail-b", "dir": 14, "dx": -4.0, "dy": -2.0},
        {"piece": "curved-rail-b", "dir": 14, "dx": 4.0, "dy": 4.0},
        {"piece": "half-diagonal-rail", "dir": 6, "dx": -4.0, "dy": -2.0},
        {"piece": "straight-rail", "dir": 6, "dx": 3.0, "dy": 3.0},
    ],
    "curved-rail-b|8": [
        {"piece": "curved-rail-a", "dir": 8, "dx": -2.0, "dy": -5.0},
        {"piece": "curved-rail-b", "dir": 0, "dx": -2.0, "dy": -4.0},
        {"piece": "curved-rail-b", "dir": 0, "dx": 4.0, "dy": 4.0},
        {"piece": "curved-rail-b", "dir": 14, "dx": 4.0, "dy": 4.0},
        {"piece": "half-diagonal-rail", "dir": 0, "dx": -2.0, "dy": -4.0},
        {"piece": "straight-rail", "dir": 6, "dx": 3.0, "dy": 3.0},
    ],
    "curved-rail-b|10": [
        {"piece": "curved-rail-a", "dir": 10, "dx": 2.0, "dy": -5.0},
        {"piece": "curved-rail-b", "dir": 2, "dx": -4.0, "dy": 4.0},
        {"piece": "curved-rail-b", "dir": 2, "dx": 2.0, "dy": -4.0},
        {"piece": "curved-rail-b", "dir": 4, "dx": -4.0, "dy": 4.0},
        {"piece": "half-diagonal-rail", "dir": 2, "dx": 2.0, "dy": -4.0},
        {"piece": "straight-rail", "dir": 2, "dx": -3.0, "dy": 3.0},
    ],
    "curved-rail-b|12": [
        {"piece": "curved-rail-a", "dir": 12, "dx": 5.0, "dy": -2.0},
        {"piece": "curved-rail-b", "dir": 2, "dx": -4.0, "dy": 4.0},
        {"piece": "curved-rail-b", "dir": 4, "dx": -4.0, "dy": 4.0},
        {"piece": "curved-rail-b", "dir": 4, "dx": 4.0, "dy": -2.0},
        {"piece": "half-diagonal-rail", "dir": 4, "dx": 4.0, "dy": -2.0},
        {"piece": "straight-rail", "dir": 2, "dx": -3.0, "dy": 3.0},
    ],
    "curved-rail-b|14": [
        {"piece": "curved-rail-a", "dir": 14, "dx": 5.0, "dy": 2.0},
        {"piece": "curved-rail-b", "dir": 6, "dx": -4.0, "dy": -4.0},
        {"piece": "curved-rail-b", "dir": 6, "dx": 4.0, "dy": 2.0},
        {"piece": "curved-rail-b", "dir": 8, "dx": -4.0, "dy": -4.0},
        {"piece": "half-diagonal-rail", "dir": 6, "dx": 4.0, "dy": 2.0},
        {"piece": "straight-rail", "dir": 6, "dx": -3.0, "dy": -3.0},
    ],
}


def neighbors(piece, direction):
    """Connectable successors of a rail pose -> [{piece, dir, dx, dy}] (rails.js:23-25).
    Unknown piece/direction -> []."""
    return ADJ.get("%s|%d" % (piece, direction), [])


def _whole(v):
    return int(v) if float(v).is_integer() else v


def _edge(a, b):
    """True if pose b is an ADJ successor of pose a. Poses are the emitted ghost dicts."""
    for n in neighbors(a["name"], a["direction"]):
        if (n["piece"] == b["name"] and n["dir"] == b["direction"]
                and a["x"] + n["dx"] == b["x"] and a["y"] + n["dy"] == b["y"]):
            return True
    return False


# --------------------------------------------------------------------------- search
def _astar(start, goal, max_iter, hdiv, blocked):
    """rails.js:27-61 verbatim in semantics. `hdiv` is the heuristic divisor (2 = upstream
    and inadmissible, 8 = admissible since 8 is the max single-edge manhattan displacement).
    `blocked` is a set of centers the repair pass forbids; upstream has no such set.

    Upstream re-sorts the open list by f and shifts it, i.e. min-f with ties in insertion
    order (JS sort is stable). heapq on (f, seq) is exactly that, so paths are identical."""
    def key(s):
        return "%s|%d|%s|%s" % (s["piece"], s["dir"], s["x"], s["y"])

    def h(s):
        return (abs(s["x"] - goal["x"]) + abs(s["y"] - goal["y"])) / hdiv

    seq = 0
    open_ = [(h(start), seq, start, 0)]
    came = {}
    gbest = {key(start): 0}
    it = 0
    while open_ and it < max_iter:
        it += 1
        _f, _q, s, g = heapq.heappop(open_)
        if (s["piece"] == goal["piece"] and s["dir"] == goal["dir"]
                and abs(s["x"] - goal["x"]) <= 1 and abs(s["y"] - goal["y"]) <= 1):
            path, k = [s], key(s)
            while k in came:
                prev = came[k]
                path.insert(0, prev)
                k = key(prev)
            # Rail centers are integers; the ADJ offsets are floats, so normalise the sum
            # back to int - a "601.0" in a stamp_blueprint spec is legal Lua but reads as
            # a bug, and callers key tiles off these coordinates.
            return [{"name": p["piece"], "x": _whole(p["x"]), "y": _whole(p["y"]),
                     "direction": p["dir"]} for p in path]
        for n in neighbors(s["piece"], s["dir"]):
            nx = {"piece": n["piece"], "dir": n["dir"],
                  "x": s["x"] + n["dx"], "y": s["y"] + n["dy"]}
            if blocked and (nx["x"], nx["y"]) in blocked:
                continue
            nk = key(nx)
            ng = g + 1
            if ng < gbest.get(nk, float("inf")):
                gbest[nk] = ng
                came[nk] = s
                seq += 1
                heapq.heappush(open_, (ng + h(nx), seq, nx, ng))
    return None      # no route within budget - check parity/orientation of the goal


def validate_chain(pieces):
    """(ok, problem). A chain is placeable only if every consecutive pair is a real ADJ edge
    AND no two pieces share a center - two rails at one center collide, which is exactly
    what the upstream search emits on ~half of non-trivial goals."""
    if not pieces:
        return False, "empty chain"
    seen = {}
    for i, p in enumerate(pieces):
        if p["name"] not in PIECES:
            return False, "piece %d is %r (not a 2.0 rail; legacy rails never connect)" % (i, p["name"])
        c = (p["x"], p["y"])
        if c in seen:
            return False, "two pieces share center %s (index %d and %d)" % (c, seen[c], i)
        seen[c] = i
    for i in range(len(pieces) - 1):
        if not _edge(pieces[i], pieces[i + 1]):
            return False, ("illegal edge %d->%d: %s|%d@(%s,%s) -> %s|%d@(%s,%s)"
                           % (i, i + 1,
                              pieces[i]["name"], pieces[i]["direction"], pieces[i]["x"], pieces[i]["y"],
                              pieces[i + 1]["name"], pieces[i + 1]["direction"], pieces[i + 1]["x"], pieces[i + 1]["y"]))
    return True, None


def _trim_loops(pieces):
    """If a center repeats, try dropping the span between the two occurrences (keeping each
    end in turn) and accept whichever splice is still a legal chain. Cheap and usually
    enough; when it is not, route() escalates instead of returning a colliding list."""
    cur = list(pieces)
    for _ in range(len(pieces)):
        ok, _p = validate_chain(cur)
        if ok:
            return cur
        seen, dup = {}, None
        for i, p in enumerate(cur):
            c = (p["x"], p["y"])
            if c in seen:
                dup = (seen[c], i)
                break
            seen[c] = i
        if dup is None:
            return cur                       # failure is an illegal edge, not a loop
        i, j = dup
        for cand in (cur[:i] + cur[j:], cur[:i + 1] + cur[j + 1:]):
            if len(cand) >= 1 and validate_chain(cand)[0]:
                return cand
        cur = cur[:i] + cur[j:]              # keep shrinking; re-check next lap
    return cur


def route(start, goal, max_iter=20000, strict=True, heuristic="upstream"):
    """Rail chain from pose `start` to pose `goal` -> [{name,x,y,direction}] or None.

    Poses are {piece, dir, x, y} with x,y the entity CENTER. The returned list INCLUDES
    both ends. The goal test is upstream's: same piece, same direction, and within 1 tile -
    so the last emitted pose may be +/-1 off the requested goal. Callers must not assume
    exact goal coordinates.

    strict=True (default) refuses to hand a colliding chain to the game. It runs the
    upstream search, then (only if that chain is unplaceable) a repair ladder: trim the
    loop, re-search with the offending centers forbidden, re-search with the admissible
    heuristic. Of every candidate that validates it returns the SHORTEST - fewer pieces is
    strictly cheaper and the upstream chain has no optimality claim to preserve. If nothing
    validates it raises RailRouteError rather than returning garbage.
    strict=False is bit-for-bit upstream (defects included) - porting checks only.

    heuristic="upstream" is rails.js's inadmissible h=manhattan/2; "admissible" is
    h=manhattan/8 (8 = the largest single-edge manhattan displacement, so it never
    overestimates; slower, cost-optimal paths)."""
    hdiv = {"upstream": 2.0, "admissible": 8.0}.get(heuristic)
    if hdiv is None:
        raise ValueError("heuristic must be 'upstream' or 'admissible', got %r" % (heuristic,))
    path = _astar(start, goal, max_iter, hdiv, blocked=None)
    if not strict:
        return path
    if path is None:
        return None
    ok, problem = validate_chain(path)
    if ok:
        return path
    good = []
    trimmed = _trim_loops(path)
    if validate_chain(trimmed)[0]:
        good.append(trimmed)
    blocked, cur = set(), path
    for _ in range(6):                       # forbid each colliding center, re-search
        dup = _first_dup_center(cur)
        if dup is None:
            break
        blocked.add(dup)
        cur = _astar(start, goal, max_iter, hdiv, blocked=blocked)
        if cur is None:
            break
        ok2, prob2 = validate_chain(cur)
        if ok2:
            good.append(cur)
            break
        problem = prob2
    alt = _astar(start, goal, max_iter, 8.0, blocked=None) if hdiv != 8.0 else None
    if alt is not None:
        alt = _trim_loops(alt)
        if validate_chain(alt)[0]:
            good.append(alt)
    if good:
        return min(good, key=len)
    raise RailRouteError(
        "no placeable chain %s|%d@(%s,%s) -> %s|%d@(%s,%s): %s"
        % (start["piece"], start["dir"], start["x"], start["y"],
           goal["piece"], goal["dir"], goal["x"], goal["y"], problem))


def _first_dup_center(pieces):
    seen = set()
    for p in pieces or ():
        c = (p["x"], p["y"])
        if c in seen:
            return c
        seen.add(c)
    return None


# --------------------------------------------------------------------------- emit
def bom(pieces):
    """Bill of materials for a chain -> {"rail": N}. Every 2.0 rail piece is built from the
    single item "rail"; a curve costs 3, a half-diagonal 2, a straight 1 (engine-probed)."""
    n = 0
    for p in pieces or ():
        if p["name"] not in RAIL_ITEM_COST:
            raise ValueError("not a placeable 2.0 rail: %r" % (p["name"],))
        n += RAIL_ITEM_COST[p["name"]]
    return {"rail": n} if n else {}


def to_ghosts(pieces):
    """-> autopilot.stamp_blueprint shape [{name,x,y,dir}]. Rail positions ARE centers and
    ARE integers, so they pass through unchanged - never add tile_width/2 here."""
    return [{"name": p["name"], "x": p["x"], "y": p["y"], "dir": p["direction"]}
            for p in pieces or ()]


# --------------------------------------------------------------------------- engine check
def _probe(lua):
    return rcon.run("/sc " + lua)


def verify_against_engine():
    """READ-ONLY confirmation that this table's piece names/geometry match the live server.
    Probes prototypes only - it never creates, destroys or ghosts a rail.

    -> {ok, missing:[names], geometry:{name:(tw,th)}, items:{name:{item:count}},
        adjacency:"skipped ..."|"checked ...", notes:[...]}

    Adjacency is only spot-checked when rails already exist on the surface: confirming an
    ADJ edge needs a real LuaRail.get_connected_rail, and manufacturing one would be a
    write. Never place a rail to test the table."""
    names = list(PIECES) + list(LEGACY)
    lua = ("local out={};"
           "for _,n in ipairs{" + ",".join("'%s'" % n for n in names) + "} do"
           "  local p=prototypes.entity[n];"
           "  if p then local it={};"
           "    if p.items_to_place_this then for _,i in pairs(p.items_to_place_this) do"
           "      it[#it+1]=i.name..':'..i.count end end;"
           "    out[#out+1]=n..'|'..p.type..'|'..p.tile_width..'|'..p.tile_height..'|'..table.concat(it,',')"
           "  else out[#out+1]=n..'|MISSING' end end;"
           "rcon.print(table.concat(out,';'))")
    res = {"ok": True, "missing": [], "geometry": {}, "items": {},
           "adjacency": "skipped", "notes": []}
    raw = (_probe(lua) or "").strip()
    seen = set()
    for rec in raw.split(";"):
        rec = rec.strip()
        if not rec:
            continue
        f = rec.split("|")
        name = f[0]
        seen.add(name)
        if len(f) < 5 or f[1] == "MISSING":
            if name in PIECES:
                res["missing"].append(name)
                res["ok"] = False
            else:
                res["notes"].append("legacy prototype %s absent (fine - never emitted)" % name)
            continue
        size = (int(f[2]), int(f[3]))
        items = {}
        for pair in f[4].split(","):
            if pair:
                k, _, v = pair.partition(":")
                items[k] = int(float(v))
        res["geometry"][name] = size
        res["items"][name] = items
        if name in EXPECTED:
            exp = EXPECTED[name]
            if f[1] != exp["type"]:
                res["ok"] = False
                res["notes"].append("%s type %s != expected %s" % (name, f[1], exp["type"]))
            if size != exp["size"]:
                res["ok"] = False
                res["notes"].append("%s size %sx%s != expected %sx%s"
                                    % (name, size[0], size[1], exp["size"][0], exp["size"][1]))
            if items != exp["items"]:
                res["ok"] = False
                res["notes"].append("%s items %s != expected %s" % (name, items, exp["items"]))
        elif name in LEGACY and size != LEGACY[name]:
            res["notes"].append("%s size %sx%s != probed %sx%s (save migrated?)"
                                % (name, size[0], size[1], LEGACY[name][0], LEGACY[name][1]))
    for n in PIECES:
        if n not in seen:
            res["missing"].append(n)
            res["ok"] = False
    if res["missing"]:
        res["notes"].append("missing 2.0 rail prototypes: %s" % ", ".join(sorted(set(res["missing"]))))

    count = (_probe("local s=game.surfaces[1];"
                    "rcon.print(#s.find_entities_filtered{type={" +
                    ",".join("'%s'" % p for p in PIECES) + "}})") or "0").strip()
    try:
        n_rails = int(count)
    except ValueError:
        n_rails = 0
    if n_rails == 0:
        res["adjacency"] = ("skipped (no rails on surface; a spot-check requires an "
                            "existing rail - never place one to test)")
    else:
        res["adjacency"] = _spot_check_adjacency(n_rails, res)
    return res


def _spot_check_adjacency(n_rails, res):
    """Walk one existing rail's get_connected_rail results and confirm each is an ADJ edge.
    READ ONLY - it only reads rails that are already there."""
    raw = (_probe(
        "local s=game.surfaces[1];"
        "local r=s.find_entities_filtered{type={" + ",".join("'%s'" % p for p in PIECES) + "}}[1];"
        "if not r then rcon.print('none') return end;"
        "local out={r.name..'|'..tostring(r.direction)..'|'..r.position.x..'|'..r.position.y};"
        "for _,dir in pairs{defines.rail_direction.front, defines.rail_direction.back} do"
        "  for _,cd in pairs(defines.rail_connection_direction) do"
        "    local ok,c=pcall(function() return r.get_connected_rail{rail_direction=dir,rail_connection_direction=cd} end);"
        "    if ok and c then out[#out+1]=c.name..'|'..tostring(c.direction)..'|'..c.position.x..'|'..c.position.y end"
        "  end end;"
        "rcon.print(table.concat(out,';'))") or "").strip()
    if not raw or raw == "none":
        return "skipped (surface reports %d rails but none readable)" % n_rails
    recs = [r.split("|") for r in raw.split(";") if r.strip()]
    base = {"name": recs[0][0], "direction": int(float(recs[0][1])),
            "x": float(recs[0][2]), "y": float(recs[0][3])}
    bad = 0
    for r in recs[1:]:
        nb = {"name": r[0], "direction": int(float(r[1])), "x": float(r[2]), "y": float(r[3])}
        if not _edge(base, nb):
            bad += 1
            res["ok"] = False
            res["notes"].append("engine connects %s|%d@(%s,%s) -> %s|%d@(%s,%s) but ADJ does not"
                                % (base["name"], base["direction"], base["x"], base["y"],
                                   nb["name"], nb["direction"], nb["x"], nb["y"]))
    return "checked %d connection(s) off %s|%d@(%s,%s): %d disagree" % (
        len(recs) - 1, base["name"], base["direction"], base["x"], base["y"], bad)


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(verify_against_engine(), indent=2, default=str))
