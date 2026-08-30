#!/usr/bin/env python3
"""A GHOST IS A RESERVATION, AND A CONNECTED LANE IS NOT A FAILED ONE.

    python3 -m pytest test_reservations.py

Two bugs from 2026-08-30, both of the same family: asking the question that is easy to query
instead of the one that matters.

1. THE BUS ATE THE LAB RESERVATION. A 116-belt spine was pre-flighted tile by tile with
   `can_place_entity`, reported "116 checked | ORE:none | BLOCKED:none", and destroyed 21
   ghosts of the operator's reserved 36-lab array - a whole service column (5 poles + 12
   inserters at x=14, y=30..46) and four labs at x=16. A ghost is NOT a collision, so
   can_place_entity returns true over one and create_entity silently consumes it. "Is this tile
   empty" and "is this tile UNCLAIMED" are different questions.

2. THE COPPER LANE WAS TORN OUT NINE TIMES. `connect_mine_to_array` verified with
   `_lane_connected(ore) and lane_moves_ore(ore)` and removed the lane when that was false -
   conflating "this route does not connect" (a real failure) with "this route carries nothing"
   (correct infrastructure, starved from somewhere else). 83 belts at a time, nine times, while
   every copper furnace sat at full_output. Removing it guarantees the loop: the next pass
   rebuilds the same route and measures the same zero.
"""
import sys

import rcon

_REAL = rcon.run


def _no_rcon(cmd, timeout=10.0):
    raise AssertionError("offline test issued RCON: %s" % str(cmd)[:160])


rcon.run = _no_rcon

import autopilot as A                                                     # noqa: E402
import bootstrap as B                                                     # noqa: E402
import build_gates as G                                                   # noqa: E402


def _patch(mod, name, value):
    old = getattr(mod, name)
    setattr(mod, name, value)
    return lambda: setattr(mod, name, old)


# ------------------------------------------------------------------- 1. ghosts are reservations
def test_place_refuses_ground_reserved_by_a_foreign_ghost():
    """The exact failure: a belt placed onto a reserved lab tile."""
    sent = {}

    def fake(lua, *a, **k):
        sent["lua"] = lua
        return "GHOST_RESERVED lab @tile(16,32) - that ground is reserved"

    undo = _patch(A, "_print", fake)
    try:
        out = A.place("transport-belt", 16, 32, direction=8, clear=0)
    finally:
        undo()
    assert "GHOST_RESERVED" in out
    lua = sent["lua"]
    assert "entity-ghost" in lua, "place() must look for ghosts at all"
    assert "ghost_name~=ename" in lua, \
        "the check must compare the ghost's entity to what is being built"
    assert lua.index("entity-ghost") < lua.index("create_entity"), \
        "the ghost check must run BEFORE the entity is created, not after"


def test_place_still_allows_fulfilling_the_ghosts_own_entity():
    """Building a ghost's OWN entity is how a blueprint gets fulfilled - build_ghosts depends
    on it. The guard must block foreign builds, not blueprint completion."""
    seen = {}

    def fake(lua, *a, **k):
        seen["lua"] = lua
        return "BUILT lab @(16.5,32.5)"

    undo = _patch(A, "_print", fake)
    try:
        out = A.place("lab", 15, 31, clear=0)
    finally:
        undo()
    assert "BUILT" in out
    assert "ghost_name~=ename" in seen["lua"], \
        "same-name ghosts must be exempt, or a blueprint can never be built out"


def test_reserved_tiles_reports_the_claim():
    undo = _patch(A, "_print", lambda *a, **k: "lab@16,32 inserter@14,31 small-electric-pole@14,30")
    try:
        got = A.reserved_tiles(14, 30, 17, 46)
    finally:
        undo()
    assert len(got) == 3 and "lab@16,32" in got


def test_reserved_tiles_can_ignore_the_entity_being_built():
    seen = {}

    def fake(lua, *a, **k):
        seen["lua"] = lua
        return ""

    undo = _patch(A, "_print", fake)
    try:
        assert A.reserved_tiles(0, 0, 4, 4, ignore="lab") == []
    finally:
        undo()
    assert "ghost_name~='lab'" in seen["lua"]


# --------------------------------------------------- 2. a connected lane is not a failed lane
def _mine_ctx(connected, moving, calls):
    return [
        _patch(B, "lay_belt_path", lambda route: [(1, 1), (1, 2)]),
        _patch(B, "teardown_lane", lambda ore, keep=None: calls.append(("teardown", ore))),
        _patch(B, "_lanes_load", lambda: {}),
        _patch(B, "_lanes_save", lambda d: None),
        _patch(B, "_lane_connected", lambda ore: connected),
        _patch(B, "lane_moves_ore", lambda ore: moving),
        _patch(B, "build_worked", lambda check, tries=6, delay=5: bool(check())),
        _patch(B.status, "log", lambda m: calls.append(("log", str(m)))),
    ]


def test_a_connected_lane_with_no_flow_is_KEPT():
    """The nine-teardown bug. The belt is intact; the stall is downstream."""
    calls = []
    undos = _mine_ctx(connected=True, moving=False, calls=calls)
    undos.append(_patch(B, "no_flow_reason", lambda ore: "28 furnaces jammed at full_output"))
    try:
        B._verify_lane_or_remove("copper-ore", [(1, 1)])
    finally:
        for u in undos:
            u()
    assert ("teardown", "copper-ore") not in calls, \
        "a CONNECTED lane must never be torn out for want of flow"
    assert any("KEEPING it" in m for k, m in calls if k == "log"), calls
    assert any("full_output" in m for k, m in calls if k == "log"), \
        "the log must name the REAL stall, not blame the belt"


def test_a_disconnected_lane_is_still_removed():
    """The guard must not become a blanket amnesty - a route that never connected did nothing
    and Seth's rule still applies to it."""
    calls = []
    undos = _mine_ctx(connected=False, moving=False, calls=calls)
    try:
        B._verify_lane_or_remove("copper-ore", [(1, 1)])
    finally:
        for u in undos:
            u()
    assert ("teardown", "copper-ore") in calls, "a lane that does not connect must be removed"


def test_no_flow_reason_names_a_jammed_array():
    undo = _patch(G, "sense", lambda *a, **k: {
        "counts": {"stone-furnace": 28},
        "status": {"stone-furnace": {"full_output": 28}},
        "status_type": {}, "flows": {}})
    try:
        why = B.no_flow_reason("copper-ore")
    finally:
        undo()
    assert "full_output" in why and "CONSUMES" in why


def test_no_flow_reason_names_blocked_drills():
    undo = _patch(G, "sense", lambda *a, **k: {
        "counts": {}, "status": {},
        "status_type": {"mining-drill": {"waiting_for_space_in_destination": 9, "working": 0}},
        "flows": {}})
    try:
        why = B.no_flow_reason("iron-ore")
    finally:
        undo()
    assert "blocked" in why and "upstream" in why


def test_no_flow_reason_never_guesses_when_the_census_fails():
    def boom(*a, **k):
        raise RuntimeError("rcon down")
    undo = _patch(G, "sense", boom)
    try:
        assert "unknown" in B.no_flow_reason("coal")
    finally:
        undo()


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


# ------------------------- the SAME bug, fourth instance: mine_planner_v2.verify_lane
def test_verify_lane_keeps_a_connected_but_idle_lane():
    """`ok` gates buildplan's rollback_on_fail, so `connected and moving` rolls out a correct
    lane whenever something downstream is stalled. Fourth site of one bug."""
    import mine_planner_v2 as M
    import lane_lint
    undos = [
        _patch(lane_lint, "verify_supply",
               lambda ore, a, b, settle=3.0: {"connected": True, "moving": False,
                                              "arrived": False, "path_len": 40, "findings": []}),
        _patch(B, "no_flow_reason", lambda ore: "28 furnaces jammed at full_output"),
    ]
    try:
        r = M.verify_lane({"args": {"ore": "copper-ore", "from_xy": (0, 0), "to_xy": (9, 9)}})
    finally:
        for u in undos:
            u()
    assert r["ok"] is True, "a connected lane must not be rolled back for want of flow"
    assert "KEPT" in r["detail"] and "full_output" in r["detail"], r["detail"]


def test_verify_lane_still_fails_a_disconnected_lane():
    import mine_planner_v2 as M
    import lane_lint
    undo = _patch(lane_lint, "verify_supply",
                  lambda ore, a, b, settle=3.0: {"connected": False, "moving": False,
                                                 "arrived": False, "path_len": 0, "findings": []})
    try:
        r = M.verify_lane({"args": {"ore": "iron-ore", "from_xy": (0, 0), "to_xy": (9, 9)}})
    finally:
        undo()
    assert r["ok"] is False, "a lane that never connected must still roll back"
