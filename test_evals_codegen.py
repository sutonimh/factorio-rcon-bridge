#!/usr/bin/env python3
"""Offline tests for evals.py + codegen.py — no network, no RCON, no LLM, no repo files touched.

Run: python3 test_evals_codegen.py   (or: python3 -m pytest test_evals_codegen.py)
"""
import difflib
import json
import pathlib
import tempfile
import traceback

import codegen
import evals
import llm

FIXTURE = """\
def fuel(amount=300):
    # haul coal to the boiler
    if amount <= 0:
        return 0
    total = amount * 2
    return total


def other():
    return 1
"""


def _diff(old, new, path="fixture.py"):
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile="a/" + path, tofile="b/" + path))


# --------------------------------------------------------------------------- applier
def test_applier_roundtrip():
    new = FIXTURE.replace("    total = amount * 2",
                          "    if amount > 1000:\n"
                          "        raise ValueError('haul too large')\n"
                          "    total = amount * 2")
    files = codegen.parse_diff(_diff(FIXTURE, new))
    assert [f["path"] for f in files] == ["fixture.py"]
    assert codegen.apply_hunks(FIXTURE, files[0]["hunks"]) == new


def test_applier_multi_hunk_with_drift():
    # two edits far apart -> two hunks; then shift the file by two lines so the
    # second hunk's header line number is stale and the fuzz search must find it
    old = "\n".join("line%d = %d" % (i, i) for i in range(40)) + "\n"
    new = old.replace("line3 = 3", "line3 = 30").replace("line36 = 36", "line36 = 360")
    files = codegen.parse_diff(_diff(old, new))
    assert len(files[0]["hunks"]) == 2
    shifted_old = "# pad\n# pad\n" + old
    shifted_new = "# pad\n# pad\n" + new
    assert codegen.apply_hunks(shifted_old, files[0]["hunks"]) == shifted_new


def test_applier_rejects_bad_context():
    files = codegen.parse_diff(_diff(FIXTURE, FIXTURE.replace("total = amount * 2", "total = 9")))
    try:
        codegen.apply_hunks("completely = 'different'\n", files[0]["hunks"])
    except ValueError:
        pass
    else:
        raise AssertionError("mismatched context was applied")


def test_parse_rejects_garbage():
    for bad in ("", "hello there", "--- a/x.py\nno plus line"):
        try:
            codegen.parse_diff(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("parsed garbage: %r" % bad)


# --------------------------------------------------------------------------- validate_patch
def test_validate_patch_accepts_good_and_rejects_syntax_error():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "fixture.py").write_text(FIXTURE)
        good = _diff(FIXTURE, FIXTURE.replace("    if amount <= 0:",
                                              "    if amount <= 0 or amount > 5000:"))
        ok, why = codegen.validate_patch(good, root=root)
        assert ok, why
        bad = _diff(FIXTURE, FIXTURE.replace("    if amount <= 0:", "    if amount <= 0"))
        ok, why = codegen.validate_patch(bad, root=root)
        assert not ok and "compile" in why, why
        # real tree untouched
        assert (root / "fixture.py").read_text() == FIXTURE


def test_validate_patch_rejects_bad_targets():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "fixture.py").write_text(FIXTURE)
        cases = {
            "missing": _diff(FIXTURE, FIXTURE.replace("return 0", "return 1"), path="nope.py"),
            "escape": _diff(FIXTURE, FIXTURE.replace("return 0", "return 1"), path="../fixture.py"),
            "non-py": _diff("a\n", "b\n", path="notes.txt"),
        }
        for label, d in cases.items():
            ok, why = codegen.validate_patch(d, root=root)
            assert not ok, "%s diff was accepted" % label
    assert codegen.validate_patch("", root=td)[0] is False


# --------------------------------------------------------------------------- draft_fix (mocked)
def test_draft_fix_mocked_llm():
    lesson = {
        "id": 7, "count": 4, "phase": 0,
        "condition": "phase 0 build pass",
        "mistake": "fuel() hauled with no cap",
        "rule": "cap a single haul",
        "evidence": 'Traceback...\n  File "bootstrap.py", line 130, in fuel\nRuntimeError: x',
    }
    canned = _diff(FIXTURE, FIXTURE.replace("total = amount * 2", "total = min(amount, 500) * 2"))
    calls = {}
    real = llm.chat

    def fake_chat(messages, model=None, **kw):
        calls["model"] = model
        calls["think"] = kw.get("think")
        calls["prompt"] = messages[-1]["content"]
        return "Here you go:\n```diff\n" + canned + "```\n"
    llm.chat = fake_chat
    try:
        got = codegen.draft_fix(lesson)
    finally:
        llm.chat = real
    assert calls["model"] == llm.CODER and calls["think"] is False
    assert "fuel" in calls["prompt"]                       # gathered the named function's source
    assert got is not None and got.startswith("--- ")      # fences/prose stripped
    assert "min(amount, 500)" in got


def test_draft_fix_no_context_returns_none():
    real = llm.chat
    llm.chat = lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM called without context"))
    try:
        assert codegen.draft_fix({"condition": "x", "mistake": "y", "rule": "z",
                                  "evidence": "nothing recognizable"}) is None
    finally:
        llm.chat = real


def test_candidate_names():
    names = codegen._candidate_names({
        "evidence": 'File "planner.py", line 9, in phase0\nfailed in mine_outpost_v2(iron)',
        "mistake": "power() returned None", "condition": "", "rule": ""})
    assert names[0] == "phase0"                 # traceback frames ranked first
    assert "mine_outpost_v2" in names and "power" in names


# --------------------------------------------------------------------------- evals.compare
def _result(name="burner", **after):
    base_before = {"tick": 0, "iron_plates": 100, "drills": 4, "research_pct": 10}
    base_after = {"tick": 3600, "iron_plates": 400, "drills": 4, "research_pct": 25}
    base_after.update(after)
    return {"name": name, "save": "eval-" + name, "laps": 30, "duration_s": 60.0,
            "timed_out": False, "before": base_before, "after": base_after}


def test_compare_creates_then_passes_then_regresses():
    with tempfile.TemporaryDirectory() as td:
        bf = pathlib.Path(td) / "evals-baseline.json"
        v = evals.compare(bf, _result())
        assert v["status"] == "baseline-created" and bf.exists()
        stored = json.loads(bf.read_text())["burner"]
        assert stored["iron_plates"] == 300     # cumulative counter -> per-run gain
        assert stored["drills"] == 4            # gauge -> absolute
        # same performance -> ok
        assert evals.compare(bf, _result())["status"] == "ok"
        # within tolerance -> still ok
        assert evals.compare(bf, _result(iron_plates=380))["status"] == "ok"
        # beyond tolerance -> regressed
        v = evals.compare(bf, _result(iron_plates=200))
        assert v["status"] == "regressed" and any("iron_plates" in r for r in v["regressions"])
        # a passing run never moves the bar; update_baseline does
        assert json.loads(bf.read_text())["burner"]["iron_plates"] == 300
        evals.update_baseline(_result(iron_plates=700), baseline_file=bf)
        assert json.loads(bf.read_text())["burner"]["iron_plates"] == 600


def test_scenarios_shape():
    assert [s["name"] for s in evals.SCENARIOS] == ["burner", "bus", "robot", "rail"]
    for s in evals.SCENARIOS:
        assert s["save"].startswith("eval-") and s["timeout_s"] > 0 and s["laps"] > 0
        assert s["score_lua"].startswith("/sc ") and "rcon.print" in s["score_lua"]
    assert evals.scenario("eval-bus")["name"] == "bus"
    try:
        evals.scenario("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("unknown scenario did not raise")


# --------------------------------------------------------------------------- plain runner
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print("PASS %s" % t.__name__)
        except Exception:
            failed += 1
            print("FAIL %s" % t.__name__)
            traceback.print_exc()
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    raise SystemExit(1 if failed else 0)
