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
