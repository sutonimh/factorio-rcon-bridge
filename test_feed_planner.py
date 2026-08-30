"""Tests for feed_planner: the bot works out where a feed goes instead of asking."""
import feed_planner as F


def box(x, y, w=3, h=3):
    return (x, y, x + w - 1, y + h - 1)


def ins(pick, drop):
    return {"pick": pick, "drop": drop}


# --------------------------------------------------------------------------- chain reading
def test_chain_graph_reads_lab_to_lab_handoff():
    """An inserter picking out of one lab and dropping into the next is what makes a lab grid
    flood from one corner. Nobody has to say so; it is on the map."""
    labs = [box(0, 0), box(4, 0), box(8, 0)]
    inserters = [ins((2, 1), (4, 1)), ins((6, 1), (8, 1))]
    g = F.chain_graph(labs, inserters)
    assert g[0] == {1} and g[1] == {2} and g[2] == set()


def test_chain_graph_ignores_inserters_that_touch_no_sink():
    labs = [box(0, 0)]
    assert F.chain_graph(labs, [ins((50, 50), (51, 50))])[0] == set()


def test_chain_graph_ignores_a_sinks_self_loop():
    labs = [box(0, 0)]
    assert F.chain_graph(labs, [ins((0, 0), (1, 1))])[0] == set()


def test_head_is_the_one_that_reaches_the_most():
    labs = [box(0, 0), box(4, 0), box(8, 0)]
    g = F.chain_graph(labs, [ins((2, 1), (4, 1)), ins((6, 1), (8, 1))])
    assert F.head(labs, g) == 0


def test_head_handles_a_grid_that_floods_two_ways():
    """The real array feeds east AND down from one corner."""
    labs = [box(0, 0), box(4, 0), box(0, 4), box(4, 4)]
    g = F.chain_graph(labs, [ins((2, 1), (4, 1)), ins((1, 2), (1, 4)), ins((6, 1), (4, 5))])
    assert F.head(labs, g) == 0


def test_head_is_stable_when_nothing_chains():
    """No chain -> every sink reaches only itself; the tie-break must still be deterministic,
    or the feed would move every lap."""
    labs = [box(8, 8), box(0, 0), box(4, 4)]
    g = F.chain_graph(labs, [])
    assert F.head(labs, g) == F.head(labs, g)


def test_head_of_nothing_is_none():
    assert F.head([], {}) is None


# --------------------------------------------------------------------------- injection
def test_injection_points_need_both_tiles_free():
    """The inserter goes against the machine and the belt on its far side."""
    sink = box(0, 0)
    pts = F.injection_points(sink, lambda t: True)
    assert ((-1, 0), (-2, 0)) in pts
    assert ((0, -1), (0, -2)) in pts
    for inserter, belt in pts:                       # never on the machine itself
        assert inserter not in F._tiles(sink)
        assert belt not in F._tiles(sink)


def test_injection_points_skips_blocked_belt_tile():
    sink = box(0, 0)
    blocked = {(-2, 0)}
    pts = F.injection_points(sink, lambda t: t not in blocked)
    assert ((-1, 0), (-2, 0)) not in pts
    assert ((0, -1), (0, -2)) in pts


def test_injection_points_empty_when_boxed_in():
    assert F.injection_points(box(0, 0), lambda t: False) == []


def test_rank_drops_unroutable_candidates():
    """An unroutable spot must be removed, not merely ranked last - otherwise a caller taking
    the first entry could pick one that cannot be reached."""
    cands = [((-1, 0), (-2, 0)), ((0, -1), (0, -2))]
    ranked = F.rank_injections(cands, lambda b: None if b == (-2, 0) else 5)
    assert [r[1] for r in ranked] == [(0, -2)]


def test_rank_is_cheapest_first():
    cands = [((-1, 0), (-2, 0)), ((0, -1), (0, -2))]
    ranked = F.rank_injections(cands, lambda b: 9 if b == (-2, 0) else 3)
    assert ranked[0][1] == (0, -2) and ranked[0][2] == 3


# --------------------------------------------------------------------------- geometry
def test_toward_matches_factorio_directions():
    assert F._toward((0, 0), (1, 0)) == 4       # east
    assert F._toward((0, 0), (-1, 0)) == 12     # west
    assert F._toward((0, 0), (0, 1)) == 8       # south
    assert F._toward((0, 0), (0, -1)) == 0      # north


def test_beside_never_starts_on_the_source_belt():
    """Starting a route on the source belt lets the router's cheapest first move be a reversal
    back down the line that feeds it - it paved over a live output column once already."""
    assert F._beside((5, 5), set()) != (5, 5)


def test_beside_respects_occupancy():
    occupied = {(4, 5), (6, 5), (5, 4)}
    assert F._beside((5, 5), occupied) == (5, 6)


def test_tiles_covers_the_whole_box():
    assert F._tiles((0, 0, 1, 1)) == {(0, 0), (0, 1), (1, 0), (1, 1)}


def test_describe_reports_the_reason_when_blocked():
    assert "nothing on this map consumes" in F.describe(
        {"item": "x", "reason": "nothing on this map consumes x"})


# --------------------------------------------------------------------------- fallback order
def test_by_reach_is_best_first():
    labs = [box(0, 0), box(4, 0), box(8, 0)]
    g = F.chain_graph(labs, [ins((2, 1), (4, 1)), ins((6, 1), (8, 1))])
    assert F.by_reach(labs, g) == [0, 1, 2]


def test_by_reach_lets_the_planner_fall_back_past_a_walled_in_head():
    """The live case: the true head reaches 9 of 10 labs but every tile around it is taken by
    the chain inserters, and the only lab with a free slot reaches 3. Insisting on the head
    would mean doing nothing, which is worse than the smaller feed."""
    labs = [box(0, 0), box(4, 0), box(8, 0)]
    g = F.chain_graph(labs, [ins((2, 1), (4, 1)), ins((6, 1), (8, 1))])
    order = F.by_reach(labs, g)
    walled = {order[0]}
    nxt = next(si for si in order if si not in walled)
    assert nxt == 1 and len(F.reachable(g, nxt)) == 2


def test_by_reach_is_deterministic_with_no_chain():
    labs = [box(8, 8), box(0, 0), box(4, 4)]
    g = F.chain_graph(labs, [])
    assert F.by_reach(labs, g) == F.by_reach(labs, g)
    assert F.head(labs, g) == F.by_reach(labs, g)[0]


def test_describe_reports_how_much_of_the_array_a_feed_reaches():
    d = F.describe({"item": "p", "from": (0, 0), "to_sink": (1, 1, 3, 3), "inserter": (0, 1),
                    "route": [1, 2, 3], "reaches": 3, "of": 10})
    assert "reaches 3 of 10" in d


# --------------------------------------------------------------------------- materials
def test_shortfall_reports_only_what_is_missing():
    assert F.shortfall({"transport-belt": 68, "inserter": 1},
                       {"transport-belt": 68, "inserter": 1}) == {}
    assert F.shortfall({"transport-belt": 68}, {"transport-belt": 60}) == {"transport-belt": 8}


def test_shortfall_treats_absent_as_zero():
    assert F.shortfall({"inserter": 1}, {}) == {"inserter": 1}


def test_shortfall_is_what_blocks_a_partial_run():
    """Half a lane is not half a feed, it is a broken belt that reads as a feed."""
    assert F.shortfall({"transport-belt": 68}, {"transport-belt": 67})
