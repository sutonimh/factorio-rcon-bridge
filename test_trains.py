#!/usr/bin/env python3
"""Offline unit tests for trains.py — NO live server.

Run with either:
    python3 -m pytest test_trains.py
    python3 test_trains.py

Same harness style as test_world_executor.py: a scripted fake rcon.run installed over
rcon.run, with native handling of the storage._world chunked-read protocol (trains.py
reads go through world._chunked). Asserts the generated schedule-building Lua is correct
(interrupt shapes, clear-before-rebuild, quoting), group/stop naming is stable, and the
stop setter targets exactly the named stops.
"""
import json
import re
import traceback

import rcon
import trains
import world


# --------------------------------------------------------------------------- harness
class FakeRcon:
    """Scripted rcon.run: a list of (substring, response) steps consumed in order, plus
    native handling of the chunked storage._world reads. A response may be a callable(cmd)
    -> str; return payload_len(obj) to serve a chunked read."""
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
        self._orig = rcon.run
        self.fake = FakeRcon(script)
        rcon.run = self.fake

    def close(self):
        rcon.run = self._orig


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


# --------------------------------------------------------------------------- naming
@_with_ctx
def test_stop_names_stable(ctx):
    # blueprints carry these literal stop names; they must never drift
    assert trains.stop_names("iron-ore") == ("iron-ore pickup", "iron-ore dropoff")
    assert trains.stop_names("copper-ore") == ("copper-ore pickup", "copper-ore dropoff")
    assert trains.REFUEL_STOP == "refuel"
    assert ctx.fake.calls == []                          # pure naming, no RCON


@_with_ctx
def test_bad_item_rejected_before_rcon(ctx):
    for bad in ("Iron Ore", "iron_ore", "", None, "x'); game.print('pwn"):
        try:
            trains.stop_names(bad)
            raise AssertionError("accepted bad item %r" % (bad,))
        except ValueError:
            pass
        try:
            trains.create_group_schedule("g", bad)
            raise AssertionError("accepted bad item %r" % (bad,))
        except ValueError:
            pass
    assert ctx.fake.calls == []                          # rejected before any RCON write


# --------------------------------------------------------------------------- group schedule
@_with_ctx
def test_create_group_schedule_lua(ctx):
    seen = {}

    def capture(cmd):
        seen["lua"] = cmd
        return "GROUP iron shuttle interrupts=3"

    ctx.fake.script = [("add_interrupt", capture)]
    out = trains.create_group_schedule("iron shuttle", "iron-ore", train_id=7)
    assert out == {"group": "iron shuttle", "interrupts": 3}
    lua = seen["lua"]
    # seed train by id, joins the group, then the schedule is rebuilt deterministically
    assert "get_train_by_id(7)" in lua
    assert "t.group='iron shuttle'" in lua
    assert lua.index("t.group=") < lua.index("clear_interrupts()")
    assert lua.index("clear_interrupts()") < lua.index("add_interrupt")
    assert "remove_record{schedule_index=1}" in lua      # records cleared: interrupt-only
    # pickup: cargo empty -> '<item> pickup', wait until full
    assert ("sch.add_interrupt{name='iron-ore pickup',conditions={{type='empty'}},"
            "targets={{station='iron-ore pickup',wait_conditions={{type='full'}}}}}") in lua
    # dropoff: cargo full -> '<item> dropoff', wait until empty
    assert ("sch.add_interrupt{name='iron-ore dropoff',conditions={{type='full'}},"
            "targets={{station='iron-ore dropoff',wait_conditions={{type='empty'}}}}}") in lua
    # refuel: low fuel -> 'refuel', wait fuel_full, allowed to fire inside other interrupts
    assert "name='refuel',inside_interrupt=true" in lua
    assert ("conditions={{type='fuel_item_count_all',condition={comparator='<',"
            "constant=%d,first_signal={type='item',name='coal'}}}}" % trains.FUEL_LOW) in lua
    assert "targets={{station='refuel',wait_conditions={{type='fuel_full'}}}}" in lua


@_with_ctx
def test_create_group_schedule_no_refuel_and_seedless(ctx):
    seen = {}

    def capture(cmd):
        seen["lua"] = cmd
        return "GROUP copper shuttle interrupts=2"

    ctx.fake.script = [("add_interrupt", capture)]
    out = trains.create_group_schedule("copper shuttle", "copper-ore", refuel=False)
    assert out["interrupts"] == 2
    lua = seen["lua"]
    assert "get_train_by_id" not in lua                  # no train_id: seed from the group
    assert "g=='copper shuttle'" in lua.replace("g == ", "g==")
    assert "refuel" not in lua and "fuel_item_count_all" not in lua


@_with_ctx
def test_create_group_schedule_no_seed_train_fails(ctx):
    ctx.fake.script = [("add_interrupt", "NO_TRAIN")]
    try:
        trains.create_group_schedule("stone shuttle", "stone")
        raise AssertionError("NO_TRAIN not raised")
    except trains.TrainsError as e:
        assert "no seed train" in str(e)


@_with_ctx
def test_group_name_quoting(ctx):
    # a group name with a quote must be escaped, never break out of the Lua literal
    def capture(cmd):
        assert "\\'" in cmd
        return "GROUP it's ore interrupts=3"

    ctx.fake.script = [("add_interrupt", capture)]
    out = trains.create_group_schedule("it's ore", "iron-ore", train_id=1)
    assert out["group"] == "it's ore"


# --------------------------------------------------------------------------- assign
@_with_ctx
def test_assign_train_verified(ctx):
    seen = {}

    def capture(cmd):
        seen["lua"] = cmd
        return "OK iron shuttle"

    ctx.fake.script = [("get_train_by_id(42)", capture)]
    assert trains.assign_train(42, "iron shuttle") == "iron shuttle"
    assert "t.group='iron shuttle'" in seen["lua"]
    assert "get_schedule().group" in seen["lua"]         # verify-after-write readback


@_with_ctx
def test_assign_train_missing_or_unstuck(ctx):
    ctx.fake.script = [("get_train_by_id(9)", "NO_TRAIN")]
    try:
        trains.assign_train(9, "g")
        raise AssertionError("missing train not raised")
    except trains.TrainsError as e:
        assert "no train" in str(e)
    ctx.fake.script = [("get_train_by_id(10)", "OK other-group")]
    try:
        trains.assign_train(10, "g")
        raise AssertionError("bad readback not raised")
    except trains.TrainsError as e:
        assert "did not stick" in str(e)


# --------------------------------------------------------------------------- reads
@_with_ctx
def test_list_trains_chunked(ctx):
    def serve(cmd):
        assert "get_trains{}" in cmd and "defines.train_state" in cmd
        return ctx.fake.payload_len([
            {"id": 1, "group": "iron shuttle", "state": "wait_station",
             "station": "iron-ore pickup", "cargo": {"iron-ore": 4000}},
            {"id": 2, "group": "", "state": "no_schedule", "station": "", "cargo": []},
        ])

    ctx.fake.script = [("rcon.print(#storage.", serve)]
    ts = trains.list_trains()
    assert [t["id"] for t in ts] == [1, 2]
    assert ts[0]["cargo"] == {"iron-ore": 4000}
    assert ts[1]["cargo"] == {}                          # empty lua table normalized


@_with_ctx
def test_list_trains_empty(ctx):
    ctx.fake.script = [("get_trains{}", "0")]            # length 0 -> no slice reads
    assert trains.list_trains() == []


@_with_ctx
def test_train_status(ctx):
    def serve(cmd):
        assert "get_train_by_id(5)" in cmd
        return ctx.fake.payload_len(
            {"id": 5, "group": "iron shuttle", "state": "on_the_path",
             "station": "", "cargo": {"iron-ore": 1200}, "current": 1,
             "records": [], "interrupts": ["iron-ore pickup", "iron-ore dropoff", "refuel"]})

    ctx.fake.script = [("rcon.print(#storage.", serve)]
    st = trains.train_status(5)
    assert st["group"] == "iron shuttle" and st["state"] == "on_the_path"
    assert st["interrupts"] == ["iron-ore pickup", "iron-ore dropoff", "refuel"]
    assert st["records"] == [] and st["cargo"] == {"iron-ore": 1200}


@_with_ctx
def test_train_status_missing(ctx):
    ctx.fake.script = [("rcon.print(#storage.",
                        lambda cmd: ctx.fake.payload_len({"err": "NO_TRAIN"}))]
    try:
        trains.train_status(99)
        raise AssertionError("missing train not raised")
    except trains.TrainsError as e:
        assert "99" in str(e)


# --------------------------------------------------------------------------- stops
@_with_ctx
def test_set_stop_limit_and_priority(ctx):
    seen = {}

    def capture(cmd):
        seen["lua"] = cmd
        return "3"

    ctx.fake.script = [("type='train-stop'", capture)]
    assert trains.set_stop("iron-ore pickup", limit=2, priority=100) == 3
    lua = seen["lua"]
    assert "e.backer_name=='iron-ore pickup'" in lua     # targets exactly the named stops
    assert "e.trains_limit=2;" in lua
    assert "e.train_stop_priority=100;" in lua
    assert "area=" not in lua                            # name-scoped, never area writes


@_with_ctx
def test_set_stop_partial_setters(ctx):
    def only_limit(cmd):
        assert "trains_limit=1;" in cmd and "train_stop_priority" not in cmd
        return "1"

    def only_priority(cmd):
        assert "train_stop_priority=200;" in cmd and "trains_limit" not in cmd
        return "2"

    ctx.fake.script = [("train-stop", only_limit), ("train-stop", only_priority)]
    assert trains.set_stop("refuel", limit=1) == 1
    assert trains.set_stop("refuel", priority=200) == 2


@_with_ctx
def test_set_stop_validation(ctx):
    for kwargs in ({}, {"limit": -1}, {"priority": 256}, {"priority": -5}):
        try:
            trains.set_stop("x", **kwargs)
            raise AssertionError("accepted bad args %r" % (kwargs,))
        except ValueError:
            pass
    assert ctx.fake.calls == []                          # rejected before any RCON write


@_with_ctx
def test_set_stop_no_match_fails(ctx):
    ctx.fake.script = [("train-stop", "0")]
    try:
        trains.set_stop("nope pickup", limit=1)
        raise AssertionError("zero-match not raised")
    except trains.TrainsError as e:
        assert "nope pickup" in str(e)


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
