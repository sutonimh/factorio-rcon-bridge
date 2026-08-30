#!/usr/bin/env python3
"""Offline unit tests for supply_planner.py — NO live server, NO real ledger.

Run with either:
    python3 test_supply_planner.py
    python3 -m pytest test_supply_planner.py

Every test gets a fresh tmp dir (supply-lanes.json + buildplan's PLANS_DIR/_dirty.json
repointed there), a scripted fake rcon.run (the FakeRcon from test_world_executor.py /
test_buildplan.py, extended to serve ANY storage._<name>:sub chunked read so
world.scan_tiles, buildplan.refresh_dirty and supply_planner.probe_consumers all run for
real), and monkeypatched bootstrap wrappers — so no test can touch built-tiles.json,
protected-tiles.json, lanes.json or the live game.

The routing fixtures are real belt_router.Obstacles built in memory, so plan_supply's A* is
exercised for real too; only the RCON boundary is faked.
"""
import json
import pathlib
import re
import shutil
import tempfile
import traceback

import belt_router
import buildplan as B
import rcon
import supply_planner as SP
import world


# --------------------------------------------------------------------------- harness
class FakeRcon:
    """Scripted rcon.run: (substring, response) steps consumed in order, plus native handling
    of every chunked `storage._<name>:sub(i,j)` read. A response may be a callable(cmd)."""
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

    def consumers(self, tiles):
        """A response serving supply_planner.consumer_lua: the tiles drawn from."""
        return lambda cmd: self.payload_len({"c": ["%d,%d" % (x, y) for (x, y) in tiles]})

    def __call__(self, cmd, timeout=10.0):
        self.calls.append(cmd)
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
    """tmp registry + tmp plans dir + fake rcon + in-memory bootstrap ledger."""
    def __init__(self, script=(), protected=(), operator=False):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="supply-test-"))
        self._orig = (SP.LANES_PATH, B.PLANS_DIR, B.DIRTY_PATH, rcon.run,
                      B._protected, B._record_built, B._forget_built, B._operator_present,
                      SP._protected, SP._operator_present, dict(B.KINDS))
        SP.LANES_PATH = self.tmp / "supply-lanes.json"
        B.PLANS_DIR = self.tmp / "plans"
        B.DIRTY_PATH = B.PLANS_DIR / "_dirty.json"
        self.fake = FakeRcon(script)
        rcon.run = self.fake
        self.protected = {tuple(t) for t in protected}
        self.built = set()
        self.operator = operator
        self.recorded = []
        B._protected = lambda: set(self.protected)
        B._record_built = self._record
        B._forget_built = self._forget
        B._operator_present = lambda: self.operator
        SP._protected = lambda: set(self.protected)
        SP._operator_present = lambda: self.operator
        B.KINDS = {}
        SP._register_kind()

    def _record(self, tiles):
        self.recorded.append(("record", [tuple(t) for t in tiles]))
        self.built |= {tuple(t) for t in tiles}

    def _forget(self, tiles):
        self.recorded.append(("forget", [tuple(t) for t in tiles]))
        self.built -= {tuple(t) for t in tiles}

    def close(self):
        (SP.LANES_PATH, B.PLANS_DIR, B.DIRTY_PATH, rcon.run, B._protected, B._record_built,
         B._forget_built, B._operator_present, SP._protected, SP._operator_present,
         B.KINDS) = self._orig
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


# --------------------------------------------------------------------------- fixtures
BOUNDS = (0, -6, 24, 6)
# A foreign N-S trunk belt filling column x=10 across the whole corridor height: a route from
# the west to the east CANNOT go around it, so the only legal crossing is an underground.
TRUNK_X = 10
TRUNK = {(TRUNK_X, y): {"name": "transport-belt", "dir": 8, "type": "surface"}
         for y in range(BOUNDS[1], BOUNDS[3] + 1)}
# A 2x3 building sitting on the straight line at x=5..6.
MACHINE = {(x, y) for x in (5, 6) for y in (-1, 0, 1)}


def obstacles(hard=(), belts=None, reserved=()):
    return belt_router.Obstacles(hard=set(hard), reserved=set(reserved),
                                 belts=dict(belts or {}), bounds=BOUNDS,
                                 under_max={"underground-belt": 5})


class Placer:
    """place_fn recording exactly which tiles it was handed. `world` is the fake ground."""
    def __init__(self, world=(), refuse=None):
        self.world = set(world)
        self.refuse = dict(refuse or {})
        self.handed = []

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


class Remover:
    """remove_fn recording the tiles it tore out; stands in for the refunding RCON remover."""
    def __init__(self, placer=None):
        self.placer = placer
        self.calls = []

    def __call__(self, plan, tiles):
        tiles = [tuple(t[:2]) for t in tiles]
        self.calls.append(list(tiles))
        gone = []
        for t in tiles:
            if self.placer is None or t in self.placer.world:
                if self.placer is not None:
                    self.placer.world.discard(t)
                gone.append(t)
        return {"removed": len(gone), "not_found": len(tiles) - len(gone),
                "removed_tiles": gone}


def _probe_from(placer):
    return lambda plan, tiles: {tuple(t[:2]) for t in tiles} & placer.world


def _ok(detail="iron-ore: connected=True moving=True"):
    return lambda plan: {"ok": True, "detail": detail}


def _bad(detail="iron-ore: connected=True moving=False arrived=0"):
    return lambda plan: {"ok": False, "detail": detail}


def _applied(ctx, tick):
    """The RCON steps a successful apply makes AFTER place_fn: verify.at_tick, then absorb's
    read-only cell re-fingerprint."""
    return [("game.tick", str(tick)), ("find_entities_filtered", ctx.fake.scan([]))]


def _lay(ctx, item, frm, to, *, obs=None, tick=100, verify=None, remover=None, placer=None,
         dest_tol=SP.DEST_TOL):
    """plan_supply + build with fake place/verify (no lua), returning (result, plan, placer)."""
    obs = obs if obs is not None else obstacles(belts=TRUNK)
    res = SP.plan_supply(item, frm, to, obstacles=obs, scan_tick=tick, dest_tol=dest_tol)
    assert res["ok"], res["reason"]
    placer = placer or Placer()
    if remover is not None:
        B.register(SP.KIND, place=SP.place_lane, verify=SP.verify_lane, remove=remover)
    ctx.fake.script = _applied(ctx, tick + 800)
    plan = SP.build(res["plan"], place_fn=placer, verify_fn=verify or _ok(),
                    probe_fn=_probe_from(placer), tries=1, delay=0)
    return res, plan, placer


# --------------------------------------------------------------------------- tests
@_with_ctx()
def test_registry_roundtrip_and_dest_tolerance(ctx):
    res, plan, _ = _lay(ctx, "iron-ore", (0, 0), (24, 0))
    rec = SP.get_lane(res["lane"]["id"])
    assert rec["status"] == "active" and rec["item"] == "iron-ore"
    assert rec["from"] == [0, 0] and rec["to"] == [24, 0]
    assert json.loads(pathlib.Path(SP.LANES_PATH).read_text())["lanes"][0]["id"] == rec["id"]
    # keyed on the DESTINATION only, within DEST_TOL
    assert SP.find_lane("iron-ore", (24, 0)) is not None
    assert SP.find_lane("iron-ore", (24, 2)) is not None            # tol 2
    assert SP.find_lane("iron-ore", (24, 3)) is None                # outside tol
    assert SP.find_lane("copper-ore", (24, 0)) is None              # different commodity
    assert SP.lanes(status="active") and not SP.lanes(status="retired")


@_with_ctx()
def test_duplicate_lane_is_refused_and_returns_the_existing_one(ctx):
    """The 72.4% rule on the creation side: a second lane into a destination the item already
    reaches is never planned - the caller is handed the lane that already serves it."""
    first, _, _ = _lay(ctx, "iron-ore", (0, 0), (24, 0))
    before = len(SP.lanes())

    dup = SP.plan_supply("iron-ore", (0, 4), (24, 0),
                         obstacles=obstacles(belts=TRUNK), scan_tick=100)
    assert dup["ok"] is False and dup["code"] == SP.DUPLICATE, dup
    assert dup["existing"] is True
    assert dup["lane"]["id"] == first["lane"]["id"], "must hand back the EXISTING lane"
    assert dup["plan"] is None and dup["route"] is None
    assert len(SP.lanes()) == before, "a refused duplicate must not register anything"
    assert len(B.plans()) == 1, "a refused duplicate must not mint a buildplan"

    # within tolerance of the same destination -> still a duplicate
    near = SP.plan_supply("iron-ore", (0, -4), (24, 2),
                          obstacles=obstacles(belts=TRUNK), scan_tick=100)
    assert near["code"] == SP.DUPLICATE and near["lane"]["id"] == first["lane"]["id"]

    # a DIFFERENT commodity to the same destination is legal (the operator's feed rows carry
    # ore on one lane and coal on the other), as is the same commodity elsewhere
    other = SP.plan_supply("coal", (0, 4), (24, 4),
                           obstacles=obstacles(belts=TRUNK), scan_tick=100)
    assert other["ok"] is True, other["reason"]
    assert len(SP.lanes()) == before + 1

    # and once the first lane is retired, its destination is free again
    rec = SP.get_lane(first["lane"]["id"])
    rec["status"] = "retired"
    SP._put(rec)
    again = SP.plan_supply("iron-ore", (0, -4), (24, 0),
                           obstacles=obstacles(belts=TRUNK), scan_tick=100)
    assert again["ok"] is True, again["reason"]


@_with_ctx(protected=[(24, 0)])
def test_protected_endpoint_is_refused(ctx):
    res = SP.plan_supply("iron-ore", (0, 0), (24, 0),
                         obstacles=obstacles(belts=TRUNK), scan_tick=100)
    assert res["ok"] is False and res["code"] == SP.PROTECTED_ENDPOINT, res
    assert "BUILD LAW 3" in res["reason"]
    assert SP.lanes() == [] and B.plans() == []


@_with_ctx(protected=[(TRUNK_X + 3, y) for y in range(BOUNDS[1], BOUNDS[3] + 1)])
def test_protected_tiles_are_tunnelled_under_never_built_on(ctx):
    """Protected tiles join the router's HARD set, which is SPAN-passable: an underground may
    tunnel beneath a tile the operator cleared (the tile stays empty, which is what he asked
    for) but nothing may ever be PLACED on it. Here a full-height protected column blocks the
    corridor, and the route goes under it."""
    res = SP.plan_supply("iron-ore", (0, 0), (24, 0),
                         obstacles=obstacles(belts=TRUNK), scan_tick=100)
    assert res["ok"] is True, res["reason"]
    ent = set(belt_router.plan_tiles(res["route"]))
    assert not (ent & ctx.protected), "not one entity may land on a protected tile"
    spans = {tuple(t) for s in res["route"] for t in s.get("span", ())}
    assert ctx.protected <= (spans | (ctx.protected - {(TRUNK_X + 3, 0)}))
    assert (TRUNK_X + 3, 0) in spans, "the protected tile on the line is SPANNED, not built on"
    assert res["conflicts"]["count"] == 0


@_with_ctx(protected=[(x, y) for x in range(13, 19) for y in range(BOUNDS[1], BOUNDS[3] + 1)])
def test_protected_wall_too_wide_to_tunnel_is_refused(ctx):
    """6 columns wide beats underground-belt's max span (position delta 5 = 4 covered tiles),
    and the wall runs the full corridor height, so there is no legal route at all. The answer
    is a refusal - never a lane laid over the operator's deletions."""
    res = SP.plan_supply("iron-ore", (0, 0), (24, 0),
                         obstacles=obstacles(belts=TRUNK), scan_tick=100)
    assert res["ok"] is False and res["code"] == SP.NO_ROUTE, res
    assert SP.lanes() == [] and B.plans() == []


@_with_ctx(protected=[(3, 0)])
def test_protected_post_check_catches_a_route_that_slipped_through(ctx):
    """Belt-and-braces: even if the router handed back a route over a protected tile, nothing
    is registered and nothing is planned."""
    real = belt_router.plan_route
    belt_router.plan_route = lambda *a, **k: [
        {"x": x, "y": 0, "dir": 4, "entity": "transport-belt"} for x in range(0, 6)]
    try:
        res = SP.plan_supply("iron-ore", (0, 0), (5, 0),
                             obstacles=obstacles(), scan_tick=100)
    finally:
        belt_router.plan_route = real
    assert res["ok"] is False and res["code"] == SP.PROTECTED_TILES, res
    assert res["conflicts"]["tiles"] == [(3, 0)]
    assert SP.lanes() == [] and B.plans() == []


@_with_ctx()
def test_route_never_crosses_a_machine_or_a_lane_and_crosses_by_underground(ctx):
    obs = obstacles(hard=MACHINE, belts=TRUNK)
    res = SP.plan_supply("iron-ore", (0, 0), (24, 0), obstacles=obs, scan_tick=100)
    assert res["ok"], res["reason"]
    tiles = set(belt_router.plan_tiles(res["route"]))
    assert not (tiles & MACHINE), "a lane never runs through a machine"
    assert not (tiles & set(TRUNK)), "a lane never runs on another lane"
    # the trunk column spans the full corridor, so the ONLY way past it is under it
    ug = [s for s in res["route"] if s["entity"] == "underground-belt"]
    assert len(ug) == 2 and {s["type"] for s in ug} == {"input", "output"}, ug
    span = [t for s in ug for t in s.get("span", ())]
    assert (TRUNK_X, 0) in {tuple(t) for t in span}, span
    assert res["crossings"] == 1 and res["lane"]["crossings"] == 1
    # the underground pair spans exactly 2 - all three of the operator's pairs measure 2
    assert abs(ug[0]["x"] - ug[1]["x"]) == 2, ug


@_with_ctx()
def test_place_lane_emits_router_lua_then_reprobes_for_attribution(ctx):
    """place_lane must never report a tile placed that it did not read back: `built/total` is
    an aggregate, so the truth comes from a re-probe."""
    res = SP.plan_supply("iron-ore", (0, 0), (6, 0), obstacles=obstacles(), scan_tick=100)
    assert res["ok"], res["reason"]
    plan = res["plan"]
    steps = [s for s in res["route"] if not s.get("adopt")]
    cmds = belt_router.plan_to_lua(steps)
    assert cmds and len(plan["tiles"]) == 7
    # every tile lands except (4,0): the re-probe is what notices
    landed = [{"n": s["entity"], "x": s["x"], "y": s["y"], "d": s["dir"]}
              for s in steps if (s["x"], s["y"]) != (4, 0)]
    ctx.fake.script = ([("find_entities_filtered", ctx.fake.scan([]))] +
                       [("create_entity", "6/7")] * len(cmds) +
                       [("find_entities_filtered", ctx.fake.scan(landed))] +
                       _applied(ctx, 900))
    out = SP.build(res, verify_fn=_ok(), tries=1, delay=0)
    v = out["verify"]
    assert out["status"] == "verified", v
    assert len(v["placed"]) == 6 and [4, 0] not in v["placed"]
    assert v["failed"] == [{"tile": [4, 0],
                            "reason": v["failed"][0]["reason"]}] and "6/7" in v["failed"][0]["reason"]
    assert ctx.built == {tuple(t) for t in v["placed"]}, "only what landed is recorded as ours"


@_with_ctx()
def test_build_rolls_back_when_the_lane_moves_nothing(ctx):
    """BUILD LAW 1+2: the acceptance test is 'items arriving', and a lane that moves nothing is
    torn out in the SAME pass — and its (item, destination) pair is freed."""
    placer = Placer()
    rm = Remover(placer)
    res, plan, placer = _lay(ctx, "iron-ore", (0, 0), (24, 0),
                             verify=_bad("iron-ore: connected=True moving=False arrived=0"),
                             remover=rm, placer=placer)
    assert plan["status"] == "failed", plan["verify"]
    assert plan["verify"]["check"]["ok"] is False
    assert plan["verify"]["rollback"]["removed"] == len(rm.calls[0]) > 0
    assert rm.calls and set(rm.calls[0]) == set(placer.world) | set(rm.calls[0])
    assert placer.world == set(), "rollback must leave nothing in the ground"
    assert ctx.built == set(), "and nothing recorded as ours"

    rec = SP.get_lane(res["lane"]["id"])
    assert rec["status"] == "retired" and "moving=False" in rec["reason"]
    # the pair is free again: a DIFFERENT route to the same destination may now be planned
    again = SP.plan_supply("iron-ore", (0, 2), (24, 0),
                           obstacles=obstacles(belts=TRUNK), scan_tick=100)
    assert again["ok"] is True, again["reason"]


@_with_ctx()
def test_retire_obsolete_drops_a_consumerless_lane_and_spares_a_live_one(ctx):
    """The coal-to-drill fuel lane after electrify_mines: the burner drills it fed are gone,
    so no inserter's pickup lands on it. The belts still look perfect; the lane is dead."""
    live_p, dead_p = Placer(), Placer()
    rm = Remover(dead_p)
    live_res, _, _ = _lay(ctx, "iron-ore", (0, 0), (24, 0), placer=live_p)
    dead_res, _, _ = _lay(ctx, "coal", (0, 4), (24, 4), placer=dead_p, remover=rm)
    live_id, dead_id = live_res["lane"]["id"], dead_res["lane"]["id"]
    assert dead_p.world, "the dead lane is in the ground before retirement"

    counts = {live_id: 3, dead_id: 0}
    rows = SP.retire_obsolete(consumers_fn=lambda r: counts[r["id"]])
    assert len(rows) == 1, rows
    assert rows[0]["id"] == dead_id and rows[0]["consumers"] == 0
    assert "no consumer" in rows[0]["reason"]
    assert rows[0]["removed"] == len(rm.calls[0]) > 0

    assert SP.get_lane(dead_id)["status"] == "retired"
    assert SP.get_lane(live_id)["status"] == "active", "a lane with consumers is untouched"
    assert dead_p.world == set() and live_p.world, "teardown is registry-SCOPED"
    assert B.load(dead_id)["status"] == "superseded"
    assert B.load(live_id)["status"] == "verified"
    # idempotent: a second pass finds nothing left to retire
    assert SP.retire_obsolete(consumers_fn=lambda r: counts[r["id"]]) == []


@_with_ctx()
def test_retire_obsolete_spares_a_trunk_whose_consumer_is_another_lane(ctx):
    """L1_copper_trunk is 111 belts with no inserter of its own: it hands off to a feed row.
    A tail touching another live lane counts as a consumer, or every trunk on the map would
    read dead."""
    trunk, _, _ = _lay(ctx, "coal", (0, 0), (8, 0), obs=obstacles())     # the trunk
    row, _, _ = _lay(ctx, "coal", (9, 0), (20, 0), obs=obstacles())      # the row it feeds
    tid, rid = trunk["lane"]["id"], row["lane"]["id"]
    assert SP._downstream_feeds(SP.lanes(status=SP.ACTIVE)) == {tid: 1, rid: 0}
    rows = SP.retire_obsolete(consumers_fn=lambda r: 0 if r["id"] == tid else 3)
    assert rows == [], rows
    assert all(r["status"] == "active" for r in SP.lanes())


@_with_ctx()
def test_retire_obsolete_removes_a_parallel_duplicate(ctx):
    """Spec L2, the operator's biggest deletion class (92/127 belts): two runs of the same item
    within 3 tiles over 8+ shared tiles. The one nothing draws from loses.

    The second lane is planned with dest_tol=0 on purpose: plan_supply's own dedupe would
    normally refuse it outright, so this detector is what catches a duplicate that predates
    the registry or was registered under a destination the dest key did not match.
    """
    keep_p, dup_p = Placer(), Placer()
    rm = Remover(dup_p)
    keep, _, _ = _lay(ctx, "iron-ore", (0, 0), (24, 0), obs=obstacles(), placer=keep_p)
    dup, _, _ = _lay(ctx, "iron-ore", (0, 2), (24, 2), obs=obstacles(), placer=dup_p,
                     remover=rm, dest_tol=0)
    pairs = SP.parallel_duplicates(SP.lanes(status=SP.ACTIVE))
    assert len(pairs) == 1, pairs
    assert pairs[0][2] == 2 <= SP.DUP_SEP_MAX
    assert pairs[0][3] >= SP.DUP_OVERLAP_MIN, pairs

    counts = {keep["lane"]["id"]: 4, dup["lane"]["id"]: 0}
    rows = SP.retire_obsolete(consumers_fn=lambda r: counts[r["id"]])
    assert len(rows) == 1 and rows[0]["id"] == dup["lane"]["id"], rows
    assert "parallel duplicate" in rows[0]["reason"]
    assert SP.get_lane(keep["lane"]["id"])["status"] == "active"
    assert dup_p.world == set() and keep_p.world


@_with_ctx()
def test_retire_obsolete_spares_a_lane_that_is_only_planned(ctx):
    """A lane still in "planned" has not been laid, so of course nothing draws from it.
    Retiring it here would supersede the plan the caller is about to apply - and a superseded
    plan is refused forever. Only ACTIVE lanes are judged on consumers."""
    res = SP.plan_supply("iron-ore", (0, 0), (20, 0), obstacles=obstacles(), scan_tick=100)
    assert res["lane"]["status"] == "planned"
    assert SP.retire_obsolete(consumers_fn=lambda r: 0) == []
    assert SP.get_lane(res["lane"]["id"])["status"] == "planned"
    assert B.load(res["plan"]["id"])["status"] == "planned", "the plan stays appliable"


@_with_ctx()
def test_retire_obsolete_never_deletes_on_a_probe_failure(ctx):
    _lay(ctx, "iron-ore", (0, 0), (20, 0), obs=obstacles())

    def boom(rec):
        raise RuntimeError("rcon read failed")
    rows = SP.retire_obsolete(consumers_fn=boom)
    assert rows == [], rows
    assert SP.lanes(status="active"), "an unreadable lane counts as supplied"


@_with_ctx()
def test_a_failed_probe_raises_rather_than_reading_as_zero_consumers(ctx):
    """REGRESSION. The guard above only fires if the REAL probe reports failure as failure.
    probe_consumers used to swallow both of its failure modes and return 0 - and a 0 from
    there is a delete order, so a single flaky RCON read would refund-tear-out a working
    lane while `except Exception` sat there never firing. A failed read must be
    indistinguishable from no read, never from an answer of zero."""
    res, _, _ = _lay(ctx, "coal", (0, 0), (20, 0), obs=obstacles())
    rec = SP.get_lane(res["lane"]["id"])

    # 1. the /sc threw: the response is Lua prose where a payload length should be.
    # Two steps: read_chunked retries a failed read once (tries=2) before giving up.
    ctx.fake.script = [("find_entities_filtered",
                        "Error: The mod level (level) caused a non-recoverable error.")] * 2
    try:
        SP.probe_consumers(rec)
        raise AssertionError("a Lua error must not read back as 'no consumers'")
    except RuntimeError as e:
        assert "FAILED" in str(e)

    # 2. the payload came back SHORT of the length Lua reported - a truncated read, or the
    # buffer clobbered mid-read. read_chunked's length check names it; it used to surface only
    # as a JSONDecodeError at whatever offset the bytes happened to stop making sense.
    ctx.fake.payload = '{"c":["0,0","1,'
    ctx.fake.script = [("find_entities_filtered", lambda cmd: "999")] * 2
    try:
        SP.probe_consumers(rec)
        raise AssertionError("a truncated payload must not read back as 'no consumers'")
    except RuntimeError as e:
        assert "FAILED" in str(e)

    # 3. and end to end: retire_obsolete with the REAL probe spares the lane
    ctx.fake.script = [("find_entities_filtered", "Error: something broke")] * 2
    assert SP.retire_obsolete() == []
    assert SP.get_lane(rec["id"])["status"] == "active", "a flaky read is never a delete order"

    # a read that genuinely SUCCEEDS and finds nothing still retires it
    ctx.fake.script = [("find_entities_filtered", ctx.fake.consumers([]))]
    assert SP.probe_consumers(rec) == 0


@_with_ctx()
def test_a_parallel_duplicate_needs_the_same_destination(ctx):
    """REGRESSION. spec L2 is "two runs carrying the same item ... TERMINATING AT THE SAME
    CONSUMER". The detector used to drop that clause, so two healthy iron lanes to consumers
    far apart that merely shared a corridor read as duplicates - and this detector, unlike
    principles.py's `warn`, tears belts out of the ground.

    A feeds a consumer at (20,0); B runs two rows south, past it, to a different consumer at
    (24,2). 20+ tiles of shared span, 2 tiles apart, both fully fed."""
    a, _, ap = _lay(ctx, "iron-ore", (0, 0), (20, 0), obs=obstacles())
    b, _, bp = _lay(ctx, "iron-ore", (0, 2), (24, 2), obs=obstacles(), dest_tol=0)
    assert SP.parallel_duplicates(SP.lanes(status=SP.ACTIVE)) == [], "different consumers"
    assert SP.retire_obsolete(consumers_fn=lambda r: 6) == []
    assert ap.world and bp.world, "neither healthy lane may be torn out"

    # move B's destination onto A's and it IS the duplicate the operator deleted
    rec = SP.get_lane(b["lane"]["id"])
    rec["to"] = [20, 1]                                   # within DEST_TOL of (20,0)
    SP._put(rec)
    pairs = SP.parallel_duplicates(SP.lanes(status=SP.ACTIVE))
    assert len(pairs) == 1 and pairs[0][2] == 2 and pairs[0][3] >= SP.DUP_OVERLAP_MIN, pairs


@_with_ctx()
def test_build_refuses_a_kind_whose_only_verifier_cannot_pass(ctx):
    """BUILD LAW 1 is "never build what cannot be verified", not "build, then find out".
    verify_supply floods the BELT graph, so a pipe run is invisible to it: applied, that lane
    would be laid, fail every attempt, and be rolled straight back out. Refuse before the
    first create_entity - or hand build() a real fluid check."""
    res = SP.plan_supply("water", (0, 0), (6, 0), kind="pipe", obstacles=obstacles(),
                         scan_tick=100)
    assert res["ok"] is True, res["reason"]               # planning is free and read-only
    assert SP.verify_lane(res["plan"])["ok"] is False
    try:
        SP.build(res)
        raise AssertionError("a lane with no working verifier must never reach the ground")
    except ValueError as e:
        assert "no functional verifier" in str(e)
    assert B.load(res["plan"]["id"])["status"] == "planned", "nothing was applied"
    # the escape hatch: a caller WITH a real check for this kind may still build
    ctx.fake.script = _applied(ctx, 900)
    out = SP.build(res, place_fn=Placer(), verify_fn=_ok(), probe_fn=lambda p, t: set(),
                   tries=1, delay=0)
    assert out["status"] == "verified", out["verify"]


@_with_ctx(protected=[(6, 0)])
def test_every_refusal_carries_the_documented_result_keys(ctx):
    """The result dict is documented as always carrying these; a caller logging
    res["crossings"] must not eat a KeyError on the one branch that refused."""
    KEYS = ("ok", "code", "reason", "lane", "plan", "route", "existing", "conflicts",
            "crossings")
    results = [SP.plan_supply("iron-ore", (0, 0), (6, 0), obstacles=obstacles(),
                              scan_tick=100),                       # PROTECTED_ENDPOINT
               SP.plan_supply("iron-ore", (0, 0), (99, 99), obstacles=obstacles(),
                              scan_tick=100),                       # NO_ROUTE
               SP.plan_supply("iron-ore", (0, 0), (20, 0), obstacles=obstacles(),
                              scan_tick=100),                       # ok
               SP.plan_supply("iron-ore", (0, 4), (20, 0), obstacles=obstacles(),
                              scan_tick=100)]                       # DUPLICATE_LANE
    assert [r["code"] for r in results] == [SP.PROTECTED_ENDPOINT, SP.NO_ROUTE, None,
                                            SP.DUPLICATE], [r["code"] for r in results]
    for r in results:
        assert not [k for k in KEYS if k not in r], (r["code"], sorted(r))


@_with_ctx(operator=True)
def test_retire_obsolete_defers_to_the_operator_truce(ctx):
    ctx.operator = False
    _lay(ctx, "iron-ore", (0, 0), (20, 0), obs=obstacles())
    ctx.operator = True
    rows = SP.retire_obsolete(consumers_fn=lambda r: 0)
    assert len(rows) == 1 and "OPERATOR PRESENT" in rows[0]["reason"], rows
    assert SP.lanes(status="active"), "nothing torn out while a human is connected"
    # a dry run is read-only and still reports
    dry = SP.retire_obsolete(consumers_fn=lambda r: 0, dry_run=True)
    assert len(dry) == 1 and dry[0]["dry_run"] is True and dry[0]["removed"] == 0
    assert SP.lanes(status="active")


@_with_ctx()
def test_probe_consumers_reads_pickup_positions(ctx):
    res = SP.plan_supply("iron-ore", (0, 0), (6, 0), obstacles=obstacles(), scan_tick=100)
    rec = res["lane"]
    # two inserters pick off the lane, one picks off bare ground beside it
    ctx.fake.script = [("find_entities_filtered", ctx.fake.consumers([(2, 0), (5, 0), (3, 4)]))]
    assert SP.probe_consumers(rec) == 2
    # the emitted Lua must carry the REAL padded box, not a %d template: a substring-matching
    # fake would happily accept "area={{%d,%d},{%d,%d}}" and the live call would then throw.
    cmd = ctx.fake.calls[0]
    assert "%d" not in cmd, cmd[:200]
    assert cmd.count("area={{-3,-3},{9,3}}") == 2, cmd[:300]
    ctx.fake.script = [("find_entities_filtered", ctx.fake.consumers([(3, 4)]))]
    assert SP.probe_consumers(rec) == 0


@_with_ctx()
def test_build_accepts_result_registry_record_or_id_and_rejects_a_refusal(ctx):
    """A REGISTRY record also carries "status" and "tiles", so the dispatch must match it on
    plan_id first - otherwise buildplan.apply gets a record with no `args` and no directions."""
    res = SP.plan_supply("iron-ore", (0, 0), (6, 0), obstacles=obstacles(), scan_tick=100)
    pid = res["plan"]["id"]
    assert SP._as_plan(res)["id"] == pid
    assert SP._as_plan(pid)["id"] == pid
    assert SP._as_plan(res["lane"])["id"] == pid            # registry record -> its buildplan
    assert SP._as_plan(res["plan"])["id"] == pid
    assert SP._as_plan(res["lane"])["args"]["item"] == "iron-ore"
    dup = SP.plan_supply("iron-ore", (0, 2), (6, 0), obstacles=obstacles(), scan_tick=100)
    assert dup["code"] == SP.DUPLICATE
    try:
        SP._as_plan(dup)
        raise AssertionError("a refused plan must never be buildable")
    except ValueError as e:
        assert "REFUSED" in str(e)


@_with_ctx()
def test_lane_kind_is_registered_for_crash_resume(ctx):
    assert B.KINDS[SP.KIND]["place"] is SP.place_lane
    assert B.KINDS[SP.KIND]["verify"] is SP.verify_lane


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
