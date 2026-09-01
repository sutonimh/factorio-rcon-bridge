"""Experience the bot can consult BEFORE acting - the half that was missing."""
import memory as M


def ctx(**kw):
    return kw


# --------------------------------------------------------------------------- scoring
def test_recency_decays_but_never_vetoes_forever():
    """A base changes. A lane that failed yesterday for reasons since fixed should count
    less, not block the action for good."""
    now = 1_000_000.0
    assert M.recency(now, now) == 1.0
    assert 0.49 < M.recency(now - M.HALF_LIFE_S, now) < 0.51
    assert M.recency(now - 100 * M.HALF_LIFE_S, now) >= 0.0


def test_relevance_is_partial_not_exact():
    """Exact-key matching is what made corrections.check() useless: a signature had to line up
    perfectly, so almost nothing ever retrieved."""
    a = ctx(action="lane", mine="small", block="big")
    assert M.relevance(a, a) == 1.0
    assert M.relevance(a, ctx(action="lane", mine="small", block="small")) == 2 / 3
    assert M.relevance(a, ctx(action="pole")) == 0.0


def test_numerically_close_contexts_partly_match():
    assert M.relevance(ctx(drills=5), ctx(drills=5)) == 1.0
    assert 0.7 < M.relevance(ctx(drills=5), ctx(drills=4)) < 0.9
    assert M.relevance(ctx(drills=5), ctx(drills=100)) < 0.1


def test_relevance_of_an_empty_context_is_zero():
    assert M.relevance({}, ctx(a=1)) == 0.0


# --------------------------------------------------------------------------- store
def test_remember_then_recall(tmp_path):
    p = tmp_path / "m.jsonl"
    M.remember("build_lane", ctx(mine="small"), "bad", path=p, detail="delivered 4 ore")
    got = M.recall("build_lane", ctx(mine="small"), path=p)
    assert len(got) == 1 and got[0]["outcome"] == "bad"


def test_a_torn_line_does_not_poison_the_memory(tmp_path):
    p = tmp_path / "m.jsonl"
    M.remember("a", ctx(x=1), "good", path=p)
    with p.open("a") as f:
        f.write("{not json\n")
    M.remember("a", ctx(x=1), "good", path=p)
    assert len(M._all(p)) == 2


def test_recall_ranks_the_similar_situation_first(tmp_path):
    p = tmp_path / "m.jsonl"
    M.remember("build_lane", ctx(mine="big", block="big"), "good", path=p, now=1000)
    M.remember("build_lane", ctx(mine="small", block="big"), "bad", path=p, now=1000)
    top = M.recall("build_lane", ctx(mine="small", block="big"), path=p, now=1000)[0]
    assert top["outcome"] == "bad"


# --------------------------------------------------------------------------- advise
def test_advise_warns_off_a_repeated_failure(tmp_path):
    """The live case: a one-drill outpost was sent to feed forty furnaces, twice."""
    p = tmp_path / "m.jsonl"
    for _ in range(3):
        M.remember("build_lane", ctx(mine="tiny", block="big"), "bad", path=p, now=1000,
                   detail="delivered 4 ore to 40 furnaces")
    a = M.advise("build_lane", ctx(mine="tiny", block="big"), path=p, now=1000)
    assert a["verdict"] == "bad" and a["confidence"] > 0.5
    assert "4 ore" in a["why"]


def test_advise_approves_what_has_worked(tmp_path):
    p = tmp_path / "m.jsonl"
    for _ in range(3):
        M.remember("build_lane", ctx(mine="big", block="big"), "good", path=p, now=1000)
    assert M.advise("build_lane", ctx(mine="big", block="big"), path=p, now=1000)["verdict"] == "good"


def test_no_experience_is_unknown_not_permission(tmp_path):
    """Callers must read this as "no information". The bot has never done most things."""
    a = M.advise("build_lane", ctx(mine="big"), path=tmp_path / "m.jsonl")
    assert a["verdict"] == "unknown" and a["confidence"] == 0.0


def test_one_data_point_is_not_a_verdict(tmp_path):
    p = tmp_path / "m.jsonl"
    M.remember("build_lane", ctx(mine="tiny"), "bad", path=p, now=1000)
    assert M.advise("build_lane", ctx(mine="tiny"), path=p, now=1000)["verdict"] == "unknown"


def test_recent_failure_outweighs_an_old_success(tmp_path):
    p = tmp_path / "m.jsonl"
    now = 1_000_000.0
    for _ in range(3):
        M.remember("build_lane", ctx(mine="tiny"), "good", path=p, now=now - 20 * M.HALF_LIFE_S)
        M.remember("build_lane", ctx(mine="tiny"), "bad", path=p, now=now)
    assert M.advise("build_lane", ctx(mine="tiny"), path=p, now=now)["verdict"] == "bad"


# --------------------------------------------------------------------------- reflection
def test_reflection_promotes_a_pattern_not_an_incident(tmp_path):
    p = tmp_path / "m.jsonl"
    for _ in range(3):
        M.remember("build_lane", ctx(mine="tiny"), "bad", path=p, detail="four ore")
    out = M.reflect(p)
    assert out and out[0]["action"] == "build_lane" and out[0]["bad"] == 3


def test_two_failures_are_not_yet_a_pattern(tmp_path):
    p = tmp_path / "m.jsonl"
    for _ in range(2):
        M.remember("build_lane", ctx(mine="tiny"), "bad", path=p)
    assert M.reflect(p) == []


def test_a_mostly_successful_action_is_not_promoted(tmp_path):
    p = tmp_path / "m.jsonl"
    for _ in range(3):
        M.remember("build_lane", ctx(mine="tiny"), "bad", path=p)
    for _ in range(9):
        M.remember("build_lane", ctx(mine="tiny"), "good", path=p)
    assert M.reflect(p) == []


def test_summary_of_an_empty_memory(tmp_path):
    assert "no experience recorded yet" in M.summary(tmp_path / "nope.jsonl")
