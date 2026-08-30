#!/usr/bin/env python3
"""Offline tests for RELIEF-AWARE GATING (build_gates LAW 5) and the deadlock detector.

    python3 test_gate_relief.py
    python3 -m pytest test_gate_relief.py

NOTHING here touches the live server: `rcon.run` is replaced by a raiser for the whole
session, so a stray RCON call is a hard failure rather than a write into the operator's base.

WHAT THIS FILE IS ABOUT. On 2026-08-30 the build gates, each of them individually correct,
produced a DEADLOCK on a running base. The live log, verbatim:

    gate BLOCK: power_capacity x1 [coal_at_boiler] - coal 120/min < 178/min ... mine more coal
    gate BLOCK: smelter_array x12 [overbuild_within_budget] - 28 furnaces would be 2.92x ...
    gate BLOCK: lab x1 [flows] - automation-science-pack 0.0/min < 2.0/min needed
    gate BLOCK: science_assembler x1 [power_headroom] - headroom 0.992 < 1.50 ...
    spine: no plant poles recorded yet - the plant stage runs first
    phase 0 gate not met: labs, logistics-2 - builder idles 90s

Every line is true and nothing can ever be built again: power is refused for want of coal, the
build that raises coal is refused for want of power, and the coal-lane stage that would deliver
it sits downstream of the plant stage that is blocked. THE BUG: a gate that blocks structure X
because constraint C is unsatisfied must not ALSO block the build that INCREASES C.

The scenarios below are that base, reconstructed to the digit (2 boilers / 4 engines = 3.60 MW,
16 electric drills, 113 inserters, 9 labs, 28 stone furnaces, coal 120/min, 3.55 MW of nominal
load = headroom 0.99), so the numbers in the assertions are the numbers from the log.

POSTSCRIPT, 2026-08-29 — THE FIRST LINE WAS NOT TRUE. Measured against the running game: 2
boilers at 405.2 kW burned 6.0 coal/min, where `boilers * BOILER_COAL_PER_MIN` predicted 54. A
boiler converts fuel for the work its engines are asked for; it is not a fire that burns
whether you use it or not. The coal gate was therefore protecting a plant running at 11% load
by demanding 178 coal/min against 120 supplied, and it made capacity self-blocking - every
column raised the demand it had to satisfy.

With the burn model corrected (and the bound restated as fuelability: the plant AFTER the
build, at full tilt, must be something the mine can actually run) the live base has a legal
first move again - one boiler column - and the chain runs: column -> headroom -> ELECTRIC coal
drills -> more coal -> next column. So `live()` is no longer coal-blocked, and the tests that
exercise the coal branch of the ladder use `coal_short()` below, a base whose mine genuinely
cannot fuel its plant. The relief machinery is unchanged and still under test; what changed is
that this particular base was never really out of moves.
"""
import pathlib
import traceback

import rcon

_REAL_RCON = rcon.run


def _no_rcon(cmd, timeout=10.0):
    raise AssertionError("offline test issued RCON: %s" % str(cmd)[:160])


rcon.run = _no_rcon

import build_gates as G                                                   # noqa: E402
import planner                                                            # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent


# --------------------------------------------------------------------------- fixtures
def state(**kw):
    st = {"tick": 1, "counts": {}, "counts_type": {}, "status": {}, "status_type": {},
          "recipes": {}, "ghosts": {}, "networks": 0, "flows": {},
          "boiler_water_min": -1, "boiler_coal_min": -1, "research": ""}
    st.update(kw)
    return st


def live():
    """THE OPERATOR'S BASE AT THE MOMENT IT DEADLOCKED. Reproduces every number in the log."""
    return state(
        counts={"boiler": 2, "steam-engine": 4, "offshore-pump": 1,
                "electric-mining-drill": 16, "inserter": 113, "lab": 9,
                "stone-furnace": 28, "small-electric-pole": 69, "transport-belt": 421},
        status={"stone-furnace": {"full_output": 28}, "lab": {"missing_science_packs": 9}},
        status_type={"mining-drill": {"working": 1, "waiting_for_space_in_destination": 15}},
        recipes={"iron-plate": {"full_output": 16}, "copper-plate": {"full_output": 12}},
        ghosts={"lab": 25}, networks=1, boiler_water_min=200, boiler_coal_min=5,
        research="logistics-2",
        drills_by_ore={"iron-ore": {"electric-mining-drill": 6},
                       "copper-ore": {"electric-mining-drill": 6},
                       "coal": {"electric-mining-drill": 4}},
        flows={"iron-plate": 83.0, "copper-plate": 54.0, "coal": 120.0, "stone": 0.0,
               "iron-ore": 94.0, "copper-ore": 98.0, "automation-science-pack": 0.0,
               "logistic-science-pack": 0.0})


def coal_short():
    """The same base with a genuine COAL constraint and nothing else in the way: ONE coal drill
    (30/min) behind a 7.2 MW plant that burns 108/min flat out plus 37.8/min of fed furnaces.

    Power is deliberately ample (4 boilers / 8 engines against 3.55 MW of load = 2.03 headroom)
    so that coal is the only thing binding. That also makes an ELECTRIC coal outpost legal
    here, which is what the relief ladder should reach for - this fixture is where the
    no-tier-regression law gets exercised rather than dodged."""
    st = live()
    st["counts"]["boiler"], st["counts"]["steam-engine"] = 4, 8
    st["drills_by_ore"]["coal"] = {"electric-mining-drill": 1}
    st["flows"]["coal"] = 30.0
    return st


def both_short():
    """Coal short AND power tight at once: the live plant (3.6 MW against 3.55 MW of load) with
    a single coal drill behind it. Two constraints refuse builds in the same pass, which is
    what the deadlock detector and the attribution ranking are there to arbitrate between."""
    st = live()
    st["drills_by_ore"]["coal"] = {"electric-mining-drill": 1}
    st["flows"]["coal"] = 30.0
    return st


def jammed():
    """A mine whose drills have nowhere to drop: the lane was never laid. 16 drills, every one
    at waiting_for_space_in_destination, so `producer_live` ('is any drill WORKING') is false
    and refuses the very lane that would make them work."""
    return state(
        counts={"boiler": 2, "steam-engine": 4, "offshore-pump": 1,
                "electric-mining-drill": 16},
        status_type={"mining-drill": {"waiting_for_space_in_destination": 16}},
        networks=1, boiler_water_min=200, boiler_coal_min=5,
        drills_by_ore={"coal": {"electric-mining-drill": 4}},
        flows={"coal": 2.0})


LANE = {"item": "coal", "producer": "mining-drill", "drills": 4}


# ===================================================== the live deadlock, dissolved
def test_the_live_deadlock_was_a_modelling_error_and_the_base_has_a_legal_move():
    """The deadlock this file was written about is GONE at its root, not routed around.

    Everything downstream still blocks - science on power, labs on packs, an electric coal
    outpost on the headroom it would consume - so the base is still tightly constrained. What
    is no longer true is the first line: the plant does not burn 178 coal/min. It burns for
    its load, and one more boiler column is something the mine can fuel. That single legal
    move is the whole chain: column -> headroom -> electric coal drills -> coal -> column."""
    st = live()
    ok, why = G.gate("power_capacity", 1, st)
    assert ok, why
    assert "coal at the boiler" in why and "1.521" in why, why

    ok, why = G.gate("science_assembler", 1, st, {"recipe": "automation-science-pack"})
    assert not ok and "power_headroom" in why and "0.99" in why, why
    ok, why = G.gate("lab", 1, st)
    # Still refused. The REASON moved from the pack flow to pack_producer_live once headroom
    # became measured rather than nameplate - a lab with no pack assembler anywhere is refused
    # on the missing CONVERTER, which is the more direct truth about this base.
    assert not ok and ("pack_producer_live" in why
                       or "automation-science-pack 0.0/min < 2.0/min" in why), why
    ok, why = G.gate("mine_outpost", 4, st, {"drills": 4, "ore": "coal"})
    assert not ok and "power_headroom" in why, why
    # ...and the cycle is broken because the ONE build that raises headroom is now allowed.
    assert G.gate("power_capacity", 1, st)[0], "the base must never be out of legal moves"


def test_a_genuinely_coal_short_base_still_blocks_its_plant():
    """The correction must not become a rubber stamp. One coal drill (30/min) cannot fuel a
    3.6 MW plant that burns 54/min flat out plus 37.8/min of fed furnaces, and the gate says
    so - in terms of what the plant would burn, not of a multiplier."""
    ok, why = G.gate("power_capacity", 1, coal_short())
    assert not ok and "coal_at_boiler" in why, why
    assert "at full tilt" in why and "30/min" in why, why


# ===================================================== 1. relief-aware gating
def test_a_relief_build_is_allowed_through_the_exact_gate_that_blocks_it():
    """THE HEADLINE. The same structure, the same census, the same failing check - allowed
    only when it is declared as the build that INCREASES that check."""
    st = jammed()
    ok, why = G.gate("ore_lane", 1, st, LANE, relieves=())
    assert not ok and "producer_live" in why, why
    ok, why = G.gate("ore_lane", 1, st, LANE, relieves=("producer_live",))
    assert ok, why
    assert why.startswith("RELIEF ALLOWED"), "a waiver must never read as a plain ALLOW"
    assert "producer_live" in why and "waiting_for_space_in_destination" in why


def test_the_non_relief_version_of_the_same_build_is_still_blocked():
    """`relieves=()` is the non-relief build. Nothing else about it differs."""
    st = jammed()
    for rel in ((), ("coal_at_boiler",), ("flows:coal",), ("power_headroom",)):
        ok, why = G.gate("ore_lane", 1, st, LANE, relieves=rel)
        assert not ok, "relieves=%r waived a check it does not cover: %s" % (rel, why)
        assert "producer_live" in why


def test_a_relief_claim_is_refused_when_the_build_cannot_actually_help():
    """Condition 3. A lane relieves `producer_live` only where the producer EXISTS and is
    jammed on its output - that is the exact discriminator between "the drill is idle because
    this lane is missing" and the duplicate lane ahead of its producer that was 72.4% of the
    127 belts the operator deleted."""
    st = jammed()
    st["status_type"] = {"mining-drill": {"no_power": 16}}      # idle, but not for want of a lane
    ok, why = G.gate("ore_lane", 1, st, LANE, relieves=("producer_live",))
    assert not ok and "producer_live" in why, why
    st2 = jammed()
    st2["status_type"] = {}
    st2["counts"].pop("electric-mining-drill")                  # no producer at all
    ok, why = G.gate("ore_lane", 1, st2, LANE, relieves=("producer_live",))
    assert not ok, why


def test_relief_never_waives_a_check_the_build_would_make_worse():
    """Condition 3, the other way round: a boiler column BURNS coal, so it may not waive the
    coal check no matter what it claims to relieve."""
    st = coal_short()
    ok, why = G.gate("power_capacity", 1, st,
                     {"projected_load_kw": 0.0}, relieves=("coal_at_boiler",))
    assert not ok and "coal_at_boiler" in why, why
    inert, msg = G.relief_inert("coal_at_boiler", st, "power_capacity", 1, {})
    assert not inert and "BURNS" in msg
    # ...and a furnace row may not waive the overbuild ratio it is itself raising.
    inert, msg = G.relief_inert("overbuild_within_budget:iron-ore", st, "smelter_array", 12,
                                {"ore": "iron-ore"})
    assert not inert and "raises the ratio" in msg


def test_relief_keys_carry_their_item_so_one_flow_never_waives_another():
    """`flows` is one predicate over twelve items. A converter relieves the PACK flow and
    consumes the PLATE flow; a bare predicate name would have let it build with no plates."""
    st = live()
    rel = G.relieves_for("science_assembler", {"recipe": "automation-science-pack"})
    assert "flows:automation-science-pack" in rel
    assert "flows:iron-plate" not in rel and "flows:copper-plate" not in rel
    inert, msg = G.relief_inert("flows:iron-plate", st, "science_assembler", 1, {})
    assert not inert and "itself consumes" in msg
    inert, _ = G.relief_inert("flows:automation-science-pack", st, "ore_lane",
                              1, {"item": "automation-science-pack"})
    assert inert


def test_a_bare_predicate_name_never_covers_a_keyed_constraint():
    """`relieves=('flows',)` must not waive `flows:iron-plate`. That looseness is exactly what
    would turn the exemption into a bypass, so coverage is EXACT keys plus an explicit
    '<check>:*' wildcard and nothing else."""
    assert G._covers(("flows",), "flows:coal") is None
    assert G._covers(("flows:coal",), "flows:coal") == "flows:coal"
    assert G._covers(("flows:*",), "flows:coal") == "flows:*"
    assert G._covers(("flows:iron-plate",), "flows:coal") is None
    assert G._covers(("producer_live",), "producer_live") == "producer_live"
    st = jammed()
    ok, _why = G.gate("ore_lane", 1, st, LANE, relieves=("producer_live:*",))
    assert not ok, "a wildcard on an unkeyed predicate must not match"


def test_the_default_relief_set_is_derived_not_assumed():
    """`relieves=None` - the default every stage uses - DERIVES the set from relieves_for, so
    a build carries its own honest relief claim and a caller cannot invent one. `relieves=()`
    switches relief off entirely, which is what the cycle search uses internally so that a
    waiver can never justify itself."""
    st = jammed()
    assert "producer_live" in G.relieves_for("ore_lane", LANE)
    assert G.gate("ore_lane", 1, st, LANE)[0] is True            # derived
    assert G.gate("ore_lane", 1, st, LANE, relieves=())[0] is False
    rep = G.explain("ore_lane", 1, st, LANE)
    assert rep["waived"] == ["producer_live"] and rep["relieves"]
    rep = G.explain("ore_lane", 1, st, LANE, relieves=())
    assert rep["waived"] == [] and rep["relieves"] == []


def test_a_check_with_no_inertness_rule_is_never_waivable():
    """LAW 1's sink half, P2's single network, the research queue: facts about the world, not
    budgets. Nothing may claim to be neutral toward them."""
    st = live()
    for key in ("sink_exists:iron-plate", "grid_single", "research_queued", "water_source",
                "lane_capacity", "headroom_after", "labs_satisfied", "chest_is_terminus"):
        inert, msg = G.relief_inert(key, st, "ore_lane", 1, {})
        assert not inert, "%s became waivable" % key
        assert "never waivable" in msg


def test_headroom_after_is_not_relievable_by_the_column_it_sizes():
    """power_capacity raises power_headroom - but `headroom_after` is a SUFFICIENCY test on
    the very column being asked for. Waiving it would authorise unlimited columns."""
    rel = G.relieves_for("power_capacity")
    assert "power_headroom" in rel and "headroom_after" not in rel
    st = state(counts={"boiler": 1, "steam-engine": 2, "offshore-pump": 1,
                       "electric-mining-drill": 40},
               networks=1, boiler_water_min=200, boiler_coal_min=5,
               drills_by_ore={"coal": {"electric-mining-drill": 40}},
               flows={"coal": 1200.0})
    ok, why = G.gate("power_capacity", 1, st, {"projected_load_kw": 0.0})
    assert not ok and "headroom_after" in why, why


def test_the_relief_exemption_needs_a_real_cycle():
    """Condition 2. If a LEGAL move already fixes the check, take that move - no waiver.
    Here the drills are jammed AND a second, unblocked producer exists, so producer_live has
    a proper fix and the shortcut is refused."""
    st = jammed()
    calls = {"n": 0}
    real = G.relief_candidates

    def candidates(key, s=None):
        if key == "producer_live":
            calls["n"] += 1
            return [{"structure": "mine_outpost", "params": {"ore": "coal"}, "n": 1,
                     "key": "mine:coal", "why": "a legal outpost"}]
        return real(key, s)
    G.relief_candidates = candidates
    try:
        # capacity 3.6 MW against 16 drills = 1.44 MW: an electric outpost passes cleanly,
        # so producer_live is fixable and the lane's relief claim must be refused.
        ok, why = G.gate("ore_lane", 1, st, LANE, relieves=("producer_live",))
        assert calls["n"] >= 1, "the cycle search never ran"
        assert not ok, "a waiver was granted while a legal fix existed: %s" % why
    finally:
        G.relief_candidates = real


def test_a_burner_outpost_is_not_charged_for_power_it_does_not_draw():
    """The edge whose removal breaks the live cycle. burner-mining-drill is in NON_ELECTRIC:
    0 kW, 15 ore/min, no pole, no network. The gate table charges 90 kW because it cannot know
    the tier; params={'drill': ...} tells it."""
    st = live()
    assert G.adds_kw_for("mine_outpost", {"drill": "burner-mining-drill"}) == 0.0
    assert G.adds_kw_for("mine_outpost", {"drill": "electric-mining-drill"}) == 90.0
    assert G.adds_kw_for("mine_outpost", None) == 90.0, "the default must stay conservative"
    ok, why = G.gate("mine_outpost", 4, st, {"drills": 4, "ore": "coal",
                                             "drill": "electric-mining-drill"})
    assert not ok and "power_headroom" in why
    ok, why = G.gate("mine_outpost", 4, st, {"drills": 4, "ore": "coal",
                                             "drill": "burner-mining-drill"})
    assert ok, why
    assert "0 kW" in why or "cannot make it worse" in why


def test_the_headroom_check_is_marginal_not_absolute():
    """It exists to stop a build EATING the headroom its own load needs. A build that adds
    nothing does not appear in the ratio, so refusing it is a pure false negative - and it is
    what refused every zero-cost relief on a base whose headroom only a boiler column could
    fix and only more coal could pay for."""
    st = live()
    assert G.headroom(st) < G.POWER_HEADROOM_MIN
    ok, msg = G._pr_power_headroom(st, 1, G.GATES["mine_outpost"],
                                   {"structure": "mine_outpost",
                                    "drill": "burner-mining-drill"})
    assert ok and "ALREADY below" in msg and "cannot make it worse" in msg
    ok, msg = G._pr_power_headroom(st, 1, G.GATES["mine_outpost"],
                                   {"structure": "mine_outpost"})
    assert not ok and "boiler column FIRST" in msg


def test_coal_supply_is_what_the_mine_can_deliver_not_last_minutes_throughput():
    """On a BACK-PRESSURED base production equals consumption, not capacity: measured live at
    2 coal/min off four drills whose belts were 100% full. Reading a full pipe as an empty one
    is how this gate demanded 'mine more coal' from a mine standing idle."""
    st = state(counts={"boiler": 1, "steam-engine": 2, "offshore-pump": 1},
               networks=1, boiler_water_min=200, boiler_coal_min=5,
               drills_by_ore={"coal": {"electric-mining-drill": 4}},
               flows={"coal": 2.0})          # 4 drills = 120/min; the belt is full, so 2/min
    assert G.drill_capacity_per_min(st, "coal") == 120.0
    assert G.flow(st, "coal") == 2.0
    ok, why = G.gate("power_capacity", 1, st, {"projected_load_kw": 0.0})
    assert ok, why
    assert "120/min the coal drills can deliver" in why, why
    # ...and a mine that genuinely cannot cover the demand still blocks. Four columns are
    # charged at full tilt (4 * 27 = 108/min needed at 1.5x) against one drill's 30/min: you
    # do not build capacity to leave it idle, so the plan must be able to fuel what it adds.
    st["drills_by_ore"] = {"coal": {"electric-mining-drill": 1}}
    ok, why = G.gate("power_capacity", 4, st, {"projected_load_kw": 0.0})
    assert not ok and "coal_at_boiler" in why, why


# ===================================================== 2. next_relief
def test_next_relief_picks_the_coal_lane_in_the_live_shaped_scenario():
    """The whole point: the base that had NO legal move now has one, and it is the cheapest
    one - belts, 0 kW, no new machine, and plan_supply refuses a duplicate outright."""
    r = G.next_relief(coal_short(), attribution={})
    assert r is not None, "the coal-short base was handed no legal move"
    assert r["structure"] == "ore_lane"
    assert r["params"]["item"] == "coal"
    assert r["constraint"] == "coal_at_boiler"
    assert "power_capacity" in r["unblocks"]
    assert r["key"] == "lane:coal"


def test_next_relief_escalates_to_an_electric_outpost_never_a_burner_one():
    """`done` is the ladder: once the coal lane is built the relief escalates to more drills.
    They must be ELECTRIC ones.

    This is the defect the fix exists to prevent. The first autonomous action the deadlock fix
    made possible was 12 BURNER coal drills, on a map where the operator had converted every
    burner drill to electric and deleted the fuel belts that feed them - handing him back the
    exact infrastructure he had just removed."""
    r = G.next_relief(coal_short(), done={"lane:coal"}, attribution={})
    assert r is not None and r["structure"] == "mine_outpost", r["structure"]
    assert r["params"]["ore"] == "coal"
    assert r["params"]["drill"] == "electric-mining-drill", r["params"]


def test_a_truly_cornered_base_reports_a_deadlock_rather_than_regressing():
    """`both_short` is genuinely cornered: coal cannot fuel another column, and the grid cannot
    carry another electric drill. The ladder's honest answer is NO MOVE - which the deadlock
    detector reports to the operator - and emphatically not a burner outpost smuggled in as
    progress. A relief that undoes the operator's work is worse than admitting we are stuck."""
    r = G.next_relief(both_short(), done={"lane:coal"}, attribution={})
    assert r is None, r
    d = G.deadlock(both_short(), attribution={})
    assert d is not None and "burner" not in d["line"], d


def test_relief_drill_never_regresses_to_burner_once_electric_stands():
    """Electric where the grid carries it; None - not burner - where it cannot, because a
    burner outpost is infrastructure this base already tore out once."""
    st = live()
    st["counts"]["boiler"], st["counts"]["steam-engine"] = 20, 40      # 18 MW
    assert G.relief_drill(st, 6) == "electric-mining-drill"
    st["counts"]["boiler"], st["counts"]["steam-engine"] = 2, 4
    assert G.relief_drill(st, 6) is None, "a burner relief undoes the operator's conversion"


def test_relief_drill_allows_burner_on_a_base_that_has_never_had_electric():
    """The no-regression law is about REGRESSION. A fresh start with no electric drill and no
    power has to begin somewhere, and there the burner tier is genuinely the only one."""
    st = {"counts": {"burner-mining-drill": 4}, "networks": 0, "flows": {}}
    assert G.relief_drill(st, 2) == "burner-mining-drill"


def test_next_relief_is_ranked_by_bottleneck_attribution():
    """bottleneck.py is the only input measured AT THE MACHINES. When the starved machines say
    they are missing coal, the coal constraint outranks one that merely blocks more gate rows.
    """
    st = both_short()
    rows = G.blocking_constraints(st)
    by = {r["constraint"]: r for r in rows}
    assert by["power_headroom"]["n_blocks"] > by["coal_at_boiler"]["n_blocks"], \
        "breadth alone should have favoured power_headroom"
    hot = G._attribution_keys({"recipe": "iron-plate", "missing": "coal"})
    assert "coal_at_boiler" in hot and "flows:coal" in hot
    r = G.next_relief(st, attribution={"recipe": "iron-plate", "missing": "coal"})
    assert r["constraint"] == "coal_at_boiler" and r["attributed"] is True
    assert r["score"] >= G.ATTRIBUTION_WEIGHT


def test_next_relief_never_proposes_a_build_its_own_gate_refuses():
    """A relief is a legal move or it is not a relief. Every candidate is gated before it is
    handed back."""
    st = live()
    for _ in range(6):
        r = G.next_relief(st, done=set(), attribution={})
        if r is None:
            break
        ok, why = G.gate(r["structure"], r["n"], st, r["params"],
                         relieves=(r["constraint"],))
        assert ok, "next_relief proposed a refused build: %s" % why
        break


def test_next_relief_returns_none_when_nothing_is_blocked():
    st = state(counts={"boiler": 9, "steam-engine": 18, "offshore-pump": 1}, networks=1,
               boiler_coal_min=50, boiler_water_min=200, flows={"coal": 900.0})
    assert G.blocking_constraints(st, structures=["power_capacity", "power_grid"]) == []
    assert G.next_relief(st, structures=["power_capacity", "power_grid"],
                         attribution={}) is None


def test_next_relief_survives_a_broken_bottleneck_ring():
    """Attribution SHARPENS the choice; it never gates it. A bottleneck module that raises
    must not take the last legal move away from a stuck base."""
    import sys
    import types
    stub = types.ModuleType("bottleneck")

    def boom(window_s=600):
        raise RuntimeError("bottleneck ring unreadable")
    stub.top_cause = boom
    saved = sys.modules.get("bottleneck")
    sys.modules["bottleneck"] = stub
    try:
        assert G._live_attribution() is None, "the guard swallowed nothing"
        r = G.next_relief(coal_short())                # attribution=None -> the lazy path
    finally:
        if saved is None:
            sys.modules.pop("bottleneck", None)
        else:
            sys.modules["bottleneck"] = saved
    assert r is not None and r["structure"] == "ore_lane"


# ===================================================== 3. stage order
def test_no_power_stages_are_attempted_before_power_gated_ones():
    """Builds that need no power - belts, lanes, splitters, poles, burner drills, boilers -
    must all be attemptable before anything gated on headroom."""
    order = [n for n, _fn in planner.PHASE0_STAGES]
    powered = [n for n in order if (planner.STAGE_SPEC.get(n) or {}).get("power")]
    assert powered, "no stage is marked power-gated - the table stopped meaning anything"
    first_powered = min(order.index(n) for n in powered)
    for n in planner.NO_POWER_STAGES:
        assert order.index(n) < first_powered or n in powered, \
            "no-power stage %s runs after the first power-gated stage" % n
    for n in ("electrify", "red_science", "science"):
        assert n in powered, "%s is gated on power headroom and must be marked so" % n


def test_the_coal_lane_precedes_plant_expansion():
    """A boiler column burns 27 coal/min and power_capacity's own gate refuses one with no
    coal behind it. That is the live deadlock in one edge."""
    order = [n for n, _fn in planner.PHASE0_STAGES]
    assert order.index("coal_lane") < order.index("plant_expand")
    assert order.index("mines") < order.index("coal_lane")
    assert order.index("plant") < order.index("plant_expand")


def test_relief_runs_before_the_stages_it_unblocks():
    order = [n for n, _fn in planner.PHASE0_STAGES]
    for later in ("mines", "arrays", "ore_lanes", "coal_lane", "plant_expand", "science"):
        assert order.index("relief") < order.index(later)


def test_every_stage_is_still_present_and_unique():
    order = [n for n, _fn in planner.PHASE0_STAGES]
    assert len(order) == len(set(order))
    for n in ("world", "plant", "spine", "mines", "arrays", "array_grid", "ore_lanes",
              "coal_lane", "science", "electrify", "oil", "red_science"):
        assert n in order, "stage %s was dropped" % n


def test_a_dependency_is_a_precondition_not_an_ordering():
    """Ordering alone is not a dependency. Each of these is only meaningful after another
    stage, and each says so - with a reason a human can act on."""
    st = state()
    for name in ("spine", "array_grid", "coal_lane", "plant_expand", "electrify",
                 "red_science", "science"):
        pre = planner.STAGE_SPEC[name]["pre"]
        ok, why = pre({}, st)
        assert not ok and why, "%s has an empty precondition" % name
        assert "first" in why or "no " in why, why


def test_plant_columns_are_sized_by_the_same_inequality_the_gate_uses():
    """The stage asked for 1 column while its own gate wanted 4 and refused every one: the
    planner treated PROJECTED_LOAD_KW as the total load and ignored 3.55 MW already drawing."""
    st = live()
    cols = planner.plant_columns_needed(st)
    assert cols == 4, cols
    g = G.GATES["power_capacity"]
    ok, _ = G._pr_headroom_after(st, cols, g, {"projected_load_kw": planner.PROJECTED_LOAD_KW})
    assert ok, "the column count the stage asks for is one its own gate refuses"
    ok, _ = G._pr_headroom_after(st, cols - 1, g,
                                 {"projected_load_kw": planner.PROJECTED_LOAD_KW})
    assert not ok, "the count is not minimal"
    # a fresh map is unchanged: current load is zero, so both readings agree
    fresh = planner.plant_columns_needed({"counts": {}})
    assert fresh * G.BOILER_MW >= (planner.PROJECTED_LOAD_KW / 1000.0) * G.POWER_HEADROOM_MIN
    assert planner.plant_columns_needed({"counts": {"boiler": 9, "steam-engine": 18}}) == 0


# ===================================================== 4. the deadlock detector
class _Log:
    def __init__(self):
        self.lines = []

    def __call__(self, msg):
        self.lines.append(str(msg))

    def has(self, *subs):
        return any(all(s in ln for s in subs) for ln in self.lines)

    def count(self, sub):
        return sum(1 for ln in self.lines if sub in ln)


class _Ctx:
    """planner with its logging captured and its world replaced by a synthetic census."""

    def __init__(self, st=None, phase=None):
        self.log = _Log()
        self._saved = []
        self.patch(planner.status, "log", self.log)
        self.patch(planner, "PHASE_FILE", HERE / "_test_gate_relief_phase.json")
        self.patch(planner.build_gates, "sense", lambda force=False, **k: st or live())
        self.patch(planner, "save", lambda p: None)
        self.patch(planner.B, "operator_present", lambda: False)
        self.patch(planner.A, "purpose", lambda *a, **k: None)
        planner.gate_reset()
        planner.pass_reset()

    def patch(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def close(self):
        for obj, name, value in reversed(self._saved):
            setattr(obj, name, value)
        planner.gate_reset()
        planner.pass_reset()


def _with_ctx(fn):
    def wrapper():
        ctx = _Ctx()
        try:
            fn(ctx)
        finally:
            ctx.close()
    wrapper.__name__ = fn.__name__
    return wrapper


@_with_ctx
def test_the_deadlock_detector_fires_once_per_stuck_pass(ctx):
    ctx.patch(planner.build_gates, "sense", lambda force=False, **k: both_short())
    p = {}
    ctx.patch(planner, "PHASE0_STAGES", (
        ("plant", lambda q: planner.gate("power_capacity", 1,
                                         params={"projected_load_kw": 0.0})),
        ("science", lambda q: planner.gate("science_assembler", 1,
                                           params={"recipe": "automation-science-pack"})),
    ))
    planner.phase0(p)
    assert ctx.log.count("DEADLOCK:") == 1, ctx.log.lines
    assert ctx.log.has("DEADLOCK:", "is the binding limit", "relief build =")
    assert ctx.log.has("DEADLOCK:", "coal_at_boiler")
    assert p.get("relief"), "the detector named a relief but never recorded it"
    assert p["relief"]["structure"] == "ore_lane"


@_with_ctx
def test_the_detector_is_silent_when_the_pass_built_something(ctx):
    """Zero verified builds AND at least one refusal. One build makes it not a deadlock."""
    p = {}

    def blocked(q):
        planner.gate("power_capacity", 1, params={"projected_load_kw": 0.0})

    def built(q):
        planner.verified({"status": "verified", "verify": {"check": {"detail": "ok"}}}, "x")
    ctx.patch(planner, "PHASE0_STAGES", (("a", blocked), ("b", built)))
    planner.phase0(p)
    assert not ctx.log.has("DEADLOCK:")
    assert "relief" not in p


@_with_ctx
def test_the_detector_is_silent_when_nothing_was_refused(ctx):
    p = {}
    ctx.patch(planner, "PHASE0_STAGES", (("a", lambda q: None),))
    planner.phase0(p)
    assert not ctx.log.has("DEADLOCK:")


@_with_ctx
def test_a_stale_relief_is_dropped_the_moment_the_pass_makes_progress(ctx):
    p = {"relief": {"structure": "ore_lane", "n": 1, "key": "lane:coal",
                    "params": {"item": "coal"}, "constraint": "coal_at_boiler"}}
    ctx.patch(planner, "PHASE0_STAGES",
              (("a", lambda q: planner.verified({"status": "verified", "verify": {}}, "x")),))
    planner.phase0(p)
    assert "relief" not in p


@_with_ctx
def test_the_recorded_relief_is_attempted_on_the_next_pass(ctx):
    """'attempt that relief build next pass', literally: stage_relief executes what the
    detector recorded, through the normal gated/planned/verified path."""
    seen = {}
    ctx.patch(planner, "RELIEF_EXECUTORS", {
        "ore_lane": lambda p, r, rel: seen.setdefault("call", (r["params"], rel)) or True})
    p = {"relief": {"structure": "ore_lane", "n": 1, "key": "lane:coal",
                    "params": {"item": "coal"}, "constraint": "coal_at_boiler",
                    "unblocks": ["power_capacity"]}}
    planner.stage_relief(p)
    assert seen["call"] == ({"item": "coal"}, ("coal_at_boiler",))
    assert ctx.log.has("relief: attempting ore_lane", "coal_at_boiler")
    assert ctx.log.has("relief: lane:coal BUILT")
    assert p["relief_done"] == ["lane:coal"]
    assert "relief" not in p, "the record must be consumed, not retried forever"


@_with_ctx
def test_an_unexecutable_relief_escalates_instead_of_looping(ctx):
    """A relief the planner cannot execute is recorded as TRIED, which is what makes
    next_relief hand out the next rung of the ladder instead of the same move forever."""
    ctx.patch(planner, "RELIEF_EXECUTORS", {"ore_lane": lambda p, r, rel: False})
    p = {"relief": {"structure": "ore_lane", "n": 1, "key": "lane:coal",
                    "params": {"item": "coal"}, "constraint": "coal_at_boiler"}}
    planner.stage_relief(p)
    assert p["relief_tried"] == ["lane:coal"]
    assert ctx.log.has("not executable - escalating")
    nxt = G.next_relief(coal_short(), done=set(p["relief_tried"]), attribution={})
    assert nxt["structure"] == "mine_outpost" and nxt["params"]["ore"] == "coal"


@_with_ctx
def test_stage_relief_is_a_noop_on_a_healthy_base(ctx):
    ctx.patch(planner, "RELIEF_EXECUTORS", {"ore_lane": lambda p, r, rel: 1 / 0})
    planner.stage_relief({})                      # must not raise, must not log
    assert not ctx.log.lines


@_with_ctx
def test_an_unmet_precondition_is_logged_not_silent(ctx):
    """Nine stages used to die without a single log line: the operator saw four blocked gates
    and no hint that five more stages had never run at all."""
    p = {}
    ctx.patch(planner, "PHASE0_STAGES", (("coal_lane", lambda q: 1 / 0),))
    planner.phase0(p)
    assert ctx.log.has("stage coal_lane: SKIPPED", "no coal mine recorded")


@_with_ctx
def test_a_relief_gate_logs_distinctly(ctx):
    """'gate RELIEF: allowing X because it increases C' - an exemption that reads like an
    ordinary pass is an exemption nobody can audit."""
    ctx.patch(planner.build_gates, "sense", lambda force=False, **k: jammed())
    planner.gate_reset()
    assert planner.gate("ore_lane", 1, params=LANE, relieves=("producer_live",))
    assert ctx.log.has("gate RELIEF:", "allowing ore_lane x1", "because it increases",
                       "producer_live")
    assert not ctx.log.has("gate ALLOW:")
    assert not planner.gate("ore_lane", 1, params=LANE, relieves=())
    assert ctx.log.has("gate BLOCK:")


@_with_ctx
def test_every_refusal_in_a_pass_is_recorded_for_the_detector(ctx):
    ctx.patch(planner.build_gates, "sense", lambda force=False, **k: both_short())
    planner.pass_reset()
    planner.gate("power_capacity", 1, params={"projected_load_kw": 0.0})
    planner.gate("science_assembler", 1, params={"recipe": "automation-science-pack"})
    keys = [k for b in planner._PASS["blocked"] for k in b["blocking"]]
    assert "coal_at_boiler" in keys and "power_headroom" in keys
    assert planner._PASS["built"] == 0


# ===================================================== 5. everything else is preserved
def test_the_laws_still_refuse_what_the_operator_deleted():
    """LAW 5 is a narrow exemption, not a bypass: the builds his optimization condemned are
    still refused, on a census where no relief is declared."""
    st = live()
    ok, why = G.gate("lab", 9, st)
    # Still refused - which is the law this test is about. The REASON now leads with
    # pack_producer_live rather than the pack flow, because with headroom measured the power
    # clause no longer fires first. The lab array he deleted stays deleted either way.
    assert not ok and ("automation-science-pack" in why or "pack_producer_live" in why)
    st2 = state(counts={"boiler": 4, "steam-engine": 8, "electric-mining-drill": 6},
                networks=2)
    ok, why = G.gate("mine_outpost", 6, st2, {"drills": 6})
    assert not ok and "SPLIT" in why                             # net 405
    ok, why = G.gate("overflow_chest", 1, st, {"lane_chests": 1, "is_terminus": True})
    assert not ok and "budget is 1" in why                       # LAW 4
    ok, why = G.gate("plate_lane", 1, state(), {"product": "iron-plate"})
    assert not ok and "nothing built consumes" in why            # LAW 1's sink half


def test_a_gate_that_cannot_evaluate_still_refuses():
    st = live()
    ok, why = G.gate("science_assembler", 1, st, {"recipe": "military-science-pack"},
                     relieves=("flows:iron-plate", "power_headroom", "sink_exists:*"))
    assert not ok and "cannot evaluate" in why, why


def test_relief_never_reaches_a_structure_that_declares_none():
    for s in ("overflow_chest", "mall_assembler"):
        assert G.relieves_for(s) == {}, s


def test_the_builder_safe_mode_and_the_truce_are_untouched():
    """The kill switch still exists and still works. Its DEFAULT flipped on 2026-08-30 when
    the operator withdrew "zero unrequested building" - but a switch you would have to re-add
    under pressure is a switch you do not have, so the guard itself is pinned here."""
    src = (HERE / "planner.py").read_text()
    assert 'os.environ.get("BUILDER_ENABLED", "1") != "1"' in src
    assert src.count("B.operator_present()") >= 3           # truce: play + phase0 + queue
    assert "if not verified(rec" in src or "verified(rec" in src
    for call in ("A.place(", "A.build(", "rcon.run(", "script.on_event", "on_nth_tick"):
        assert call not in src, "planner.py gained %s" % call
    # the only NEW Lua this change adds is a read; prove it
    assert "find_entities_filtered" in src
    for banned in ("create_entity{", "destroy()", ".insert{", "set_recipe("):
        assert banned not in src, "planner.py Lua gained %r" % banned


def test_build_gates_is_still_read_only():
    src = (HERE / "build_gates.py").read_text()
    for banned in ("script.on_event", "on_nth_tick", "e.destroy", "remove_item"):
        assert banned not in src, "build_gates.py gained %r" % banned
    # create_entity appears ONLY inside reserve()'s RETURNED (never executed) ghost Lua and
    # in the docstring that says so; nothing in this module ever runs it.
    assert "create_entity{" in src and src.count("create_entity{") == 1
    assert "_ghost_lua" in src and "RETURNED, NEVER RUN" in src


def test_relief_tables_only_name_real_predicates_and_structures():
    """A typo in a relief table would be a silent waiver of nothing, or of the wrong thing."""
    for s in sorted(G.GATES):
        for key in G.relieves_for(s, {"ore": "coal", "item": "coal", "product": "iron-plate"}):
            assert G.constraint_check(key) in G.PREDICATES, "%s -> %s" % (s, key)
    for check in G.RELIEF_INERT:
        assert check in G.PREDICATES, check
    seen = set()
    for check in list(G.PREDICATES):
        for cand in G.relief_candidates(check, live()):
            seen.add(cand["structure"])
    assert seen and seen <= set(G.GATES), sorted(seen - set(G.GATES))


def test_relief_candidates_are_ordered_delivery_before_production():
    """A belt costs 0 kW, 0 ore and no machine; drills cost a patch and a tier decision."""
    cands = G.relief_candidates("coal_at_boiler", coal_short())
    assert [c["structure"] for c in cands] == ["ore_lane", "mine_outpost"]
    cands = G.relief_candidates("flows:iron-ore", live())
    assert cands[0]["structure"] == "ore_lane"


def test_deadlock_says_so_when_no_relief_is_legal():
    """'NONE IS LEGAL' is a real answer and must be said out loud, never swallowed into a
    silent idle."""
    st = live()
    real = G.next_relief
    G.next_relief = lambda *a, **k: None
    try:
        d = G.deadlock(st, attribution={})
    finally:
        G.next_relief = real
    assert d is not None
    assert "NONE IS LEGAL" in d["line"] and "needs a human" in d["line"]


def test_deadlock_returns_none_when_nothing_is_blocked():
    st = state(counts={"boiler": 9, "steam-engine": 18, "offshore-pump": 1}, networks=1,
               boiler_coal_min=50, boiler_water_min=200, flows={"coal": 900.0})
    assert G.deadlock(st, structures=["power_capacity", "power_grid"], attribution={}) is None


def test_the_deadlock_line_names_the_constraint_and_the_build():
    d = G.deadlock(coal_short(), attribution={})
    assert d["line"].startswith("DEADLOCK: coal_at_boiler is the binding limit")
    assert "relief build = ore_lane x1" in d["line"]
    assert "power_capacity" in d["line"]


# ============================================================ ADVERSARIAL VERIFY (2026-08-30)
# Three defects the first cut of LAW 5 shipped with, each reproduced here from the LIVE census
# before its fix is asserted.

# --------------------------------------------------------------- 1. the ghost-sink false sink
def test_a_ghost_lab_is_not_a_sink_for_plates():
    """LAW 1's sink half, and the one hole the relief work opened that LAW 5 does NOT guard.

    `_pr_sink_exists` counted a GHOST of any crafting machine - including `lab` - as a
    committed sink for ANY product. The operator's base carries 25 reserved lab ghosts, so
    `plate_lane` was ALLOWED with nothing at the far end: a 33-tile conveyor to a chest,
    wearing LAW 3's clothes. A lab consumes science packs and nothing else.
    """
    ok, why = G.gate("plate_lane", 1, live(), {"product": "iron-plate"})
    assert not ok, why
    assert "nothing built consumes iron-plate" in why
    # ...and it stays refused however the caller decorates it, because `sink_exists` has no
    # inertness rule and is therefore never waivable at all.
    for rel in (("sink_exists:iron-plate",), ("sink_exists:*",), ("flows:*", "sink_exists:*")):
        assert not G.gate("plate_lane", 1, live(), {"product": "iron-plate"}, relieves=rel)[0]


def test_a_ghost_assembler_is_still_a_sink_for_plates():
    """The intent the ghost branch was written for SURVIVES: a lane laid toward a reserved
    converter block is a lane with a destination. Only `lab` left the list."""
    st = live()
    st["ghosts"] = {"assembling-machine-1": 4}
    ok, why = G.gate("plate_lane", 1, st, {"product": "iron-plate"})
    assert ok, why
    assert "4 ghost crafting machine(s)" in why


def test_ghost_labs_are_still_the_sink_for_PACKS():
    """The pack branch is untouched - it is what dissolves the assembler/lab deadlock."""
    st = live()
    st["counts"]["lab"] = 0
    ok, why = G._pr_sink_exists(st, 1, G.GATES["science_assembler"],
                                {"product": "automation-science-pack"})
    assert ok and "25 ghost lab(s)" in why, why


# ------------------------------------------------- 2. the tier the mines stage never declared
def test_a_burner_outpost_is_legal_on_a_base_with_no_power_at_all():
    """The no-power build the stage order promises to attempt FIRST. `adds_kw_for` made this
    possible; the mines stage has to actually say which drill it means for it to happen."""
    dead = state(flows={"coal": 0.0}, drills_by_ore={})
    ok, why = G.gate("mine_outpost", 8, dead,
                     {"drills": 8, "ore": "iron-ore", "drill": "burner-mining-drill"})
    assert ok, why
    assert "0 kW added" in why
    # the electric tier on the same dead base is still refused, which is the whole point
    assert not G.gate("mine_outpost", 8, dead,
                      {"drills": 8, "ore": "iron-ore",
                       "drill": "electric-mining-drill"})[0]


@_with_ctx
def test_the_mines_stage_declares_the_drill_TIER_to_the_gate(ctx):
    """It did not, so on a pre-power base every burner outpost was charged 90 kW per drill and
    refused - the sub-bug the fix identified, still live in the stage that hits it."""
    seen = []
    ctx.patch(planner, "gate", lambda s, n=1, params=None, **k: (seen.append((s, params))
                                                                or False))
    ctx.patch(planner.B, "STATE", {"iron-ore": (10, 10, 9), "copper-ore": (20, 20, 9),
                                   "coal": (30, 30, 9)})
    ctx.patch(planner.B, "_tech_done", lambda name: False)          # -> burner tier
    planner.stage_mines({})
    assert seen, "stage_mines gated nothing"
    for structure, params in seen:
        assert structure == "mine_outpost"
        assert params.get("drill") == planner.BURNER_DRILL, params
        assert params.get("ore"), params


# ------------------------------------------------------- 3. the plant that could never expand
def _adoptable(ctx, standing=True, missing=()):
    """planner with a STANDING 2-column plant at the operator's own shore, no phase record."""
    ctx.patch(planner, "_live_pump", lambda: (-32, 51))
    ctx.patch(planner, "_plant_standing", lambda plan: (standing, list(missing)))
    return {}


@_with_ctx
def test_the_standing_plant_is_adopted_from_the_world(ctx):
    """phase.json is bookkeeping, not evidence. Adopting only the plant's POLES was not enough:
    `stage_plant` saw capacity and deferred to `stage_plant_expand`, which demanded a buildplan
    record, and `stage_coal_lane` demanded a `coal_intake` only a plan names. On the operator's
    own base BOTH were unreachable forever - the gate said ALLOW and nothing could build."""
    p = _adoptable(ctx)
    rec = planner.adopt_plant(p, live())
    assert rec["n_columns"] == 2 and rec["power_MW"] == 3.6, rec
    assert tuple(rec["anchor"]) == (-35, 45), rec          # anchor_from_pump's own measurement
    assert rec["coal_intake"] and rec["pump"] == [-32, 51]
    assert rec["adopted"] is True
    assert ctx.log.has("ADOPTED the standing 2-column plant")


@_with_ctx
def test_adoption_refuses_a_plant_it_cannot_read_back(ctx):
    """MEASURED, NOT REMEMBERED. A reconstruction whose entities are not in the ground is a
    plant on some other lattice, and adopting it would point the coal lane at a tile with no
    feeder belt on it. Nothing is recorded, and the log says which entities were missing."""
    p = _adoptable(ctx, standing=False, missing=[("boiler", -33.5, 46.0)])
    assert planner.adopt_plant(p, live()) == {}
    assert p.get("plant") in (None, {})
    assert ctx.log.has("is NOT in the ground")


@_with_ctx
def test_expansion_and_the_coal_lane_are_reachable_without_a_buildplan_record(ctx):
    """The two preconditions that were unsatisfiable on the operator's base."""
    p = _adoptable(ctx)
    ok, why = planner._pre_plant_record(p, live())
    assert ok, why
    p.setdefault("mines", {})["coal"] = {"drill": "burner-mining-drill", "n": 6,
                                         "to_xy": [1, 0]}
    ok, why = planner._pre_coal_lane(p, live())
    assert ok, why


@_with_ctx
def test_a_plant_that_cannot_be_adopted_still_refuses_to_expand(ctx):
    """No record and nothing readable in the ground = nothing to extend, said in one line
    instead of a silent return."""
    p = _adoptable(ctx, standing=False)
    ok, why = planner._pre_plant_record(p, live())
    assert not ok and "no standing plant this planner can reconstruct" in why


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
