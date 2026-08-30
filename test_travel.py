#!/usr/bin/env python3
"""Offline unit tests for the travel stack (travel.py + the fle_lib travel chunks).

NO live server — same harness style as test_world_executor.py: a scripted fake
rcon.run (shared by travel and fle_tools, which both call through the rcon module
object) plus a fake clock so polling loops run instantly.

Run with either:
    python3 -m pytest test_travel.py
    python3 test_travel.py
"""
import math
import sys
import traceback
import types

import rcon
import fle_tools
import travel


# --------------------------------------------------------------------------- harness
class FakeRcon:
    """Scripted rcon.run: (substring, response) steps consumed in order (the
    test_world_executor.py pattern). A response may be a callable(cmd) -> str."""
    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []

    def __call__(self, cmd, timeout=10.0):
        self.calls.append(cmd)
        if not self.script:
            raise AssertionError("unexpected RCON call (script exhausted): %s" % cmd[:160])
        sub, resp = self.script.pop(0)
        assert sub in cmd, "expected %r in RCON cmd, got: %s" % (sub, cmd[:200])
        return resp(cmd) if callable(resp) else resp


class FakeTime:
    """Deterministic clock: time() advances only via sleep()."""
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Ctx:
    def __init__(self, script=()):
        self.fake = FakeRcon(script)
        self._orig = (rcon.run, travel.time)
        rcon.run = self.fake
        travel.time = FakeTime()

    def close(self):
        rcon.run, travel.time = self._orig
        sys.modules.pop("controller", None) if isinstance(
            sys.modules.get("controller"), types.SimpleNamespace) else None


def _with_ctx(fn):
    def wrapper():
        ctx = Ctx()
        try:
            fn(ctx)
        finally:
            ctx.close()
    wrapper.__name__ = fn.__name__
    return wrapper


VER = str(fle_tools.lib_version())

# ensure_handlers() when the in-game lib is already current: version probe + travel_on
_ENSURE = [("fle.VERSION", VER), ("fle.travel_on", "on")]


# --------------------------------------------------------------------------- chunks
def test_travel_chunks_split():
    """The new lua chunks exist, are ordered after the build chunks, and each fits
    the per-command RCON limit (split_lua raises on oversize, so a clean split IS
    the size check). Data may live in storage; functions must not."""
    chunks = fle_tools.split_lua(fle_tools.LIB.read_text())
    names = [c[0] for c in chunks]
    for want in ("travelreq", "travelq", "travelstep", "travelinit"):
        assert want in names, "missing chunk %s" % want
    assert names.index("travelreq") > names.index("api"), "travel chunks go after api"
    by = dict(chunks)
    assert "request_to_generate_chunks" in by["travelreq"], "corridor pre-gen missing"
    assert "force_generate_chunk_requests" in by["travelreq"]
    assert "on_script_path_request_finished" in by["travelinit"]
    assert "on_nth_tick(5" in by["travelinit"]
    assert "storage.fle " not in "".join(c for _, c in chunks), \
        "functions must live in the `fle` global, never storage"


# --------------------------------------------------------------------------- goal math
def test_retry_goal_math():
    """arturh85 displaced-goal pattern: original, then 8 tiles E, S, W, N (a 90-degree
    rotation per attempt)."""
    assert travel.retry_goals(10, -20) == [
        (10, -20), (18, -20), (10, -12), (2, -20), (10, -28)]
    assert travel.retry_goals(0, 0, step=4) == [
        (0, 0), (4, 0), (0, 4), (-4, 0), (0, -4)]
    assert len(set(travel.retry_goals(5, 5))) == 5, "goals must be distinct"


# --------------------------------------------------------------------------- flows
@_with_ctx
def test_goto_far_success(ctx):
    ctx.fake.script = _ENSURE + [
        ("storage.fle_travel=nil", ""),                       # pre-flight stop
        ("fle.travel_request(-38,15,3)", "7"),
        ("fle.travel_poll(7)", '{"status":"pending"}'),
        ("fle.travel_poll(7)", '{"status":"success","n":42}'),
        ("fle.travel_go(7)", "go"),
        ("fle.travel_status()",
         '{"active":true,"done":false,"partial":false,"wp":9,"total":42,"hops":0,"x":-20.0,"y":-10.0}'),
        ("fle.travel_status()",
         '{"active":false,"done":true,"partial":false,"wp":43,"total":42,"hops":1,"x":-37.2,"y":14.1}'),
        ("storage.fle_travel=nil", ""),                       # post-travel cleanup
    ]
    x, y, ok = travel.goto_far(-38, 15, radius=3)
    assert ok, "clean completion within radius must be ok"
    assert (x, y) == (-37.2, 14.1)
    assert not ctx.fake.script, "script fully consumed"


@_with_ctx
def test_goto_far_displaced_goal(ctx):
    """not_found on the original goal -> re-request with the goal displaced 8 tiles
    (first rotation: +x); success there widens the arrival allowance by the
    displacement."""
    ctx.fake.script = _ENSURE + [
        ("storage.fle_travel=nil", ""),
        ("fle.travel_request(-38,15,3)", "7"),
        ("fle.travel_poll(7)", '{"status":"not_found"}'),
        ("fle.travel_request(-30,15,3)", "8"),                # tx + 8, same ty
        ("fle.travel_poll(8)", '{"status":"success","n":30}'),
        ("fle.travel_go(8)", "go"),
        ("fle.travel_status()",
         '{"active":false,"done":true,"partial":false,"wp":31,"total":30,"hops":0,"x":-30.0,"y":15.0}'),
        ("storage.fle_travel=nil", ""),
    ]
    x, y, ok = travel.goto_far(-38, 15, radius=3)
    assert ok, "full walk to the displaced goal counts as arrival"
    assert math.hypot(x - (-30), y - 15) < 0.01
    assert not ctx.fake.script


@_with_ctx
def test_goto_far_partial_far_fails(ctx):
    """done+partial (stuck watchdog gave up on the LAST waypoint) far from the
    target -> ok=False."""
    ctx.fake.script = _ENSURE + [
        ("storage.fle_travel=nil", ""),
        ("fle.travel_request(0,0,3)", "9"),
        ("fle.travel_poll(9)", '{"status":"success","n":50}'),
        ("fle.travel_go(9)", "go"),
        ("fle.travel_status()",
         '{"active":false,"done":true,"partial":true,"wp":20,"total":50,"hops":2,"x":-60.0,"y":-45.0}'),
        ("storage.fle_travel=nil", ""),
    ]
    x, y, ok = travel.goto_far(0, 0, radius=3)
    assert not ok
    assert (x, y) == (-60.0, -45.0)


@_with_ctx
def test_goto_far_preempt(ctx):
    """controller.PREEMPT set -> travel aborts before requesting a path and reports
    failure (the builder must service the sev-0/1 issue)."""
    sys.modules["controller"] = types.SimpleNamespace(PREEMPT={"want": "boiler-dry"})
    try:
        ctx.fake.script = _ENSURE + [
            ("storage.fle_travel=nil", ""),
            ("storage.derpface", "12.5,-8.25"),               # _pos read
        ]
        x, y, ok = travel.goto_far(100, 100, radius=3)
        assert not ok and (x, y) == (12.5, -8.25)
    finally:
        del sys.modules["controller"]


@_with_ctx
def test_goto_far_all_goals_unreachable(ctx):
    """Every goal (original + 4 displaced) not_found -> give up with ok=False."""
    script = _ENSURE + [("storage.fle_travel=nil", "")]
    for i, (gx, gy) in enumerate(travel.retry_goals(0, 0)):
        script += [
            ("fle.travel_request(%d,%d,3)" % (gx, gy), str(20 + i)),
            ("fle.travel_poll(%d)" % (20 + i), '{"status":"not_found"}'),
        ]
    script += [("storage.derpface", "1.0,2.0")]               # final _pos read
    ctx.fake.script = script
    x, y, ok = travel.goto_far(0, 0, radius=3)
    assert not ok and (x, y) == (1.0, 2.0)
    assert not ctx.fake.script, "all five goals must be attempted"


# --------------------------------------------------------------------------- wiring
def test_walk_far_wiring():
    """autopilot dispatches long walks to travel.goto_far; the threshold is the
    documented 40 tiles; short hops keep the leg-walker."""
    import autopilot
    assert autopilot.walk_far is travel.goto_far
    assert autopilot.FAR_WALK == 40


@_with_ctx
def test_stop_clears_travel_queue(ctx):
    """autopilot.stop() must clear storage.fle_travel: the nth-tick walker re-sets
    walking_state every 5 ticks, so a bare walking_state=false wouldn't stick."""
    import autopilot
    ctx.fake.script = [("walking_state={walking=false}", "")]
    autopilot.stop()
    assert "storage.fle_travel=nil" in ctx.fake.calls[-1]


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
