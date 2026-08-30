#!/usr/bin/env python3
"""Offline unit tests for world.py + executor.py — NO live server.

Run with either:
    python3 -m pytest test_world_executor.py
    python3 test_world_executor.py

Every test builds its own tmp dir (world-db.json / orders.json repointed there) and
installs a scripted fake rcon.run, so no test ever touches the live game. The fake
understands the storage._world chunked-read protocol (length then :sub slices) so
scan_area/scan_tiles/reconcile are exercised for real.
"""
import json
import pathlib
import re
import shutil
import tempfile
import traceback

import rcon
import world
import executor


# --------------------------------------------------------------------------- harness
class FakeRcon:
    """Scripted rcon.run: a list of (substring, response) steps consumed in order, plus
    native handling of the chunked storage._world reads. A response may be a callable(cmd)
    -> str; return _payload(json) to serve a chunked scan."""
    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []
        self.payload = None

    def payload_len(self, obj):
        self.payload = json.dumps(obj, separators=(",", ":"))
        return str(len(self.payload))

    def __call__(self, cmd, timeout=10.0):
        self.calls.append(cmd)
        # the buffer key is minted per read (rcon.read_chunked), so match ANY scratch
        m = re.search(r"storage\._\w+:sub\((\d+),(\d+)\)", cmd)
        if m:
            i, j = int(m.group(1)), int(m.group(2))
            return self.payload[i - 1:j] + "\n"
        if re.search(r"storage\._\w+=nil", cmd):
            return ""                      # read_chunked clears its scratch key in a finally
        if not self.script:
            raise AssertionError("unexpected RCON call (script exhausted): %s" % cmd[:160])
        sub, resp = self.script.pop(0)
        assert sub in cmd, "expected %r in RCON cmd, got: %s" % (sub, cmd[:200])
        return resp(cmd) if callable(resp) else resp


class Ctx:
    def __init__(self, script=()):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="wexec-test-"))
        self._orig = (world.DB_PATH, executor.DB_PATH, rcon.run)
        world.DB_PATH = self.tmp / "world-db.json"
        executor.DB_PATH = self.tmp / "orders.json"
        self.fake = FakeRcon(script)
        rcon.run = self.fake

    def close(self):
        world.DB_PATH, executor.DB_PATH, rcon.run = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)


def _with_ctx(fn):
    """Decorator: run the test inside a fresh Ctx (passed as the only arg), always restore."""
    def wrapper():
        ctx = Ctx()
        try:
            fn(ctx)
        finally:
            ctx.close()
    wrapper.__name__ = fn.__name__
    return wrapper


# --------------------------------------------------------------------------- world tests
@_with_ctx
def test_registry_roundtrip(ctx):
    uids = world.register(
        [{"name": "stone-furnace", "tile_pos": (5, 7)},
         {"name": "transport-belt", "x": 6, "y": 8, "direction": 4},
         {"name": "boiler", "tile_pos": (10, -2), "direction": 8}],
        role="smelter", phase=0, order_id="o1")
    assert len(uids) == 3 and len(set(uids)) == 3
    # persisted + reloadable (fresh read from disk on every query)
    recs = world.query(role="smelter")
    assert len(recs) == 3
    belt = next(r for r in recs if r["name"] == "transport-belt")
    assert belt["tile_pos"] == [6, 8] and belt["direction"] == 4
    assert belt["phase"] == 0 and belt["order_id"] == "o1" and not belt["missing"]
    assert belt["created_ts"] > 0
    assert world.unregister([uids[0]]) == 1
    assert len(world.query(role="smelter")) == 2
    assert world.unregister(["nope"]) == 0


@_with_ctx
def test_query_and_bounds(ctx):
    world.register([{"name": "burner-mining-drill", "tile_pos": (0, 0)},
                    {"name": "burner-mining-drill", "tile_pos": (10, 4)}],
                   role="mine", phase=0, order_id="oA")
    world.register([{"name": "stone-furnace", "tile_pos": (50, 50)}],
                   role="smelter", phase=1, order_id="oB")
    assert len(world.query()) == 3
    assert len(world.query(role="mine")) == 2
    assert len(world.query(phase=1)) == 1
    assert len(world.query(order_id="oA")) == 2
    assert len(world.query(bbox=(-1, -1, 11, 5))) == 2
    assert world.bounds(role="mine") == (0, 0, 10, 4)
    assert world.bounds(role="rail") is None
    assert world.bounds() == (0, 0, 50, 50)


@_with_ctx
def test_atomic_persistence(ctx):
    for i in range(20):
        world.register([{"name": "pipe", "tile_pos": (i, 0)}], "power", 0, "o%d" % i)
    data = json.loads(world.DB_PATH.read_text())    # always a complete, valid JSON file
    assert len(data["entities"]) == 20 and data["seq"] == 20
    leftovers = [p for p in world.DB_PATH.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], "atomic write leaked temp files: %s" % leftovers


@_with_ctx
def test_patches(ctx):
    world.record_patch("iron-ore", -75, 17, 1055.0)
    world.record_patch("copper-ore", 3, 40, 1054.0)
    world.record_patch("iron-ore", -75, 17, 900.0)      # re-record updates in place
    ps = world.patches("iron-ore")
    assert len(ps) == 1 and ps[0]["per_tile_density"] == 900.0
    assert world.patches("coal") == []


@_with_ctx
def test_reconcile_marks_missing_then_recovers(ctx):
    uids = world.register([{"name": "boiler", "tile_pos": (10, -2)},
                           {"name": "steam-engine", "tile_pos": (10, -6)}],
                          "power", 0, "o1")
    # live scan sees only the boiler -> engine flagged missing (never deleted)
    ctx.fake.script = [("rcon.print(#storage.", lambda cmd: ctx.fake.payload_len(
        [{"n": "boiler", "x": 10, "y": -2, "d": 0}]))]
    res = world.reconcile()
    assert res["checked"] == 2 and res["missing"] == [uids[1]] and res["recovered"] == []
    assert len(world.query()) == 2                       # flagged, not deleted
    assert not world.query(include_missing=False)[0].get("missing")
    # next scan sees both -> engine recovered
    ctx.fake.script = [("rcon.print(#storage.", lambda cmd: ctx.fake.payload_len(
        [{"n": "boiler", "x": 10, "y": -2, "d": 0},
         {"n": "steam-engine", "x": 10, "y": -6, "d": 0}]))]
    res = world.reconcile()
    assert res["missing"] == [] and res["recovered"] == [uids[1]]


# --------------------------------------------------------------------------- executor tests
@_with_ctx
def test_executor_noop_lifecycle(ctx):
    a = executor.submit({"kind": "noop", "role": "grid", "phase": 2})
    b = executor.submit({"kind": "noop"})
    assert a == "o1" and b == "o2"
    o = executor.run_next()
    assert o["id"] == a and o["status"] == "done" and o["attempts"] == 1 and o["error"] is None
    st = executor.status()
    assert st["counts"] == {"done": 1, "pending": 1} and st["total"] == 2
    # queue persisted: reload straight from disk
    data = json.loads(executor.DB_PATH.read_text())
    assert [x["status"] for x in data["orders"]] == ["done", "pending"]
    assert executor.run_all() and executor.status()["counts"] == {"done": 2}
    assert executor.run_next() is None                   # empty queue -> None, no spin


@_with_ctx
def test_executor_bad_kind_rejected(ctx):
    try:
        executor.submit({"kind": "stamp"})
        raise AssertionError("bad kind accepted")
    except ValueError:
        pass


@_with_ctx
def test_executor_retry_then_success(ctx):
    oid = executor.submit({"kind": "noop", "args": {"fail_times": 2}})
    o = executor.run(oid)
    assert o["status"] == "done" and o["attempts"] == 3 and o["error"] is None


@_with_ctx
def test_executor_bounded_failure(ctx):
    oid = executor.submit({"kind": "noop", "args": {"fail": True, "diag": "boom"}})
    o = executor.run(oid)
    assert o["status"] == "failed" and o["attempts"] == executor.MAX_ATTEMPTS
    assert o["error"] == "boom"
    assert executor.status()["failed"] == [{"id": oid, "kind": "noop", "error": "boom"}]
    # a failed order doesn't block the queue
    executor.submit({"kind": "noop"})
    assert executor.run_next()["status"] == "done"


@_with_ctx
def test_executor_run_all_max_ops(ctx):
    for _ in range(4):
        executor.submit({"kind": "noop"})
    assert len(executor.run_all(max_ops=2)) == 2
    assert executor.status()["counts"] == {"done": 2, "pending": 2}


@_with_ctx
def test_place_order_registers(ctx):
    # sequence: clear_area -> place -> post-condition existence read
    ctx.fake.script = [
        ("find_entities_filtered", "0|0"),                       # clear_area: 0 removed, 0 cliffs
        ("create_entity", "BUILT stone-furnace @(6,8)\n"),       # A.place (2x2 -> integer center)
        ("find_entities_filtered{name='stone-furnace'", "ok,6,8,0"),
    ]
    oid = executor.submit({"kind": "place", "role": "smelter", "phase": 1,
                           "args": {"name": "stone-furnace", "tile_x": 5, "tile_y": 7}})
    o = executor.run(oid)
    assert o["status"] == "done", o["error"]
    recs = world.query(order_id=oid)
    assert len(recs) == 1
    assert recs[0]["name"] == "stone-furnace" and recs[0]["tile_pos"] == [6, 8]
    assert recs[0]["role"] == "smelter" and recs[0]["phase"] == 1


@_with_ctx
def test_place_cliff_aborts_with_diagnostic(ctx):
    # GOTCHAS clearspace law: a cliff in the clear radius = move the site, never build.
    ctx.fake.script = [("find_entities_filtered", "0|2")] * executor.MAX_ATTEMPTS
    oid = executor.submit({"kind": "place",
                           "args": {"name": "boiler", "tile_x": 0, "tile_y": 0}})
    o = executor.run(oid)
    assert o["status"] == "failed" and "CLIFF" in o["error"]
    assert world.query(order_id=oid) == []               # nothing registered on failure


@_with_ctx
def test_belt_path_order(ctx):
    def serve_belts(cmd):
        return ctx.fake.payload_len([
            {"n": "transport-belt", "x": 0, "y": 0, "d": 4},
            {"n": "transport-belt", "x": 1, "y": 0, "d": 4},
            {"n": "transport-belt", "x": 2, "y": 0, "d": 4},
        ])
    ctx.fake.script = [
        ("gmatch", "0"),                                 # lay_belt_path: 0 unbridged gaps
        ("rcon.print(#storage.", serve_belts),                 # scan_tiles verify+collect
    ]
    oid = executor.submit({"kind": "belt_path", "role": "bus",
                           "args": {"waypoints": [[0, 0], [2, 0]]}})
    o = executor.run(oid)
    assert o["status"] == "done", o["error"]
    recs = world.query(order_id=oid)
    assert len(recs) == 3 and all(r["name"] == "transport-belt" for r in recs)


@_with_ctx
def test_belt_path_gap_fails(ctx):
    ctx.fake.script = [("gmatch", "2")] * executor.MAX_ATTEMPTS
    oid = executor.submit({"kind": "belt_path", "args": {"waypoints": [[0, 0], [9, 0]]}})
    o = executor.run(oid)
    assert o["status"] == "failed" and "2 unbridged gaps" in o["error"]


@_with_ctx
def test_research_order_verified(ctx):
    ctx.fake.script = [
        ("research_trigger", "OK"),                      # precheck
        ("add_research", ""),                            # write (one tech at a time)
        ("current_research", "queued"),                  # verify-after-write
    ]
    o = executor.run(executor.submit({"kind": "research", "args": {"tech": "automation-2"}}))
    assert o["status"] == "done", o["error"]


@_with_ctx
def test_research_trigger_tech_fails_clearly(ctx):
    ctx.fake.script = [("research_trigger", "TRIGGER")] * executor.MAX_ATTEMPTS
    o = executor.run(executor.submit({"kind": "research", "args": {"tech": "oil-processing"}}))
    assert o["status"] == "failed"
    assert "TRIGGER" in o["error"] and "cannot be queued" in o["error"]


@_with_ctx
def test_research_already_done_is_noop_success(ctx):
    ctx.fake.script = [("research_trigger", "DONE")]
    o = executor.run(executor.submit({"kind": "research", "args": {"tech": "automation"}}))
    assert o["status"] == "done"


@_with_ctx
def test_research_not_queued_retries_then_fails(ctx):
    ctx.fake.script = [("research_trigger", "OK"), ("add_research", ""),
                       ("current_research", "notqueued")] * executor.MAX_ATTEMPTS
    o = executor.run(executor.submit({"kind": "research", "args": {"tech": "logistics-2"}}))
    assert o["status"] == "failed" and "did not queue" in o["error"]


@_with_ctx
def test_decon_registry_surgical(ctx):
    uids = world.register([{"name": "stone-furnace", "tile_pos": (5, 7)},
                           {"name": "transport-belt", "tile_pos": (6, 8)}],
                          "smelter", 0, "oX")
    world.register([{"name": "boiler", "tile_pos": (10, -2)}], "power", 0, "oY")
    seen = {}

    def destroy(cmd):
        # the teardown lua must carry EXACTLY the scoped entities, per-entity (no area calls)
        seen["spec"] = cmd
        assert "area=" not in cmd, "decon must never be area-based"
        return "2"

    ctx.fake.script = [("e.destroy()", destroy), ("rcon.print(n)", "0")]
    o = executor.run(executor.submit({"kind": "decon_registry",
                                      "args": {"role": "smelter"}}))
    assert o["status"] == "done", o["error"]
    assert "stone-furnace,5,7" in seen["spec"] and "transport-belt,6,8" in seen["spec"]
    assert "boiler" not in seen["spec"]                  # out of scope: untouched
    assert world.query(role="smelter") == []             # scoped uids unregistered
    assert len(world.query(role="power")) == 1
    # the generated lua guards every insert-derived remove (GOTCHAS remove{count=0} law)
    assert "if g>0 then" in seen["spec"]
    assert uids  # silence lint


@_with_ctx
def test_decon_leftover_fails(ctx):
    world.register([{"name": "pipe", "tile_pos": (0, 0)}], "power", 0, "oZ")
    ctx.fake.script = [("e.destroy()", "0"), ("rcon.print(n)", "1")] * executor.MAX_ATTEMPTS
    o = executor.run(executor.submit({"kind": "decon_registry", "args": {"role": "power"}}))
    assert o["status"] == "failed" and "still standing" in o["error"] or "left" in o["error"]
    assert len(world.query(role="power")) == 1           # nothing unregistered on failure


@_with_ctx
def test_decon_requires_role(ctx):
    o = executor.run(executor.submit({"kind": "decon_registry", "args": {}}))
    assert o["status"] == "failed" and "role" in o["error"]


@_with_ctx
def test_builders_registered(ctx):
    import builds_v2                                     # noqa: F401  (registers on import)
    for name in ("mine_outpost", "power_plant", "smelter_array"):
        assert name in executor.BUILDERS, name
    o = executor.run(executor.submit({"kind": "build", "args": {"fn": "nope"}}))
    assert o["status"] == "failed" and "no registered builder" in o["error"]


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
