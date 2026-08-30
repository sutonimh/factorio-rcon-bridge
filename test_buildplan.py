#!/usr/bin/env python3
"""Offline unit tests for buildplan.py — NO live server, NO real ledger.

Run with either:
    python3 test_buildplan.py
    python3 -m pytest test_buildplan.py

Every test gets a fresh tmp dir (PLANS_DIR/DIRTY_PATH repointed there), a scripted fake
rcon.run (the FakeRcon from test_world_executor.py, which speaks the storage._world chunked
read protocol so refresh_dirty exercises world.scan_area for real), and monkeypatched
bootstrap wrappers — so no test can touch built-tiles.json, protected-tiles.json, or the
live game.
"""
import json
import pathlib
import re
import shutil
import tempfile
import traceback

import buildplan as B
import rcon
import world


# --------------------------------------------------------------------------- harness
class FakeRcon:
    """Scripted rcon.run: (substring, response) steps consumed in order, plus native handling
    of the chunked storage._world reads. A response may be a callable(cmd) -> str."""
    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []
        self.payload = None

    def payload_len(self, obj):
        self.payload = json.dumps(obj, separators=(",", ":"))
        return str(len(self.payload))

    def scan(self, entities):
        """A response serving a scan_area/scan_tiles result: [{n,x,y,d}]."""
        return lambda cmd: self.payload_len(entities)

    def __call__(self, cmd, timeout=10.0):
        self.calls.append(cmd)
        m = re.search(r"storage\._world:sub\((\d+),(\d+)\)", cmd)
        if m:
            i, j = int(m.group(1)), int(m.group(2))
            return self.payload[i - 1:j] + "\n"
        if not self.script:
            raise AssertionError("unexpected RCON call (script exhausted): %s" % cmd[:160])
        sub, resp = self.script.pop(0)
        assert sub in cmd, "expected %r in RCON cmd, got: %s" % (sub, cmd[:200])
        return resp(cmd) if callable(resp) else resp


class Ctx:
    """tmp plans dir + fake rcon + fake bootstrap ledger (in-memory)."""
    def __init__(self, script=(), protected=(), operator=False):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="buildplan-test-"))
        self._orig = (B.PLANS_DIR, B.DIRTY_PATH, rcon.run, B._protected, B._record_built,
                      B._forget_built, B._operator_present, dict(B.KINDS))
        B.PLANS_DIR = self.tmp / "plans"
        B.DIRTY_PATH = B.PLANS_DIR / "_dirty.json"
        self.fake = FakeRcon(script)
        rcon.run = self.fake
        # in-memory stand-ins for bootstrap's ledger + truce
        self.protected = set(protected)
        self.built = set()
        self.operator = operator
        self.recorded = []          # call log: ("record"|"forget", [tiles])
        B._protected = lambda: set(self.protected)
        B._record_built = self._record
        B._forget_built = self._forget
        B._operator_present = lambda: self.operator
        B.KINDS = {}

    def _record(self, tiles):
        tiles = [tuple(t) for t in tiles]
        self.recorded.append(("record", tiles))
        self.built |= set(tiles)

    def _forget(self, tiles):
        tiles = [tuple(t) for t in tiles]
        self.recorded.append(("forget", tiles))
        self.built -= set(tiles)

    def close(self):
        (B.PLANS_DIR, B.DIRTY_PATH, rcon.run, B._protected, B._record_built,
         B._forget_built, B._operator_present, B.KINDS) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)


def _with_ctx(**ctxkw):
    def deco(fn):
        def wrapper():
            ctx = Ctx(**ctxkw)
            try:
                fn(ctx)
            finally:
                ctx.close()
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


# --------------------------------------------------------------------------- fakes
class Placer:
    """place_fn recording exactly which tiles it was handed on each call. `world` is the set
    of tiles that currently hold our entity; `refuse` maps a tile to a failure reason."""
    def __init__(self, world=(), refuse=None):
        self.world = set(world)
        self.refuse = dict(refuse or {})
        self.handed = []            # list of tile-lists, one per call

    def __call__(self, plan, tiles):
        tiles = [tuple(t[:2]) for t in tiles]
        self.handed.append(list(tiles))
        placed, failed = [], []
        for t in tiles:
            if t in self.refuse:
                failed.append({"tile": t, "reason": self.refuse[t]})
            else:
                self.world.add(t)
                placed.append(t)
        return {"placed": placed, "already": [], "failed": failed}


def _probe_from(placer):
    """probe_fn backed by the fake world (stands in for world.scan_tiles)."""
    return lambda plan, tiles: {tuple(t[:2]) for t in tiles} & placer.world


def _ok(detail="ore is moving"):
    return lambda plan: {"ok": True, "detail": detail}


def _bad(detail="no iron-ore moving on the lane after 30s"):
    return lambda plan: {"ok": False, "detail": detail}


def _plan(ctx, tiles, scan_tick=100, names=("transport-belt",), kind="belt_path", pid=None):
    return B.new_plan(kind, {"ore": "iron"}, tiles, scan_tick=scan_tick,
                      names=list(names), id=pid)


def _applied(ctx, tick, live=()):
    """The RCON steps one SUCCESSFUL apply makes, in order: verify.at_tick, then absorb's
    read-only cell re-fingerprint."""
    return [("game.tick", str(tick)),
            ("find_entities_filtered", ctx.fake.scan(list(live)))]


T5 = [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)]


# --------------------------------------------------------------------------- tests
@_with_ctx()
def test_plan_roundtrip(ctx):
    p = _plan(ctx, [(1, 2), (3, 4, 4)], scan_tick=500)
    assert p["status"] == "planned" and p["scan_tick"] == 500 and p["created_tick"] == 500
    assert p["tiles"] == [[1, 2], [3, 4, 4]], p["tiles"]     # 16-way direction carried through
    f = B.path_for(p["id"])
    assert f.is_file() and json.loads(f.read_text())["id"] == p["id"]  # atomic write = valid JSON
    got = B.load(p["id"])
    assert got == p
    try:
        B.load("p-nope")
        raise AssertionError("expected KeyError for unknown id")
    except KeyError:
        pass
    q = _plan(ctx, [(9, 9)], scan_tick=500)
    q["status"] = "verified"
    B.save(q)
    assert [x["id"] for x in B.plans(status="planned")] == [p["id"]]
    assert [x["id"] for x in B.plans(status="verified")] == [q["id"]]
    assert len(B.plans()) == 2
    # _dirty.json is shared runtime state, never a plan
    B._dirty_save({"cells": {}})
    assert len(B.plans()) == 2


@_with_ctx()
def test_plan_ids_never_collide(ctx):
    """Two plans minted in the same second (one planner loop, or two sessions sharing plans/)
    must not land on the same file: save() would overwrite the older record and with it the
    verify.placed that is rollback's only scope."""
    import random
    real = random.getrandbits
    random.getrandbits = lambda k: 0xabcd          # force the collision
    try:
        ids = [_plan(ctx, [(i, 0)])["id"] for i in range(3)]
    finally:
        random.getrandbits = real
    assert len(set(ids)) == 3, ids
    assert all(B.path_for(i).is_file() for i in ids)
    assert len(B.plans()) == 3, "a collision silently ate a plan record"


@_with_ctx()
def test_new_plan_without_scan_tick_scans_its_own_area(ctx):
    """A bare game_tick() is not the dirty map's clock: every stamped cell tick is <= now, so
    a plan carrying one can NEVER be stale and gate 2 silently defaults to off. Omitting
    scan_tick must therefore plan_scan the plan's own area, not just read the clock."""
    ctx.fake.script = [("find_entities_filtered", ctx.fake.scan([])), ("game.tick", "777")]
    p = B.new_plan("belt_path", {}, T5)
    assert p["scan_tick"] == 777 and p["created_tick"] == 777
    assert set(B._dirty_load()["cells"]) == {"0|0"}, "the plan's own cells must be baselined"
    # and that baseline is what makes a later operator edit visible as staleness
    ctx.fake.script = [("find_entities_filtered",
                        ctx.fake.scan([{"n": "transport-belt", "x": 0, "y": 0, "d": 4}])),
                       ("game.tick", "800")]
    assert B.refresh_dirty((0, 0, 4, 0))["dirtied"] == ["0|0"]
    assert B.is_stale(p)["last_change_tick"] == 800


@_with_ctx()
def test_apply_idempotent_refill(ctx):
    placer = Placer()
    probe = _probe_from(placer)
    ctx.fake.script = _applied(ctx, 900)
    p = _plan(ctx, T5)
    p = B.apply(p, place_fn=placer, verify_fn=_ok(), probe_fn=probe, tries=1, delay=0)
    assert p["status"] == "verified", p["verify"]
    assert placer.handed[0] == T5                      # first apply: all 5 handed over
    assert len(p["verify"]["placed"]) == 5

    # second apply: everything is already in the ground -> place_fn is handed NOTHING
    ctx.fake.script = _applied(ctx, 950)
    p2 = B.apply(B.load(p["id"]), place_fn=placer, verify_fn=_ok(), probe_fn=probe,
                 tries=1, delay=0, force=True)
    assert len(placer.handed) == 1, "place_fn must not be called when nothing is missing"
    assert p2["verify"]["placed"] == [list(t) for t in T5]
    assert len(p2["verify"]["already"]) == 5 and p2["verify"]["placed"] and p2["status"] == "verified"

    # third apply after one tile is externally cleared: exactly that one is refilled
    placer.world.discard((2, 0))
    ctx.fake.script = _applied(ctx, 980)
    p3 = B.apply(B.load(p["id"]), place_fn=placer, verify_fn=_ok(), probe_fn=probe,
                 tries=1, delay=0, force=True)
    assert placer.handed[-1] == [(2, 0)], placer.handed
    assert len(p3["verify"]["placed"]) == 5 and p3["status"] == "verified"


@_with_ctx()
def test_verify_writeback(ctx):
    # partial apply 1: one tile refuses with a per-tile reason
    placer = Placer(refuse={(3, 0): "collides with an existing entity"})
    probe = _probe_from(placer)
    ctx.fake.script = [("game.tick", "900")]
    p = _plan(ctx, T5)
    p = B.apply(p, place_fn=placer, verify_fn=_bad("no iron-ore moving on the lane after 30s"),
                probe_fn=probe, tries=1, delay=0, rollback_on_fail=False)
    v = p["verify"]
    assert v["at_tick"] == 900 and v["attempts"] == 1
    assert v["failed"] == [{"tile": [3, 0], "reason": "collides with an existing entity"}]
    assert v["check"] == {"ok": False, "detail": "no iron-ore moving on the lane after 30s"}
    first = {tuple(t) for t in v["placed"]}
    assert first == set(T5) - {(3, 0)}
    assert json.loads(B.path_for(p["id"]).read_text())["verify"]["failed"] == v["failed"]

    # apply 2: the blocker cleared -> placed is the UNION across both applies
    placer.refuse.clear()
    ctx.fake.script = _applied(ctx, 1000)
    p = B.apply(B.load(p["id"]), place_fn=placer, verify_fn=_ok("iron-ore x14 on the lane"),
                probe_fn=probe, tries=1, delay=0, force=True)
    v = p["verify"]
    assert {tuple(t) for t in v["placed"]} == set(T5), v["placed"]
    assert v["attempts"] == 2 and v["failed"] == []
    assert v["check"] == {"ok": True, "detail": "iron-ore x14 on the lane"}
    assert B.load(p["id"])["verify"]["placed"] == v["placed"]      # survives reload


@_with_ctx()
def test_never_verified_without_check(ctx):
    placer = Placer()
    ctx.fake.script = [("game.tick", "900")]
    p = _plan(ctx, T5)
    p = B.apply(p, place_fn=placer, verify_fn=lambda pl: False, probe_fn=_probe_from(placer),
                tries=2, delay=0, rollback_on_fail=False)
    assert len(p["verify"]["placed"]) == 5 and p["verify"]["failed"] == []   # placement worked
    assert p["status"] == "failed", "a passing place_fn must never alone yield verified"
    assert p["verify"]["check"]["ok"] is False

    # and a plan with no functional check at all is refused BEFORE anything is placed
    ctx.fake.script = []
    q = _plan(ctx, T5, scan_tick=100)
    q = B.apply(q, place_fn=placer, verify_fn=None, probe_fn=_probe_from(placer))
    assert q["status"] == "planned" and "NO FUNCTIONAL CHECK" in q["verify"]["refused"]
    assert len(placer.handed) == 1, "nothing may be placed without a verifier"


@_with_ctx()
def test_staleness_refusal(ctx):
    placer = Placer()
    p = _plan(ctx, T5, scan_tick=100)
    B._dirty_save({"cells": {"0|0": {"fp": "deadbeef", "tick": 101}}})
    p = B.apply(p, place_fn=placer, verify_fn=_ok(), probe_fn=_probe_from(placer),
                tries=1, delay=0)
    assert placer.handed == [], "a stale plan must not touch the world at all"
    assert ctx.fake.calls == [], "the staleness gate must cost no RCON"
    assert p["status"] == "planned"
    assert "re-scan and re-plan" in p["verify"]["refused"]
    assert p["verify"]["stale"]["last_change_tick"] == 101
    st = B.is_stale(p)
    assert st["cells"] == ["0|0"] and st["advice"] == "re-scan and re-plan"
    # a tick at or before scan_tick is not stale
    B._dirty_save({"cells": {"0|0": {"fp": "deadbeef", "tick": 100}}})
    assert B.is_stale(p) is None


@_with_ctx()
def test_staleness_self_absorb(ctx):
    """After a verified apply the plan's own build must not make the NEXT plan stale."""
    placer = Placer()
    live = [{"n": "transport-belt", "x": x, "y": 0, "d": 4} for (x, y) in T5]
    ctx.fake.script = [
        ("find_entities_filtered", ctx.fake.scan([])),      # baseline: cell 0|0 empty
        ("game.tick", "1000"),                              # refresh_dirty's tick
    ] + _applied(ctx, 1100, live)                           # apply: at_tick, then absorb
    p = _plan(ctx, T5, scan_tick=100)
    B.refresh_dirty((0, 0, 4, 0), tick=None)                # establishes the baseline
    p["scan_tick"] = 1000
    B.save(p)
    p = B.apply(p, place_fn=placer, verify_fn=_ok(), probe_fn=_probe_from(placer),
                tries=1, delay=0)
    assert p["status"] == "verified" and p["scan_tick"] == 1100
    cells = B._dirty_load()["cells"]
    assert cells["0|0"]["tick"] == 0, "absorb must NOT bump the cell's tick"
    follow = _plan(ctx, T5, scan_tick=1100)
    assert B.is_stale(follow) is None, "our own build made our next plan stale (thrash)"
    # and the absorbed fingerprint is the post-build one, so a rescan sees no change
    ctx.fake.script = [("find_entities_filtered", ctx.fake.scan(live)), ("game.tick", "1200")]
    assert B.refresh_dirty((0, 0, 4, 0))["dirtied"] == []


@_with_ctx()
def test_dirty_map_from_scan(ctx):
    a = [{"n": "transport-belt", "x": 1, "y": 1, "d": 4}]
    ctx.fake.script = [("find_entities_filtered", ctx.fake.scan(a)), ("game.tick", "500")]
    r = B.refresh_dirty((0, 0, 20, 5))
    assert r["scanned"] == 2 and r["dirtied"] == [], "first observation is never dirty"
    assert r["tick"] == 500
    assert set(B._dirty_load()["cells"]) == {"0|0", "1|0"}

    # identical rescan: nothing moved
    ctx.fake.script = [("find_entities_filtered", ctx.fake.scan(a)), ("game.tick", "600")]
    assert B.refresh_dirty((0, 0, 20, 5))["dirtied"] == []

    # an entity appears in cell 0|0 only -> only 0|0 is stamped
    b = a + [{"n": "inserter", "x": 2, "y": 3, "d": 8}]
    ctx.fake.script = [("find_entities_filtered", ctx.fake.scan(b)), ("game.tick", "700")]
    assert B.refresh_dirty((0, 0, 20, 5))["dirtied"] == ["0|0"]
    cells = B._dirty_load()["cells"]
    assert cells["0|0"]["tick"] == 700 and cells["1|0"]["tick"] == 0

    # a REMOVAL is a change too (the cell empties -> the empty-set fingerprint)
    ctx.fake.script = [("find_entities_filtered", ctx.fake.scan([])), ("game.tick", "800")]
    assert B.refresh_dirty((0, 0, 20, 5))["dirtied"] == ["0|0"]
    # negative coordinates floor the same way Lua's math.floor does
    assert B.cell(-1, -1) == (-1, -1) and B.cell(-16, -17) == (-1, -2)


@_with_ctx()
def test_rollback_scoped(ctx):
    placer = Placer()
    ctx.fake.script = _applied(ctx, 900)
    p = _plan(ctx, [(0, 0), (1, 0), (2, 0)])
    p = B.apply(p, place_fn=placer, verify_fn=_ok(), probe_fn=_probe_from(placer),
                tries=1, delay=0)
    assert ctx.built == {(0, 0), (1, 0), (2, 0)}
    # a FOREIGN entity sits on a 4th tile inside the same bbox — it must never be touched
    placer.world.add((3, 0))
    seen = []

    def remove_fn(plan, tiles):
        seen.append([tuple(t) for t in tiles])
        for t in seen[-1]:
            placer.world.discard(t)
        return {"removed": len(seen[-1]), "not_found": 0}

    out = B.rollback(B.load(p["id"]), remove_fn=remove_fn)
    assert seen == [[(0, 0), (1, 0), (2, 0)]], seen
    assert (3, 0) in placer.world, "rollback removed a tile this plan never placed"
    assert out == {"removed": 3, "not_found": 0}
    assert ("forget", [(0, 0), (1, 0), (2, 0)]) in ctx.recorded and ctx.built == set()
    assert B.load(p["id"])["verify"]["placed"] == []


@_with_ctx()
def test_rollback_on_verify_failure(ctx):
    placer = Placer()
    removed = []

    def remove_fn(plan, tiles):
        removed.extend(tuple(t) for t in tiles)
        return {"removed": len(tiles), "not_found": 0}

    B.register("belt_path", remove=remove_fn)
    ctx.fake.script = [("game.tick", "900")]
    p = _plan(ctx, T5)
    p = B.apply(p, place_fn=placer, verify_fn=_bad(), probe_fn=_probe_from(placer),
                tries=1, delay=0)
    assert p["status"] == "failed"
    assert sorted(removed) == sorted(T5), "Build Law 2: tear it out in the SAME pass"
    assert ctx.built == set(), "the built ledger must be clean after our own teardown"
    assert p["verify"]["rollback"] == {"removed": 5, "not_found": 0}
    assert [k for k, _ in ctx.recorded] == ["record", "forget"]


@_with_ctx()
def test_resume_stuck_applying(ctx):
    good = _plan(ctx, [(0, 0)], scan_tick=100, pid="p-good")
    bad = _plan(ctx, [(40, 0)], scan_tick=100, pid="p-bad")
    orph = _plan(ctx, [(80, 0)], scan_tick=100, kind="mystery", pid="p-orph")
    for p in (good, bad, orph):
        p["status"] = "applying"
        p["verify"] = {"placed": [list(p["tiles"][0])], "attempts": 1}
        B.save(p)

    removed = []
    B.register("belt_path", verify=lambda pl: pl["id"] == "p-good",
               remove=lambda pl, tiles: removed.extend(tuple(t) for t in tiles) or len(tiles))
    ctx.fake.script = [("find_entities_filtered", ctx.fake.scan([]))]   # absorb for p-good
    out = B.resume(tries=1, delay=0)

    by = {p["id"]: p for p in out}
    assert by["p-good"]["status"] == "verified" and by["p-good"]["verify"]["check"]["ok"] is True
    assert by["p-bad"]["status"] == "failed"
    assert removed == [(40, 0)], "the unverifiable plan must be torn out, and only it"
    assert by["p-orph"]["status"] == "failed"
    assert "no verifier registered" in by["p-orph"]["verify"]["check"]["detail"]
    assert (80, 0) not in removed, "never tear down blind when we cannot verify"
    assert B.plans(status="applying") == []


@_with_ctx()
def test_record_built_before_verify(ctx):
    """The crash window guarantee: if we die between placing and verifying, the ledger must
    already know WE built those tiles (or reconcile_removals blames the operator)."""
    order = []
    placer = Placer()
    ctx.fake.script = _applied(ctx, 900)

    def place(plan, tiles):
        order.append("place")
        return placer(plan, tiles)

    def record(tiles):
        order.append("record_built")
        ctx._record(tiles)

    def verify(plan):
        order.append("verify")
        return True

    B._record_built = record
    p = B.apply(_plan(ctx, T5), place_fn=place, verify_fn=verify,
                probe_fn=_probe_from(placer), tries=1, delay=0)
    assert order == ["place", "record_built", "verify"], order
    assert p["status"] == "verified"


@_with_ctx(protected=[(2, 0)])
def test_protected_tiles_skipped(ctx):
    placer = Placer()
    ctx.fake.script = _applied(ctx, 900)
    p = _plan(ctx, T5)          # 1 of 5 protected = 20%, under the 25% owned threshold
    p = B.apply(p, place_fn=placer, verify_fn=_ok(), probe_fn=_probe_from(placer),
                tries=1, delay=0)
    assert placer.handed == [[(0, 0), (1, 0), (3, 0), (4, 0)]], placer.handed
    assert p["verify"]["protected_skipped"] == [[2, 0]]
    assert p["status"] == "verified"
    assert (2, 0) not in ctx.built


@_with_ctx(protected=[(0, 0), (1, 0)])
def test_operator_owned_refusal(ctx):
    placer = Placer()
    p = _plan(ctx, T5)          # 2 of 5 = 40% >= 25%
    p = B.apply(p, place_fn=placer, verify_fn=_ok(), probe_fn=_probe_from(placer),
                tries=1, delay=0)
    assert placer.handed == [], "an operator-owned route must never be laid again"
    assert p["status"] == "superseded"
    assert "OPERATOR-OWNED ROUTE" in p["verify"]["refused"]
    assert p["verify"]["protected_skipped"] == [[0, 0], [1, 0]]
    assert ctx.built == set() and ctx.fake.calls == []


@_with_ctx(operator=True)
def test_operator_truce_refusal(ctx):
    placer = Placer()
    p = _plan(ctx, T5)
    p = B.apply(p, place_fn=placer, verify_fn=_ok(), probe_fn=_probe_from(placer),
                tries=1, delay=0)
    assert placer.handed == [] and ctx.fake.calls == [] and ctx.built == set()
    assert p["status"] == "planned", "the truce must not alter the plan's status"
    assert "OPERATOR PRESENT" in p["verify"]["refused"]


@_with_ctx()
def test_supersede_keeps_shared_tiles(ctx):
    placer = Placer()
    ctx.fake.script = _applied(ctx, 900)
    old = _plan(ctx, T5)
    old = B.apply(old, place_fn=placer, verify_fn=_ok(), probe_fn=_probe_from(placer),
                  tries=1, delay=0)
    removed = []

    def remove_fn(plan, tiles):
        removed.extend(tuple(t) for t in tiles)
        return {"removed": len(tiles), "not_found": 0}

    new_route = [(0, 0), (1, 0), (2, 0), (2, 1), (2, 2)]        # reuses the first three
    B.register("belt_path", remove=remove_fn)
    old = B.supersede(B.load(old["id"]), keep=new_route, reason="rerouted around the operator")
    assert sorted(removed) == [(3, 0), (4, 0)], removed
    assert old["status"] == "superseded"
    assert old["verify"]["superseded"]["kept"] == 3
    assert old["verify"]["superseded"]["removed"] == 2
    assert ctx.built == {(0, 0), (1, 0), (2, 0)}, "shared tiles stay ours in the ledger"


@_with_ctx()
def test_rcon_command_cap(ctx):
    """Every generated /sc must stay under the 4KB RCON cap, and so must its RESPONSE. A
    remove command truncated mid-spec destroys an entity the caller never named; a truncated
    probe reads as 'not built' and double-places; a truncated echo mis-attributes the ledger."""
    tiles = [(x, -300) for x in range(-250, 250)]       # 500 tiles, long name, signed coords
    p = _plan(ctx, tiles, names=["electric-mining-drill"])
    sent, replies = [], []

    def fake(cmd, timeout=10.0):
        """Stands in for the Lua: echo back the x,y of every entry in the [==[spec]==]."""
        sent.append(cmd)
        spec = re.search(r"\[==\[(.*?)\]==\]", cmd, re.S).group(1)
        r = ";".join("%s,%s" % tuple(e.split(",")[1:]) for e in spec.split(";") if e)
        replies.append(r)
        return r + "\n"

    real_scan = world.scan_tiles
    world.scan_tiles = lambda t, n: (_ for _ in ()).throw(
        AssertionError("a single-name plan must not probe first: the probe is blind to "
                       "even-footprint entities and would skip them"))
    rcon.run = fake
    try:
        out = B._default_remove(p, tiles)
    finally:
        world.scan_tiles = real_scan
        rcon.run = ctx.fake

    assert sent and max(len(c) for c in sent) < 4096, max(len(c) for c in sent)
    assert len(sent) > 1, "500 long-named tiles cannot fit in one command"
    assert max(len(r) for r in replies) < 4096, max(len(r) for r in replies)
    got = set()
    for c in sent:
        got |= {(int(a), int(b))
                for a, b in re.findall(r"electric-mining-drill,(-?\d+),(-?\d+)", c)}
    assert got == set(tiles), "batching dropped or duplicated tiles"
    assert out["removed"] == 500 and out["not_found"] == 0
    assert sorted(out["removed_tiles"]) == sorted(tiles), "the echo must attribute every tile"

    # a MULTI-name plan cannot attribute a tile to a name on its own, so it probes first —
    # and world.scan_tiles' one-command-per-call must be chunked or the probe truncates.
    q = _plan(ctx, tiles, names=["transport-belt", "underground-belt"])
    scans, sent[:] = [], []
    world.scan_tiles = lambda t, n: scans.append(list(t)) or [
        {"n": "transport-belt", "x": x, "y": y, "d": 0} for (x, y) in t]
    rcon.run = fake
    try:
        out = B._default_remove(q, tiles)
    finally:
        world.scan_tiles = real_scan
        rcon.run = ctx.fake
    assert scans and max(len(s) for s in scans) <= B.SCAN_BATCH
    assert sum(len(s) for s in scans) == 500, "the probe must cover every tile, not truncate"
    assert max(len(c) for c in sent) < 4096
    assert out["removed"] == 500


@_with_ctx()
def test_rollback_leaves_unfound_tiles_in_the_ledger(ctx):
    """The tile we could NOT find is the one the OPERATOR most likely deleted. Forgetting it
    from the built ledger is what stops reconcile_removals from ever protecting it — and the
    bot then re-lays exactly what he deleted, which is the bug this module exists to prevent."""
    placer = Placer()
    ctx.fake.script = _applied(ctx, 900)
    p = B.apply(_plan(ctx, T5), place_fn=placer, verify_fn=_ok(),
                probe_fn=_probe_from(placer), tries=1, delay=0)
    assert ctx.built == set(T5)

    # the operator deleted (2,0) and (4,0) behind our back: the remover finds neither
    def remove_fn(plan, tiles):
        found = [t for t in tiles if t not in ((2, 0), (4, 0))]
        return {"removed": len(found), "not_found": len(tiles) - len(found),
                "removed_tiles": found}

    out = B.rollback(B.load(p["id"]), remove_fn=remove_fn)
    assert out == {"removed": 3, "not_found": 2}
    assert ctx.built == {(2, 0), (4, 0)}, "his deletions must stay attributed to us"
    assert ("forget", [(0, 0), (1, 0), (3, 0)]) in ctx.recorded

    # a remover that removed NOTHING must forget nothing
    ctx.fake.script = _applied(ctx, 950)
    q = B.apply(_plan(ctx, [(9, 9)]), place_fn=Placer(), verify_fn=_ok(),
                probe_fn=lambda pl, t: set(), tries=1, delay=0)
    ctx.built = {(9, 9)}
    B.rollback(B.load(q["id"]),
               remove_fn=lambda pl, t: {"removed": 0, "not_found": 1, "removed_tiles": []})
    assert ctx.built == {(9, 9)}, "an empty removed_tiles must not forget the whole scope"


@_with_ctx()
def test_superseded_plan_is_never_reapplied(ctx):
    placer = Placer()
    ctx.fake.script = _applied(ctx, 900)
    p = B.apply(_plan(ctx, T5), place_fn=placer, verify_fn=_ok(),
                probe_fn=_probe_from(placer), tries=1, delay=0)
    p = B.supersede(B.load(p["id"]), keep=T5, reason="rerouted around the operator")
    assert p["status"] == "superseded"
    placer.world.clear()                       # the old route is gone from the ground
    handed = len(placer.handed)
    p = B.apply(B.load(p["id"]), place_fn=placer, verify_fn=_ok(),
                probe_fn=_probe_from(placer), tries=1, delay=0, force=True)
    assert len(placer.handed) == handed, "a retired route must never be laid a second time"
    assert p["status"] == "superseded" and "SUPERSEDED PLAN" in p["verify"]["refused"]


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    ok = fail = 0
    for t in TESTS:
        try:
            t()
        except Exception:
            fail += 1
            print("FAIL %s" % t.__name__)
            print(traceback.format_exc())
        else:
            ok += 1
            print("PASS %s" % t.__name__)
    print("\n%d passed, %d failed (%d total)" % (ok, fail, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
