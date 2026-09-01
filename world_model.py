"""What is ACTUALLY on the base, derived from the map every pass. No caches, no records.

WHY THIS EXISTS
---------------
The self-heals were never broken. They were aiming at a base that had moved.

`lanes.json` recorded `iron-ore: (31,-40) west to (-4,-40)` - a lane to a smelter row that
had been demolished - while the new base sat at x=38..108. So `lane_stalled` fired every
twenty seconds, triage routed to `fix_lanes`, and fix_lanes diligently repaired a lane
pointing at bare dirt, forever. Elsewhere: "no coal mine recorded", "no electric consumers in
the smelting block yet", and a protected-tile set that had credited our own demolition to the
operator so the builder refused to rebuild on it.

Every one of those is the same bug: a CACHE of what the builder once believed, consulted as
if it were the world. The operator's instruction (2026-08-31): "the self heal should be
dynamic so changing the base doesn't break things - derpface should know about any
infrastructure that is currently on the base and be autonomously optimizing, adding to and
maintaining that infrastructure."

THE RULE
--------
Structure is DERIVED, never remembered. A mine is wherever drills are standing now. A
smelting block is wherever machines cluster now. A lane is a run of belt that exists now, and
where it goes is where its belts point. If the operator moves something, the next census
simply sees it somewhere else, and nothing has to be told.

The technique is the one that already works twice in this repo: ask the entities what they
are doing rather than reading a layout. `feed_planner.chain_graph` finds a lab grid's head
from inserter pick/drop pairs; `array_io.classify` tells an input belt from an output belt the
same way. This generalises that to the whole base.
"""
import collections

import array_io


# --------------------------------------------------------------------------- clustering
def cluster(points, gap=8):
    """Group points into connected clusters, joining any two within `gap` on both axes.

    This is what makes "a mine" a derived fact: six drills standing near each other ARE an
    outpost, and there is nothing to record or keep in sync. Deterministic ordering so the
    same map always yields the same clusters - a self-heal whose targets shuffle every pass
    is worse than none.
    """
    pts = sorted(set(map(tuple, points)))
    seen, out = set(), []
    for p in pts:
        if p in seen:
            continue
        stack, group = [p], []
        seen.add(p)
        while stack:
            q = stack.pop()
            group.append(q)
            for r in pts:
                if r not in seen and abs(r[0] - q[0]) <= gap and abs(r[1] - q[1]) <= gap:
                    seen.add(r)
                    stack.append(r)
        out.append(sorted(group))
    return sorted(out)


def bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


# --------------------------------------------------------------------------- census
def mines(drills):
    """[{'ore', 'drills', 'bbox', 'drops'}] - one per cluster of drills, from the map.

    `drills` is a list of {'x','y','ore','drop'}. The ore is whatever the drills are standing
    on, so a patch running out and the outpost moving needs no bookkeeping anywhere.
    """
    by_ore = collections.defaultdict(list)
    for d in drills:
        by_ore[d.get("ore") or "unknown"].append(d)
    out = []
    for ore, ds in sorted(by_ore.items()):
        for group in cluster([(d["x"], d["y"]) for d in ds]):
            g = set(group)
            members = [d for d in ds if (d["x"], d["y"]) in g]
            out.append({"ore": ore, "drills": len(members), "bbox": bbox(group),
                        "drops": sorted({tuple(d["drop"]) for d in members if d.get("drop")})})
    return out


def blocks(machines, inserters):
    """[{'kind','count','bbox','io'}] - clusters of machines plus their derived belt I/O.

    A "smelting block" is not a coordinate someone wrote down; it is wherever furnaces are
    standing, and its input and output rows are whichever belts its own inserters lift from
    and drop onto.
    """
    by_kind = collections.defaultdict(list)
    for m in machines:
        by_kind[m.get("kind") or "machine"].append(m)
    out = []
    for kind, ms in sorted(by_kind.items()):
        for group in cluster([(m["x"], m["y"]) for m in ms], gap=6):
            bx = bbox(group)
            # A machine occupies a FOOTPRINT, not a centre row: a 2x2 furnace centred on y
            # spans y-1..y, a 3x3 assembler y-1..y+1, and its inserters drop onto that edge
            # row rather than the centre. Classifying against centres alone loses every
            # inserter that feeds the far side - which reads as "this block has no input",
            # the exact false negative that would send a fixer off to build a lane that is
            # already there.
            rows = set()
            for (_, y) in group:
                rows.update((y - 1, y, y + 1))
            near = [(i["pick"][1], i["drop"][1]) for i in inserters
                    if bx[0] - 3 <= i["pick"][0] <= bx[2] + 3
                    and bx[1] - 3 <= i["pick"][1] <= bx[3] + 3]
            out.append({"kind": kind, "count": len(group), "bbox": bx,
                        "io": array_io.classify(near, rows)})
    return out


# --------------------------------------------------------------------------- the gaps
def unfed_blocks(blocks_, lane_ends, belt_dirs=None, belt_tiles=()):
    """Blocks whose input rows nothing delivers to - the work that actually needs doing.

    THE SIDE MATTERS. An input row that flows west is fed at its EAST end; a lane ending at
    its west end is that belt running out, not a delivery. The first version of this counted
    any lane end near the row and so declared both smelting blocks fed, when in truth their
    only "lane ends" were their own input belts terminating at the far side.

    `belt_dirs` maps an input row to the directions of the belts on it (from array_io); with
    it, the feed side is derived. Without it we fall back to "a lane ends anywhere along the
    row", which is the older, laxer test - stated plainly so a caller knows what it bought.
    """
    ends = set(map(tuple, lane_ends))
    feeds = set(map(tuple, belt_tiles))
    out = []
    for b in blocks_:
        x1, _, x2, _ = b["bbox"]
        for row in b["io"].get("input", []):
            side = array_io.feed_end((belt_dirs or {}).get(row, [])) if belt_dirs else None
            # ASK WHETHER SOMETHING ARRIVES, NOT WHETHER A LANE STOPS NEARBY. A connected
            # lane has no terminus at all - it runs into the block - so testing for a lane
            # END marked the working iron lane (58 plates/min at the time) as unfed, which
            # would have sent a fixer to rebuild a belt that was already delivering.
            # A row is fed when a belt sits on the tile just past its feed end.
            if side == "east":
                ok = (x2 + 1, row) in feeds or any(
                    abs(ey - row) <= 1 and ex > x2 for (ex, ey) in ends)
            elif side == "west":
                ok = (x1 - 1, row) in feeds or any(
                    abs(ey - row) <= 1 and ex < x1 for (ex, ey) in ends)
            else:
                ok = any(abs(ey - row) <= 1 and x1 - 4 <= ex <= x2 + 4 for (ex, ey) in ends)
            if not ok:
                out.append({"block": b, "input_row": row, "feed_side": side})
    return out


def orphan_producers(blocks_):
    """Blocks that have an output row but no consumer downstream is a separate question;
    here we simply report blocks with outputs, so a caller can pair them with sinks."""
    return [b for b in blocks_ if b["io"].get("output")]


def summary(census):
    parts = []
    for m in census.get("mines", []):
        parts.append("%s mine: %d drills at %s" % (m["ore"], m["drills"], m["bbox"]))
    for b in census.get("blocks", []):
        io = b["io"]
        parts.append("%s block: %d machines at %s in=%s out=%s"
                     % (b["kind"], b["count"], b["bbox"], io.get("input"), io.get("output")))
    for u in census.get("unfed", []):
        parts.append("UNFED: %s block input row y=%d has no lane delivering to its %s end"
                     % (u["block"]["kind"], u["input_row"], u.get("feed_side") or "feed"))
    return "\n".join(parts) or "nothing on the base yet"


# --------------------------------------------------------------------------- live census
def census(A):
    """One read of the map -> the whole structure of the base. No files consulted."""
    raw = A._print(
        "/sc local s=game.surfaces[1] local o={} "
        "for _,d in pairs(s.find_entities_filtered{type='mining-drill'}) do "
        "  local r=s.find_entities_filtered{position=d.position,radius=2,type='resource'}[1] "
        "  o[#o+1]='D|'..math.floor(d.position.x)..'|'..math.floor(d.position.y)..'|'"
        "    ..(r and r.name or 'unknown')..'|'..math.floor(d.drop_position.x)..'|'"
        "    ..math.floor(d.drop_position.y) end "
        "for _,m in pairs(s.find_entities_filtered{type={'furnace','assembling-machine'}}) do "
        "  o[#o+1]='M|'..math.floor(m.position.x)..'|'..math.floor(m.position.y)..'|'..m.type end "
        "for _,i in pairs(s.find_entities_filtered{type='inserter'}) do "
        "  o[#o+1]='I|'..math.floor(i.pickup_position.x)..'|'..math.floor(i.pickup_position.y)"
        "    ..'|'..math.floor(i.drop_position.x)..'|'..math.floor(i.drop_position.y) end "
        "rcon.print(table.concat(o,';'))").strip()
    drills, machines, inserters = [], [], []
    for tok in raw.split(";"):
        f = tok.split("|")
        if f[0] == "D" and len(f) == 6:
            drills.append({"x": int(f[1]), "y": int(f[2]), "ore": f[3],
                           "drop": (int(f[4]), int(f[5]))})
        elif f[0] == "M" and len(f) == 4:
            machines.append({"x": int(f[1]), "y": int(f[2]), "kind": f[3]})
        elif f[0] == "I" and len(f) == 5:
            inserters.append({"pick": (int(f[1]), int(f[2])), "drop": (int(f[3]), int(f[4]))})
    ends = lane_ends(A)
    bl = blocks(machines, inserters)
    dirs, tiles = belt_rows(A)
    return {"mines": mines(drills), "blocks": bl, "lane_ends": ends,
            "belt_dirs": dirs, "unfed": unfed_blocks(bl, ends, dirs, tiles)}


def belt_rows(A):
    """({row -> [directions]}, {(x,y) of every belt}) - the side, and what actually arrives."""
    raw = A._print(
        "/sc local s=game.surfaces[1] local o={} "
        "for _,b in pairs(s.find_entities_filtered{type='transport-belt'}) do "
        "  o[#o+1]=math.floor(b.position.x)..'|'..math.floor(b.position.y)..'|'..b.direction end "
        "rcon.print(table.concat(o,';'))").strip()
    out, tiles = {}, set()
    for tok in raw.split(";"):
        f = tok.split("|")
        if len(f) == 3:
            out.setdefault(int(f[1]), []).append(int(f[2]))
            tiles.add((int(f[0]), int(f[1])))
    return out, tiles


def lane_ends(A):
    """Tiles where a belt run stops - its downstream tile is bare ground."""
    raw = A._print(
        "/sc local s=game.surfaces[1] local o={} "
        "local D={[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}} "
        "for _,b in pairs(s.find_entities_filtered{type='transport-belt'}) do "
        "  local v=D[b.direction] "
        "  if v then local nx=math.floor(b.position.x)+v[1] "
        "    local ny=math.floor(b.position.y)+v[2] "
        "    local a=s.find_entities_filtered{area={{nx,ny},{nx+0.99,ny+0.99}}} "
        "    local solid=false "
        "    for _,e in pairs(a) do if e.name~='character' then solid=true end end "
        "    if not solid then o[#o+1]=math.floor(b.position.x)..'|'..math.floor(b.position.y) end "
        "  end end "
        "rcon.print(table.concat(o,';'))").strip()
    out = []
    for tok in raw.split(";"):
        f = tok.split("|")
        if len(f) == 2:
            out.append((int(f[0]), int(f[1])))
    return out
