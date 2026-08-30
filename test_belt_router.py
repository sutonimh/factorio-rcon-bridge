#!/usr/bin/env python3
"""Offline unit tests for belt_router.py — NO live server.

Run with either:
    python3 test_belt_router.py
    python3 -m pytest test_belt_router.py

Every routing test builds a synthetic obstacle grid; only test_scan_parsing touches rcon,
and it installs a scripted fake that speaks the chunked storage._broute protocol (length,
then :sub slices) so Obstacles.from_scan is exercised for real. Nothing here can reach the
game: plan_to_lua returns strings, and test_plan_to_lua_is_safe stubs rcon.run to raise so a
regression that starts EXECUTING commands fails loudly.
"""
import json
import re
import traceback

import rcon
import belt_router as B


# --------------------------------------------------------------------------- harness
class FakeRcon:
    """Scripted rcon.run for the chunked storage._broute read: the first call (the scan /sc)
    returns the payload length, later :sub calls serve slices (with the trailing newline
    rcon.print really appends — the router must rstrip it)."""
    def __init__(self, payload):
        self.payload = json.dumps(payload, separators=(",", ":"))
        self.calls = []

    def __call__(self, cmd, timeout=10.0):
        self.calls.append(cmd)
        m = re.search(r"storage\._broute:sub\((\d+),(\d+)\)", cmd)
        if m:
            i, j = int(m.group(1)), int(m.group(2))
            return self.payload[i - 1:j] + "\n"
        return str(len(self.payload))


def wall(xs, ys, name="transport-belt", d=8, t="surface"):
    """A foreign belt lane: every tile holds a belt we may not overwrite."""
    return {(x, y): {"name": name, "dir": d, "type": t} for x in xs for y in ys}


def moves(plan):
    """[(from, to, direction, length)] for each hop between consecutive steps."""
    out = []
    for i in range(len(plan) - 1):
        a = (plan[i]["x"], plan[i]["y"])
        b = (plan[i + 1]["x"], plan[i + 1]["y"])
        out.append((a, b, B.rel_dir(a, b), B.chebyshev(a, b)))
    return out


def lua_entries(cmds):
    """Every (x,y,dir,name,type) create_entity plan_to_lua actually emits."""
    out = []
    for c in cmds:
        names = re.search(r"local NM=\{([^}]*)\}", c).group(1)
        names = [n.strip().strip("'") for n in names.split(",")]
        spec = re.search(r"\[==\[(.*?)\]==\]", c, re.S).group(1)
        for e in spec.split(";"):
            x, y, d, k, u = (int(v) for v in e.split(","))
            out.append((x, y, d, names[k], (None, "input", "output")[u]))
    return out


# --------------------------------------------------------------------------- tests
def test_straight_path():
    obs = B.Obstacles(bounds=(-3, -3, 9, 3))
    plan = B.plan_route((0, 0), (6, 0), obstacles=obs)
    assert plan is not None, B.LAST_ERROR
    assert len(plan) == 7, plan
    assert [(s["x"], s["y"]) for s in plan] == [(x, 0) for x in range(7)]
    assert all(s["entity"] == "transport-belt" and s["dir"] == 4 for s in plan), plan
    assert all("type" not in s for s in plan), "no undergrounds on an empty grid"
    assert B.route_cost(plan) == 6


def test_routes_around_building():
    block = {(4, 0), (5, 0), (4, 1), (5, 1)}          # a 2x2 astride the straight line
    obs = B.Obstacles(hard=block, bounds=(-2, -4, 10, 4))
    plan = B.plan_route((0, 0), (8, 0), obstacles=obs)
    assert plan is not None, B.LAST_ERROR
    tiles = set(B.plan_tiles(plan))
    assert not (tiles & block), tiles & block
    turns = sum(1 for i in range(len(moves(plan)) - 1)
                if moves(plan)[i][2] != moves(plan)[i + 1][2])
    assert turns >= 2, moves(plan)
    # a detour costs +2; an underground over the block would cost +6, so A* must not tunnel
    assert all(s["entity"] == "transport-belt" for s in plan), plan
    assert B.route_cost(plan) == 10, B.route_cost(plan)


def test_underground_for_3_wide_crossing():
    lane = wall(range(4, 7), range(-6, 7))            # 3-wide foreign lane, running south
    obs = B.Obstacles(belts=lane, bounds=(-2, -10, 12, 10))
    plan = B.plan_route((0, 0), (10, 0), obstacles=obs)
    assert plan is not None, B.LAST_ERROR
    ins = [s for s in plan if s.get("type") == "input"]
    outs = [s for s in plan if s.get("type") == "output"]
    assert len(ins) == 1 and len(outs) == 1, plan
    assert ins[0]["entity"] == outs[0]["entity"] == "underground-belt"
    delta = B.chebyshev((ins[0]["x"], ins[0]["y"]), (outs[0]["x"], outs[0]["y"]))
    assert delta == 4 and delta <= 5, delta
    assert ins[0]["dir"] == outs[0]["dir"] == 4, plan          # both carry the TRAVEL direction
    assert [tuple(t) for t in ins[0]["span"]] == [(4, 0), (5, 0), (6, 0)]
    assert not (set(B.plan_tiles(plan)) & set(lane)), "never place on the foreign lane"


def test_refuses_reserved_inserter_tile():
    # an inserter's drop tile reads free but jams the line if you build on it
    obs = B.Obstacles(reserved={(4, 0)}, bounds=(-2, -4, 10, 4))
    plan = B.plan_route((0, 0), (8, 0), obstacles=obs)
    assert plan is not None, B.LAST_ERROR
    assert (4, 0) not in B.plan_tiles(plan), plan
    # walled to a 1-tall corridor with a 5-wide reservation: too wide for any legal hop
    # (delta 6 > max 5) and no room to detour -> refuse rather than jam the line
    obs2 = B.Obstacles(reserved={(x, 0) for x in range(4, 9)}, bounds=(0, 0, 12, 0))
    assert B.plan_route((0, 0), (12, 0), obstacles=obs2) is None
    assert B.LAST_ERROR


def test_adopts_collinear_same_direction_belt():
    good = {(3, 0): {"name": "transport-belt", "dir": 4, "type": "surface"}}
    obs = B.Obstacles(belts=good, bounds=(-2, -4, 10, 4))
    plan = B.plan_route((0, 0), (6, 0), obstacles=obs)
    assert plan is not None, B.LAST_ERROR
    step = [s for s in plan if (s["x"], s["y"]) == (3, 0)][0]
    assert step["adopt"] is True, step
    assert sum(1 for s in plan if s.get("adopt")) == 1
    emitted = {(x, y) for (x, y, _d, _n, _t) in lua_entries(B.plan_to_lua(plan))}
    assert (3, 0) not in emitted, "an adopted belt must emit NO command"
    assert emitted == set(B.plan_tiles(plan)) - {(3, 0)}
    # same tile facing the other way is someone else's lane: hard, so the route detours
    bad = {(3, 0): {"name": "transport-belt", "dir": 12, "type": "surface"}}
    plan2 = B.plan_route((0, 0), (6, 0), obstacles=B.Obstacles(belts=bad, bounds=(-2, -4, 10, 4)))
    assert plan2 is not None, B.LAST_ERROR
    assert (3, 0) not in B.plan_tiles(plan2), plan2
    assert not any(s.get("adopt") for s in plan2)


def test_no_path_returns_none():
    # a wall 7 tiles thick in a 3-tall corridor: no detour, and no hop can span it either
    # (delta would be 8, over the 5-tile maximum). A THIN wall is NOT unreachable — an
    # underground legitimately tunnels under it and surfaces on the far side.
    walled = B.Obstacles(hard={(x, y) for x in range(3, 10) for y in (-1, 0, 1)},
                         bounds=(-2, -1, 12, 1))
    assert B.plan_route((0, 0), (11, 0), obstacles=walled) is None
    assert B.LAST_ERROR
    hard = B.Obstacles(hard={(0, 0), (6, 0)}, bounds=(-2, -4, 10, 4))
    assert B.plan_route((0, 0), (5, 0), obstacles=hard) is None      # start on hard
    assert "start" in B.LAST_ERROR
    assert B.plan_route((1, 0), (6, 0), obstacles=hard) is None      # goal on hard
    assert "goal" in B.LAST_ERROR


def test_astar_constraints():
    lane = wall(range(4, 7), range(-10, 11))
    lane.update(wall(range(12, 15), range(-10, 11)))
    obs = B.Obstacles(belts=lane, bounds=(-2, -11, 20, 11))
    plan = B.plan_route((0, 0), (18, 0), obstacles=obs)
    assert plan is not None, B.LAST_ERROR
    mv = moves(plan)
    assert sum(1 for s in plan if s.get("type") == "input") == 2, plan
    for i in range(len(mv) - 1):
        assert mv[i + 1][2] != B.opposite(mv[i][2]), "a move reversed: %s" % (mv[i:i + 2],)
    for i, s in enumerate(plan):
        if s.get("type") == "input":
            assert i + 1 < len(plan) and plan[i + 1].get("type") == "output"
            if i > 0:                       # no underground may BEGIN on a turn tile
                assert mv[i - 1][2] == mv[i][2], "underground started on a turn: %s" % (s,)
        if s.get("type") == "output":
            assert plan[i - 1].get("type") == "input"
            assert i == 0 or plan[i - 1].get("type") != "output"
            if i + 1 < len(plan):           # fix #1: after an exit, straight and length 1
                assert mv[i][3] == 1, "long hop straight out of an exit: %s" % (mv[i],)
                assert mv[i][2] == mv[i - 1][2], "turned straight out of an exit"
                assert plan[i + 1].get("type") != "input", "chained undergrounds"


def test_underground_span_rules():
    # a same-name underground on a PARALLEL axis under the span interlocks with the pair
    base = {(4, 0), (6, 0)}
    par = {(5, 0): {"name": "underground-belt", "dir": 12, "type": "input"}}
    obs = B.Obstacles(hard=base, belts=par, bounds=(0, 0, 10, 0))
    assert B.plan_route((0, 0), (10, 0), obstacles=obs) is None, "must not span a parallel ug"
    # a PERPENDICULAR one does not interlock: the same hop is legal
    perp = {(5, 0): {"name": "underground-belt", "dir": 0, "type": "input"}}
    plan = B.plan_route((0, 0), (10, 0),
                        obstacles=B.Obstacles(hard=base, belts=perp, bounds=(0, 0, 10, 0)))
    assert plan is not None, B.LAST_ERROR
    ins = [s for s in plan if s.get("type") == "input"]
    assert len(ins) == 1 and (ins[0]["x"], ins[0]["y"]) == (3, 0), plan
    assert (5, 0) in [tuple(t) for t in ins[0]["span"]]


def test_route_cost_and_conflicts():
    lane = wall(range(4, 7), range(-6, 7))
    plan = B.plan_route((0, 0), (10, 0),
                        obstacles=B.Obstacles(belts=lane, bounds=(-2, -10, 12, 10)))
    assert plan is not None, B.LAST_ERROR
    assert B.route_cost(plan) == 3 + 4 * 3 + 3, B.route_cost(plan)   # 1/tile + 3*length/hop
    straight = B.plan_route((0, 0), (7, 0), obstacles=B.Obstacles(bounds=(-2, -2, 9, 2)))
    assert len(B.plan_tiles(straight)) == 8
    c = B.plan_conflicts(straight, [(2, 0), (3, 0)])
    assert c["count"] == 2 and c["tiles"] == [(2, 0), (3, 0)]
    assert abs(c["fraction"] - 0.25) < 1e-9 and c["operator_owned"] is True   # BUILD LAW 3
    c1 = B.plan_conflicts(straight, [(2, 0)])
    assert c1["count"] == 1 and c1["operator_owned"] is False
    assert B.plan_conflicts(straight, [])["operator_owned"] is False
    # span tiles carry no entity, so they are not "touched" unless explicitly asked for
    assert (5, 0) not in B.plan_tiles(plan)
    assert (5, 0) in B.plan_tiles(plan, include_spans=True)


def test_plan_to_lua_is_safe():
    orig = rcon.run
    rcon.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("plan_to_lua executed RCON"))
    try:
        lane = wall(range(4, 7), range(-6, 7))
        plan = B.plan_route((0, 0), (10, 0),
                            obstacles=B.Obstacles(belts=lane, bounds=(-2, -10, 12, 10)))
        cmds = B.plan_to_lua(plan)
        assert cmds
        for c in cmds:
            assert len(c) <= B.CMD_LIMIT, len(c)
            # the ONLY destroy is the tree/rock clear; no belt or building is ever removed
            assert c.count("destroy") == 2, c
            assert ("type={'tree','simple-entity'}}) do if e.destroy then e.destroy() end"
                    in c), c
            assert "can_place_entity" in c
            assert "inv.remove{name=nm,count=1}" in c            # keep-it-legit: consume stock
        ents = lua_entries(cmds)
        assert {(x, y) for (x, y, _d, _n, _t) in ents} == set(B.plan_tiles(plan))
        ug = [e for e in ents if e[4] is not None]
        assert [e[4] for e in ug] == ["input", "output"], ug
        assert ug[0][2] == ug[1][2] == 4, ug                     # equal TRAVEL directions
        assert all(e[3] == "underground-belt" for e in ug)
        assert not B.plan_to_lua([])
        assert "inv.remove" not in "".join(B.plan_to_lua(plan, consume=False))
        # pipe-to-ground: entrance faces BACK along travel, exit faces travel, and NO type=
        # is emitted (fle_lib.lua:208) — the halves are told apart by direction alone
        pipe = B.plan_route((0, 0), (12, 0), kind="pipe",
                            obstacles=B.Obstacles(hard={(x, 0) for x in range(4, 8)},
                                                  bounds=(0, 0, 12, 0)))
        assert pipe is not None, B.LAST_ERROR
        pin = [s for s in pipe if s.get("type") == "input"][0]
        pout = [s for s in pipe if s.get("type") == "output"][0]
        assert pin["dir"] == B.opposite(pout["dir"]) == 12 and pout["dir"] == 4, pipe
        assert pin["entity"] == pout["entity"] == "pipe-to-ground"
        assert all(e[4] is None for e in lua_entries(B.plan_to_lua(pipe))), "pipes take no type="
    finally:
        rcon.run = orig


def test_scan_parsing():
    orig = rcon.run
    # a reservation at (14,3) whose owning inserter sits OUTSIDE the requested rect (the pad
    # case: without the padded second query this tile reads free and gets built on)
    payload = {"b": [-6, -6, 16, 16],
               "h": ["5,5", "5,6"],
               # 5,5 and 2,1 are also claimed as reservations: a reservation must never mask
               # a building or a belt (inside a lane every tile is its predecessor's feed
               # target, which would make the whole lane un-adoptable)
               "r": ["3,3", "14,3", "5,5", "2,1"],
               "belts": [{"x": 1, "y": 1, "d": 4, "n": "transport-belt", "t": "surface"},
                         {"x": 2, "y": 1, "d": 4, "n": "underground-belt", "t": "input"}],
               "ug": {"underground-belt": 5, "pipe-to-ground": 10}}
    rcon.run = FakeRcon(payload)
    try:
        obs = B.scan_obstacles(0, 0, 10, 10)
        assert obs.hard == {(5, 5), (5, 6)}, obs.hard
        assert obs.reserved == {(3, 3), (14, 3)}, obs.reserved
        assert obs.belts[(1, 1)] == {"name": "transport-belt", "dir": 4, "type": "surface"}
        assert obs.belts[(2, 1)]["type"] == "input"
        assert obs.bounds == (-6, -6, 16, 16)
        assert obs.under_max["underground-belt"] == 5
        # the live prototype value wins over the module default
        assert len(rcon.run.calls) >= 2 and "_broute:sub(1," in rcon.run.calls[1]
    finally:
        rcon.run = orig
    cmd = B.scan_lua(0, 0, 10, 10, pad=6, res_pad=5)
    assert len(cmd) <= B.CMD_LIMIT, len(cmd)
    for forbidden in ("create_entity", "destroy", ".remove{", "set_recipe", "walking_state"):
        assert forbidden not in cmd, forbidden                   # READ-ONLY, absolutely
    assert "local X1,Y1,X2,Y2=-6,-6,16,16" in cmd                # entity scan is padded
    assert "local P=5" in cmd
    assert "area={{X1-P,Y1-P},{X2+1+P,Y2+1+P}}" in cmd           # reservations padded again
    assert "if not H[k] and not BK[k] then" in cmd               # never mask a building/belt


def test_long_dense_run_falls_back_to_weighted():
    """A 200-tile cross-base lane through 32 foreign lanes costs far more than manhattan, so
    the optimal pass exhausts MAX_EXPANSIONS. Returning None there would push the caller back
    onto lay_belt_path — the destructive thing this module replaces — so it retries weighted.
    The route stays fully LEGAL either way: every constraint lives in the successor test."""
    lanes = wall(range(10, 200, 6), range(-20, 21))
    obs = B.Obstacles(belts=lanes, bounds=(-5, -20, 210, 20))
    plan = B.plan_route((0, 0), (200, 0), obstacles=obs)
    assert plan is not None, B.LAST_ERROR
    assert B.LAST_ERROR == "", "success must not set LAST_ERROR"
    assert B.LAST_WEIGHT > 1, "this case is expected to need the weighted pass"
    assert not (set(B.plan_tiles(plan)) & set(lanes)), "never lands on a foreign lane"
    assert sum(1 for s in plan if s.get("type") == "input") == 32
    assert B.route_cost(plan) == 200 + 4 * 32     # 1/tile, +4 per 2-tile crossing
    B.plan_route((0, 0), (6, 0), obstacles=B.Obstacles(bounds=(-3, -3, 9, 3)))
    assert B.LAST_WEIGHT == 1, "an easy route must not report a weighted pass"


def test_never_emits_a_self_crossing_plan():
    """Regression: the A* state is (last_last, last, cur) — no memory of the tiles already
    used — so in a dense field the route could CROSS ITSELF and plan two entities on one tile.
    In game the second create_entity just fails can_place_entity and is skipped: the lane ends
    up pointing the wrong way, or an underground exit stands with no entrance. Found by fuzzing
    (~0.5-3% of dense routes, at weight 1 — not only in the weighted fallback)."""
    import random
    rng = random.Random(1234)
    checked = 0
    for _ in range(120):
        W, H = rng.randint(25, 60), rng.randint(4, 20)
        hard, res, belts = set(), set(), {}
        dens = rng.uniform(0.25, 0.55)
        for x in range(W + 1):
            for y in range(H + 1):
                r = rng.random()
                if r < dens * 0.6:
                    hard.add((x, y))
                elif r < dens * 0.8:
                    res.add((x, y))
                elif r < dens:
                    belts[(x, y)] = {"name": "transport-belt", "dir": rng.choice((0, 4, 8, 12)),
                                     "type": rng.choice(("surface", "surface", "input", "output"))}
        hard -= set(belts)
        res -= hard | set(belts)
        start, goal = (0, rng.randint(0, H)), (W, rng.randint(0, H))
        for p in (start, goal):
            hard.discard(p)
            belts.pop(p, None)
        plan = B.plan_route(start, goal, obstacles=B.Obstacles(
            hard=hard, reserved=res, belts=belts, bounds=(0, 0, W, H)))
        if plan is None:
            continue
        checked += 1
        tiles = B.plan_tiles(plan)
        assert len(set(tiles)) == len(tiles), (
            "plan uses a tile twice: %s" % sorted({t for t in tiles if tiles.count(t) > 1}))
        # the own-plan twin of the Konano span rule: an underground half inside one of our
        # OWN spans on a parallel axis re-pairs that span and kills the hop
        spans = [(set(map(tuple, s["span"])), s["dir"]) for s in plan if s.get("type") == "input"]
        for s in plan:
            if s.get("type") in ("input", "output"):
                for sp, sd in spans:
                    assert not ((s["x"], s["y"]) in sp and (s["dir"] - sd) % 8 == 0), s
    assert checked > 40, checked


def test_goal_dir_is_honoured_even_at_an_underground_exit():
    """Regression: goal_reached accepted ANY underground arrival, and assembly then wrote the
    TRAVEL direction on the exit — so a caller-specified goal_dir was silently dropped ~7% of
    the time. An exit cannot be turned, so such a route must be rejected, not mis-emitted."""
    hard = {(x, 0) for x in range(4, 8)}
    obs = B.Obstacles(hard=hard, bounds=(0, -3, 9, 3))
    # goal (8,0) is reachable only as an underground exit travelling EAST
    plan = B.plan_route((0, 0), (8, 0), obstacles=B.Obstacles(hard=hard, bounds=(0, 0, 8, 0)))
    assert plan is not None and plan[-1]["type"] == "output" and plan[-1]["dir"] == 4, plan
    # asking for that exit to face SOUTH is impossible in a 1-tall corridor -> refuse
    assert B.plan_route((0, 0), (8, 0), goal_dir=8,
                        obstacles=B.Obstacles(hard=hard, bounds=(0, 0, 8, 0))) is None
    assert B.LAST_ERROR
    # with room to surface first it routes and the LAST tile really carries goal_dir
    p2 = B.plan_route((0, 0), (8, 0), goal_dir=8, obstacles=obs)
    assert p2 is not None, B.LAST_ERROR
    assert p2[-1]["dir"] == 8 and p2[-1].get("type") is None, p2
    for gd in (0, 4, 8, 12):
        p = B.plan_route((0, 0), (8, 0), goal_dir=gd, obstacles=obs)
        assert p is None or p[-1]["dir"] == gd, (gd, p)


def test_lua_inventory_guard_is_truthiness():
    """`storage.derpface and storage.derpface.valid and ...` yields FALSE (not nil) for an
    invalid derpface; the old `inv~=nil` guard then indexed a boolean and aborted the whole
    /sc. fle_lib's F.take (lua/fle_lib.lua:52) tests truthiness — match it."""
    plan = B.plan_route((0, 0), (4, 0), obstacles=B.Obstacles(bounds=(0, 0, 4, 0)))
    lua = "".join(B.plan_to_lua(plan))
    assert "inv~=nil" not in lua, lua
    assert "local took=(inv and inv.get_item_count(nm)>0)" in lua, lua


def test_underground_tier_table_is_complete():
    """UNDER_FOR.get falls back to 'underground-belt' (max 5), so a belt tier missing from the
    table would silently plan YELLOW undergrounds into a faster lane. Live-probed 2026-08-30:
    5 / 7 / 9 / 11 / 10."""
    assert B.MAX_UNDER == {"underground-belt": 5, "fast-underground-belt": 7,
                           "express-underground-belt": 9, "turbo-underground-belt": 11,
                           "pipe-to-ground": 10}
    for belt, under in B.UNDER_FOR.items():
        assert under in B.MAX_UNDER, (belt, under)
    for tier, umax in (("fast-transport-belt", 7), ("express-transport-belt", 9),
                       ("turbo-transport-belt", 11)):
        plan = B.plan_route((0, 0), (umax, 0), name=tier,
                            obstacles=B.Obstacles(hard={(x, 0) for x in range(1, umax)},
                                                  bounds=(0, 0, umax, 0)))
        assert plan is not None, (tier, B.LAST_ERROR)
        assert plan[0]["entity"] == B.UNDER_FOR[tier], plan   # the tier's OWN underground
        assert B.chebyshev((plan[0]["x"], plan[0]["y"]), (plan[1]["x"], plan[1]["y"])) == umax
    assert "'turbo-underground-belt'" in B.scan_lua(0, 0, 4, 4)


def test_lane_tiles_stay_adoptable():
    """Regression (found live): the Konano feed-target rule reserved every tile of every
    existing lane, so a continuous belt row became untraversable end to end."""
    lane = {(x, 0): {"name": "transport-belt", "dir": 4, "type": "surface"} for x in range(0, 7)}
    feed_targets = {(x, 0) for x in range(1, 8)}          # what the raw rule would reserve
    obs = B.Obstacles(belts=lane, reserved=feed_targets, bounds=(-2, -2, 9, 2))
    obs.reserved -= set(obs.belts)                        # the invariant from_scan enforces
    plan = B.plan_route((0, 0), (6, 0), obstacles=obs)
    assert plan is not None, B.LAST_ERROR
    assert all(s.get("adopt") for s in plan), plan        # the whole run is already there
    assert B.plan_to_lua(plan) == []                      # so it builds nothing at all


# --------------------------------------------------------------------------- plain runner
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
