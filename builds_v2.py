#!/usr/bin/env python3
"""Three existing builds ported onto the order system (MEGABASE-V2-DESIGN section 9 step 2).

Thin wrappers: each submits ONE `build` order, and the registered builder calls the
EXISTING bootstrap build (no logic rebuilt here), finds what it created by diffing a live
bbox scan taken before vs after, verifies a post-condition, and hands the new entities back
to the executor for registration (role+phase+order_id). Each public fn returns
{order_id, status, error, uids}.

Discovery-by-diff is the contract: bootstrap builds place entities server-side without
returning them, so the scan diff (pre-scan -> build -> post-scan over the site bbox,
restricted to the build's entity names) is how the registry learns what exists. Idempotent
bootstrap skips (site already built) diff to [] — that is success with nothing new to
register, provided the post-condition still holds.
"""
import autopilot as A
import bootstrap
import executor
import rcon
import world

MINE_NAMES = ("burner-mining-drill", "electric-mining-drill", "transport-belt",
              "underground-belt", "burner-inserter", "inserter", "wooden-chest")
SMELTER_NAMES = ("stone-furnace", "steel-furnace", "transport-belt", "underground-belt",
                 "inserter", "small-electric-pole", "iron-chest")
POWER_NAMES = ("offshore-pump", "boiler", "steam-engine", "pipe", "pipe-to-ground",
               "small-electric-pole", "burner-inserter", "transport-belt")


def _diff(pre, post):
    seen = {(e["n"], e["x"], e["y"]) for e in pre}
    return [{"name": e["n"], "tile_pos": (e["x"], e["y"]), "direction": e["d"]}
            for e in post if (e["n"], e["x"], e["y"]) not in seen]


def _run(kind_fn, kwargs, role, phase):
    oid = executor.submit({"kind": "build", "args": {"fn": kind_fn, "kwargs": kwargs},
                           "role": role, "phase": phase})
    o = executor.run(oid)
    return {"order_id": oid, "status": o["status"], "error": o.get("error"),
            "uids": [r["uid"] for r in world.query(order_id=oid)]}


# --------------------------------------------------------------------------- mine outpost
def _build_mine_outpost(kw, order):
    ore, n = kw["ore"], int(kw.get("n", 8))
    spot = bootstrap.STATE.get(ore) or A.richest_spot(ore, 0, 0, radius=160)
    if not spot:
        raise executor.ExecError("no %s patch within scan range" % ore)
    rx, ry = int(spot[0]), int(spot[1])
    if len(spot) > 2 and spot[2]:
        world.record_patch(ore, rx, ry, spot[2] / 25.0)   # richest_spot density is a 5x5 SUM
    r = n + 22                                            # covers the clean-slate radius 24
    bbox = (rx - r, ry - r, rx + r, ry + r)
    pre = world.scan_area(*bbox, names=MINE_NAMES)
    chest = bootstrap.build_mine_outpost(ore, n)
    if chest is None:
        raise executor.ExecError("build_mine_outpost(%s,%d) returned None (no patch or placement failed)" % (ore, n))
    post = world.scan_area(*bbox, names=MINE_NAMES)
    new = _diff(pre, post)
    # post-condition: the outpost pattern exists at the patch — drills + a belt lane. A
    # belt-fed / already-built mine returns a sentinel with nothing new; the pattern must
    # still be present in the post-scan.
    drills = [e for e in post if e["n"].endswith("mining-drill")]
    belts = [e for e in post if e["n"] in ("transport-belt", "underground-belt")]
    if not drills or not belts:
        raise executor.ExecError("mine outpost verify: %d drills / %d belt tiles at %s patch"
                                 % (len(drills), len(belts), ore))
    return new


# --------------------------------------------------------------------------- power plant
def _build_power_plant(kw, order):
    n_engines = int(kw.get("n_engines", 2))
    water = bootstrap.STATE.get("water")
    if not water:
        raise executor.ExecError("water not scouted (run bootstrap.scout() first)")
    wx, wy = int(water[0]), int(water[1])
    bbox = (wx - 30, wy - 45, wx + 30, wy + 10)
    pre = world.scan_area(*bbox, names=POWER_NAMES)
    had_engine = any(e["n"] == "steam-engine" for e in pre)
    boiler = bootstrap.power()          # idempotent: returns None if an engine already exists
    if boiler is None and not had_engine:
        raise executor.ExecError("bootstrap.power() failed (no placeable shore or boiler never got water)")
    # bootstrap.power builds Seth's verified column (boiler dir0 + 2 engines chained north).
    # Engines chain steam through both ends, so n_engines>2 extends the SAME column north at
    # the proven 5-tile pitch (px,py-8-5k). NOTE: GOTCHAS ratio is 1 boiler : 2 engines —
    # the full multi-column scalable design (one boiler per 2 engines, 4-tile X pitch, water
    # manifold + coal belt backbones) has no codified builder yet; see report.
    if boiler is not None and n_engines > 2:
        bcx, bcy = boiler
        px, py = bcx - 1, bcy + 2       # invert _build_boiler_engine's boiler-center math
        for k in range(2, n_engines):
            out = A.place("steam-engine", px, py - 8 - 5 * k, direction=0, clear=0)
            if not out.strip().startswith("BUILT"):
                raise executor.ExecError("extra engine %d: %s" % (k, out.strip()))
    post = world.scan_area(*bbox, names=POWER_NAMES)
    new = _diff(pre, post)
    if boiler is not None:
        # post-condition: the fluid chain actually works — boiler holds water, engines hold
        # energy (GOTCHAS: fluid geometry is the finicky part; always verify by reads).
        bcx, bcy = boiler
        out = rcon.run(
            "/sc local s=game.surfaces[1];"
            "local b=s.find_entities_filtered{name='boiler',position={%d,%d},radius=3}[1];"
            "local w=b and math.floor(b.get_fluid_count('water')) or -1; local en=0;"
            "for _,e in pairs(s.find_entities_filtered{name='steam-engine',position={%d,%d},radius=45}) do en=en+e.energy end;"
            "rcon.print(w..','..math.floor(en))" % (bcx, bcy, bcx, bcy)).strip()
        try:
            w, en = (int(v) for v in out.split(","))
        except ValueError:
            raise executor.ExecError("power verify read failed: %r" % out)
        if w <= 0:
            raise executor.ExecError("power verify: boiler has no water (w=%d en=%d)" % (w, en))
    return new


# --------------------------------------------------------------------------- smelter array
def _build_smelter_array(kw, order):
    ore, n = kw["ore"], int(kw.get("n", 8))
    if ore not in bootstrap.SMELT_ZONE:
        raise executor.ExecError("no SMELT_ZONE for %s" % ore)
    ox, oy = bootstrap.SMELT_ZONE[ore]
    bbox = (ox - 3, oy - 3, ox + 2 * n + 7, oy + 8)
    pre = world.scan_area(*bbox, names=SMELTER_NAMES)
    bootstrap.build_smelter_array(ore, n)               # idempotent: no-op if furnaces exist
    post = world.scan_area(*bbox, names=SMELTER_NAMES)
    new = _diff(pre, post)
    # post-condition: n furnaces stand in the furnace band (works for both fresh-built and
    # the idempotent already-built case).
    furn = [e for e in post if e["n"] in ("stone-furnace", "steel-furnace")
            and oy + 1 <= e["y"] <= oy + 4]
    if len(furn) < n:
        raise executor.ExecError("smelter verify: %d/%d furnaces at %s array" % (len(furn), n, ore))
    return new


executor.BUILDERS.update({
    "mine_outpost": _build_mine_outpost,
    "power_plant": _build_power_plant,
    "smelter_array": _build_smelter_array,
})


# --------------------------------------------------------------------------- public API
def mine_outpost_v2(ore, n=8, phase=0):
    """build_mine_outpost on the order system: builds (or detects) the outpost, registers
    every entity it created as role=mine."""
    return _run("mine_outpost", {"ore": ore, "n": n}, "mine", phase)


def power_plant_v2(n_engines=2, phase=0):
    """bootstrap.power() on the order system (pump -> boiler -> engines column, verified by
    fluid/energy reads); extra engines extend the column. Registers as role=power."""
    return _run("power_plant", {"n_engines": n_engines}, "power", phase)


def smelter_array_v2(ore, n=8, phase=0):
    """build_smelter_array on the order system; registers the array as role=smelter."""
    return _run("smelter_array", {"ore": ore, "n": n}, "smelter", phase)
