#!/usr/bin/env python3
"""THE TRUCE MUST COVER EVERY PATH THAT WRITES.

    python3 -m pytest test_truce.py

2026-08-30, from the live log, in order:

    06:27:59  operator online - layout heals suspended
    06:28:11  triage -> actuator fix_lanes
    06:28:38  triage -> actuator fix_lanes

The truce suspended the two paths that check `operator_present()` - the read-only invariant
audit and the LAYOUT_ISSUES heals - and missed the one that actually writes: an LLM triage
verdict routed straight into an actuator. Belts were relaid under the operator's hands while he
was repairing them, for the third time in this project.

The second failure is why it was running at all: the model read "18 furnaces starved, 8 drills
blocked" and classified it "ore lane broken", firing fix_lanes every 15-20 seconds for hours.
All 28 furnaces were jammed at `full_output` with 3200 plates in each terminal chest and nothing
consuming them. A lane repair cannot drain a full chest.
"""
import sys

import rcon

_REAL = rcon.run


def _no_rcon(cmd, timeout=10.0):
    raise AssertionError("offline test issued RCON: %s" % str(cmd)[:160])


rcon.run = _no_rcon

import build_gates as G                                                   # noqa: E402
import controller as C                                                    # noqa: E402


def _st(**kw):
    st = {"counts": {}, "status": {}, "flows": {}, "networks": 1}
    st.update(kw)
    return st


class _Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append(a)
        return 1


def _patch(mod, name, value):
    old = getattr(mod, name)
    setattr(mod, name, value)
    return lambda: setattr(mod, name, old)


# ------------------------------------------------------------------ back-pressure discrimination
def test_a_jammed_smelter_block_is_not_a_broken_lane():
    """28 of 28 furnaces at full_output: the lane is fine, the DRAIN is missing."""
    undo = _patch(G, "sense", lambda *a, **k: _st(
        counts={"stone-furnace": 28},
        status={"stone-furnace": {"full_output": 28}}))
    try:
        assert C._backpressured() is True
    finally:
        undo()


def test_genuinely_starved_furnaces_are_still_a_lane_problem():
    """The guard must not swallow the real case it sits in front of - furnaces with no
    ingredients ARE a supply problem and fix_lanes is the right answer."""
    undo = _patch(G, "sense", lambda *a, **k: _st(
        counts={"stone-furnace": 28},
        status={"stone-furnace": {"no_ingredients": 26, "working": 2}}))
    try:
        assert C._backpressured() is False
    finally:
        undo()


def test_a_failed_census_does_not_read_as_back_pressure():
    """An unreadable world must never suppress a repair - a failed read is not an answer."""
    def boom(*a, **k):
        raise RuntimeError("rcon down")
    undo = _patch(G, "sense", boom)
    try:
        assert C._backpressured() is False
    finally:
        undo()


def test_fix_lanes_refuses_to_relay_belt_on_a_backpressured_base():
    """The actuator that rewrote his belts all night must decline, and SAY WHY."""
    spy = _Spy()
    undos = [_patch(C, "_backpressured", lambda: True),
             _patch(C.B, "scrub_mixed_ore", spy),
             _patch(C.B, "repair_belt_gaps", spy),
             _patch(C.B, "ensure_lanes", spy)]
    said = []
    undos.append(_patch(C.status, "log", lambda m: said.append(str(m))))
    try:
        assert C._fix_lanes() == 0
        assert not spy.calls, "no belt may be touched on a back-pressured base"
        assert any("WITHHELD" in s and "CONSUMER" in s for s in said), said
    finally:
        for u in undos:
            u()


def test_fix_lanes_still_repairs_a_genuinely_broken_lane():
    spy = _Spy()
    undos = [_patch(C, "_backpressured", lambda: False),
             _patch(C.B, "scrub_mixed_ore", spy),
             _patch(C.B, "repair_belt_gaps", spy),
             _patch(C.B, "ensure_lanes", spy)]
    try:
        C._fix_lanes()
        assert len(spy.calls) == 3, "a real lane repair must still run all three steps"
    finally:
        for u in undos:
            u()


# ------------------------------------------------------------------------------- the truce hole
def test_the_triage_actuator_path_checks_the_operator():
    """THE HOLE ITSELF. The write path must consult operator_present(); the two paths that
    already did are the read-only ones, which is precisely backwards."""
    import pathlib
    src = (pathlib.Path(C.__file__)).read_text()
    body = src[src.index("def _triage_worker"):]
    body = body[:body.index("except Exception")]
    assert "operator_present()" in body, \
        "an LLM verdict routes straight into a WRITE actuator - it must respect the truce"
    gate = body[body.index('act = v.get("actuator")'):]
    assert "operator_present()" in gate[:900], \
        "the check must gate the ACTUATOR, not merely appear somewhere in the worker"


def test_classification_is_not_suppressed_only_actuation():
    """Reading the world while he works is fine and useful; writing to it is not. Suppressing
    the whole worker would also blind the log and the lesson counter."""
    import pathlib
    src = (pathlib.Path(C.__file__)).read_text()
    head = src[src.index("if lap % 7 == 0"):]
    head = head[:head.index("def _triage_worker")]
    assert "operator_present()" not in head, \
        "gate the actuator, not the classifier - the verdict itself is read-only"


if __name__ == "__main__":
    import traceback
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   %s" % name)
            except Exception:
                fails += 1
                print("FAIL %s" % name)
                traceback.print_exc()
    sys.exit(1 if fails else 0)
