"""What worked, kept so it is reused rather than rediscovered."""
import skills as S


def ctx(**kw):
    return kw


# --------------------------------------------------------------------------- identity
def test_a_skill_is_an_action_plus_its_parameters():
    """Not the situation it ran in - that is what retrieval matches on separately."""
    assert S.key("lane", {"source": "big"}) == S.key("lane", {"source": "big"})
    assert S.key("lane", {"source": "big"}) != S.key("lane", {"source": "tiny"})


def test_parameter_order_does_not_change_identity():
    assert S.key("lane", {"a": 1, "b": 2}) == S.key("lane", {"b": 2, "a": 1})


# --------------------------------------------------------------------------- scoring
def test_one_lucky_win_is_not_a_hundred_percent_skill():
    """A Laplace prior, so a single fluke does not outrank a long record."""
    assert S.success({"wins": 1, "losses": 0}) < 0.9
    assert S.success({"wins": 20, "losses": 0}) > 0.9
    assert S.success({"wins": 0, "losses": 0}) == 0.5


def test_a_losing_skill_scores_below_an_unproven_one():
    assert S.success({"wins": 0, "losses": 5}) < 0.5


def test_fit_matches_the_situation_not_the_parameters():
    s = {"contexts": [ctx(machines=40, ore="iron-ore")]}
    assert S.fit(s, ctx(machines=40, ore="iron-ore")) == 1.0
    assert S.fit(s, ctx(machines=40, ore="copper-ore")) == 0.5
    assert S.fit({"contexts": []}, ctx(machines=40)) == 0.0


# --------------------------------------------------------------------------- learning
def test_a_skill_is_created_then_strengthened(tmp_path):
    p = tmp_path / "s.jsonl"
    S.record("build_lane", {"source": "big"}, ctx(machines=40), "good", path=p)
    S.record("build_lane", {"source": "big"}, ctx(machines=40), "good", path=p)
    r = S.rank("build_lane", ctx(machines=40), path=p)
    assert len(r) == 1 and r[0]["wins"] == 2


def test_wins_and_losses_are_tracked_separately(tmp_path):
    p = tmp_path / "s.jsonl"
    S.record("build_lane", {"source": "tiny"}, ctx(machines=40), "bad", path=p)
    S.record("build_lane", {"source": "tiny"}, ctx(machines=40), "bad", path=p)
    assert S.rank("build_lane", ctx(machines=40), path=p)[0]["losses"] == 2


def test_the_proven_option_is_ranked_first(tmp_path):
    """The live lesson: a one-drill mine could not feed forty furnaces; a five-drill one did.
    That is the conclusion the bot should reach from its own evidence, rather than from the
    MIN_FEED_DRILLS constant I hardcoded after watching it happen."""
    p = tmp_path / "s.jsonl"
    for _ in range(3):
        S.record("build_lane", {"source": "big"}, ctx(machines=40), "good", path=p)
        S.record("build_lane", {"source": "tiny"}, ctx(machines=40), "bad", path=p)
    cands = [{"source": "tiny"}, {"source": "big"}]
    out = S.prefer("build_lane", ctx(machines=40), cands, param_of=lambda c: c, path=p)
    assert out[0]["source"] == "big"


# --------------------------------------------------------------------------- preference
def test_nothing_is_ever_discarded(tmp_path):
    """A preference that silently drops an option makes the base unrecoverable the moment the
    library learns something wrong - which, with an automated verifier feeding it, is a when."""
    p = tmp_path / "s.jsonl"
    for _ in range(4):
        S.record("build_lane", {"source": "tiny"}, ctx(machines=40), "bad", path=p)
    cands = [{"source": "tiny"}, {"source": "big"}]
    out = S.prefer("build_lane", ctx(machines=40), cands, param_of=lambda c: c, path=p)
    assert len(out) == 2 and {c["source"] for c in out} == {"tiny", "big"}


def test_an_unseen_option_outranks_a_known_bad_one(tmp_path):
    p = tmp_path / "s.jsonl"
    for _ in range(4):
        S.record("build_lane", {"source": "tiny"}, ctx(machines=40), "bad", path=p)
    out = S.prefer("build_lane", ctx(machines=40),
                   [{"source": "tiny"}, {"source": "new"}], param_of=lambda c: c, path=p)
    assert out[0]["source"] == "new"


def test_an_anecdote_ranks_with_the_unknown(tmp_path):
    """One use is not yet a preference."""
    p = tmp_path / "s.jsonl"
    S.record("build_lane", {"source": "lucky"}, ctx(machines=40), "good", path=p)
    out = S.prefer("build_lane", ctx(machines=40),
                   [{"source": "lucky"}, {"source": "other"}], param_of=lambda c: c, path=p)
    assert [c["source"] for c in out] == ["lucky", "other"], "original order should survive"


def test_with_an_empty_library_the_original_order_survives(tmp_path):
    cands = [{"source": "a"}, {"source": "b"}]
    out = S.prefer("build_lane", ctx(), cands, param_of=lambda c: c, path=tmp_path / "n.jsonl")
    assert out == cands


def test_a_skill_learned_elsewhere_still_applies_here(tmp_path):
    """Contexts carry shape, not position, so the library transfers across the map."""
    p = tmp_path / "s.jsonl"
    for _ in range(3):
        S.record("build_lane", {"source": "big"}, ctx(machines=40, ore="iron-ore"), "good", path=p)
    r = S.rank("build_lane", ctx(machines=38, ore="iron-ore"), path=p)
    assert r and r[0]["score"] > 0.3


def test_explain_on_an_empty_library(tmp_path):
    assert "no skills learned yet" in S.explain(path=tmp_path / "none.jsonl")


def test_the_full_ordering_is_proven_then_unknown_then_failed(tmp_path):
    p = tmp_path / "s.jsonl"
    for _ in range(3):
        S.record("build_lane", {"source": "good"}, ctx(machines=40), "good", path=p)
        S.record("build_lane", {"source": "bad"}, ctx(machines=40), "bad", path=p)
    out = S.prefer("build_lane", ctx(machines=40),
                   [{"source": "bad"}, {"source": "new"}, {"source": "good"}],
                   param_of=lambda c: c, path=p)
    assert [c["source"] for c in out] == ["good", "new", "bad"]
