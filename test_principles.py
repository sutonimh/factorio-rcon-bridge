#!/usr/bin/env python3
"""Offline unit tests for principles.py — NO live server.

Run with either:
    python3 test_principles.py          (pytest is NOT installed on this box)
    python3 -m pytest test_principles.py

Every invariant gets a PASSING world and a FAILING world built from synthetic entity
dicts, so the rule set is exercised without RCON. `probe()` is tested against a scripted
FakeRcon that speaks the chunked storage._principles read protocol (mirrors the harness
in test_world_executor.py). The last block re-checks the real before/after snapshots as
golden regression tests — the operator's base must score better than the bot's on every
headline metric.
"""
import json
import pathlib
import re
import traceback

import principles as P


# --------------------------------------------------------------------------- harness
class FakeRcon:
    """Scripted rcon.run for principles.probe(): serves the length reply, then the
    chunked storage._principles:sub(i,j) slices, then swallows the cleanup write."""

    def __init__(self, payload_obj):
        self.payload = json.dumps(payload_obj, separators=(",", ":"))
        self.calls = []

    def __call__(self, cmd, timeout=10.0):
        self.calls.append(cmd)
        m = re.search(r"storage\._principles:sub\((\d+),(\d+)\)", cmd)
        if m:
            i, j = int(m.group(1)), int(m.group(2))
            return self.payload[i - 1:j] + "\n"
        if "storage._principles=nil" in cmd:
            return ""
        return "%d\n" % len(self.payload)          # the initial PROBE_LUA call


def ent(n, x, y, t=None, **kw):
    """Compact entity literal. Type defaults to a sensible mapping from the name."""
    guess = {"transport-belt": "transport-belt", "underground-belt": "underground-belt",
             "splitter": "splitter", "small-electric-pole": "electric-pole",
             "electric-mining-drill": "mining-drill", "burner-mining-drill": "mining-drill",
             "stone-furnace": "furnace", "inserter": "inserter",
             "burner-inserter": "burner-inserter", "wooden-chest": "container",
             "iron-chest": "container", "steam-engine": "generator", "boiler": "boiler",
             "offshore-pump": "offshore-pump", "lab": "lab",
             "assembling-machine-1": "assembling-machine", "pipe": "pipe"}
    e = {"n": n, "t": t or guess.get(n, n), "x": x, "y": y}
    e.update(kw)
    return e


def belt_run(x0, y, n, d=4):
    """n belts heading east from (x0,y)."""
    return [ent("transport-belt", x0 + i, y, d=d) for i in range(n)]


def working_mine(lane_y=-0.5, n_belts=4):
    """A minimal end-to-end flow: drill -> lane -> inserter -> furnace.

    Drill at (0.5, lane_y-2) facing S drops at lane_y-2 + 1.85 -> the lane row.
    Inserter direction points at its PICKUP side, so an inserter SOUTH of the lane
    facing N picks off the lane and drops into the furnace below it.
    """
    ents = [ent("electric-mining-drill", 0.5, lane_y - 2, d=8, e=1)]
    ents += belt_run(0.5, lane_y, n_belts, d=4)
    last_x = 0.5 + n_belts - 1
    ents += [ent("inserter", last_x, lane_y + 1, d=0, e=1),
             ent("stone-furnace", last_x, lane_y + 2.5)]
    return ents


def W(ents, **meta):
    return P.World(ents, meta)


def _names(findings):
    return {f["check"] for f in findings}


# --------------------------------------------------------------------------- world model
def test_world_normalizes_footprints():
    w = W([ent("electric-mining-drill", 0.5, 0.5, d=8),
           ent("transport-belt", 5.5, 5.5, d=4)])
    drill = w.drills[0]
    assert drill["bb"] == [-1, -1, 1, 1], drill["bb"]
    assert len(drill["tiles"]) == 9
    assert w.belts[0]["tiles"] == [(5, 5)]


def test_inserter_direction_convention():
    """Regression guard for the single most inversion-prone fact in the whole module:
    an inserter's direction points at its PICKUP side (measured live 2026-08-29)."""
    w = W([ent("inserter", 3.5, 4.5, d=8)])
    i = w.inserters[0]
    assert w.pickup_tile(i) == (3, 5), w.pickup_tile(i)
    assert w.drop_tile(i) == (3, 3), w.drop_tile(i)


def test_drill_drop_offset_matches_game():
    """Live: drill (14.5,-42.5) dir 8 -> drop_position y -40.652 -> tile -41."""
    w = W([ent("electric-mining-drill", 14.5, -42.5, d=8)])
    assert w.drop_tile(w.drills[0]) == (14, -41)


def test_belt_graph_and_underground_pairing():
    ents = belt_run(0.5, 0.5, 2, d=4)
    ents += [ent("underground-belt", 2.5, 0.5, d=4, bg="input"),
             ent("underground-belt", 5.5, 0.5, d=4, bg="output"),
             ent("transport-belt", 6.5, 0.5, d=4)]
    w = W(ents)
    g = w.belt_graph()
    assert (3, 0) not in g.get((2, 0), set())      # jumps the gap, not into it
    assert (5, 0) in g[(2, 0)]
    assert (6, 0) in g[(5, 0)]


def test_splitter_feeds_both_outputs():
    ents = [ent("splitter", 2.0, 0.5, d=0),        # 2 wide, faces north
            ent("transport-belt", 1.5, -0.5, d=0),
            ent("transport-belt", 2.5, -0.5, d=0)]
    w = W(ents)
    g = w.belt_graph()
    outs = set()
    for t in w.belts[0]["tiles"]:
        outs |= g.get(t, set())
    assert outs == {(1, -1), (2, -1)}, outs


# --------------------------------------------------------------------------- P1 flow
def test_no_belt_without_consumer_passes_on_working_mine():
    w = W(working_mine())
    assert P.no_belt_without_consumer(w) == []
    assert P.metrics(w)["flow_coverage"] == 1.0


def test_no_belt_without_consumer_flags_orphan_lane():
    ents = working_mine() + belt_run(40.5, 40.5, 12, d=4)   # a lane feeding nothing
    w = W(ents)
    fs = P.no_belt_without_consumer(w)
    assert fs and fs[0]["severity"] == "error"
    assert "flow coverage" in fs[0]["msg"]
    assert P.metrics(w)["flow_coverage"] < 0.6


def test_production_is_moving():
    w = W(working_mine(), production={"iron-plate": 0, "coal": 120})
    fs = P.production_is_moving(w)
    assert len(fs) == 1 and "iron-plate" in fs[0]["msg"]
    assert P.production_is_moving(W([], production={"iron-plate": 5})) == []


def test_dead_belt_fraction_budget_is_not_zero():
    """P14: tolerate ~5% residue. One dead lead-in among many live belts must PASS."""
    ents = working_mine(n_belts=30) + [ent("transport-belt", 90.5, 90.5, d=4)]
    assert P.dead_belt_fraction_ok(W(ents)) == []


# --------------------------------------------------------------------------- P2 power
def test_grid_is_single_network():
    good = W([ent("small-electric-pole", 0.5, 0.5, e=1),
              ent("small-electric-pole", 7.5, 0.5, e=1)])
    assert P.grid_is_single_network(good) == []
    bad = W([ent("small-electric-pole", 0.5, 0.5, e=1),
             ent("small-electric-pole", 7.5, 0.5, e=1),
             ent("electric-mining-drill", 40.5, 40.5, d=8, e=405),
             ent("small-electric-pole", 42.5, 40.5, e=405)])
    fs = P.grid_is_single_network(bad)
    assert len(fs) == 1 and "island network 405" in fs[0]["msg"]
    assert "0 generators" in fs[0]["msg"]


def test_no_power_status_is_an_error():
    w = W([ent("lab", 0.5, 0.5, e=9, s="no_power")])
    fs = P.grid_is_single_network(w)
    assert any("no_power" in f["msg"] for f in fs)


def test_pole_degree_headroom():
    assert P.pole_degree_headroom(W([ent("small-electric-pole", 0.5, 0.5, deg=4)])) == []
    fs = P.pole_degree_headroom(W([ent("small-electric-pole", 0.5, 0.5, deg=5)]))
    assert len(fs) == 1 and "degree 5" in fs[0]["msg"]
    # unknown degree (snapshot mode) must NOT be guessed at
    assert P.pole_degree_headroom(W([ent("small-electric-pole", 0.5, 0.5)])) == []


def test_wire_reach_respected_flags_isolated_and_stacked_poles():
    fs = P.wire_reach_respected(W([ent("small-electric-pole", 0.5, 0.5),
                                   ent("small-electric-pole", 60.5, 60.5)]))
    assert len([f for f in fs if "isolated" in f["msg"]]) == 2
    fs = P.wire_reach_respected(W([ent("small-electric-pole", 0.5, 0.5),
                                   ent("small-electric-pole", 1.5, 0.5)]))
    assert any("1.00 tiles apart" in f["msg"] for f in fs)


# --------------------------------------------------------------------------- P3/P5 geometry
def test_every_drill_drops_on_lane():
    assert P.every_drill_drops_on_lane(W(working_mine())) == []
    fs = P.every_drill_drops_on_lane(W([ent("electric-mining-drill", 0.5, -2.5, d=8)]))
    assert len(fs) == 1 and "bare ground" in fs[0]["msg"]


def test_drill_pitch_ok_catches_the_pitch_2_bug():
    """The exact live bug: 3x3 electric drills stepped at the 2x2 burner's pitch."""
    row = [ent("electric-mining-drill", 14.5 + 2 * i, -42.5, d=8) for i in range(3)]
    fs = P.drill_pitch_ok(W(row))
    assert len(fs) == 2 and all(f["pitch"] == 2.0 and f["width"] == 3 for f in fs)
    ok = [ent("electric-mining-drill", 14.5 + 3 * i, -42.5, d=8) for i in range(3)]
    assert P.drill_pitch_ok(W(ok)) == []


def test_no_entity_overlap_reports_buried_belt():
    w = W([ent("electric-mining-drill", 14.5, -42.5, d=8),
           ent("transport-belt", 14.5, -41.5, d=4)])
    fs = P.no_entity_overlap(w)
    assert len(fs) == 1 and "buried" in fs[0]["msg"]


def test_mine_row_geometry_ok():
    assert P.mine_row_geometry_ok(W(working_mine())) == []
    off = [ent("electric-mining-drill", 0.5, -3.5, d=8), ent("transport-belt", 0.5, -1.5, d=4)]
    assert P.mine_row_geometry_ok(W(off)) == []          # drop lands on the lane: fine
    strayed = [ent("electric-mining-drill", 0.5, -0.5, d=8, dp=[0.5, 3.4]),
               ent("transport-belt", 0.5, 3.5, d=4)]
    fs = P.mine_row_geometry_ok(W(strayed))
    assert len(fs) == 1 and "off its lane row" in fs[0]["msg"]


# --------------------------------------------------------------------------- P4/P8 poles
def test_no_pole_on_lane():
    ents = working_mine() + [ent("small-electric-pole", 1.5, -0.5)]
    fs = P.no_pole_on_lane(W(ents))
    assert len(fs) == 1 and "occupies belt tile" in fs[0]["msg"]
    ok = working_mine() + [ent("small-electric-pole", 1.5, -4.5)]
    assert P.no_pole_on_lane(W(ok)) == []


def test_no_pole_on_drill_drop_tile():
    ents = [ent("electric-mining-drill", 0.5, -2.5, d=8),
            ent("small-electric-pole", 0.5, -0.5)]
    fs = P.no_pole_on_lane(W(ents))
    assert any("drop tile" in f["msg"] for f in fs)


def test_poles_cover_machines_flags_the_zero_coverage_scatter():
    ents = [ent("small-electric-pole", 0.5, 0.5, e=1), ent("inserter", 0.5, 1.5, d=0, e=1),
            ent("small-electric-pole", 60.5, 60.5, e=1)]
    fs = P.poles_cover_machines(W(ents))
    assert len(fs) == 1 and "powers nothing" in fs[0]["msg"]
    assert fs[0]["pos"] == [60.5, 60.5]


def test_cut_vertex_pole_is_not_called_redundant():
    """P4: a pole that looks redundant for COVERAGE can be load-bearing for CONNECTIVITY —
    the operator's service poles ARE the network inside a block."""
    ents = [ent("inserter", 0.5, 0.5, d=0, e=1),
            ent("small-electric-pole", 0.5, 1.5, e=1),     # covers the inserter
            ent("small-electric-pole", 1.5, 1.5, e=1),     # also covers it, but bridges...
            ent("small-electric-pole", 8.5, 1.5, e=1)]     # ...to this far pole
    fs = P.poles_cover_machines(W(ents))
    assert not any("fully redundant" in f["msg"] and f["pos"] == [1.5, 1.5] for f in fs)


def test_trunk_pitch_is_one_sided():
    """Shorter hops are always safe; only EXCEEDING the pitch risks a dead network."""
    good = W([ent("small-electric-pole", 0.5, 0.5 + 7 * i) for i in range(4)])
    assert P.trunk_pitch_ok(good) == []
    short = W([ent("small-electric-pole", 0.5, y) for y in (0.5, 6.5, 12.5, 18.5)])
    assert P.trunk_pitch_ok(short) == []
    over = W([ent("small-electric-pole", 0.5, y) for y in (0.5, 8.5, 16.5, 24.5)])
    fs = P.trunk_pitch_ok(over)
    assert len(fs) == 1 and "exceed pitch" in fs[0]["msg"]


# --------------------------------------------------------------------------- P6/P7/P9 lanes
def test_one_lane_per_item_per_destination():
    single = W(belt_run(0.5, 0.5, 20, d=4))
    assert P.one_lane_per_item_per_destination(single) == []
    doubled = W(belt_run(0.5, 0.5, 20, d=4) + belt_run(0.5, 1.5, 20, d=4))
    fs = P.one_lane_per_item_per_destination(doubled)
    assert len(fs) == 1 and fs[0]["offset"] == 1 and fs[0]["overlap"] == 20


def test_lane_shared_from_both_sides():
    one_side = [ent("electric-mining-drill", 0.5 + 3 * i, -2.5, d=8) for i in range(3)]
    one_side += belt_run(0.5, -0.5, 8, d=4)
    fs = P.lane_shared_from_both_sides(W(one_side))
    assert len(fs) == 1 and "ONE side only" in fs[0]["msg"]
    both = one_side + [ent("electric-mining-drill", 0.5 + 3 * i, 1.5, d=0) for i in range(3)]
    assert P.lane_shared_from_both_sides(W(both)) == []


def test_no_belt_into_wall():
    ents = belt_run(0.5, 0.5, 3, d=4) + [ent("steam-engine", 4.5, 0.5, d=4)]
    fs = P.no_belt_into_wall(W(ents))
    assert len(fs) == 1 and "steam-engine" in fs[0]["msg"]
    assert P.no_belt_into_wall(W(belt_run(0.5, 0.5, 3, d=4))) == []


def test_underground_pairs_complete():
    paired = [ent("underground-belt", 0.5, 0.5, d=4, bg="input"),
              ent("underground-belt", 3.5, 0.5, d=4, bg="output")]
    assert P.underground_pairs_complete(W(paired)) == []
    orphan = [ent("underground-belt", 0.5, 0.5, d=4, bg="input")]
    fs = P.underground_pairs_complete(W(orphan))
    assert len(fs) == 1 and "no partner" in fs[0]["msg"]
    too_far = [ent("underground-belt", 0.5, 0.5, d=4, bg="input"),
               ent("underground-belt", 9.5, 0.5, d=4, bg="output")]
    assert len(P.underground_pairs_complete(W(too_far))) == 2


# --------------------------------------------------------------------------- P10 chests
def test_no_orphan_chest():
    lone = W([ent("wooden-chest", 0.5, 0.5)])
    fs = P.no_orphan_chest(lone)
    assert len(fs) == 1 and "no inserter" in fs[0]["msg"]
    served = W([ent("wooden-chest", 0.5, 0.5), ent("inserter", 0.5, 1.5, d=8),
                ent("transport-belt", 0.5, 2.5, d=4)])
    assert P.no_orphan_chest(served) == []


def test_chest_relay_is_rejected():
    """P10: belts buffer, chests only terminate. A belt->chest->belt relay is a hard stop
    where throughput becomes a human walking."""
    ents = [ent("wooden-chest", 0.5, 0.5),
            ent("inserter", 0.5, -0.5, d=0),        # picks from belt above, drops in chest
            ent("transport-belt", 0.5, -1.5, d=4),
            ent("inserter", 0.5, 1.5, d=0),         # picks from chest, drops to belt below
            ent("transport-belt", 0.5, 2.5, d=4)]
    fs = P.no_orphan_chest(W(ents))
    assert len(fs) == 1 and "relays belt->chest->belt" in fs[0]["msg"]


def test_io_cell_is_atomic():
    ents = [ent("inserter", 0.5, 0.5, d=8, s="waiting_for_source_items")]
    fs = P.io_cell_is_atomic(W(ents))
    assert len(fs) == 1 and "never built" in fs[0]["msg"]


# --------------------------------------------------------------------------- P11/P12 plant
def test_plant_ratio_ok():
    good = [ent("boiler", 0.5, 0.5), ent("boiler", 4.5, 0.5),
            ent("steam-engine", 0.5, -3.5), ent("steam-engine", 4.5, -3.5),
            ent("steam-engine", 0.5, -8.5), ent("steam-engine", 4.5, -8.5),
            ent("offshore-pump", 2.5, 6.5)]
    assert P.plant_ratio_ok(W(good)) == []
    orphan = good + [ent("steam-engine", 8.5, -3.5)]
    fs = P.plant_ratio_ok(W(orphan))
    assert any("1:2" in f["msg"] for f in fs)


def test_plant_needs_a_pump():
    fs = P.plant_ratio_ok(W([ent("boiler", 0.5, 0.5), ent("steam-engine", 0.5, -3.5),
                             ent("steam-engine", 0.5, -8.5)]))
    assert any("no offshore pump" in f["msg"] for f in fs)


def test_plant_sited_at_fuel():
    far = [ent("boiler", 0.5, 0.5), ent("stone-furnace", 3.5, 0.5),
           ent("electric-mining-drill", 90.5, 0.5, d=8, res="coal")]
    fs = P.plant_sited_at_fuel(W(far))
    assert len(fs) == 1 and "distance to the FUEL source" in fs[0]["msg"]
    near = [ent("boiler", 0.5, 0.5), ent("stone-furnace", 60.5, 0.5),
            ent("electric-mining-drill", 10.5, 0.5, d=8, res="coal")]
    assert P.plant_sited_at_fuel(W(near)) == []


# --------------------------------------------------------------------------- P13 order
def test_no_consumer_ahead_of_supply():
    fs = P.no_consumer_ahead_of_supply(W([ent("lab", 0.5, 0.5, e=1)], research=""))
    assert any("NO research queued" in f["msg"] for f in fs)
    ok = W([ent("lab", 0.5, 0.5, e=1)], research="automation")
    assert not any("NO research" in f["msg"] for f in P.no_consumer_ahead_of_supply(ok))


def test_assembler_needs_both_inserters():
    fs = P.no_consumer_ahead_of_supply(W([ent("assembling-machine-1", 0.5, 0.5, e=1)]))
    assert any("0 inserters" in f["msg"] for f in fs)


def test_debt_statuses_are_counted():
    ents = [ent("inserter", 0.5, 0.5, d=8, s="waiting_for_target_to_be_built", e=1)]
    fs = P.no_consumer_ahead_of_supply(W(ents))
    assert any("never built" in f["msg"] for f in fs)


# --------------------------------------------------------------------------- report / probe
def test_check_all_shape():
    rep = P.check_all(W(working_mine(), research="", production={"iron-plate": 5}))
    for key in ("ok", "errors", "warnings", "findings", "by_check", "by_principle",
                "metrics"):
        assert key in rep, key
    assert set(rep["by_check"]) == {fn.__name__ for fn in P.CHECKS}
    assert isinstance(rep["metrics"]["flow_coverage"], float)


def test_check_all_only_filter():
    rep = P.check_all(W(working_mine()), only={"plant_ratio_ok"})
    assert list(rep["by_check"]) == ["plant_ratio_ok"]


def test_a_raising_check_never_breaks_the_report():
    boom = lambda w: (_ for _ in ()).throw(ValueError("boom"))
    boom.__name__ = "boom"
    orig = P.CHECKS[:]
    P.CHECKS.append(boom)
    try:
        rep = P.check_all(W([]))
        assert any("check raised ValueError" in f["msg"] for f in rep["findings"])
    finally:
        P.CHECKS[:] = orig


def test_probe_reads_chunked_payload_and_cleans_up():
    payload = {"ents": [ent("small-electric-pole", 0.5, 0.5, e=1, deg=2)] * 200,
               "meta": {"tick": 42, "research": "", "production": {"coal": 120}}}
    fake = FakeRcon(payload)
    import rcon
    orig = rcon.run
    rcon.run = fake
    try:
        w = P.probe()
    finally:
        rcon.run = orig
    assert len(w.ents) == 200 and w.meta["tick"] == 42
    assert len(fake.payload) > P.CHUNK, "payload must span multiple chunks"
    assert any("storage._principles=nil" in c for c in fake.calls), "scratch not cleared"


def test_probe_lua_is_read_only():
    """Hard guard: the probe must never contain a mutating call. A write here would be
    catastrophic on the operator's live base."""
    lua = P.PROBE_LUA
    for banned in ("create_entity", "destroy", "set_recipe", "rotate", "walking_state",
                   "on_event", "on_nth_tick", "clear_items", "insert{", "remove{",
                   "teleport", "revive", "order_deconstruction"):
        assert banned not in lua, "PROBE_LUA contains a mutating call: %s" % banned


def test_format_report_runs():
    txt = P.format_report(P.check_all(W(working_mine())))
    assert "PRINCIPLES REPORT" in txt and "metrics:" in txt


# --------------------------------------------------------------------------- golden snapshots
SNAPS = pathlib.Path(__file__).resolve().parent / "snapshots"


def test_golden_operator_beats_bot():
    """The whole point, as a regression test: the operator's 619-entity base must beat the
    bot's 713-entity one on every headline metric. Numbers measured 2026-08-29."""
    if not (SNAPS / "before.json").exists() or not (SNAPS / "after.json").exists():
        print("    (skipped: snapshots/ not present)")
        return
    before = P.metrics(P.from_snapshot("before"))
    after = P.metrics(P.from_snapshot("after"))
    assert before["entities"] == 713 and after["entities"] == 619
    # P1: flow coverage 40% -> 95%
    assert before["flow_coverage"] < 0.45 and after["flow_coverage"] > 0.94
    # P2: two networks (one generator-less) -> one
    assert before["networks"] == 2 and after["networks"] == 1
    # P4: redundant coverage -> one pole per consumer
    assert before["incidences_per_consumer"] > 2.0
    assert after["incidences_per_consumer"] < 1.1
    assert after["consumers_per_pole"] > before["consumers_per_pole"]
    # fewer entities doing strictly more
    assert after["entities"] < before["entities"]


def test_golden_before_fails_the_named_checks():
    if not (SNAPS / "before.json").exists():
        print("    (skipped: snapshots/ not present)")
        return
    rep = P.check_all(P.from_snapshot("before"))
    failed = {f["check"] for f in rep["findings"] if f["severity"] == "error"}
    for expected in ("no_belt_without_consumer", "grid_is_single_network",
                     "production_is_moving", "underground_pairs_complete",
                     "drill_pitch_ok", "no_entity_overlap", "no_belt_into_wall",
                     "no_consumer_ahead_of_supply"):
        assert expected in failed, "before.json should fail %s" % expected
    assert not rep["ok"]


def test_golden_after_only_fails_on_inherited_drill_overlap():
    """Everything the operator could reach, he fixed. The only hard failures left in his
    base are the bot's own pitch-2 iron drills and the 13 belts buried under them, which
    a human physically cannot click."""
    if not (SNAPS / "after.json").exists():
        print("    (skipped: snapshots/ not present)")
        return
    rep = P.check_all(P.from_snapshot("after"))
    failed = {f["check"] for f in rep["findings"] if f["severity"] == "error"}
    assert failed == {"drill_pitch_ok", "no_entity_overlap"}, failed


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
