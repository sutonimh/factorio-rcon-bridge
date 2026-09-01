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
def unfed_blocks(blocks_, lane_ends=(), belt_dirs=None, belt_tiles=(), starved=None):
    """Blocks that are actually starving, and which input row a lane should therefore reach.

    STARVATION IS OBSERVED, NOT INFERRED FROM GEOMETRY. Three attempts at deriving "is this
    row fed?" from belt layout each needed another special case: a connected lane has no
    terminus, so testing for a lane end marked a working lane as unfed; then a print's input
    belt overhangs its machine cluster, so testing past the machine bbox let a block's OWN
    belt count as its supply and hid forty starving furnaces behind a clean census. Belt
    geometry cannot cheaply tell an arriving lane from the print's own tail.

    The machines already know. A furnace with no ore reports it, unambiguously, whatever the
    belts look like. So STATUS answers "is this block fed" and STRUCTURE answers "where would
    the lane go" - each used for what it can actually settle.

    `starved` maps a machine tile to True when the game reports it short of ingredients.
    """
    starved = starved or {}
    out = []
    for b in blocks_:
        x1, y1, x2, y2 = b["bbox"]
        hungry = sum(1 for (mx, my), v in starved.items()
                     if v and x1 - 1 <= mx <= x2 + 1 and y1 - 1 <= my <= y2 + 1)
        if not hungry:
            continue
        for row in b["io"].get("input", []):
            side = array_io.feed_end((belt_dirs or {}).get(row, [])) if belt_dirs else None
            out.append({"block": b, "input_row": row, "feed_side": side, "starved": hungry})
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
        parts.append("STARVED: %s block (%d machines short) wants a lane to the %s end of "
                     "input row y=%d"
                     % (u["block"]["kind"], u.get("starved", 0),
                        u.get("feed_side") or "feed", u["input_row"]))
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
    hungry = starved_machines(A)
    return {"mines": mines(drills), "blocks": bl, "lane_ends": ends, "belt_dirs": dirs,
            "starved": hungry, "unfed": unfed_blocks(bl, ends, dirs, tiles, hungry)}


def starved_machines(A):
    """{(x,y): True} for machines the GAME reports short of ingredients. The unambiguous
    answer to "is this block fed", which no amount of belt geometry gives cheaply."""
    raw = A._print(
        "/sc local s=game.surfaces[1] local o={} "
        "local S={[defines.entity_status.no_ingredients]=true,"
        "[defines.entity_status.item_ingredient_shortage]=true} "
        "for _,m in pairs(s.find_entities_filtered{type={'furnace','assembling-machine'}}) do "
        "  if S[m.status] then "
        "    o[#o+1]=math.floor(m.position.x)..'|'..math.floor(m.position.y) end end "
        "rcon.print(table.concat(o,';'))").strip()
    out = {}
    for tok in raw.split(";"):
        f = tok.split("|")
        if len(f) == 2:
            out[(int(f[0]), int(f[1]))] = True
    return out


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
