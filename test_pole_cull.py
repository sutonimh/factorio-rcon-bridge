"""Tests for pole_cull: a pole comes out only when removing it provably costs nothing."""
import pole_cull as PC
import power_planner as PP


def P(x, y, name="small-electric-pole"):
    return {"name": name, "x": x, "y": y}


def box(x, y, w=1, h=1):
    return (x, y, x + w - 1, y + h - 1)


# --------------------------------------------------------------------------- coverage
def test_covers_matches_power_planner():
    poles = [P(0, 0)]
    # small pole at tile (0,0) supplies the 5x5 window centred on it
    assert PC.coverage(poles, [box(2, 0)])[0] == {0}
    assert PC.coverage(poles, [box(3, 0)])[0] == set()


def test_consumer_with_no_pole_is_not_a_veto():
    """A machine nobody powers is already dark. It must not freeze cleanup forever."""
    poles = [P(0, 0), P(1, 0), P(2, 0), P(3, 0)]
    consumers = [box(0, 0), box(99, 99)]        # second is unreachable
    gone = PC.cull(poles, consumers)
    assert gone, "an already-dark consumer blocked the whole cull"


# --------------------------------------------------------------------------- the real job
def test_removes_duplicate_cover_four_tiles_apart():
    """The case the OLD dedupe_poles could never find: a small pole supplies a 5x5 window, so
    two poles FOUR tiles apart can cover the same consumer and one is spare. Its candidate
    rule was 'another pole within 2.0 tiles' and never saw these."""
    #  (2,0) covers x 0..4 | (6,0) covers x 4..8  -> both reach a consumer at x=4
    poles = [P(2, 0), P(6, 0), P(2, 4), P(6, 4)]
    consumers = [box(4, 0), box(4, 4)]
    for pi in range(4):                          # premise: every pole really does cover one
        assert any(PP.covers(poles[pi]["name"], poles[pi]["x"], poles[pi]["y"], c)
                   for c in consumers)
    gone = PC.cull(poles, consumers)
    assert len(gone) == 2, "expected one spare pole per consumer"
    keep = set(range(4)) - set(gone)
    assert PC.connected(poles, keep)
    for ci, pis in PC.coverage(poles, consumers).items():
        assert pis & keep, "consumer %d went dark" % ci


def test_keeps_the_only_pole_covering_a_consumer():
    poles = [P(0, 0), P(20, 0), P(26, 0), P(32, 0)]
    consumers = [box(0, 0), box(20, 0), box(26, 0), box(32, 0)]
    assert PC.cull(poles, consumers) == []


def test_never_splits_the_grid():
    """The bridge pole supplies nothing but ties two clusters together. Deleting connectors
    is exactly what islanded the steam engine every maintenance lap."""
    #  cluster A at x=0..1, bridge at x=7, cluster B at x=14..15
    poles = [P(0, 0), P(1, 0), P(7, 0), P(14, 0), P(15, 0)]
    consumers = [box(0, 0), box(15, 0)]
    gone = set(PC.cull(poles, consumers))
    assert 2 not in gone, "removed the bridge and split the network"


def test_removes_an_orphan_that_is_not_load_bearing():
    """A pole supplying nothing AND bridging nothing is pure waste. The old code refused to
    touch any orphan; with a connectivity test we can tell the two apart."""
    poles = [P(0, 0), P(1, 0), P(2, 0), P(3, 0)]
    consumers = [box(0, 0)]
    gone = PC.cull(poles, consumers)
    assert gone, "kept poles that power nothing and bridge nothing"
    assert PC.connected(poles, set(range(len(poles))) - set(gone))


def test_result_is_always_still_connected_and_covering():
    poles = [P(x, y) for x in range(0, 17, 2) for y in (0, 4)]
    consumers = [box(x, y) for x in range(0, 17, 4) for y in (0, 4)]
    gone = set(PC.cull(poles, consumers))
    keep = set(range(len(poles))) - gone
    assert PC.connected(poles, keep)
    cov = PC.coverage(poles, consumers)
    for ci, pis in cov.items():
        if pis:
            assert pis & keep, "consumer %d went dark" % ci


def test_generators_are_protected_by_being_consumers():
    """read_world appends generators to `consumers`; a layout that powers every machine but
    strands the engine is not a saving."""
    poles = [P(0, 0), P(1, 0), P(2, 0), P(6, 0), P(12, 0)]
    engine = box(12, 0, 3, 5)
    gone = set(PC.cull(poles, [box(0, 0), engine]))
    keep = set(range(len(poles))) - gone
    assert any(PP.covers(poles[i]["name"], poles[i]["x"], poles[i]["y"], engine) for i in keep)


# --------------------------------------------------------------------------- guards
def test_protect_is_honoured():
    poles = [P(0, 0), P(4, 0), P(8, 0)]
    consumers = [box(4, 0)]
    assert 1 not in PC.cull(poles, consumers, protect={1})


def test_tiny_grids_are_left_alone():
    assert PC.cull([P(0, 0), P(1, 0)], [box(0, 0)]) == []


def test_is_deterministic():
    poles = [P(x, 0) for x in range(0, 13)]
    consumers = [box(0, 0), box(6, 0), box(12, 0)]
    assert PC.cull(poles, consumers) == PC.cull(poles, consumers)


def test_cull_is_a_fixpoint():
    """Re-running on the surviving set must find nothing more, or the loop would keep
    nibbling poles lap after lap."""
    poles = [P(x, y) for x in range(0, 13, 2) for y in (0, 4)]
    consumers = [box(x, y) for x in range(0, 13, 4) for y in (0, 4)]
    keep = sorted(set(range(len(poles))) - set(PC.cull(poles, consumers)))
    survivors = [poles[i] for i in keep]
    assert PC.cull(survivors, consumers) == []


def test_explain_mentions_counts():
    poles = [P(2, 0), P(6, 0), P(2, 4), P(6, 4)]
    assert "cull" in PC.explain(poles, [box(4, 0), box(4, 4)])
    assert "none redundant" in PC.explain([P(0, 0), P(20, 0), P(26, 0), P(32, 0)],
                                          [box(0, 0), box(20, 0), box(26, 0), box(32, 0)])


def test_medium_poles_wire_at_the_shorter_reach():
    """A mixed-tier pair wires at the SHORTER of the two reaches; assuming the longer one
    would let us delete a pole and split the grid."""
    a, b = P(0, 0), P(0, 0, "medium-electric-pole")
    assert PC._wires(a, b) == PC._wires(b, a)


# --------------------------------------------------------------------------- the heal
def test_connected_is_about_what_is_achievable_not_what_exists():
    """`connected` answers "could these be one network", because a cull is always paired with
    rewire(). The first live run split the grid 86/19 by reading it as "already connected"."""
    poles = [P(0, 0), P(6, 0), P(12, 0)]
    assert PC.connected(poles, {0, 1, 2})
    assert PC.connected(poles, {0, 2}) is False      # 12 apart, beyond the 7.5 reach


def test_pos_prefers_the_real_position_when_we_have_it():
    """Deriving position back from the tile drops any pole the operator placed off our
    half-tile grid; the destroy then misses and the revert loses a pole."""
    assert PC._pos(P(3, 4)) == (3.5, 4.5)
    odd = dict(P(3, 4), px=3.25, py=4.75)
    assert PC._pos(odd) == (3.25, 4.75)


def test_chunks_cover_everything_exactly_once():
    """60-odd removals ran to 7k characters on one /sc line, which is a single line."""
    seq = list(range(57))
    out = [x for c in PC._chunks(seq, 25) for x in c]
    assert out == seq
    assert all(len(c) <= 25 for c in PC._chunks(seq, 25))


# --------------------------------------------------------------------------- split grids
def test_components_counts_islands():
    poles = [P(0, 0), P(6, 0), P(40, 0), P(46, 0)]
    assert PC.components(poles, {0, 1, 2, 3}) == 2
    assert PC.components(poles, {0, 1}) == 1
    assert PC.components(poles, set()) == 0


def test_cull_can_clean_up_a_grid_that_is_already_split():
    """Requiring a single network meant refusing to remove anything whenever the grid was
    already split - exactly when cleanup matters most. Three stray poles at x=-40 formed two
    islands, which tripped the grid_energized gate (blocking science_assembler, lab and
    mine_outpost) and the planner declared a deadlock, while the culler stood by unable to
    delete the three useless poles causing it."""
    main = [P(x, 0) for x in range(0, 13, 2)]          # one connected run
    strays = [P(60, 0), P(80, 0)]                      # two useless islands, powering nothing
    poles = main + strays
    consumers = [box(x, 0) for x in range(0, 13, 4)]
    gone = set(PC.cull(poles, consumers))
    assert len(main) in gone and len(main) + 1 in gone, "the isolated strays survived"
    keep = set(range(len(poles))) - gone
    assert PC.components(poles, keep) <= PC.components(poles, set(range(len(poles))))


def test_cull_still_refuses_to_split_a_whole_grid():
    poles = [P(0, 0), P(1, 0), P(7, 0), P(14, 0), P(15, 0)]
    consumers = [box(0, 0), box(15, 0)]
    assert 2 not in set(PC.cull(poles, consumers)), "removed the bridge and split the network"


def test_cull_never_increases_component_count():
    poles = [P(x, y) for x in range(0, 17, 2) for y in (0, 4)] + [P(70, 70), P(90, 90)]
    consumers = [box(x, y) for x in range(0, 17, 4) for y in (0, 4)]
    before = PC.components(poles, set(range(len(poles))))
    keep = set(range(len(poles))) - set(PC.cull(poles, consumers))
    assert PC.components(poles, keep) <= before


def test_a_dark_consumer_does_not_protect_its_island_poles():
    """Coverage is not power. An island with no generator covers plenty and supplies none of
    it, so its poles read as load-bearing and the culler refused to remove them - the exact
    poles tripping grid_energized and deadlocking the planner. The game's own no_power flag
    settles it."""
    main = [P(x, 0) for x in range(0, 13, 2)]
    island = [P(60, 0), P(62, 0)]
    poles = main + island
    stranded = box(61, 0)
    consumers = [box(x, 0) for x in range(0, 13, 4)] + [stranded]
    # without the hint, the island looks load-bearing and survives
    assert not (set(PC.cull(poles, consumers)) >= {len(main), len(main) + 1})
    # told the consumer is already dark, the island is provably waste
    gone = set(PC.cull(poles, consumers, dark=[stranded]))
    assert {len(main), len(main) + 1} <= gone, "the dead island survived"


def test_dark_hint_never_sacrifices_a_powered_consumer():
    poles = [P(0, 0), P(4, 0), P(8, 0), P(12, 0)]
    consumers = [box(0, 0), box(12, 0)]
    gone = set(PC.cull(poles, consumers, dark=[box(0, 0)]))
    keep = set(range(len(poles))) - gone
    cov = PC.coverage(poles, consumers)
    assert cov[1] & keep, "the still-powered consumer lost its last pole"
