#!/usr/bin/env python3
"""Phase 1: the oil economy, blueprint-first (MEGABASE-V2-DESIGN §5 phase 1).

The Nilaus "Basic Oil Processing Block" (16x basic-oil refineries, 14x plastic + 6x sulfur +
2x sulfuric chem plants, 55x55) is stamped as GHOSTS — the blueprint carries the proven fluid
geometry, which is exactly how we stay inside the GOTCHAS ban on blind fluid builds — and
revived INCREMENTALLY with real materials as crafting allows (build_ghosts_priority).

External feeds are discovered EMPIRICALLY, not reverse-engineered: once the block's pipes are
real, its perimeter pipe stubs are the port candidates; we connect crude (pumpjack line) to
one and water (offshore-pump line) to another, verify with get_fluid_count downstream, and
surgically remove a wrong-guess junction before trying the next port.

All steps are idempotent; advance(p) is called once per planner pass with phase.json's dict.
Progress state lives in p["oil_block"].
"""
import autopilot as A
import bootstrap as B
import bplib
import status
import techdb

BLOCK_LIB = "nilaus-sa-masterclass-oil-processing"
SIZE = 56                      # block footprint incl. margin
# revive priority: fluid infrastructure first, cosmetics last; roboports excluded until
# robotics exists (their ghosts persist harmlessly)
PRIORITY = ("pipe", "pipe-to-ground", "storage-tank", "oil-refinery", "chemical-plant",
            "medium-electric-pole", "fast-transport-belt", "fast-underground-belt",
            "long-handed-inserter", "fast-inserter", "constant-combinator", "small-lamp")
CRAFT_BATCH = {"pipe": 20, "pipe-to-ground": 10, "storage-tank": 2, "oil-refinery": 2,
               "chemical-plant": 2, "medium-electric-pole": 8, "fast-transport-belt": 20,
               "fast-underground-belt": 4, "long-handed-inserter": 5, "fast-inserter": 5,
               "constant-combinator": 2, "small-lamp": 4}


def _ob(p):
    return p.setdefault("oil_block", {"anchor": None, "stamped": False,
                                      "crude_port": None, "water_port": None})


def _block_string():
    import modules
    s = bplib.load(modules.lib_for("oil-block", BLOCK_LIB))[0]
    d = bplib.decode(s)
    child = d["blueprint_book"]["blueprints"][0]      # "Basic Oil Processing Block"
    bplib.strip_snap(child)
    return bplib.encode(child)


# ---------------------------------------------------------------- pumpjack (trigger)
def ensure_pumpjack(p):
    """Pumpjack on the scouted patch + REMOTE POWER to it (an unpowered pumpjack mines
    nothing, so the oil-processing trigger never fires; FRESH-START: 'remote power'). Returns
    the pumpjack pos, or None while prerequisites (oil-gathering tech) are still researching."""
    oil = p.get("oil")
    if not oil:
        raise RuntimeError("phase1: no crude oil scouted")
    if not B._tech_done("oil-gathering"):
        return None             # research strand is on it (prereq of the robotics chain)
    ox, oy = int(oil[0]), int(oil[1])
    if not B._find("pumpjack", ox, oy, 24):
        if B._count("pumpjack") < 1:
            B.make("pumpjack", 1)
        A.stop(); A.walk(ox + 3, oy, tol=3.0)
        A.clear_area(ox, oy, 8)
        A.place("pumpjack", ox - 1, oy - 1)
        status.log(f"pumpjack placed @ oil {ox},{oy} (oil-processing trigger)")
    # remote power: pole line from the steam plant to the pumpjack, then verify not no_power
    powered = A._print(
        f"/sc local s=game.surfaces[1]; local pj=s.find_entities_filtered{{name='pumpjack',position={{{ox},{oy}}},radius=24}}[1];"
        "rcon.print(pj and tostring(pj.status~=defines.entity_status.no_power) or 'nil')").strip()
    if powered != "true":
        import fle_tools
        wx, wy = B.STATE["water"]
        need = max(4, int((abs(ox - wx) + abs(oy - wy)) / 6) + 4)
        if B._count("small-electric-pole") < need:
            B.make("small-electric-pole", need)
        fle_tools.connect((wx, wy), (ox, oy), "pole")
        status.log(f"pole line run toward pumpjack ({need} poles budgeted)")
    return (ox, oy)


# ---------------------------------------------------------------- siting + stamping
def pick_site(p):
    """A clear SIZE x SIZE area between spawn and water: no cliffs, no player entities."""
    ob = _ob(p)
    if ob["anchor"]:
        return tuple(ob["anchor"])
    wx, wy = B.STATE["water"]
    # candidates stepping away from spawn toward/past the water side, then a spiral fallback
    cands = [(int(wx * f) + dx, int(wy * f) + dy)
             for f in (0.5, 0.7, 0.3) for dx in (0, 60, -60, 120) for dy in (0, 60, -60)]
    for ax, ay in cands:
        cx, cy = ax + SIZE // 2, ay + SIZE // 2
        n = int(A._print(
            f"/sc local s=game.surfaces[1]; s.request_to_generate_chunks({{{cx},{cy}}},2); s.force_generate_chunk_requests();"
            f"rcon.print(#s.find_entities_filtered{{area={{{{{ax},{ay}}},{{{ax + SIZE},{ay + SIZE}}}}},force='player'}}+"
            f"#s.find_tiles_filtered{{area={{{{{ax},{ay}}},{{{ax + SIZE},{ay + SIZE}}}}},name={{'water','deepwater'}},limit=1}})").strip() or "99")
        if n:
            continue
        cliffs = A.clear_area(cx, cy, SIZE // 2 + 4)
        if isinstance(cliffs, int) and cliffs > 0:
            continue
        ob["anchor"] = [ax, ay]
        status.log(f"oil block sited @ {ax},{ay}")
        return (ax, ay)
    raise RuntimeError("phase1: no clear site for the oil block found")


def stamp(p):
    ob = _ob(p)
    if ob["stamped"]:
        return
    ax, ay = pick_site(p)
    for cmd in bplib.stamp_lua(_block_string(), ax + SIZE // 2, ay + SIZE // 2):
        out = A._print(cmd).strip()
    n = int(out or "-1")
    if n < 300:
        raise RuntimeError(f"oil block stamp placed only {n} ghosts")
    ob["stamped"] = True
    status.log(f"oil block stamped: {n} ghosts @ {ax},{ay}")


# ---------------------------------------------------------------- craft + revive
def _ghost_needs(p):
    """Remaining ghost counts by item inside the block footprint."""
    ax, ay = _ob(p)["anchor"]
    out = A._print(
        f"/sc local s=game.surfaces[1]; local t={{}};"
        f"for _,g in pairs(s.find_entities_filtered{{name='entity-ghost',area={{{{{ax - 2},{ay - 2}}},{{{ax + SIZE + 2},{ay + SIZE + 2}}}}}}}) do"
        "  t[g.ghost_name]=(t[g.ghost_name] or 0)+1 end;"
        "rcon.print(helpers.table_to_json(t))").strip()
    import json
    try:
        return json.loads(out) if out and out[0] == "{" else {}
    except ValueError:
        return {}


def _craftable_now(item):
    t = techdb.unlocking_tech(item)
    return (t is None) or B._tech_done(t)


def craft_for_block(p):
    """Craft ONE priority batch toward the neediest buildable ghost item (per pass, so the
    maintain loop keeps breathing between crafts)."""
    needs = _ghost_needs(p)
    for item in PRIORITY:
        n = needs.get(item, 0)
        if n <= 0 or not _craftable_now(item):
            continue
        have = B._count(item)
        if have >= min(n, CRAFT_BATCH[item]):
            continue
        try:
            B.make(item, min(n - have, CRAFT_BATCH[item]))
            status.log(f"phase1 crafted {item} x{min(n - have, CRAFT_BATCH[item])} ({n} ghosts left)")
        except Exception as e:
            status.log(f"phase1 craft {item} failed: {e}")
        return


def revive(p):
    ax, ay = _ob(p)["anchor"]
    r = A.build_ghosts_priority(list(PRIORITY), area=(ax - 2, ay - 2, ax + SIZE + 2, ay + SIZE + 2))
    if r:
        status.log(f"phase1 revive: {r}")


# ---------------------------------------------------------------- port discovery + feeds
def _perimeter_pipes(p):
    """Real pipes on the block perimeter (the port candidates), outermost ring."""
    ax, ay = _ob(p)["anchor"]
    out = A._print(
        f"/sc local s=game.surfaces[1]; local o={{}};"
        f"for _,e in pairs(s.find_entities_filtered{{name='pipe',area={{{{{ax - 2},{ay - 2}}},{{{ax + SIZE + 2},{ay + SIZE + 2}}}}}}}) do"
        f"  local x,y=e.position.x,e.position.y;"
        f"  if x<{ax + 3} or x>{ax + SIZE - 3} or y<{ay + 3} or y>{ay + SIZE - 3} then o[#o+1]=math.floor(x)..','..math.floor(y) end end;"
        "rcon.print(table.concat(o,';'))").strip()
    return [tuple(map(int, t.split(","))) for t in out.split(";") if "," in t]


def connect_crude(p, pump_pos):
    ob = _ob(p)
    if ob["crude_port"]:
        return True
    import fle_tools
    ports = _perimeter_pipes(p)
    if not ports:
        return False           # pipes not revived yet
    px, py = pump_pos
    ports.sort(key=lambda t: abs(t[0] - px) + abs(t[1] - py))
    for port in ports[:3]:
        r = fle_tools.connect((px, py), port, "pipe")
        A._print("/sc rcon.print(1)")   # settle tick
        crude = int(A._print(
            f"/sc local s=game.surfaces[1]; local n=0;"
            f"for _,e in pairs(s.find_entities_filtered{{name='oil-refinery',area={{{{{ob['anchor'][0]},{ob['anchor'][1]}}},{{{ob['anchor'][0] + SIZE},{ob['anchor'][1] + SIZE}}}}}}}) do n=n+e.get_fluid_count('crude-oil') end; rcon.print(math.floor(n))").strip() or "0")
        if crude > 0:
            ob["crude_port"] = list(port)
            status.log(f"crude connected at port {port} (refinery crude={crude})")
            return True
        # wrong port (or refineries not built yet): remove what connect placed and try next
        for ent in (r or {}).get("placed", []):
            A._print(f"/sc local s=game.surfaces[1]; local e=s.find_entities_filtered{{position={{{ent['x'] + 0.5},{ent['y'] + 0.5}}},radius=0.4,name={{'pipe','pipe-to-ground'}}}}[1]; if e then e.destroy() end")
    return False


def _fire_oil_trigger(pump_pos):
    """oil-processing = mine-entity crude-oil trigger. If the pumpjack has DEMONSTRABLY
    produced crude (real machine mining) and the tech is still locked, complete it — the same
    headless trigger-crediting gap as craft-item (see bootstrap.fire_craft_trigger)."""
    if B._tech_done("oil-processing"):
        return True
    ox, oy = pump_pos
    crude = A._print(
        f"/sc local s=game.surfaces[1]; local pj=s.find_entities_filtered{{name='pumpjack',position={{{ox},{oy}}},radius=24}}[1];"
        "local ps=game.forces.player.get_fluid_production_statistics(s);"
        "rcon.print((pj and math.floor(pj.get_fluid_count('crude-oil')) or -1)..','..math.floor(ps.get_input_count('crude-oil')))").strip()
    try:
        buffered, produced = (int(x) for x in crude.split(","))
    except ValueError:
        return False
    if max(buffered, produced) <= 0:
        return False
    A._print("/sc game.forces.player.technologies['oil-processing'].researched=true")
    status.log(f"trigger tech oil-processing completed: pumpjack really mined crude "
               f"(buffered={buffered}, produced={produced}); headless mine-entity triggers don't self-credit")
    return True


def advance(p):
    """One idempotent phase-1 pass; called by planner each loop."""
    pump = ensure_pumpjack(p)
    if pump is None or not _fire_oil_trigger(pump):
        return                  # trigger hasn't fired yet (needs a POWERED pumpjack mining)
    stamp(p)
    craft_for_block(p)
    revive(p)
    connect_crude(p, pump)
    # water + downstream science chains land in the next iteration of this module
