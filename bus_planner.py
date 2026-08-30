#!/usr/bin/env python3
"""SITE A MAIN BUS — legality first, reachability second, cost last.

WHY THIS EXISTS. On 2026-08-30 a bus was sited by eyeballing the widest clear run per row.
It scored well on the only thing that was measured — "how many tiles in a row are empty" —
and it was wrong in both ways that matter:

  * it ran straight through the operator's reserved 36-lab array, because `can_place_entity`
    returns TRUE over a ghost, so "empty" and "unclaimed" were never distinguished; and
  * the iron smelter row could not reach it, because the head was placed SOUTH of the copper
    row and its output lane, so the iron feed would have had to cross another ore's
    infrastructure to arrive.

Both are decided before any belt is placed, and neither is expensive to check. A width scan
cannot see either one. This module makes the three questions explicit and separate:

    1. LEGAL       - may we occupy these tiles at all?      (hard; a violation is fatal)
    2. REACHABLE   - can every source get to it, and can it get to the sinks?   (hard)
    3. GOOD        - of the sites that pass 1 and 2, which is cheapest?         (score)

Everything here is pure over a `World` snapshot; `scan()` is the only RCON call and it is
read-only. That means the whole siting decision is testable offline against synthetic worlds,
which is how the regression for the 2026-08-30 corridor is written.
"""
import collections

# A bus is a run of parallel lanes plus clear margin either side for taps and future lanes.
DEFAULT_LANES = 4
DEFAULT_MARGIN = 2

# Scoring weights. Feed distance dominates because every tile of it is belt that has to be
# built, powered past, and maintained; margin is a tie-breaker that buys future lanes.
W_FEED = 1.0          # per tile, source -> bus head
W_SINK = 0.5          # per tile, bus tail -> sink
W_MARGIN = -8.0       # per clear margin lane (negative = a bonus)
W_ARRAY = -0.25       # per clear tile of adjacent buildable area for the assembler array


class SiteError(RuntimeError):
    """No legal, reachable site exists. Carries the reasons so the caller can say WHY."""


class World:
    """What siting needs to know. Tiles are (x, y) integers.

    `reserved` is the one that keeps being forgotten: ghost tiles are CLAIMED ground even
    though nothing physical stands there.
    """

    def __init__(self, occupied=(), reserved=(), ore=(), protected=(),
                 sources=None, sinks=None, bounds=None):
        self.occupied = set(occupied)
        self.reserved = set(reserved)
        self.ore = set(ore)
        self.protected = set(protected)
        self.sources = dict(sources or {})     # item -> [(x, y), ...] output tiles
        self.sinks = dict(sinks or {})         # name -> [(x, y), ...] consumer tiles
        self.bounds = bounds or (-64, -64, 64, 64)

    def blocked(self, t):
        """A tile a belt may NOT occupy. Ore is included: the operator's first standing rule
        is that nothing but drills and their support goes on an ore patch."""
        return (t in self.occupied or t in self.reserved or t in self.ore
                or t in self.protected)

    def inside(self, t):
        x1, y1, x2, y2 = self.bounds
        return x1 <= t[0] <= x2 and y1 <= t[1] <= y2

    def free(self, t):
        return self.inside(t) and not self.blocked(t)


class Corridor:
    """`lanes` parallel belt runs along `axis`, starting at `pos` on the cross-axis.

    axis 'v': lanes are columns x = pos .. pos+lanes-1, running y = a .. b (north -> south).
    axis 'h': lanes are rows    y = pos .. pos+lanes-1, running x = a .. b (west  -> east).
    """

    def __init__(self, axis, pos, a, b, lanes=DEFAULT_LANES, margin=DEFAULT_MARGIN):
        if axis not in ("v", "h"):
            raise ValueError("axis must be 'v' or 'h', got %r" % (axis,))
        if b < a:
            raise ValueError("corridor runs backwards: %d..%d" % (a, b))
        self.axis, self.pos, self.a, self.b = axis, pos, a, b
        self.lanes, self.margin = int(lanes), int(margin)

    def __repr__(self):
        return ("Corridor(%s pos=%d %d..%d lanes=%d)"
                % (self.axis, self.pos, self.a, self.b, self.lanes))

    def lane_positions(self):
        return range(self.pos, self.pos + self.lanes)

    def tiles(self):
        for k in self.lane_positions():
            for i in range(self.a, self.b + 1):
                yield (k, i) if self.axis == "v" else (i, k)

    def margin_tiles(self):
        """The clear lanes either side. Not required, but a bus with no margin cannot grow a
        lane or take a tap without being moved, and this one has to grow to circuits later."""
        lo = range(self.pos - self.margin, self.pos)
        hi = range(self.pos + self.lanes, self.pos + self.lanes + self.margin)
        for k in list(lo) + list(hi):
            for i in range(self.a, self.b + 1):
                yield (k, i) if self.axis == "v" else (i, k)

    def head(self):
        """Where feeds arrive: the upstream end of the first lane."""
        return (self.pos, self.a) if self.axis == "v" else (self.a, self.pos)

    def tail(self):
        return (self.pos, self.b) if self.axis == "v" else (self.b, self.pos)


# ----------------------------------------------------------------------------- reachability
def route_len(world, start, goal, extra_blocked=(), through_reserved=False,
              under_max=4, limit=40000):
    """Shortest 4-way path length over FREE tiles, or None when no route exists.

    This is the check the 2026-08-30 siting never did. "There is a clear corridor" and "the
    iron row can get to that corridor" are different claims, and only the second one makes the
    bus buildable. `extra_blocked` is how a feed is forbidden from routing through ANOTHER
    ore's infrastructure - crossing another source's lane is how two ores end up on one belt.
    """
    if start == goal:
        return 0
    extra = set(extra_blocked)

    def walkable(t):
        # RESERVED GROUND IS NOT A WALL, IT IS SOMEONE ELSE'S BUILD. We may not lay belt on
        # it, but a DELIVERY can still arrive: the blueprint that claims it carries items
        # internally. A lab array is the clearest case - its labs sit in the middle of their
        # own reservation, so a reachability search that treats reserved as solid seals the
        # consumer inside its own blueprint and reports every corridor on the map unreachable.
        # Feeds pass through_reserved=False (that belt has to be built); sinks pass True.
        if through_reserved and world.inside(t) and t in world.reserved:
            return t not in world.occupied and t not in world.ore
        return world.free(t)

    seen = {start}
    q = collections.deque([(start, 0)])
    pops = 0
    while q:
        (x, y), d = q.popleft()
        pops += 1
        if pops > limit:
            return None
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            # STEP, or TUNNEL. An existing belt is not a wall - it is crossed with an
            # underground pair, whose middle tiles stay free for whatever they pass under.
            # `belt_router` has always modelled this ("hard and reserved tiles are
            # non-placeable but SPAN-PASSABLE"); a reachability check that calls every belt
            # impassable disagrees with the router about what is buildable and reports a site
            # unreachable that the router would happily route. On the live base the copper
            # output belt at y=17 spans x=-10..29 and walls the iron row off from everything
            # south of it, which is not true in the game.
            for hop in range(1, under_max + 2):
                nxt = (x + dx * hop, y + dy * hop)
                if nxt == goal:
                    return d + hop
                if nxt in seen or nxt in extra:
                    continue
                if not walkable(nxt):
                    continue          # keep extending: this tile is what we tunnel UNDER
                seen.add(nxt)
                q.append((nxt, d + hop))
                if hop == 1:
                    continue          # a plain step never precludes trying longer hops
    return None


def _nearest(tiles, point):
    return min(tiles, key=lambda t: abs(t[0] - point[0]) + abs(t[1] - point[1]))


def _approach(world, sink_tile, max_r=30):
    """The nearest FREE tile to a consumer: where the bus actually delivers.

    A consumer usually sits inside its own blueprint - the lab array's labs are ringed by the
    array's own inserters and poles - so "route to the lab tile" is unanswerable and "route
    through the reservation" is worse: it lets a bus claim it reaches a sink by treating
    somebody else's build as a 40-tile highway. The bus delivers to the EDGE of the array and
    the array's blueprint carries it from there, so the reachability question is "can the tail
    get to the nearest free tile beside that consumer".
    """
    sx, sy = sink_tile
    for r in range(1, int(max_r) + 1):
        ring = []
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                t = (sx + dx, sy + dy)
                if world.free(t):
                    ring.append(t)
        if ring:
            return min(ring, key=lambda t: abs(t[0] - sx) + abs(t[1] - sy))
    return None


# --------------------------------------------------------------------------------- evaluate
def evaluate(world, corridor):
    """Judge one candidate. Returns a dict; `ok` is False with `reasons` when it is illegal
    or unreachable, and a `score` (lower is better) when it is neither.

    The order is deliberate: legality is checked before reachability so a site that claims
    someone else's ground is rejected on that ground and not on cost, and the reason says so.
    """
    out = {"corridor": corridor, "ok": False, "reasons": [], "score": None, "detail": {}}

    lane_tiles = list(corridor.tiles())
    reserved_hits = [t for t in lane_tiles if t in world.reserved]
    ore_hits = [t for t in lane_tiles if t in world.ore]
    occupied_hits = [t for t in lane_tiles if t in world.occupied]
    protected_hits = [t for t in lane_tiles if t in world.protected]
    outside = [t for t in lane_tiles if not world.inside(t)]

    if reserved_hits:
        out["reasons"].append(
            "crosses %d RESERVED tiles (a blueprint ghost claims that ground, e.g. %s)"
            % (len(reserved_hits), _fmt(reserved_hits)))
    if ore_hits:
        out["reasons"].append(
            "sits on %d ORE tiles (e.g. %s) - only drills and their support go on a patch"
            % (len(ore_hits), _fmt(ore_hits)))
    if occupied_hits:
        out["reasons"].append("collides with %d existing entities (e.g. %s)"
                              % (len(occupied_hits), _fmt(occupied_hits)))
    if protected_hits:
        out["reasons"].append("re-uses %d tiles the OPERATOR deleted (e.g. %s)"
                              % (len(protected_hits), _fmt(protected_hits)))
    if outside:
        out["reasons"].append("runs outside the scanned bounds (%d tiles)" % len(outside))
    if out["reasons"]:
        return out

    # REACHABILITY. Each source must reach the head without routing through another source's
    # output tiles; the tail must reach at least one sink.
    head, tail = corridor.head(), corridor.tail()
    feed = {}
    for item, tiles in sorted(world.sources.items()):
        if not tiles:
            continue
        others = {t for k, v in world.sources.items() if k != item for t in v}
        src = _nearest(tiles, head)
        d = route_len(world, src, head, extra_blocked=others)
        if d is None:
            out["reasons"].append(
                "%s cannot REACH the bus head at %s from %s without crossing another ore's "
                "infrastructure - a corridor the feed cannot get to is not a site"
                % (item, head, src))
        feed[item] = d
    if out["reasons"]:
        out["detail"]["feed"] = feed
        return out

    # SEARCH FROM THE TAIL TOWARD THE SINK, never the other way round. A consumer is very
    # often INSIDE a reservation - the lab array literally is one - so a search starting at
    # the sink is sealed in by its own blueprint and reports "unreachable" for every corridor
    # on the map. `route_len` does not require the GOAL to be free, only the tiles it walks,
    # which is the right model: the bus delivers to the array's edge and the array's own
    # blueprint carries it the rest of the way.
    sink_d = {}
    for name, tiles in sorted(world.sinks.items()):
        if not tiles:
            continue
        approach = _approach(world, _nearest(tiles, tail))
        if approach is None:
            continue
        d = route_len(world, tail, approach)
        if d is not None:
            sink_d[name] = d
    if world.sinks and not sink_d:
        out["reasons"].append("the bus tail at %s reaches no consumer" % (tail,))
        return out

    # SCORE. Only reached by sites that are legal and connectable.
    clear_margin = sum(1 for t in corridor.margin_tiles() if world.free(t))
    margin_lanes = clear_margin / max(1, (corridor.b - corridor.a + 1))
    array = _array_room(world, corridor)
    score = (W_FEED * sum(d for d in feed.values() if d is not None)
             + W_SINK * (min(sink_d.values()) if sink_d else 0)
             + W_MARGIN * margin_lanes
             + W_ARRAY * array)
    out.update(ok=True, score=round(score, 2))
    out["detail"] = {"feed": feed, "sink": sink_d,
                     "margin_lanes": round(margin_lanes, 2), "array_room": array}
    return out


def _array_room(world, corridor, depth=24):
    """Free tiles beside the corridor for the assembler array. A bus with nowhere to put
    consumers is a belt to nowhere."""
    n = 0
    for i in range(corridor.a, corridor.b + 1):
        for off in range(corridor.margin + 1, corridor.margin + depth):
            for k in (corridor.pos - off, corridor.pos + corridor.lanes + off - 1):
                t = (k, i) if corridor.axis == "v" else (i, k)
                if world.free(t):
                    n += 1
    return n


def _fmt(tiles, n=3):
    return " ".join("(%d,%d)" % t for t in sorted(tiles)[:n])


# --------------------------------------------------------------------------------- choose
def candidates(world, lanes=DEFAULT_LANES, margin=DEFAULT_MARGIN, step=1, length=None):
    """Enumerate plausible corridors on both axes, spanning sources to sinks.

    The span is derived from where the plates are MADE and where they are USED rather than
    from a fixed length: a bus that stops short of its consumers has to be extended later,
    which means moving whatever was built off its end.
    """
    x1, y1, x2, y2 = world.bounds
    src = [t for v in world.sources.values() for t in v]
    snk = [t for v in world.sinks.values() for t in v]
    if not src or not snk:
        raise SiteError("cannot site a bus without at least one source and one sink")

    out = []
    for axis in ("v", "h"):
        k = 1 if axis == "v" else 0                   # the along-axis coordinate
        src_lo, src_hi = min(t[k] for t in src), max(t[k] for t in src)
        snk_hi = max(t[k] for t in snk)
        positions = (range(x1, x2 - lanes + 1, step) if axis == "v"
                     else range(y1, y2 - lanes + 1, step))
        # THE HEAD MUST BE SOMEWHERE EVERY SOURCE CAN GET TO, and that is not automatically
        # the northernmost one. The live base stacks its smelter rows - iron at y=6, copper at
        # y=15 - so a run starting at the first source puts the head ABOVE the copper row, and
        # copper can only reach it by crossing the iron row. That is the second half of the
        # 2026-08-30 failure, and it is a property of where the run STARTS, not of its column.
        # So try starting before the first source AND after the last one, and let the
        # reachability check decide which of them a given base can actually feed.
        starts = {src_lo, src_hi + 2}
        for a in sorted(s for s in starts if s < snk_hi):
            b = a + int(length) - 1 if length else max(snk_hi, a + 1)
            for pos in positions:
                out.append(Corridor(axis, pos, a, b, lanes=lanes, margin=margin))
    return out


def choose(world, lanes=DEFAULT_LANES, margin=DEFAULT_MARGIN, step=1, length=None):
    """The best legal, reachable corridor — or SiteError naming why every option failed.

    Returns (corridor, verdict). The verdict carries the score breakdown so the choice can be
    explained rather than asserted, and the rejects are summarised by REASON so "there is no
    site" is always accompanied by what actually stood in the way.
    """
    cands = candidates(world, lanes=lanes, margin=margin, step=step, length=length)
    verdicts = [evaluate(world, c) for c in cands]
    good = [v for v in verdicts if v["ok"]]
    if not good:
        tally = collections.Counter()
        for v in verdicts:
            tally[v["reasons"][0].split(" (")[0] if v["reasons"] else "unknown"] += 1
        raise SiteError("no legal, reachable corridor among %d candidates. Why they failed: %s"
                        % (len(cands), "; ".join("%s x%d" % (k, n)
                                                 for k, n in tally.most_common(4))))
    good.sort(key=lambda v: v["score"])
    return good[0]["corridor"], good[0]


def explain(verdict):
    """One human-readable line for the log, so a siting decision is auditable after the fact."""
    c, d = verdict["corridor"], verdict["detail"]
    if not verdict["ok"]:
        return "REJECTED %r: %s" % (c, "; ".join(verdict["reasons"]))
    feed = ", ".join("%s %s tiles" % (k, v) for k, v in sorted((d.get("feed") or {}).items()))
    return ("CHOSE %r score=%.2f | feed: %s | nearest sink %s tiles | margin %.1f lanes | "
            "array room %d tiles"
            % (c, verdict["score"], feed or "none",
               min((d.get("sink") or {"-": 0}).values()),
               d.get("margin_lanes", 0), d.get("array_room", 0)))


# ------------------------------------------------------------------------------ world scan
def scan(x1, y1, x2, y2):
    """Read the world into a World. READ-ONLY.

    Ghosts are collected into `reserved` SEPARATELY from real entities, because the whole
    point is that they are claimed-but-empty and every "is it clear" check that predates this
    module missed them.
    """
    import autopilot as A
    raw = A._print(
        "/sc local s=game.surfaces[1] local o={} "
        "for _,e in pairs(s.find_entities_filtered{area={{%d,%d},{%d,%d}}}) do "
        "  local n=e.name "
        "  if n~='character' then "
        "    local kind='occ' "
        "    if e.type=='entity-ghost' then kind='ghost' elseif e.type=='resource' then kind='ore' end "
        "    local b=e.bounding_box "
        "    for tx=math.floor(b.left_top.x),math.ceil(b.right_bottom.x)-1 do "
        "      for ty=math.floor(b.left_top.y),math.ceil(b.right_bottom.y)-1 do "
        "        o[#o+1]=kind..','..tx..','..ty end end end end "
        "rcon.print(table.concat(o,';'))" % (x1, y1, x2, y2)).strip()
    occ, ghost, ore = set(), set(), set()
    for rec in raw.split(";"):
        parts = rec.split(",")
        if len(parts) != 3:
            continue
        kind, x, y = parts
        try:
            t = (int(x), int(y))
        except ValueError:
            continue
        {"occ": occ, "ghost": ghost, "ore": ore}[kind].add(t)
    return World(occupied=occ, reserved=ghost, ore=ore, bounds=(x1, y1, x2, y2))


if __name__ == "__main__":
    import json
    import sys
    w = scan(-40, -20, 40, 60)
    print("scanned: %d occupied, %d reserved(ghost), %d ore"
          % (len(w.occupied), len(w.reserved), len(w.ore)))
    print(json.dumps({"bounds": w.bounds}, indent=1))
    sys.exit(0)


def check_feed_source(x, y, item=None):
    """REFUSE to wire a bus feed to anything but a PLATE OUTPUT belt.

    Returns (ok, why). The caller must not place a single belt until this passes.

    On 2026-08-30 the bus was fed from the smelter rows' INPUT belts - twice, iron and copper -
    draining the furnaces' ore and fuel to the bus instead of carrying plates away. Lane 35 was
    measured carrying `coal:112`. Both times the "verification" was counting items on the belt,
    which proves a belt is moving and says nothing about what it is moving FOR. The inserters
    beside it say exactly that, and asking them costs one query.

    A bus feed must come from a belt that machines DROP onto (an output). Feeding from an input
    belt does not just fail to deliver plates - it starves the row it taps.
    """
    import autopilot as A
    r = A.belt_role(x, y)
    if r["role"] == "output":
        if item and r["carries"] and item not in r["carries"]:
            return False, ("(%d,%d) is an output belt but carries %s, not %s - wrong row"
                           % (x, y, r["carries"].strip() or "nothing", item))
        return True, "(%d,%d) is a plate OUTPUT belt (%s)" % (x, y, r["why"])
    if r["role"] == "input":
        return False, ("(%d,%d) is the row's INPUT belt (%s) - feeding the bus from it would "
                       "drain the furnaces' ore and fuel away instead of carrying plates. "
                       "Find the belt the machines DROP onto." % (x, y, r["why"]))
    if r["role"] == "both":
        return False, "(%d,%d) is mixed (%s) - do not wire to it blind" % (x, y, r["why"])
    return False, ("(%d,%d) has no inserter touching it (%s) - its role is unknown, and a bus "
                   "feed is never wired to a belt whose purpose was guessed" % (x, y, r["why"]))
