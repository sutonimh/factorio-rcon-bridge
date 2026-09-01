"""Structure is DERIVED, never remembered - so moving the base cannot break a self-heal."""
import world_model as W


def d(x, y, ore="iron-ore", drop=None):
    return {"x": x, "y": y, "ore": ore, "drop": drop or (x, y + 2)}


def m(x, y, kind="furnace"):
    return {"x": x, "y": y, "kind": kind}


def ins(pick, drop):
    return {"pick": pick, "drop": drop}


# --------------------------------------------------------------------------- clustering
def test_nearby_points_form_one_cluster():
    assert W.cluster([(0, 0), (4, 0), (8, 0)]) == [[(0, 0), (4, 0), (8, 0)]]


def test_distant_points_are_separate_clusters():
    got = W.cluster([(0, 0), (100, 0)])
    assert len(got) == 2


def test_clustering_is_deterministic():
    pts = [(9, 1), (0, 0), (4, 0), (80, 3), (84, 3)]
    assert W.cluster(pts) == W.cluster(list(reversed(pts)))


def test_bbox_covers_the_group():
    assert W.bbox([(1, 5), (9, 2)]) == (1, 2, 9, 5)


# --------------------------------------------------------------------------- mines
def test_a_mine_is_wherever_drills_are_standing():
    """Not a coordinate anyone recorded. lanes.json said the iron mine fed (-4,-40); the
    drills say otherwise, and the drills are right by construction."""
    got = W.mines([d(14, -42), d(18, -42), d(20, -42)])
    assert len(got) == 1
    assert got[0]["ore"] == "iron-ore" and got[0]["drills"] == 3
    assert got[0]["bbox"] == (14, -42, 20, -42)


def test_two_ores_are_two_mines_even_when_adjacent():
    got = W.mines([d(0, 0, "iron-ore"), d(2, 0, "copper-ore")])
    assert sorted(g["ore"] for g in got) == ["copper-ore", "iron-ore"]


def test_moving_the_mine_needs_no_bookkeeping():
    """The whole point: relocate the drills and the next census simply sees them there."""
    before = W.mines([d(14, -42), d(18, -42)])
    after = W.mines([d(214, 342), d(218, 342)])
    assert before[0]["drills"] == after[0]["drills"]
    assert after[0]["bbox"] == (214, 342, 218, 342)


def test_drop_tiles_are_carried_through():
    got = W.mines([d(14, -42, drop=(14, -40))])
    assert got[0]["drops"] == [(14, -40)]


# --------------------------------------------------------------------------- blocks
def test_a_block_derives_its_input_and_output_rows():
    """The measured iron array: belts y=-31/-26/-21, furnaces y=-28/-23."""
    machines = [m(x, -28) for x in range(40, 60, 2)] + [m(x, -23) for x in range(40, 60, 2)]
    inserters = ([ins((45, -31), (45, -29))] + [ins((45, -28), (45, -26))]
                 + [ins((45, -24), (45, -26))] + [ins((45, -21), (45, -23))])
    got = W.blocks(machines, inserters)
    assert len(got) == 1
    io = got[0]["io"]
    assert -31 in io["input"] and -21 in io["input"]
    assert -26 in io["output"]


def test_two_separate_arrays_are_two_blocks():
    machines = [m(x, 0) for x in range(0, 10, 2)] + [m(x, 60) for x in range(0, 10, 2)]
    got = W.blocks(machines, [])
    assert len(got) == 2


def test_a_block_with_no_inserters_reports_no_io():
    got = W.blocks([m(0, 0), m(2, 0)], [])
    assert got[0]["io"] == {"input": [], "output": []}


# --------------------------------------------------------------------------- the gaps
def test_an_input_row_with_no_lane_reaching_it_is_unfed():
    """Derivable, rather than something a stalled-flow detector infers minutes later."""
    blocks = [{"kind": "furnace", "count": 20, "bbox": (38, -31, 81, -21),
               "io": {"input": [-31], "output": [-26]}}]
    assert W.unfed_blocks(blocks, lane_ends=[]) != []
    assert W.unfed_blocks(blocks, lane_ends=[(82, -31)]) == []


def test_a_lane_ending_far_away_does_not_count_as_feeding():
    blocks = [{"kind": "furnace", "count": 20, "bbox": (38, -31, 81, -21),
               "io": {"input": [-31], "output": []}}]
    assert W.unfed_blocks(blocks, lane_ends=[(200, -31)]) != []


def test_summary_names_what_is_starved():
    census = {"mines": [{"ore": "iron-ore", "drills": 5, "bbox": (14, -42, 24, -42)}],
              "blocks": [], "unfed": [{"block": {"kind": "furnace"}, "input_row": -31}]}
    out = W.summary(census)
    assert "iron-ore mine: 5 drills" in out
    assert "UNFED" in out and "y=-31" in out


def test_summary_of_an_empty_base():
    assert "nothing on the base yet" in W.summary({})


def test_a_lane_ending_on_the_WRONG_side_does_not_feed_the_block():
    """An input row flowing west is fed at its EAST end. A lane ending at the west end is
    that belt running out, not a delivery. The first version counted any end near the row and
    declared both smelting blocks fed, when their only lane ends were their own input belts
    terminating at the far side."""
    blocks = [{"kind": "furnace", "count": 40, "bbox": (40, -31, 78, -21),
               "io": {"input": [-31], "output": [-26]}}]
    dirs = {-31: [12] * 41}                     # flows west -> must be fed from the east
    assert W.unfed_blocks(blocks, [(40, -31)], dirs) != [], "west-end terminus counted as a feed"
    assert W.unfed_blocks(blocks, [(82, -31)], dirs) == [], "east-end delivery not recognised"


def test_the_feed_side_is_reported():
    blocks = [{"kind": "furnace", "count": 40, "bbox": (40, -31, 78, -21),
               "io": {"input": [-31], "output": []}}]
    got = W.unfed_blocks(blocks, [], {-31: [12] * 41})
    assert got[0]["feed_side"] == "east"
    assert "east end" in W.summary({"unfed": got})


def test_without_directions_it_falls_back_to_the_laxer_test():
    """Stated plainly so a caller knows what it bought."""
    blocks = [{"kind": "furnace", "count": 40, "bbox": (40, -31, 78, -21),
               "io": {"input": [-31], "output": []}}]
    assert W.unfed_blocks(blocks, [(40, -31)]) == []


def test_a_belt_arriving_at_the_feed_end_counts_as_fed():
    """A CONNECTED lane has no terminus - it runs into the block - so testing for a lane END
    marked the working iron lane (58 plates/min) as unfed, which would have sent a fixer to
    rebuild a belt that was already delivering. Ask whether something ARRIVES."""
    blocks = [{"kind": "furnace", "count": 40, "bbox": (40, -31, 78, -21),
               "io": {"input": [-31], "output": []}}]
    dirs = {-31: [12] * 41}
    assert W.unfed_blocks(blocks, [], dirs, belt_tiles=[(79, -31)]) == []
    assert W.unfed_blocks(blocks, [], dirs, belt_tiles=[(20, -31)]) != []
