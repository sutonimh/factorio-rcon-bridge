#!/usr/bin/env python3
"""Belt-lane semantics + honest supply verification. ALL RCON READS — NEVER WRITES.

STANDING INVARIANT (every neighbouring module in this repo mutates; this one must not):
the ONLY state this module touches is `storage._lanelint`, a JSON *string* — the exact
world.py:152 chunked-read precedent, on a PRIVATE key because world.scan_area shares
storage._world and the autopilot writes it concurrently. No create_entity / destroy / rotate /
remove_item / walking_state appears in any lua string here, and no runtime event handler is
ever registered (GOTCHAS 2026-08-30: script.on_event from /sc locks human players out of the
server with "mod event handlers are not identical"). test_lane_lint guards this with a regex.

WHY THIS EXISTS
  bootstrap._lane_connected (a BFS with a "best distance <= 6" fudge) plus lane_moves_ore
  (an item count over a hardcoded rectangle) reported SUCCESS all night on a lane that moved
  nothing. Neither knows about lanes, flow direction, junctions or consumers. The four
  failures they missed, and the finding that catches each:
    a "connected" lane moving no ore ......... verify_supply(moving=False) / STARVED
    two segments on adjacent rows ............ DEAD_END (+ the orphan run as evidence)
    a mid-lane inserter draining to a chest .. DRAIN
    iron+copper merged onto one column ....... MIXED_ITEMS
    a run whose belts disagree about flow .... DIRECTION_SPLIT

PORTED FROM
  chebykinn/factorio-planning-agent mod/planning-agent/control.lua
    :1311-1368 TOOLS.trace_belt  — the two-directional walk ("exactly one successor, and it
      is a belt" — a splitter/merge is a TERMINATOR, never something to guess through), the
      reverse(upstream)+start+downstream splice, and the per-entity lane aggregation over
      get_max_transport_line_index() with i==1 -> left, i==2 -> right.
    :1270-1295 "functional reservations" — the padded-bbox inserter/mining-drill sweep with
      pcall around every *_position (the owning entity sits OUTSIDE the run's bbox).
    :1373-1405 TOOLS.trace_entity — an inserter is an INPUT if its drop_position lands in the
      target and an OUTPUT if its pickup_position does, with what_at() resolving the far end.
      Inverted here (the belt is the thing being tapped) to give feeders/tappers.
  daemon/lanes.ts
    :30-42  DIRV over the 16-way enum + floor|floor tile keys.
    :51-66  side-load rule: perpendicular entry fills the lane on the side it enters from.
    :67-79  inserter-drop rule — the IDEA is ported, the heuristic is NOT: lanes.ts guesses
      "far lane" from the inserter's own side; we read the engine's own drop_position, which
      is authoritative for inserters AND drills (they differ — see the calibration below).
      Both rules then collapse into ONE predicate, lane_at().
    :84-118 straight-run chain ids -> per-lane contention. Our trace already IS the chain, so
      the intent is kept and the code dropped.
    :133-156 worldLaneWarnings — a junction into a lane upstream already occupies will block.
  lua/fle_lib.lua:230-243 F.ug_partner — the geometric underground hop.

2.1.17 COMPATIBILITY (re-verified live on this server, tick ~992048-999773, reads only)
  1. LuaEntity.neighbours is GONE: "LuaEntity doesn't contain key neighbours." Any upstream
     port that hops undergrounds through .neighbours is dead code here.
  2. belt_neighbours omits the underground partner in BOTH directions, and has no key for it:
     input @(-10.5,11.5) d=4 -> keys=[inputs,outputs] nin=1 nout=0
     output@( -8.5,11.5) d=4 -> keys=[inputs,outputs] nin=0 nout=1
     So the geometric hop is MANDATORY downstream (input->output) and upstream (output->input).
     It exists twice on purpose — in lua to REACH the far side (so it lands in the dump) and
     in python to ORDER it. Both derive from fle_lib.lua:230-243; keep them in step.
  3. get_contents() returns a LIST of {name,count,quality}, transport lines included
     (live: name=coal count=4). Never iterate it as a name->count map.
  4. belt_to_ground_type errors on non-undergrounds — gate on type or pcall.
  5. get_max_transport_line_index(): transport-belt 2, underground-belt 4, splitter 8 — all
     three verified live. Loaders and linked-belts still have none on this map, so their
     counts are assumed. NOTE the odd->left / even->right mapping below is verified only for
     transport-belt (i==1/2, upstream's own rule) and is an APPROXIMATION for the 4- and
     8-line entities; a splitter's 8 lines span two tiles and two sides.
  6. Directions are the 16-way enum; live belts read 0/4/8/12 only.
  7. max_underground_distance comes from the prototype (underground-belt=5 verified;
     fast=7). fle_lib.F.underground_range's hardcoded 4 is wrong for fast/express — it only
     feeds lay_line, so it is out of scope here, but flagging it.
  8. get_detailed_contents() is the movement oracle: {stack.name, position, unique_id}.
     Two samples of uid+position are TRUE flow proof; both upstreams only ever compare counts,
     so a backed-up belt reads "working" to them. Live proof of why that matters, one belt at
     (-38.5,15.5) sampled 143 ticks apart: lane 1 held the same four uids at the same four
     positions (frozen solid) while lane 2 gained a new uid — count-based checks call that
     belt healthy; per-lane uid tracking calls lane 1 dead.
  9. Lane geometry is CALIBRATED, not assumed (three live fixtures, all agreeing with
     lane_at + left_normal):
       drill @(14.5,-42.5) d=8 drop=(14.500,-40.652) -> belt@(14.5,-40.5) d=12 : L2 held 1
       drill @(-42.5,13.5) d=8 drop=(-42.500,15.348) -> belt@(-42.5,15.5) d=4  : left/north
       insrt @( -5.5,4.5)  d=8 drop=( -5.500,3.301)  -> belt@( -5.5, 3.5) d=4  : left/north
     Note the offsets differ (a drill drops ~0.15 short of centre onto the NEAR lane; an
     inserter reaches ~0.20 past centre onto the FAR lane) — which is exactly why the offset
     must be READ and never inferred from which side the entity sits on.
 10. RCON commands stay small (two compact /sc gathers), and the payload comes back through
     the chunked storage read — a full trace far exceeds one response.
 11. Splitters sit on a tile boundary and cover TWO tiles, so floor() alone would key only one
     of them. Verified live: an east-running splitter reads position (-28.50,16.00) — x mid-
     tile, y on the boundary — and a north-running one reads (-12.00,13.50). _tiles_of keys
     both tiles from the centre, which is why it must never be handed a floored coord.
 12. THIS SERVER IS SHARED. The entity count moved 833 -> 844 during one read sweep (the
     autopilot builds while we read), so a lane can legitimately change between two traces,
     and any storage key world.py also uses WILL be clobbered mid-read. Hence caveat 10's
     private key, and hence verify_supply samples rather than assumes.

LIVE FIXTURES worth keeping (real coordinates on the current map): underground pairs
(-10.5,11.5)/(-8.5,11.5) d=4 and (-9.5,14.5)/(-9.5,16.5) d=8; and the belt at (-9.5,15.5)
d=8 pointing into the BACK of an underground output piece — belt_neighbours.outputs={} — a
live "looks laid, is not connected" case, which is the exact class that burned us.

CLI:  python3 lane_lint.py trace X Y
      python3 lane_lint.py lint X Y [expected-item]
      python3 lane_lint.py verify ORE X1 Y1 X2 Y2
"""
import json
import math
import sys
import time

import rcon

# 2.1 16-way direction enum -> unit travel vector (screen coords: +x east, +y south).
DIRV = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
# control.lua:1305-1309 verbatim. fle_lib.F.belt_connected only knows the first three.
BELT_TYPES = ("transport-belt", "underground-belt", "splitter", "loader", "loader-1x1",
              "linked-belt")
CONTAINERS = ("container", "logistic-container", "linked-container", "infinity-container")
SEV = {"MIXED_ITEMS": 1, "SIDELOAD_CONTENTION": 2, "DEAD_END": 1, "STARVED": 2,
       "DRAIN": 1, "DIRECTION_SPLIT": 1}
SPLIT_GAP = 3          # how far a torn row's two halves may sit apart and still be ONE row
LANE_FULL = 4          # items per lane on one transport-belt tile: a full lane blocks a merge
STORE = "storage._lanelint"     # private read buffer — see _read()
CHUNK = 3000                    # chars per RCON slice (a single large response truncates)
_SC = "/sc "                    # the ONLY console prefix this module ever emits


# --------------------------------------------------------------------------- lane geometry
def left_normal(d):
    """Unit normal pointing LEFT of travel for direction d (screen coords, +y south):
    east->north, north->west, south->east, west->south."""
    vx, vy = DIRV.get(d, (0, -1))
    return (vy, -vx)


def lane_at(d, cx, cy, px, py):
    """Which transport line a drop/entry at (px,py) lands on for a belt travelling d whose
    TILE CENTRE is (cx,cy): 1 = left of travel, 2 = right. Calibrated against three live
    fixtures (module docstring item 9) — the engine's own drop_position decides, so the same
    predicate serves inserters, drills and side-loads (lanes.ts needed two heuristics)."""
    nx, ny = left_normal(d)
    return 1 if (px - cx) * nx + (py - cy) * ny > 0 else 2


def _tk(x, y):
    return (int(math.floor(x)), int(math.floor(y)))


def _tiles_of(cx, cy):
    """Tile keys an entity CENTRE covers. A 1x1 belt centre (n+0.5) yields exactly one tile; a
    splitter sits on a tile boundary (position.x on .0) so floor() would pick only one of its
    two tiles — key it under both (caveat 11). Must be fed the live centre, never an
    already-floored tile coord: doing that invented phantom keys one tile up-left of every
    belt and produced three bogus side-loads on the live (-11,11) run."""
    xs = {math.floor(cx), math.floor(cx - 0.5)}
    ys = {math.floor(cy), math.floor(cy - 0.5)}
    return {(int(a), int(b)) for a in xs for b in ys}


def _lst(v):
    """helpers.table_to_json emits an EMPTY lua table as {} (an object), not []."""
    return v if isinstance(v, list) else []


def _centre(t):
    """Tile centre of a run tile — used for lane geometry so a splitter's boundary-centred
    position doesn't skew the normal."""
    return (t["x"] + 0.5, t["y"] + 0.5)


# --------------------------------------------------------------------------- lua gathers
def _lua_component(x, y, limit, contents):
    """Flood the belt component from (x,y) over belt_neighbours PLUS the geometric
    underground hop (caveat 2 — without it the far side never lands in the dump), and emit
    compact single-letter keys into the private storage key. READ ONLY."""
    return (
        "local s=game.surfaces[1];"
        "local BT={" + ",".join("['%s']=1" % t for t in BELT_TYPES) + "};"
        "local st;for _,e in pairs(s.find_entities_filtered{position={%f,%f},radius=0.7}) do"
        % (x + 0.5, y + 0.5) +
        " if BT[e.type] then st=e break end end;"
        "if not st then %s='' rcon.print(0) return end;" % STORE +
        ""
        # fle_lib.lua:230-243 F.ug_partner, inlined (see caveat 2: this geometry lives twice)
        "local function hop(b) local d=b.direction;"
        " local vx=(d==4 and 1) or (d==12 and -1) or 0;"
        " local vy=(d==8 and 1) or (d==0 and -1) or 0;"
        " if b.belt_to_ground_type=='output' then vx,vy=-vx,-vy end;"
        " local md=prototypes.entity[b.name].max_underground_distance or 5;"
        " local w=(b.belt_to_ground_type=='input') and 'output' or 'input';"
        " for k=1,md do local e=s.find_entity(b.name,{b.position.x+vx*k,b.position.y+vy*k});"
        "  if e and e.belt_to_ground_type==w and e.direction==d then return e end end end;"
        "local seen,q,out={},{st},{};seen[st.unit_number]=true;local n=0;"
        "while #q>0 and n<%d do local b=table.remove(q);n=n+1;" % limit +
        " local nb=b.belt_neighbours or {};local ii,oo={},{};"
        " for _,e in pairs(nb.inputs or {}) do ii[#ii+1]=e.unit_number;"
        "  if not seen[e.unit_number] then seen[e.unit_number]=true;q[#q+1]=e end end;"
        " for _,e in pairs(nb.outputs or {}) do oo[#oo+1]=e.unit_number;"
        "  if not seen[e.unit_number] then seen[e.unit_number]=true;q[#q+1]=e end end;"
        " local r={n=b.name,t=b.type,d=b.direction,u=b.unit_number,"
        "  x=b.position.x,y=b.position.y,i=ii,o=oo};"
        " if b.type=='underground-belt' then r.g=b.belt_to_ground_type;"
        "  r.m=prototypes.entity[b.name].max_underground_distance or 5;"
        "  local p=hop(b); if p then r.h=p.unit_number;"
        "   if not seen[p.unit_number] then seen[p.unit_number]=true;q[#q+1]=p end end end;"
        + ("" if not contents else
           " local L,D={},{};"
           " pcall(function() for li=1,b.get_max_transport_line_index() do"
           "  local tl=b.get_transport_line(li);"
           "  for _,c in pairs(tl.get_contents()) do L[#L+1]={l=li,n=c.name,c=c.count} end;"
           "  local dc=tl.get_detailed_contents();"
           "  for k=1,math.min(#dc,8) do"
           "   D[#D+1]={l=li,n=dc[k].stack.name,p=dc[k].position,u=dc[k].unique_id} end"
           " end end); r.L=L;r.D=D;") +
        " out[#out+1]=r end;"
        "%s=helpers.table_to_json({s=st.unit_number,N=out});rcon.print(#%s)" % (STORE, STORE)
    )


def _lua_bbox(x1, y1, x2, y2):
    """Padded-bbox sweep (control.lua:1270-1295): every inserter/mining-drill with its
    pickup/drop tiles RESOLVED to the entity there (control.lua:1373-1405 what_at), plus every
    belt in the box so python can subtract the component and get the orphans. READ ONLY."""
    return (
        "local s=game.surfaces[1];"
        "local BT={" + ",".join("['%s']=1" % t for t in BELT_TYPES) + "};"
        "local A={{%d,%d},{%d,%d}};" % (x1, y1, x2, y2) +
        # what_at with BOTH of control.lua's guards restored (:1379-1395), because dropping
        # either one is wrong here. find_entities_filtered's radius is measured to the entity
        # CENTRE (verified live), so radius=0.4 resolves 1x1 targets ONLY: every inserter
        # facing a 2x2 stone-furnace came back nil, and all 12 real consumers on the live
        # (-8,17) plate row reported `to: null` — a furnace was indistinguishable from bare
        # ground. trace_entity's inside() predicate (:1379-1382) is exact at any size, so it
        # runs first over a wider sweep; the old nearest-centre rule stays as the fallback so
        # nothing that resolved before can stop resolving. The wider sweep is also why
        # `e ~= target` (:1386) has to come back: at 1.6 an inserter reaches its own drop tile.
        "local function what(p,me) local near;"
        " for _,e in pairs(s.find_entities_filtered{position=p,radius=1.6}) do"
        "  if e.unit_number~=me and e.type~='resource' and e.type~='character'"
        "   and e.type~='item-entity' then"
        "   local b=e.bounding_box;"
        "   local r={n=e.name,t=e.type,x=e.position.x,y=e.position.y,u=e.unit_number};"
        "   if p.x>=b.left_top.x and p.x<=b.right_bottom.x"
        "    and p.y>=b.left_top.y and p.y<=b.right_bottom.y then return r end;"
        "   if not near and (p.x-e.position.x)^2+(p.y-e.position.y)^2<=0.16 then near=r end"
        "  end end;"
        " return near end;"
        "local I={};"
        "for _,e in pairs(s.find_entities_filtered{area=A,type={'inserter','mining-drill'}}) do"
        " local r={n=e.name,t=e.type,d=e.direction,u=e.unit_number,x=e.position.x,y=e.position.y};"
        # every *_position in its own pcall, per control.lua:1287
        " pcall(function() local p=e.drop_position;r.dx=p.x;r.dy=p.y;r.dt=what(p,r.u) end);"
        " if e.type=='inserter' then"
        "  pcall(function() local p=e.pickup_position;r.px=p.x;r.py=p.y;r.pt=what(p,r.u) end) end;"
        " I[#I+1]=r end;"
        "local B={};"
        "for _,e in pairs(s.find_entities_filtered{area=A}) do if BT[e.type] then"
        # p = fed ACROSS its own axis, i.e. this belt is the HEAD OF A LEG rather than the
        # continuation of a line. _split needs this for ORPHANS too (the same two belts are run
        # tiles in one trace and orphans in another, and the answer must not depend on that), and
        # an orphan has no predecessor to read it off. NB: no '%' anywhere in this block - it is
        # one adjacent-literal with the table_to_json line, so a modulo would be eaten as a
        # format spec (the same trap _lua_tail documents).
        " local nb=e.belt_neighbours or {};local ax=(e.direction==4 or e.direction==12);local pp;"
        " for _,q in pairs(nb.inputs or {}) do"
        "  if ax~=(q.direction==4 or q.direction==12) then pp=1 end end;"
        " B[#B+1]={n=e.name,t=e.type,d=e.direction,u=e.unit_number,x=e.position.x,"
        "  y=e.position.y,p=pp}"
        " end end;"
        "%s=helpers.table_to_json({I=I,B=B});rcon.print(#%s)" % (STORE, STORE)
    )


def _lua_tail(tiles):
    """Lane counts + detailed contents for a handful of named tiles. READ ONLY."""
    spec = ";".join("%d,%d" % (t["x"], t["y"]) for t in tiles)
    return (
        "local s=game.surfaces[1];"
        "local BT={" + ",".join("'%s'" % t for t in BELT_TYPES) + "};local T={};"
        "for a,b in ([==[" + spec + "]==]):gmatch('(-?%d+),(-?%d+)') do"
        " local x,y=tonumber(a),tonumber(b);"
        " local e=s.find_entities_filtered{position={x+0.5,y+0.5},radius=0.7,type=BT}[1];"
        " if e then local L,D={},{};"
        "  pcall(function() for li=1,e.get_max_transport_line_index() do"
        "   local tl=e.get_transport_line(li);"
        "   for _,c in pairs(tl.get_contents()) do L[#L+1]={l=li,n=c.name,c=c.count} end;"
        "   local dc=tl.get_detailed_contents();"
        "   for k=1,math.min(#dc,8) do"
        "    D[#D+1]={l=li,n=dc[k].stack.name,p=dc[k].position,u=dc[k].unique_id} end"
        "  end end);"
        "  T[#T+1]={x=x,y=y,L=L,D=D} end end;"
        # explicit concat: the gmatch pattern above contains %d, so a trailing % on the whole
        # literal would try to format it
        + ("%s=helpers.table_to_json({T=T});rcon.print(#%s)" % (STORE, STORE))
    )


def _read(lua, tries=2):
    """world.py:152's chunked-read protocol on a PRIVATE storage key.

    NOT storage._world: world.scan_area/scan_tiles share that key and the autopilot calls them
    concurrently on this server. Observed live — a mid-read clobber truncated a payload into
    "Unterminated string starting at: line 1" on a run that had traced cleanly moments before.
    rcon.print appends a newline to EVERY response, so each slice is stripped or a control char
    lands in the JSON at each chunk boundary (GOTCHAS "RCON client protocol").
    A parse failure is retried once, then returned as a value rather than raised."""
    for attempt in range(tries):
        head = rcon.run(_SC + lua).strip()
        try:
            n = int(head or "0")
        except ValueError:
            return {"_err": head[:200]}                 # a lua error came back, not a length
        if n == 0:
            return []
        parts, i = [], 1
        while i <= n:
            parts.append(rcon.run(_SC + "rcon.print(%s:sub(%d,%d))" % (STORE, i, i + CHUNK - 1))
                         .rstrip("\r\n"))
            i += CHUNK
        try:
            return json.loads("".join(parts))
        except ValueError as e:
            if attempt + 1 >= tries:
                return {"_err": "chunked read did not parse: %s" % str(e)[:120]}
    return {"_err": "chunked read failed"}


# --------------------------------------------------------------------------- the walk
def _succ(node, nodes, field):
    """Successors of a node: belt_neighbours[field] PLUS the geometric underground partner.
    Caveat 2 — an underground 'input' has NO outputs and an 'output' has NO inputs, so the
    hop supplies the missing half in each direction. Mirrors fle_lib.lua:230-243."""
    out = [nodes[u] for u in _lst(node.get(field)) if u in nodes]
    h, g = node.get("h"), node.get("g")
    if h in nodes and ((field == "o" and g == "input") or (field == "i" and g == "output")):
        out.append(nodes[h])
    return out


def _walk(nodes, start, field, limit):
    """control.lua:1318-1331. Follow successors while EXACTLY ONE exists and it is a belt
    type and unseen; a splitter/merge stops the walk as a terminator rather than being
    guessed through. Returns (path, terminators, looped, truncated)."""
    path, seen, cur = [], {start["u"]}, start
    for _ in range(limit):
        nxt = _succ(cur, nodes, field)
        if len(nxt) != 1 or nxt[0]["t"] not in BELT_TYPES:
            return path, nxt, False, False
        if nxt[0]["u"] in seen:
            return path, [], True, False
        cur = nxt[0]
        seen.add(cur["u"])
        path.append(cur)
    return path, [], False, True


def _lanes_of(node):
    """Per-tile lane contents. control.lua:1344-1352 maps i==1->left, i==2->right; an
    underground belt has 4 lines and a splitter 8 (both verified live), so odd indices are
    taken as left and even as right. Exact for transport-belt; an approximation for the wider
    entities, whose extra lines span two tiles / two sides (caveat 5)."""
    out = {"1": {}, "2": {}}
    for c in _lst(node.get("L")):
        k = "1" if int(c["l"]) % 2 else "2"
        out[k][c["n"]] = out[k].get(c["n"], 0) + int(c["c"])
    return out


def _frame_breaks(a, b):
    """Does the transition a -> b REMAP the lanes? A straight run and a genuine curve (the
    target's only belt input is a) carry lane identity through; a perpendicular SIDE-LOAD does
    not — the engine compresses BOTH of the source's lanes onto the single lane the source
    enters from. Aggregating transport-line indices across such a junction is what made the
    live two-sided merge at (-8,17) read as coal+copper on one lane when the physical result
    is a clean coal-left / copper-right belt. (Upstream control.lua:1344 aggregates the whole
    line blind and has the same flaw.)"""
    return a["d"] != b["d"] and b["nin"] >= 2


def _segments(tiles):
    """Index ranges over which transport-line 1/2 mean the same physical lane."""
    segs, start = [], 0
    for i in range(1, len(tiles)):
        if _frame_breaks(tiles[i - 1], tiles[i]):
            segs.append([start, i - 1])
            start = i
    if tiles:
        segs.append([start, len(tiles) - 1])
    return segs


def _term(n):
    return {"name": n["n"], "type": n["t"], "x": int(math.floor(n["x"])),
            "y": int(math.floor(n["y"]))}


def trace(x, y, contents=True, limit=400, pad=3):
    """Walk a belt line end to end from any belt at tile (x,y) and describe it fully.

    Two RCON READS: the component flood, then the padded-bbox sweep for the inserters,
    drills and stray belts around it. Returns tiles ordered UPSTREAM -> DOWNSTREAM, per-tile
    and aggregated lane contents, the terminators at each end, every junction that fills a
    lane (side-loads and inserter/drill feeders), every inserter tapping the run, the belts
    inside the bbox that are NOT part of the component, and flags."""
    empty = {"start": [int(x), int(y)], "tiles": [], "lanes": {"left": {}, "right": {}},
             "upstream": [], "downstream": [], "sideloads": [], "feeders": [], "tappers": [],
             "orphans": [], "flags": {"dead_start": False, "dead_end": False, "loops": False,
                                      "truncated": False}}
    raw = _read(_lua_component(x, y, limit, contents))
    if not isinstance(raw, dict) or "N" not in raw:
        err = raw.get("_err") if isinstance(raw, dict) else "no belt at (%d,%d)" % (x, y)
        return dict(empty, error=err or "no belt at (%d,%d)" % (x, y))
    nodes = {n["u"]: n for n in _lst(raw.get("N"))}
    start = nodes.get(raw.get("s"))
    if start is None:
        return dict(empty, error="no belt at (%d,%d)" % (x, y))

    up, up_ends, lu, tu = _walk(nodes, start, "i", limit)
    down, down_ends, ld, td = _walk(nodes, start, "o", limit)
    line, uniq = [], set()                              # control.lua:1334-1337 splice
    for nd in list(reversed(up)) + [start] + down:
        # dedupe: on a CLOSED LOOP both walks cover the whole ring, so upstream's splice
        # would list every tile twice (upstream doesn't dedupe, and it hangs no rule off the
        # tile list). First-occurrence order is still the true downstream order.
        if nd["u"] not in uniq:
            uniq.add(nd["u"])
            line.append(nd)

    tiles, agg = [], {"left": {}, "right": {}}
    for nd in line:
        lanes = _lanes_of(nd)
        for k, side in (("1", "left"), ("2", "right")):
            for nm, c in lanes[k].items():
                agg[side][nm] = agg[side].get(nm, 0) + c
        tiles.append({
            "x": int(math.floor(nd["x"])), "y": int(math.floor(nd["y"])),
            "name": nd["n"], "type": nd["t"], "d": int(nd.get("d", 0)),
            "uid": nd["u"], "ug": nd.get("g"), "nin": len(_lst(nd.get("i"))), "lanes": lanes,
            "cx": nd["x"], "cy": nd["y"],               # live centre: tile keying + splitters
            "items": [{"n": it["n"], "pos": it["p"], "uid": it["u"], "line": it["l"]}
                      for it in _lst(nd.get("D"))],
        })
    segs = _segments(tiles)

    xs = [t["x"] for t in tiles] or [int(x)]
    ys = [t["y"] for t in tiles] or [int(y)]
    box = (min(xs) - pad, min(ys) - pad, max(xs) + pad + 1, max(ys) + pad + 1)
    env = _read(_lua_bbox(*box))
    env = env if isinstance(env, dict) else {}

    run_uids = {t["uid"] for t in tiles}
    by_tile = {}                                        # tile -> run tile record
    for t in tiles:
        for k in _tiles_of(t["cx"], t["cy"]):
            by_tile.setdefault(k, t)
    by_uid = {t["uid"]: t for t in tiles}
    pred = {}                                           # run tile uid -> its predecessor uid
    for a, b in zip(tiles, tiles[1:]):
        pred[b["uid"]] = a["uid"]

    # --- junctions that FILL a lane -----------------------------------------------------
    belts = _lst(env.get("B"))
    sideloads = []
    for b in belts:                                     # lanes.ts:51-66
        v = DIRV.get(int(b.get("d", 0)))
        if not v:
            continue
        tgt = by_tile.get(_tk(b["x"] + v[0], b["y"] + v[1]))
        if tgt is None or tgt["uid"] == b["u"]:
            continue
        tv = DIRV.get(tgt["d"])
        if not tv or tv[0] * v[0] + tv[1] * v[1] != 0:  # not perpendicular = not a side-load
            continue
        if pred.get(tgt["uid"]) == b["u"]:              # a CORNER inside the run, not a junction
            continue
        cx, cy = _centre(tgt)
        sideloads.append({"from": [int(math.floor(b["x"])), int(math.floor(b["y"]))],
                          "into": [tgt["x"], tgt["y"]],
                          "lane": lane_at(tgt["d"], cx, cy, b["x"], b["y"]),
                          "src_d": int(b.get("d", 0))})

    feeders, tappers = [], []
    ins = _lst(env.get("I"))
    for e in ins:                                       # lanes.ts:67-79, but engine-authoritative
        dt = e.get("dt") or {}
        tgt = by_uid.get(dt.get("u"))
        if tgt is not None and e.get("dx") is not None:
            cx, cy = _centre(tgt)
            feeders.append({"x": int(math.floor(e["x"])), "y": int(math.floor(e["y"])),
                            "via": e["n"], "type": e["t"],
                            "lane": lane_at(tgt["d"], cx, cy, e["dx"], e["dy"]),
                            "drop": [e["dx"], e["dy"]], "into": [tgt["x"], tgt["y"]]})
        pt = e.get("pt") or {}
        if e["t"] == "inserter" and pt.get("u") in run_uids:   # control.lua:1373-1405 inverted
            src = by_uid[pt["u"]]
            tappers.append({"x": int(math.floor(e["x"])), "y": int(math.floor(e["y"])),
                            "via": e["n"], "from": [src["x"], src["y"]],
                            "to": ({"name": dt["n"], "type": dt["t"], "uid": dt.get("u"),
                                    "x": int(math.floor(dt["x"])), "y": int(math.floor(dt["y"]))}
                                   if dt else None)})

    orphans = [{"x": int(math.floor(b["x"])), "y": int(math.floor(b["y"])),
                "d": int(b.get("d", 0)), "name": b["n"]}
               for b in belts if b["u"] not in run_uids]

    # belts fed ACROSS their own axis: heads of a leg, not halves of a torn row (see _split).
    # Taken from the engine so it is the SAME answer whether the belt landed on this run or in
    # its orphans; the tile-order fallback keeps hand-built traces working.
    turns = sorted({(int(math.floor(b["x"])), int(math.floor(b["y"]))) for b in belts if b.get("p")}
                   | {(t["x"], t["y"]) for a, t in zip(tiles, tiles[1:]) if a["d"] != t["d"]})

    return {"start": [int(x), int(y)], "tiles": tiles, "lanes": agg, "segments": segs,
            "turns": [list(t) for t in turns],
            "contents": bool(contents),   # False = lanes were never read; content rules must
                                          # stay silent rather than call an unread lane empty
            "upstream": [_term(n) for n in up_ends], "downstream": [_term(n) for n in down_ends],
            "sideloads": sideloads, "feeders": feeders, "tappers": tappers, "orphans": orphans,
            "pullers": [e for e in ins if (e.get("pt") or {}).get("u") is not None],
            "flags": {"dead_start": not up_ends, "dead_end": not down_ends,
                      "loops": lu or ld, "truncated": tu or td or len(nodes) >= limit}}


# --------------------------------------------------------------------------- the lint
def _f(code, x, y, detail, evidence=None):
    return {"code": code, "sev": SEV[code], "x": x, "y": y, "detail": detail,
            "evidence": evidence or {}}


def _mixed(tr, expect):
    """>=2 distinct item names aggregated on ONE lane of the run. left=ore / right=coal is
    LEGAL and must stay silent (GOTCHAS:616 — coal never shares a lane with ore)."""
    out = []
    for lo, hi in tr.get("segments") or [[0, len(tr["tiles"]) - 1]]:
        span = tr["tiles"][lo:hi + 1]
        for k, side in (("1", "left"), ("2", "right")):
            names, seen, at = set(), set(), None
            for t in span:                              # first tile introducing a 2nd name
                here = set(t["lanes"][k])
                names |= here
                if at is None and seen and here - seen:
                    at = t
                seen |= here
            if len(names) < 2:
                continue
            names = sorted(names)
            t = at or span[0]
            foreign = [n for n in names if n != expect] if expect in names else names
            out.append(_f("MIXED_ITEMS", t["x"], t["y"],
                          "%s lane carries %s on one lane" % (side, "+".join(names)),
                          {"lane": side, "items": names, "foreign": foreign, "expect": expect,
                           "segment": [span[0]["x"], span[0]["y"], span[-1]["x"], span[-1]["y"]]}))
    return out


def _contention(tr):
    """lanes.ts:106-118 perLane (two sources filling one lane of the run), then :133-156
    (a junction into a lane the tiles UPSTREAM of it already carry — the merge will block).
    The trace already gives us the upstream lane contents, so no extra fetch.

    DRILLS ARE NOT MERGES. A row of drills all dropping onto one lane is the documented mine
    layout (GOTCHAS 406-418: top drills drop from above, bottom drills from below, deliberately
    interleaved), so every healthy mine would trip both arms — verified live on the coal row at
    (-43..-38,15). Drills therefore collapse to ONE aggregate source in the perLane arm (so
    drills PLUS a real merge onto that lane still fires) and are excluded from the occupied-lane
    arm, where their backpressure is ordinary and verify_supply(moving) is the honest reading."""
    out = []
    per = {}
    js = ([dict(s, kind="side_load") for s in tr["sideloads"]] +
          [dict(f, kind=f["type"]) for f in tr["feeders"]])
    idx = {(t["x"], t["y"]): i for i, t in enumerate(tr["tiles"])}
    segs = tr.get("segments") or [[0, len(tr["tiles"]) - 1]]

    def _seg(at):                                       # lane identity resets at a frame break
        i = idx.get(tuple(at)) if at else None
        return next((k for k, (lo, hi) in enumerate(segs) if i is not None and lo <= i <= hi), -1)

    for j in js:
        per.setdefault((_seg(j.get("into")), j["lane"]), []).append(j)
    for (seg, lane), group in sorted(per.items()):
        drills = [j for j in group if j.get("kind") == "mining-drill"]
        group = [j for j in group if j.get("kind") != "mining-drill"] + drills[:1]
        if len(group) > 1:
            at = group[0].get("into") or [group[0]["x"], group[0]["y"]]
            out.append(_f("SIDELOAD_CONTENTION", at[0], at[1],
                          "%d sources fill lane %d of one run - downstream sources block"
                          % (len(group), lane),
                          {"lane": lane, "segment": seg,
                           "sources": [j.get("into") or [j["x"], j["y"]] for j in group]}))
    blocked = {}
    for j in js:
        if j.get("kind") == "mining-drill":
            continue
        at = j.get("into")
        i = idx.get(tuple(at)) if at else None
        if not i:                                       # not on the run, or the head (no upstream)
            continue
        a, b = tr["tiles"][i - 1], tr["tiles"][i]
        # SATURATION, not mere occupancy. lanes.ts ran this over a layout being DESIGNED; on a
        # live loaded belt every junction has something upstream of it, which on the plate row
        # at y=3 produced 15 findings for one run. The actionable condition is that the target
        # lane is FULL at the junction tile, so the source genuinely cannot insert — and only
        # the most upstream such junction per lane is reported: it is the blocker, the rest
        # are its consequence.
        if b["type"] != "transport-belt":               # ug/splitter line lengths differ
            continue
        if sum(b["lanes"][str(j["lane"])].values()) < LANE_FULL:
            continue
        if _frame_breaks(a, b):
            # a side-loads into b too: it occupies only the lane IT enters from, and its own
            # line indices mean nothing here. This is the live two-sided merge at (-8,17) —
            # coal enters from the north (lane 1), copper from the south (lane 2), no conflict.
            cx, cy = _centre(b)
            up = ({} if lane_at(b["d"], cx, cy, a["x"] + 0.5, a["y"] + 0.5) != j["lane"]
                  else dict(a["lanes"]["1"], **a["lanes"]["2"]))
        else:
            up = a["lanes"][str(j["lane"])]
        if not up:
            continue
        k = (_seg(at), j["lane"])
        if k not in blocked or i < blocked[k][0]:
            blocked[k] = (i, _f("SIDELOAD_CONTENTION", at[0], at[1],
                                "junction fills lane %d but it is FULL of %s from upstream"
                                % (j["lane"], ",".join(sorted(up))),
                                {"lane": j["lane"], "upstream": up,
                                 "from": j.get("from") or [j.get("x"), j.get("y")]}))
    out += [f for _, f in blocked.values()]
    return out


def _dead_end(tr):
    """A terminus WITH a consumer is legitimate and silent (GOTCHAS:806). Orphan belts within
    2 tiles of the tail are the "two segments on adjacent rows" signature - carry them as
    evidence so the caller can see the lane continues one row over."""
    if not tr["flags"]["dead_end"] or not tr["tiles"]:
        return []
    tail = tr["tiles"][-1]
    if any(t["from"] == [tail["x"], tail["y"]] for t in tr["tappers"]):
        return []
    near = [o for o in tr["orphans"]
            if abs(o["x"] - tail["x"]) <= 2 and abs(o["y"] - tail["y"]) <= 2]
    return [_f("DEAD_END", tail["x"], tail["y"],
               "run ends with no downstream belt and nothing consuming from it",
               {"orphans_near_tail": near})]


def _starved(tr):
    """Nothing on any lane, nothing feeding the head, and no producer upstream at all."""
    if not tr["tiles"] or tr["feeders"] or not tr["flags"]["dead_start"]:
        return []
    if not tr.get("contents", True):        # an unread lane is not an empty lane
        return []
    if any(t["lanes"]["1"] or t["lanes"]["2"] for t in tr["tiles"]):
        return []
    h = tr["tiles"][0]
    return [_f("STARVED", h["x"], h["y"],
               "every lane empty over %d tiles, no feeder and no upstream producer"
               % len(tr["tiles"]), {"tiles": len(tr["tiles"])})]


def _drain(tr):
    """GOTCHAS:459-473 — build_mine_outpost's terminal chest+inserter left on a THROUGH lane
    drains it into a dead end, so the belt past it stays empty while the mine looks healthy.
    A tapper into a furnace/assembler, into a belt, or on the LAST tile is legitimate."""
    if len(tr["tiles"]) < 2:
        return []
    last = (tr["tiles"][-1]["x"], tr["tiles"][-1]["y"])
    pulled = {(e.get("pt") or {}).get("u") for e in tr.get("pullers", [])}
    out = []
    for t in tr["tappers"]:
        to = t["to"]
        if not to or to["type"] not in CONTAINERS or tuple(t["from"]) == last:
            continue
        if to.get("uid") in pulled:                     # something empties it: a real buffer
            continue
        out.append(_f("DRAIN", t["from"][0], t["from"][1],
                      "%s at (%d,%d) drains a through-lane into %s with no puller"
                      % (t["via"], t["x"], t["y"], to["name"]),
                      {"inserter": [t["x"], t["y"]], "to": to}))
    return out


def _split(tr):
    """GOTCHAS:831 — half the drop row flowed west and half east, so the array got a trickle
    then nothing while a connectivity BFS still passed. A split row BREAKS the component, so
    the traced run alone can never see it: the orphans are the other half.

    DIVERGENCE ONLY. Opposed directions on one row are not automatically a fault: belts that
    flow TOWARD each other are a legitimate two-sided merge, and this map has one (live at
    x=-8: d=8 at y=16 and d=0 at y=18 both feed the side-load junction at (-8,17), which reads
    belt_neighbours.inputs=2). The fault is belts flowing APART, which tears the row in two.
    So the forward-flowing belt sitting BEYOND the backward-flowing one = split; before it =
    merge, and silent."""
    belts = [{"x": t["x"], "y": t["y"], "d": t["d"], "name": t["name"]} for t in tr["tiles"]]
    belts += tr["orphans"]
    out = []
    # A belt fed ACROSS its own axis is the HEAD OF A NEW LEG, never the severed half of a row:
    # whatever sits "behind" it on that axis was never part of it. Live false positive this
    # kills, column x=-8: (-8,10) runs north and (-8,11) runs south one tile apart, and
    # belt_neighbours shows each fed by its OWN underground output from the west (in{(-9,10)}
    # and in{(-9,11)}, both d=4) - two deliberate opposing lines, reported as a tear, and the
    # only finding on an otherwise healthy 70-tile run. GOTCHAS:831's real tear has both halves
    # continuing ALONG the row with drills feeding each, so neither half is a leg head.
    turns = {tuple(t) for t in tr.get("turns") or []}
    turns |= {(b["x"], b["y"]) for a, b in zip(tr["tiles"], tr["tiles"][1:]) if a["d"] != b["d"]}
    for axis, key, other in (("row", "y", "x"), ("column", "x", "y")):
        bwd, fwd = (12, 4) if axis == "row" else (0, 8)      # -axis flow, +axis flow
        groups = {}
        for b in belts:
            if b["d"] in (bwd, fwd):
                groups.setdefault(b[key], []).append(b)
        for k, g in sorted(groups.items()):
            # ...and only where the two halves nearly TOUCH. A torn row's halves meet (the
            # iron row broke between x=13 and x=14, with at most a small gap of missing
            # tiles); two independent lines running opposite ways far apart on the same row
            # are just two lines.
            apart = [(p[other] - q[other], p, q)
                     for p in g if p["d"] == fwd
                     for q in g if q["d"] == bwd and 0 < p[other] - q[other] <= SPLIT_GAP]
            if not apart:
                continue
            _, p, q = min(apart, key=lambda t: t[0])         # the closest diverging pair
            # test the CLOSEST pair only: it is what characterises the axis. Excluding turn
            # heads pairwise instead just lets the rule fall through to the next-nearest pair
            # and report the same divergence from one tile further away.
            if (p["x"], p["y"]) in turns or (q["x"], q["y"]) in turns:
                continue
            out.append(_f("DIRECTION_SPLIT", p["x"], p["y"],
                          "%s %s=%d flows APART: d=%d at (%d,%d) vs d=%d at (%d,%d)"
                          % (axis, key, k, p["d"], p["x"], p["y"], q["d"], q["x"], q["y"]),
                          {"axis": axis, key: k, "split_%s" % other: (p[other] + q[other]) / 2.0,
                           "belts": [[p["x"], p["y"], p["d"]], [q["x"], q["y"], q["d"]]]}))
    return out


def lint_lane(tr, expect=None):
    """Findings over a trace() result, worst first. `expect` pins declared intent (lanes.ts's
    declared-intent arm): it names the foreign item in a MIXED_ITEMS detail. Returns [] for an
    error trace rather than raising - callers lint whatever they got."""
    if not isinstance(tr, dict) or tr.get("error") or not tr.get("tiles"):
        return []
    out = _contention(tr) + _dead_end(tr) + _starved(tr) + _drain(tr) + _split(tr)
    if tr.get("contents", True):            # lane-content rules need lanes to have been read
        out += _mixed(tr, expect)
    out.sort(key=lambda f: (f["sev"], f["code"], f["x"], f["y"]))
    return out


# --------------------------------------------------------------------------- verification
def _tail_items(tiles):
    """{(item uid, line): position} over these tiles. get_detailed_contents (caveat 8) is the
    only honest movement oracle: counts alone call a frozen full belt 'working'."""
    return {(it["uid"], it["line"]): it["pos"] for t in tiles for it in t["items"]}


def _sample_tail(tiles):
    """ONE small read of these tiles' lanes + detailed contents, for verify_supply's second
    sample. Re-tracing the whole run instead cost ~60 extra RCON round trips (18s on a live
    70-tile run) and gave the run 18 more seconds to change under the comparison. Returns None
    if the read failed — the caller must then decline to claim movement, never assume it."""
    raw = _read(_lua_tail(tiles))
    if not isinstance(raw, dict) or "T" not in raw:
        return None
    return [{"x": r["x"], "y": r["y"], "lanes": _lanes_of(r),
             "items": [{"n": it["n"], "pos": it["p"], "uid": it["u"], "line": it["l"]}
                       for it in _lst(r.get("D"))]}
            for r in _lst(raw.get("T"))]


def verify_supply(ore, from_xy, to_xy, settle=3.0, tol=1):
    """The honest post-build check that bootstrap.py:838 needed:
    `_lane_connected(ore) and lane_moves_ore(ore)` is exactly the pair that reported success
    on a lane moving nothing. `connected` = to_xy (+/- tol) is genuinely ON the traced run,
    not "a BFS got within 6 tiles of it". `moving` = a second sample of the tail after
    `settle` shows an item whose unique_id advanced, or a changed id set - so a backed-up but
    ARRIVING lane reads True while a full-but-frozen or empty tail reads False. Findings come
    back alongside, so the caller gets the REASON, not just a bool."""
    tr = trace(from_xy[0], from_xy[1])
    if tr.get("error"):
        return {"connected": False, "moving": False, "arrived": 0, "findings": [],
                "path_len": 0, "trace": tr}
    connected = any(abs(t["x"] - to_xy[0]) <= tol and abs(t["y"] - to_xy[1]) <= tol
                    for t in tr["tiles"])
    tail = tr["tiles"][-4:]
    a = _tail_items(tail)
    if settle:
        time.sleep(settle)
    s2 = _sample_tail(tail)
    b = _tail_items(s2) if s2 else {}
    moving = bool(s2) and (set(a) != set(b) or any(a[k] != b[k] for k in set(a) & set(b)))
    last = (s2 or tail)[-1:]
    arrived = sum(t["lanes"][k].get(ore, 0) for t in last for k in ("1", "2"))
    return {"connected": connected, "moving": moving, "arrived": arrived,
            "findings": lint_lane(tr, expect=ore), "path_len": len(tr["tiles"]), "trace": tr}


# --------------------------------------------------------------------------- cli
def _main(argv):
    if len(argv) < 2:
        print(__doc__.rsplit("CLI:", 1)[-1].strip())
        return 2
    cmd = argv[1]
    if cmd == "trace" and len(argv) >= 4:
        print(json.dumps(trace(int(argv[2]), int(argv[3])), indent=1))
    elif cmd == "lint" and len(argv) >= 4:
        tr = trace(int(argv[2]), int(argv[3]))
        if tr.get("error"):
            print("error: %s" % tr["error"])
            return 1
        found = lint_lane(tr, expect=argv[4] if len(argv) > 4 else None)
        print("%d tiles, flags=%s" % (len(tr["tiles"]), tr["flags"]))
        for f in found:
            print("sev%d %-20s (%d,%d) %s" % (f["sev"], f["code"], f["x"], f["y"], f["detail"]))
        print("%d finding(s)" % len(found))
    elif cmd == "verify" and len(argv) >= 7:
        r = verify_supply(argv[2], (int(argv[3]), int(argv[4])), (int(argv[5]), int(argv[6])))
        r.pop("trace", None)
        print(json.dumps(r, indent=1))
    else:
        print(__doc__.rsplit("CLI:", 1)[-1].strip())
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
