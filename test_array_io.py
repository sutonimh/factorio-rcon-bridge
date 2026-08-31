"""Tests for array_io: ask the inserters which belt is which, never the layout."""
import array_io as IO


def test_classifies_the_measured_iron_array():
    """The live iron array at x38..81: belts y=-31/-26/-21, furnaces y=-28/-23.
    17x (-31 -> -29), 18x (-28 -> -26), 18x (-24 -> -26), 20x (-21 -> -23)."""
    inserters = ([(-31, -29)] * 17 + [(-28, -26)] * 18 + [(-24, -26)] * 18 + [(-21, -23)] * 20)
    io = IO.classify(inserters, machine_rows={-28, -23, -29, -24})
    assert io["input"] == [-31, -21]
    assert io["output"] == [-26]


def test_an_output_is_never_mistaken_for_an_input():
    """The bus was once wired to two smelter INPUT belts because a belt near a smelter looked
    good enough. Direction of transfer is the whole distinction."""
    io = IO.classify([(5, 3)], machine_rows={3})     # picks off belt 5 INTO machine 3
    assert io == {"input": [5], "output": []}
    io = IO.classify([(3, 5)], machine_rows={3})     # picks off machine 3 ONTO belt 5
    assert io == {"input": [], "output": [5]}


def test_machine_to_machine_inserters_are_not_belts():
    """A lab or furnace chain hands along machine to machine; neither row is a belt."""
    assert IO.classify([(3, 7)], machine_rows={3, 7}) == {"input": [], "output": []}


def test_a_shared_middle_belt_is_reported_as_both():
    """Real configuration: one belt feeds a row below and collects from a row above. Hiding
    that would be worse than saying so."""
    io = IO.classify([(5, 3), (7, 5)], machine_rows={3, 7})
    assert io["input"] == [5] and io["output"] == [5]


def test_feed_end_follows_the_belt_direction():
    """Items enter where they come FROM. Feeding the wrong end puts ore on a belt that
    carries it away from the furnaces - a connected lane that delivers nothing."""
    assert IO.feed_end([12] * 41) == "east"      # flowing west -> feed the east end
    assert IO.feed_end([4] * 41) == "west"
    assert IO.feed_end([]) is None
    assert IO.feed_end([4, 12]) is None          # ambiguous, say so rather than guess


def test_feed_end_tolerates_a_few_corner_tiles():
    """A real row has a corner or two; the majority direction is the flow."""
    assert IO.feed_end([12] * 39 + [4, 8]) == "east"


def test_describe_names_the_end_to_feed():
    io = {"input": [-31], "output": [-26]}
    out = IO.describe(io, {-31: [12] * 41})
    assert "input y=-31 (feed from the east)" in out and "output y=-26" in out


def test_empty_array_says_so_rather_than_guessing():
    assert "no belt-to-machine inserters" in IO.describe({"input": [], "output": []}, {})
