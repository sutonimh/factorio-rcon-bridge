#!/usr/bin/env python3
"""Autopilot primitives for driving the player character over RCON.

Real-resources only: walks the real character, mines real ore patches into the
real inventory, builds from the real inventory. Nothing is spawned from nothing.

Usage:
    python3 autopilot.py walk <x> <y>            walk to a coordinate
    python3 autopilot.py mine <name> <count>     mine nearby resource until count
    python3 autopilot.py goto-mine <name> <n>    walk to nearest <name>, mine n
    python3 autopilot.py pos                      print current position
"""
import sys, time, math, json, pathlib
import rcon  # reuse the client in this dir

HERE = pathlib.Path(__file__).resolve().parent

DIRS16 = 16  # Factorio 2.0 uses a 16-direction system; cardinals are multiples of 4


def _print(cmd):
    return rcon.run(cmd)


def pos():
    out = _print("/sc local p=storage.derpface; rcon.print(p.position.x..','..p.position.y)")
    x, y = out.strip().split(",")
    return float(x), float(y)


def heading(px, py, tx, ty):
    # Factorio: +y is south. angle 0 = north, clockwise.
    ang = math.atan2(tx - px, -(ty - py))  # radians, 0=north
    idx = round(ang / (2 * math.pi / DIRS16)) % DIRS16
    return idx


# The 8 directions the character can HOLD continuously (one fixed walking_state).
# Aiming a continuous heading at an off-axis point makes the 16-way snap oscillate
# between two neighbours (crab/triangle); instead we move in pure axis/diagonal legs.
DIR8 = {(0, -1): 0, (1, -1): 2, (1, 0): 4, (1, 1): 6,
        (0, 1): 8, (-1, 1): 10, (-1, 0): 12, (-1, -1): 14}


def stop():
    """Halt the character NOW. ALWAYS call this before killing/restarting any pathing
    driver: a killed walk leaves walking_state=true in-game and the character runs
    endlessly in the last direction (Seth saw this happen). Idempotent + cheap."""
    return _print("/sc storage.derpface.walking_state={walking=false}")


def _legs_to(px, py, wx, wy):
    """Decompose a move (px,py)->(wx,wy) into FIXED legs the character holds in ONE
    direction each: a 45-degree diagonal leg (consuming the shorter axis) then a single
    cardinal leg for the remainder. Each leg is ((sx,sy), endx, endy). Direction is fixed
    per leg, so it physically can't oscillate/crab (the resilient model)."""
    dx, dy = wx - px, wy - py
    adx, ady = abs(dx), abs(dy)
    legs = []
    cx, cy = px, py
    diag = min(adx, ady)
    if diag >= 0.5 and adx >= 0.5 and ady >= 0.5:
        sx = 1 if dx > 0 else -1
        sy = 1 if dy > 0 else -1
        cx, cy = px + sx * diag, py + sy * diag
        legs.append(((sx, sy), cx, cy))
    rdx, rdy = wx - cx, wy - cy
    if abs(rdx) >= 0.5:
        legs.append(((1 if rdx > 0 else -1, 0), wx, cy))
        cx = wx
    if abs(rdy) >= 0.5:
        legs.append(((0, 1 if rdy > 0 else -1), cx, wy))
    return legs


def _blocked_tiles(minx, miny, maxx, maxy):
    """Query obstacle tiles in a bbox -> set of (x,y). Treats belts/splitters/
    undergrounds as obstacles too: belts PUSH the walking character, so routing over
    them causes the stutter/slow-crawl. Excludes only character/resource/item/ghost."""
    lua = (
        "/sc local s=game.surfaces['nauvis']; local o={};"
        "for _,e in pairs(s.find_entities_filtered{area={{" + str(minx) + "," + str(miny) + "},{" + str(maxx) + "," + str(maxy) + "}}}) do"
        "  local t=e.type;"
        "  if t~='character' and t~='character-corpse' and t~='resource' and t~='item-entity' and t~='entity-ghost' and t~='tile-ghost' then"
        "    local b=e.bounding_box; for tx=math.floor(b.left_top.x),math.ceil(b.right_bottom.x)-1 do for ty=math.floor(b.left_top.y),math.ceil(b.right_bottom.y)-1 do o[#o+1]=tx..','..ty end end end end;"
        "for tx=" + str(minx) + "," + str(maxx) + " do for ty=" + str(miny) + "," + str(maxy) + " do if string.find(s.get_tile(tx,ty).name,'water') then o[#o+1]=tx..','..ty end end end;"
        "rcon.print(table.concat(o,';'))"
    )
    out = _print(lua).strip()
    blocked = set()
    for tok in out.split(";"):
        if "," in tok:
            try:
                x, y = tok.split(","); blocked.add((int(x), int(y)))
            except ValueError:
                pass
    return blocked


def _clear_line(ax, ay, bx, by, blocked):
    """True if the straight line from (ax,ay) to (bx,by) crosses no blocked tile."""
    n = int(max(abs(bx - ax), abs(by - ay)))
    for i in range(n + 1):
        t = i / n if n else 0
        if (round(ax + (bx - ax) * t), round(ay + (by - ay) * t)) in blocked:
            return False
    return True


def _clear_Lpath(ax, ay, bx, by, blocked):
    """True if the L-PATH the leg-walker actually takes - a 45-degree diagonal leg then a
    cardinal leg - from (ax,ay) to (bx,by) is clear. Used for string-pulling so the few
    waypoints we keep are exactly the ones the walker can glide between without clipping."""
    dx, dy = bx - ax, by - ay
    sx = (dx > 0) - (dx < 0)
    sy = (dy > 0) - (dy < 0)
    x, y = ax, ay
    for _ in range(min(abs(dx), abs(dy))):           # diagonal leg
        x += sx; y += sy
        if (x, y) in blocked:
            return False
    while x != bx:                                   # cardinal remainder (x)
        x += (1 if bx > x else -1)
        if (x, y) in blocked:
            return False
    while y != by:                                   # cardinal remainder (y)
        y += (1 if by > y else -1)
        if (x, y) in blocked:
            return False
    return True


def route(sx, sy, gx, gy, pad=6):
    """Path from (sx,sy) to (gx,gy) avoiding obstacles (incl. belts). Straight line if
    clear; else node-capped A* string-pulled into a few long straight segments (so the
    walk is smooth). Falls back to a direct line if no path is found or it's too complex."""
    sx, sy, gx, gy = round(sx), round(sy), round(gx), round(gy)
    minx, maxx = min(sx, gx) - pad, max(sx, gx) + pad
    miny, maxy = min(sy, gy) - pad, max(sy, gy) + pad
    blocked = _blocked_tiles(minx, miny, maxx, maxy)
    blocked.discard((sx, sy)); blocked.discard((gx, gy))
    if _clear_line(sx, sy, gx, gy, blocked):
        return [(gx, gy)]
    import heapq
    start, goal = (sx, sy), (gx, gy)
    nbrs = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]
    openq = [(0, start)]; came = {start: None}; g = {start: 0}
    found = False; expansions = 0
    while openq and expansions < 25000:
        _, cur = heapq.heappop(openq); expansions += 1
        if cur == goal:
            found = True; break
        for dx, dy in nbrs:
            nx, ny = cur[0] + dx, cur[1] + dy
            if not (minx <= nx <= maxx and miny <= ny <= maxy) or (nx, ny) in blocked:
                continue
            if (dx and dy) and ((cur[0] + dx, cur[1]) in blocked or (cur[0], cur[1] + dy) in blocked):
                continue  # don't cut corners
            ng = g[cur] + (1.414 if dx and dy else 1.0)
            if (nx, ny) not in g or ng < g[(nx, ny)]:
                g[(nx, ny)] = ng; came[(nx, ny)] = cur
                heapq.heappush(openq, (ng + math.hypot(gx - nx, gy - ny), (nx, ny)))
    if not found:
        return [(gx, gy)]
    path = []
    n = goal
    while n is not None:
        path.append(n); n = came[n]
    path.reverse()
    # String-pull into FEW waypoints, each reachable from the previous by a clear L-PATH (the
    # diagonal+cardinal legs the walker actually takes). This collapses the jagged A* staircase
    # (which made the leg-walker oscillate) into a handful of long, glide-able legs that also
    # never clip a building. Greedily extend each waypoint to the farthest L-clear point.
    if len(path) < 2:
        return [goal]
    wps = []
    i = 0
    while i < len(path) - 1:
        j = len(path) - 1
        while j > i + 1 and not _clear_Lpath(path[i][0], path[i][1], path[j][0], path[j][1], blocked):
            j -= 1
        wps.append(path[j]); i = j
    return wps


_route_cache = {}   # (start_region, goal) -> waypoints; reused so we don't recompute the same
                    # route every walk (Seth's rule). Invalidated when the character deviates.


def walk(tx, ty, tol=2.0, timeout=150):
    """Smooth pathfinding walk: route around obstacles into a few long straight L-legs, walk
    each with ONE fixed direction (no mid-leg re-sends) for a smooth glide. The route is CACHED
    by start-region+goal and reused; it's only recomputed when the character genuinely stalls
    (deviates from the plan) - we don't recalculate pathfinding every time."""
    t0 = time.time()
    key = (round(tx), round(ty))
    try:
        px, py = pos()
        ck = (round(px / 8), round(py / 8), key[0], key[1])
        wps = _route_cache.get(ck)
        if wps is None:
            wps = route(px, py, tx, ty) or [(round(tx), round(ty))]
            _route_cache[ck] = wps
        for k, (wx, wy) in enumerate(wps):
            final = (k == len(wps) - 1)
            px, py = pos()
            for (dvec, ex, ey) in _legs_to(px, py, wx, wy):
                d = DIR8[dvec]
                nrm = math.hypot(dvec[0], dvec[1]) or 1.0
                # leg arrival tolerance: tight for the final approach, loose at turns
                legtol = tol if (final and (ex, ey) == (wx, wy)) else 0.8
                _print(f"/sc storage.derpface.walking_state={{walking=true,direction={d}}}")
                last_move = time.time()
                ppx, ppy = px, py
                sidesteps = 0
                while True:
                    if time.time() - t0 > timeout:
                        _route_cache.pop(ck, None)   # plan was bad -> drop it
                        return px, py, False
                    px, py = pos()
                    # remaining distance ALONG the leg direction (projection):
                    # overshoot/lateral drift can't fool it, and direction never
                    # changes mid-leg, so the character glides straight (no crab).
                    rem = ((ex - px) * dvec[0] + (ey - py) * dvec[1]) / nrm
                    if rem <= legtol:
                        break
                    if math.hypot(px - ppx, py - ppy) > 0.1:
                        last_move = time.time()
                    elif time.time() - last_move > 1.0:   # genuinely stuck: sidestep + resume
                        sidesteps += 1
                        if sidesteps > 3:
                            # the character is NOT routing as expected -> drop the cached plan
                            # and recompute a fresh route from where it actually is.
                            _route_cache.pop(ck, None)
                            wps2 = route(px, py, tx, ty) or [(round(tx), round(ty))]
                            _route_cache[ck] = wps2
                            return walk(tx, ty, tol=tol, timeout=max(10, int(timeout - (time.time() - t0))))
                        sd = (d + 4) % 16
                        _print(f"/sc storage.derpface.walking_state={{walking=true,direction={sd}}}")
                        time.sleep(0.3)
                        _print(f"/sc storage.derpface.walking_state={{walking=true,direction={d}}}")
                        last_move = time.time()
                    ppx, ppy = px, py
                    time.sleep(0.12)
        return px, py, True
    finally:
        # ALWAYS halt on exit (normal, timeout, or exception) so the character never
        # keeps running. Process-kill bypasses this, hence stop() before re-pathing.
        _print("/sc storage.derpface.walking_state={walking=false}")


def belt_path(sx, sy, gx, gy, pad=14):
    """4-directional A* tile path from (sx,sy) to (gx,gy) that AVOIDS every building
    AND every existing belt (Seth's rule: belts never go through buildings or cross
    other belts). Returns the full ordered tile list, or [] if no clear path exists."""
    import heapq
    sx, sy, gx, gy = round(sx), round(sy), round(gx), round(gy)
    minx, maxx = min(sx, gx) - pad, max(sx, gx) + pad
    miny, maxy = min(sy, gy) - pad, max(sy, gy) + pad
    blocked = _blocked_tiles(minx, miny, maxx, maxy)   # includes ALL entities (turrets, belts, poles, ...) + water
    blocked.discard((sx, sy)); blocked.discard((gx, gy))
    start, goal = (sx, sy), (gx, gy)
    openq = [(0, start)]; came = {start: None}; g = {start: 0}; found = False; exp = 0
    while openq and exp < 60000:
        _, cur = heapq.heappop(openq); exp += 1
        if cur == goal:
            found = True; break
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = cur[0] + dx, cur[1] + dy
            if not (minx <= nx <= maxx and miny <= ny <= maxy) or (nx, ny) in blocked:
                continue
            ng = g[cur] + 1
            if (nx, ny) not in g or ng < g[(nx, ny)]:
                g[(nx, ny)] = ng; came[(nx, ny)] = cur
                heapq.heappush(openq, (ng + abs(gx - nx) + abs(gy - ny), (nx, ny)))
    if not found:
        return []
    path = []; n = goal
    while n is not None:
        path.append(n); n = came[n]
    path.reverse()
    return path


def _blocked_buildings(minx, miny, maxx, maxy):
    """Obstacle tiles for DIRECT belt routing: NON-belt buildings (turrets, machines,
    poles, chests, furnaces, splitters, drills) + water. Existing transport-belts and
    underground-belts are NOT obstacles here: a routed belt crosses them with an
    underground pair (the direct+underground model). Returns set of (x,y)."""
    lua = (
        "/sc local s=game.surfaces['nauvis']; local o={};"
        "for _,e in pairs(s.find_entities_filtered{area={{" + str(minx) + "," + str(miny) + "},{" + str(maxx) + "," + str(maxy) + "}}}) do"
        "  local t=e.type;"
        "  if t~='character' and t~='character-corpse' and t~='resource' and t~='item-entity' and t~='entity-ghost' and t~='tile-ghost'"
        "     and t~='transport-belt' and t~='underground-belt' then"
        "    local b=e.bounding_box; for tx=math.floor(b.left_top.x),math.ceil(b.right_bottom.x)-1 do for ty=math.floor(b.left_top.y),math.ceil(b.right_bottom.y)-1 do o[#o+1]=tx..','..ty end end end end;"
        "for tx=" + str(minx) + "," + str(maxx) + " do for ty=" + str(miny) + "," + str(maxy) + " do if string.find(s.get_tile(tx,ty).name,'water') then o[#o+1]=tx..','..ty end end end;"
        "rcon.print(table.concat(o,';'))"
    )
    out = _print(lua).strip()
    blocked = set()
    for tok in out.split(";"):
        if "," in tok:
            try:
                x, y = tok.split(","); blocked.add((int(x), int(y)))
            except ValueError:
                pass
    return blocked


def belt_route(sx, sy, gx, gy, pad=10):
    """Near-DIRECT 4-dir A* path from (sx,sy) to (gx,gy) treating only NON-belt
    buildings + water as hard obstacles (existing belts are passable, to be crossed
    with undergrounds). Diagonal-free, Manhattan heuristic, so it hugs a straight
    corridor and only detours around real buildings. Returns the full tile list."""
    import heapq
    sx, sy, gx, gy = round(sx), round(sy), round(gx), round(gy)
    minx, maxx = min(sx, gx) - pad, max(sx, gx) + pad
    miny, maxy = min(sy, gy) - pad, max(sy, gy) + pad
    blocked = _blocked_buildings(minx, miny, maxx, maxy)
    blocked.discard((sx, sy)); blocked.discard((gx, gy))
    start, goal = (sx, sy), (gx, gy)
    openq = [(0, start)]; came = {start: None}; g = {start: 0}; found = False; exp = 0
    while openq and exp < 60000:
        _, cur = heapq.heappop(openq); exp += 1
        if cur == goal:
            found = True; break
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = cur[0] + dx, cur[1] + dy
            if not (minx <= nx <= maxx and miny <= ny <= maxy) or (nx, ny) in blocked:
                continue
            # small turn penalty keeps the path straight (fewer corners = cleaner bus)
            turn = 0.0
            prev = came[cur]
            if prev is not None:
                pdx, pdy = cur[0] - prev[0], cur[1] - prev[1]
                if (pdx, pdy) != (dx, dy):
                    turn = 0.4
            ng = g[cur] + 1 + turn
            if (nx, ny) not in g or ng < g[(nx, ny)]:
                g[(nx, ny)] = ng; came[(nx, ny)] = cur
                heapq.heappush(openq, (ng + abs(gx - nx) + abs(gy - ny), (nx, ny)))
    if not found:
        return []
    path = []; n = goal
    while n is not None:
        path.append(n); n = came[n]
    path.reverse()
    return path


_TRACK_FILE = str(HERE / "build-track.json")


def _track_add(entries, tag):
    """Append placed (name,x,y) records under `tag` to the build-track file, so the
    exact entities a build placed can be torn down surgically (never area-destroy)."""
    try:
        with open(_TRACK_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.setdefault(tag, []).extend(entries)
    with open(_TRACK_FILE, "w") as f:
        json.dump(data, f)


def teardown(tag):
    """Surgically remove ONLY the entities a tagged build placed (from build-track),
    matching name + exact position. Never area-destroys, so existing infrastructure is
    untouched. Refunds nothing (conservative removal of our own additions)."""
    try:
        with open(_TRACK_FILE) as f:
            data = json.load(f)
    except Exception:
        return "teardown: no build-track file"
    ents = data.get(tag, [])
    if not ents:
        return f"teardown: nothing tracked under '{tag}'"
    rows = ";".join("{'%s',%g,%g}" % (e[0], e[1], e[2]) for e in ents)
    lua = (
        "/sc local s=game.surfaces['nauvis']; local L={" + rows + "}; local rem=0;"
        "for _,d in ipairs(L) do local e=s.find_entities_filtered{position={d[2],d[3]},radius=0.4,name=d[1]}[1]; if e then e.destroy(); rem=rem+1 end end;"
        "rcon.print('teardown: removed '..rem..'/'..#L..' tracked entities')"
    )
    out = _print(lua)
    data[tag] = []
    with open(_TRACK_FILE, "w") as f:
        json.dump(data, f)
    return out


def build_belt(sx, sy, gx, gy, item='transport-belt', ug='underground-belt', tag='belt', walk_first=True):
    """Build a DIRECT belt corridor from (sx,sy) to (gx,gy): route treating only NON-belt
    buildings as hard obstacles, and CROSS existing belts with underground-belt pairs
    (entrance just before the crossing, exit just after) instead of A*-snaking around
    them (the lesson from Seth's iron-belt fix). Walks to the start first. Tracks every
    placed entity under `tag` for surgical teardown(). Returns a status string."""
    if walk_first:
        goto(sx, sy)
    path = belt_route(sx, sy, gx, gy)
    if not path:
        return f"build_belt: NO route from ({sx},{sy}) to ({gx},{gy}) avoiding buildings; widen/clear"

    def d(a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        return 4 if dx == 1 else 12 if dx == -1 else 8 if dy == 1 else 0

    # which path tiles currently hold a crossable belt (transport/underground)?
    qrows = ",".join("{%d,%d}" % (t[0], t[1]) for t in path)
    qlua = (
        "/sc local s=game.surfaces['nauvis']; local L={" + qrows + "}; local o={};"
        "for i,t in ipairs(L) do local e=s.find_entities_filtered{position={t[1]+0.5,t[2]+0.5},radius=0.4,type={'transport-belt','underground-belt','splitter'}}[1];"
        "  o[#o+1]=(e and '1' or '0') end; rcon.print(table.concat(o,''))"
    )
    occ = _print(qlua).strip()
    isbelt = [c == '1' for c in occ] if len(occ) == len(path) else [False] * len(path)

    # plan placements: free tile -> transport-belt; runs of existing belt -> underground pair
    placements = []   # (name, tilex, tiley, dir, type) ; type '' for belt, 'input'/'output' for ug
    i = 0
    n = len(path)
    while i < n:
        if isbelt[i]:
            j = i
            while j < n and isbelt[j]:
                j += 1
            # crossing run is path[i..j-1]; entrance = path[i-1], exit = path[j]
            if i - 1 < 0 or j >= n or (j - i) > 4:
                # can't bracket the crossing cleanly; skip these tiles (leave the belt)
                i = j
                continue
            ent = path[i - 1]; ex = path[j]
            dd = d(ent, ex if ex != ent else path[i])
            # replace the entrance free-tile belt with an underground input
            if placements and placements[-1][1] == ent[0] and placements[-1][2] == ent[1]:
                placements.pop()
            placements.append((ug, ent[0], ent[1], dd, 'input'))
            placements.append((ug, ex[0], ex[1], dd, 'output'))
            i = j + 1
            # the tile after exit (if any) belt faces from exit onward; continue loop
            continue
        else:
            nxt = path[i + 1] if i + 1 < n else path[i - 1] if i > 0 else path[i]
            di = d(path[i], nxt) if i + 1 < n else (d(path[i - 1], path[i]) if i > 0 else 0)
            placements.append((item, path[i][0], path[i][1], di, ''))
            i += 1

    # emit build commands (conservative: pull from inventory)
    cmds = []
    for nm, tx, ty, di, ty2 in placements:
        cx, cy = tx + 0.5, ty + 0.5
        if ty2:
            spec = "name='%s',position={%g,%g},direction=%d,type='%s',force=f" % (nm, cx, cy, di, ty2)
        else:
            spec = "name='%s',position={%g,%g},direction=%d,force=f" % (nm, cx, cy, di)
        cmds.append(
            "do local it='%s'; if inv.get_item_count(it)>0 and s.can_place_entity{%s} then local e=s.create_entity{%s}; if e then inv.remove{name=it,count=1}; n=n+1; pl[#pl+1]=string.format('%%s|%%g|%%g',e.name,e.position.x,e.position.y) end end end"
            % (nm, spec, spec)
        )
    lua = (
        "/sc local s=game.surfaces['nauvis']; local p=storage.derpface; local f=p.force; local inv=p.get_main_inventory(); local n=0; local pl={}; "
        + " ".join(cmds) +
        " rcon.print(n..'/'..#pl..'\\n'..table.concat(pl,';'))"
    )
    out = _print(lua).strip()
    lines = out.split("\n", 1)
    placed = []
    if len(lines) > 1:
        for rec in lines[1].split(";"):
            parts = rec.split("|")
            if len(parts) == 3:
                placed.append([parts[0], float(parts[1]), float(parts[2])])
    if placed:
        _track_add(placed, tag)
    nplaced = lines[0].split("/")[0] if lines else "?"
    return f"build_belt[{tag}]: placed {nplaced} entities ({len(placements)} planned, route {len(path)} tiles) -> tracked"


def goto(cx, cy, tol=4.0):
    """Walk the character to a build/work site BEFORE operating there (Seth's standing
    rule: always be where you're actively building). Call this at the start of every
    build/teardown so Seth can watch it happen. Thin wrapper over the smooth walk()."""
    return walk(cx, cy, tol=tol)


def mine(name, count):
    # Deplete-and-insert: take ore from real resource entities near the player
    # and add the same amount to the inventory. The patch loses exactly what the
    # inventory gains, so no resources are created. Respects inventory space.
    lua = (
        "/sc local p=storage.derpface; local inv=p.get_main_inventory();"
        "local target=" + str(int(count)) + "; local name='" + name + "';"
        "local got=0;"
        "local es=p.surface.find_entities_filtered{position=p.position,radius=10,name=name};"
        "table.sort(es,function(a,b) return (a.position.x-p.position.x)^2+(a.position.y-p.position.y)^2 < (b.position.x-p.position.x)^2+(b.position.y-p.position.y)^2 end);"
        "for _,e in pairs(es) do if got>=target then break end;"
        "  if e.valid and e.amount and e.amount>0 then"
        "    local prod=e.prototype.mineable_properties.products[1].name;"
        "    local take=math.min(e.amount, target-got);"
        "    local ins=inv.insert{name=prod, count=take};"
        "    if ins>0 then if ins>=e.amount then e.destroy() else e.amount=e.amount-ins end; got=got+ins end"
        "  end end;"
        "rcon.print('mined '..got..' '..name)"
    )
    return _print(lua)


def clear_spaceship_debris(radius=300):
    """Remove the Space Age crash-site spaceship wreckage that litters spawn on a FRESH
    world (Seth's rule: always clear it). Collects any mineable loot first, then destroys
    every crash-site-* entity (ship, wreck pieces, loot chests). Returns count removed."""
    lua = (
        "/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); local n=0;"
        "for _,e in pairs(s.find_entities_filtered{position={0,0},radius=" + str(int(radius)) + "}) do"
        "  if e.valid and string.sub(e.name,1,11)=='crash-site-' then"
        "    local oi=e.get_output_inventory and e.get_output_inventory();"
        "    if oi then for _,c in pairs(oi.get_contents()) do inv.insert{name=c.name,count=c.count} end end;"
        "    local mp=e.prototype.mineable_properties;"
        "    if mp and mp.products then for _,pr in pairs(mp.products) do"
        "      if pr.type=='item' then local a=pr.amount or pr.amount_max or 0; if a>0 then inv.insert{name=pr.name,count=a} end end end end;"
        "    e.destroy(); n=n+1 end end;"
        "rcon.print('removed '..n..' crash-site debris')"
    )
    return _print(lua)


def richest_spot(name, near_x, near_y, radius=90):
    """Find the RICHEST tile of an ore patch (max ore summed over a 5x5 neighbourhood),
    not the nearest sparse edge (Seth's rule: always drill the richest deposits). Returns
    (tx, ty, density) - the ore tile to anchor a drill on - or None if no ore in range."""
    nx, ny, R = round(near_x), round(near_y), int(radius)
    lua = (
        "/sc local s=game.surfaces[1];"
        "local es=s.find_entities_filtered{position={" + str(nx) + "," + str(ny) + "},radius=" + str(R) + ",name='" + name + "'};"
        "local amt={}; for _,e in pairs(es) do amt[math.floor(e.position.x)..','..math.floor(e.position.y)]=e.amount end;"
        "local best,bx,by=-1,nil,nil;"
        "for _,e in pairs(es) do local x=math.floor(e.position.x); local y=math.floor(e.position.y); local sum=0;"
        "  for dx=-2,2 do for dy=-2,2 do local v=amt[(x+dx)..','..(y+dy)]; if v then sum=sum+v end end end;"
        "  if sum>best then best=sum; bx=x; by=y end end;"
        "if bx then rcon.print(bx..','..by..','..best) else rcon.print('NONE') end"
    )
    out = _print(lua).strip()
    if out == "NONE" or "," not in out:
        return None
    x, y, d = out.split(",")
    return int(x), int(y), int(d)


def clear_area(cx, cy, radius=10):
    """Clear a build site of trees + rocks (Seth's rule: >=10-tile clearspace around
    EVERY building, no trees/boulders/cliffs). Collects the wood/stone/coal the cleared
    trees+rocks yield (free stone for the bootstrap). Returns how many were removed and
    how many CLIFFS remain - cliffs can't be mined without explosives, so if cliffs>0 the
    caller must MOVE the build site. Returns (removed, cliffs)."""
    cx, cy, r = round(cx), round(cy), int(radius)
    lua = (
        "/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory();"
        "local A={{" + str(cx - r) + "," + str(cy - r) + "},{" + str(cx + r) + "," + str(cy + r) + "}};"
        "local removed=0;"
        "for _,e in pairs(s.find_entities_filtered{area=A, type={'tree','simple-entity'}}) do"
        "  if e.valid then local mp=e.prototype.mineable_properties;"
        "    if mp and mp.products then for _,pr in pairs(mp.products) do"
        "      if pr.type=='item' then local amt=pr.amount or pr.amount_max or 1; if amt>0 then inv.insert{name=pr.name,count=amt} end end end end;"
        "    e.destroy(); removed=removed+1 end end;"
        "local cliffs=#s.find_entities_filtered{area=A, type='cliff'};"
        "rcon.print(removed..'|'..cliffs)"
    )
    out = _print(lua).strip()
    try:
        rem, cliffs = out.split("|")
        return int(rem), int(cliffs)
    except ValueError:
        return 0, 0


def build(name, x, y, direction=0, walk_first=True, reach_tol=3.0, clear=10):
    # Walk the character to the build site (visible travel), then place the entity.
    # The cursor/hand-build API is client-authoritative for a connected player and
    # can't be driven over RCON, so placement itself is server-side create_entity.
    # Conservative: the item is removed from the real inventory (world += 1, inv -= 1).
    if walk_first:
        walk(x, y, tol=reach_tol)
    if clear:
        # Seth's rule: >=10-tile clearspace around every building. Clear trees/rocks;
        # if a cliff remains (unmineable) the site is bad - abort so the caller MOVES it.
        _, cliffs = clear_area(x, y, clear)
        if cliffs:
            return f"CLIFF x{cliffs} within {clear} of ({x},{y}) - MOVE build site"
    lua = (
        "/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory();"
        "local item='" + name + "'; local pos={" + str(x) + "," + str(y) + "}; local dir=" + str(int(direction)) + ";"
        "if inv.get_item_count(item)<1 then rcon.print('NO_ITEM '..item) return end;"
        "local proto=prototypes.item[item]; local ename=proto and proto.place_result and proto.place_result.name or item;"
        "if not s.can_place_entity{name=ename,position=pos,direction=dir,force=p.force} then rcon.print('CANT_PLACE '..item..' at '..pos[1]..','..pos[2]) return end;"
        "local e=s.create_entity{name=ename,position=pos,direction=dir,force=p.force};"
        "if e then inv.remove{name=item,count=1}; rcon.print('BUILT '..ename..' at '..math.floor(e.position.x)..','..math.floor(e.position.y)) else rcon.print('CREATE_FAILED '..item) end"
    )
    return _print(lua)


def craft(recipe, count, timeout=90):
    # script-craft on the player-less derpface (recursive hand-craft); see bootstrap._SC.
    sc = ("local D=storage.derpface; local INV=D.get_main_inventory(); local F=D.force;"
          "local STOP={['iron-plate']=true,['copper-plate']=true,['steel-plate']=true,['stone']=true,['coal']=true,['iron-ore']=true,['copper-ore']=true};"
          "local function cnt(n) return INV.get_item_count(n) end;"
          "local sc; sc=function(name,count) if STOP[name] then return 0 end; local r=F.recipes[name];"
          "  if not r or not r.enabled then return 0 end;"
          "  for _,fi in pairs(r.ingredients) do if fi.type=='fluid' then return 0 end end; local made=0;"
          "  for i=1,count do local ok=true;"
          "    for _,ing in pairs(r.ingredients) do if ing.type=='item' then"
          "      if cnt(ing.name)<ing.amount then sc(ing.name, ing.amount-cnt(ing.name)) end;"
          "      if cnt(ing.name)<ing.amount then ok=false; break end end end;"
          "    if not ok then break end;"
          "    for _,ing in pairs(r.ingredients) do if ing.type=='item' then INV.remove{name=ing.name,count=ing.amount} end end;"
          "    for _,prod in pairs(r.products) do if prod.type=='item' then INV.insert{name=prod.name,count=(prod.amount or prod.amount_max or 1)} end end;"
          "    made=made+1 end; return made end;")
    made = _print(f"/sc {sc} rcon.print(sc('{recipe}',{int(count)}))").strip()
    return f"crafted {made} {recipe}"


# --- Placement geometry (learned from Seth's hand-built layout) -----------------
# An entity's CENTER = top-left footprint tile + (tile_width/2, tile_height/2):
#   1x1 (belt/inserter/chest): tile (x,y) -> center (x+0.5, y+0.5)
#   2x2 (drill/furnace):       tile (x,y) -> center (x+1,   y+1)
# Passing the wrong center is why 1x1 belts/inserters used to land a tile off.
# Directions (2.0/2.1): N=0, E=4, S=8, W=12.
# Inserter `direction` is its PICKUP side (dir=12/west picks from the west tile,
# drops east). `drill.drop_position` / `inserter.pickup_position`/`drop_position`
# are readable - use them to verify, unlike fluidbox.

def place(name, tile_x, tile_y, direction=0, clear=10):
    """Place an entity by its TOP-LEFT footprint tile, auto-centering by size.
    Conservative: removes one from the real inventory. Returns a status string
    with the actual snapped position. Clears a >=10-tile clearspace (trees/rocks)
    around the site first (Seth's rule); aborts on an unmineable cliff so the caller
    MOVES the site."""
    if clear:
        _, cliffs = clear_area(tile_x, tile_y, clear)
        if cliffs:
            return f"CLIFF x{cliffs} within {clear} of tile({tile_x},{tile_y}) - MOVE build site"
    lua = (
        "/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory();"
        "local item='" + name + "'; local tx=" + str(tile_x) + "; local ty=" + str(tile_y) + "; local dir=" + str(int(direction)) + ";"
        "local proto=prototypes.item[item]; local ename=(proto and proto.place_result and proto.place_result.name) or item;"
        "local ep=prototypes.entity[ename]; local cx=tx+ep.tile_width/2; local cy=ty+ep.tile_height/2;"
        "if inv.get_item_count(item)<1 then rcon.print('NO_ITEM '..item) return end;"
        "if not s.can_place_entity{name=ename,position={cx,cy},direction=dir,force=p.force} then rcon.print('CANT_PLACE '..ename..' @tile('..tx..','..ty..')') return end;"
        "local e=s.create_entity{name=ename,position={cx,cy},direction=dir,force=p.force};"
        "if e then inv.remove{name=item,count=1}; rcon.print('BUILT '..ename..' @('..e.position.x..','..e.position.y..')') else rcon.print('CREATE_FAILED '..item) end"
    )
    return _print(lua)


def stamp_blueprint(entities):
    """STEP 1 of a build: stamp the blueprint as entity-ghosts (nothing real yet),
    so the player can review the plan before construction. `entities`: list of
    {name, x(center), y(center), dir?}. Follow with build_ghosts() once approved."""
    specs = ";".join("{'%s',%s,%s,%s}" % (e["name"], e["x"], e["y"], e.get("dir", 0)) for e in entities)
    lua = (
        "/sc local s=game.surfaces['nauvis']; local f=game.forces.player; local L={" + specs + "}; local n=0;"
        "for _,d in ipairs(L) do local g=s.create_entity{name='entity-ghost', inner_name=d[1], position={d[2],d[3]}, direction=d[4], force=f}; if g then n=n+1 end end;"
        "rcon.print('blueprint stamped: '..n..' ghosts (awaiting approval)')"
    )
    return _print(lua)


def build_ghosts(cadence=0.35, batch=2):
    """STEP 2: after the player approves the stamped blueprint, build the ghosts in
    a realistic player-like cadence (a couple at a time with a short delay),
    consuming items from inventory and fueling burners."""
    revive = (
        "/sc local p=storage.derpface; local s=game.surfaces['nauvis']; local inv=p.get_main_inventory(); local built=0;"
        "for _,g in pairs(s.find_entities_filtered{name='entity-ghost', force='player'}) do if built>=" + str(batch) + " then break end;"
        "  local gp=g.ghost_prototype; local item=(gp.items_to_place_this and gp.items_to_place_this[1] and gp.items_to_place_this[1].name) or g.ghost_name;"
        "  if inv.get_item_count(item)>0 then local col,e=g.revive{}; if e then inv.remove{name=item,count=1}; built=built+1;"
        "    if e.type=='furnace' or e.name=='burner-mining-drill' or e.name=='burner-inserter' then local c=math.min(5,inv.get_item_count('coal')); if c>0 then e.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end end end end end;"
        "rcon.print(built..'|'..#s.find_entities_filtered{name='entity-ghost',force='player'})"
    )
    n = 0
    for _ in range(500):
        r = _print(revive).strip()
        try:
            built, remaining = r.split("|")
            n += int(built)
            if remaining == "0":
                break
            if built == "0":
                return f"built {n}; STALLED (out of materials), {remaining} ghosts remain"
        except ValueError:
            break
        time.sleep(cadence)
    return f"built {n} entities in cadence"


NOTE_ANCHOR = (-10, -30)   # world tile NW of + above the base furnaces (clear space); panel grows down


def _render_notes(lines):
    """Draw a vertical task panel in WORLD-SPACE near the base via the rendering API, replacing the
    previous one each call. WHY world-space, not a GUI: derpface is a PLAYER-LESS character (no
    `.gui`), and the autopilot runs 24/7 with NO connected player, so the old GUI panel
    (`storage.derpface.gui.screen`) crashed every lap ('LuaEntity doesn't contain key gui') and the
    on-screen note never showed. Rendering needs no player and persists across saves. `lines` is a
    list of (text, bold) tuples. The render objects are stored in `storage.autopilot_notes` so each
    update destroys exactly the prior panel - no leak, and no `rendering.clear()` that would also
    wipe unrelated renders. Anchored at the base so anyone who connects to watch sees it there."""
    ax, ay = NOTE_ANCHOR
    safe = lambda s: str(s).replace("'", "").replace("\\", "")[:90]
    parts = []
    for i, (txt, bold) in enumerate(lines):
        color = "{1,0.85,0.3}" if bold else "{0.72,0.78,0.85}"
        scale = "1.5" if bold else "1.1"
        parts.append(
            "r=rendering.draw_text{text='" + safe(txt) + "', surface=s, target={" + str(ax) + "," + str(ay) + "+" + str(i) + "*0.95}, "
            "color=" + color + ", scale=" + scale + ", alignment='left', scale_with_zoom=false}; "
            "storage.autopilot_notes[#storage.autopilot_notes+1]=r; "
        )
    lua = (
        "/sc if storage.autopilot_notes then for _,id in pairs(storage.autopilot_notes) do if id and id.valid then id.destroy() end end end; "
        "storage.autopilot_notes={}; local s=game.surfaces[1]; local r; "
        + "".join(parts) +
        "rcon.print('notes: " + safe(lines[0][0] if lines else "") + "')"
    )
    return _print(lua)


def now(action, plan=None):
    """Update the on-screen note (world-space rendering near the base; see `_render_notes`).
    Structure (Seth's rule): the FIRST line is always the live pending task / thing being waited on
    (bold, highlighted); below it is the task QUEUE. Keep it current - call this at every action and
    each maintenance lap so it never goes stale."""
    plan = plan if plan is not None else [
        "Supply: iron/copper/coal mines -> base smelters",
        "Red + green science (assemblers -> labs)",
        "Research -> oil-gathering",
        "Oil economy + blue science",
        "construction-robotics -> robot factory",
        "(biters OFF, crash debris cleared)",
    ]
    lines = [("> " + str(action), True), ("-- queue --", False)] + [(t, False) for t in plan]
    return _render_notes(lines)


def notepad(lines):
    """Persistent on-screen 'notepad' (world-space rendering near the base; see `_render_notes`).
    The first line is the bold header; each queue entry is its own line."""
    items = [("AUTOPILOT QUEUE", True)] + [(s, False) for s in lines]
    return _render_notes(items)


def snapshot(path=None):
    """Dump every player-built entity to a JSON file (the persistent build store),
    so infrastructure can be rebuilt if destroyed/deleted. Read-only on the game."""
    path = path or str(HERE / "base-snapshot.json")
    lua = (
        "/sc local s=game.surfaces['nauvis']; local out={};"
        "for _,e in pairs(s.find_entities_filtered{force='player'}) do"
        "  if e.name~='character' and e.name~='character-corpse' then"
        "    local rec='';"
        "    if e.type=='assembling-machine' or e.type=='furnace' then pcall(function() local r=e.get_recipe(); if r then rec=r.name end end) end;"
        "    out[#out+1]=e.name..'\\t'..string.format('%.2f',e.position.x)..'\\t'..string.format('%.2f',e.position.y)..'\\t'..e.direction..'\\t'..rec"
        "  end end;"
        "rcon.print(#out..'\\n'..table.concat(out,'\\n'))"
    )
    raw = _print(lua)
    lines = raw.split("\n")
    count = int(lines[0]) if lines and lines[0].strip().isdigit() else 0
    ents = []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        parts = ln.split("\t")
        if len(parts) < 4:
            continue
        ents.append({
            "name": parts[0], "x": float(parts[1]), "y": float(parts[2]),
            "direction": int(parts[3]), "recipe": parts[4] if len(parts) > 4 else "",
        })
    with open(path, "w") as f:
        json.dump({"count": len(ents), "entities": ents}, f, indent=0)
    return f"snapshot: {len(ents)} entities -> {path} (reported {count})"


def rebuild(path=None):
    """Restore any entity in the snapshot that is missing from the world (e.g.
    destroyed by biters or deleted). Re-creates missing entities at their saved
    position/direction and re-applies assembler recipes."""
    path = path or str(HERE / "base-snapshot.json")
    with open(path) as f:
        snap = json.load(f)
    # send the entity list to the game; it re-creates only what is missing
    rows = ";".join(
        f"{{'{e['name']}',{e['x']},{e['y']},{e['direction']},'{e['recipe']}'}}"
        for e in snap["entities"]
    )
    lua = (
        "/sc local s=game.surfaces['nauvis']; local f=game.forces.player; local want={" + rows + "};"
        "local rebuilt=0; local ok=0;"
        "for _,d in ipairs(want) do local name,x,y,dir,rec=d[1],d[2],d[3],d[4],d[5];"
        "  local found=s.find_entities_filtered{position={x,y},radius=0.6,name=name}[1];"
        "  if found then ok=ok+1 else"
        "    if s.can_place_entity{name=name,position={x,y},direction=dir,force=f} then"
        "      local e=s.create_entity{name=name,position={x,y},direction=dir,force=f};"
        "      if e then rebuilt=rebuilt+1; if rec~='' then pcall(function() e.set_recipe(rec) end) end end"
        "    end end end;"
        "rcon.print('rebuild: '..ok..' intact, '..rebuilt..' restored')"
    )
    return _print(lua)


def announce(msg):
    """Post a status line to the IN-GAME console log (top-left, persists, scrollable)
    so the player can see what the autopilot is working on / its queue."""
    safe = msg.replace("'", "").replace("\\", "")
    return _print("/sc game.print('[autopilot] " + safe + "')")


def pickup(radius=12):
    """Pick up items lying on the ground (e.g. spilled coal) near the character
    into the real inventory. Conservative: items move from ground to inventory."""
    lua = (
        "/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory();"
        "local picked={};"
        "for _,e in pairs(s.find_entities_filtered{position=p.position, radius=" + str(radius) + ", name='item-on-ground'}) do"
        "  if e.valid and e.stack and e.stack.valid_for_read then local n=e.stack.name; local c=e.stack.count;"
        "    local ins=inv.insert{name=n,count=c};"
        "    if ins>0 then picked[n]=(picked[n] or 0)+ins; if ins>=c then e.destroy() else e.stack.count=c-ins end end end end;"
        "local o={}; for n,c in pairs(picked) do o[#o+1]=n..'x'..c end;"
        "rcon.print(#o>0 and ('picked up: '..table.concat(o,', ')) or 'nothing on ground within " + str(radius) + "')"
    )
    return _print(lua)


def refill_turrets(chest_x=20.5, chest_y=-2.5, threshold=50, target=100):
    """Keep gun turrets topped up to a FULL stack (100 magazines), refilling any
    turret that drops below 50%. Moves magazines from the ammo buffer chest.
    Designed to run on the defend loop; caps at available buffer ammo."""
    lua = (
        "/sc local s=game.surfaces['nauvis'];"
        "local chest=s.find_entities_filtered{position={" + str(chest_x) + "," + str(chest_y) + "},radius=2,name={'wooden-chest','iron-chest','steel-chest'}}[1];"
        "if not chest then rcon.print('no ammo chest') return end;"
        "local ci=chest.get_inventory(defines.inventory.chest);"
        "local refilled=0; local low=0;"
        "for _,t in pairs(s.find_entities_filtered{name='gun-turret'}) do"
        "  local ai=t.get_inventory(defines.inventory.turret_ammo);"
        "  if ai then local have=ai.get_item_count('firearm-magazine');"
        "    if have<" + str(threshold) + " then low=low+1;"
        "      local move=math.min(" + str(target) + "-have, ci.get_item_count('firearm-magazine'));"
        "      if move>0 then ai.insert{name='firearm-magazine',count=move}; ci.remove{name='firearm-magazine',count=move}; refilled=refilled+move end end end end;"
        "rcon.print('refill: '..low..' turrets low, +'..refilled..' mags ('..ci.get_item_count('firearm-magazine')..' left in buffer)')"
    )
    return _print(lua)


def store_overflow(ox=-20, oy=-36, keep_coal=100):
    """Drain bulk items from the player inventory into an auto-scaling chest array
    at (ox,oy). Keeps `keep_coal` coal in inventory for fueling. Places a new chest
    (from inventory wooden/iron chests) whenever the array is full."""
    lua = (
        "/sc local p=storage.derpface; local s=game.surfaces['nauvis']; local inv=p.get_main_inventory();"
        "local OX=" + str(ox) + "; local OY=" + str(oy) + "; local COLS=12;"
        "local function chests() return s.find_entities_filtered{area={{OX,OY},{OX+COLS,OY+10}}, name={'wooden-chest','iron-chest','steel-chest'}} end;"
        # clearance guard: only place in the dedicated zone, never adjacent to a non-chest build
        "local function clear_of_builds(gx,gy) for _,e in pairs(s.find_entities_filtered{area={{gx-1,gy-1},{gx+2,gy+2}},force='player'}) do if not string.find(e.name,'chest') then return false end end return true end;"
        "local function add_chest() for gy=OY,OY+9 do for gx=OX,OX+COLS-1 do"
        "  if clear_of_builds(gx,gy) and s.can_place_entity{name='wooden-chest',position={gx+0.5,gy+0.5},force=p.force} then"
        "    local item=(inv.get_item_count('iron-chest')>0 and 'iron-chest') or (inv.get_item_count('wooden-chest')>0 and 'wooden-chest') or nil;"
        "    if item then local c=s.create_entity{name=item,position={gx+0.5,gy+0.5},force=p.force}; if c then inv.remove{name=item,count=1}; return c end end;"
        "    return nil end end end return nil end;"
        # bulk items to overflow (raw/intermediate); keep tools/ammo/equipment in inventory
        "local bulk={'iron-ore','copper-ore','stone','iron-plate','copper-plate','iron-gear-wheel','copper-cable','electronic-circuit','wood'};"
        "local moved={}; local cs=chests(); if #cs==0 then local c=add_chest(); if c then cs={c} end end;"
        "local function put(name,count) for _,c in pairs(cs) do local n=c.get_inventory(defines.inventory.chest).insert{name=name,count=count}; count=count-n; moved[name]=(moved[name] or 0)+n; if count<=0 then return 0 end end; return count end;"
        "for _,name in ipairs(bulk) do local have=inv.get_item_count(name); local keep=(name=='coal') and " + str(keep_coal) + " or 0; local over=have-keep;"
        "  if over>0 then local left=put(name,over); if left>0 then local c=add_chest(); if c then cs=chests(); left=put(name,left) end end;"
        "    local actually=over-left; if actually>0 then inv.remove{name=name,count=actually} end end end;"
        "local o={}; for n,c in pairs(moved) do o[#o+1]=n..'x'..c end;"
        "rcon.print('overflow: stored '..(#o>0 and table.concat(o,', ') or 'nothing')..' | chests in array='..#chests())"
    )
    return _print(lua)


def feed_smelter():
    """Keep the auto-smelting plant's ore belt (row y=14) stocked with iron ore from
    the mining chest (~17,0), and top up furnace + burner-inserter coal. Run on the
    maintain loop so the 12-furnace plant smelts continuously. (A physical belt from
    the mine would make it fully self-running; this is the software feed until then.)"""
    lua = (
        "/sc local p=storage.derpface; local s=game.surfaces['nauvis']; local inv=p.get_main_inventory();"
        # feed both stacks: (ore, chest_pos, ore-belt area, plant area)
        "local stacks={"
        "  {'iron-ore',{17.5,0.5},{{-3,-28},{23,-27}},{{-3,-33},{23,-28}}},"
        "  {'copper-ore',{1.5,6.5},{{-3,-40},{23,-39}},{{-3,-45},{23,-40}}}"
        "};"
        "local put=0; local fueled=0;"
        "for _,st in ipairs(stacks) do local ore=st[1];"
        "  local mc=s.find_entities_filtered{position=st[2],radius=2,name={'iron-chest','wooden-chest','steel-chest'}}[1];"
        "  if mc then local mci=mc.get_inventory(defines.inventory.chest);"
        "    for _,b in pairs(s.find_entities_filtered{area=st[3],name='transport-belt'}) do"
        "      for _,tl in ipairs({1,2}) do local line=b.get_transport_line(tl);"
        "        if line.get_item_count()<2 and mci.get_item_count(ore)>0 then if line.insert_at_back({name=ore,count=1}) then mci.remove{name=ore,count=1}; put=put+1 end end end end end;"
        "  for _,e in pairs(s.find_entities_filtered{area=st[4],name={'stone-furnace','burner-inserter'}}) do local fi=e.get_fuel_inventory(); if fi and fi.get_item_count('coal')<3 then local c=math.min(5,inv.get_item_count('coal')); if c>0 then e.insert{name='coal',count=c}; inv.remove{name='coal',count=c}; fueled=fueled+1 end end end end;"
        "rcon.print('feed_smelter: +'..put..' ore to belts, fueled '..fueled..' burners (both stacks)')"
    )
    return _print(lua)


def fill_ore_chests(target=1200):
    """Keep Seth's two smelter-feed storage chests topped up from the mining chests.
    Iron: mine chest (17.5,0.5) -> iron storage chest (-1.5,-25.5). Copper: mine
    chest (1.5,6.5) -> copper storage chest (-1.5,-37.5). Each storage chest has a
    loader inserter dropping onto its stack's distribution belt. Draining the mine
    chests also unblocks the drills (they sit at status 36 once their chest fills).
    Run on the maintain loop. A physical belt mine->storage would make it offline-proof."""
    lua = (
        "/sc local s=game.surfaces['nauvis']; local T=" + str(int(target)) + ";"
        "local pairs_={{'iron-ore',{17.5,0.5},{-1.5,-25.5}},{'copper-ore',{1.5,6.5},{-1.5,-37.5}}};"
        "local moved={};"
        "for _,pp in ipairs(pairs_) do local ore=pp[1];"
        "  local mc=s.find_entities_filtered{position=pp[2],radius=1,type='container'}[1];"
        "  local sc=s.find_entities_filtered{position=pp[3],radius=1,type='container'}[1];"
        "  if mc and sc then local have=sc.get_inventory(1).get_item_count(ore);"
        "    local want=T-have; if want>0 then local avail=mc.get_inventory(1).get_item_count(ore);"
        "      local n=math.min(want,avail); if n>0 then local ins=sc.insert{name=ore,count=n}; mc.remove_item({name=ore,count=ins}); moved[#moved+1]=ore..' +'..ins end end end end;"
        "rcon.print('fill_ore_chests: '..(#moved>0 and table.concat(moved,', ') or 'both chests full'))"
    )
    return _print(lua)


def science_factory():
    """Drive the Phase-1 green-science sub-factory at the (-20,-13) plot.
    4 assemblers in a row at y=-9.5: cable(-18.5) -> circuit(-14.5) -> inserter(-10.5)
    -> green(-6.5). Each has south input chests (y=-6.5) and a north output chest
    (y=-12.5). This moves intermediates along the chain, refills raw inputs from the
    smelter furnace outputs + cluster gear assembler, and feeds finished green packs
    into the lab so the research queue keeps progressing. Run on the maintain loop.
    (Belt input is topped from player inventory; a dedicated belt+gear assembler would
    make it fully self-contained - Phase-1 TODO.)"""
    lua = (
        "/sc local s=game.surfaces['nauvis']; local p=storage.derpface; local inv=p.get_main_inventory();"
        "local function cc(x,y) return s.find_entities_filtered{position={x,y},radius=0.5,type='container'}[1] end;"
        "local function move(fx,fy,tx,ty,item,cap) local a=cc(fx,fy); local b=cc(tx,ty); if not(a and b) then return 0 end;"
        "  local have=a.get_inventory(1).get_item_count(item); local room=cap-b.get_inventory(1).get_item_count(item);"
        "  local n=math.min(have,room); if n>0 then local ins=b.insert{name=item,count=n}; a.get_inventory(1).remove{name=item,count=ins}; return ins end return 0 end;"
        # move intermediates along the chain
        "local m1=move(-18.5,-12.5,-13.5,-6.5,'copper-cable',100);"
        "local m2=move(-14.5,-12.5,-9.5,-6.5,'electronic-circuit',100);"
        "local m3=move(-10.5,-12.5,-5.5,-6.5,'inserter',100);"
        # green-out -> lab
        "local g=cc(-6.5,-12.5); local lab=s.find_entities_filtered{position={-1.5,-17.5},radius=1.5,name='lab'}[1]; local gl=0;"
        "if g and lab then local n=g.get_inventory(1).get_item_count('logistic-science-pack'); if n>0 then gl=lab.get_inventory(defines.inventory.lab_input).insert{name='logistic-science-pack',count=n}; g.get_inventory(1).remove{name='logistic-science-pack',count=gl} end end;"
        # refill raw: copper + iron from furnace outputs, gear from cluster gear asm, belt from player inv
        "local function fillFrom(cx,cy,item,cap,srcArea) local c=cc(cx,cy); if not c then return 0 end; local need=cap-c.get_inventory(1).get_item_count(item); if need<=0 then return 0 end;"
        "  local got=0; for _,f in pairs(s.find_entities_filtered{area=srcArea,type='furnace'}) do local o=f.get_output_inventory(); local a=o.get_item_count(item); if a>0 then local k=math.min(a,need-got); o.remove{name=item,count=k}; c.insert{name=item,count=k}; got=got+k end if got>=need then break end end return got end;"
        "local fcu=fillFrom(-18.5,-6.5,'copper-plate',180,{{-3,-46},{24,-40}});"
        "local fi=fillFrom(-15.5,-6.5,'iron-plate',150,{{-3,-33},{24,-28}})+fillFrom(-11.5,-6.5,'iron-plate',150,{{-3,-33},{24,-28}});"
        # also draw iron from the science feed chest where it overflows (feed belt outruns the cluster)
        "local ironsrc=s.find_entities_filtered{position={-12.5,-17.5},radius=1,type='container'}[1];"
        "if ironsrc then local isi=ironsrc.get_inventory(1); for _,cx in ipairs({-15.5,-11.5}) do local c=cc(cx,-6.5); if c then local need=150-c.get_inventory(1).get_item_count('iron-plate'); local k=math.min(need,isi.get_item_count('iron-plate')); if k>0 then isi.remove{name='iron-plate',count=k}; c.insert{name='iron-plate',count=k}; fi=fi+k end end end end;"
        "local gc=cc(-10.5,-6.5); local fg=0; if gc then local need=50-gc.get_inventory(1).get_item_count('iron-gear-wheel'); if need>0 then for _,a in pairs(s.find_entities_filtered{area={{-12,-19},{-8,-16}},type='assembling-machine'}) do local o=a.get_output_inventory(); local k=math.min(o.get_item_count('iron-gear-wheel'),need-fg); if k>0 then o.remove{name='iron-gear-wheel',count=k}; gc.insert{name='iron-gear-wheel',count=k}; fg=fg+k end end end end;"
        "local bc=cc(-7.5,-6.5); local fb=0; if bc then local need=50-bc.get_inventory(1).get_item_count('transport-belt'); local av=inv.get_item_count('transport-belt'); local k=math.min(need,av); if k>0 then bc.insert{name='transport-belt',count=k}; inv.remove{name='transport-belt',count=k}; fb=k end end;"
        "rcon.print('science_factory: chain('..m1..'/'..m2..'/'..m3..') green->lab='..gl..' refill cu='..fcu..' iron='..fi..' gear='..fg..' belt='..fb)"
    )
    return _print(lua)


def keep_fueled():
    """Comprehensive fueling for the maintenance patrol. (1) Keeps the coal STOCK chest
    (20.5,-1.5) itself supplied by pulling coal from any other coal-bearing chest (the
    coal mine) when it dips, so the fuel source never runs dry. (2) Tops up EVERY burner
    that needs coal: all stone furnaces (smelter stacks) to 10, boilers to 50, mining
    drills to 5, burner inserters to 3. Nothing that burns coal is left to starve."""
    lua = (
        "/sc local s=game.surfaces['nauvis'];"
        "local stock=s.find_entities_filtered{position={20.5,-1.5},radius=1.5,type='container'}[1];"
        "if not stock then rcon.print('keep_fueled: no coal stock chest at (20.5,-1.5)') return end;"
        "local si=stock.get_inventory(1);"
        # refill the stock chest from any other coal-bearing chest (e.g. the coal mine) when low
        "local pulled=0; if si.get_item_count('coal')<500 then"
        "  for _,c in pairs(s.find_entities_filtered{type='container'}) do if c~=stock then"
        "    local ci=c.get_inventory(1); local av=ci.get_item_count('coal');"
        "    if av>0 then local need=900-si.get_item_count('coal'); local k=math.min(av,need);"
        "      if k>0 then ci.remove{name='coal',count=k}; si.insert{name='coal',count=k}; pulled=pulled+k end end end;"
        "    if si.get_item_count('coal')>=900 then break end end end;"
        "local function fuel(ents,target) local n=0; for _,e in pairs(ents) do local fi=e.get_fuel_inventory(); if fi then local need=target-fi.get_item_count('coal'); if need>0 then local k=math.min(need,si.get_item_count('coal')); if k>0 then e.insert{name='coal',count=k}; si.remove{name='coal',count=k}; n=n+1 end end end end return n end;"
        "local f=fuel(s.find_entities_filtered{type='furnace'},10);"
        "local b=fuel(s.find_entities_filtered{name='boiler'},50);"
        "local d=fuel(s.find_entities_filtered{type='mining-drill'},5);"
        "local bi=fuel(s.find_entities_filtered{type='inserter'},3);"
        "rcon.print('keep_fueled: stock+='..pulled..' fueled furnaces='..f..' boilers='..b..' drills='..d..' burner-ins='..bi..' stock_coal='..si.get_item_count('coal'))"
    )
    return _print(lua)


def service_components():
    """Ensure assembler/structure COMPONENTS (not just fuel) stay stocked on the patrol:
    top up the cluster's copper input chest (-5.5,-14.5) and the iron chest (-12.5,-17.5)
    from the smelter furnace outputs, so the gear/red-science assemblers never starve.
    The green sub-factory's own inputs are handled by science_factory()."""
    lua = (
        "/sc local s=game.surfaces['nauvis'];"
        "local function topup(cx,cy,item,target,area) local c=s.find_entities_filtered{position={cx,cy},radius=1,type='container'}[1]; if not c then return 0 end;"
        "  local need=target-c.get_inventory(1).get_item_count(item); if need<=0 then return 0 end; local got=0;"
        "  for _,f in pairs(s.find_entities_filtered{area=area,type='furnace'}) do local o=f.get_output_inventory(); local a=o.get_item_count(item); if a>0 then local k=math.min(a,need-got); o.remove{name=item,count=k}; c.insert{name=item,count=k}; got=got+k end if got>=need then break end end return got end;"
        "local cu=topup(-5.5,-14.5,'copper-plate',200,{{-3,-46},{24,-40}});"
        "local fe=topup(-12.5,-17.5,'iron-plate',200,{{-3,-33},{24,-28}});"
        "rcon.print('service_components: cluster copper+='..cu..' iron+='..fe)"
    )
    return _print(lua)


def manage_inventory():
    """Keep the player inventory from clogging so queued builds ALWAYS have room
    (Seth's rule: maintain free space). Offloads excess bulk to chests: firearm-magazine
    -> ammo buffer, iron-ore/stone -> mine chest, copper/iron plate over 300 -> storage
    chests in the buffer zone (-22..-12, -38..-28). Keeps all build items + a working
    material buffer. Runs on the maintain loop."""
    lua = (
        "/sc local s=game.surfaces['nauvis']; local p=storage.derpface; local inv=p.get_main_inventory();"
        # only offload when free space is actually low, so we don't strip materials a build needs
        "if inv.count_empty_stacks()>=12 then rcon.print('manage_inventory: '..inv.count_empty_stacks()..' free (ok, no offload)') return end;"
        "local function dump(item,keep,chest) if not chest then return end; local n=inv.get_item_count(item)-keep; if n>0 then local k=chest.insert{name=item,count=n}; if k>0 then inv.remove{name=item,count=k} end end end;"
        "local ammo=s.find_entities_filtered{position={20.5,-2.5},radius=3,type='container'}[1];"
        "local mc=s.find_entities_filtered{position={17.5,0.5},radius=2,type='container'}[1];"
        "dump('firearm-magazine',0,ammo); dump('iron-ore',0,mc); dump('stone',0,mc);"
        "local function store(item,keep) local excess=inv.get_item_count(item)-keep; if excess<=0 then return end; local stored=0;"
        "  for cx=-22,-12,2 do for cy=-38,-28,2 do if stored<excess then local c=s.find_entities_filtered{position={cx+0.5,cy+0.5},radius=0.6,type='container'}[1];"
        "    if (not c) and inv.get_item_count('iron-chest')>0 and s.can_place_entity{name='iron-chest',position={cx+0.5,cy+0.5},force=p.force} then c=s.create_entity{name='iron-chest',position={cx+0.5,cy+0.5},force=p.force}; inv.remove{name='iron-chest',count=1} end;"
        "    if c then local want=excess-stored; if want>0 then local k=c.insert{name=item,count=want}; if k>0 then inv.remove{name=item,count=k}; stored=stored+k end end end end end end end;"
        "store('copper-plate',600); store('iron-plate',600);"
        "rcon.print('manage_inventory: offloaded -> '..inv.count_empty_stacks()..' free slots')"
    )
    return _print(lua)


def cleanup_infra():
    """Maintenance cleanup (Seth's standing rule: remove unneeded infra on patrol).
    Conservatively removes ONLY truly orphaned things: transport belts with no adjacent
    belt/inserter/splitter/machine/chest (stray belt stubs from abandoned builds), and
    electric poles that power no consumer AND have no other pole within wire range
    (islands in empty space). Never touches connected lines or connectivity poles."""
    lua = (
        "/sc local s=game.surfaces['nauvis']; local rb=0; local rp=0;"
        "for _,b in pairs(s.find_entities_filtered{type='transport-belt'}) do local p=b.position;"
        "  local nb=s.find_entities_filtered{area={{p.x-1.4,p.y-1.4},{p.x+1.4,p.y+1.4}}, type={'transport-belt','underground-belt','splitter','inserter','loader','loader-1x1','assembling-machine','furnace','lab','container','logistic-container','mining-drill'}};"
        "  local cnt=0; for _,e in pairs(nb) do if e~=b then cnt=cnt+1 end end;"
        "  if cnt==0 then b.destroy(); rb=rb+1 end end;"
        "for _,pole in pairs(s.find_entities_filtered{type='electric-pole'}) do local p=pole.position;"
        "  local hascons=false;"
        "  for _,e in pairs(s.find_entities_filtered{area={{p.x-2.5,p.y-2.5},{p.x+2.5,p.y+2.5}}}) do"
        "    if (e.prototype.electric_energy_source_prototype) or e.name=='steam-engine' then hascons=true break end end;"
        "  local np=0; for _,e in pairs(s.find_entities_filtered{area={{p.x-7,p.y-7},{p.x+7,p.y+7}},type='electric-pole'}) do if e~=pole then np=np+1 end end;"
        "  if (not hascons) and np==0 then pole.destroy(); rp=rp+1 end end;"
        "rcon.print('cleanup_infra: removed '..rb..' orphan belts, '..rp..' island poles')"
    )
    return _print(lua)


def collect_science():
    """Pull finished packs from the AUTOMATED producers into player inventory so
    feed_labs can spread them across ALL labs (not just the main one): green from the
    green sub-factory output chest (-6.5,-12.5), red from the cluster red assembler
    output (-5.5,-17.5, leaving a couple for the cluster's own lab inserter)."""
    lua = (
        "/sc local s=game.surfaces['nauvis']; local p=storage.derpface; local inv=p.get_main_inventory();"
        "local g=0; local gc=s.find_entities_filtered{position={-6.5,-12.5},radius=0.6,type='container'}[1];"
        "if gc then local n=gc.get_inventory(1).get_item_count('logistic-science-pack'); if n>0 then gc.get_inventory(1).remove{name='logistic-science-pack',count=n}; inv.insert{name='logistic-science-pack',count=n}; g=n end end;"
        "local r=0; local ra=s.find_entities_filtered{position={-5.5,-17.5},radius=1.2,type='assembling-machine'}[1];"
        "if ra then local o=ra.get_output_inventory(); local n=o.get_item_count('automation-science-pack')-2; if n>0 then o.remove{name='automation-science-pack',count=n}; inv.insert{name='automation-science-pack',count=n}; r=n end end;"
        "rcon.print('collect_science: green+'..g..' red+'..r)"
    )
    return _print(lua)


def feed_labs(target=10):
    """Top up EVERY lab to `target` of BOTH red and green packs from player inventory,
    so all labs in the array keep working (not just whichever got fed first). Reports
    how many labs are actually working + research %. Part of every maintenance patrol."""
    lua = (
        "/sc local s=game.surfaces['nauvis']; local p=storage.derpface; local inv=p.get_main_inventory(); local T=" + str(int(target)) + ";"
        "local ri=0; local gi=0; local labs=s.find_entities_filtered{name='lab'}; local work=0;"
        "for _,l in pairs(labs) do local li=l.get_inventory(defines.inventory.lab_input);"
        "  local cr=math.min(T-li.get_item_count('automation-science-pack'),inv.get_item_count('automation-science-pack')); if cr>0 then local k=li.insert{name='automation-science-pack',count=cr}; inv.remove{name='automation-science-pack',count=k}; ri=ri+k end;"
        "  local cg=math.min(T-li.get_item_count('logistic-science-pack'),inv.get_item_count('logistic-science-pack')); if cg>0 then local k=li.insert{name='logistic-science-pack',count=cg}; inv.remove{name='logistic-science-pack',count=k}; gi=gi+k end;"
        "  if l.status==1 then work=work+1 end end;"
        "local f=game.forces.player;"
        "rcon.print('feed_labs: red+='..ri..' green+='..gi..' labs_working='..work..'/'..#labs..' inv(r='..inv.get_item_count('automation-science-pack')..',g='..inv.get_item_count('logistic-science-pack')..') | '..(f.current_research and f.current_research.name or 'NONE')..' '..string.format('%.1f',f.research_progress*100)..'%')"
    )
    return _print(lua)


def produce_ammo():
    """One ammo-production cycle: collect smelted iron from the furnace row, reload
    the furnaces from the mining chest, craft magazines from available iron, and
    stock the ammo buffer chest. Run repeatedly (e.g. on the maintain loop) to
    drive turret ammo back to full after it drops below 50%."""
    lua = (
        "/sc local p=storage.derpface; local s=game.surfaces['nauvis']; local inv=p.get_main_inventory();"
        "local got=0;"
        "for _,fur in pairs(s.find_entities_filtered{area={{-3,-33},{23,-28}},name='stone-furnace'}) do"
        "  local o=fur.get_output_inventory(); local c=o.get_item_count('iron-plate'); if c>0 then got=got+c; inv.insert{name='iron-plate',count=c}; o.remove{name='iron-plate',count=c} end end;"
        "local mc=s.find_entities_filtered{position={17.5,0.5},radius=2,name='iron-chest'}[1];"
        "if mc then local mci=mc.get_inventory(defines.inventory.chest);"
        "  for _,fur in pairs(s.find_entities_filtered{area={{-3,-33},{23,-28}},name='stone-furnace'}) do"
        "    if fur.get_fuel_inventory().get_item_count('coal')<4 then local cc=math.min(8,inv.get_item_count('coal')); if cc>0 then fur.insert{name='coal',count=cc}; inv.remove{name='coal',count=cc} end end;"
        "    local take=math.min(20,mci.get_item_count('iron-ore')); if take>0 then local ins=fur.insert{name='iron-ore',count=take}; mci.remove{name='iron-ore',count=ins} end end end;"
        "local iron=inv.get_item_count('iron-plate'); local make=math.floor((iron-8)/4); if make>0 then p.begin_crafting{recipe='firearm-magazine',count=make} end;"
        "local buf=s.find_entities_filtered{position={20.5,-2.5},radius=2,name='wooden-chest'}[1];"
        "local mags=inv.get_item_count('firearm-magazine'); if buf and mags>0 then buf.get_inventory(defines.inventory.chest).insert{name='firearm-magazine',count=mags}; inv.remove{name='firearm-magazine',count=mags} end;"
        "rcon.print('ammo cycle: +'..got..' iron collected, crafting '..(make>0 and make or 0)..' mags, '..mags..' to buffer')"
    )
    return _print(lua)


def turrets_low():
    """Return True if any gun turret is below 50% ammo (50/100 magazines)."""
    lua = (
        "/sc local s=game.surfaces['nauvis']; local low=0; local n=0;"
        "for _,t in pairs(s.find_entities_filtered{name='gun-turret'}) do n=n+1;"
        "  local ai=t.get_inventory(defines.inventory.turret_ammo); if ai and ai.get_item_count('firearm-magazine')<50 then low=low+1 end end;"
        "rcon.print(low..'/'..n)"
    )
    out = _print(lua).strip()
    try:
        low = int(out.split("/")[0])
        return low > 0, out
    except Exception:
        return False, out


def maintain():
    """Unified periodic maintenance loop body. Runs the whole resilience system:
    pickup ground items, refill turrets, and if any turret is <50% drive ammo
    production; then defend_check (rebuild/repair after an attack)."""
    log = [pickup().strip(), fill_ore_chests().strip(), science_factory().strip(),
           service_components().strip(), keep_fueled().strip(),
           collect_science().strip(), feed_labs().strip(),
           manage_inventory().strip(), cleanup_infra().strip()]
    low, ratio = turrets_low()
    log.append(refill_turrets().strip())
    if low:
        log.append(f"turrets low ({ratio}) -> " + produce_ammo().strip())
    log.append(defend_check().strip())
    return "\n".join(log)


def storage_inventory(ox=-20, oy=-36):
    """Report the consolidated contents of the overflow chest array - so stored
    materials can be found and reused. The chests themselves are the memory."""
    lua = (
        "/sc local s=game.surfaces['nauvis'];"
        "local cs=s.find_entities_filtered{area={{" + str(ox) + "," + str(oy) + "},{" + str(ox+12) + "," + str(oy+10) + "}}, name={'wooden-chest','iron-chest','steel-chest'}};"
        "local tot={};"
        "for _,c in pairs(cs) do for _,it in pairs(c.get_inventory(defines.inventory.chest).get_contents()) do tot[it.name]=(tot[it.name] or 0)+it.count end end;"
        "local o={}; for n,c in pairs(tot) do o[#o+1]=n..'='..c end; table.sort(o);"
        "rcon.print('storage ('..#cs..' chests): '..(#o>0 and table.concat(o,' ') or 'empty'))"
    )
    return _print(lua)


def retrieve(item, count, ox=-20, oy=-36):
    """Pull up to `count` of `item` from the overflow chest array into the player
    inventory. Use when a craft/build needs materials that were stored."""
    lua = (
        "/sc local p=storage.derpface; local s=game.surfaces['nauvis']; local inv=p.get_main_inventory();"
        "local need=" + str(int(count)) + "; local got=0;"
        "for _,c in pairs(s.find_entities_filtered{area={{" + str(ox) + "," + str(oy) + "},{" + str(ox+12) + "," + str(oy+10) + "}}, name={'wooden-chest','iron-chest','steel-chest'}}) do"
        "  if got>=need then break end; local ci=c.get_inventory(defines.inventory.chest); local avail=ci.get_item_count('" + item + "');"
        "  local take=math.min(avail, need-got); if take>0 then local ins=inv.insert{name='" + item + "',count=take}; ci.remove{name='" + item + "',count=ins}; got=got+ins end end;"
        "rcon.print('retrieved '..got..' " + item + " (have '..inv.get_item_count('" + item + "')..' in inventory now)')"
    )
    return _print(lua)


def fortify(cx, cy, count=16, radius=13, detect_range=90, ammo_each=10, base=6, per_nest=4):
    """Adaptive, nest-scaled turret ring around (cx,cy). Counts biter nests within
    detect_range and sizes the ring to the threat: target = base + per_nest*nests,
    capped at `count`. With nests present it weights ~half the turrets into the arc
    facing the NEAREST nest (rest spread behind); with NO nest in range it places a
    light EVEN ring (`base`). Salvages+replaces existing turrets, so it re-runs on
    the maintain loop to reconfigure as nests spawn/grow/die. Caps at turrets on hand."""
    lua = (
        "/sc local p=storage.derpface; local s=game.surfaces['nauvis']; local inv=p.get_main_inventory();"
        "local cx=" + str(cx) + "; local cy=" + str(cy) + "; local cmax=" + str(int(count)) + "; local R=" + str(radius) + "; local det=" + str(detect_range) + "; local pi=math.pi;"
        "local nest=nil; local bd=det*det; local nnests=0; for _,e in pairs(s.find_entities_filtered{type='unit-spawner',force='enemy'}) do local d=(e.position.x-cx)^2+(e.position.y-cy)^2; if d<det*det then nnests=nnests+1 end; if d<bd then bd=d; nest=e end end;"
        "local count=math.max(" + str(base) + ", math.min(cmax, " + str(base) + "+" + str(per_nest) + "*nnests));"
        # salvage existing turrets in the area back to inventory
        "for _,t in pairs(s.find_entities_filtered{position={cx,cy},radius=R+10,name='gun-turret'}) do local ai=t.get_inventory(defines.inventory.turret_ammo); if ai then local m=ai.get_item_count('firearm-magazine'); if m>0 then inv.insert{name='firearm-magazine',count=m} end end; inv.insert{name='gun-turret',count=1}; t.destroy() end;"
        "local pos={};"
        "if nest then local a0=math.atan2(nest.position.y-cy, nest.position.x-cx); local front=math.ceil(count*0.5); local rear=count-front;"
        "  for i=0,front-1 do local a=a0+(-0.85+1.7*(front>1 and i/(front-1) or 0)); pos[#pos+1]={cx+R*math.cos(a), cy+R*math.sin(a)} end;"
        "  for i=0,rear-1 do local a=a0+pi+(-1.3+2.6*(rear>1 and i/(rear-1) or 0)); pos[#pos+1]={cx+R*math.cos(a), cy+R*math.sin(a)} end;"
        "else for i=0,count-1 do local a=2*pi*i/count; pos[#pos+1]={cx+R*math.cos(a), cy+R*math.sin(a)} end end;"
        "local placed=0; for _,pp in ipairs(pos) do if inv.get_item_count('gun-turret')>0 then local np=s.find_non_colliding_position('gun-turret', pp, 6, 1); if np then local t=s.create_entity{name='gun-turret',position=np,force=p.force}; if t then inv.remove{name='gun-turret',count=1}; placed=placed+1; local mag=math.min(" + str(ammo_each) + ", inv.get_item_count('firearm-magazine')); if mag>0 then t.insert{name='firearm-magazine',count=mag}; inv.remove{name='firearm-magazine',count=mag} end end end end end;"
        "rcon.print('fortify ('..cx..','..cy..'): '..placed..'/'..count..' turrets ('..nnests..' nests in range); '..(nest and ('nearest@('..math.floor(nest.position.x)..','..math.floor(nest.position.y)..') -> WEIGHTED') or 'no nest -> EVEN')..'; turrets in hand='..inv.get_item_count('gun-turret'))"
    )
    return _print(lua)


def auto_repair():
    """Repair damaged (not destroyed) player structures using repair-packs from the
    inventory. Part of the post-attack sequence. No-op if no repair-packs yet."""
    lua = (
        "/sc local p=storage.derpface; local s=game.surfaces['nauvis']; local inv=p.get_main_inventory();"
        "local packs=inv.get_item_count('repair-pack'); local damaged=0; local repaired=0;"
        "for _,e in pairs(s.find_entities_filtered{force='player'}) do"
        "  if e.name~='character' and e.health and e.max_health and e.health<e.max_health then damaged=damaged+1;"
        "    if packs>0 then local use=math.min(math.ceil((e.max_health-e.health)/200),packs);"
        "      e.health=e.max_health; inv.remove{name='repair-pack',count=use}; packs=packs-use; repaired=repaired+1 end end end;"
        "rcon.print('repair: '..damaged..' damaged, '..repaired..' repaired ('..packs..' packs left)')"
    )
    return _print(lua)


def defend_check(base_x=10, base_y=-12, radius=70):
    """Attack-detection + full post-attack maintenance. Counts enemies near the base.
    While enemies are present -> 'UNDER ATTACK' (turrets handle it; don't repair
    mid-fight). When clear -> rebuild destroyed, repair damaged, refill turrets."""
    lua = (
        "/sc local s=game.surfaces['nauvis'];"
        "local enemies=s.count_entities_filtered{position={" + str(base_x) + "," + str(base_y) + "},radius=" + str(radius) + ",force='enemy'};"
        "rcon.print(enemies)"
    )
    n = _print(lua).strip()
    try:
        n = int(n)
    except ValueError:
        return f"defend_check error: {n}"
    if n > 0:
        # keep turrets fed during the fight, but hold repairs until it's over
        refill_turrets()
        return f"UNDER ATTACK: {n} enemies within {radius} of base ({base_x},{base_y})"
    # clear -> repair damage + keep turrets stocked. Do NOT rebuild() every cycle: that
    # restores the whole snapshot and reverts any intentional change (e.g. an optimized
    # pole layout). rebuild() is a manual op, run explicitly after real destruction.
    parts = [auto_repair(), refill_turrets()]
    return "clear (0 enemies) | " + " | ".join(p.strip() for p in parts)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "pos":
        print(pos())
    elif cmd == "walk":
        x, y = float(sys.argv[2]), float(sys.argv[3])
        print(walk(x, y))
    elif cmd == "mine":
        print(mine(sys.argv[2], int(sys.argv[3])))
    elif cmd == "build":
        d = int(sys.argv[5]) if len(sys.argv) > 5 else 0
        print(build(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), d))
    elif cmd == "craft":
        print(craft(sys.argv[2], int(sys.argv[3])))
    elif cmd == "place":
        d = int(sys.argv[5]) if len(sys.argv) > 5 else 0
        print(place(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), d))
    elif cmd == "snapshot":
        print(snapshot())
    elif cmd == "rebuild":
        print(rebuild())
    elif cmd == "defend-check":
        print(defend_check())
    elif cmd == "notepad":
        print(notepad(sys.argv[2:]))
    elif cmd == "build-ghosts":
        print(build_ghosts())
    elif cmd == "announce":
        print(announce(" ".join(sys.argv[2:])))
    elif cmd == "pickup":
        print(pickup(int(sys.argv[2]) if len(sys.argv) > 2 else 12))
    elif cmd == "refill-turrets":
        print(refill_turrets())
    elif cmd == "auto-repair":
        print(auto_repair())
    elif cmd == "fortify":
        cnt = int(sys.argv[4]) if len(sys.argv) > 4 else 10
        print(fortify(float(sys.argv[2]), float(sys.argv[3]), cnt))
    elif cmd == "feed-smelter":
        print(feed_smelter())
    elif cmd == "store-overflow":
        print(store_overflow())
    elif cmd == "storage-inventory":
        print(storage_inventory())
    elif cmd == "retrieve":
        print(retrieve(sys.argv[2], int(sys.argv[3])))
    elif cmd == "goto-mine":
        name, n = sys.argv[2], int(sys.argv[3])
        # find nearest patch, walk to it, mine
        out = _print(
            "/sc local p=storage.derpface; local es=p.surface.find_entities_filtered{position=p.position,radius=400,name='"
            + name + "'}; local best,bd=nil,1e18; for _,e in pairs(es) do local d=(e.position.x-p.position.x)^2+(e.position.y-p.position.y)^2; if d<bd then bd=d; best=e end end; if best then rcon.print(best.position.x..','..best.position.y) else rcon.print('none') end"
        ).strip()
        if out == "none":
            print("no", name, "found"); sys.exit(1)
        tx, ty = map(float, out.split(","))
        print("walking to", (tx, ty)); print(walk(tx, ty))
        print(mine(name, n))
    else:
        print(__doc__); sys.exit(2)
