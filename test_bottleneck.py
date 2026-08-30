#!/usr/bin/env python3
"""Offline unit tests for bottleneck.py — NO live server.

Run with either:
    python3 -m pytest test_bottleneck.py
    python3 test_bottleneck.py

Every test repoints bottleneck.HIST_PATH at its own tmp dir and installs a scripted fake
rcon.run that speaks the storage._bn chunked-read protocol (length, then :sub slices), so the
real sample() path — command build, chunking, rejoin, attribution — is exercised end to end
without touching the live game.

FakeRcon is deliberately local to this file (test_world_executor.py's copy hardcodes
storage._world) and its storage key is configurable.
"""
import json
import pathlib
import re
import shutil
import tempfile
import time
import traceback

import rcon
import bottleneck


# --------------------------------------------------------------------------- harness
class FakeRcon:
    """Scripted rcon.run: (substring, response) steps consumed in order, plus native handling of
    the chunked storage.<key> reads. A response may be a callable(cmd) -> str; return
    payload_len(obj) from one to serve a chunked scan."""
    def __init__(self, script=(), key="_bn"):
        self.script = list(script)
        self.calls = []
        self.payload = None
        self.slices = 0
        self._re = re.compile(r"storage\.%s:sub\((\d+),(\d+)\)" % re.escape(key))

    def payload_len(self, obj):
        self.payload = json.dumps(obj, separators=(",", ":"))
        return str(len(self.payload))

    def __call__(self, cmd, timeout=10.0):
        self.calls.append(cmd)
        m = self._re.search(cmd)
        if m:
            self.slices += 1
            i, j = int(m.group(1)), int(m.group(2))
            return self.payload[i - 1:j] + "\n"      # rcon.print appends a newline per response
        if not self.script:
            raise AssertionError("unexpected RCON call (script exhausted): %s" % cmd[:160])
        sub, resp = self.script.pop(0)
        assert sub in cmd, "expected %r in RCON cmd, got: %s" % (sub, cmd[:200])
        return resp(cmd) if callable(resp) else resp


class Ctx:
    def __init__(self, script=()):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bneck-test-"))
        self._orig = (bottleneck.HIST_PATH, bottleneck.MAX_SAMPLES, rcon.run)
        bottleneck.HIST_PATH = self.tmp / "bottleneck-history.json"
        self.fake = FakeRcon(script)
        rcon.run = self.fake

    def serve(self, rows, tot=None, stv=None, tick=100):
        """Script one scan whose payload is the given Lua-shape rows."""
        starved = stv if stv is not None else sum(
            1 for r in rows if r.get("s") in bottleneck.STARVED)
        payload = {"t": tick, "tot": tot if tot is not None else len(rows),
                   "stv": starved, "rows": rows}
        self.fake.script = [("storage._bn", lambda cmd: self.fake.payload_len(payload))]

    def close(self):
        bottleneck.HIST_PATH, bottleneck.MAX_SAMPLES, rcon.run = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)


def _with_ctx(fn):
    def wrapper():
        ctx = Ctx()
        try:
            fn(ctx)
        finally:
            ctx.close()
    wrapper.__name__ = fn.__name__
    return wrapper


def _hist(groups_per_sample, now=None, step=10.0, total=28):
    """Build a fake ring: groups_per_sample is a list (oldest first) of group lists."""
    now = time.time() if now is None else now
    n = len(groups_per_sample)
    return [{"tick": 1000 + i, "ts": now - (n - 1 - i) * step, "total": total,
             "starved": sum(g["n"] for g in gs), "groups": gs}
            for i, gs in enumerate(groups_per_sample)]


def _g(recipe, missing, n, deficit=1, status="no_ingredients", example=None):
    return {"recipe": recipe, "status": status, "missing": missing, "n": n,
            "deficit": deficit, "example": example or ["stone-furnace", -5, 6]}


# --------------------------------------------------------------------------- sample()
@_with_ctx
def test_sample_attributes_missing_ingredient(ctx):
    ctx.serve([
        {"n": "stone-furnace", "x": -5, "y": 6, "r": "iron-plate", "s": "no_ingredients",
         "g": [{"n": "iron-ore", "d": 12}]},
        {"n": "stone-furnace", "x": -3, "y": 6, "r": "iron-plate", "s": "no_ingredients",
         "g": [{"n": "iron-ore", "d": 9}]},
        {"n": "stone-furnace", "x": -1, "y": 6, "r": "iron-plate", "s": "working"},
    ])
    s = bottleneck.sample()
    assert s["total"] == 3 and s["starved"] == 2 and not s["truncated"]
    assert s["tick"] == 100 and s["ts"] > 0
    assert [m["missing"] for m in s["machines"]] == ["iron-ore", "iron-ore", None]
    assert len(s["groups"]) == 1
    g = s["groups"][0]
    assert g["recipe"] == "iron-plate" and g["missing"] == "iron-ore" and g["n"] == 2
    assert g["status"] == "no_ingredients" and g["deficit"] == 21
    assert g["example"] == ["stone-furnace", -5, 6]


@_with_ctx
def test_sample_picks_largest_deficit(ctx):
    # a multi-ingredient recipe: the biggest shortfall is the thing to go fix, and EQUAL
    # deficits resolve alphabetically (Lua pairs order is nondeterministic)
    ctx.serve([
        {"n": "assembling-machine-1", "x": 0, "y": 0, "r": "electronic-circuit",
         "s": "item_ingredient_shortage",
         "g": [{"n": "iron-plate", "d": 2}, {"n": "copper-cable", "d": 7}]},
        {"n": "assembling-machine-1", "x": 2, "y": 0, "r": "inserter",
         "s": "item_ingredient_shortage",
         "g": [{"n": "iron-plate", "d": 5}, {"n": "electronic-circuit", "d": 5},
               {"n": "iron-gear-wheel", "d": 5}]},
    ])
    s = bottleneck.sample()
    assert s["machines"][0]["missing"] == "copper-cable" and s["machines"][0]["deficit"] == 7
    assert s["machines"][1]["missing"] == "electronic-circuit"     # alphabetically first of the 5s
    # pure-function determinism, independent of the order the game hands us the ingredients
    gaps = [{"n": "iron-gear-wheel", "d": 5}, {"n": "electronic-circuit", "d": 5},
            {"n": "iron-plate", "d": 5}]
    assert bottleneck.pick_missing(gaps) == ("electronic-circuit", 5)
    assert bottleneck.pick_missing(list(reversed(gaps))) == ("electronic-circuit", 5)
    assert bottleneck.pick_missing([{"n": "coal", "d": 0}, {"n": "x", "d": -3}]) == (None, 0)
    assert bottleneck.pick_missing(None) == (None, 0)


@_with_ctx
def test_sample_handles_no_recipe_and_working(ctx):
    # a furnace whose previous_recipe fallback also came back empty, and a healthy machine:
    # both count toward total, neither may invent a group
    ctx.serve([
        {"n": "stone-furnace", "x": 4, "y": 4, "r": None, "s": "no_ingredients"},
        {"n": "stone-furnace", "x": 6, "y": 4, "r": "copper-plate", "s": "working"},
        {"n": "assembling-machine-1", "x": 8, "y": 4, "r": "iron-gear-wheel", "s": "full_output"},
    ])
    s = bottleneck.sample()
    assert s["total"] == 3 and s["starved"] == 1
    assert all(m["missing"] is None for m in s["machines"])
    assert s["groups"] == []
    assert bottleneck.format_report(bottleneck.report(600, hist=[])).startswith("bottleneck:")


@_with_ctx
def test_chunked_read_rejoins(ctx):
    # a payload bigger than CHUNK forces several :sub slices; the rejoin must parse (a dropped
    # .rstrip("\r\n") injects a control char at every boundary — GOTCHAS RCON protocol)
    rows = [{"n": "stone-furnace", "x": i, "y": 6, "r": "iron-plate", "s": "no_ingredients",
             "g": [{"n": "iron-ore", "d": 3}]} for i in range(60)]
    ctx.serve(rows)
    s = bottleneck.sample()
    assert len(ctx.fake.payload) > bottleneck.CHUNK, "payload too small to test chunking"
    assert ctx.fake.slices >= 2
    assert s["total"] == 60 and s["groups"][0]["n"] == 60 and s["groups"][0]["deficit"] == 180


def test_scan_lua_is_read_only():
    """The guard that protects the live server: the generated command must mutate NOTHING and
    must never register an event handler (that locks human players out)."""
    for bbox in (None, (-100, -100, 100, 100)):
        lua = bottleneck.scan_lua(bbox)
        for bad in ("create_entity", "destroy", ".remove{", ".insert{", "set_recipe",
                    "walking_state", "script.on_event", "script.on_nth_tick", "teleport",
                    "/c ", "clear_items_inside"):
            assert bad not in lua, "scan lua must be read-only, found %r" % bad
        cmd = "/sc " + lua
        assert len(cmd.encode()) < 3500, "command exceeds the per-/sc budget: %d" % len(cmd)
        # 2.1 correctness: the renamed input inventory, and the previous_recipe fallback
        assert "crafter_input" in lua and "furnace_source" not in lua
        assert "assembling_machine_input" not in lua
        assert "previous_recipe" in lua
        assert "ing.type=='fluid'" in lua            # never prototypes.item[<fluid>].type
        assert "storage._bn" in lua                  # our own key: never races _arch / _world
    assert "area={{-100,-100},{100,100}}" in bottleneck.scan_lua((-100, -100, 100, 100))


# --------------------------------------------------------------------------- ring buffer
@_with_ctx
def test_ring_buffer_trims(ctx):
    bottleneck.MAX_SAMPLES = 5
    for i in range(8):
        ctx.serve([{"n": "stone-furnace", "x": i, "y": 6, "r": "iron-plate",
                    "s": "no_ingredients", "g": [{"n": "iron-ore", "d": 4}]}], tick=1000 + i)
        bottleneck.record()
    hist = bottleneck.load_history()
    assert len(hist) == 5
    assert [h["tick"] for h in hist] == [1003, 1004, 1005, 1006, 1007]      # newest 5, in order
    leftovers = [p for p in bottleneck.HIST_PATH.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], "atomic write leaked temp files: %s" % leftovers


@_with_ctx
def test_record_returns_and_persists_groups_only(ctx):
    ctx.serve([{"n": "stone-furnace", "x": -5, "y": 6, "r": "iron-plate",
                "s": "no_ingredients", "g": [{"n": "iron-ore", "d": 12}]}])
    s = bottleneck.record()
    assert s["machines"] and s["groups"]                       # the caller gets the detail
    row = json.loads(bottleneck.HIST_PATH.read_text())[0]
    assert "machines" not in row, "history must stay O(recipes x samples), not O(machines)"
    assert set(row) == {"tick", "ts", "total", "starved", "groups"}
    assert row["groups"][0]["missing"] == "iron-ore"
    # a caller-supplied sample is recorded as-is (no second RCON scan)
    bottleneck.record(s)
    assert len(bottleneck.load_history()) == 2


# --------------------------------------------------------------------------- report()
@_with_ctx
def test_report_ranks_by_persistence_and_machines(ctx):
    # copper: 16 machines in 9 of 10 samples. iron: 2 machines in 3 of 10. Copper must win.
    samples = []
    for i in range(10):
        gs = []
        if i != 4:
            gs.append(_g("copper-plate", "copper-ore", 16, deficit=32))
        if i < 3:
            gs.append(_g("iron-plate", "iron-ore", 2, deficit=99))
        samples.append(gs)
    rows = bottleneck.report(600, hist=_hist(samples))
    assert [r["recipe"] for r in rows] == ["copper-plate", "iron-plate"]
    top = rows[0]
    assert top["starved_pct"] == 90 and top["samples"] == 9 and top["window_samples"] == 10
    assert top["machines"] == 16 and top["peak"] == 16 and top["deficit"] == 288
    assert top["text"] == ("recipe copper-plate was starved 90% of samples, "
                           "missing copper-ore (16 machines)")
    assert rows[1]["starved_pct"] == 30 and rows[1]["machines"] == 2
    assert "copper-plate" in bottleneck.format_report(rows)


@_with_ctx
def test_report_window_excludes_old(ctx):
    now = time.time()
    old = _hist([[_g("iron-plate", "iron-ore", 5)]] * 4, now=now - 5000, step=10.0)
    new = _hist([[_g("copper-plate", "copper-ore", 3)]] * 2, now=now, step=10.0)
    rows = bottleneck.report(600, hist=old + new)
    assert len(rows) == 1 and rows[0]["recipe"] == "copper-plate"
    assert rows[0]["window_samples"] == 2 and rows[0]["starved_pct"] == 100
    assert bottleneck.report(600, hist=old) == []          # everything stale -> nothing to blame
    assert bottleneck.report(100000, hist=old + new)[0]["window_samples"] == 6


@_with_ctx
def test_report_collapses_same_cause_across_statuses(ctx):
    """One sample may carry the SAME (recipe,missing) under two statuses — a machine bank split
    between no_ingredients and item_ingredient_shortage is the normal partially-fed case. That
    must count as ONE sample, or starved_pct exceeds 100% and `machines` reports one group
    instead of the bank (the 200%-of-1-sample headline)."""
    ec = lambda st, n, d: _g("electronic-circuit", "copper-cable", n, deficit=d, status=st)
    hist = _hist([[ec("no_ingredients", 4, 12), ec("item_ingredient_shortage", 2, 6)]] * 3,
                 total=10)
    rows = bottleneck.report(600, hist=hist)
    assert len(rows) == 1
    r = rows[0]
    assert r["samples"] == 3 and r["window_samples"] == 3      # 3 samples, not 6
    assert r["starved_pct"] == 100.0 and r["starved_pct"] <= 100.0
    assert r["machines"] == 6 and r["peak"] == 6               # the whole bank, not one group
    assert r["deficit"] == 54                                  # (12+6) x 3
    assert r["share"] == 6.0
    bottleneck.HIST_PATH.write_text(json.dumps(hist))
    h = bottleneck.top_cause(600)["headline"]
    assert "100% of the last 3 samples" in h and "6/10 machines" in h, h
    # a group with no recipe/missing is never a ranked cause and never fabricates a row
    assert bottleneck.report(600, hist=_hist([[{"recipe": None, "missing": None, "n": 3,
                                                "deficit": 1, "status": "no_ingredients"}]])) == []


@_with_ctx
def test_failed_scan_raises_never_records_a_healthy_lap(ctx):
    """An RCON blip or Lua error must NOT degrade into a 0-starved sample: record() would
    persist it as a healthy lap and every such lap dilutes starved_pct in report()."""
    for bad in ("", "   ", "Error: something died in the /sc", "0"):
        ctx.fake.script = [("storage._bn", bad)]
        try:
            bottleneck.sample()
        except RuntimeError:
            pass
        else:
            raise AssertionError("a %r scan response must raise, not report a healthy base" % bad)
    assert bottleneck.load_history() == [], "a failed scan must never reach the ring"


@_with_ctx
def test_empty_history(ctx):
    assert bottleneck.load_history() == []                 # file absent
    assert bottleneck.report(600) == [] and bottleneck.top_cause(600) is None
    bottleneck.HIST_PATH.write_text("[]")                  # empty ring
    assert bottleneck.load_history() == []
    assert bottleneck.report(600) == [] and bottleneck.top_cause(600) is None
    bottleneck.HIST_PATH.write_text('[{"tick":1,"ts":')    # truncated / corrupt write
    assert bottleneck.load_history() == []
    assert bottleneck.report(600) == [] and bottleneck.top_cause(600) is None
    assert bottleneck.format_report([]) == "bottleneck: no starved machines in window"


@_with_ctx
def test_top_cause_headline_quotable(ctx):
    hist = _hist([[_g("copper-plate", "copper-ore", 16, deficit=32,
                      example=["stone-furnace", -5, 6])]] * 10, total=28)
    bottleneck.HIST_PATH.write_text(json.dumps(hist))
    t = bottleneck.top_cause(600)
    h = t["headline"]
    assert "\n" not in h and "\r" not in h                 # one line: quotable in a prompt
    for frag in ("copper-plate", "copper-ore", "16/28 machines", "@(-5,6)", "stone-furnace"):
        assert frag in h, "headline missing %r: %s" % (frag, h)
    assert h.startswith("BOTTLENECK: ")
    assert t["recipe"] == "copper-plate" and t["missing"] == "copper-ore"


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
