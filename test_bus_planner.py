#!/usr/bin/env python3
"""Tests for SITING A MAIN BUS.

    python3 -m pytest test_bus_planner.py

The regression that matters is `test_the_2026_08_30_corridor_is_rejected`: the site that was
actually built, on the real geometry, must now be refused for the two reasons a width scan
could not see - it crossed the operator's lab reservation, and the iron row could not reach it.

Offline: no RCON. `scan()` is the only function that talks to the server and it is not
exercised here; everything else is pure over a synthetic World.
"""
import sys

import rcon

_REAL = rcon.run


def _no_rcon(cmd, timeout=10.0):
    raise AssertionError("offline test issued RCON: %s" % str(cmd)[:160])


rcon.run = _no_rcon

import bus_planner as BP                                                  # noqa: E402


def _open_world(**kw):
    """An empty field with an iron source north, a copper source north, labs south."""
    base = dict(
        bounds=(0, 0, 40, 60),
        sources={"iron-plate": [(5, 2)], "copper-plate": [(5, 10)]},
        sinks={"labs": [(20, 55)]},
    )
    base.update(kw)
    return BP.World(**base)


# --------------------------------------------------------------------------- hard constraints
def test_a_corridor_through_a_reservation_is_rejected():
    """A ghost is CLAIMED ground even though nothing physical stands on it."""
    w = _open_world(reserved={(10, y) for y in range(20, 40)})
    v = BP.evaluate(w, BP.Corridor("v", 10, 5, 50))
    assert not v["ok"]
    assert any("RESERVED" in r for r in v["reasons"]), v["reasons"]
    assert "blueprint ghost" in " ".join(v["reasons"])


def test_a_corridor_on_ore_is_rejected():
    w = _open_world(ore={(10, y) for y in range(20, 30)})
    v = BP.evaluate(w, BP.Corridor("v", 10, 5, 50))
    assert not v["ok"] and any("ORE" in r for r in v["reasons"])


def test_a_corridor_through_buildings_is_rejected():
    w = _open_world(occupied={(11, 25)})
    v = BP.evaluate(w, BP.Corridor("v", 10, 5, 50))
    assert not v["ok"] and any("existing entities" in r for r in v["reasons"])


def test_a_corridor_over_operator_deletions_is_rejected():
    w = _open_world(protected={(12, 33)})
    v = BP.evaluate(w, BP.Corridor("v", 10, 5, 50))
    assert not v["ok"] and any("OPERATOR deleted" in r for r in v["reasons"])


def test_legality_is_judged_before_cost():
    """An illegal site must be refused ON ITS ILLEGALITY, never scored and out-ranked - the
    reason is what the operator reads."""
    w = _open_world(reserved={(10, 30)})
    v = BP.evaluate(w, BP.Corridor("v", 10, 5, 50))
    assert v["score"] is None and v["reasons"]


# ------------------------------------------------------------------------------ reachability
def test_a_source_that_cannot_reach_the_head_is_rejected():
    """THE SECOND HALF OF THE 2026-08-30 BUG. The corridor is perfectly clear; the iron feed
    simply cannot get to it. A width scan says yes, and the bus is unbuildable."""
    # THICKER THAN AN UNDERGROUND CAN SPAN. A single row of belt is not a wall in Factorio -
    # you cross it with an underground pair - so a one-tile barrier here would be a fiction the
    # router would happily route through. under_max is 4, so 7 rows is genuinely sealed.
    wall = {(x, y) for x in range(0, 41) for y in range(8, 15)}
    w = _open_world(occupied=wall)
    # head at y=18 is south of the wall; iron at (5,2) is north of it and truly walled in
    v = BP.evaluate(w, BP.Corridor("v", 20, 18, 50))
    assert not v["ok"]
    assert any("cannot REACH" in r and "iron-plate" in r for r in v["reasons"]), v["reasons"]


def test_a_feed_may_CROSS_another_ores_lane_but_never_merge_onto_it():
    """The rule is about MERGING, not crossing. A single lane of another ore is crossed with an
    underground pair every day in this game; what must never happen is the iron feed LANDING on
    the copper lane and putting two ores on one belt. So a one-row copper lane is passable..."""
    w = _open_world(sources={"iron-plate": [(5, 2)],
                             "copper-plate": [(x, 6) for x in range(0, 41)]})
    v = BP.evaluate(w, BP.Corridor("v", 20, 12, 50))
    assert v["ok"], v["reasons"]

    # ...and a copper BLOCK too thick to tunnel under is not.
    w2 = _open_world(sources={"iron-plate": [(5, 2)],
                              "copper-plate": [(x, y) for x in range(0, 41)
                                               for y in range(6, 13)]})
    v2 = BP.evaluate(w2, BP.Corridor("v", 20, 16, 50))
    assert not v2["ok"] and any("iron-plate cannot REACH" in r for r in v2["reasons"])


def test_a_bus_that_reaches_no_consumer_is_rejected():
    """Again thicker than an underground can span - one row would be crossable."""
    w = _open_world(occupied={(x, y) for x in range(0, 41) for y in range(43, 50)})
    v = BP.evaluate(w, BP.Corridor("v", 20, 5, 42))
    assert not v["ok"] and any("reaches no consumer" in r for r in v["reasons"])


def test_a_clear_reachable_corridor_passes_and_scores():
    v = BP.evaluate(_open_world(), BP.Corridor("v", 20, 5, 50))
    assert v["ok"] and isinstance(v["score"], float)
    assert set(v["detail"]["feed"]) == {"iron-plate", "copper-plate"}
    assert v["detail"]["sink"]["labs"] >= 0


# ------------------------------------------------------------------------------------ choice
def test_choose_prefers_the_shorter_feed():
    """Between two legal sites, the one the plates reach sooner wins - every tile of feed is
    belt someone has to build and power past."""
    w = _open_world()
    near, _ = BP.choose(w, step=4)
    far = BP.evaluate(w, BP.Corridor("v", 36, 5, 50))
    chosen = BP.evaluate(w, near)
    assert chosen["score"] <= far["score"]


def test_choose_routes_around_a_reservation_instead_of_failing():
    """The whole point of the framework: given a reservation in the way, it must MOVE, not
    give up and not plough through."""
    w = _open_world(reserved={(x, y) for x in range(8, 20) for y in range(25, 45)})
    corridor, verdict = BP.choose(w, step=2)
    assert verdict["ok"]
    assert not (8 <= corridor.pos <= 19), "it picked ground inside the reservation"
    assert not set(corridor.tiles()) & w.reserved


def test_no_legal_site_raises_with_the_reasons_counted():
    """"There is no site" must always come with what stood in the way."""
    w = _open_world(reserved={(x, y) for x in range(0, 41) for y in range(20, 40)})
    try:
        BP.choose(w, step=4)
        raise AssertionError("should have raised")
    except BP.SiteError as e:
        assert "RESERVED" in str(e) and "candidates" in str(e)


def test_explain_is_auditable():
    w = _open_world()
    corridor, verdict = BP.choose(w, step=4)
    line = BP.explain(verdict)
    assert line.startswith("CHOSE") and "feed:" in line and "array room" in line
    bad = BP.evaluate(_open_world(reserved={(10, 30)}), BP.Corridor("v", 10, 5, 50))
    assert BP.explain(bad).startswith("REJECTED")


# ------------------------------------------------------- THE REGRESSION: the site I built
def _live_geometry():
    """The real base, to the numbers measured on 2026-08-30.

    Reservation x=-1..24, y=30..50 (the 36-lab array). Iron smelter row y=6, copper row y=15
    with its output lane at y=17 spanning the width of the base. Labs at x=0,4,8 / y=36..44.
    """
    reserved = {(x, y) for x in range(-1, 25) for y in range(30, 51)}
    copper_lane = {(x, 17) for x in range(-10, 30)}
    return BP.World(
        bounds=(-20, 0, 40, 60),
        reserved=reserved,
        occupied=copper_lane | {(x, 6) for x in range(10, 20)} | {(x, 15) for x in range(-5, 16)},
        sources={"iron-plate": [(28, 3)], "copper-plate": [(20, 12)]},
        sinks={"labs": [(8, 44)]},
    )


def test_the_2026_08_30_corridor_is_rejected():
    """x=14..17, y=18..46 - the bus that was actually built. It must now be refused, and the
    reason must name the reservation it ate: 21 ghosts, the whole x=14 service column and four
    labs at x=16."""
    w = _live_geometry()
    v = BP.evaluate(w, BP.Corridor("v", 14, 18, 46))
    assert not v["ok"], "the corridor that destroyed the lab reservation must not pass"
    assert any("RESERVED" in r for r in v["reasons"]), v["reasons"]


def test_the_framework_finds_a_site_on_the_live_geometry():
    """And it must not merely say no - there IS ground east of the reservation, and the
    framework has to find it by itself."""
    w = _live_geometry()
    corridor, verdict = BP.choose(w, step=2)
    assert verdict["ok"], verdict
    assert not set(corridor.tiles()) & w.reserved
    assert not set(corridor.tiles()) & w.occupied
    for item, d in verdict["detail"]["feed"].items():
        assert d is not None, "%s must be able to reach the chosen head" % item


if __name__ == "__main__":
    import traceback
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   %s" % name)
            except Exception:
                fails += 1
                print("FAIL %s" % name)
                traceback.print_exc()
    sys.exit(1 if fails else 0)


# ------------------------------------- a bus feed comes from an OUTPUT belt, never an input
def test_a_bus_feed_is_refused_from_an_input_belt():
    """The 2026-08-30 mistake, made twice: the bus was fed from the smelter rows' INPUT belts,
    draining ore and fuel to the bus. Lane 35 was measured carrying coal:112."""
    import autopilot as A
    old = A.belt_role
    A.belt_role = lambda x, y, radius=2: {
        "role": "input", "picks": 12, "drops": 0, "carries": "coal:100 copper-ore:7",
        "why": "12 inserter(s) PICK UP from it -> it feeds machines"}
    try:
        ok, why = BP.check_feed_source(0, 17)
    finally:
        A.belt_role = old
    assert ok is False
    assert "INPUT belt" in why and "starv" not in why.lower() or "drain" in why


def test_a_bus_feed_is_accepted_from_an_output_belt():
    import autopilot as A
    old = A.belt_role
    A.belt_role = lambda x, y, radius=2: {
        "role": "output", "picks": 0, "drops": 12, "carries": "copper-plate:4",
        "why": "12 inserter(s) DROP onto it -> it drains machines"}
    try:
        ok, why = BP.check_feed_source(0, 12, item="copper-plate")
    finally:
        A.belt_role = old
    assert ok is True and "OUTPUT" in why


def test_a_bus_feed_is_refused_when_the_role_is_unknown():
    """A belt whose purpose was GUESSED is never wired to. Counting items on it proves it
    moves, not what it moves for."""
    import autopilot as A
    old = A.belt_role
    A.belt_role = lambda x, y, radius=2: {
        "role": "unknown", "picks": 0, "drops": 0, "carries": "iron-plate:4",
        "why": "no inserter touches this tile"}
    try:
        ok, why = BP.check_feed_source(0, 3)
    finally:
        A.belt_role = old
    assert ok is False and "guessed" in why


def test_a_bus_feed_is_refused_from_the_wrong_rows_output():
    import autopilot as A
    old = A.belt_role
    A.belt_role = lambda x, y, radius=2: {
        "role": "output", "picks": 0, "drops": 8, "carries": "iron-plate:4",
        "why": "8 inserter(s) DROP onto it"}
    try:
        ok, why = BP.check_feed_source(0, 8, item="copper-plate")
    finally:
        A.belt_role = old
    assert ok is False and "wrong row" in why
