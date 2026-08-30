#!/usr/bin/env python3
"""Offline unit tests for build_gates.py — NO live server.

Run with either:
    python3 test_build_gates.py
    python3 -m pytest test_build_gates.py

Every gate is a pure function of a `state` dict, so almost every test is a synthetic state.
Three tests use snapshots/{before,after}.json as fixtures — they are the measurement the whole
module is derived from, so they are the tests that would actually catch a wrong constant.
The one test that exercises RCON installs a scripted FakeRcon (test_world_executor.py's
style); nothing here ever opens a socket, and reserve() is asserted to make zero RCON calls.
"""
import json
import pathlib
import re
import traceback

import build_gates as G

SNAPS = pathlib.Path(__file__).resolve().parent / "snapshots"


# --------------------------------------------------------------------------- harness
class FakeRcon:
    """Scripted rcon.run: (substring, response) steps consumed in order, plus native handling
    of the chunked storage._gates read. Mirrors test_world_executor.FakeRcon."""
    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []
        self.payload = None

    def payload_len(self, obj):
        self.payload = json.dumps(obj, separators=(",", ":"))
        return str(len(self.payload))

    def __call__(self, cmd, timeout=10.0):
        self.calls.append(cmd)
        m = re.search(r"storage\._gates:sub\((\d+),(\d+)\)", cmd)
        if m:
            i, j = int(m.group(1)), int(m.group(2))
            return self.payload[i - 1:j] + "\n"
        if not self.script:
            raise AssertionError("unexpected RCON call (script exhausted): %s" % cmd[:160])
        sub, resp = self.script.pop(0)
        assert sub in cmd, "expected %r in RCON cmd, got: %s" % (sub, cmd[:200])
        return resp(cmd) if callable(resp) else resp


def state(**kw):
    """A blank state with every table present, so a test only names what it means."""
    st = {"tick": 1, "research": "", "counts": {}, "counts_type": {}, "status": {},
          "status_type": {}, "recipes": {}, "ghosts": {}, "networks": 0, "flows": {},
          "boiler_water_min": -1, "boiler_coal_min": -1}
    st.update(kw)
    return st


def powered(cap_boilers=2, drills=0, inserters=0, labs=0, asms=0, **kw):
    """A state with a real, single, energized grid — the common precondition."""
    counts = {"boiler": cap_boilers, "steam-engine": cap_boilers * 2}
    if drills:
        counts["electric-mining-drill"] = drills
    if inserters:
        counts["inserter"] = inserters
    if labs:
        counts["lab"] = labs
    if asms:
        counts["assembling-machine-1"] = asms
    st = state(counts=counts, networks=1, **kw)
    return st


def snap(name):
    p = SNAPS / ("%s.json" % name)
    return G.state_from_snapshot(p) if p.exists() else None


# --------------------------------------------------------------------------- LAW 1: labs
def test_lab_blocked_with_no_pack_flow():
    """The two labs he deleted: both `missing_science_packs`, zero pack assemblers anywhere."""
    st = powered(labs=0, inserters=20)
    ok, why = G.gate("lab", 1, st)
    assert ok is False
    assert "automation-science-pack" in why and "BLOCKED lab x1" in why
    assert "flows" in why


def test_lab_allowed_with_pack_flow():
    st = powered(cap_boilers=4, inserters=20, labs=0,
                 research="logistics-2",
                 flows={"automation-science-pack": 20.0},
                 recipes={"automation-science-pack": {"working": 3}},
                 status={"stone-furnace": {"working": 20},
                         "mining-drill": {"working": 16}})
    ok, why = G.gate("lab", 9, st)
    assert ok is True, why
    assert "ALLOWED lab x9" in why
    # 9 labs x 2.0 packs/min is exactly the flow requirement
    assert G.required_flows("lab", 9) == {"automation-science-pack": 18.0}


def test_lab_blocked_when_upstream_is_backed_up():
    """LAW 1 from the other side: the bot's 9 labs drove 23/28 furnaces to full_output and
    took iron 174 -> 37/min. A new sink cannot help a chain that is already choked."""
    st = powered(cap_boilers=4, inserters=20, research="logistics-2",
                 flows={"automation-science-pack": 20.0},
                 recipes={"automation-science-pack": {"working": 3}},
                 status={"stone-furnace": {"full_output": 23, "working": 5}})
    ok, why = G.gate("lab", 9, st)
    assert ok is False and "BACKED UP" in why, why


def test_lab_blocked_without_research_queued():
    st = powered(cap_boilers=4, inserters=20,
                 flows={"automation-science-pack": 20.0},
                 recipes={"automation-science-pack": {"working": 3}},
                 status={"stone-furnace": {"working": 20}})
    rep = G.explain("lab", 9, st)
    assert rep["allowed"] is False
    assert "research_queued" in rep["failed"]
    assert "no_research_in_progress" in dict((c["check"], c["msg"]) for c in rep["checks"])["research_queued"]


def test_lab_blocked_when_the_pack_assembler_is_idle():
    """A built-but-idle converter is not a live input. `pack_producer_live` wants `working`."""
    st = powered(cap_boilers=4, inserters=20, research="logistics-2",
                 flows={"automation-science-pack": 20.0},
                 recipes={"automation-science-pack": {"no_ingredients": 2}},
                 status={"stone-furnace": {"working": 20}})
    rep = G.explain("lab", 9, st)
    assert "pack_producer_live" in rep["failed"]
    msg = dict((c["check"], c["msg"]) for c in rep["checks"])["pack_producer_live"]
    assert "built but idle" in msg


# --------------------------------------------------------------------------- LAW 1: sinks
def test_science_assembler_needs_a_sink():
    """He deleted an iron-gear-wheel assembler at full_output because nothing ate gears."""
    st = powered(cap_boilers=4, inserters=10,
                 flows={"iron-plate": 174.0, "copper-plate": 90.0})
    ok, why = G.gate("science_assembler", 1, st)
    # parenthesised: `a and b or c` made this pass on the `c` arm alone, ok=True included
    assert ok is False and ("sink" in why.lower() or "consumes" in why), why


def test_ghost_labs_are_a_committed_sink():
    """reserve() dissolves the assembler<->lab deadlock: a ghost array costs nothing, draws
    nothing and holds the ground, so it counts as the sink."""
    st = powered(cap_boilers=4, inserters=10,
                 flows={"iron-plate": 174.0, "copper-plate": 90.0},
                 ghosts={"lab": 26})
    ok, why = G.gate("science_assembler", 1, st)
    assert ok is True, why
    assert "ghost lab" in why


def test_gear_cell_gated_on_a_red_assembler_existing():
    """A gear cell is sized to the ONE cell it feeds (6 gears/min), not to its own 60/min
    ceiling — an assembler run at its ceiling is the full_output machine he deleted."""
    assert G.required_flows("science_assembler", 1, "iron-gear-wheel") == {"iron-plate": 24.0}
    st = powered(cap_boilers=4, inserters=10, flows={"iron-plate": 174.0})
    ok, why = G.gate("science_assembler", 1, st, {"product": "iron-gear-wheel"})
    assert ok is False and "iron-gear-wheel" in why, why
    st["recipes"] = {"automation-science-pack": {"working": 1}}
    ok, why = G.gate("science_assembler", 1, st, {"product": "iron-gear-wheel"})
    assert ok is True, why


def test_a_full_output_consumer_is_not_a_sink():
    st = powered(cap_boilers=4, inserters=10, flows={"iron-plate": 174.0},
                 recipes={"automation-science-pack": {"full_output": 2}})
    ok, why = G.gate("science_assembler", 1, st, {"product": "iron-gear-wheel"})
    assert ok is False and "full_output" in why, why


def test_plate_lane_may_terminate_in_one_overflow_chest():
    st = powered(cap_boilers=4, inserters=10, flows={"iron-plate": 174.0})
    ok, why = G.gate("plate_lane", 1, st)
    assert ok is False
    ok, why = G.gate("plate_lane", 1, st, {"terminal_chest": True})
    assert ok is True and "WARNING" in why and "back-pressure" in why


# --------------------------------------------------------------------------- LAW 2: power
def test_power_arithmetic_reproduces_the_measured_headroom():
    """The load table is only trustworthy if it reproduces the two states it was read from:
    before 6*90 + 67*13.9 + 2*60 + 1*77.5 = 1.6688 MW against 1.8 -> 1.079 (broken, and 2
    networks); after 16*90 + 58*13.9 = 2.2462 against 3.6 -> 1.603 (working, 1 network)."""
    before, after = snap("before"), snap("after")
    if before is None or after is None:
        print("    (skipped: snapshots/ not present)")
        return
    assert round(G.capacity_mw(before), 2) == 1.80
    assert round(G.load_mw(before), 4) == 1.6688
    assert round(G.headroom(before), 3) == 1.079
    assert before["networks"] == 2
    assert round(G.capacity_mw(after), 2) == 3.60
    assert round(G.load_mw(after), 4) == 2.2462
    assert round(G.headroom(after), 3) == 1.603
    assert after["networks"] == 1


def test_the_bots_nine_labs_would_have_been_blocked():
    """The whole module in one assertion: from the operator's own finished base, the very
    next thing the bot did — 9 labs with no pack assembler — is refused."""
    after = snap("after")
    if after is None:
        print("    (skipped: snapshots/ not present)")
        return
    ok, why = G.gate("lab", 9, after)
    assert ok is False
    assert "automation-science-pack 0.0/min < 18.0/min" in why, why


def test_electric_drills_blocked_on_a_split_grid():
    """before.json's net 405: 6 electric drills + 2 poles, no generator, coal 0/min."""
    st = powered(cap_boilers=4, drills=6, inserters=10)
    st["networks"] = 2
    ok, why = G.gate("mine_outpost", 6, st)
    assert ok is False and "SPLIT" in why and "405" in why


def test_electric_drills_blocked_without_power_headroom():
    st = powered(cap_boilers=1, drills=6, inserters=67, labs=2, asms=1)
    ok, why = G.gate("mine_outpost", 10, st)
    assert ok is False and "power headroom" in why and "boiler column FIRST" in why


def test_electric_drills_blocked_when_nothing_generates():
    st = state(counts={"electric-mining-drill": 1}, networks=1)
    rep = G.explain("mine_outpost", 1, st)
    assert rep["failed"] == ["power_headroom", "grid_energized"]
    msgs = dict((c["check"], c["msg"]) for c in rep["checks"])
    assert "no generation" in msgs["grid_energized"]


def test_first_power_column_is_unconditional():
    """LAW 2 has to bottom out somewhere: with nothing built, power goes first."""
    ok, why = G.gate("power_capacity", 1, state())
    assert ok is True, why


def test_third_boiler_column_blocked_on_coal():
    """Measured ceiling: 2*27 + 17*1.35 = 77/min demand against 120 mined = 1.56x. A third
    column takes demand past what a 50/50 splitter tap can carry."""
    after = snap("after")
    if after is None:
        print("    (skipped: snapshots/ not present)")
        return
    assert round(G.coal_demand_per_min(after), 2) == 76.95
    assert round(G.flow(after, "coal") / G.coal_demand_per_min(after), 2) == 1.56
    ok, why = G.gate("power_capacity", 1, after)
    assert ok is False and "coal" in why


def test_coal_gate_blocks_at_zero_mined():
    """A ZERO reading is the worst reading, not a missing one. `have > 0 and have < need`
    ALLOWED a boiler column on before.json — a base mining 0 coal/min, on a split grid, with
    nothing working — while the approval line itself printed "0/min mined vs 54/min demand"."""
    before = snap("before")
    if before is None:
        print("    (skipped: snapshots/ not present)")
        return
    assert before["flows"]["coal"] == 0.0
    ok, why = G.gate("power_capacity", 1, before)
    assert ok is False, why
    assert "coal 0/min" in why and "coal_at_boiler" in why
    # ...and an absent reading is still honestly reported as unchecked, not as pass
    st = state(counts={"boiler": 2, "steam-engine": 4, "offshore-pump": 1}, boiler_coal_min=5)
    ok, why = G.gate("power_capacity", 1, st)
    assert ok is True and "NOT MEASURED" in why, why


def test_gate_refuses_an_unknown_recipe_instead_of_raising():
    """explain() recomputed required_flows OUTSIDE its own try, so an unknown recipe made
    gate() raise KeyError instead of returning (False, reason) — a builder looping over gates
    died rather than moving to a stage that is allowed."""
    st = powered(cap_boilers=4, inserters=10, flows={"iron-plate": 174.0})
    ok, why = G.gate("science_assembler", 1, st, {"recipe": "military-science-pack"})
    assert ok is False
    assert "cannot evaluate flows" in why and "military-science-pack" in why
    rep = G.explain("science_assembler", 1, st, {"recipe": "military-science-pack"})
    assert rep["required_flows"] == {} and "flows" in rep["failed"]


# --------------------------------------------------------------------------- LAW 3
def test_smelter_array_is_exempt_from_the_flow_gate():
    """He kept 11/28 furnaces at no_ingredients and deleted 0; he deleted 3/3 idle ELECTRIC
    consumers. A burner draws 0 kW and locks 0 items — that is the whole discriminator."""
    ok, why = G.gate("smelter_array", 28, state())
    assert ok is True, why
    assert G.required_flows("smelter_array", 28) == {}
    assert "flows" not in G.GATES["smelter_array"]["requires"]
    assert "power_headroom" not in G.GATES["smelter_array"]["requires"]


def test_smelter_overbuild_reproduces_the_operators_ratio():
    """LAW 3 is a licence, not a blank cheque: 16 iron furnaces on 6 drills = 1.67x, 12
    copper on 4 = 1.88x, ceiling 2.0x. The denominator is DRILL CAPACITY, not plate flow."""
    st = state(counts={"electric-mining-drill": 10, "stone-furnace": 28},
               drills_by_ore={"iron-ore": {"electric-mining-drill": 6},
                              "copper-ore": {"electric-mining-drill": 4}},
               recipes={"iron-plate": {"working": 16}, "copper-plate": {"working": 12}})
    ok, why = G.gate("smelter_array", 0, st, {"ore": "iron-ore"})
    assert ok is True and "1.67x" in why, why
    ok, why = G.gate("smelter_array", 0, st, {"ore": "copper-ore"})
    assert ok is True and "1.88x" in why, why
    # 4 more iron furnaces would be 20/9.6 = 2.08x
    ok, why = G.gate("smelter_array", 4, st, {"ore": "iron-ore"})
    assert ok is False and "2.08x" in why and "ceiling is 2.0x" in why, why


def test_smelter_overbuild_is_unbounded_without_a_drill_census():
    """A snapshot carries no mining_target. Unknown must read as unknown, not as zero."""
    st = state(counts={"stone-furnace": 28})
    assert G.drill_capacity_per_min(st) is None
    ok, why = G.gate("smelter_array", 28, st, {"ore": "iron-ore"})
    assert ok is True and "unbounded" in why, why
    # ...but an explicit supply figure binds it
    ok, why = G.gate("smelter_array", 28, st, {"ore": "iron-ore", "supply_per_min": 180.0})
    assert ok is False and "2.0x" in why


def test_smelter_blocked_when_the_mine_does_not_exist_yet():
    st = state(drills_by_ore={"copper-ore": {"electric-mining-drill": 4}})
    ok, why = G.gate("smelter_array", 16, st, {"ore": "iron-ore"})
    assert ok is False and "no drill capacity" in why


def test_lane_capacity_caps_drills_at_fifteen():
    """450 items/min per belt lane / 30 per drill = 15."""
    assert G.MAX_DRILLS_PER_LANE == 15
    assert G.MAX_STONE_FURNACES_PER_LANE == 24
    st = powered(cap_boilers=8, status={"mining-drill": {"working": 4}})
    ok, why = G.gate("ore_lane", 15, st)
    assert ok is True, why
    ok, why = G.gate("ore_lane", 16, st)
    assert ok is False and "16 drills on one lane > 15" in why
    assert "split the outpost across two lanes" in why


def test_derived_constants_match_the_measured_ones():
    assert G.BOILER_COAL_PER_MIN == 27.0        # 1.8 MW / 4 MJ
    assert G.FURNACE_COAL_PER_MIN == 1.35       # 90 kW / 4 MJ
    assert G.DRILL_TO_FURNACE_RATIO == 1.6      # 30 / 18.75


def test_idle_furnaces_do_not_count_as_coal_demand():
    st = state(counts={"boiler": 2, "steam-engine": 4, "stone-furnace": 28},
               status={"stone-furnace": {"working": 14, "full_output": 3, "no_ingredients": 11}})
    assert round(G.coal_demand_per_min(st), 2) == 76.95      # 2*27 + 17*1.35


# --------------------------------------------------------------------------- LAW 4
def test_chest_gate_refuses_to_guess_lane_topology():
    ok, why = G.gate("overflow_chest", 1, state())
    assert ok is False and "lane_chests" in why


def test_chest_gate_rejects_a_midlane_chest():
    ok, why = G.gate("overflow_chest", 1, state(),
                     {"lane_chests": 0, "is_terminus": False})
    assert ok is False and "mid-lane" in why


def test_chest_gate_rejects_a_second_chest_on_one_lane():
    ok, why = G.gate("overflow_chest", 1, state(),
                     {"lane_chests": 1, "is_terminus": True})
    assert ok is False and "budget is 1" in why


def test_chest_gate_accepts_the_lane_terminus():
    ok, why = G.gate("overflow_chest", 1, state(),
                     {"lane_chests": 0, "is_terminus": True})
    assert ok is True, why


# --------------------------------------------------------------------------- flow table
def test_required_flows_table():
    assert G.required_flows("lab", 1) == {"automation-science-pack": 2.0}
    assert G.required_flows("lab", 9) == {"automation-science-pack": 18.0}
    # red cell: 6 packs/min x (2 Fe, 1 Cu) x 2.0 supply headroom
    assert G.required_flows("science_assembler", 1) == {"iron-plate": 24.0, "copper-plate": 12.0}
    # green cell [INFERENCE]: 5 packs/min x (5.5 Fe, 1.5 Cu) x 2.0
    assert G.required_flows("science_assembler", 1, "logistic-science-pack") == {
        "iron-plate": 55.0, "copper-plate": 15.0}
    assert G.required_flows("mall_assembler", 1) == {"iron-plate": 30.0}
    try:
        G.required_flows("teleporter")
        raise AssertionError("unknown structure accepted")
    except KeyError:
        pass


def test_affordable_count_is_the_revive_batch():
    st = powered(cap_boilers=4, inserters=20, flows={"automation-science-pack": 7.0})
    assert G.affordable_count("lab", st) == 3           # flow-capped: floor(7 / 2.0)
    st["flows"]["automation-science-pack"] = 72.0
    assert G.affordable_count("lab", st) == 36          # still flow-capped: floor(72 / 2.0)
    assert G.affordable_count("lab", st, cap=9) == 9    # and never more than the print holds


def test_affordable_count_is_power_capped_too():
    """LAW 2 binds the revive batch as well: 1 column (1.8 MW) with 20 inserters leaves
    1.8/1.5 - 0.278 = 0.922 MW, and a lab draws 60 kW -> 15."""
    st = powered(cap_boilers=1, inserters=20, flows={"automation-science-pack": 1000.0})
    assert G.affordable_count("lab", st) == 15


def test_affordable_count_never_negative():
    st = powered(cap_boilers=1, inserters=200, flows={"automation-science-pack": 100.0})
    assert G.affordable_count("lab", st) == 0


# --------------------------------------------------------------------------- clearance
IRON_ARRAY = {"name": "iron_array", "kind": "smelter_array", "bbox": (-10, 2, 28, 8)}
COPPER_ARRAY = {"name": "copper_array", "kind": "smelter_array", "bbox": (-14, 11, 20, 17)}
POWER = {"name": "power_plant", "kind": "power_plant", "bbox": (-37, 34, -28, 51)}
TRUNK = {"name": "pole_trunk", "kind": "corridor", "bbox": (-15, -65, -15, 26)}


def test_clearance_rejects_a_cramped_site():
    """A lab block 4 rows under the copper array. Measured minimum smelter_array ->
    consumer_block is 12 (his lab pole row y=30 against the copper array's y=17)."""
    ok, why = G.clearance_ok((0, 21, 25, 41), "lab_array", [IRON_ARRAY, COPPER_ARRAY])
    assert ok is False
    assert "measured minimum is 12" in why and "3 tile(s)" in why, why


def test_clearance_accepts_the_operators_own_lab_site():
    ok, why = G.clearance_ok((-2, 30, 25, 50), "lab_array", [IRON_ARRAY, COPPER_ARRAY, POWER])
    assert ok is True, why


def test_clearance_rejects_an_overlap_outright():
    """The lab that sat exactly on the 3x5 footprint of the engine that replaced it."""
    ok, why = G.clearance_ok((-31, 40, -29, 42), "lab_array", [POWER])
    assert ok is False and "OVERLAPS" in why


def test_clearance_uses_the_measured_pair_table():
    assert G.min_clearance("smelter_array", "smelter_array") == 3
    assert G.min_clearance("smelter_array", "lab_array") == 12
    assert G.min_clearance("lab_array", "smelter_array") == 12     # symmetric
    assert G.min_clearance("power_plant", "consumer_block") == 21
    assert G.min_clearance("mine_outpost", "mine_outpost") == 14
    assert G.min_clearance("mine_outpost", "smelter_array") == 23  # any_base_block
    assert G.min_clearance("mine_outpost", "power_plant") == 14
    assert G.min_clearance("rail_yard", "oil_field") == 12         # any_block floor


def test_clearance_ignores_corridors():
    """The gap is not empty: it carries the pole trunk and the transit lanes. That is what
    the clearance is FOR, so a corridor inside it is never a violation."""
    ok, why = G.clearance_ok((-2, 30, 25, 50), "lab_array", [TRUNK])
    assert ok is True, why


def test_clearance_separation_arithmetic():
    assert G.separation((0, 0, 5, 5), (10, 0, 15, 5)) == 4      # tiles 6..9
    assert G.separation((0, 0, 5, 5), (6, 0, 9, 5)) == 0        # touching
    assert G.separation((0, 0, 5, 5), (3, 3, 9, 9)) == -3       # overlapping by 3 on both axes
    assert G.separation((0, 0, 5, 5), (0, 0, 5, 5)) < 0         # identical: always an overlap


def test_clearance_rejects_an_inverted_bbox():
    ok, why = G.clearance_ok((10, 10, 0, 0), "lab_array", [])
    assert ok is False and "inverted" in why


# --------------------------------------------------------------------------- reserve
def lab_print(rows=5, cols=7, x0=0.5, y0=32.5, pitch=4):
    """A stand-in for the operator's 35-lab print: labs on a 4-pitch lattice, one pole in the
    seam NW of each lab, one inserter on each lab's west seam."""
    ents = []
    for r in range(rows):
        for c in range(cols):
            x, y = x0 + pitch * c, y0 + pitch * r
            ents.append({"name": "lab", "x": x, "y": y})
            ents.append({"name": "small-electric-pole", "x": x - 2, "y": y - 2})
            ents.append({"name": "inserter", "x": x - 2, "y": y, "direction": 4})
    return ents


def science_running(packs, **kw):
    """A state where the CONVERTER is live and research is queued — the other half of LAW 1.
    reserve() gates the revive set through gate(), so a fixture that omits these is a fixture
    asserting the module will revive the exact 9 labs it exists to refuse."""
    return powered(cap_boilers=4, inserters=20, research="logistics-2",
                   flows={"automation-science-pack": packs},
                   recipes={"automation-science-pack": {"working": 3}}, **kw)


def test_reserve_produces_the_ghost_plan():
    st = science_running(18.0)
    plan = G.reserve(lab_print(), unit="lab", state=st, feed=(0.5, 32.5))
    assert plan["gate_ok"] is True, plan["gate_reason"]
    assert plan["kind"] == "reserve"
    assert plan["total"] == 105 and plan["unit_total"] == 35
    assert plan["affordable_units"] == 9                 # floor(18 / 2.0)
    assert len([e for e in plan["revive"] if e["name"] == "lab"]) == 9
    assert len(plan["reserved"]) == plan["total"] - len(plan["revive"])
    # the reservation is the point: most of the print stays as ghosts (his own ratio was
    # 9 revived / 110 ghosts)
    assert len(plan["reserved"]) > len(plan["revive"])
    # every entity is ghosted, revived or not
    assert len(plan["ghosts"]) == plan["total"]
    assert plan["bbox"] == [-2, 30, 25, 49]
    assert "entity-ghost" in plan["ghost_lua"][0] and "inner_name" in plan["ghost_lua"][0]
    assert "find_entities_filtered" in plan["verify_lua"]
    assert plan["law"] == G.LAW_PASSIVE_ONLY
    assert "revive 9 unit(s)" in plan["reason"]


def test_reserve_revives_the_units_nearest_the_feed():
    st = science_running(6.0)
    plan = G.reserve(lab_print(), unit="lab", state=st, feed=(0.5, 32.5))
    labs = sorted((e["x"], e["y"]) for e in plan["revive"] if e["name"] == "lab")
    assert plan["affordable_units"] == 3
    assert labs == [(0.5, 32.5), (0.5, 36.5), (4.5, 32.5)]


def test_reserve_pulls_support_entities_in_with_their_lab():
    """Support entities come with the lab they belong to — and NOT with the lab next door.
    A revived inserter whose lab is still a ghost reads `waiting_for_target_to_be_built`,
    which is "built something that does nothing" in status form."""
    st = science_running(2.0)
    plan = G.reserve(lab_print(), unit="lab", n=1, state=st, feed=(0.5, 32.5))
    got = sorted((e["name"], e["x"], e["y"]) for e in plan["revive"])
    assert [g for g in got if g[0] == "lab"] == [("lab", 0.5, 32.5)]
    # the four seam poles that touch the revived lab, and only inserters adjacent to it
    assert [g[1:] for g in got if g[0] == "small-electric-pole"] == [
        (-1.5, 30.5), (-1.5, 34.5), (2.5, 30.5), (2.5, 34.5)]
    assert [g[1:] for g in got if g[0] == "inserter"] == [(-1.5, 32.5), (2.5, 32.5)]
    # the inserter that belongs to the un-revived lab below stays a ghost
    assert ("inserter", -1.5, 36.5) in sorted(
        (e["name"], e["x"], e["y"]) for e in plan["reserved"])


def test_reserve_with_no_unit_gate_reserves_everything():
    plan = G.reserve(lab_print(), state=state())
    assert plan["revive"] == [] and len(plan["reserved"]) == 105
    assert "reviving nothing" in plan["reason"]


def test_reserve_never_touches_rcon():
    """It returns a PLAN. Placement belongs to buildplan/executor, which own the truce check,
    the protected-tile ledger and rollback."""
    import rcon
    orig = rcon.run
    fake = FakeRcon()
    rcon.run = fake
    try:
        st = powered(cap_boilers=4, flows={"automation-science-pack": 18.0})
        G.reserve(lab_print(), unit="lab", state=st)
    finally:
        rcon.run = orig
    assert fake.calls == [], fake.calls


def test_reserve_refuses_to_revive_what_the_gate_refuses():
    """affordable_count() reads FLOWS and POWER only. The rest of LAW 1 — a live pack producer,
    queued research, upstream not backed up — is not in it, so reserve() must run gate() over
    the revive set. Without this it hands back 9 labs on a base with no pack assembler and no
    research: the exact build the module exists to refuse."""
    st = powered(cap_boilers=4, inserters=20, flows={"automation-science-pack": 18.0})
    assert G.affordable_count("lab", st) == 9          # flows alone say yes
    assert G.gate("lab", 9, st)[0] is False            # the full gate says no
    plan = G.reserve(lab_print(), unit="lab", state=st, feed=(0.5, 32.5))
    assert plan["gate_ok"] is False
    assert plan["revive"] == [] and plan["affordable_units"] == 0
    assert len(plan["reserved"]) == plan["total"]      # the ghosts are still free, and still laid
    assert "REVIVE REFUSED" in plan["reason"] and "pack_producer_live" in plan["reason"]


def test_reserve_accepts_the_stamp_blueprint_dir_key():
    """autopilot.stamp_blueprint and mine_layout.to_ghosts emit {name,x,y,dir}. Reading only
    `direction` silently reserved every belt, inserter and drill facing NORTH."""
    fp = [{"name": "transport-belt", "x": 1.5, "y": 2.5, "dir": 8},
          {"name": "electric-mining-drill", "x": 4.5, "y": 2.5, "dir": 4}]
    plan = G.reserve(fp, unit="electric-mining-drill", n=0, state=state())
    assert [e["direction"] for e in plan["ghosts"]] == [8, 4]
    assert "{'transport-belt',1.5,2.5,8}" in plan["ghost_lua"][0]
    # an explicit `direction` still wins over a stale `dir`
    plan = G.reserve([{"name": "inserter", "x": 0.5, "y": 0.5, "dir": 8, "direction": 12}],
                     n=0, state=state())
    assert plan["ghosts"][0]["direction"] == 12


def test_reserve_records_the_pole_wiring_obligation():
    """Script-placed poles do NOT reliably auto-connect. reserve() ships poles inside `revive`,
    so the plan must carry that obligation instead of leaving it implicit."""
    plan = G.reserve(lab_print(), unit="lab", n=1, state=science_running(2.0), feed=(0.5, 32.5))
    assert plan["wire_required"] is True
    assert len(plan["revive_poles"]) == 4
    assert "electric_network_id" in plan["wire_hint"] and "connect_to" in plan["wire_hint"]
    assert "WIRE THE 4 REVIVED POLE(S)" in plan["reason"]
    # nothing revived -> nothing to wire
    plan = G.reserve(lab_print(), state=state())
    assert plan["wire_required"] is False and plan["revive_poles"] == []


def test_reserve_verify_lua_is_scoped_to_the_plans_own_prototypes():
    """A bare ghost count over a bbox also counts the neighbouring build's ghosts, and has
    nothing to compare against."""
    plan = G.reserve(lab_print(), unit="lab", n=0, state=state())
    assert plan["expect_ghosts"] == 105
    v = plan["verify_lua"]
    assert "['lab']=1" in v and "['inserter']=1" in v and "['small-electric-pole']=1" in v
    assert "ghost_name" in v and "create_entity" not in v


def test_reserve_rejects_a_malformed_footprint():
    for bad in ([], [{"name": "lab"}]):
        try:
            G.reserve(bad, unit="lab", n=0, state=state())
            raise AssertionError("malformed footprint accepted: %r" % (bad,))
        except ValueError:
            pass


# --------------------------------------------------------------------------- reporting
def test_why_blocked_is_a_human_readable_list():
    st = powered(cap_boilers=1, drills=6, inserters=67, labs=2, asms=1)
    lines = G.why_blocked(st)
    assert lines and all(isinstance(s, str) and s.startswith("BLOCKED ") for s in lines)
    joined = "\n".join(lines)
    assert "lab" in joined and "power headroom" in joined


def test_why_blocked_honours_a_plan():
    st = powered(cap_boilers=4, inserters=20, flows={"automation-science-pack": 4.0},
                 research="logistics-2", recipes={"automation-science-pack": {"working": 1}},
                 status={"stone-furnace": {"working": 20}})
    assert G.why_blocked(st, ["lab"], {"lab": 2}) == []          # 4/min feeds 2 labs
    blocked = G.why_blocked(st, ["lab"], {"lab": 9})
    assert len(blocked) == 1 and "< 18.0/min" in blocked[0]


def test_explain_exposes_every_check():
    st = powered(cap_boilers=1, inserters=67)
    rep = G.explain("lab", 9, st)
    assert rep["allowed"] is False
    assert [c["check"] for c in rep["checks"]] == list(G.GATES["lab"]["requires"])
    assert rep["required_flows"] == {"automation-science-pack": 18.0}
    assert rep["law"] == G.LAW_TWO_SIDED


def test_gate_rejects_an_unknown_structure():
    try:
        G.gate("teleporter", 1, state())
        raise AssertionError("unknown structure accepted")
    except KeyError as e:
        assert "teleporter" in str(e)


def test_build_order_is_covered_by_the_gate_table():
    for structure, _why in G.BUILD_ORDER:
        assert structure in G.GATES, structure


# --------------------------------------------------------------------------- sense()
def test_sense_parses_the_chunked_payload():
    import rcon
    orig, fake = rcon.run, FakeRcon()
    payload = {"tick": 1232799, "counts": {"lab": 9, "boiler": 2, "steam-engine": 4,
                                           "inserter": 113, "electric-mining-drill": 16},
               "counts_type": {"lab": 9}, "status": {}, "status_type": {},
               "recipes": {"iron-plate": {"full_output": 16}}, "ghosts": {"lab": 26},
               "networks": 1, "boiler_water_min": 200, "boiler_coal_min": 5,
               "generated_kw": 323, "research": "", "flows": {"iron-plate": 0.0}}
    fake.script = [("storage._gates=helpers.table_to_json", lambda c: fake.payload_len(payload)),
                   ("storage._gates=nil", "")]
    rcon.run = fake
    G._CACHE["state"] = None
    try:
        st = G.sense(ttl=0)
    finally:
        rcon.run = orig
        G._CACHE["state"] = None
    assert st["networks"] == 1 and st["ghosts"]["lab"] == 26
    assert round(G.capacity_mw(st), 2) == 3.60
    # 16 drills * 90 + 113 inserters * 13.9 + 9 labs * 60 = 3.55 MW -> headroom 1.014
    assert round(G.headroom(st), 3) == 1.014
    ok, why = G.gate("lab", 9, st)
    assert ok is False, why
    # the scratch key is cleared, and nothing but reads went out
    assert any("storage._gates=nil" in c for c in fake.calls)
    for c in fake.calls:
        for verb in ("create_entity", "destroy", "set_recipe", "walking_state", "rotate"):
            assert verb not in c, c


def test_sense_raises_on_a_broken_scan():
    """A gate that silently sees an empty world would ALLOW everything — the exact failure
    this module exists to stop. bottleneck.py's rule: a monitor that lies when it breaks is
    worse than one that stops."""
    import rcon
    orig, fake = rcon.run, FakeRcon([("storage._gates", "Error: unknown key")])
    rcon.run = fake
    G._CACHE["state"] = None
    try:
        G.sense(ttl=0)
        raise AssertionError("broken scan did not raise")
    except RuntimeError as e:
        assert "sense failed" in str(e)
    finally:
        rcon.run = orig
        G._CACHE["state"] = None


def test_state_from_snapshot_matches_the_snapshot():
    after = snap("after")
    if after is None:
        print("    (skipped: snapshots/ not present)")
        return
    assert after["counts"]["stone-furnace"] == 28
    assert after["counts"]["electric-mining-drill"] == 16
    assert after["status"]["stone-furnace"] == {"working": 14, "full_output": 3,
                                                "no_ingredients": 11}
    assert after["flows"]["iron-plate"] == 174.0
    assert after["boiler_coal_min"] == 5.0


# --------------------------------------------------------------------------- plain runner
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print("PASS %s" % t.__name__)
        except Exception:
            failed += 1
            print("FAIL %s" % t.__name__)
            traceback.print_exc()
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    raise SystemExit(1 if failed else 0)
