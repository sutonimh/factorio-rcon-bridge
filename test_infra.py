"""Infrastructure is maintained from a census, never from a record of a base that moved."""
import infra


def block(x1, y1, x2, y2, inputs, kind="furnace"):
    return {"kind": kind, "count": 40, "bbox": (x1, y1, x2, y2),
            "io": {"input": inputs, "output": []}}


def mine(ore, x, y):
    return {"ore": ore, "drills": 5, "bbox": (x, y, x + 6, y), "drops": [(x, y + 2)]}


def test_the_nearest_mine_is_matched_to_a_starved_row():
    b = block(40, -31, 78, -21, [-31])
    c = {"mines": [mine("iron-ore", 14, -43), mine("copper-ore", -32, -66)],
         "unfed": [{"block": b, "input_row": -31, "feed_side": "east"}]}
    got = infra.assign(c)
    assert len(got) == 1 and got[0][0]["ore"] == "iron-ore"


def test_one_mine_is_not_assigned_to_two_blocks():
    """Otherwise both smelting blocks get lanes from the same patch and one starves."""
    b1 = block(40, -31, 78, -21, [-31])
    b2 = block(40, -15, 78, -5, [-15])
    c = {"mines": [mine("iron-ore", 14, -43), mine("copper-ore", 20, -20)],
         "unfed": [{"block": b1, "input_row": -31, "feed_side": "east"},
                   {"block": b2, "input_row": -15, "feed_side": "east"}]}
    ores = [m["ore"] for m, _, _, _ in infra.assign(c)]
    assert sorted(ores) == ["copper-ore", "iron-ore"]


def test_coal_is_not_treated_as_a_smelting_feed():
    """Furnaces burn it; it is not the ore the block is for."""
    b = block(40, -31, 78, -21, [-31])
    c = {"mines": [mine("coal", 0, 0)],
         "unfed": [{"block": b, "input_row": -31, "feed_side": "east"}]}
    assert infra.assign(c) == []


def test_assignment_is_deterministic():
    """A half-built lane must be resumed, not replaced by a different plan next pass."""
    b = block(40, -31, 78, -21, [-31])
    c = {"mines": [mine("iron-ore", 14, -43), mine("copper-ore", 16, -43)],
         "unfed": [{"block": b, "input_row": -31, "feed_side": "east"}]}
    assert infra.assign(c)[0][0]["ore"] == infra.assign(c)[0][0]["ore"]


def test_the_feed_tile_is_past_the_correct_end():
    """A west-flowing input row is fed from the EAST. Delivering to the wrong end puts ore on
    a belt that carries it away from the furnaces."""
    b = block(40, -31, 78, -21, [-31])
    assert infra.feed_tile(b, -31, "east") == (80, -31)
    assert infra.feed_tile(b, -31, "west") == (38, -31)


def test_an_ambiguous_feed_side_is_refused_not_guessed():
    b = block(40, -31, 78, -21, [-31])
    assert infra.feed_tile(b, -31, None) is None


def test_nothing_unfed_means_nothing_to_do():
    assert infra.assign({"mines": [mine("iron-ore", 0, 0)], "unfed": []}) == []


def test_every_input_row_of_a_block_gets_the_SAME_ore():
    """A smelting block's rows are all the same feedstock. Assigning per row sent copper ore
    to the iron block's second input, because that row happened to be next in the list."""
    b = block(40, -28, 78, -23, [-31, -21])
    c = {"mines": [mine("iron-ore", 14, -43), mine("copper-ore", -32, -66)],
         "unfed": [{"block": b, "input_row": -31, "feed_side": "east"},
                   {"block": b, "input_row": -21, "feed_side": "east"}]}
    got = infra.assign(c)
    assert len(got) == 2
    assert {m["ore"] for m, _, _, _ in got} == {"iron-ore"}, "a block was fed two different ores"
    assert sorted(r for _, _, r, _ in got) == [-31, -21]


def test_two_blocks_still_get_different_ores():
    b1 = block(40, -28, 78, -23, [-31])
    b2 = block(40, -12, 78, -7, [-15])
    c = {"mines": [mine("iron-ore", 14, -43), mine("copper-ore", 30, -20)],
         "unfed": [{"block": b1, "input_row": -31, "feed_side": "east"},
                   {"block": b2, "input_row": -15, "feed_side": "east"}]}
    got = infra.assign(c)
    assert len({m["ore"] for m, _, _, _ in got}) == 2


class _FakeA:
    def __init__(self, inv):
        self.inv = inv
        self.sent = []
    def _print(self, lua):
        if "get_item_count" in lua and "rcon.print(table.concat" in lua:
            return " ".join("%s=%d" % kv for kv in self.inv.items())
        self.sent.append(lua)
        return ""


def test_a_partial_lane_is_refused(monkeypatch):
    """Half a lane is not half a feed, it is a broken belt that reads as one."""
    import bootstrap as B
    monkeypatch.setattr(B, "operator_present", lambda: False)
    route = [{"x": i, "y": 0, "dir": 4, "entity": "transport-belt"} for i in range(60)]
    monkeypatch.setattr(infra, "plan_lanes",
                        lambda *a, **k: [{"ore": "iron-ore", "from": (0, 0), "to": (60, 0),
                                          "route": route, "row": 0, "block": (0, 0, 1, 1)}])
    fake = _FakeA({"transport-belt": 59})
    said = []
    assert infra.build_lanes(fake, {}, log=said.append) == []
    assert any("not laying a partial run" in m for m in said)
    assert fake.sent == [], "it built anyway"


def test_a_full_lane_is_laid(monkeypatch):
    import bootstrap as B
    monkeypatch.setattr(B, "operator_present", lambda: False)
    route = [{"x": i, "y": 0, "dir": 4, "entity": "transport-belt"} for i in range(60)]
    monkeypatch.setattr(infra, "plan_lanes",
                        lambda *a, **k: [{"ore": "iron-ore", "from": (0, 0), "to": (60, 0),
                                          "route": route, "row": 0, "block": (0, 0, 1, 1)}])
    fake = _FakeA({"transport-belt": 200})
    got = infra.build_lanes(fake, {}, log=lambda m: None)
    assert len(got) == 1 and fake.sent, "nothing was sent to the game"


def test_the_truce_stops_it(monkeypatch):
    import bootstrap as B
    monkeypatch.setattr(B, "operator_present", lambda: True)
    fake = _FakeA({"transport-belt": 999})
    assert infra.build_lanes(fake, {}, log=lambda m: None) == []
    assert fake.sent == []


def test_one_lane_per_pass(monkeypatch):
    """A lane is dozens of belts; the controller's other duties should not wait behind it.
    The next pass re-censuses and moves to the next gap."""
    import bootstrap as B
    monkeypatch.setattr(B, "operator_present", lambda: False)
    route = [{"x": 0, "y": 0, "dir": 4, "entity": "transport-belt"}]
    monkeypatch.setattr(infra, "plan_lanes", lambda *a, **k: [
        {"ore": "iron-ore", "from": (0, 0), "to": (1, 0), "route": route, "row": 0,
         "block": (0, 0, 1, 1)},
        {"ore": "copper-ore", "from": (0, 0), "to": (2, 0), "route": route, "row": 0,
         "block": (0, 0, 1, 1)}])
    fake = _FakeA({"transport-belt": 999})
    assert len(infra.build_lanes(fake, {}, log=lambda m: None)) == 1


def test_capacity_beats_distance_when_choosing_a_mine():
    """Picking the nearest sent a ONE-DRILL outpost to feed a forty-furnace block, because it
    sat closer than the five-drill patch. The lane built fine and delivered four ore. Belt
    length is cheap and one-off; a source that cannot fill the block starves it forever."""
    b = block(40, -28, 78, -23, [-31])
    near_tiny = dict(mine("iron-ore", 35, -44), drills=1)
    far_big = dict(mine("iron-ore", 14, -43), drills=5)
    c = {"mines": [near_tiny, far_big],
         "unfed": [{"block": b, "input_row": -31, "feed_side": "east"}]}
    chosen = infra.assign(c)[0][0]
    assert chosen["drills"] == 5, "picked the nearest mine over the one that can feed the block"


def test_distance_still_breaks_ties_between_equal_mines():
    b = block(40, -28, 78, -23, [-31])
    near = dict(mine("iron-ore", 30, -40), drills=5)
    far = dict(mine("copper-ore", -80, -80), drills=5)
    c = {"mines": [far, near],
         "unfed": [{"block": b, "input_row": -31, "feed_side": "east"}]}
    assert infra.assign(c)[0][0]["ore"] == "iron-ore"


def test_a_blocks_known_ore_overrides_capacity():
    """Capacity-first sent COPPER to the iron block, because copper had six drills to iron's
    five and nothing recorded what the block smelts. If the block has ever run, it knows."""
    b = dict(block(40, -28, 78, -23, [-31]), ore="iron-ore")
    c = {"mines": [dict(mine("copper-ore", -32, -66), drills=6),
                   dict(mine("iron-ore", 14, -43), drills=5)],
         "unfed": [{"block": b, "input_row": -31, "feed_side": "east"}]}
    assert infra.assign(c)[0][0]["ore"] == "iron-ore"


def test_an_unknown_block_still_falls_back_to_capacity():
    """A cold block that has never run says None, and inventing an affinity from where it
    sits would be a guess dressed as knowledge."""
    b = block(40, -28, 78, -23, [-31])
    c = {"mines": [dict(mine("copper-ore", -32, -66), drills=6),
                   dict(mine("iron-ore", 35, -44), drills=1)],
         "unfed": [{"block": b, "input_row": -31, "feed_side": "east"}]}
    assert infra.assign(c)[0][0]["drills"] == 6


def test_a_tiny_outpost_is_not_a_candidate_while_a_real_mine_exists():
    """Nearest-only sent a one-drill outpost to feed forty furnaces; the lane built fine and
    delivered four ore."""
    b = block(40, -28, 78, -23, [-31])
    c = {"mines": [dict(mine("iron-ore", 35, -44), drills=1),
                   dict(mine("iron-ore", 14, -43), drills=5)],
         "unfed": [{"block": b, "input_row": -31, "feed_side": "east"}]}
    assert infra.assign(c)[0][0]["drills"] == 5


def test_among_viable_mines_the_nearest_wins():
    """Capacity-only sent copper 148 belts past a five-drill iron patch sixty belts away,
    because copper had one more drill. Past the floor, belt is just cost."""
    b = block(40, -28, 78, -23, [-31])
    c = {"mines": [dict(mine("copper-ore", -32, -66), drills=6),
                   dict(mine("iron-ore", 14, -43), drills=5)],
         "unfed": [{"block": b, "input_row": -31, "feed_side": "east"}]}
    assert infra.assign(c)[0][0]["ore"] == "iron-ore"


def test_a_tiny_mine_is_used_when_it_is_all_there_is():
    b = block(40, -28, 78, -23, [-31])
    c = {"mines": [dict(mine("iron-ore", 35, -44), drills=1)],
         "unfed": [{"block": b, "input_row": -31, "feed_side": "east"}]}
    assert infra.assign(c) != []


def test_a_lane_is_extended_from_where_the_ore_already_reaches():
    """The mine's drop tile usually already has a belt on it, so routing from there finds no
    route at all - and when it does, it builds a second lane beside the one that exists."""
    m = mine("iron-ore", 14, -43)
    census = {"lane_ends": [(26, -41), (999, 999)]}
    assert infra._lane_start(m, census) == (26, -41)


def test_with_no_existing_lane_it_starts_at_the_drop():
    m = mine("iron-ore", 14, -43)
    assert infra._lane_start(m, {"lane_ends": []}) == m["drops"][0]


def test_a_far_away_lane_end_is_not_treated_as_this_mines():
    m = mine("iron-ore", 14, -43)
    assert infra._lane_start(m, {"lane_ends": [(900, 900)]}) == m["drops"][0]
