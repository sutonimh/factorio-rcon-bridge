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



def test_summary_of_an_empty_base():
    assert "nothing on the base yet" in W.summary({})







# --------------------------------------------------------------------------- starvation
def _blk(inputs):
    return {"kind": "furnace", "count": 40, "bbox": (40, -28, 78, -23),
            "io": {"input": inputs, "output": [-26]}}


def test_starvation_is_observed_not_inferred_from_belts():
    """Three attempts at deriving "is this row fed?" from belt layout each needed another
    special case - a connected lane has no terminus; a print's input belt overhangs its
    machines so the block's OWN belt counted as its supply, hiding 40 starving furnaces.
    The machines already know."""
    b = _blk([-31])
    hungry = {(50, -28): True, (52, -28): True}
    got = W.unfed_blocks([b], starved=hungry, belt_dirs={-31: [12] * 41})
    assert len(got) == 1 and got[0]["starved"] == 2
    assert got[0]["feed_side"] == "east"


def test_a_fed_block_is_not_reported_however_its_belts_look():
    b = _blk([-31])
    assert W.unfed_blocks([b], starved={}) == []


def test_starving_machines_elsewhere_do_not_implicate_this_block():
    b = _blk([-31])
    assert W.unfed_blocks([b], starved={(500, 500): True}) == []


def test_summary_says_which_end_the_lane_should_reach():
    got = [{"block": {"kind": "furnace"}, "input_row": -31, "feed_side": "east", "starved": 40}]
    out = W.summary({"unfed": got})
    assert "STARVED" in out and "east end" in out and "y=-31" in out


def test_the_feed_side_vote_is_scoped_to_the_block():
    """belt_dirs is keyed by row alone, so every belt anywhere on that y - including a lane
    ninety tiles away serving something else - voted on which end of THIS block to feed. Live,
    the array's own input row ran EAST while distant belts on the same y ran west, so the
    census said "feed the east end" and every lane was aimed at the wrong side."""
    b = {"kind": "furnace", "count": 40, "bbox": (40, -28, 78, -23),
         "io": {"input": [-31], "output": []}}
    near = {(x, -31): 4 for x in range(40, 79)}      # the block's own row: flows EAST
    far = {(x, -31): 12 for x in range(-200, -150)}  # something else entirely, flows west
    tiles = dict(near); tiles.update(far)
    got = W.unfed_blocks([b], [], {-31: [12] * 50 + [4] * 39}, belt_tiles=tiles,
                         starved={(50, -28): True})
    assert got and got[0]["feed_side"] == "west", \
        "distant belts on the same row decided this block's feed side"
