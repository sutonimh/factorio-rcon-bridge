#!/usr/bin/env python3
"""Offline unit tests for lane_lint.py — NO live server.

Run with either:
    python3 test_lane_lint.py
    python3 -m pytest test_lane_lint.py

The analysis functions are PURE over the trace dict, so most tests need no RCON at all.
The trace tests serve synthetic component/bbox dumps through test_world_executor's
FakeRcon (the storage._world chunked protocol), extended only to hold a QUEUE of payloads
because trace() issues two chunked reads per call (component, then padded bbox).

Coordinates in the underground/lane fixtures are the real live ones from the module
docstring, so a future 2.1 behaviour change shows up here as a failing assumption.
"""
import json
import re
import traceback

import rcon
import lane_lint
from test_world_executor import FakeRcon


# --------------------------------------------------------------------------- harness
class FakeWorld(FakeRcon):
    """FakeRcon + a queue of chunked payloads. Any non-:sub command pops the next payload
    and answers with its length, exactly like the real length-then-slices protocol."""
    def __init__(self, payloads):
        super().__init__()
        self.queue = [json.dumps(p, separators=(",", ":")) for p in payloads]

    def __call__(self, cmd, timeout=10.0):
        self.calls.append(cmd)
        # lane_lint reads from its OWN storage key (world.scan_area shares storage._world and
        # the autopilot clobbers it mid-read), so match the protocol, not the key name
        m = re.search(r"storage\._\w+:sub\((\d+),(\d+)\)", cmd)
        if m:
            return self.payload[int(m.group(1)) - 1:int(m.group(2))] + "\n"
        if re.search(r"storage\._\w+=nil", cmd):
            return ""                      # read_chunked clears its scratch key in a finally
        assert self.queue, "unexpected RCON call (payloads exhausted): %s" % cmd[:120]
        self.payload = self.queue.pop(0)
        return str(len(self.payload))


def _with_rcon(*payloads):
    """Decorator: install a FakeWorld serving these payloads, always restore rcon.run."""
    def deco(fn):
        def wrapper():
            orig, fake = rcon.run, FakeWorld(payloads)
            rcon.run = fake
            try:
                fn(fake)
            finally:
                rcon.run = orig
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


def belt(u, x, y, d=4, i=(), o=(), lanes=None, items=(), t="transport-belt", **kw):
    """One component node in the lua dump's compact form (x,y are TILE coords here; the
    dump carries entity centres, so +0.5)."""
    r = {"n": kw.pop("n", t if t != "underground-belt" else "underground-belt"),
         "t": t, "d": d, "u": u, "x": x + 0.5, "y": y + 0.5,
         "i": list(i), "o": list(o),
         "L": [{"l": ln, "n": nm, "c": c} for ln, nm, c in (lanes or [])],
         "D": [{"l": ln, "n": nm, "p": p, "u": iu} for ln, nm, p, iu in items]}
    r.update(kw)
    return r


def run_east(n, y=0, x0=0, u0=100, lanes_of=lambda k: ()):
    """n belts flowing east on row y, wired head->tail through belt_neighbours."""
    out = []
    for k in range(n):
        u = u0 + k
        out.append(belt(u, x0 + k, y, 4,
                        i=[u - 1] if k else [], o=[u + 1] if k < n - 1 else [],
                        lanes=lanes_of(k)))
    return out


def comp(nodes, start=None):
    return {"s": start if start is not None else nodes[0]["u"], "N": nodes}


def env(inserters=(), belts=()):
    return {"I": list(inserters), "B": list(belts)}


def ins(u, x, y, drop=None, pick=None, dt=None, pt=None, name="inserter", t="inserter", d=8):
    r = {"n": name, "t": t, "d": d, "u": u, "x": x + 0.5, "y": y + 0.5}
    if drop:
        r["dx"], r["dy"] = drop
        r["dt"] = dt
    if pick:
        r["px"], r["py"] = pick
        r["pt"] = pt
    return r


def tgt(u, x, y, n="transport-belt", t="transport-belt"):
    return {"n": n, "t": t, "x": x + 0.5, "y": y + 0.5, "u": u}


def codes(found):
    return [f["code"] for f in found]


# --------------------------------------------------------------------------- geometry
def test_lane_of_geometry():
    # pins the three live calibration fixtures (module docstring item 9)
    assert lane_lint.left_normal(4) == (0, -1)      # east  -> left is north
    assert lane_lint.left_normal(0) == (-1, 0)      # north -> left is west
    assert lane_lint.left_normal(8) == (1, 0)       # south -> left is east
    assert lane_lint.left_normal(12) == (0, 1)      # west  -> left is south
    # drill @(-42.5,13.5) d=8 drop=(-42.500,15.348) onto belt@(-42.5,15.5) d=4 -> line 1
    assert lane_lint.lane_at(4, -42.5, 15.5, -42.500, 15.348) == 1
    # drill @(14.5,-42.5) d=8 drop=(14.500,-40.652) onto belt@(14.5,-40.5) d=12 -> line 2 (live L2=1)
    assert lane_lint.lane_at(12, 14.5, -40.5, 14.500, -40.652) == 2
    # inserter @(-5.5,4.5) d=8 drop=(-5.500,3.301) onto belt@(-5.5,3.5) d=4 -> line 1 (FAR lane)
    assert lane_lint.lane_at(4, -5.5, 3.5, -5.500, 3.301) == 1
    # rotations of the same physical offset
    assert lane_lint.lane_at(0, 0.5, 0.5, 0.30, 0.5) == 1      # north-running, west side
    assert lane_lint.lane_at(0, 0.5, 0.5, 0.70, 0.5) == 2
    assert lane_lint.lane_at(8, 0.5, 0.5, 0.70, 0.5) == 1      # south-running, east side
    assert lane_lint.lane_at(8, 0.5, 0.5, 0.30, 0.5) == 2


# --------------------------------------------------------------------------- trace
@_with_rcon(comp(run_east(6, y=0, lanes_of=lambda k: [(1, "iron-ore", 2)])), env())
def test_trace_continuous_lane(fake):
    tr = lane_lint.trace(3, 0)                     # start mid-run: the splice must order it
    assert "error" not in tr
    assert [(t["x"], t["y"]) for t in tr["tiles"]] == [(k, 0) for k in range(6)]
    assert len(tr["tiles"]) == 6
    assert tr["lanes"] == {"left": {"iron-ore": 12}, "right": {}}
    assert tr["flags"] == {"dead_start": True, "dead_end": True, "loops": False,
                           "truncated": False}
    assert tr["tiles"][0]["lanes"] == {"1": {"iron-ore": 2}, "2": {}}
    # both /sc gathers are reads only
    assert any("belt_neighbours" in c for c in fake.calls)


@_with_rcon(comp([belt(1, 0, 0, 4, o=[2]),
                  belt(2, 1, 0, 4, i=[1], t="underground-belt", g="input", m=5, h=3),
                  belt(3, 4, 0, 4, o=[4], t="underground-belt", g="output", m=5, h=2),
                  belt(4, 5, 0, 4, i=[3], o=[5, 6]),          # a merge = a real terminator
                  belt(5, 6, 0, 4, i=[4]), belt(6, 5, 1, 0, i=[4])], start=1), env())
def test_trace_underground_hop(fake):
    # 2.1: belt_neighbours omits the partner both ways (input nout=0, output nin=0), so the
    # walk only crosses via the geometric hop. Order must stay contiguous through the gap.
    tr = lane_lint.trace(0, 0)
    assert [(t["x"], t["y"]) for t in tr["tiles"]] == [(0, 0), (1, 0), (4, 0), (5, 0)]
    assert [t["ug"] for t in tr["tiles"]] == [None, "input", "output", None]
    # the walk stops AT the merge rather than guessing through it (control.lua:1318-1331)
    assert tr["flags"]["dead_end"] is False and tr["flags"]["dead_start"] is True
    assert sorted((d["x"], d["y"]) for d in tr["downstream"]) == [(5, 1), (6, 0)]


@_with_rcon(comp([belt(1, 0, 0, 4, o=[2]),
                  belt(2, 1, 0, 4, i=[1], t="underground-belt", g="input", m=5)], start=1),
            env())
def test_trace_underground_unpaired_is_a_dead_end(fake):
    # a partner with the wrong direction or a different tier never pairs -> lua returns no h,
    # and the run must read as a DEAD END rather than silently jumping the gap.
    tr = lane_lint.trace(0, 0)
    assert len(tr["tiles"]) == 2 and tr["flags"]["dead_end"] is True
    assert lane_lint._succ(tr and {"o": [], "g": "input", "h": None}, {}, "o") == []


@_with_rcon({"s": None, "N": None}, env())
def test_no_belt_at_start(fake):
    # lua emits '[]' when nothing is there; _chunked gives back a list, not a dict
    fake.queue = [json.dumps([])]
    tr = lane_lint.trace(999, 999)
    assert tr["error"] and tr["tiles"] == []
    assert lane_lint.lint_lane(tr) == []                # lints clean rather than raising
    assert lane_lint.lint_lane({"error": "x"}) == [] and lane_lint.lint_lane(None) == []


@_with_rcon(comp([belt(1, 0, 0, 4, i=[3], o=[2]),
                  belt(2, 1, 0, 8, i=[1], o=[3]),
                  belt(3, 1, 1, 12, i=[2], o=[1])], start=1), env())
def test_trace_loop_terminates(fake):
    tr = lane_lint.trace(0, 0)
    assert tr["flags"]["loops"] is True
    assert len(tr["tiles"]) == 3                       # walked the ring once, did not spin


@_with_rcon(comp(run_east(4)), env())
def test_contentless_trace_makes_no_content_claims(fake):
    # trace(contents=False) never reads the lanes, so "every lane empty" is unknown, not true
    tr = lane_lint.trace(0, 0, contents=False)
    assert tr["contents"] is False and len(tr["tiles"]) == 4
    assert "get_detailed_contents" not in fake.calls[0]
    assert [f["code"] for f in lane_lint.lint_lane(tr)] == ["DEAD_END"]


@_with_rcon(comp(run_east(12)), env())
def test_trace_truncation(fake):
    tr = lane_lint.trace(0, 0, limit=4)
    assert tr["flags"]["truncated"] is True and tr["tiles"]


# --------------------------------------------------------------------------- lint rules
@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 4)] if k < 2
                          else [(1, "iron-ore", 2), (1, "copper-ore", 2)])), env())
def test_mixed_ore_detected(fake):
    tr = lane_lint.trace(0, 0)
    found = [f for f in lane_lint.lint_lane(tr, expect="iron-ore") if f["code"] == "MIXED_ITEMS"]
    assert len(found) == 1
    assert (found[0]["x"], found[0]["y"]) == (2, 0)     # first tile carrying the second name
    assert found[0]["evidence"]["foreign"] == ["copper-ore"] and found[0]["sev"] == 1


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 4), (2, "coal", 4)])), env())
def test_ore_left_coal_right_is_clean(fake):
    # GOTCHAS:616 - ore on one lane, coal on the other is the CORRECT two-lane design
    tr = lane_lint.trace(0, 0)
    assert tr["lanes"] == {"left": {"iron-ore": 16}, "right": {"coal": 16}}
    assert [f for f in lane_lint.lint_lane(tr, expect="iron-ore")
            if f["code"] == "MIXED_ITEMS"] == []


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 2)])),
            env(belts=[{"n": "transport-belt", "t": "transport-belt", "d": 12,
                        "u": 900, "x": 5.5, "y": 1.5},
                       {"n": "transport-belt", "t": "transport-belt", "d": 4,
                        "u": 901, "x": 8.5, "y": 1.5}]))
def test_direction_split(fake):
    # GOTCHAS:831 - the iron row, west half feeding the lane and east half running to a dead
    # end, i.e. the two halves flowing APART. They are ORPHANS to the traced run (a split
    # breaks the component), which is why the rule must count orphans.
    tr = lane_lint.trace(0, 0)
    assert len(tr["orphans"]) == 2
    found = [f for f in lane_lint.lint_lane(tr) if f["code"] == "DIRECTION_SPLIT"]
    assert len(found) == 1 and found[0]["evidence"]["y"] == 1
    assert found[0]["evidence"]["split_x"] == 6.5


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 2)])),
            env(belts=[{"n": "transport-belt", "t": "transport-belt", "d": 12,
                        "u": 900, "x": 2.5, "y": 1.5},
                       {"n": "transport-belt", "t": "transport-belt", "d": 4,
                        "u": 901, "x": 9.5, "y": 1.5}]))
def test_two_distant_opposed_lines_are_not_a_split(fake):
    # opposed directions 7 tiles apart on one row = two independent lines, not a torn one
    assert [f for f in lane_lint.lint_lane(lane_lint.trace(0, 0))
            if f["code"] == "DIRECTION_SPLIT"] == []


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 2)])),
            env(belts=[{"n": "transport-belt", "t": "transport-belt", "d": 4,
                        "u": 900, "x": 5.5, "y": 1.5},
                       {"n": "transport-belt", "t": "transport-belt", "d": 12,
                        "u": 901, "x": 6.5, "y": 1.5}]))
def test_converging_merge_is_not_a_split(fake):
    # the same two directions the other way round is a legitimate two-sided merge - live on
    # this map at column x=-8 (d=8 at y=16 and d=0 at y=18 both feed the junction at y=17)
    assert [f for f in lane_lint.lint_lane(lane_lint.trace(0, 0))
            if f["code"] == "DIRECTION_SPLIT"] == []


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 2)])), env())
def test_dead_end_detected(fake):
    tr = lane_lint.trace(0, 0)
    found = [f for f in lane_lint.lint_lane(tr) if f["code"] == "DEAD_END"]
    assert len(found) == 1 and (found[0]["x"], found[0]["y"]) == (3, 0)


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 2)])),
            env(inserters=[ins(50, 3, 1, pick=(3.5, 0.5), pt=tgt(103, 3, 0),
                               drop=(3.5, 2.5), dt=tgt(60, 3, 2, "stone-furnace", "furnace"))]))
def test_dead_end_with_consumer_is_clean(fake):
    # GOTCHAS:806 - a terminus WITH a consumer is a legitimate end, not a fault
    tr = lane_lint.trace(0, 0)
    assert len(tr["tappers"]) == 1
    assert [f for f in lane_lint.lint_lane(tr) if f["code"] == "DEAD_END"] == []


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 2)])),
            env(belts=[{"n": "transport-belt", "t": "transport-belt", "d": 4,
                        "u": 900, "x": 4.5, "y": 1.5}]))
def test_dead_end_carries_the_adjacent_row_orphan(fake):
    # "the lane's two segments sat on adjacent rows" - the orphan run one row off IS the
    # evidence a caller needs to see the lane continues but never joined
    tr = lane_lint.trace(0, 0)
    found = [f for f in lane_lint.lint_lane(tr) if f["code"] == "DEAD_END"][0]
    assert found["evidence"]["orphans_near_tail"] == [{"x": 4, "y": 1, "d": 4,
                                                       "name": "transport-belt"}]


@_with_rcon(comp(run_east(4)), env())
def test_starved(fake):
    tr = lane_lint.trace(0, 0)
    found = [f for f in lane_lint.lint_lane(tr) if f["code"] == "STARVED"]
    assert len(found) == 1 and found[0]["sev"] == 2


@_with_rcon(comp(run_east(4)),
            env(inserters=[ins(51, 0, 1, drop=(0.5, 0.30), dt=tgt(100, 0, 0),
                               name="burner-mining-drill", t="mining-drill")]))
def test_starved_clean_with_a_feeder(fake):
    tr = lane_lint.trace(0, 0)
    assert tr["feeders"] and tr["feeders"][0]["lane"] == 1
    assert [f for f in lane_lint.lint_lane(tr) if f["code"] == "STARVED"] == []


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 2)])),
            env(inserters=[ins(52, 1, 1, pick=(1.5, 0.5), pt=tgt(101, 1, 0),
                               drop=(1.5, 2.5),
                               dt=tgt(70, 1, 2, "wooden-chest", "container"))]))
def test_drain_finding(fake):
    # GOTCHAS:459-473 - the terminal chest+inserter left on a THROUGH lane
    tr = lane_lint.trace(0, 0)
    found = [f for f in lane_lint.lint_lane(tr) if f["code"] == "DRAIN"]
    assert len(found) == 1 and (found[0]["x"], found[0]["y"]) == (1, 0)
    assert found[0]["evidence"]["to"]["name"] == "wooden-chest"


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 2)])),
            env(inserters=[ins(52, 1, 1, pick=(1.5, 0.5), pt=tgt(101, 1, 0),
                               drop=(1.5, 2.5),
                               dt=tgt(71, 1, 2, "stone-furnace", "furnace"))]))
def test_drain_into_a_furnace_is_clean(fake):
    assert [f for f in lane_lint.lint_lane(lane_lint.trace(0, 0))
            if f["code"] == "DRAIN"] == []


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 2)])),
            env(inserters=[ins(52, 3, 1, pick=(3.5, 0.5), pt=tgt(103, 3, 0),
                               drop=(3.5, 2.5),
                               dt=tgt(70, 3, 2, "wooden-chest", "container"))]))
def test_drain_on_the_last_tile_is_clean(fake):
    # unloading at the END of a lane is the legitimate terminal chest
    assert [f for f in lane_lint.lint_lane(lane_lint.trace(0, 0))
            if f["code"] == "DRAIN"] == []


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 2)])),
            env(inserters=[ins(52, 1, 1, pick=(1.5, 0.5), pt=tgt(101, 1, 0), drop=(1.5, 2.5),
                               dt=tgt(70, 1, 2, "wooden-chest", "container")),
                           ins(53, 2, 2, pick=(1.5, 2.5), pt=tgt(70, 1, 2), drop=(2.5, 3.5),
                               dt=tgt(72, 2, 3, "stone-furnace", "furnace"))]))
def test_drain_clean_when_the_chest_has_a_puller(fake):
    # a chest something actually empties is a legitimate buffer, not a dead end
    assert [f for f in lane_lint.lint_lane(lane_lint.trace(0, 0))
            if f["code"] == "DRAIN"] == []


@_with_rcon(comp(run_east(4)),
            env(inserters=[ins(60, 0, 1, drop=(0.5, 0.30), dt=tgt(100, 0, 0)),
                           ins(61, 2, 1, drop=(2.5, 0.30), dt=tgt(102, 2, 0))]))
def test_sideload_contention(fake):
    tr = lane_lint.trace(0, 0)
    assert [f["lane"] for f in tr["feeders"]] == [1, 1]
    found = [f for f in lane_lint.lint_lane(tr) if f["code"] == "SIDELOAD_CONTENTION"]
    assert len(found) == 1 and found[0]["sev"] == 2 and found[0]["evidence"]["lane"] == 1


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "coal", 4)])),
            env(inserters=[ins(60 + k, k, 1, drop=(k + 0.5, 0.30), dt=tgt(100 + k, k, 0),
                               name="electric-mining-drill", t="mining-drill")
                           for k in range(4)]))
def test_a_drill_row_is_not_contention(fake):
    # GOTCHAS 406-418: a row of drills all dropping onto ONE lane is the intended mine
    # layout, not a merge. Verified live: the coal drop row at (-43..-38,15) tripped both
    # arms before this exemption.
    tr = lane_lint.trace(0, 0)
    assert len(tr["feeders"]) == 4 and {f["lane"] for f in tr["feeders"]} == {1}
    assert [f for f in lane_lint.lint_lane(tr) if f["code"] == "SIDELOAD_CONTENTION"] == []


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "coal", 4)])),
            env(inserters=[ins(60 + k, k, 1, drop=(k + 0.5, 0.30), dt=tgt(100 + k, k, 0),
                               name="electric-mining-drill", t="mining-drill")
                           for k in range(4)] +
                [ins(70, 2, -1, drop=(2.5, 0.30), dt=tgt(102, 2, 0))]))
def test_a_drill_row_plus_a_real_merge_fires(fake):
    # drills collapse to one source, so the inserter merging onto the same lane still trips
    # both arms: 2 sources on lane 1 (perLane), and lane 1 upstream already carries coal
    found = [f for f in lane_lint.lint_lane(lane_lint.trace(0, 0))
             if f["code"] == "SIDELOAD_CONTENTION"]
    assert len(found) == 2 and {f["evidence"]["lane"] for f in found} == {1}
    assert any("FULL of coal from upstream" in f["detail"] for f in found)


@_with_rcon(comp(run_east(4)),
            env(inserters=[ins(60, 0, 1, drop=(0.5, 0.30), dt=tgt(100, 0, 0)),
                           ins(61, 2, -1, drop=(2.5, 0.70), dt=tgt(102, 2, 0))]))
def test_sideload_one_per_lane_is_clean(fake):
    tr = lane_lint.trace(0, 0)
    assert sorted(f["lane"] for f in tr["feeders"]) == [1, 2]
    assert [f for f in lane_lint.lint_lane(tr) if f["code"] == "SIDELOAD_CONTENTION"] == []


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 2)] if k < 2 else ())),
            env(belts=[{"n": "transport-belt", "t": "transport-belt", "d": 0,
                        "u": 900, "x": 2.5, "y": 1.5}]))
def test_sideload_into_an_occupied_lane(fake):
    # lanes.ts:133-156 - the belt at (2,1) runs NORTH into run tile (2,0); the tile upstream
    # of the junction already carries iron on that lane, so the merge will block.
    tr = lane_lint.trace(0, 0)
    assert tr["sideloads"] == [{"from": [2, 1], "into": [2, 0], "lane": 2, "src_d": 0}]
    found = [f for f in lane_lint.lint_lane(tr) if f["code"] == "SIDELOAD_CONTENTION"]
    assert len(found) == 0                              # lane 2 upstream is empty -> silent


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(2, "iron-ore", 4)] if k < 3 else ())),
            env(belts=[{"n": "transport-belt", "t": "transport-belt", "d": 0,
                        "u": 900, "x": 2.5, "y": 1.5}]))
def test_sideload_into_an_occupied_lane_fires(fake):
    # the junction tile's lane 2 is FULL (4) and the flow filling it comes from upstream, so
    # the merging belt genuinely cannot insert
    tr = lane_lint.trace(0, 0)
    assert tr["sideloads"][0]["lane"] == 2
    found = [f for f in lane_lint.lint_lane(tr) if f["code"] == "SIDELOAD_CONTENTION"]
    assert len(found) == 1 and found[0]["evidence"]["upstream"] == {"iron-ore": 4}


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(2, "iron-ore", 2)] if k < 3 else ())),
            env(belts=[{"n": "transport-belt", "t": "transport-belt", "d": 0,
                        "u": 900, "x": 2.5, "y": 1.5}]))
def test_sideload_into_a_loaded_but_unsaturated_lane_is_clean(fake):
    # SATURATION, not occupancy: a lane with room still accepts the merge. Occupancy alone
    # produced 15 findings on one live plate row (y=3) where only the full tiles matter.
    tr = lane_lint.trace(0, 0)
    assert tr["sideloads"][0]["lane"] == 2
    assert [f for f in lane_lint.lint_lane(tr) if f["code"] == "SIDELOAD_CONTENTION"] == []


@_with_rcon(comp([belt(1, 0, 0, 4, o=[2]), belt(2, 1, 0, 8, i=[1], o=[3]),
                  belt(3, 1, 1, 8, i=[2])], start=1), env())
def test_a_corner_inside_the_run_is_not_a_sideload(fake):
    tr = lane_lint.trace(0, 0)
    assert tr["sideloads"] == []


@_with_rcon(comp([belt(1, -2, 1, 4, o=[2]), belt(2, -1, 1, 4, i=[1], o=[3]),
                  belt(3, 0, 1, 8, i=[2], o=[4]), belt(4, 0, 2, 8, i=[3])], start=1),
            env(belts=[{"n": "transport-belt", "t": "transport-belt", "d": 0,
                        "u": 900, "x": 0.5, "y": 0.5}]))
def test_a_turned_leg_is_not_a_direction_split(fake):
    # LIVE false positive, column x=-8: (-8,10) runs north and (-8,11) runs south one tile
    # apart, each fed by its OWN underground output from the west - two deliberate opposing
    # lines. It was the ONLY finding on a healthy 70-tile run. Here the run turns south at
    # (0,1) and the orphan at (0,0) runs north: the closest diverging pair is a turn head,
    # so the column is two legs, not a tear.
    tr = lane_lint.trace(-2, 1)
    assert [(t["x"], t["y"]) for t in tr["tiles"]] == [(-2, 1), (-1, 1), (0, 1), (0, 2)]
    assert tr["orphans"] == [{"x": 0, "y": 0, "d": 0, "name": "transport-belt"}]
    assert [f for f in lane_lint.lint_lane(tr) if f["code"] == "DIRECTION_SPLIT"] == []


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 2)])),
            env(belts=[{"n": "transport-belt", "t": "transport-belt", "d": 12,
                        "u": 900, "x": 5.5, "y": 1.5},
                       {"n": "transport-belt", "t": "transport-belt", "d": 4,
                        "u": 901, "x": 6.5, "y": 1.5},
                       {"n": "transport-belt", "t": "transport-belt", "d": 4,
                        "u": 902, "x": 9.5, "y": 1.5, "p": 1}]))
def test_a_leg_head_elsewhere_on_the_axis_does_not_mask_a_real_tear(fake):
    # the exemption is scoped to the PAIR the rule names, never to the whole row: a side-fed
    # leg head at (9,1) must not buy silence for the genuine tear at (5,1)/(6,1), which is
    # GOTCHAS:831's shape (both halves fed along the row, neither of them a leg head).
    tr = lane_lint.trace(0, 0)
    assert tr["turns"] == [[9, 1]]
    found = [f for f in lane_lint.lint_lane(tr) if f["code"] == "DIRECTION_SPLIT"]
    assert len(found) == 1 and found[0]["evidence"]["belts"] == [[6, 1, 4], [5, 1, 12]]


@_with_rcon(comp(run_east(4, lanes_of=lambda k: [(1, "iron-ore", 2)])),
            env(belts=[{"n": "transport-belt", "t": "transport-belt", "d": 12,
                        "u": 900, "x": 5.5, "y": 1.5, "p": 1},
                       {"n": "transport-belt", "t": "transport-belt", "d": 4,
                        "u": 901, "x": 6.5, "y": 1.5, "p": 1}]))
def test_two_orphan_leg_heads_are_not_a_split(fake):
    # the LIVE case, with both belts as ORPHANS: column x=-8 traced from (-8,19) puts (-8,10)
    # and (-8,11) off the run, so the engine's perpendicular-input flag is the only source of
    # the turn - a tile-order fallback alone reported the tear again from a different start.
    tr = lane_lint.trace(0, 0)
    assert sorted(tr["turns"]) == [[5, 1], [6, 1]]
    assert [f for f in lane_lint.lint_lane(tr) if f["code"] == "DIRECTION_SPLIT"] == []


def test_what_resolves_targets_by_bounding_box():
    """radius is measured to the entity CENTRE, so the ported radius=0.4 resolved 1x1 targets
    only: live, every inserter facing a 2x2 stone-furnace resolved to nil and all 12 real
    consumers on the (-8,17) run reported `to: null`. The lua must test bbox CONTAINMENT
    (control.lua:1379-1382 inside()) and must exclude the querying entity (:1386), which a
    wider sweep now reaches."""
    lua = lane_lint._lua_bbox(-5, -5, 5, 5)
    assert "left_top" in lua and "right_bottom" in lua, "no bbox-containment test"
    assert "e.unit_number~=me" in lua, "what() must not resolve to the querying entity"
    assert "radius=0.4" not in lua, "centre-distance 0.4 cannot see a 2x2 target"
    assert "what(p,r.u)" in lua, "the owning uid must be passed through"


# --------------------------------------------------------------------------- verify_supply
def _moving_payloads(second_items):
    """trace (component + bbox), then ONE small tail sample — not a second full trace."""
    run = run_east(4, lanes_of=lambda k: [(1, "iron-ore", 2)])
    run[-1]["D"] = [{"l": 1, "n": "iron-ore", "p": 0.03125, "u": 500},
                    {"l": 1, "n": "iron-ore", "p": 0.28125, "u": 501}]
    tail = {"T": [{"x": k, "y": 0, "L": [{"l": 1, "n": "iron-ore", "c": 2}],
                   "D": second_items if k == 3 else []} for k in range(4)]}
    return [comp(run), env(), tail]


def test_verify_supply_connected_not_moving():
    # tonight's headline bug: the lane IS connected and IS full, and nothing moves.
    same = [{"l": 1, "n": "iron-ore", "p": 0.03125, "u": 500},
            {"l": 1, "n": "iron-ore", "p": 0.28125, "u": 501}]
    orig, rcon.run = rcon.run, FakeWorld(_moving_payloads(same))
    try:
        r = lane_lint.verify_supply("iron-ore", (0, 0), (3, 0), settle=0)
    finally:
        rcon.run = orig
    assert r["connected"] is True and r["moving"] is False
    assert r["path_len"] == 4 and r["arrived"] == 2
    assert "DEAD_END" in codes(r["findings"])           # the REASON, not just a bool


def test_verify_supply_moving():
    moved = [{"l": 1, "n": "iron-ore", "p": 0.15625, "u": 500},   # same uid, advanced
             {"l": 1, "n": "iron-ore", "p": 0.40625, "u": 501}]
    orig, rcon.run = rcon.run, FakeWorld(_moving_payloads(moved))
    try:
        r = lane_lint.verify_supply("iron-ore", (0, 0), (3, 0), settle=0)
    finally:
        rcon.run = orig
    assert r["connected"] is True and r["moving"] is True


def test_verify_supply_not_connected():
    orig, rcon.run = rcon.run, FakeWorld(_moving_payloads([]))
    try:
        r = lane_lint.verify_supply("iron-ore", (0, 0), (40, 40), settle=0)
    finally:
        rcon.run = orig
    assert r["connected"] is False and r["moving"] is True   # id set changed -> flow, wrong place


def test_verify_supply_no_belt():
    orig, rcon.run = rcon.run, FakeWorld([[]])
    try:
        r = lane_lint.verify_supply("iron-ore", (5, 5), (9, 9), settle=0)
    finally:
        rcon.run = orig
    assert r == {"connected": False, "moving": False, "arrived": 0, "findings": [],
                 "path_len": 0, "trace": r["trace"]}


# --------------------------------------------------------------------------- safety guard
def test_reads_are_read_only():
    """This module runs against a LIVE server with the operator in it. Every /sc string it
    can emit must be incapable of mutating the world or registering an event handler."""
    lua = [lane_lint._lua_component(0, 0, 400, True),
           lane_lint._lua_component(0, 0, 400, False),
           lane_lint._lua_bbox(-10, -10, 10, 10),
           # _lua_tail is emitted by every verify_supply and was the one gatherer this guard
           # never covered - an unguarded lua emitter is exactly how a mutating verb gets in
           lane_lint._lua_tail([{"x": -3, "y": 4}, {"x": 5, "y": -6}]),
           "rcon.print(%s:sub(1,3000))" % lane_lint.STORE]
    bad = r"create_entity|destroy|remove_item|\.direction\s*=[^=]|\brotate\b|walking_state" \
          r"|on_nth_tick|script\.on_event|\.insert\s*\{|clear_items|order_deconstruction" \
          r"|set_recipe|teleport|\.amount\s*=[^=]|researched\s*=|add_research"
    for s in lua:
        m = re.search(bad, s)
        assert not m, "MUTATING lua emitted: %r in %s" % (m.group(0), s[:120])
        assert "rcon.print" in s
    # the ONLY world state written is the private read buffer, and it only ever gets a string
    for s in lua[:4]:
        writes = set(re.findall(r"(storage\.[\w.]+)\s*=[^=]", s))
        assert writes <= {lane_lint.STORE}, "wrote outside the private buffer: %s" % writes
    assert lane_lint.STORE != "storage._world", "must not share world.py's clobbered key"
    # silent-command only: no /c (which echoes), no other console prefix
    src = open(lane_lint.__file__).read()
    assert lane_lint._SC == "/sc "
    for lit in ('"/c ', "'/c ", "'/sc"):
        assert lit not in src, "unexpected console command literal: %s" % lit
    assert src.count('"/sc ') == 1, "the /sc prefix must exist once, as the _SC constant"


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
