#!/usr/bin/env python3
"""Offline unit tests for mine_planner_v2.py - NO live server.

Run with:
    python3 test_mine_planner_v2.py

Nothing here opens a socket. The buildplan tests get a tmp PLANS_DIR, a scripted FakeRcon in
the test_world_executor.py style, in-memory stand-ins for bootstrap's ledger/truce, and a
fake `autopilot` module injected into sys.modules, so `build()` is exercised end to end
(place -> verify -> rollback) without touching the operator's base.

The geometry expectations are the numbers measured on the operator's own copper (lane
y=-63.5 -> tile row -64) and coal (lane 15.5 -> tile row 15) outposts. The bot's iron row is
deliberately NOT a fixture: it is single-sided at pitch 2 with overlapping collision boxes,
and this module's whole job is to refuse to produce it.
"""
import math
import pathlib
import re
import shutil
import sys
import tempfile
import traceback
import types

import buildplan as B
import mine_layout as ML
import mine_planner_v2 as MP
import rcon


# --------------------------------------------------------------------------- harness
class FakeRcon:
    """Scripted rcon.run: (substring, response) steps consumed in order. A response may be a
    callable(cmd) -> str."""

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []

    def __call__(self, cmd, timeout=10.0):
        self.calls.append(cmd)
        if not self.script:
            raise AssertionError("unexpected RCON call (script exhausted): %s" % cmd[:160])
        sub, resp = self.script.pop(0)
        assert sub in cmd, "expected %r in RCON cmd, got: %s" % (sub, cmd[:200])
        return resp(cmd) if callable(resp) else resp


class Ctx:
    """tmp plans dir + fake rcon + fake bootstrap ledger + fake autopilot, all restored."""

    def __init__(self, script=(), operator=False, place=None):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="minev2-test-"))
        self._orig = (B.PLANS_DIR, B.DIRTY_PATH, rcon.run, B._protected, B._record_built,
                      B._forget_built, B._operator_present, B._default_remove, B.plan_scan,
                      B.game_tick, B.is_stale, B.probe, B._build_worked, B.absorb,
                      B._scan_tiles, dict(B.KINDS), sys.modules.get("autopilot"))
        B.PLANS_DIR = self.tmp / "plans"
        B.DIRTY_PATH = B.PLANS_DIR / "_dirty.json"
        self.fake = FakeRcon(script)
        rcon.run = self.fake
        self.protected, self.built, self.operator = set(), set(), operator
        self.removed = []
        B._protected = lambda: set(self.protected)
        B._record_built = lambda t: self.built.update(tuple(x) for x in t)
        B._forget_built = lambda t: self.built.difference_update(tuple(x) for x in t)
        B._operator_present = lambda: self.operator
        B._default_remove = self._remove
        B.plan_scan = lambda bbox: 1000
        B.game_tick = lambda: 1000
        B.is_stale = lambda plan: None
        B.probe = lambda plan, tiles: set()
        B._build_worked = lambda check, tries, delay: check()
        B.absorb = lambda plan: None
        # world.scan_tiles' shape, stubbed: {(x,y): name} of what is standing in the ground.
        # The KEY is the tile the entity's CENTRE falls in, which is what the real
        # find_entities_filtered{position=tile+0.5, radius<=0.8} lookup can actually see.
        self.world = {}
        B._scan_tiles = self._scan
        B.KINDS = {}
        self.placed_calls = []
        mod = types.ModuleType("autopilot")
        mod.place = place or self._place
        sys.modules["autopilot"] = mod

    def _place(self, name, tx, ty, direction=0, clear=10):
        self.placed_calls.append((name, tx, ty, direction))
        return "BUILT %s @(%.1f,%.1f)" % (name, tx + 0.5, ty + 0.5)

    def _remove(self, plan, tiles):
        tiles = [(int(x), int(y)) for (x, y) in tiles]
        self.removed.extend(tiles)
        return {"removed": len(tiles), "not_found": 0, "removed_tiles": tiles}

    def _scan(self, tiles, names):
        names = set(names or ())
        return [{"n": self.world[(int(x), int(y))], "x": int(x), "y": int(y), "d": 0}
                for (x, y) in tiles
                if (int(x), int(y)) in self.world and self.world[(int(x), int(y))] in names]

    def close(self):
        (B.PLANS_DIR, B.DIRTY_PATH, rcon.run, B._protected, B._record_built, B._forget_built,
         B._operator_present, B._default_remove, B.plan_scan, B.game_tick, B.is_stale,
         B.probe, B._build_worked, B.absorb, B._scan_tiles, B.KINDS, ap) = self._orig
        if ap is None:
            sys.modules.pop("autopilot", None)
        else:
            sys.modules["autopilot"] = ap
        shutil.rmtree(self.tmp, ignore_errors=True)


def _with_ctx(**ctxkw):
    def deco(fn):
        def wrapper():
            ctx = Ctx(**ctxkw)
            try:
                fn(ctx)
            finally:
                ctx.close()
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


# --------------------------------------------------------------------------- fixtures
def rect_patch(ore, x1, y1, x2, y2, foreign=None):
    tiles = {(x, y): 1000 for x in range(x1, x2 + 1) for y in range(y1, y2 + 1)}
    return {"ore": ore, "tiles": tiles, "bbox": (x1, y1, x2, y2), "foreign": foreign or {}}


# a patch shaped like the operator's copper outpost (his lane tile row is -64)
COPPER = rect_patch("copper-ore", -36, -70, -20, -58)
COAL = rect_patch("coal", -46, 10, -36, 21)
IRON = rect_patch("iron-ore", 10, -46, 28, -36)


def roles(plan, *want):
    return [e for e in plan["entities"] if e["role"] in want]


def copper_plan(**kw):
    kw.setdefault("lane_y", -64)
    kw.setdefault("trunk", (-10, 18))
    kw.setdefault("power_trunk_x", -15)
    kw.setdefault("grid_anchor", (-15, -65))
    return MP.plan_outpost("copper-ore", kw.pop("n", 6), patch=COPPER, **kw)


# --------------------------------------------------------------------------- spec numbers
def test_spec_constants_are_the_measured_ones():
    s = MP.OPERATOR_MINE_SPEC
    assert s["drill"] == "electric-mining-drill" and s["pole"] == "small-electric-pole"
    assert s["drill_pitch"] == 3 and s["pole_pitch"] == 7
    assert s["lane_offset"] == 2.0 and s["pole_offset"] == 2.0
    assert s["double_sided"] is True and s["output"] is None
    assert s["min_pole_sep"] == 3.0 and s["max_axis_hop"] == 7
    assert s["wire_reach"] == 7.5 and s["supply_radius"] == 2.5
    assert s["electric_networks"] == 1 and s["trunk_column_sep"] == 2


def test_pitches_are_derived_not_hardcoded():
    """pitch == tile_width because the collision half-width says so; the supply window is
    tw+4 integers wide, which is exactly what makes a pitch-7 run phase-independent."""
    assert MP.drill_pitch("electric-mining-drill") == 3 == ML.DRILLS["electric-mining-drill"]["tw"]
    assert MP.drill_pitch("burner-mining-drill") == 2
    lo, hi = MP.supply_window("electric-mining-drill", "small-electric-pole")
    assert (lo, hi) == (-2, 4) and hi - lo + 1 == 7
    assert MP.pole_pitch_for("electric-mining-drill", "small-electric-pole") == 7
    # a 2-wide drill has a 6-wide window, so its regular pitch must SHRINK, not stay at 7
    lo2, hi2 = MP.supply_window("burner-mining-drill", "small-electric-pole")
    assert hi2 - lo2 + 1 == 6
    assert MP.pole_pitch_for("burner-mining-drill", "small-electric-pole") == 6
    # the flank rows: lane_y -/+ (drill height + pole height) == lane_y -/+ 4 for 3x3 + small
    assert MP.pole_rows("electric-mining-drill", "small-electric-pole", -64) == {
        "top": -68, "bottom": -60}
    assert MP.pole_rows("electric-mining-drill", "small-electric-pole", 15) == {
        "top": 11, "bottom": 19}


def test_phase_independence_of_the_pitch7_run():
    """The property the pole lattice rests on: a window of 7 consecutive integers contains
    exactly one member of every residue class mod 7, so ANY phase covers EVERY drill."""
    lo, hi = MP.supply_window("electric-mining-drill", "small-electric-pole")
    for dx in range(-40, 41):
        window = set(range(dx + lo, dx + hi + 1))
        for r in range(7):
            assert len([p for p in window if p % 7 == r]) == 1, (dx, r)


# --------------------------------------------------------------------------- the guarantee
def _assert_drops_on_lane(plan):
    belt = plan["belt_tiles"]
    drills = roles(plan, "drill")
    assert drills, "a plan with no drills proves nothing"
    for e in drills:
        d = ML.drop_tile(plan["drill"], e["x"], e["y"], e["direction"])
        assert d in belt, "%s at (%d,%d) dir %d drops on %s, off the lane" % (
            e["entity"], e["x"], e["y"], e["direction"], (d,))
        assert d[1] == plan["lane_y"], "drop row %d != lane row %d" % (d[1], plan["lane_y"])


def test_every_drop_lands_on_the_lane_3x3():
    for n in (1, 2, 3, 4, 6, 8, 12):
        for ly in (-68, -66, -64, -63, -60):
            p = MP.plan_outpost("copper-ore", n, patch=COPPER, lane_y=ly, trunk=(-10, 18),
                                power_trunk_x=-15, grid_anchor=(-15, ly - 1))
            _assert_drops_on_lane(p)
            assert p["drill"] == "electric-mining-drill"
    for patch, trunk in ((COPPER, (-10, 18)), (COAL, (-28, 8)), (IRON, (-8, 8))):
        auto = MP.plan_outpost(patch["ore"], 6, patch=patch, trunk=trunk)    # lane_y swept
        _assert_drops_on_lane(auto)


def test_every_drop_lands_on_the_lane_2x2():
    """The 2x2 burner drops on its LEFT column facing north and its RIGHT column facing south
    - half of the copper failure. Both still land on the lane ROW, and the lane span must
    cover both columns."""
    for n in (2, 4, 6, 9):
        p = MP.plan_outpost("copper-ore", n, patch=COPPER, lane_y=-64, trunk=(-10, 18),
                            drill="burner-mining-drill", pole=None)
        _assert_drops_on_lane(p)
        assert p["drill_pitch"] == 2
    p = MP.plan_outpost("copper-ore", 6, patch=COPPER, lane_y=-64, trunk=(-10, 18),
                        drill="burner-mining-drill", pole=None)
    tops = {ML.drop_tile("burner-mining-drill", e["x"], e["y"], e["direction"])[0]
            for e in roles(p, "drill") if e["side"] == "top"}
    bots = {ML.drop_tile("burner-mining-drill", e["x"], e["y"], e["direction"])[0]
            for e in roles(p, "drill") if e["side"] == "bottom"}
    assert tops and bots and tops != bots, "burner drop columns must differ between the rows"


def test_double_sided_pairs_share_a_drop_tile():
    """Two facing drill rows drop onto the SAME belt tile but OPPOSITE lanes - that is the
    +167% the operator bought by rebuilding copper and coal double-sided."""
    p = copper_plan()
    tops = {(e["x"]) for e in roles(p, "drill") if e["side"] == "top"}
    bots = {(e["x"]) for e in roles(p, "drill") if e["side"] == "bottom"}
    assert tops == bots, "both rows must sit on the SAME x lattice, not interleaved"
    drops = [ML.drop_tile(p["drill"], e["x"], e["y"], e["direction"]) for e in roles(p, "drill")]
    assert len(set(drops)) * 2 == len(drops), "each N/S pair shares exactly one drop tile"


def test_no_pole_sits_on_a_belt_tile():
    cases = [copper_plan(),
             copper_plan(n=12),
             copper_plan(spur_side="bottom"),
             copper_plan(power_trunk_x=-10, grid_anchor=(-10, -70), spur_side="bottom"),
             MP.plan_outpost("coal", 4, patch=COAL, lane_y=15, trunk=(-28, 8),
                             power_trunk_x=-36, grid_anchor=(-36, 12)),
             MP.plan_outpost("iron-ore", 6, patch=IRON, lane_y=-41, trunk=(-8, 8),
                             power_trunk_x=-15, grid_anchor=(-15, -41))]
    for p in cases:
        poles = roles(p, "pole", "bridge")
        assert poles, "the electric outpost must plan poles"
        for e in poles:
            fp = ML.footprint(e["entity"], e["x"], e["y"])
            assert not (fp & p["belt_tiles"]), "pole at (%d,%d) is on a belt tile" % (
                e["x"], e["y"])
        drops = {ML.drop_tile(p["drill"], d["x"], d["y"], d["direction"])
                 for d in roles(p, "drill")}
        dfp = set()
        for d in roles(p, "drill"):
            dfp |= ML.footprint(d["entity"], d["x"], d["y"])
        for e in poles:
            fp = ML.footprint(e["entity"], e["x"], e["y"])
            assert not (fp & drops) and not (fp & dfp)


def test_lattice_poles_are_on_the_flank_rows_at_pitch_7():
    p = copper_plan()
    rows = MP.pole_rows(p["drill"], p["pole"], p["lane_y"])
    assert rows == p["pole_rows"] == {"top": -68, "bottom": -60}
    for side, py in rows.items():
        xs = sorted(e["x"] for e in roles(p, "pole") if e["y"] == py)
        for a, b in zip(xs, xs[1:]):
            assert 3 <= b - a <= 7, "%s row hop %d->%d is %d" % (side, a, b, b - a)
    assert all(e["y"] in rows.values() for e in roles(p, "pole"))
    # the spur row walks out to the power trunk column and terminates ON it
    spur = sorted(e["x"] for e in roles(p, "pole") if e["y"] == rows["top"])
    assert max(spur) == -15, spur


def test_every_drill_is_supplied_and_the_poles_are_one_network():
    for p in (copper_plan(), copper_plan(n=12),
              MP.plan_outpost("coal", 4, patch=COAL, lane_y=15, trunk=(-28, 8),
                              power_trunk_x=-36, grid_anchor=(-36, 12))):
        lo, hi = MP.supply_window(p["drill"], p["pole"])
        poles = roles(p, "pole", "bridge")
        for d in roles(p, "drill"):
            assert any(d["x"] + lo <= q["x"] <= d["x"] + hi
                       and MP._row_covers(p["pole"], q["y"], d["y"],
                                          ML.DRILLS[p["drill"]]["th"]) for q in poles), d
        anchor = p["params"]["grid_anchor"]
        nodes = poles + [{"entity": p["pole"], "x": anchor[0], "y": anchor[1]}]
        assert len(ML._components(nodes, ML.POLES[p["pole"]]["wire"])) == 1


def test_drill_pitch_never_overlaps_collision_boxes():
    """create_entity performs NO collision check: the live iron row is 6 drills at pitch 2
    whose 3x3 boxes overlap by 0.696 tiles. The planner is the only thing that can stop it."""
    p = copper_plan(n=12)
    half = MP.COLLISION_HALFWIDTH["electric-mining-drill"]
    for side in ("top", "bottom"):
        xs = sorted(e["x"] for e in roles(p, "drill") if e["side"] == side)
        for a, b in zip(xs, xs[1:]):
            assert b - a >= 3 and b - a >= 2 * half, (side, a, b)
    assert p["drill_pitch"] == 3


# --------------------------------------------------------------------------- lane geometry
def test_lane_direction_is_computed_from_the_trunk():
    east = MP.plan_outpost("copper-ore", 6, patch=COPPER, lane_y=-64, trunk=(-10, 18),
                           power_trunk_x=-15, grid_anchor=(-15, -65))
    assert east["lane_dir"] == MP.E
    west = MP.plan_outpost("iron-ore", 6, patch=IRON, lane_y=-41, trunk=(-8, 8),
                           power_trunk_x=-15, grid_anchor=(-15, -41))
    assert west["lane_dir"] == MP.W, "a trunk west of the mine must flip the row, not dead-end"
    for p in (east, west):
        lane = [e for e in p["entities"] if e["role"] == "lane"]
        tx, ty = p["trunk"]
        turn = [e for e in lane if e["x"] == tx]
        assert len(turn) == 1 and turn[0]["direction"] == p["turn_dir"]
        assert p["turn_dir"] == (MP.S if ty > p["lane_y"] else MP.N)
        assert all(e["direction"] == p["lane_dir"] for e in lane if e["x"] != tx)


def test_lane_has_no_head_overshoot_and_no_gaps():
    for p in (copper_plan(), MP.plan_outpost("iron-ore", 6, patch=IRON, lane_y=-41,
                                             trunk=(-8, 8), power_trunk_x=-15,
                                             grid_anchor=(-15, -41))):
        drops = sorted(ML.drop_tile(p["drill"], e["x"], e["y"], e["direction"])[0]
                       for e in roles(p, "drill"))
        s, en = p["lane_span"]
        xs = sorted(e["x"] for e in p["entities"] if e["role"] == "lane")
        assert xs == list(range(s, en + 1)), "the lane must be contiguous"
        if p["lane_dir"] == MP.E:
            assert s == drops[0], "east lane starts at the first drop, never upstream of it"
        else:
            assert en == drops[-1], "west lane starts at the first drop, never upstream of it"


def test_trunk_column_is_contiguous_from_the_lane():
    p = copper_plan()
    tx, ty = p["trunk"]
    ys = sorted(y for (x, y) in p["trunk_tiles"])
    assert ys == list(range(p["lane_y"] + 1, ty + 1))
    assert all(x == tx for (x, y) in p["trunk_tiles"])
    assert all(e["direction"] == MP.S for e in p["entities"] if e["role"] == "trunk")


def test_no_terminal_chest_and_no_inserter():
    """mine_layout defaults to inserter+wooden-chest; the operator's mines have ZERO of both
    (the whole map has 2 chests, and both are plate-belt drains)."""
    for p in (copper_plan(), MP.plan_outpost("coal", 4, patch=COAL, lane_y=15, trunk=(-28, 8),
                                             power_trunk_x=-36, grid_anchor=(-36, 12))):
        assert p["params"]["output"] is None
        names = set(p["bom"])
        assert not any(n.endswith("-chest") or "inserter" in n for n in names), names


# --------------------------------------------------------------------------- bom + output
def test_bom_matches_the_plan_exactly():
    for p in (copper_plan(), copper_plan(n=12),
              MP.plan_outpost("copper-ore", 6, patch=COPPER, lane_y=-64, trunk=(-10, 18),
                              drill="burner-mining-drill", pole=None)):
        b = MP.bom(p)
        assert b == p["bom"]
        assert sum(b.values()) == len(p["entities"])
        recount = {}
        for e in p["entities"]:
            recount[e["entity"]] = recount.get(e["entity"], 0) + 1
        assert recount == b
        assert b.get(p["drill"]) == len(roles(p, "drill"))
        assert b.get(p["belt"]) == len(p["belt_tiles"])
        if p["pole"]:
            assert b.get(p["pole"]) == len(roles(p, "pole", "bridge"))


def test_orders_and_ghosts_round_trip():
    p = copper_plan()
    orders = MP.to_orders(p)
    assert len(orders) == len(p["entities"])
    assert orders[0]["args"]["name"] == p["drill"], "drills must be ordered first"
    for o in orders:
        assert o["kind"] == "place"
        assert set(o["args"]) == {"name", "tile_x", "tile_y", "direction"}
        assert o["args"]["direction"] in (MP.N, MP.E, MP.S, MP.W)
    ghosts = MP.to_ghosts(p)
    assert len(ghosts) == len(p["entities"])
    drill = next(g for g in ghosts if g["name"] == p["drill"])
    assert drill["x"] % 1 == 0.5 and drill["y"] % 1 == 0.5     # 3x3 -> .5 centre
    tiles = MP.plan_tiles(p)
    assert len(tiles) == len(p["entities"]) and all(len(t) == 3 for t in tiles)
    assert len({(x, y) for (x, y, _d) in tiles}) == len(tiles), "one entity per tile"


# --------------------------------------------------------------------------- validation
def test_validate_catches_a_pole_on_the_lane():
    p = copper_plan()
    assert p["validation"]["ok"]
    bad = dict(p)
    bad["entities"] = list(p["entities"]) + [
        {"entity": p["pole"], "x": p["lane_span"][0] + 2, "y": p["lane_y"],
         "direction": MP.N, "role": "pole", "side": "top"}]
    bad["bom"] = MP.bom(bad)
    v = MP.validate(bad)
    assert not v["ok"]
    assert any("on a belt tile" in e for e in v["errors"]), v["errors"]


def test_validate_catches_a_drop_off_the_lane():
    p = copper_plan()
    bad = dict(p)
    moved = dict(roles(p, "drill")[0])
    moved["y"] += 1                                    # the in-place tier-swap failure mode
    bad["entities"] = [moved] + [e for e in p["entities"] if e is not roles(p, "drill")[0]]
    bad["bom"] = MP.bom(bad)
    v = MP.validate(bad)
    assert not v["ok"]
    assert any("not a planned belt tile" in e or "not the lane row" in e for e in v["errors"])


def test_validate_catches_overlapping_drills_and_a_terminal_chest():
    p = copper_plan()
    d = roles(p, "drill")[0]
    bad = dict(p)
    bad["entities"] = list(p["entities"]) + [
        {"entity": p["drill"], "x": d["x"] + 2, "y": d["y"], "direction": d["direction"],
         "role": "drill", "side": d["side"], "ore": 9},
        {"entity": "wooden-chest", "x": 99, "y": 99, "direction": MP.N, "role": "lane"}]
    bad["bom"] = MP.bom(bad)
    v = MP.validate(bad)
    assert any("apart" in e and "needs >=" in e for e in v["errors"]), v["errors"]
    assert any("belt-fed" in e for e in v["errors"]), v["errors"]


def test_strict_plan_refuses_to_return_an_invalid_layout():
    try:
        MP.plan_outpost("copper-ore", 6, patch=COPPER, lane_y=-64, trunk=(-10, 18),
                        pole_pitch=12)      # 12 > wire reach 7.5: the run cannot be connected
    except MP.LayoutError as e:
        assert "invariant" in str(e) or "network" in str(e) or "hop" in str(e), str(e)
    else:
        raise AssertionError("a pitch past the wire reach must not produce a plan")


def test_foreign_ore_veto_and_trunk_sanity():
    foreign = {(x, y): "stone" for x in range(-30, -20) for y in range(-70, -57)}
    p = MP.plan_outpost("copper-ore", 4, patch=rect_patch("copper-ore", -36, -70, -20, -58,
                                                          foreign),
                        lane_y=-64, trunk=(-10, 18), power_trunk_x=-15,
                        grid_anchor=(-15, -65))
    for d in roles(p, "drill"):
        a, b, c, e = ML.mining_area(p["drill"], d["x"], d["y"])
        assert not any((x, y) in foreign for x in range(a, c + 1) for y in range(b, e + 1))
    try:
        MP.plan_outpost("copper-ore", 6, patch=COPPER, lane_y=-64, trunk=(-28, 18))
    except MP.LayoutError as err:
        assert "divert mid-row" in str(err), str(err)
    else:
        raise AssertionError("a trunk column inside the drill span must be refused")


# --------------------------------------------------------------------------- electrify
def test_electrification_replan_produces_a_valid_layout():
    """The in-place swap put 3x3 drills on 2x2 centres and moved every drop tile onto bare
    ground. replan_electric re-derives the whole outpost instead."""
    burner = MP.plan_outpost("copper-ore", 6, patch=COPPER, lane_y=-64, trunk=(-10, 18),
                             drill="burner-mining-drill", pole=None)
    assert burner["drill_pitch"] == 2 and burner["pole"] is None
    old_positions = {(e["x"], e["y"]) for e in roles(burner, "drill")}
    old_drops = {ML.drop_tile(burner["drill"], e["x"], e["y"], e["direction"])
                 for e in roles(burner, "drill")}

    elec = MP.replan_electric(burner, n_drills=6, power_trunk_x=-15, grid_anchor=(-15, -65))
    assert elec["validation"]["ok"], elec["validation"]["errors"]
    assert elec["drill"] == "electric-mining-drill" and elec["drill_pitch"] == 3
    assert elec["lane_y"] == burner["lane_y"], "the lane ROW rule is tier-independent"
    _assert_drops_on_lane(elec)
    new_positions = {(e["x"], e["y"]) for e in roles(elec, "drill")}
    assert new_positions != old_positions, "a re-plan must move the drills, not reuse centres"
    new_drops = {ML.drop_tile(elec["drill"], e["x"], e["y"], e["direction"])
                 for e in roles(elec, "drill")}
    assert new_drops != old_drops, "the 3x3 footprint MOVES the drop tile - that is the bug"
    assert elec["pole"] == "small-electric-pole" and roles(elec, "pole")
    assert not any("inserter" in n or n.endswith("-chest") for n in elec["bom"])


def test_obsolete_fuel_tiles_are_everything_the_new_layout_does_not_need():
    burner = MP.plan_outpost("copper-ore", 6, patch=COPPER, lane_y=-64, trunk=(-10, 18),
                             drill="burner-mining-drill", pole=None)
    elec = MP.replan_electric(burner, n_drills=6, power_trunk_x=-15, grid_anchor=(-15, -65))
    fuel = [(x, -70) for x in range(-36, -25)] + [(-36, y) for y in range(-70, -66)]
    shared = sorted(elec["tiles_used"])[:3]
    dead = MP.obsolete_fuel_tiles(elec, fuel + shared)
    assert set(dead) == set(fuel), "only tiles the new layout does not reuse may be torn out"
    assert not (set(dead) & elec["tiles_used"])
    assert MP.obsolete_fuel_tiles(elec, ()) == []


def test_upgrade_to_electric_plans_without_touching_the_server():
    burner = MP.plan_outpost("copper-ore", 6, patch=COPPER, lane_y=-64, trunk=(-10, 18),
                             drill="burner-mining-drill", pole=None)
    fuel = [(x, -70) for x in range(-36, -25)]
    out = MP.upgrade_to_electric("copper-ore", burner, fuel_tiles=fuel, apply=False,
                                 power_trunk_x=-15, grid_anchor=(-15, -65))
    assert out["build"] is None and out["superseded"] is None and out["fuel_removed"] is None
    assert out["plan"]["validation"]["ok"]
    assert set(out["obsolete_fuel"]) == set(fuel)


# --------------------------------------------------------------------------- build/verify
@_with_ctx()
def test_build_places_verifies_and_records(ctx):
    MP._register()
    p = copper_plan()
    calls = []

    def place(rec, tiles):
        calls.append(len(tiles))
        return {"placed": [(int(t[0]), int(t[1])) for t in tiles], "already": [], "failed": []}

    rec = MP.build(p, place_fn=place, verify_fn=lambda r: (True, "connected+moving"), tries=1)
    assert rec["status"] == "verified", rec["verify"]
    assert calls == [len(p["entities"])]
    assert len(rec["verify"]["placed"]) == len(p["entities"])
    assert rec["verify"]["check"] == {"ok": True, "detail": "connected+moving"}
    assert ctx.built == {(e["x"], e["y"]) for e in p["entities"]}
    assert rec["names"] == sorted(p["bom"])


@_with_ctx()
def test_build_rolls_back_when_the_lane_does_not_move_ore(ctx):
    """BUILD LAW 2: if the result is nothing, remove what you built - in the SAME pass."""
    MP._register()
    p = copper_plan()

    def place(rec, tiles):
        return {"placed": [(int(t[0]), int(t[1])) for t in tiles], "already": [], "failed": []}

    rec = MP.build(p, place_fn=place, tries=1,
                   verify_fn=lambda r: {"ok": False, "detail": "connected=True moving=False"})
    assert rec["status"] == "failed"
    assert rec["verify"]["check"]["ok"] is False
    assert rec["verify"]["rollback"]["removed"] == len(p["entities"])
    # what reaches the WORLD remover is each entity's CENTRE tile - a 3x3 drill looked up at
    # its top-left tile is invisible at radius 0.8 and would be left standing (see
    # test_rollback_finds_a_3x3_drill_where_the_engine_actually_has_it)
    assert set(ctx.removed) == {MP.probe_tile(e["entity"], e["x"], e["y"])
                                for e in p["entities"]}
    assert ctx.built == set(), "the ledger must forget exactly what rollback removed"
    assert rec["verify"]["placed"] == []


@_with_ctx()
def test_build_honours_the_truce_and_the_protected_ledger(ctx):
    MP._register()
    p = copper_plan()
    ctx.operator = True
    rec = MP.build(p, place_fn=lambda r, t: {"placed": list(t)},
                   verify_fn=lambda r: True, tries=1)
    assert rec["status"] == "planned" and "OPERATOR PRESENT" in rec["verify"]["refused"]
    assert ctx.built == set(), "zero construction while a human is connected"

    ctx.operator = False
    ctx.protected = {(e["x"], e["y"]) for e in p["entities"][:len(p["entities"]) // 2]}
    rec2 = MP.build(p, place_fn=lambda r, t: {"placed": list(t)},
                    verify_fn=lambda r: True, tries=1)
    assert rec2["status"] == "superseded"
    assert "OPERATOR-OWNED ROUTE" in rec2["verify"]["refused"]


@_with_ctx()
def test_build_refuses_an_invalid_plan_before_placing(ctx):
    MP._register()
    p = copper_plan()
    p["entities"] = list(p["entities"]) + [
        {"entity": p["pole"], "x": p["lane_span"][0] + 3, "y": p["lane_y"],
         "direction": MP.N, "role": "pole", "side": "top"}]
    p["bom"] = MP.bom(p)
    p["validation"] = MP.validate(p)
    try:
        MP.build(p, place_fn=lambda r, t: {"placed": list(t)}, verify_fn=lambda r: True)
    except MP.LayoutError as e:
        assert "refusing to build an invalid plan" in str(e)
    else:
        raise AssertionError("build must refuse a plan that fails its own invariants")
    assert ctx.built == set(), "nothing may be placed by a refused build"
    records = list(B.PLANS_DIR.glob("*.json")) if B.PLANS_DIR.is_dir() else []
    assert records == [], "an invalid plan must not even get a buildplan record"


@_with_ctx()
def test_place_tiles_uses_the_planned_entity_per_tile_and_wires_the_poles(ctx):
    MP._register()
    p = copper_plan()
    ctx.fake.script = [("get_wire_connector", "WIRED 6 NETS 1 POLES %d\n"
                        % (len(roles(p, "pole", "bridge")) + 1))]
    rec = B.new_plan(MP.KIND, args=MP._plan_args(p), tiles=MP.plan_tiles(p),
                     names=sorted(p["bom"]))
    out = MP.place_tiles(rec, rec["tiles"])
    assert not out["failed"], out["failed"]
    assert len(out["placed"]) == len(p["entities"])
    byname = {}
    for (name, tx, ty, d) in ctx.placed_calls:
        byname[name] = byname.get(name, 0) + 1
        want = next(e for e in p["entities"] if e["x"] == tx and e["y"] == ty)
        assert name == want["entity"] and d == want["direction"]
    assert byname == p["bom"]
    # drills first, then the lane, then the trunk, then poles: a partial run leaves a mine
    # closer to working, never further
    seq = [MP.ROLE_RANK[next(e for e in p["entities"] if e["x"] == tx and e["y"] == ty)["role"]]
           for (_n, tx, ty, _d) in ctx.placed_calls]
    assert seq == sorted(seq)
    assert out["wiring"]["ok"] and out["wiring"]["networks"] == 1


@_with_ctx()
def test_wire_poles_emits_the_explicit_connector_call_and_checks_the_network(ctx):
    """Script-placed poles do NOT auto-connect: two small poles 4.0 apart sat on different
    electric_network_ids until wired by hand."""
    ctx.fake.script = [("pole_copper", "WIRED 2 NETS 1 POLES 3\n"),
                       ("pole_copper", "WIRED 1 NETS 2 POLES 3\n")]
    ok = MP.wire_poles([(-29, -68), (-22, -68)], anchor=(-15, -68))
    cmd = ctx.fake.calls[-1]
    assert "get_wire_connector(defines.wire_connector_id.pole_copper,true)" in cmd
    assert ".connect_to(cb,false)" in cmd and "electric_network_id" in cmd
    assert re.search(r"local R=7\.5", cmd), cmd
    assert ok == {"wired": 2, "networks": 1, "poles": 3, "ok": True,
                  "detail": "WIRED 2 NETS 1 POLES 3"}
    split = MP.wire_poles([(-29, -68), (-22, -68)], anchor=(-15, -68))
    assert split["ok"] is False and split["networks"] == 2, "2 networks is never a pass"


@_with_ctx()
def test_verify_lane_uses_lane_lint_verify_supply(ctx):
    import lane_lint
    orig = lane_lint.verify_supply
    seen = {}

    def fake(ore, from_xy, to_xy, settle=3.0, tol=1):
        seen.update(ore=ore, from_xy=tuple(from_xy), to_xy=tuple(to_xy))
        return {"connected": seen.get("connected", True), "moving": seen.get("moving", True),
                "arrived": 4, "path_len": 40,
                "findings": [{"code": "DEAD_END", "severity": "error"}]}
    lane_lint.verify_supply = fake
    try:
        p = copper_plan()
        rec = B.new_plan(MP.KIND, args=MP._plan_args(p), tiles=MP.plan_tiles(p),
                         names=sorted(p["bom"]))
        r = MP.verify_lane(rec)
        assert r["ok"] is True and "connected=True moving=True" in r["detail"]
        assert seen["ore"] == "copper-ore"
        assert seen["from_xy"] == p["from_xy"] and seen["to_xy"] == p["to_xy"]
        # A FROZEN LANE USED TO FAIL HERE, AND THAT WAS THE BUG. `ok` gates
        # buildplan's rollback_on_fail, so failing a CONNECTED-but-idle lane tore out correct
        # belt whenever something downstream was stalled - and it came straight back next
        # pass. Live on 2026-08-30 that was 83 copper belts removed and relaid nine times in
        # twelve minutes while every copper furnace sat at full_output. `connected` is this
        # build's own result; `moving` is the world's, and a belt cannot fix a stall that is
        # not on the belt.
        seen["moving"] = False
        r = MP.verify_lane(rec)
        assert r["ok"] is True, "a connected lane is never rolled back for want of flow"
        assert "KEPT" in r["detail"], r["detail"]
        # ...but a route that never connected DID nothing, and still rolls back.
        seen["connected"] = False
        assert MP.verify_lane(rec)["ok"] is False, "a disconnected lane is never a pass"
    finally:
        lane_lint.verify_supply = orig


@_with_ctx()
def test_remove_tiles_is_scoped_and_audited(ctx):
    MP._register()
    tiles = [(-36, -70), (-35, -70), (-34, -70)]
    out = MP.remove_tiles(tiles, MP.BELT_NAMES, reason="dead coal fuel belts")
    assert out == {"removed": 3, "not_found": 0}
    assert ctx.removed == tiles
    recs = [r for r in B.plans() if r["kind"].endswith("_teardown")]
    assert len(recs) == 1 and recs[0]["status"] == "superseded"
    assert recs[0]["verify"]["superseded"]["reason"] == "dead coal fuel belts"


# ------------------------------------------------- the entity is where its CENTRE is
def test_a_3x3_drill_is_invisible_at_its_top_left_tile():
    """Probed LIVE, read only, 2026-08-29: an electric-mining-drill whose top-left tile is
    (-33,-67) sits at position (-31.5,-65.5), and
    find_entities_filtered{position={-32.5,-66.5}, radius=r} returns 0 for r=0.6/0.8/1.0 and
    1 for r=1.5 - the radius is measured to the entity's POSITION, not its bounding box.

    world.scan_tiles probes at 0.6 and buildplan._default_remove at 0.8, both around
    tile+0.5. So addressing a 3x3 drill by its TOP-LEFT tile (the planner's own coordinate
    convention) finds nothing: probe() calls every drill un-built and rollback leaves all 16
    standing while reporting them not_found. probe_tile() is the translation."""
    d = "electric-mining-drill"
    assert ML.center(d, -33, -67) == (-31.5, -65.5)            # the live position, exactly
    assert math.hypot(-31.5 - -32.5, -65.5 - -66.5) > 1.0      # invisible at 0.6/0.8/1.0
    assert MP.probe_tile(d, -33, -67) == (-32, -66)            # its centre's tile
    assert math.hypot(-31.5 - -31.5, -65.5 - -65.5) == 0.0     # exact hit there
    # 2x2 and 1x1 are already inside the 0.8 radius; the translation must not move them out
    assert math.hypot(*[c - t for c, t in zip(ML.center("burner-mining-drill", 4, 4),
                                              (4.5, 4.5))]) < 0.8
    assert MP.probe_tile("transport-belt", 4, 4) == (4, 4)


@_with_ctx()
def test_rollback_addresses_every_entity_by_its_centre_tile(ctx):
    """BUILD LAW 2 in the only form that matters: what rollback hands the world remover must
    be findable. It must still REPORT (and forget) the top-left tiles the record owns."""
    MP._register()
    p = copper_plan()
    rec = B.new_plan(MP.KIND, args=MP._plan_args(p), tiles=MP.plan_tiles(p),
                     names=sorted(p["bom"]))
    tiles = [(e["x"], e["y"]) for e in p["entities"]]
    out = MP.remove_placed(rec, tiles)
    drills = [e for e in p["entities"] if e["role"] == "drill"]
    assert drills, "fixture must contain 3x3 drills"
    for e in drills:
        want = (e["x"] + 1, e["y"] + 1)
        assert want in ctx.removed, "the drill was addressed at a tile the engine cannot see"
        assert (e["x"], e["y"]) not in ctx.removed
    assert sorted(out["removed_tiles"]) == sorted(tiles), "the ledger scope stays top-left"
    assert out["removed"] == len(tiles) and out["not_found"] == 0


@_with_ctx()
def test_probe_only_counts_the_planned_entity_at_its_centre_tile(ctx):
    """buildplan.probe accepts a hit under ANY of the plan's names, so a leftover belt on a
    tile the plan wants a DRILL on read as 'already built' and the drill was never placed."""
    MP._register()
    p = copper_plan()
    rec = B.new_plan(MP.KIND, args=MP._plan_args(p), tiles=MP.plan_tiles(p),
                     names=sorted(p["bom"]))
    tiles = [(e["x"], e["y"]) for e in p["entities"]]
    drill = [e for e in p["entities"] if e["role"] == "drill"][0]
    lane = [e for e in p["entities"] if e["role"] == "lane"][0]
    dc = MP.probe_tile(drill["entity"], drill["x"], drill["y"])
    assert MP.probe_placed(rec, tiles) == set()               # empty ground

    ctx.world[dc] = "transport-belt"                          # wrong entity, right tile
    assert (drill["x"], drill["y"]) not in MP.probe_placed(rec, tiles)

    ctx.world[dc] = drill["entity"]                           # the drill itself
    assert (drill["x"], drill["y"]) in MP.probe_placed(rec, tiles)

    ctx.world[(lane["x"], lane["y"])] = lane["entity"]        # 1x1: centre tile is itself
    assert (lane["x"], lane["y"]) in MP.probe_placed(rec, tiles)


# ------------------------------------------------- power, truce, reuse
def test_an_all_electric_mine_with_no_poles_is_refused():
    """`pole=None` (or two flank rows that both fail) used to fall straight past validate's
    power block and return ok=True: an electric mine with zero poles is drills that never
    turn. A BURNER mine legitimately has none."""
    try:
        MP.plan_outpost("copper-ore", 4, patch=COPPER, lane_y=-64, pole=None, trunk=(-10, 18))
    except MP.LayoutError as e:
        assert "NO poles" in str(e), e
    else:
        raise AssertionError("an all-electric mine with zero poles must not validate")
    p = MP.plan_outpost("copper-ore", 4, patch=COPPER, lane_y=-64, pole=None,
                        drill="burner-mining-drill", trunk=(-10, 18))
    assert p["validation"]["ok"] and not roles(p, "pole", "bridge")


@_with_ctx()
def test_remove_tiles_honours_the_truce_and_refuses_multi_tile_entities(ctx):
    """remove_tiles does NOT go through buildplan.apply, so none of apply's gates run on it -
    but teardown is construction and the truce is a law, not a build-path detail."""
    MP._register()
    ctx.operator = True
    out = MP.remove_tiles([(-36, -70)], MP.BELT_NAMES, reason="dead coal fuel belts")
    assert out["removed"] == 0 and "OPERATOR PRESENT" in out["refused"]
    assert ctx.removed == [] and not B.plans()
    ctx.operator = False
    try:
        MP.remove_tiles([(-36, -70)], ["electric-mining-drill"])
    except MP.LayoutError as e:
        assert "1x1" in str(e), e
    else:
        raise AssertionError("a tile-addressed remover cannot find a 3x3 drill")


def test_reusable_tiles_is_same_name_same_tile_never_footprints():
    """supersede(keep=new["tiles_used"]) kept FOOTPRINT tiles, so a 2x2 burner standing on a
    3x3 electric drill's top-left tile was 'reused' - it just blocks the placement."""
    new = copper_plan()
    d = [e for e in new["entities"] if e["role"] == "drill"][0]
    lane = [e for e in new["entities"] if e["role"] == "lane"][0]
    old = {"names": ["burner-mining-drill", "transport-belt"],
           "args": {"entities": [{"entity": "burner-mining-drill", "x": d["x"], "y": d["y"]},
                                 {"entity": "transport-belt", "x": lane["x"], "y": lane["y"]},
                                 {"entity": "transport-belt", "x": lane["x"], "y": lane["y"] + 9}]}}
    keep = MP.reusable_tiles(old, new)
    assert (lane["x"], lane["y"]) in keep                    # same name, same tile: real reuse
    assert (d["x"], d["y"]) not in keep                      # different entity: tear it out
    assert (lane["x"], lane["y"] + 9) not in keep            # not in the new plan at all
    tops = {(e["x"], e["y"]) for e in new["entities"]}
    assert set(keep) <= tops and not (set(keep) & (new["tiles_used"] - tops))


# --------------------------------------------------------------------------- operator shape
def test_reproduces_the_operator_copper_outpost_shape():
    """His copper mine, measured live: lane tile row -64 flowing east; N drill centres
    y=-65.5 (top-left tile row -67) facing south, S centres y=-61.5 (tile row -63) facing
    north; flank pole rows -68 and -60 (centres -67.5 / -59.5); no chest, no inserter."""
    p = copper_plan()
    assert p["lane_y"] == -64 and p["lane_dir"] == MP.E
    tops = sorted(e["y"] for e in roles(p, "drill") if e["side"] == "top")
    bots = sorted(e["y"] for e in roles(p, "drill") if e["side"] == "bottom")
    assert set(tops) == {-67} and set(bots) == {-63}
    assert ML.center(p["drill"], 0, -67)[1] == -65.5      # matches his measured N centre
    assert ML.center(p["drill"], 0, -63)[1] == -61.5      # and his measured S centre
    assert {e["direction"] for e in roles(p, "drill") if e["side"] == "top"} == {MP.S}
    assert {e["direction"] for e in roles(p, "drill") if e["side"] == "bottom"} == {MP.N}
    assert p["pole_rows"] == {"top": -68, "bottom": -60}
    assert p["drill_pitch"] == 3 and p["pole_pitch"] == 7
    assert len(roles(p, "drill")) == 6
    assert p["validation"]["ok"]
    # the ONLY warning a clean plan may carry: the run from the last drop to the trunk column
    # is laid without any world obstacle model (this module does not route it).
    assert len(p["warnings"]) == 1 and "laid BLIND" in p["warnings"][0], p["warnings"]
    # the flank rows are 8 apart - past the 7.5 wire reach on purpose, so the join is a BRIDGE
    assert p["pole_rows"]["bottom"] - p["pole_rows"]["top"] == 8
    assert roles(p, "bridge"), "the two flank rows must be bridged, not stretched"


# --------------------------------------------------------------------------- runner
TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    passed, failed = 0, []
    for t in TESTS:
        try:
            t()
            passed += 1
            print("  pass  %s" % t.__name__)
        except Exception:
            failed.append(t.__name__)
            print("  FAIL  %s" % t.__name__)
            traceback.print_exc()
    print("\n%d passed, %d failed (%d total)" % (passed, len(failed), len(TESTS)))
    raise SystemExit(1 if failed else 0)
