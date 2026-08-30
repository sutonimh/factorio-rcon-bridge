#!/usr/bin/env python3
"""Offline unit tests for power_planner.py — NO live server, NO real ledger.

Run with either:
    python3 test_power_planner.py
    python3 -m pytest test_power_planner.py

Planning, wiring and the Lua builders are pure, so most tests need no harness at all. The
apply() tests get the FakeRcon from test_world_executor.py/test_buildplan.py (it speaks the
chunked storage.<key> read protocol) plus monkeypatched buildplan ledger wrappers, so no test
can touch built-tiles.json, protected-tiles.json or the live game.

The fixtures are the OPERATOR'S OWN GEOMETRY, rebuilt from the measurements:
    smelter row  16 stone furnaces 2x2 at x0=-6 pitch 2, machine rows 5-6, inserters on the
                 LEFT tile of every furnace in rows 4 and 7, belts on rows 3 and 8
    lab block    3x3 labs on a 4-lattice, one feed inserter in the seam north of each
    mine row     3x3 electric drills at pitch 3, two rows facing a shared lane
    trunk        x=-15 from y=-65 to y=26 (91 tiles, his measured 14 poles / 13 gaps of 7)
"""
import json
import pathlib
import re
import shutil
import tempfile
import traceback

import belt_router as BR
import buildplan as B
import power_planner as P
import rcon

POLE = "small-electric-pole"


# --------------------------------------------------------------------------- fixtures
def smelter(x0=-6, my=5, n=16):
    """(consumers, obstacles, area) for one operator smelter stack."""
    ins = []
    for k in range(n):
        fx = x0 + 2 * k
        ins.append((fx, my - 1))                 # out-inserter row (my-1)
        ins.append((fx, my + 2))                 # in-inserter row  (my+2)
    hard = {(x0 + 2 * k + i, my + j)
            for k in range(n) for i in range(2) for j in range(2)}
    belts = {}
    for x in range(x0 - 2, x0 + 2 * n + 2):
        belts[(x, my - 2)] = {"name": "transport-belt", "dir": 4, "type": "surface"}
        belts[(x, my + 3)] = {"name": "transport-belt", "dir": 4, "type": "surface"}
    obs = BR.Obstacles(hard=hard, belts=belts)
    return ins, obs, (x0 - 4, my - 4, x0 + 2 * n + 3, my + 6)


def labs(x0=-1, y0=35, nx=3, ny=3):
    body = [{"x": x0 + 4 * i, "y": y0 + 4 * j, "w": 3, "h": 3}
            for i in range(nx) for j in range(ny)]
    hard = {(L["x"] + i, L["y"] + j) for L in body for i in range(3) for j in range(3)}
    ins = [{"x": L["x"] + 1, "y": L["y"] - 1} for L in body]
    return body + ins, BR.Obstacles(hard=hard), (x0 - 4, y0 - 3, x0 + 4 * nx + 2,
                                                 y0 + 4 * ny + 2)


def mine(ly=-64, xs=(-34, -31, -28, -25)):
    drills = [{"x": x, "y": ly - 3, "w": 3, "h": 3} for x in xs] + \
             [{"x": x, "y": ly + 1, "w": 3, "h": 3} for x in xs]
    hard = {(d["x"] + i, d["y"] + j) for d in drills for i in range(3) for j in range(3)}
    belts = {(x, ly): {"name": "transport-belt", "dir": 4, "type": "surface"}
             for x in range(min(xs) - 2, -10)}
    return drills, BR.Obstacles(hard=hard, belts=belts), (min(xs) - 6, ly - 6, -12, ly + 6)


def pole_ent(tx, ty, net=535):
    return {"n": POLE, "t": "electric-pole", "x": tx + 0.5, "y": ty + 0.5, "e": net}


def ins_ent(tx, ty, net=535):
    return {"n": "inserter", "t": "inserter", "x": tx + 0.5, "y": ty + 0.5, "d": 8, "e": net}


# --------------------------------------------------------------------------- geometry
def test_pitch_is_derived_not_hardcoded():
    """`pitch_for` reproduces every measured lattice from the prototype window alone."""
    assert P.pitch_for(2, 1) == 4, "1x1 inserters at pitch 2 -> pole pitch 4 (smelter rows)"
    assert P.pitch_for(4, 3) == 4, "3x3 labs at pitch 4 -> pole pitch 4 (lab block)"
    assert P.pitch_for(3, 3) == 6, "3x3 drills at pitch 3 -> pole pitch 6 (mine rows)"
    assert P.pitch_for(2, 1, cap=99) == 4
    # medium pole: supply 3.5 -> a 7-tile window -> the smelter row stretches to 6
    assert P.pitch_for(2, 1, pole="medium-electric-pole") == 6
    assert P.max_hop(POLE) == 7 and P.max_hop("medium-electric-pole") == 9


def test_consumer_forms_all_normalize_to_the_same_box():
    """Live probes, snapshots and hand-written fixtures all speak different dialects; a
    3x3 drill at centre (-42.5,-65.5) must come out as tiles (-44,-67)..(-42,-65) whichever
    one the caller used."""
    want = (-44, -67, -42, -65)
    assert P.consumer_box({"bb": [-44, -67, -42, -65]}) == want
    assert P.consumer_box({"x": -44, "y": -67, "w": 3, "h": 3}) == want
    assert P.consumer_box({"x": -44, "y": -67, "name": "electric-mining-drill"}) == want
    assert P.consumer_box({"cx": -42.5, "cy": -65.5,
                           "name": "electric-mining-drill"}) == want
    assert P.consumer_box((-44, -67, -42, -65)) == want
    assert P.consumer_box((3, 4)) == (3, 4, 3, 4)
    # and the live/snapshot entity shape goes through in one call
    assert P.from_entities([{"n": "electric-mining-drill", "x": -42.5, "y": -65.5},
                            {"n": "inserter", "bb": [0, 1, 0, 1]}]) == [want, (0, 1, 0, 1)]


def test_supply_window_is_the_engine_rule():
    """A pole at tile px supplies tiles px-2..px+2; a machine is powered when its box
    OVERLAPS that, which is what makes 2 machines fit under one pole at pitch 4."""
    assert P.supply_span(POLE, 0, "tw") == (-2, 2)
    assert P.covers(POLE, 0, 0, (2, 0, 2, 0)) and not P.covers(POLE, 0, 0, (3, 0, 3, 0))
    assert P.covers(POLE, 0, 0, (-4, 0, -2, 0)), "a 3-wide machine reaching px-2 is covered"
    assert not P.covers(POLE, 0, 0, (-5, 0, -3, 0)), "one tile short of the window is not"
    # the machine-start window is mw+4 wide, which is what pitch_for divides by the pitch
    starts = [dx for dx in range(-10, 10) if P.covers(POLE, 0, 0, (dx, 0, dx + 2, 0))]
    assert len(starts) == 3 + 4 and starts == list(range(-4, 3)), starts


# --------------------------------------------------------------------------- plan_grid
def test_lattice_covers_all_consumers():
    """REQUIREMENT: every consumer is powered, on every fixture."""
    for name, (cons, obs, area) in (("smelter", smelter()), ("labs", labs()),
                                    ("mine", mine())):
        plan = P.plan_grid(area, cons, obstacles=obs)
        tiles = P.plan_tiles(plan)
        for c in cons:
            box = P.consumer_box(c)
            assert any(P.covers(POLE, t[0], t[1], box) for t in tiles), \
                "%s: consumer %s is unpowered" % (name, box)
        assert P.LAST_INFO["uncovered"] == 0, name


def test_no_pole_on_a_reserved_or_belt_or_machine_tile():
    """LAW 1, via belt_router's own obstacle model: a pole never lands on a belt tile, an
    inserter pickup/drop tile, a drill drop tile, or inside a machine footprint."""
    drills, obs, area = mine()
    # the functional reservations belt_router.scan_obstacles would report: every drill's
    # drop tile lands on the lane row, and the inserters of a mine's downstream cell.
    obs.reserved |= {(x, -64) for x in (-33, -30, -27, -24)}
    obs.reserved |= {(-20, -68), (-20, -60)}
    plan = P.plan_grid(area, drills, obstacles=obs)
    tiles = set(P.plan_tiles(plan))
    assert tiles, "no plan"
    assert not (tiles & obs.reserved), "pole on a reserved tile: %s" % (tiles & obs.reserved)
    assert not (tiles & set(obs.belts)), "pole on a belt tile"
    assert not (tiles & obs.hard), "pole inside a machine footprint"
    assert not [f for f in P.validate(plan, consumers=drills, obstacles=obs)
                if f["check"] == "pole_on_forbidden_tile"]


def test_plan_is_connected_by_construction():
    """LAW 3. A mine's two pole rows are 8 apart - past the 7.5 wire reach - so this only
    passes because plan_grid closes the graph itself instead of handing back two networks."""
    for name, (cons, obs, area) in (("smelter", smelter()), ("labs", labs()),
                                    ("mine", mine())):
        plan = P.plan_grid(area, cons, obstacles=obs)
        tiles = P.plan_tiles(plan)
        assert P.connected(tiles), "%s: plan is %d networks" % (
            name, len(P.components(tiles)))
        assert not [f for f in P.validate(plan, consumers=cons, obstacles=obs)
                    if f["check"] == "grid_split"]
    # and the mine really did need a bridge (the rows are lane_y -/+ 4 = 8 apart)
    drills, obs, area = mine()
    P.plan_grid(area, drills, obstacles=obs)
    rows = P.LAST_INFO["rows"]
    assert -68 in rows and -60 in rows, "mine pole rows must be lane_y -/+ 4, got %s" % rows
    assert P.LAST_INFO["bridged"], "8 > 7.5: the two rows cannot wire without a bridge pole"


def test_smelter_lattice_matches_the_operator():
    """P4 + the measured smelter spec: pitch 4, poles IN the inserter rows (my-1, my+2), all
    on odd columns - the free tile of every other 2x2 furnace - never flanking the belts."""
    cons, obs, area = smelter()
    plan = P.plan_grid(area, cons, obstacles=obs)
    tiles = P.plan_tiles(plan)
    assert P.LAST_INFO["pitch"] == 4, P.LAST_INFO
    assert P.LAST_INFO["rows"] == [4, 7], "poles ride the inserter rows, not rows 2/9"
    assert len({x % 4 for x, _ in tiles}) == 1, "one phase for the whole area"
    assert all(x % 2 for x, _ in tiles), "inserters take the even tiles; poles take the odd"
    assert len(tiles) == 16, "8 poles per row for 16 furnaces (operator: 9, both cover all)"
    # every pole covers exactly the 2 inserters the measured spec predicts
    per = [sum(1 for c in cons if P.covers(POLE, t[0], t[1], P.consumer_box(c)))
           for t in tiles]
    assert min(per) >= 2, per


def test_flanking_is_refused_while_a_service_row_exists():
    """The bot flanked at rows 2/9/11/18 and the operator deleted all four rows. Flanking is
    CHEAPER (12 poles at pitch 6 vs 16 at pitch 4 on this fixture), so it has to be forbidden
    outright, not merely penalised."""
    cons, obs, area = smelter()
    plan = P.plan_grid(area, cons, obstacles=obs)
    rows = P.LAST_INFO["rows"]
    assert 2 not in rows and 9 not in rows, rows
    assert all(P._row_service_dist(r, [P.consumer_box(c) for c in cons])
               <= P.MAX_SERVICE_DIST for r in rows)
    assert not P.LAST_WARNINGS, P.LAST_WARNINGS
    # forcing pitch 6 is the flank solution; it must at least say so
    plan6 = P.plan_grid(area, cons, obstacles=obs, pitch=6)
    assert P.LAST_WARNINGS and "FLANKING" in P.LAST_WARNINGS[0]
    assert P.LAST_INFO["rows"] == [2, 9] and len(P.plan_tiles(plan6)) == 12


def test_lab_block_uses_the_seam_lattice():
    """The lab print's poles are the intersections of the 1-tile seam columns and rows."""
    cons, obs, area = labs(x0=-1, y0=31, nx=7, ny=5)
    plan = P.plan_grid(area, cons, obstacles=obs)
    tiles = P.plan_tiles(plan)
    assert P.LAST_INFO["pitch"] == 4, P.LAST_INFO
    assert len({x % 4 for x, _ in tiles}) == 1
    # x0=-1 + 3 = 2 is the first seam column; the operator's live cols are -6,-2,2,6,10
    assert sorted({x for x, _ in tiles})[0] % 4 == 2 % 4
    assert P.connected(tiles) and P.LAST_INFO["uncovered"] == 0


def test_anchor_is_reached_by_a_straight_trunk_never_a_chain():
    """The operator joins an area lattice to the trunk with 1-2 poles on a straight line.
    The bot laid a 7-pole non-axis-aligned chain to the lab array that powered nothing."""
    cons, obs, area = smelter()
    anchor = (-15, 5)                      # his main N-S trunk column
    area = (area[0] - 12, area[1], area[2], area[3])
    plan = P.plan_grid(area, cons, obstacles=obs, anchor=anchor)
    tiles = P.plan_tiles(plan)
    assert P.connected(tiles + [anchor]), "the lattice must reach the existing grid"
    assert anchor not in tiles, "the anchor is an EXISTING pole and is never re-placed"
    bridge = P.LAST_INFO["bridged"]
    assert len(bridge) <= 2, "join with 1-2 poles, not a chain: %s" % (bridge,)


def test_an_ORPHAN_standing_pole_does_not_veto_every_lattice():
    """REGRESSION 2026-08-30. The SPLIT check asked `connected(tiles + existing + anchor)` -
    "are the plan AND every pole standing in this area one network?" - which is not a question
    the plan controls. One pole someone left further than the wire reach from everything else
    makes the answer no for EVERY pitch and EVERY phase, so plan_grid raised on all of them,
    and stage_array_grid absorbs a GridError as "retrying next pass": a permanent, silent
    stall over a pole the lattice was never going to touch.

    What LAW 3 actually requires is here: the plan is one network, and it reaches its anchor.
    The orphan is REPORTED, not raised on.
    """
    cons, obs, area = smelter()
    # room to the south for a pole nothing can reach: the lattice rows are chosen from the
    # CONSUMER band (rows 4 and 7), so an empty y=35 is 28 tiles from the nearest pole
    area = (area[0] - 12, area[1], area[2], area[3] + 24)
    anchor = (-15, 5)
    orphan = (area[2] - 1, area[3])            # SE corner, far past the 7.5 wire reach
    plan = P.plan_grid(area, cons, obstacles=obs, anchor=anchor, existing=[orphan])
    tiles = P.plan_tiles(plan)
    assert tiles, "an unreachable standing pole vetoed the whole lattice"
    assert P.connected(tiles + [anchor]), "the plan itself must still be ONE network"
    assert any("ALREADY islanded" in w for w in P.LAST_WARNINGS), P.LAST_WARNINGS
    # ...and validate() must agree, or apply() refuses for the reason plan_grid stopped raising
    bad = [f for f in P.validate(plan, consumers=cons, obstacles=obs, anchor=anchor,
                                 existing=[orphan]) if f["severity"] == "error"]
    assert not bad, bad
    orph = [f for f in P.validate(plan, consumers=cons, obstacles=obs, anchor=anchor,
                                  existing=[orphan]) if f["check"] == "orphan_pole"]
    assert len(orph) == 1 and orph[0]["severity"] == "warn", orph
    # a PLANNED pole off the network is still an ERROR - that is the split this module prevents
    split = P.validate(list(plan) + [{"x": 60, "y": 60, "entity": POLE}],
                       consumers=cons, obstacles=obs, anchor=anchor, existing=[orphan])
    assert any(f["check"] == "grid_split" and f["severity"] == "error" for f in split), split


def test_a_trunk_endpoint_this_run_would_NEWLY_place_is_legality_checked():
    """REGRESSION 2026-08-30. `_run`'s endpoint check was reachable only through `hard`, and
    `hard` was the unconditional {a, b} - so BOTH endpoints were exempt and the loop was dead
    code. stage_spine's far end (SPINE_X, oy) is a brand-new tile, not a standing pole, and
    nothing stopped it landing on a belt: the same hole `_corners` was written to close one
    tile over.

    `existing` names the endpoints that really ARE poles already. Everything else is checked.
    """
    a, b = (0, 0), (0, 21)                     # a = a standing pole, b = a fresh tile
    blocked = {a, b}                           # b is a belt tile; a is the pole's own tile
    # the historical contract - both endpoints are the caller's own poles - is unchanged
    tiles = P.plan_tiles(P.plan_trunk(a, b, blocked=blocked, area=(-2, -2, 20, 30)))
    assert tiles[0] == a and tiles[-1] == b, tiles
    # naming only the standing one makes the check LIVE: b is on a belt and the run is refused
    try:
        P.plan_trunk(a, b, blocked=blocked, area=(-2, -2, 20, 30), existing=[a])
    except P.GridError as e:
        assert "anchor" in str(e) or "occupied" in str(e), e
    else:
        raise AssertionError("a fresh endpoint on a blocked tile was planned anyway")
    # and a fresh endpoint on FREE ground is still planned, of course
    ok = P.plan_tiles(P.plan_trunk(a, b, blocked={a}, area=(-2, -2, 20, 30), existing=[a]))
    assert ok[0] == a and ok[-1] == b, ok


def test_grid_error_when_the_area_cannot_hold_a_lattice():
    cons, obs, area = smelter()
    try:
        P.plan_grid((0, 0, 0, 0), cons, obstacles=obs)
    except P.GridError:
        pass
    else:
        raise AssertionError("a 1-tile area must raise GridError, not return a split plan")


def test_poles_already_in_the_ground_push_the_lattice_out_to_MIN_POLE_SEP():
    """The planner had no notion of standing poles: they landed in `blocked`, so they only
    EXCLUDED tiles - their coverage was never credited and MIN_POLE_SEP was never measured
    against them. Live, that made it pick the next free phase and plan a full PARALLEL lattice
    2.0 tiles from the one already there, covering nothing new."""
    cons, obs, area = smelter()
    plan = P.plan_grid(area, cons, obstacles=obs)
    laid = P.plan_tiles(plan)
    assert laid, "no baseline plan"
    again = P.plan_tiles(P.plan_grid(area, cons, obstacles=obs, existing=laid[:1]))
    for t in again:
        assert P._dist(POLE, t, laid[0]) >= P.MIN_POLE_SEP - 1e-9, \
            "planned %s only %.2f from the standing pole %s" % (t, P._dist(POLE, t, laid[0]),
                                                               laid[0])


def test_validate_measures_separation_against_STANDING_poles_too():
    """A plan whose every pole is 2.0 tiles from one already in the ground reads perfectly
    clean if you only compare the plan to itself - which is all validate could do."""
    cons, obs, area = smelter()
    plan = P.plan_grid(area, cons, obstacles=obs)
    tiles = P.plan_tiles(plan)
    interleaved = [(x + 2, y) for x, y in tiles]        # the 2.0-tile parallel lattice
    blind = [f for f in P.validate(plan, consumers=cons, obstacles=obs)
             if f["check"] == "poles_too_close"]
    seeing = [f for f in P.validate(plan, consumers=cons, obstacles=obs, existing=interleaved)
              if f["check"] == "poles_too_close"]
    assert not blind, "the plan is internally fine: %s" % blind[:2]
    assert len(seeing) >= len(tiles), "the interleave must be reported: %d" % len(seeing)


# --------------------------------------------------------------------------- plan_trunk
def test_trunk_spacing_never_exceeds_the_wire_reach():
    """REQUIREMENT, and the operator's measured run: x=-15, y=-65..26, 91 tiles, 14 poles,
    gaps 7,7,7,7,7,7,7,7,7,7,7,7,7."""
    tiles = P.plan_tiles(P.plan_trunk((-15, -65), (-15, 26)))
    gaps = [b[1] - a[1] for a, b in zip(tiles, tiles[1:])]
    assert len(tiles) == 14 and gaps == [7] * 13, (len(tiles), gaps)
    assert all(g <= P.TRUNK_SPACING for g in gaps)
    assert P.connected(tiles)
    # a length that is NOT a multiple of 7: endpoints are hard, the last hop is SHORT
    t2 = P.plan_tiles(P.plan_trunk((0, 0), (0, 30)))
    g2 = [b[1] - a[1] for a, b in zip(t2, t2[1:])]
    assert t2[0] == (0, 0) and t2[-1] == (0, 30), t2
    assert max(g2) <= 7 and g2[-1] == 2, g2
    # off-axis endpoints give two straight legs, never a diagonal staircase
    t3 = P.plan_tiles(P.plan_trunk((-36, 12), (-15, 26)))
    assert all(a[0] == b[0] or a[1] == b[1] for a, b in zip(t3, t3[1:])), t3
    assert max(P._dist(POLE, a, b) for a, b in zip(t3, t3[1:])) <= 7.5
    assert P.connected(t3)


def test_trunk_nudges_back_never_sideways():
    """A blocked lattice point shifts the pole BACK along the run (the hop only shrinks);
    the column itself never moves, or the run stops being straight."""
    tiles = P.plan_tiles(P.plan_trunk((-15, -65), (-15, -40), blocked={(-15, -58)}))
    assert {x for x, _ in tiles} == {-15}, "the run must stay in its column"
    gaps = [b[1] - a[1] for a, b in zip(tiles, tiles[1:])]
    assert max(gaps) <= 7 and (-15, -58) not in tiles, (tiles, gaps)


def test_trunk_refuses_a_spacing_past_the_wire_reach():
    try:
        P.plan_trunk((0, 0), (0, 30), spacing=9)
    except P.GridError:
        pass
    else:
        raise AssertionError("spacing 9 > small-pole reach 7.5 must raise")


# The live geometry, reduced to what actually decided it. `_reach_anchor` planned
# (-5,4)->(-15,3); (-5,3) is a transport-belt tile on the iron plate row, and the two candidate
# corners scored a TIE on `_leg_blocked` (6 apiece, measured on the live obstacle set), which
# the old `<=` handed to the blocked one. Two blocked tiles on the y=4 leg reproduce that tie.
_TRUNK_A, _TRUNK_B = (-5, 4), (-15, 3)
_TIE_BLOCKED = {(-5, 3), (-8, 4), (-9, 4)}


def test_the_trunk_CORNER_is_legality_checked_like_every_other_tile():
    """REGRESSION, live 2026-08-29. Every INTERIOR tile of a leg went through `_fits`, but the
    corner is leg 1's endpoint and leg 2's start, and `_run` anchored its endpoints
    unconditionally - so the one brand-new pole nobody checked was emitted straight into the
    plan, and phase 0's array_grid then died on the same GridError every pass forever.
    `_leg_blocked` only ever COUNTED blocked tiles as a tie-break; it never rejected a corner."""
    assert (P._leg_blocked(_TRUNK_A, (-5, 3), _TRUNK_B, _TIE_BLOCKED)
            == P._leg_blocked(_TRUNK_A, (-15, 4), _TRUNK_B, _TIE_BLOCKED)), \
        "the fixture must reproduce the TIE that handed the route to the blocked corner"
    tiles = P.plan_tiles(P.plan_trunk(_TRUNK_A, _TRUNK_B, blocked=set(_TIE_BLOCKED),
                                      area=(-15, 0, 32, 20)))
    assert (-5, 3) not in tiles, "the corner is a belt tile and was planned anyway: %s" % tiles
    assert not (set(tiles) & _TIE_BLOCKED), "pole on a blocked tile: %s" % tiles
    assert tiles[0] == _TRUNK_A and tiles[-1] == _TRUNK_B, tiles    # endpoints stay HARD
    assert all(a[0] == b[0] or a[1] == b[1] for a, b in zip(tiles, tiles[1:])), tiles
    assert max(P._dist(POLE, a, b) for a, b in zip(tiles, tiles[1:])) <= 7.5, tiles
    assert P.connected(tiles)


def test_a_trunk_with_both_corners_blocked_is_REFUSED_not_planned_anyway():
    """The other half of the same rule. `_lay` reads this refusal as 'this pitch/phase cannot
    reach the grid' and tries the next one, so a cornered candidate becomes a different
    lattice - which is the only outcome better than a pole on a belt."""
    blocked = {(0, 10), (10, 0)}          # both (a.x,b.y) and (b.x,a.y)
    try:
        P.plan_trunk((0, 0), (10, 10), blocked=blocked, area=(-2, -2, 20, 20))
    except P.GridError as e:
        assert "corner" in str(e), e
    else:
        raise AssertionError("a route whose every corner is occupied must be refused")


def test_a_leg_endpoint_is_checked_unless_it_is_the_callers_own_pole():
    """from_xy/to_xy are EXISTING poles - they are in `blocked` because a pole occupies its own
    tile, and they must still be emitted. Anything else a leg anchors on is a tile this run
    would newly place, so it is checked like an interior one."""
    a, b = (0, 0), (0, 21)
    tiles = P.plan_tiles(P.plan_trunk(a, b, blocked={a, b}, area=(-2, -2, 20, 30)))
    assert tiles[0] == a and tiles[-1] == b, tiles


# --------------------------------------------------------------------------- wiring
def test_wire_pairs_span_the_grid_and_respect_the_degree_cap():
    """LAW 4 with the engine's 5-slot limit: a 4-pitch 2-D lattice has 8 neighbours inside
    7.5, so 'wire every pair' has to be a spanning pass plus a capped redundancy pass."""
    tiles = [(x, y) for y in (4, 7) for x in range(-5, 24, 4)]
    pairs = P.wire_pairs(tiles)
    assert pairs
    assert all(P._dist(POLE, a, b) <= 7.5 + 1e-9 for a, b in pairs), "a pair out of reach"
    deg = {}
    for a, b in pairs:
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
    assert max(deg.values()) <= P.MAX_POLE_DEGREE, "cap 4, leaving a slot for a later pole"
    assert max(deg.values()) <= P.POLE_DEGREE_SEEN, "and well under the 6 measured live"
    # the emitted wires alone must connect every pole (not merely the geometry)
    par = {t: t for t in tiles}

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    for a, b in pairs:
        par[find(a)] = find(b)
    assert len({find(t) for t in tiles}) == 1, "the wire graph itself is not one network"


def test_wire_pairs_joins_new_poles_to_an_existing_grid():
    """A pair between two PRE-EXISTING poles is not emitted (they are already one live
    network); a pair from a new pole to one of them is."""
    new = [(0, 0), (4, 0)]
    old = [(10, 0), (16, 0)]
    pairs = P.wire_pairs(new, existing=old)
    assert not any(a in old and b in old for a, b in pairs)
    assert any((a in new) != (b in new) for a, b in pairs), "new poles must join the grid"


def test_wire_pairs_spans_even_when_the_cap_would_refuse():
    """The spanning pass may use the 5th slot: a link that is the ONLY thing joining two
    components is never skipped for a degree cap. That is the failure that stranded the
    bot's lab block (two poles 4.0 apart, both saturated, no slot left to bridge)."""
    star = [(0, 0)] + [(dx, dy) for dx, dy in
                       ((0, -5), (0, 5), (-5, 0), (5, 0), (4, 4), (-4, 4))]
    pairs = P.wire_pairs(star, max_degree=1)
    par = {t: t for t in star}

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    for a, b in pairs:
        par[find(a)] = find(b)
    assert len({find(t) for t in star}) == 1, "spanning must beat the cap"


# --------------------------------------------------------------------------- lua builders
def test_lua_builders_are_pure_and_within_the_command_cap():
    tiles = [(x, y) for y in (4, 7) for x in range(-5, 100, 4)]
    cmds = P.place_lua(tiles)
    assert cmds and all(len(c) <= P.CMD_LIMIT for c in cmds), [len(c) for c in cmds]
    assert all(c.startswith("/sc ") for c in cmds)
    assert "can_place_entity" in cmds[0], "P5: nothing is placed without can_place_entity"
    assert "destroy()" not in cmds[0].replace("t.destroy()", ""), \
        "an occupied tile is SKIPPED, never destroyed"
    joined = "".join(cmds)
    for t in tiles:
        assert "%d,%d" % t in joined, "tile %s dropped by the batcher" % (t,)

    pairs = P.wire_pairs(tiles)
    wc = P.wire_lua(pairs)
    assert wc and all(len(c) <= P.CMD_LIMIT for c in wc)
    assert "defines.wire_connector_id.pole_copper" in wc[0]
    assert "connect_to(cq,false)" in wc[0], "LAW 4: the explicit wiring call"

    v = P.verify_lua((-10, 0, 30, 12))
    assert "electric_network_id" in v and "no_power" in v and len(v) <= P.CMD_LIMIT
    assert "create_entity" not in v and "destroy" not in v, "the verifier must be READ-ONLY"
    pr = P.probe_lua((-10, 0, 30, 12))
    assert "find_entities_filtered" in pr and "create_entity" not in pr


def test_no_module_lua_registers_an_event_handler():
    """Runtime handlers desync joining clients and locked the operator out of his own
    server. Nothing here may ever emit one."""
    src = pathlib.Path(P.__file__).read_text()
    for bad in ("script.on_event", "on_nth_tick", "script.on_nth_tick"):
        assert bad not in src, bad


# --------------------------------------------------------------------------- validate
def test_validate_reports_what_apply_refuses_on():
    cons, obs, area = smelter()
    plan = P.plan_grid(area, cons, obstacles=obs)
    assert not P.validate(plan, consumers=cons, obstacles=obs)
    # a pole dropped onto a belt tile, and a second one 40 tiles away
    bad = list(plan) + [{"x": -6, "y": 3, "entity": POLE},
                        {"x": 60, "y": 60, "entity": POLE}]
    checks = {f["check"] for f in P.validate(bad, consumers=cons, obstacles=obs)}
    assert "pole_on_forbidden_tile" in checks and "grid_split" in checks, checks
    # an uncovered consumer is reported too
    checks = {f["check"] for f in P.validate(plan, consumers=list(cons) + [(60, 60)],
                                             obstacles=obs)}
    assert "uncovered_consumer" in checks


# --------------------------------------------------------------------------- audit
def test_audit_finds_an_off_lattice_pole():
    """REQUIREMENT. A clean pitch-4 row plus one pole dropped between two lattice points -
    the signature of an interpolating error handler (`fle_tools.connect`)."""
    ents = ([pole_ent(x, 4) for x in (-7, -3, 1, 5, 9)] + [pole_ent(3, 4)] +
            [ins_ent(x, 4) for x in (-6, -2, 2, 6, 10)])
    rep = P.audit((-20, 0, 20, 10), ents=ents)
    off = [f for f in rep if f["check"] == "off_lattice_pole" and f["severity"] == "error"]
    assert len(off) == 1 and off[0]["pos"] == [3.5, 4.5], rep
    assert "pitch 4" in off[0]["msg"]
    # the clean row on its own is silent
    clean = [pole_ent(x, 4) for x in (-7, -3, 1, 5, 9)] + [ins_ent(x, 4) for x in
                                                           (-6, -2, 2, 6, 10)]
    assert not [f for f in P.audit((-20, 0, 20, 10), ents=clean)
                if f["severity"] == "error"]


def test_audit_finds_a_redundant_pole():
    """REQUIREMENT. 34% of the bot's poles were fully redundant and 0% of the operator's."""
    ents = [pole_ent(-3, 4), pole_ent(1, 4), pole_ent(5, 4),
            ins_ent(-1, 4), ins_ent(0, 4), ins_ent(2, 4), ins_ent(6, 4)]
    rep = P.audit((-20, 0, 20, 10), ents=ents)
    red = [f for f in rep if f["check"] == "redundant_pole" and f["severity"] == "error"]
    assert len(red) == 1 and red[0]["pos"] == [-2.5, 4.5], rep
    assert "duplicated" in red[0]["msg"]


def test_audit_never_flags_a_connectivity_bridge_as_redundant():
    """GOTCHAS "never delete connector poles": removing "poles that power nothing" once
    browned out the whole factory. A pole that is a CUT VERTEX is load-bearing whatever its
    coverage says."""
    # two clusters 12 apart, joined only by the middle pole
    ents = [pole_ent(0, 0), pole_ent(6, 0), pole_ent(12, 0), ins_ent(0, 1), ins_ent(12, 1)]
    rep = P.audit((-5, -5, 20, 10), ents=ents)
    assert not [f for f in rep if f["check"] == "redundant_pole" and f["pos"] == [6.5, 0.5]]


def test_audit_finds_islanded_poles_both_ways():
    """P2. The bot's real island: net 405 held 2 poles + 6 electric drills and NO generator,
    8.06 tiles from the main grid against a 7.5 reach."""
    ents = ([pole_ent(x, 4) for x in (-7, -3, 1)] +
            [pole_ent(30, 4, 405), pole_ent(34, 4, 405), pole_ent(38, 4, 405)] +
            [ins_ent(0, 4)])
    rep = P.audit((-20, 0, 50, 10), ents=ents)
    isl = [f for f in rep if f["check"] == "islanded_pole"]
    assert {tuple(f["pos"]) for f in isl} == {(30.5, 4.5), (34.5, 4.5), (38.5, 4.5)}
    assert any("electric_network_id 405" in f["msg"] for f in isl), "the live id disagrees"
    assert any("wire-isolated" in f["msg"] for f in isl), "and so does the geometry"


def test_audit_does_not_invent_islands_it_cannot_measure():
    """An island is an ERROR, and an error here invites a caller to "repair" a grid. Two ways
    to raise a false one, both fixed: scoring a mixed-tier base at one reach (two MEDIUM poles
    8.0 apart are one network at reach 9.0), and letting the wire model outrank the engine's
    own electric_network_id (the link may run through a pole outside the scanned box)."""
    med = [{"n": "medium-electric-pole", "t": "electric-pole", "x": x + 0.5, "y": 0.5,
            "e": 535} for x in (0, 8, 16)] + [ins_ent(0, 1)]
    assert not [f for f in P.audit((-9, -9, 30, 20), ents=med)
                if f["check"] == "islanded_pole"], "medium poles wire at 9.0, not 7.5"
    # small poles 9 apart with the SAME live id: the engine says one grid, so it is one grid
    linked = [pole_ent(0, 0), pole_ent(9, 0), ins_ent(0, 1)]
    assert not [f for f in P.audit((-9, -9, 20, 20), ents=linked)
                if f["check"] == "islanded_pole"], "the live id outranks our wire model"
    # ... but with no id probed at all, geometry is all there is and must still fire
    blind = [{k: v for k, v in p.items() if k != "e"} for p in linked]
    assert [f for f in P.audit((-9, -9, 20, 20), ents=blind)
            if f["check"] == "islanded_pole"], "offline, geometry is the only witness"


def test_audit_separates_the_operator_from_the_bot():
    """Ground truth. Both snapshots, unmodified, straight off disk."""
    def rep(name):
        d = json.loads((pathlib.Path(__file__).parent /
                        "snapshots" / ("%s.json" % name)).read_text())
        return P.audit((0, 0, 1, 1), ents=d["ents"])

    before, after = rep("before"), rep("after")
    eb = [f for f in before if f["severity"] == "error"]
    ea = [f for f in after if f["severity"] == "error"]
    assert not ea, "the operator's own 69 poles must be clean: %s" % ea[:3]
    assert len(eb) >= 15, "the bot's 107 poles must not be: %d errors" % len(eb)
    assert any(f["check"] == "islanded_pole" and "405" in f["msg"] for f in eb), \
        "the measured net-405 island must be found"
    assert sum(1 for f in eb if f["check"] == "redundant_pole") >= 10


# --------------------------------------------------------------------------- apply harness
_STORE_WRITE = re.compile(r"rcon\.print\(#(storage\.[A-Za-z0-9_]+)\)")
_STORE_SLICE = re.compile(r"(storage\.[A-Za-z0-9_]+):sub\((\d+),(\d+)\)")
_STORE_CLEAR = re.compile(r"storage\.[A-Za-z0-9_]+=nil")


class FakeRcon:
    """Scripted rcon.run: (substring, response) steps consumed in order, plus native handling
    of the chunked storage.<key> protocol.

    The buffer key is whatever the BUILD command minted - rcon.read_chunked mints a private one
    per read - so payloads are filed under the key the command actually used rather than under
    a hardcoded _world/_pgrid. The logical name passed to json() survives only as a label.
    """

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []
        self.payload = {}
        self.clobber = None          # see clobber_after(): the mid-read swap, defect 2

    def payload_len(self, key, obj):
        self.payload[key] = json.dumps(obj, separators=(",", ":"))
        return str(len(self.payload[key]))

    def json(self, key, obj):
        def build(cmd):
            m = _STORE_WRITE.search(cmd)
            return self.payload_len(m.group(1) if m else key, obj)
        return build

    def clobber_after(self, slices, obj):
        """Swap the buffer out from under the reader after `slices` slices have been served -
        exactly what a concurrent power_planner.scan did to the invariant thread's audit."""
        self.clobber = {"after": int(slices), "obj": obj, "seen": 0}

    def __call__(self, cmd, timeout=10.0):
        self.calls.append(cmd)
        m = _STORE_SLICE.search(cmd)
        if m:
            key, i, j = m.group(1), int(m.group(2)), int(m.group(3))
            c = self.clobber
            if c is not None:
                c["seen"] += 1
                if c["seen"] > c["after"]:
                    self.payload[key] = json.dumps(c["obj"], separators=(",", ":"))
            return self.payload[key][i - 1:j] + "\n"
        if _STORE_CLEAR.search(cmd):
            return ""
        if not self.script:
            raise AssertionError("unexpected RCON call (script exhausted): %s" % cmd[:200])
        sub, resp = self.script.pop(0)
        assert sub in cmd, "expected %r in RCON cmd, got: %s" % (sub, cmd[:200])
        return resp(cmd) if callable(resp) else resp


class Ctx:
    def __init__(self, script=(), protected=(), operator=False):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="powerplanner-test-"))
        self._orig = (B.PLANS_DIR, B.DIRTY_PATH, rcon.run, B._protected, B._record_built,
                      B._forget_built, B._operator_present, B._build_worked)
        B.PLANS_DIR = self.tmp / "plans"
        B.DIRTY_PATH = B.PLANS_DIR / "_dirty.json"
        self.fake = FakeRcon(script)
        rcon.run = self.fake
        self.protected = set(protected)
        self.built = set()
        self.forgotten = set()
        self.operator = operator
        B._protected = lambda: set(self.protected)
        B._record_built = lambda tiles: self.built.update(tuple(t) for t in tiles)
        B._forget_built = lambda tiles: self.forgotten.update(tuple(t) for t in tiles)
        B._operator_present = lambda: self.operator
        B._build_worked = lambda check, tries, delay: check()

    def close(self):
        (B.PLANS_DIR, B.DIRTY_PATH, rcon.run, B._protected, B._record_built,
         B._forget_built, B._operator_present, B._build_worked) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)


def _with_ctx(fn):
    def wrapper():
        ctx = Ctx()
        try:
            fn(ctx)
        finally:
            ctx.close()
    wrapper.__name__ = fn.__name__
    return wrapper


def _placed_echo(tiles):
    return ";".join("%d,%d,b" % t for t in tiles)


# --------------------------------------------------------------------------- apply
def test_apply_refuses_a_split_plan_before_touching_the_world():
    """LAW 3 at the placement layer, not the control layer (Build Law 4). Finding out the
    grid is split AFTER 40 create_entity calls is worse than not starting."""
    ctx = Ctx()
    try:
        plan = [{"x": 0, "y": 0, "entity": POLE}, {"x": 40, "y": 0, "entity": POLE}]
        try:
            P.apply(plan, scan_tick=100)
        except P.GridError as e:
            assert "network" in str(e) or "SPLIT" in str(e), e
        else:
            raise AssertionError("a split plan must be refused")
        assert not ctx.fake.calls, "refusal must cost zero RCON: %s" % ctx.fake.calls[:1]
    finally:
        ctx.close()


@_with_ctx
def test_apply_places_wires_and_verifies_one_network(ctx):
    tiles = [(0, 0), (4, 0), (8, 0)]
    plan = [{"x": x, "y": y, "entity": POLE} for x, y in tiles]
    ctx.fake.script = [
        ("radius=0.6", ctx.fake.json("_world", [])),        # probe: nothing built yet
        ("create_entity", _placed_echo(tiles)),             # place
        ("pole_copper", "3/0/0"),                           # wire: 3 made
        ("game.tick", "900"),
        ("electric_network_id", ctx.fake.json("_pgrid", {
            "poles": [{"n": POLE, "x": x, "y": y, "e": 535} for x, y in tiles],
            "unpowered": []})),
        ("find_entities_filtered", ctx.fake.json("_world", [])),   # absorb fingerprint
    ]
    bp = P.apply(plan, area=(-3, -3, 11, 3), scan_tick=100)
    assert bp["status"] == "verified", bp["verify"]
    assert bp["verify"]["check"]["ok"] and "one network" in bp["verify"]["check"]["detail"]
    assert ctx.built == set(tiles), ctx.built
    wire = [c for c in ctx.fake.calls if "pole_copper" in c]
    assert len(wire) == 1 and "connect_to" in wire[0], "poles must be wired EXPLICITLY"
    assert bp["args"]["pairs"] == len(P.wire_pairs(tiles))


@_with_ctx
def test_apply_rolls_back_when_the_grid_comes_back_split(ctx):
    """The whole point of LAW 4's verify step: create_entity said ok for all three poles and
    the wiring command ran, yet the read-back shows two electric_network_ids. Build Law 2 -
    if the result is nothing, remove what you built, in the same pass."""
    tiles = [(0, 0), (4, 0), (8, 0)]
    plan = [{"x": x, "y": y, "entity": POLE} for x, y in tiles]
    ctx.fake.script = [
        ("radius=0.6", ctx.fake.json("_world", [])),
        ("create_entity", _placed_echo(tiles)),
        ("pole_copper", "0/0/3"),
        ("game.tick", "900"),
        ("electric_network_id", ctx.fake.json("_pgrid", {
            "poles": [{"n": POLE, "x": 0, "y": 0, "e": 1},
                      {"n": POLE, "x": 4, "y": 0, "e": 405},
                      {"n": POLE, "x": 8, "y": 0, "e": 405}],
            "unpowered": [{"n": "electric-mining-drill", "x": 4, "y": 4,
                           "s": "no_power"}]})),
        ("e.destroy()", "0,0;4,0;8,0"),                     # rollback, refunding
    ]
    bp = P.apply(plan, area=(-3, -3, 11, 8), scan_tick=100)
    assert bp["status"] == "failed", bp["status"]
    d = bp["verify"]["check"]["detail"]
    assert "SPLIT" in d and "unpowered" in d, d
    assert bp["verify"]["rollback"]["removed"] == 3, bp["verify"]["rollback"]
    assert ctx.forgotten == set(tiles), "the built ledger must forget what we tore out"
    assert bp["verify"]["placed"] == [], "rollback empties the plan's scope"


@_with_ctx
def test_apply_obeys_the_truce_and_the_protected_ledger(ctx):
    tiles = [(0, 0), (4, 0), (8, 0)]
    plan = [{"x": x, "y": y, "entity": POLE} for x, y in tiles]
    ctx.operator = True
    bp = P.apply(plan, area=(-3, -3, 11, 3), scan_tick=100)
    assert bp["status"] == "planned" and "OPERATOR PRESENT" in bp["verify"]["refused"]
    assert not ctx.fake.calls, "the truce must cost zero RCON"
    ctx.operator = False
    ctx.protected = set(tiles)                 # he deleted this pole line on purpose
    bp = P.apply(plan, area=(-3, -3, 11, 3), scan_tick=100)
    assert bp["status"] == "superseded" and "OPERATOR-OWNED" in bp["verify"]["refused"]
    assert not ctx.fake.calls


def _spine_script(ctx, tiles, wire, plan_net, root_net, root=(-15, 33), rollback=True):
    """Script for a spine applied with anchor=`root`, which sits OUTSIDE the plan's own area
    and therefore gets its own 1x1 read-back."""
    s = [("radius=0.6", ctx.fake.json("_world", [])),
         ("create_entity", _placed_echo(tiles)),
         ("pole_copper", wire),
         ("game.tick", "900"),
         ("electric_network_id", ctx.fake.json("_pgrid", {
             "poles": [{"n": POLE, "x": x, "y": y, "e": plan_net} for x, y in tiles],
             "unpowered": []})),
         ("electric_network_id", ctx.fake.json("_pgrid", {
             "poles": [{"n": POLE, "x": root[0], "y": root[1], "e": root_net}],
             "unpowered": []}))]
    # absorb() only runs on the success path; on failure the next call is the teardown (whose
    # own command also contains find_entities_filtered, so the two must not both be scripted).
    s.append(("e.destroy()", ";".join("%d,%d" % t for t in tiles)) if rollback else
             ("find_entities_filtered", ctx.fake.json("_world", [])))
    return s


@_with_ctx
def test_apply_wires_the_anchor_it_was_given(ctx):
    """`plan_grid(anchor=...)` plans a tie-in to an EXISTING pole; LAW 4 says nothing
    auto-connects, so apply() has to emit that wire. Passing only `existing=` left the anchor
    out of the wire pass entirely - the lattice reached the trunk and stayed on its own net."""
    tiles = [(-15, 5), (-15, 12), (-15, 19), (-15, 26)]
    plan = [{"x": x, "y": y, "entity": POLE} for x, y in tiles]
    ctx.fake.script = _spine_script(ctx, tiles, "4/0/0", 535, 535, rollback=False)
    bp = P.apply(plan, anchor=(-15, 33), scan_tick=100)
    assert bp["status"] == "verified", bp["verify"]["check"]
    wire = "".join(c for c in ctx.fake.calls if "pole_copper" in c)
    assert "-15,26,-15,33" in wire, "the anchor pole is never wired to: %s" % wire[-200:]
    assert bp["args"]["pairs"] == len(P.wire_pairs(tiles, [(-15, 33)])) == 4
    assert bp["args"]["join"] == [[-15, 33]], bp["args"]


@_with_ctx
def test_apply_fails_a_grid_that_came_up_on_its_own_network(ctx):
    """P2 / GOTCHAS: "read electric_network_id and compare to the ROOT's; never get close to a
    network". "One network in the area" is trivially true of a spine that wired to NOTHING -
    it is net 405 again, and it read as a clean pass until the root was checked."""
    tiles = [(-15, 5), (-15, 12), (-15, 19), (-15, 26)]
    plan = [{"x": x, "y": y, "entity": POLE} for x, y in tiles]
    ctx.fake.script = _spine_script(ctx, tiles, "0/0/4", 900, 535)
    bp = P.apply(plan, anchor=(-15, 33), scan_tick=100)
    assert bp["status"] == "failed", bp["verify"]["check"]
    d = bp["verify"]["check"]["detail"]
    assert "SPLIT" in d and "900" in d and "root grid is 535" in d, d
    assert bp["verify"]["rollback"]["removed"] == 4, "an islanded spine must be torn out"


@_with_ctx
def test_apply_fails_when_the_tie_in_pole_is_not_there(ctx):
    """The anchor was read from a stale scan and the operator has since removed it. That is a
    failed tie-in, not a grid to hand back as verified."""
    tiles = [(-15, 19), (-15, 26)]         # (-15,26) is 7 from the anchor: a legal tie-in
    plan = [{"x": x, "y": y, "entity": POLE} for x, y in tiles]
    ctx.fake.script = [("radius=0.6", ctx.fake.json("_world", [])),
                       ("create_entity", _placed_echo(tiles)),
                       ("pole_copper", "1/0/1"),
                       ("game.tick", "900"),
                       ("electric_network_id", ctx.fake.json("_pgrid", {
                           "poles": [{"n": POLE, "x": x, "y": y, "e": 900} for x, y in tiles],
                           "unpowered": []})),
                       ("electric_network_id", ctx.fake.json("_pgrid",
                                                             {"poles": [], "unpowered": []})),
                       ("e.destroy()", "-15,19;-15,26")]
    bp = P.apply(plan, anchor=(-15, 33), scan_tick=100)
    assert bp["status"] == "failed"
    assert "not joined to the existing grid" in bp["verify"]["check"]["detail"]


@_with_ctx
def test_place_fn_keeps_the_wire_tally(ctx):
    """`wire_lua` echoes made/already/missing precisely so a silent islanding is visible;
    discarding it left verify_fn as the only witness."""
    tiles = [(0, 0), (4, 0)]
    ctx.fake.script = [("create_entity", _placed_echo(tiles)), ("pole_copper", "1/2/3")]
    res = P.make_place_fn(tiles)(None, tiles)
    assert res["wired"] == {"made": 1, "already": 2, "missing": 3}, res["wired"]


@_with_ctx
def test_apply_is_registered_for_crash_resume(ctx):
    """buildplan.resume() must be able to re-verify a crashed grid with no caller context:
    the pole name and the area both live in the record's own args."""
    assert P.GRID_KIND in B.KINDS and B.KINDS[P.GRID_KIND]["verify"]
    bp = {"id": "x", "kind": P.GRID_KIND, "tiles": [[0, 0], [4, 0]],
          "args": {"pole": POLE, "area": [-3, -3, 7, 3]}, "names": [POLE], "verify": {}}
    ctx.fake.script = [("electric_network_id", ctx.fake.json("_pgrid", {
        "poles": [{"n": POLE, "x": 0, "y": 0, "e": 535},
                  {"n": POLE, "x": 4, "y": 0, "e": 535}], "unpowered": []}))]
    ok, detail = B.KINDS[P.GRID_KIND]["verify"](bp)
    assert ok, detail


@_with_ctx
def test_resume_rebuilds_the_join_set_it_was_applied_with(ctx):
    """A resumed pass that has forgotten the anchor wires the plan to ITSELF and finishes the
    crashed build into the island the first pass was avoiding, so `join` lives in the record."""
    bp = {"id": "x", "kind": P.GRID_KIND, "tiles": [[-15, 5], [-15, 12]],
          "args": {"pole": POLE, "area": [-19, 1, -11, 16], "join": [[-15, 19]],
                   "consume": False},
          "names": [POLE], "verify": {}}
    ctx.fake.script = [("create_entity", _placed_echo([(-15, 5), (-15, 12)])),
                       ("pole_copper", "2/0/0")]
    B.KINDS[P.GRID_KIND]["place"](bp, [(-15, 5), (-15, 12)])
    wire = "".join(c for c in ctx.fake.calls if "pole_copper" in c)
    assert "-15,12,-15,19" in wire, "resume dropped the anchor: %s" % wire[-200:]
    place = "".join(c for c in ctx.fake.calls if "create_entity" in c)
    assert "inv.remove" not in place, "resume dropped consume=False and paid for the poles"

    # and the resumed VERIFIER checks the same root the first pass did
    ctx.fake.script = [("electric_network_id", ctx.fake.json("_pgrid", {
                           "poles": [{"n": POLE, "x": -15, "y": 5, "e": 900},
                                     {"n": POLE, "x": -15, "y": 12, "e": 900}],
                           "unpowered": []})),
                       ("electric_network_id", ctx.fake.json("_pgrid", {
                           "poles": [{"n": POLE, "x": -15, "y": 19, "e": 535}],
                           "unpowered": []}))]
    ok, detail = B.KINDS[P.GRID_KIND]["verify"](bp)
    assert not ok and "root grid is 535" in detail, detail


# --------------------------------------------------------------------------- end to end
def test_plan_apply_audit_round_trip():
    """Plan the operator's smelter stack, then audit the entity set it would produce: a
    lattice this module emits must not trip its own cleanup pass."""
    cons, obs, area = smelter()
    plan = P.plan_grid(area, cons, obstacles=obs)
    ents = [pole_ent(s["x"], s["y"]) for s in plan] + \
           [ins_ent(*P.consumer_box(c)[:2]) for c in cons]
    rep = P.audit(area, ents=ents)
    errs = [f for f in rep if f["severity"] == "error"]
    assert not errs, [f["msg"] for f in errs]


def test_cli_trunk_mode_plans_without_writing():
    """`power_planner.py trunk ...` must print a plan and the commands it WOULD run, and
    issue no RCON at all (belt_router's dry-run precedent)."""
    ctx = Ctx()
    try:
        assert P._main(["power_planner.py", "trunk", "-15", "-65", "-15", "26"]) == 0
        assert not ctx.fake.calls, ctx.fake.calls
        assert P._main(["power_planner.py"]) == 2
    finally:
        ctx.close()


# ------------------------------------------------- the chunked read that spliced two scans
#
# 2026-08-29 23:27:07 live: "invariants: power audit failed (Expecting ',' delimiter: line 1
# column 9003 (char 9002))". Not truncation, not UTF-8, not a trimmed rcon.print - the audit
# thread was reading storage._pgrid in 3000-char slices while the builder's array_grid scan
# wrote its own document to that same key. The reassembled string was the first 9000 chars of
# a 46926-char scan spliced onto the tail of a 32962-char one: two valid documents, meeting at
# a chunk boundary, and no reader in the repo compared the reassembled length against the
# length Lua had reported.


def _big_scan(n):
    """A payload comfortably longer than one 3000-char slice."""
    return {"ents": [{"n": "small-electric-pole", "t": "electric-pole", "x": i, "y": 0,
                      "bb": [i, 0, i, 0], "s": "working"} for i in range(n)]}


@_with_ctx
def test_a_buffer_clobbered_mid_read_is_caught_not_parsed(ctx):
    """THE DEFECT. A writer landing between two slice reads is now a named error with the
    delta in it, instead of a JSONDecodeError at an offset that means nothing."""
    ctx.fake.script = [("find_entities_filtered", ctx.fake.json("_pgrid", _big_scan(120)))] * 2
    ctx.fake.clobber_after(1, _big_scan(4))          # a different, shorter document
    try:
        P.scan((-5, -5, 5, 5))
        raise AssertionError("a spliced read must not be parsed as an answer")
    except rcon.ChunkedReadError as e:
        assert "reassembled" in str(e) and "Lua reported" in str(e), e
        assert "delta" in str(e), e


@_with_ctx
def test_every_chunked_read_mints_its_own_buffer_key(ctx):
    """The primary fix. Two reads can no longer share a buffer, so a concurrent writer cannot
    cause the mismatch above at all - the length check is only the backstop."""
    ctx.fake.script = [("find_entities_filtered", ctx.fake.json("_pgrid", _big_scan(40)))] * 2
    P.scan((-5, -5, 5, 5))
    P.scan((-5, -5, 5, 5))
    keys = {m.group(1) for m in (_STORE_WRITE.search(c) for c in ctx.fake.calls) if m}
    assert len(keys) == 2, "both reads used the same buffer key: %s" % keys
    assert all(k.startswith("storage._rd") for k in keys), keys
    for k in keys:
        assert any("%s=nil" % k in c for c in ctx.fake.calls), "scratch %s never cleared" % k


@_with_ctx
def test_a_lua_error_is_never_read_as_an_empty_area(ctx):
    """int(prose) swallowed would read as 'nothing is there'. For read_grid - a buildplan
    verify_fn - that answer would fail the verify and ROLL BACK a lattice that was built
    correctly, so a failed read must be indistinguishable from no read, never from zero."""
    ctx.fake.script = [("electric_network_id", "Error: the mod caused a non-recoverable error")] * 2
    try:
        P.read_grid((-5, -5, 5, 5))
        raise AssertionError("a Lua error must not read back as an empty grid")
    except rcon.ChunkedReadError as e:
        assert "unreadable" in str(e), e


def test_no_module_hand_rolls_a_chunked_read_any_more():
    """The lesson generalised. lane_lint hit this bug in 2026-08 and fixed it locally with a
    private key; the fix never left that file, and power_planner was still splicing scans a
    month later. One reader means the next instance cannot be written by accident.

    storage.fle_out is the one exemption: the key is chosen by the VENDORED lua/fle_lib.lua
    (fle.out()), not by Python, so it cannot be minted per read from this side.
    """
    offenders = []
    for path in sorted(pathlib.Path(__file__).resolve().parent.glob("*.py")):
        if path.name.startswith("test_") or path.name == "rcon.py":
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\bstorage\.[A-Za-z0-9_]+:sub\(", line) and "fle_out" not in line:
                offenders.append("%s:%d" % (path.name, i))
    assert not offenders, ("hand-rolled chunked slice read(s) - use rcon.read_chunked: %s"
                           % offenders)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    ok = fail = 0
    for t in TESTS:
        try:
            t()
        except Exception:
            fail += 1
            print("FAIL %s" % t.__name__)
            print(traceback.format_exc())
        else:
            ok += 1
            print("PASS %s" % t.__name__)
    print("\n%d passed, %d failed (%d total)" % (ok, fail, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
