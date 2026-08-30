#!/usr/bin/env python3
"""Obstacle-aware belt/pipe ROUTER: A* + a functional obstacle model.

Replaces `bootstrap.lay_belt_path` as the PLANNER for belt runs. lay_belt_path expands
corner waypoints blind and its Lua `freebelt()` counts an existing transport-belt as FREE,
then `old.destroy()`s it — that is exactly how a crossing lane overwrote the coal row at
(-10,15) (live-verified 2026-08-30: that tile holds a belt at direction 8/SOUTH inside a row
whose neighbours run direction 4/EAST) and killed the boiler feed. It also has no obstacle
model at all, so lanes cross lanes and machines. Nothing here destroys anything, ever.

Ported from arturh85/factorio-bot `crates/core/src/factorio/util.rs::build_entity_path`
(MIT) — the (last_last, last, current) A* whose 3-tuple state is what makes the underground
rules expressible: no reversing, straight one tile before AND after an underground hop, no
chained undergrounds, hop cost = 3*length. Three deliberate divergences from upstream:

  fix #1 (a real off-by-one). Upstream keys its underground rules on
    `last_pos.distance(last_last_pos)` — the length of the move BEFORE the most recent one —
    so "after an underground go straight" fires one move late and the move LEAVING an
    underground exit may itself be a long hop. Its result assembly then renames that
    just-emitted exit into an entrance (util.rs:451), producing entrance->entrance and a
    silently dead lane. We key on the MOST RECENT move instead.
  fix #2. `Pos::distance` is manhattan (types.rs:216), not chebyshev. Orthogonal moves make
    the two coincide along an axis; the heuristic is manhattan/3. Named correctly here.
  fix #3 (perf). h = manhattan/3 is a 3x underestimate (min real cost is 1/tile), so A*
    degenerates toward Dijkstra over a 3-tuple state space. Default h = manhattan (still
    admissible AND consistent: a length-L move costs >= L and drops manhattan by <= L);
    heuristic_div=3 reproduces upstream exactly. MAX_EXPANSIONS caps runaway searches.

The obstacle model is chebykinn/factorio-planning-agent's `TOOLS.get_occupancy`
(mod/planning-agent/control.lua:1237) minus its ASCII grid: bounding-box -> tile expansion,
and its key insight — FUNCTIONAL RESERVATIONS. An inserter's pickup/drop tile and a drill's
drop_position read FREE but jam the line if you build on them, and the owning entity usually
sits OUTSIDE the routed rectangle, so the reservation scan is padded. Two more rules come
from Konano/FactorioBeltRouter (transport_line_connector.lua:222-252): an underground pair
may not span OVER a same-name underground on a parallel axis (they interlock), and a tile an
existing belt POINTS INTO is reserved (side-feed contamination — the mixed-ore merges
GOTCHAS already burned on); underground inputs are exempt, outputs are not.

RCON is READ-ONLY here: `scan_obstacles` is find_entities_filtered + find_tiles_filtered.
`plan_to_lua` RETURNS command strings and executes nothing.
"""
import heapq
import json

import rcon

LAST_ERROR = ""          # why the most recent plan_route failed (bootstrap.LAST_LAY_GAPS precedent)
LAST_WEIGHT = 1          # heuristic weight the last route needed; >1 means "legal, not optimal"
MAX_EXPANSIONS = 200000  # autopilot.belt_route caps at 60000; this state space is 3x wider
WEIGHTS = (1, 2, 4)      # heuristic weights tried in order; >1 is fast but not cost-optimal
SELF_CROSS_RETRIES = 4   # re-searches allowed after banning a tile the route doubled up on
READ_CHUNK = 3000        # chars per chunked storage read (architect.py/world.py pattern)
CMD_LIMIT = 3500         # bytes per /sc command (fle_tools.CHUNK_LIMIT)

# Factorio 2.0/2.1 are 16-direction: N=0 E=4 S=8 W=12. Upstream factorio-bot is 8-point
# (types.rs:246) — its numeric directions are NEVER reused here.
DIRS = {(0, -1): 0, (1, 0): 4, (0, 1): 8, (-1, 0): 12}
VEC = {d: v for v, d in DIRS.items()}
ORTHO = (0, 4, 8, 12)

# 'max' = the entrance<->exit POSITION DELTA. Live-probed 2026-08-30 on 2.1.17:
# prototypes.entity[n].max_underground_distance = 5 (underground-belt), 7 (fast), 9 (express),
# 11 (turbo), 10 (pipe-to-ground) — all five re-probed 2026-08-30. That matches what both live
# layers already allow (fle_lib.lua:206 `(j-pi) <= range+1` with range=4, bootstrap.py:694
# `(j-(i-1))<=5`); fle_lib's `underground_range` table holds the TOOLTIP number (tiles
# covered), one less than this. scan_obstacles re-reads the live values anyway.
MAX_UNDER = {"underground-belt": 5, "fast-underground-belt": 7,
             "express-underground-belt": 9, "turbo-underground-belt": 11,
             "pipe-to-ground": 10}
# every tier must be listed: UNDER_FOR.get falls back to spec['under'] ('underground-belt'),
# so a missing tier would silently plan YELLOW undergrounds — at max 5 — into a faster lane.
UNDER_FOR = {"transport-belt": "underground-belt",
             "fast-transport-belt": "fast-underground-belt",
             "express-transport-belt": "express-underground-belt",
             "turbo-transport-belt": "turbo-underground-belt",
             "pipe": "pipe-to-ground"}
# mirror: pipe-to-ground's ENTRANCE faces BACK along travel (fle_lib.lua:208); belts carry
# the travel direction on both halves and are distinguished by type='input'/'output'.
# typed: whether create_entity takes type='input'/'output' (belts yes, pipes no).
KINDS = {"belt": {"surface": "transport-belt", "under": "underground-belt",
                  "max": 5, "mirror": False, "typed": True},
         "pipe": {"surface": "pipe", "under": "pipe-to-ground",
                  "max": 10, "mirror": True, "typed": False}}


# --------------------------------------------------------------------------- geometry
def opposite(d):
    return (d + 8) % 16


def rel_dir(a, b):
    """Direction from tile a to tile b (orthogonal moves only). North for a==b, matching
    upstream relative_direction (util.rs:493)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    return DIRS[((dx > 0) - (dx < 0), (dy > 0) - (dy < 0))]


def move(pos, d, length):
    vx, vy = VEC[d]
    return (pos[0] + vx * length, pos[1] + vy * length)


def manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def chebyshev(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _between(a, b):
    """Tiles STRICTLY between two collinear orthogonal tiles — what an underground spans."""
    d = rel_dir(a, b)
    return [move(a, d, k) for k in range(1, chebyshev(a, b))]


# --------------------------------------------------------------------------- obstacles
class Obstacles:
    """Pure data — constructible in tests with no RCON.

    hard      tiles nothing may be BUILT on (buildings, cliffs, water, ghosts). Spannable.
    reserved  tiles that read free but jam the line if built on (inserter pickup/drop,
              drill drop_position, the tile an existing belt points into). Spannable.
    belts     tile -> {'name','dir','type'} for existing belts/pipes. 'type' is
              'surface' | 'input' | 'output'. Hard unless collinear + same direction.
    bounds    (minx,miny,maxx,maxy) inclusive tile box the router may plan inside — the
              region actually scanned, so A* can never wander into unknown terrain.
    """

    def __init__(self, hard=None, reserved=None, belts=None, bounds=None, under_max=None):
        self.hard = set(hard or ())
        self.reserved = set(reserved or ())
        self.belts = dict(belts or {})
        self.bounds = bounds
        self.under_max = dict(under_max or {})   # live prototype max_underground_distance

    @classmethod
    def from_scan(cls, payload):
        """Parse the scan_obstacles JSON payload."""
        def pt(s):
            x, y = s.split(",")
            return (int(x), int(y))
        belts = {}
        for b in payload.get("belts", ()):
            belts[(int(b["x"]), int(b["y"]))] = {"name": b["n"], "dir": int(b.get("d", 0)),
                                                 "type": b.get("t", "surface")}
        bb = payload.get("b")
        hard = {pt(s) for s in payload.get("h", ())}
        # planning-agent's law (control.lua:1275): a reservation only ever claims a FREE tile,
        # it never masks a real building — or an existing belt. Enforced here as well as in the
        # scan, because a reserved tile that also holds a belt would make that belt
        # un-adoptable, and inside any continuous lane EVERY tile is its predecessor's
        # feed target (live: it made the whole coal row untraversable, 1 state expanded).
        res = {pt(s) for s in payload.get("r", ())} - hard - set(belts)
        return cls(hard=hard,
                   reserved=res,
                   belts=belts,
                   bounds=(int(bb[0]), int(bb[1]), int(bb[2]), int(bb[3])) if bb else None,
                   under_max={k: int(v) for k, v in (payload.get("ug") or {}).items()})


# --------------------------------------------------------------------------- A*
def plan_route(start, goal, kind="belt", obstacles=None, reserved=None, max_underground=None,
               goal_dir=None, bounds=None, adopt=True, name=None, turn_cost=0.0,
               heuristic_div=1):
    """A* a belt/pipe lane from tile `start` to tile `goal`. Returns a list of steps, or None
    (with LAST_ERROR set) when no legal route exists.

    step = {'x','y','dir','entity'} plus 'type' ('input'/'output') and 'span' (the covered
    tiles) on underground halves, and 'adopt': True for an existing belt we reuse — an
    adopted step emits NO command, which is what makes reuse safe instead of destructive.

    ADOPTION AS AN A* CONSTRAINT (the piece that makes "existing belts are hard unless
    collinear + same direction" work): a tile's direction is fixed by the move LEAVING it, so
    when expanding a move of direction d and length L out of tile P, an existing belt at P is
    traversable only if it is our surface name, faces d, is a surface piece, and L == 1 (an
    adopted tile can never become an underground entrance). Pipes adopt regardless of
    direction (fle_lib.lua:164). Everything else holding a belt is hard.

    `hard` and `reserved` tiles are non-placeable but SPAN-PASSABLE — tunnelling under a
    building, or under an inserter's drop tile, is exactly the right answer. start/goal are
    exempt from `reserved` (the caller chose those tiles deliberately: connecting to a lane
    normally means starting on the tile something else feeds).
    """
    global LAST_ERROR, LAST_WEIGHT
    LAST_ERROR, LAST_WEIGHT = "", 1
    start, goal = (int(start[0]), int(start[1])), (int(goal[0]), int(goal[1]))
    spec = KINDS.get(kind)
    if spec is None:
        LAST_ERROR = "unknown kind %r" % (kind,)
        return None
    sname = name or spec["surface"]
    uname = UNDER_FOR.get(sname, spec["under"])
    is_pipe = (kind == "pipe")
    obs = obstacles or Obstacles()
    umax = max_underground or obs.under_max.get(uname) or MAX_UNDER.get(uname) or spec["max"]
    umax = max(1, int(umax))
    box = bounds or obs.bounds
    hard = obs.hard
    belts = obs.belts if adopt else {}
    hard_belts = set() if adopt else set(obs.belts)
    res = set(obs.reserved) | set(reserved or ())
    res.discard(start)
    res.discard(goal)

    def inside(p):
        return box is None or (box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3])

    def enterable(p):
        # a belt tile IS enterable; whether it is USABLE is decided by its outgoing move
        return inside(p) and p not in hard and p not in res and p not in hard_belts

    def can_build(p, d, role):
        """Can we place OUR entity on tile p, facing d, in this role ('surface'|'in'|'out')?"""
        if not inside(p) or p in hard or p in res or p in hard_belts:
            return False
        b = belts.get(p)
        if b is None:
            return True
        if role != "surface" or b.get("type") != "surface" or b.get("name") != sname:
            return False
        return True if is_pipe else b.get("dir") == d      # pipes join regardless of direction

    def span_ok(p, t, d):
        """Konano rule: an underground pair may not span OVER a same-name underground on a
        parallel axis — the two interlock and both break."""
        for m in _between(p, t):
            if not inside(m):
                return False
            b = obs.belts.get(m)
            if b and b.get("name") == uname and (b.get("dir", 0) - d) % 8 == 0:
                return False
        return True

    def move_ok(p, d, length, t):
        if length == 1:
            return can_build(p, d, "surface") and enterable(t)
        return (can_build(p, d, "in") and span_ok(p, t, d) and can_build(t, d, "out"))

    if not inside(start) or start in hard or start in hard_belts:
        LAST_ERROR = "start %s is blocked" % (start,)
        return None
    if not inside(goal) or goal in hard or goal in hard_belts:
        LAST_ERROR = "goal %s is blocked" % (goal,)
        return None
    if start == goal:
        d = goal_dir if goal_dir is not None else 0
        if not can_build(start, d, "surface"):
            LAST_ERROR = "goal %s is not placeable" % (goal,)
            return None
        adopted = belts.get(start) is not None
        return [{"x": start[0], "y": start[1], "dir": d, "entity": sname, "adopt": adopted}]

    def goal_reached(state):
        _, last, cur = state
        if cur != goal:
            return False
        if chebyshev(last, cur) > 1:
            # arrived as an underground EXIT (already validated by move_ok). Its direction IS
            # the travel direction and cannot be turned, so a caller-specified goal_dir has to
            # match: otherwise assembly would silently emit `travel` and ignore goal_dir.
            return goal_dir is None or rel_dir(last, cur) == goal_dir
        d = goal_dir if goal_dir is not None else rel_dir(last, cur)
        return can_build(goal, d, "surface")

    # ------------------------------------------------------------------ search
    def search(w):
        """A* over (last_last, last, current). w inflates the heuristic: w=1 is optimal,
        w>1 trades optimality for expansions (the route stays fully LEGAL either way — every
        constraint lives in move_ok, never in the heuristic). Returns (goal_state, came,
        expanded); goal_state is None on exhaustion."""
        s0 = (start, start, start)
        gscore = {s0: 0}
        came = {}
        tie = 0
        heap = [(manhattan(start, goal) // heuristic_div, tie, s0)]
        expanded = 0
        seen = set()
        while heap:
            _f, _t, state = heapq.heappop(heap)
            if state in seen:
                continue
            seen.add(state)
            if goal_reached(state):
                return state, came, expanded
            expanded += 1
            if expanded > MAX_EXPANSIONS:
                return None, came, expanded
            _ll, last, cur = state
            g = gscore[state]
            cur_len = chebyshev(last, cur)                # fix #1: the MOST RECENT move
            cur_dir = rel_dir(last, cur) if cur != last else None
            for d in ORTHO:
                # we cannot move in the opposite direction after we have moved (util.rs:379)
                if cur != last and d == opposite(cur_dir):
                    continue
                # after an underground we need to go straight (util.rs:383, keyed per fix #1)
                if cur_len > 1 and d != cur_dir:
                    continue
                for length in range(1, umax + 1):
                    # after an underground we cannot immediately underground again (util.rs:388)
                    if cur_len > 1 and length > 1:
                        break
                    t = move(cur, d, length)
                    if move_ok(cur, d, length, t):
                        ns = (last, cur, t)
                        step = 1 if length == 1 else length * 3      # util.rs:394
                        if turn_cost and cur_dir is not None and d != cur_dir:
                            step += turn_cost
                        ng = g + step
                        if ng < gscore.get(ns, float("inf")):
                            gscore[ns] = ng
                            came[ns] = state
                            tie += 1
                            heapq.heappush(
                                heap,
                                (ng + w * (manhattan(t, goal) // heuristic_div), tie, ns))
                    # before an underground we need a straight connection (util.rs:399) — this
                    # is what forbids starting an underground on a TURN tile
                    if cur != last and cur_len < 2 and d != cur_dir:
                        break
        return None, came, expanded

    # ------------------------------------------------------------------ assembly
    # Shape ported from util.rs:419-467: walk the states, emit a surface piece per tile, and
    # where the move that ARRIVED here was long, convert the previously emitted piece into the
    # underground ENTRANCE and this one into the EXIT. The direction/type math is ours: on 2.1
    # both halves carry the TRAVEL direction and are distinguished by type='input'/'output'
    # (fle_lib.lua:188, bootstrap.py:696); upstream writes direction.opposite() on the exit and
    # never sets a type, which is 1.1-era and wrong here. pipe-to-ground mirrors instead.
    def assemble(path):
        out = []
        for i, (_ll, last, pos) in enumerate(path):
            nxt = path[i + 1][2] if i + 1 < len(path) else None
            d = rel_dir(pos, nxt) if nxt is not None else (
                goal_dir if goal_dir is not None else rel_dir(last, pos))
            dist = chebyshev(pos, last) if last != pos else 1
            if dist == 1:
                b = belts.get(pos)
                adopted = bool(b and b.get("type") == "surface" and b.get("name") == sname
                               and (is_pipe or b.get("dir") == d))
                out.append({"x": pos[0], "y": pos[1], "dir": d, "entity": sname,
                            "adopt": adopted})
            else:
                travel = rel_dir(last, pos)
                ent = out[i - 1]
                ent["entity"] = uname
                ent["type"] = "input"
                ent["dir"] = opposite(travel) if spec["mirror"] else travel
                ent["span"] = _between(last, pos)
                ent["adopt"] = False
                out.append({"x": pos[0], "y": pos[1], "dir": travel, "entity": uname,
                            "type": "output", "span": _between(last, pos), "adopt": False})
        return out

    def self_conflicts(plan):
        """Tiles this plan uses TWICE, or where it would break its own underground pair.

        The A* state is only (last_last, last, cur) — it has no memory of the tiles already
        used — so in a dense field the route can CROSS ITSELF and put two entities on one
        tile. Reproducible at weight 1, and silent in game: the second create_entity just
        fails can_place_entity and is skipped, leaving either a tile pointing the wrong way
        or an underground exit whose entrance was never built.

        Second rule, the own-plan twin of span_ok: an underground half sitting inside one of
        our OWN spans on a parallel axis re-pairs that span and kills the hop."""
        bad, seen = set(), {}
        for s in plan:
            t = (s["x"], s["y"])
            if t in seen:
                bad.add(t)
            seen[t] = s
        spans = [(set(map(tuple, s["span"])), s["dir"]) for s in plan if s.get("type") == "input"]
        for s in plan:
            if s.get("type") not in ("input", "output"):
                continue
            t = (s["x"], s["y"])
            for tiles, sd in spans:
                if t in tiles and (s["dir"] - sd) % 8 == 0:
                    bad.add(t)
                    break
        return bad

    # A long dense run (a 200-tile cross-base lane through 30 foreign lanes) costs far more
    # than manhattan, so the optimal search explores a huge band and hits the cap. Falling back
    # to a weighted pass matters: returning None there would push the caller back onto
    # lay_belt_path, which is the destructive thing this module exists to replace. The plan is
    # still fully legal — only its cost is no longer provably minimal.
    for _attempt in range(SELF_CROSS_RETRIES + 1):
        found = came = None
        expanded = 0
        for w in WEIGHTS:
            LAST_WEIGHT = w
            found, came, expanded = search(w)
            if found is not None:
                break
        if found is None:
            LAST_ERROR = ("no route from %s to %s (%d states expanded at weight %s)"
                          % (start, goal, expanded, WEIGHTS[-1]))
            return None

        path = []
        node = found
        while node is not None:
            path.append(node)
            node = came.get(node)
        path.reverse()

        out = assemble(path)
        clash = self_conflicts(out)
        if not clash:
            return out
        # ban what it doubled up on and search again. Never emit a self-overlapping plan:
        # half of it would be silently skipped and the lane would run the wrong way.
        ban = clash - {start, goal}
        if not ban:
            break
        res |= ban                            # `res` is what can_build/enterable close over
    LAST_ERROR = "route from %s to %s only exists by crossing itself" % (start, goal)
    return None


# --------------------------------------------------------------------------- plan queries
def plan_tiles(plan, include_spans=False):
    """Tiles the plan puts an ENTITY on (executor.path_tiles replacement). Span tiles carry no
    entity — nothing is built there — so they are excluded unless asked for."""
    tiles = [(s["x"], s["y"]) for s in plan or ()]
    if include_spans:
        seen = set(tiles)
        for s in plan or ():
            for t in s.get("span", ()):
                t = tuple(t)
                if t not in seen:
                    seen.add(t)
                    tiles.append(t)
    return tiles


def route_cost(plan):
    """The A* cost of a plan: 1 per straight tile, 3*length per underground hop."""
    total = 0
    for i in range(len(plan or ()) - 1):
        a = (plan[i]["x"], plan[i]["y"])
        b = (plan[i + 1]["x"], plan[i + 1]["y"])
        delta = chebyshev(a, b)
        total += 1 if delta == 1 else delta * 3
    return total


def plan_conflicts(plan, protected_tiles):
    """Which of a plan's tiles sit on OPERATOR-PROTECTED ground, so a caller can reject the
    route BEFORE placing anything. `operator_owned` at >=25% reproduces BUILD LAW 3
    (bootstrap.route_is_operator_owned) but measured on the tiles actually routed instead of a
    straight-line expansion of the waypoints."""
    prot = set(tuple(t) for t in (protected_tiles or ()))
    tiles = plan_tiles(plan)
    hits = [t for t in tiles if t in prot]
    frac = (len(hits) / len(tiles)) if tiles else 0.0
    return {"tiles": hits, "count": len(hits), "fraction": frac,
            "operator_owned": frac >= 0.25}


# --------------------------------------------------------------------------- lua emission
def plan_to_lua(plan, consume=True):
    """The /sc commands that WOULD build this plan. Returns strings; executes NOTHING.

    Mirrors fle_lib.F.lay_line's put() (lua/fle_lib.lua:170): clear trees/rocks on the tile,
    consume the item from storage.derpface's inventory (GOTCHAS "Vendored FLE placements must
    consume inventory"), can_place_entity guard, create_entity with type='input'/'output' for
    undergrounds. It emits NO destroy of any belt or building — that omission IS the fix for
    the overwritten coal row: where lay_belt_path destroyed whatever stood in the way, this
    simply cannot place and reports a skip.
    """
    steps = [s for s in (plan or ()) if not s.get("adopt")]
    if not steps:
        return []
    names = sorted({s["entity"] for s in steps})
    nidx = {n: i for i, n in enumerate(names)}
    utype = {None: 0, "input": 1, "output": 2}
    # pipe-to-ground takes NO type= (fle_lib.lua:209): the halves are told apart by direction,
    # the entrance facing back along travel. Only belt undergrounds are typed.
    untyped = {k["under"] for k in KINDS.values() if not k["typed"]}
    entries = ["%d,%d,%d,%d,%d" % (s["x"], s["y"], s["dir"], nidx[s["entity"]],
                                   0 if s["entity"] in untyped else utype.get(s.get("type"), 0))
               for s in steps]
    head = (
        "/sc local s=game.surfaces[1]; local f=game.forces.player;"
        "local inv=storage.derpface and storage.derpface.valid and storage.derpface.get_main_inventory();"
        "local NM={" + ",".join("'%s'" % n for n in names) + "}; local built,skip=0,0;"
        "for a,b,c,k,u in ([==[")
    tail = (
        "]==]):gmatch('(-?%d+),(-?%d+),(%d+),(%d+),(%d+)') do"
        " local x,y,d=tonumber(a),tonumber(b),tonumber(c);"
        " local nm=NM[tonumber(k)+1]; local ut=(u=='1' and 'input') or (u=='2' and 'output') or nil;"
        " for _,e in pairs(s.find_entities_filtered{area={{x+0.05,y+0.05},{x+0.95,y+0.95}},"
        "   type={'tree','simple-entity'}}) do if e.destroy then e.destroy() end end;"
        # can_place_entity is the ONLY gate: an occupied tile is SKIPPED, never destroyed.
        " if s.can_place_entity{name=nm,position={x+0.5,y+0.5},force=f} then"
        # `inv` is nil (no derpface) OR false (derpface invalid) OR the inventory — so the
        # guard must be truthiness, exactly F.take's (fle_lib.lua:52). `inv~=nil` was true for
        # the `false` case and indexed a boolean, aborting the whole /sc.
        + ("  local took=(inv and inv.get_item_count(nm)>0);"
           "  if took then inv.remove{name=nm,count=1} end;" if consume else
           "  local took=true;") +
        "  if took then local e=s.create_entity{name=nm,position={x+0.5,y+0.5},direction=d,"
        "     force=f,type=ut};"
        + ("   if e then built=built+1 else if inv then inv.insert{name=nm,count=1} end; skip=skip+1 end;"
           if consume else "   if e then built=built+1 else skip=skip+1 end;") +
        "  else skip=skip+1 end"
        " else skip=skip+1 end end;"
        "rcon.print(built..'/'..(built+skip))")
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


# --------------------------------------------------------------------------- live scan
def _chunked(build_lua):
    """rcon.read_chunked on a PRIVATE buffer key. `build_lua(store)` returns the /sc.

    The key was the fixed storage._broute; a build plus N slice reads is N+1 round-trips, and a
    key shared by two concurrent readers splices two documents together (GOTCHAS "RCON client
    protocol")."""
    return rcon.read_chunked(build_lua, chunk=READ_CHUNK)


def scan_lua(x1, y1, x2, y2, pad=6, res_pad=5, ghosts_hard=True, store="storage._broute"):
    """Build the READ-ONLY scan command (pure — unit-testable, and reviewable before it runs).
    The rectangle is padded because a building's footprint, and above all an inserter that owns
    a reservation INSIDE the route, usually sits outside it."""
    x1, y1, x2, y2 = int(x1) - pad, int(y1) - pad, int(x2) + pad, int(y2) + pad
    ghost = "['entity-ghost']=1," if not ghosts_hard else ""
    return (
        "/sc local s=game.surfaces[1]; local X1,Y1,X2,Y2=%d,%d,%d,%d; local P=%d;"
        % (x1, y1, x2, y2, res_pad) +
        "local H,R,B,BK={},{},{},{}; local function K(x,y) return x..','..y end;"
        # Name-based water set (fle_lib.lua:40). planning-agent uses tile.collides_with('player')
        # but that rides on 2.0's renamed collision layers; cliffs come through as entities.
        "for _,t in pairs(s.find_tiles_filtered{area={{X1,Y1},{X2+1,Y2+1}},name={'water',"
        "'deepwater','water-green','deepwater-green','water-shallow','water-mud'}}) do"
        " H[K(t.position.x,t.position.y)]=true end;"
        "local BL={['transport-belt']=1,['underground-belt']=1,['splitter']=1,['pipe']=1,"
        "['pipe-to-ground']=1};"
        "local SK={character=1,resource=1,['item-entity']=1,corpse=1,beam=1,tree=1,"
        "['simple-entity']=1," + ghost + "['item-request-proxy']=1};"
        "for _,e in pairs(s.find_entities_filtered{area={{X1,Y1},{X2+1,Y2+1}}}) do"
        " local ty=e.type;"
        " if not SK[ty] then"
        # bounding-box -> tile expansion (control.lua:1258): correct for 2x2 drills, 2-wide
        # splitters, 3x3 electric drills. Every attribute read is pcall'd (world.py:181).
        "  local bb=e.bounding_box;"
        "  local x0,y0=math.floor(bb.left_top.x),math.floor(bb.left_top.y);"
        "  local x9,y9=math.ceil(bb.right_bottom.x)-1,math.ceil(bb.right_bottom.y)-1;"
        "  if BL[ty] then"
        "   local okd,d=pcall(function() return e.direction end);"
        "   local bt='surface';"
        "   if ty=='underground-belt' or ty=='pipe-to-ground' then"
        "    local okg,g=pcall(function() return e.belt_to_ground_type end);"
        "    bt=(okg and g) or 'input' end;"
        "   for tx=x0,x9 do for tyy=y0,y9 do BK[K(tx,tyy)]=true;"
        "    B[#B+1]={x=tx,y=tyy,d=(okd and tonumber(d)) or 0,n=e.name,t=bt} end end"
        "  else for tx=x0,x9 do for tyy=y0,y9 do H[K(tx,tyy)]=true end end end"
        " end end;"
        # FUNCTIONAL RESERVATIONS (control.lua:1270) — tiles that read free but jam the line.
        # reserve() only ever overwrites a FREE tile: a reservation never masks a real building.
        # reserve() only ever claims a FREE tile (control.lua:1275): never a building (H) and
        # never a belt (BK) — inside a continuous lane every tile is its predecessor's feed
        # target, so reserving those would make the whole lane un-adoptable.
        "local function RS(p) local k=K(math.floor(p.x),math.floor(p.y));"
        " if not H[k] and not BK[k] then R[k]=true end end;"
        "for _,e in pairs(s.find_entities_filtered{area={{X1-P,Y1-P},{X2+1+P,Y2+1+P}},"
        " type={'inserter','mining-drill','entity-ghost'}}) do"
        " local okk,kind=pcall(function() return (e.type=='entity-ghost') and e.ghost_type or e.type end);"
        " if okk then"
        "  if kind=='inserter' then pcall(function() RS(e.pickup_position); RS(e.drop_position) end)"
        "  elseif kind=='mining-drill' then pcall(function() RS(e.drop_position) end) end"
        " end end;"
        # Konano rule: a tile an existing belt POINTS INTO is functionally reserved (side-feed
        # contamination). Underground INPUTs are exempt (their target is their own exit).
        "local DV={[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}};"
        "for _,b in pairs(B) do"
        " if b.n~='pipe' and b.n~='pipe-to-ground' and b.t~='input' then"
        "  local v=DV[b.d]; if v then RS({x=b.x+v[1],y=b.y+v[2]}) end end end;"
        "local hl,rl={},{}; for k in pairs(H) do hl[#hl+1]=k end;"
        "for k in pairs(R) do rl[#rl+1]=k end;"
        "local UG={}; for _,n in pairs({'underground-belt','fast-underground-belt',"
        "'express-underground-belt','turbo-underground-belt','pipe-to-ground'}) do"
        " local p=prototypes.entity[n];"
        " if p then UG[n]=p.max_underground_distance end end;"
        + "%s=helpers.table_to_json{b={X1,Y1,X2,Y2},h=hl,r=rl,belts=B,ug=UG};"
          "rcon.print(#%s)" % (store, store))


def scan_obstacles(x1, y1, x2, y2, pad=6, res_pad=5, ghosts_hard=True):
    """READ-ONLY RCON scan of a tile rectangle -> Obstacles. One /sc + chunked read.

    `ghosts_hard=True` (default) treats entity-ghosts as buildings — someone planned a build
    there. A deliberate divergence from fle_lib.F.classify (lua/fle_lib.lua:76), which ignores
    them; it is the safer default now that ghosts are how megabase modules get planned.
    """
    def build(store):
        cmd = scan_lua(x1, y1, x2, y2, pad=pad, res_pad=res_pad, ghosts_hard=ghosts_hard,
                       store=store)
        if len(cmd) > CMD_LIMIT:
            raise ValueError("scan command is %d bytes (>%d)" % (len(cmd), CMD_LIMIT))
        return cmd

    return Obstacles.from_scan(json.loads(_chunked(build)))


# --------------------------------------------------------------------------- cli
def _fmt(plan):
    return "\n".join(
        "  %4d,%-4d d%-2d %-20s%s%s" % (s["x"], s["y"], s["dir"], s["entity"],
                                        " " + s["type"] if s.get("type") else "",
                                        " ADOPT" if s.get("adopt") else "")
        for s in plan)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 6 and sys.argv[1] == "route":
        # live: scan the corridor read-only, plan, print the plan + the commands it WOULD run
        x1, y1, x2, y2 = (int(v) for v in sys.argv[2:6])
        kind = sys.argv[6] if len(sys.argv) > 6 else "belt"
        obs = scan_obstacles(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        print("scanned: %d hard, %d reserved, %d belt tiles, bounds=%s, ug=%s"
              % (len(obs.hard), len(obs.reserved), len(obs.belts), obs.bounds, obs.under_max))
        plan = plan_route((x1, y1), (x2, y2), kind=kind, obstacles=obs)
        if plan is None:
            print("NO ROUTE: %s" % LAST_ERROR)
            raise SystemExit(1)
        print("%d steps, cost %d\n%s" % (len(plan), route_cost(plan), _fmt(plan)))
        print("\n-- %d command(s) that WOULD build it (NOT executed):" % len(plan_to_lua(plan)))
        for c in plan_to_lua(plan):
            print("%d bytes" % len(c))
    else:
        print(__doc__.strip().splitlines()[0])
        print("usage: belt_router.py route <x1> <y1> <x2> <y2> [belt|pipe]   (read-only)")
