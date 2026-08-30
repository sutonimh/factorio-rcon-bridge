"""Connect a stranded product to something that consumes it, without asking anyone.

WHY THIS EXISTS
---------------
Red science was piling up on a belt while the labs sat at `missing_science_packs`, and what I
produced was a document offering the operator three options for where to inject it. That is
the wrong deliverable. "Where does the feed go" is not a matter of taste; it is a question
about the map, and the map can be measured. The bot should settle it.

THE THREE THINGS THAT HAVE TO BE DECIDED, AND HOW EACH IS DECIDED
-----------------------------------------------------------------
1. WHERE THE PRODUCT LEAVES  - a belt tile carrying the item whose downstream tile is empty
   ground. A terminus, not a junction. Never an INPUT belt: the bus was once wired to two
   smelter INPUT belts because "a belt near the smelter" was treated as good enough.

2. WHICH CONSUMER TO FEED    - consumers frequently chain (labs hand packs to each other by
   inserter, so a lab grid floods from one corner). Feed the HEAD: the consumer from which
   the most others are reachable along that chain. That is `head()`, and it is derived from
   inserter pickup/drop adjacency, not from a person knowing the layout.

3. WHERE THE INSERTER GOES   - a free tile orthogonally adjacent to the consumer whose
   opposite side is also free, so the inserter can pick off a belt and drop into the machine.
   Every such pair is a candidate; the cheapest to route to wins.

None of that needs a human. What DOES need a human is a change of intent - "should this base
make red science at all" - and this module does not touch that.
"""
import belt_router
import power_planner as PP        # noqa: F401  (kept for the pole/coverage vocabulary)

# Items a lab consumes. Anything else is matched against assembler recipe ingredients.
SCIENCE = ("automation-science-pack", "logistic-science-pack", "military-science-pack",
           "chemical-science-pack", "production-science-pack", "utility-science-pack",
           "space-science-pack")


# --------------------------------------------------------------------------- pure core
def _tiles(box):
    """Inclusive tile box (l,t,r,b) -> the set of tiles it occupies."""
    l, t, r, b = box
    return {(x, y) for x in range(l, r + 1) for y in range(t, b + 1)}


def chain_graph(sinks, inserters):
    """{sink index -> set of sink indices it feeds} from inserters that pick out of one sink
    and drop into another.

    This is what makes a lab grid legible without being told: an inserter picking from lab A
    and dropping into lab B is an edge A->B, and the grid's flood direction falls out of it.
    """
    owner = {}
    for si, box in enumerate(sinks):
        for t in _tiles(box):
            owner[t] = si
    out = {si: set() for si in range(len(sinks))}
    for ins in inserters:
        a = owner.get(tuple(ins["pick"]))
        b = owner.get(tuple(ins["drop"]))
        if a is not None and b is not None and a != b:
            out[a].add(b)
    return out


def reachable(graph, start):
    seen = {start}
    stack = [start]
    while stack:
        n = stack.pop()
        for m in graph.get(n, ()):
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return seen


def by_reach(sinks, graph):
    """Sink indices best-first: the one that reaches the most others along the chain first.

    Not just the head, the whole ORDER, because the best consumer to feed is often walled in.
    On the live map the true head is the lab at (0,40) - it reaches 9 of the 10 labs - but
    every tile around it is taken by the chain inserters themselves, and the only lab with a
    free inserter slot is (0,36), which still reaches 3. A planner that insisted on the head
    would report "no routable position" and do nothing, which is worse than a smaller feed.

    Ties break on position so the same map always yields the same choice; this runs unattended
    and a feed that moved every lap would be worse than no feed at all.
    """
    return sorted(range(len(sinks)),
                  key=lambda si: (-len(reachable(graph, si)), sinks[si][0], sinks[si][1]))


def head(sinks, graph):
    """The single best consumer to feed. `by_reach` is what the planner actually walks."""
    if not sinks:
        return None
    return by_reach(sinks, graph)[0]


def injection_points(sink_box, free):
    """[(inserter_tile, belt_tile)] around one consumer.

    The inserter sits against the machine and the belt sits on its far side, so the inserter
    picks from the belt and drops into the machine. `free(tile)` says whether a tile is empty.
    Ordered deterministically.
    """
    l, t, r, b = sink_box
    out = []
    for x in range(l, r + 1):
        out.append(((x, t - 1), (x, t - 2)))       # above, belt further above
        out.append(((x, b + 1), (x, b + 2)))       # below
    for y in range(t, b + 1):
        out.append(((l - 1, y), (l - 2, y)))       # left
        out.append(((r + 1, y), (r + 2, y)))       # right
    return [(i, bt) for (i, bt) in out if free(i) and free(bt)]


def rank_injections(cands, cost):
    """Cheapest-to-reach first. `cost(belt_tile)` returns a route length or None for no route.
    Unroutable candidates are dropped rather than ranked last, so callers cannot pick one."""
    scored = []
    for ins, belt in cands:
        c = cost(belt)
        if c is not None:
            scored.append((c, ins, belt))
    scored.sort(key=lambda s: (s[0], s[1], s[2]))
    return [(ins, belt, c) for (c, ins, belt) in scored]


# --------------------------------------------------------------------------- live map
def _lua_sinks(item):
    """Consumers of `item`: labs for a science pack, else assemblers whose recipe eats it."""
    if item in SCIENCE:
        sel = ("for _,e in pairs(s.find_entities_filtered{type='lab'}) do local b=e.bounding_box;"
               "o[#o+1]='S|'..math.floor(b.left_top.x)..'|'..math.floor(b.left_top.y)..'|'"
               "..(math.ceil(b.right_bottom.x)-1)..'|'..(math.ceil(b.right_bottom.y)-1) end;")
    else:
        sel = ("for _,e in pairs(s.find_entities_filtered{type='assembling-machine'}) do"
               "  local r=e.get_recipe();"
               "  if r then for _,ing in pairs(r.ingredients) do if ing.name=='%s' then"
               "    local b=e.bounding_box;"
               "    o[#o+1]='S|'..math.floor(b.left_top.x)..'|'..math.floor(b.left_top.y)..'|'"
               "    ..(math.ceil(b.right_bottom.x)-1)..'|'..(math.ceil(b.right_bottom.y)-1)"
               "  end end end end;" % item)
    return sel


def read_sinks(A, item):
    """(sinks, inserters) - consumer boxes plus every inserter's pick/drop tile, which is what
    chain_graph needs to work out how the consumers hand the item to each other."""
    raw = A._print(
        "/sc local s=game.surfaces[1]; local o={};"
        + _lua_sinks(item) +
        "for _,e in pairs(s.find_entities_filtered{type='inserter'}) do"
        "  o[#o+1]='I|'..math.floor(e.pickup_position.x)..'|'..math.floor(e.pickup_position.y)"
        "    ..'|'..math.floor(e.drop_position.x)..'|'..math.floor(e.drop_position.y) end;"
        "rcon.print(table.concat(o,';'))").strip()
    sinks, ins = [], []
    for tok in (raw or "").split(";"):
        f = tok.split("|")
        if f[0] == "S" and len(f) == 5:
            sinks.append(tuple(int(v) for v in f[1:5]))
        elif f[0] == "I" and len(f) == 5:
            ins.append({"pick": (int(f[1]), int(f[2])), "drop": (int(f[3]), int(f[4]))})
    return sinks, ins


def read_termini(A, item):
    """Belt tiles carrying `item` whose downstream tile is empty ground - where the product
    currently stops. A belt that flows into another belt is a junction, not an output."""
    raw = A._print(
        "/sc local s=game.surfaces[1]; local o={};"
        "local D={[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}};"
        "for _,b in pairs(s.find_entities_filtered{type='transport-belt'}) do"
        "  local n=0;"
        "  for li=1,b.get_max_transport_line_index() do"
        "    n=n+b.get_transport_line(li).get_item_count('%s') end;"
        "  if n>0 then local v=D[b.direction];"
        "    if v then local nx=math.floor(b.position.x)+v[1];"
        "      local ny=math.floor(b.position.y)+v[2];"
        "      local ahead=s.find_entities_filtered{position={nx+0.5,ny+0.5},radius=0.4};"
        "      local solid=false;"
        "      for _,e in pairs(ahead) do if e.name~='character' then solid=true end end;"
        "      if not solid then"
        "        o[#o+1]=math.floor(b.position.x)..'|'..math.floor(b.position.y)..'|'..n"
        "      end end end end;"
        "rcon.print(table.concat(o,';'))" % item).strip()
    out = []
    for tok in (raw or "").split(";"):
        f = tok.split("|")
        if len(f) == 3:
            out.append(((int(f[0]), int(f[1])), int(f[2])))
    out.sort(key=lambda t: (-t[1], t[0]))          # fullest terminus first
    return out


def plan(A, item, bounds=None):
    """Work out the whole feed for `item`, or say why it cannot be built.

    Returns a dict: {'item', 'from', 'to_sink', 'inserter', 'belt', 'route', 'reason'}.
    `reason` is set and `route` is None when there is nothing to do.
    """
    res = {"item": item, "from": None, "to_sink": None, "inserter": None,
           "belt": None, "route": None, "reason": None}

    termini = read_termini(A, item)
    if not termini:
        res["reason"] = "no belt is carrying %s to a dead end" % item
        return res
    sinks, inserters = read_sinks(A, item)
    if not sinks:
        res["reason"] = "nothing on this map consumes %s" % item
        return res

    src = termini[0][0]
    res["from"] = src
    graph = chain_graph(sinks, inserters)
    order = by_reach(sinks, graph)

    xs = [src[0]] + [s[0] for s in sinks] + [s[2] for s in sinks]
    ys = [src[1]] + [s[1] for s in sinks] + [s[3] for s in sinks]
    obs = belt_router.scan_obstacles(*(bounds or (min(xs) - 20, min(ys) - 20,
                                                  max(xs) + 20, max(ys) + 20)))
    occupied = set(obs.hard) | set(obs.belts)
    start = _beside(src, occupied)

    def free(t):
        return t not in occupied

    def cost(belt_tile):
        # Start one tile OFF the source belt: starting on it lets the router's cheapest first
        # move be a reversal straight back down the line that feeds it.
        r = belt_router.plan_route(start, belt_tile, obstacles=obs)
        return len(r) if r else None

    # Walk consumers best-first and take the first one we can actually reach and build on.
    for si in order:
        ranked = rank_injections(injection_points(sinks[si], free), cost)
        if not ranked:
            continue
        ins, belt, _ = ranked[0]
        route = belt_router.plan_route(start, belt, obstacles=obs, goal_dir=_toward(belt, ins))
        if not route:
            continue
        res.update({"to_sink": sinks[si], "inserter": ins, "belt": belt, "route": route,
                    "reaches": len(reachable(graph, si)), "of": len(sinks)})
        return res

    res["reason"] = ("no consumer of %s has a free inserter slot we can route to (%d checked)"
                     % (item, len(sinks)))
    return res


def _beside(tile, occupied):
    """A free tile next to `tile` - the take-off. Deterministic order."""
    x, y = tile
    for d in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        c = (x + d[0], y + d[1])
        if c not in occupied:
            return c
    return tile


def _toward(a, b):
    """Belt direction from tile a to adjacent tile b, in Factorio's 16-way numbering."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx > 0:
        return 4
    if dx < 0:
        return 12
    if dy > 0:
        return 8
    return 0


def describe(p):
    if p.get("reason"):
        return "feed %s: %s" % (p["item"], p["reason"])
    return ("feed %s: %s -> consumer %s (reaches %d of %d), %d belt tiles, inserter at %s"
            % (p["item"], p["from"], p["to_sink"], p.get("reaches", 1), p.get("of", 1),
               len(p["route"]), p["inserter"]))


# --------------------------------------------------------------------------- building it
def _inventory(A, names):
    q = ";".join("rcon.print('%s='..inv.get_item_count('%s'))" % (n, n) for n in names)
    raw = A._print("/sc local p=storage.derpface; if not (p and p.valid) then return end;"
                   "local inv=p.get_main_inventory();" + q)
    have = {}
    for line in (raw or "").splitlines():
        if "=" in line:
            k, v = line.strip().split("=", 1)
            have[k] = int(v or 0)
    return have


def shortfall(need, have):
    """{item: missing count} - what we cannot build with. A belt run must go in COMPLETE or
    not at all: half a lane is not half a feed, it is a broken belt that reads as a feed."""
    return {n: c - have.get(n, 0) for n, c in need.items() if c > have.get(n, 0)}


def build(A, item, log=None):
    """Plan and lay the feed. Returns the plan dict with 'built' set, or 'reason' if it did not.

    Refuses to build a partial run, and refuses to build at all while the operator is logged in.
    """
    say = log or (lambda m: None)
    p = plan(A, item)
    if not p.get("route"):
        return p
    try:
        import bootstrap
        if bootstrap.operator_present():
            p["reason"] = "operator present, standing down"
            return p
    except Exception:
        pass

    need = {}
    for s in p["route"]:
        if not s.get("adopt"):
            need[s["entity"]] = need.get(s["entity"], 0) + 1
    need["inserter"] = need.get("inserter", 0) + 1
    short = shortfall(need, _inventory(A, list(need)))
    if short:
        try:
            import bootstrap
            for n, c in list(short.items()):
                bootstrap.depot_take(n, c)
            short = shortfall(need, _inventory(A, list(need)))
        except Exception:
            pass
    if short:
        p["reason"] = "short " + ", ".join("%d %s" % (c, n) for n, c in sorted(short.items()))
        return p

    say(describe(p))
    for cmd in belt_router.plan_to_lua(p["route"]):
        A._print(cmd)
    # The inserter's `direction` points at its PICKUP side, not where it drops - the gotcha
    # that once put five inserters in backwards and looked fine because the machines had been
    # primed by hand.
    ix, iy = p["inserter"]
    A._print("/sc local s=game.surfaces[1]; local f=game.forces.player;"
             "local p=storage.derpface; local inv=p and p.valid and p.get_main_inventory();"
             "if s.can_place_entity{name='inserter',position={%s,%s},force=f,direction=%d} then"
             "  if inv and inv.get_item_count('inserter')>0 then inv.remove{name='inserter',count=1};"
             "    s.create_entity{name='inserter',position={%s,%s},direction=%d,force=f} end end"
             % (ix + 0.5, iy + 0.5, _toward(p["inserter"], p["belt"]),
                ix + 0.5, iy + 0.5, _toward(p["inserter"], p["belt"])))
    p["built"] = True
    return p


def verify(A, item, sink_box, settle=25.0):
    """Did it actually do something? The standing rule is that a build which changes nothing
    gets removed, so the answer has to be measurable rather than assumed.

    Settle first. A 68-tile run takes well over a minute to carry its first item, so measuring
    the instant the last belt goes down reports "0 supplied" for a feed that is working
    perfectly - and on that reading the rule would tear out a correct build.
    """
    import time
    time.sleep(settle)
    l, t, r, b = sink_box
    return A._print(
        "/sc local s=game.surfaces[1]; local n,ok=0,0;"
        "for _,e in pairs(s.find_entities_filtered{area={{%d,%d},{%d,%d}}}) do"
        "  if e.type=='lab' or e.type=='assembling-machine' then n=n+1;"
        "    if e.status~=defines.entity_status.missing_science_packs"
        "       and e.status~=defines.entity_status.item_ingredient_shortage then ok=ok+1 end end end;"
        "rcon.print(ok..'/'..n..' consumers supplied')" % (l - 1, t - 1, r + 1, b + 1)).strip()


def feed_stalled(A, items=SCIENCE, log=None):
    """Loop hook: for each item that is piling up at a dead end while something that eats it
    goes hungry, build the connection. This is the part that was missing - the analysis was
    being done and then written into a document for a person to action."""
    say = log or (lambda m: None)
    built = 0
    for item in items:
        try:
            p = build(A, item, log=say)
            if p.get("built"):
                built += 1
                say("feed built for %s: %s" % (item, verify(A, item, p["to_sink"])))
        except Exception as e:
            say("feed %s error: %s" % (item, e))
    return built


if __name__ == "__main__":
    import sys
    import autopilot as A
    item = sys.argv[1] if len(sys.argv) > 1 else "automation-science-pack"
    if "--build" in sys.argv:
        p = build(A, item, log=print)
        print(describe(p) if not p.get("built") else
              "BUILT | " + verify(A, item, p["to_sink"]))
    else:
        print(describe(plan(A, item)))
