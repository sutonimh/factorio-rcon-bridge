#!/usr/bin/env python3
"""Offline unit tests for rails.py - NO live server.

Run with:
    python3 test_rails.py

The golden paths were CAPTURED from the upstream node implementation
(factorio-planning-agent/agent-workspace/lib/rails.js) so a divergence in the port shows up
as a failing test, not as a rail chain the game rejects. verify_against_engine() is driven
by a scripted FakeRcon in the test_world_executor.py style; nothing here opens a socket.
"""
import traceback

import rcon
import rails


# --------------------------------------------------------------------------- harness
class FakeRcon:
    """Scripted rcon.run: a list of (substring, response) steps consumed in order."""

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


def _with_rcon(script):
    def deco(fn):
        def wrapper():
            orig = rcon.run
            rcon.run = FakeRcon(script)
            try:
                fn(rcon.run)
            finally:
                rcon.run = orig
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


START = {"piece": "straight-rail", "dir": 0, "x": 601, "y": 601}

# Captured verbatim from `node -e "require('./lib/rails.js').route(...)"`.
GOLD_STRAIGHT = [("straight-rail", 601, 601 - 2 * i, 0) for i in range(7)]
GOLD_CORNER = [
    ("straight-rail", 601, 601, 0), ("curved-rail-a", 601, 598, 0),
    ("curved-rail-a", 599, 592, 8), ("curved-rail-a", 599, 588, 2),
    ("curved-rail-b", 601, 583, 2), ("curved-rail-b", 605, 579, 12),
    ("curved-rail-a", 610, 577, 12), ("straight-rail", 613, 577, 4),
    ("straight-rail", 615, 577, 4), ("straight-rail", 617, 577, 4),
    ("straight-rail", 619, 577, 4), ("straight-rail", 621, 577, 4),
    ("straight-rail", 623, 577, 4), ("straight-rail", 625, 577, 4),
]


def tup(chain):
    return [(p["name"], p["x"], p["y"], p["direction"]) for p in chain]


# --------------------------------------------------------------------------- table
def test_table_shape():
    assert len(rails.ADJ) == 24, len(rails.ADJ)
    assert all(len(v) == 6 for v in rails.ADJ.values())
    assert sum(len(v) for v in rails.ADJ.values()) == 144
    for k, succ in rails.ADJ.items():
        piece, _, d = k.rpartition("|")
        assert piece in rails.PIECES, piece
        assert int(d) in rails.DIRS
        # straight + half-diagonal only exist in 4 of the 8 even directions
        if piece in ("straight-rail", "half-diagonal-rail"):
            assert int(d) in (0, 2, 4, 6), k
        for n in succ:
            assert n["piece"] in rails.PIECES and n["dir"] in rails.DIRS, n
            assert float(n["dx"]).is_integer() and float(n["dy"]).is_integer(), n
    for p in rails.PIECES:
        assert p in rails.RAIL_ITEM_COST


def test_every_edge_is_reversible():
    """The transcription canary: the engine graph is undirected, so every edge must have
    its exact inverse. A single mistyped offset breaks this."""
    edges = set()
    for k, succ in rails.ADJ.items():
        piece, _, d = k.rpartition("|")
        for n in succ:
            edges.add((piece, int(d), n["piece"], n["dir"], n["dx"], n["dy"]))
    missing = [e for e in edges if (e[2], e[3], e[0], e[1], -e[4], -e[5]) not in edges]
    assert not missing, "%d non-reversible edge(s), e.g. %s" % (len(missing), missing[:2])


def test_neighbors():
    ns = rails.neighbors("straight-rail", 0)
    assert len(ns) == 6
    assert {"piece": "straight-rail", "dir": 0, "dx": 0.0, "dy": -2.0} in ns
    assert {"piece": "curved-rail-a", "dir": 0, "dx": 0.0, "dy": -3.0} in ns
    assert rails.neighbors("straight-rail", 8) == []        # straights have no dir 8
    assert rails.neighbors("legacy-straight-rail", 0) == []
    assert rails.neighbors("nonsense", 0) == []


# --------------------------------------------------------------------------- search
def test_golden_straight_run():
    goal = {"piece": "straight-rail", "dir": 0, "x": 601, "y": 589}
    for strict in (False, True):
        chain = rails.route(START, goal, strict=strict)
        assert len(chain) == 7, len(chain)
        assert tup(chain) == GOLD_STRAIGHT, tup(chain)
    assert all(isinstance(p["x"], int) and isinstance(p["y"], int) for p in chain)


def test_golden_corner():
    goal = {"piece": "straight-rail", "dir": 4, "x": 625, "y": 577}
    raw = rails.route(START, goal, strict=False)
    assert tup(raw) == GOLD_CORNER, tup(raw)
    assert tup(rails.route(START, goal)) == GOLD_CORNER   # already valid: strict is a no-op
    assert tup(raw)[0] == ("straight-rail", 601, 601, 0)
    assert tup(raw)[1] == ("curved-rail-a", 601, 598, 0)
    assert tup(raw)[-3:] == [("straight-rail", 621, 577, 4), ("straight-rail", 623, 577, 4),
                             ("straight-rail", 625, 577, 4)]


def test_validate_chain():
    ok, why = rails.validate_chain(rails.route(START, {"piece": "straight-rail", "dir": 0,
                                                       "x": 601, "y": 589}))
    assert ok and why is None
    assert rails.validate_chain(rails.route(START, {"piece": "straight-rail", "dir": 4,
                                                    "x": 625, "y": 577}))[0]
    bogus = [{"name": "straight-rail", "x": 601, "y": 601, "direction": 0},
             {"name": "straight-rail", "x": 601, "y": 596, "direction": 0}]   # dy=-5, not -2
    ok, why = rails.validate_chain(bogus)
    assert not ok and "illegal edge" in why, why
    dup = [{"name": "straight-rail", "x": 601, "y": 601, "direction": 0},
           {"name": "curved-rail-a", "x": 601, "y": 598, "direction": 0},
           {"name": "curved-rail-a", "x": 601, "y": 598, "direction": 2}]
    ok, why = rails.validate_chain(dup)
    assert not ok and "share center" in why, why
    ok, why = rails.validate_chain([{"name": "legacy-straight-rail", "x": 0, "y": 0,
                                     "direction": 0}])
    assert not ok and "legacy" in why, why
    assert rails.validate_chain([])[0] is False


def test_self_overlap_regression():
    """The real defect. Upstream returns a 12-piece chain with curved-rail-a TWICE at
    (606,589); strict mode must never hand that to the game."""
    goal = {"piece": "straight-rail", "dir": 4, "x": 611, "y": 591}
    raw = rails.route(START, goal, strict=False)
    centers = [(p["x"], p["y"]) for p in raw]
    assert len(raw) == 12 and centers.count((606, 589)) == 2, tup(raw)
    fixed = rails.route(START, goal)                       # strict=True
    ok, why = rails.validate_chain(fixed)
    assert ok, why
    cs = [(p["x"], p["y"]) for p in fixed]
    assert len(cs) == len(set(cs)), cs
    assert len(fixed) <= len(raw)
    # two more goals that upstream also breaks, repaired the same way
    for g in ({"piece": "half-diagonal-rail", "dir": 4, "x": 613, "y": 593},
              {"piece": "curved-rail-b", "dir": 8, "x": 607, "y": 594}):
        bad = rails.route(START, g, strict=False)
        bc = [(p["x"], p["y"]) for p in bad]
        assert len(bc) != len(set(bc)), "expected upstream to self-overlap on %s" % g
        good = rails.route(START, g)
        assert rails.validate_chain(good)[0]


def test_budget_and_unreachable():
    goal = {"piece": "straight-rail", "dir": 0, "x": 601, "y": 589}
    assert rails.route(START, goal, max_iter=1) is None
    # half-diagonal rails only exist in dirs 0/2/4/6, so this pose is not a graph node
    assert rails.route(START, {"piece": "half-diagonal-rail", "dir": 8, "x": 611, "y": 591},
                       max_iter=3000) is None
    try:
        rails.route(START, goal, heuristic="nope")
        raise AssertionError("bad heuristic name should raise")
    except ValueError:
        pass


def test_admissible_heuristic_agrees():
    goal = {"piece": "straight-rail", "dir": 0, "x": 601, "y": 589}
    assert tup(rails.route(START, goal, heuristic="admissible")) == GOLD_STRAIGHT


def test_bom_and_ghosts():
    straight = rails.route(START, {"piece": "straight-rail", "dir": 0, "x": 601, "y": 589})
    assert rails.bom(straight) == {"rail": 7}
    corner = rails.route(START, {"piece": "straight-rail", "dir": 4, "x": 625, "y": 577})
    n = {}
    for p in corner:
        n[p["name"]] = n.get(p["name"], 0) + 1
    assert n == {"straight-rail": 8, "curved-rail-a": 4, "curved-rail-b": 2}, n
    assert rails.bom(corner) == {"rail": 8 * 1 + 4 * 3 + 2 * 3}
    assert rails.bom(corner) == {"rail": 26}
    assert rails.bom([]) == {}
    ghosts = rails.to_ghosts(corner)
    assert len(ghosts) == len(corner)
    assert set(ghosts[0]) == {"name", "x", "y", "dir"}
    # rail centers ARE integers: to_ghosts must not add tile_width/2
    assert all(isinstance(g["x"], int) and isinstance(g["y"], int) for g in ghosts)
    assert ghosts[0] == {"name": "straight-rail", "x": 601, "y": 601, "dir": 0}
    try:
        rails.bom([{"name": "legacy-curved-rail", "x": 0, "y": 0, "direction": 0}])
        raise AssertionError("legacy rail should not be costed")
    except ValueError:
        pass


# --------------------------------------------------------------------------- engine probe
def _proto_line(name, ptype, tw, th, items):
    return "%s|%s|%d|%d|%s" % (name, ptype, tw, th,
                               ",".join("%s:%d" % kv for kv in items.items()))


GOOD_PROTO = ";".join([
    _proto_line("straight-rail", "straight-rail", 2, 2, {"rail": 1}),
    _proto_line("half-diagonal-rail", "half-diagonal-rail", 2, 2, {"rail": 2}),
    _proto_line("curved-rail-a", "curved-rail-a", 2, 4, {"rail": 3}),
    _proto_line("curved-rail-b", "curved-rail-b", 2, 2, {"rail": 3}),
    _proto_line("legacy-straight-rail", "legacy-straight-rail", 2, 2, {"rail": 1}),
    _proto_line("legacy-curved-rail", "legacy-curved-rail", 4, 8, {"rail": 4}),
]) + "\n"


@_with_rcon([("prototypes.entity[n]", GOOD_PROTO), ("find_entities_filtered", "0\n")])
def test_verify_engine_happy(fake):
    res = rails.verify_against_engine()
    assert res["ok"] is True, res
    assert res["missing"] == [] and res["notes"] == [], res
    assert res["geometry"]["curved-rail-a"] == (2, 4)
    assert res["items"]["curved-rail-b"] == {"rail": 3}
    assert res["adjacency"].startswith("skipped"), res["adjacency"]
    # READ ONLY: no probe may mutate the world
    for cmd in fake.calls:
        for bad in ("create_entity", "destroy", "revive", "entity-ghost", ".insert",
                    "set_", "clear_"):
            assert bad not in cmd, "mutating probe %r in: %s" % (bad, cmd[:160])


@_with_rcon([("prototypes.entity[n]",
              GOOD_PROTO.strip().replace(_proto_line("curved-rail-b", "curved-rail-b", 2, 2,
                                                     {"rail": 3}),
                                         "curved-rail-b|MISSING")),
             ("find_entities_filtered", "0\n")])
def test_verify_engine_missing_piece(fake):
    res = rails.verify_against_engine()
    assert res["ok"] is False
    assert res["missing"] == ["curved-rail-b"], res["missing"]
    assert any("curved-rail-b" in n for n in res["notes"]), res["notes"]


@_with_rcon([("prototypes.entity[n]",
              GOOD_PROTO.strip()
              .replace(_proto_line("curved-rail-a", "curved-rail-a", 2, 4, {"rail": 3}),
                       _proto_line("curved-rail-a", "curved-rail-a", 4, 8, {"rail": 4}))
              .replace(_proto_line("legacy-curved-rail", "legacy-curved-rail", 4, 8,
                                   {"rail": 4}),
                       _proto_line("legacy-curved-rail", "legacy-curved-rail", 2, 2,
                                   {"rail": 1}))),
             ("find_entities_filtered", "0\n")])
def test_verify_engine_geometry_drift(fake):
    res = rails.verify_against_engine()
    assert res["ok"] is False
    assert any("curved-rail-a size 4x8" in n for n in res["notes"]), res["notes"]
    assert any("legacy-curved-rail" in n and "migrated" in n for n in res["notes"]), res["notes"]


@_with_rcon([("prototypes.entity[n]", GOOD_PROTO),
             ("find_entities_filtered", "3\n"),
             ("get_connected_rail",
              "straight-rail|0|601|601;straight-rail|0|601|599;curved-rail-a|0|601|598\n")])
def test_verify_engine_adjacency_spot_check(fake):
    res = rails.verify_against_engine()
    assert res["ok"] is True, res["notes"]
    assert res["adjacency"].startswith("checked 2 connection"), res["adjacency"]
    assert res["adjacency"].endswith("0 disagree"), res["adjacency"]


@_with_rcon([("prototypes.entity[n]", GOOD_PROTO),
             ("find_entities_filtered", "2\n"),
             ("get_connected_rail", "straight-rail|0|601|601;straight-rail|0|601|594\n")])
def test_verify_engine_adjacency_disagrees(fake):
    res = rails.verify_against_engine()
    assert res["ok"] is False
    assert "1 disagree" in res["adjacency"], res["adjacency"]
    assert any("ADJ does not" in n for n in res["notes"]), res["notes"]


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
