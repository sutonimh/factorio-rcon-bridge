"""A removal is a lesson about a KIND of mistake, never a forbidden coordinate."""
import corrections as C


def test_a_signature_carries_no_position():
    """'You put a smelter where nothing consumed its output' is portable.
    'Do not build at (31,-40)' is not."""
    s = C.signature("furnace", "smelter", "orphan_output")
    assert "31" not in s and "," not in s
    assert s == C.signature("furnace", "smelter", "orphan_output")


def test_a_correction_applies_anywhere_on_the_map(tmp_path):
    """The whole difference from protecting tiles: earned at one end, applies at the other."""
    p = tmp_path / "c.json"
    C.record([{"kind": "furnace", "role": "smelter", "output_consumed": False,
               "where": (31, -40)}], path=p)
    hit = C.check("furnace", "smelter", "orphan_output", path=p)
    assert hit and hit["count"] == 1


def test_ground_is_never_recorded_as_forbidden(tmp_path):
    """The examples are for a human reading the file, not a blacklist the builder consults."""
    p = tmp_path / "c.json"
    C.record([{"kind": "furnace", "role": "smelter", "output_consumed": False,
               "where": (31, -40)}], path=p)
    db = C.load(p)
    row = next(iter(db.values()))
    assert "where" not in row and "tiles" not in row
    assert row["examples"] == [[31, -40]]        # kept as evidence only
    assert C.check("furnace", "smelter", "orphan_output", path=p)["count"] == 1


def test_repeated_corrections_harden_into_a_rule(tmp_path):
    """One removal can be the operator tidying. Three is a policy."""
    p = tmp_path / "c.json"
    r = {"kind": "furnace", "role": "smelter", "output_consumed": False}
    for _ in range(C.HARD_AFTER):
        C.record([r], path=p)
    assert C.check("furnace", "smelter", "orphan_output", path=p)["hard"] is False
    C.record([r], path=p)
    assert C.check("furnace", "smelter", "orphan_output", path=p)["hard"] is True


def test_unknown_builds_are_not_blocked(tmp_path):
    p = tmp_path / "c.json"
    assert C.check("assembling-machine", "science", path=p) is None


# --------------------------------------------------------------------------- diagnosis
def test_diagnoses_the_faults_this_base_has_actually_produced():
    assert C.diagnose({"kind": "furnace", "output_consumed": False}, {}) == "orphan_output"
    assert C.diagnose({"kind": "assembling-machine", "input_fed": False}, {}) == "unfed_input"
    assert C.diagnose({"kind": "transport-belt", "connected": False}, {}) == "disconnected"
    assert C.diagnose({"kind": "furnace", "duplicate_of": "iron array"}, {}) == "duplicate"


def test_an_unexplained_removal_gets_no_invented_fault():
    """A fault name made up to fill the field would make a weak correction look strong."""
    assert C.diagnose({"kind": "transport-belt"}, {}) is None


def test_undiagnosed_signatures_are_surfaced_not_hidden(tmp_path):
    """They are where the model is still guessing, so they should be visible."""
    p = tmp_path / "c.json"
    C.record([{"kind": "transport-belt", "role": "lane"}], path=p)
    assert C.undiagnosed(p) == ["transport-belt|lane|?"]


def test_explain_marks_hard_rules(tmp_path):
    p = tmp_path / "c.json"
    r = {"kind": "furnace", "role": "smelter", "output_consumed": False}
    for _ in range(C.HARD_AFTER + 1):
        C.record([r], path=p)
    out = C.explain(p)
    assert "HARD RULE" in out and "orphan_output" in out


def test_explain_on_an_empty_store(tmp_path):
    assert "no corrections recorded" in C.explain(tmp_path / "nope.json")
