#!/usr/bin/env python3
"""Offline unit tests for plant_planner.py - NO live server.

Run with:
    python3 test_plant_planner.py

The geometry expectations are the MEASURED operator plant (live read-only probes at
tick ~1.12M, cross-checked against snapshots/after.json, which this file diffs against
directly). The buildplan integration is driven by the FakeRcon + Ctx harness from
test_buildplan.py / test_world_executor.py: a tmp plans dir, a scripted rcon.run, and
in-memory stand-ins for the built/protected ledger, so no test can touch built-tiles.json,
protected-tiles.json or the game.
"""
import json
import math
import pathlib
import re
import shutil
import tempfile
import traceback

import buildplan as B
import plant_planner as P
import rcon


HERE = pathlib.Path(__file__).resolve().parent

# The operator's plant, measured live: (name, centre_x, centre_y, direction). 27 entities.
MEASURED = [
    ("boiler", -33.5, 46.0, 0), ("boiler", -29.5, 46.0, 0),
    ("steam-engine", -33.5, 42.5, 0), ("steam-engine", -29.5, 42.5, 0),
    ("steam-engine", -33.5, 37.5, 0), ("steam-engine", -29.5, 37.5, 0),
    ("burner-inserter", -33.5, 47.5, 8), ("burner-inserter", -29.5, 47.5, 8),
    ("pipe", -31.5, 46.5, 0),                                    # the SHARED tap
    ("pipe-to-ground", -31.5, 47.5, 0), ("pipe-to-ground", -31.5, 49.5, 8),
    ("pipe", -31.5, 50.5, 0), ("pipe", -30.5, 50.5, 0), ("pipe", -29.5, 50.5, 0),
    ("pipe", -28.5, 50.5, 0), ("pipe", -27.5, 50.5, 0),
    ("offshore-pump", -31.5, 51.5, 8),
    ("transport-belt", -36.5, 48.5, 4), ("transport-belt", -35.5, 48.5, 4),
    ("transport-belt", -34.5, 48.5, 4), ("transport-belt", -33.5, 48.5, 4),
    ("transport-belt", -32.5, 48.5, 4), ("transport-belt", -31.5, 48.5, 4),
    ("transport-belt", -30.5, 48.5, 4), ("transport-belt", -29.5, 48.5, 4),
    ("small-electric-pole", -35.5, 40.5, 0),                     # trunk junction
    ("small-electric-pole", -31.5, 40.5, 0),                     # spur, covers 4 engines
]
PUMP = (-32, 51)          # the pump's LAND tile
ANCHOR = (-35, 45)        # boiler-0's TOP-LEFT tile


def centres(plan):
    out = set()
    for e in plan["entities"]:
        cx, cy = P.center(e["entity"], e["x"], e["y"])
        out.add((e["entity"], round(cx, 1), round(cy, 1), e["direction"]))
    return out


def roles(plan, role):
    return [e for e in plan["entities"] if e["role"] == role]


def lake_terrain(shore_y=51, x1=-50, x2=-10, y1=30, y2=60, extra_water=()):
    """A flat map with a lake filling every row south of `shore_y`."""
    water = {(x, y) for x in range(x1, x2 + 1) for y in range(shore_y + 1, y2 + 1)}
    water |= set(extra_water)
    return P.Terrain((x1, y1, x2, y2), water=water)


# --------------------------------------------------------------------------- harness
class FakeRcon:
    """Scripted rcon.run: (substring, response) steps consumed in order, plus native
    handling of the chunked storage._world read (test_world_executor.py style)."""

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []
        self.payload = None

    def payload_len(self, obj):
        self.payload = json.dumps(obj, separators=(",", ":"))
        return str(len(self.payload))

    def scan(self, entities):
        return lambda cmd: self.payload_len(entities)

    def text(self, s):
        """A response serving a raw chunked STRING payload (scan_shore's format)."""
        def _r(cmd):
            self.payload = s
            return str(len(s))
        return _r

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
            raise AssertionError("unexpected RCON call (script exhausted): %s" % cmd[:200])
        sub, resp = self.script.pop(0)
        assert sub in cmd, "expected %r in RCON cmd, got: %s" % (sub, cmd[:200])
        return resp(cmd) if callable(resp) else resp


class Ctx:
    """tmp plans dir + fake rcon + in-memory ledger + captured plant WRITE wrappers."""

    def __init__(self, script=(), protected=(), operator=False, refuse=None):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="plant-test-"))
        self._orig = (B.PLANS_DIR, B.DIRTY_PATH, rcon.run, B._protected, B._record_built,
                      B._forget_built, B._operator_present, dict(B.KINDS),
                      P._place_entity, P._clear_area, P._fuel_boilers, P.read_state,
                      P._wire_poles)
        B.PLANS_DIR = self.tmp / "plans"
        B.DIRTY_PATH = B.PLANS_DIR / "_dirty.json"
        self.fake = FakeRcon(script)
        rcon.run = self.fake
        self.protected = set(protected)
        self.built = set()
        self.operator = operator
        self.recorded = []
        B._protected = lambda: set(self.protected)
        B._record_built = self._record
        B._forget_built = self._forget
        B._operator_present = lambda: self.operator
        B.KINDS = {}
        # plant-side writes, all captured
        self.placed = []            # [(name, x, y, direction)]
        self.cleared = []
        self.fuelled = []
        self.refuse = dict(refuse or {})
        self.removed = []           # [(plan_id, [tiles])]
        self.wired = []             # [(made, already, missing)] per /sc issued
        self.wirecmds = []
        P._place_entity = self._place
        P._clear_area = self._clear
        P._fuel_boilers = self._fuel
        P._wire_poles = self._wire
        self.state = {}
        P.read_state = lambda plan: dict(self.state)

    def _place(self, name, x, y, direction):
        if (x, y) in self.refuse:
            return self.refuse[(x, y)]
        self.placed.append((name, x, y, direction))
        return "BUILT %s @(%s,%s)" % (name, x, y)

    def _clear(self, cx, cy, radius):
        self.cleared.append((cx, cy, radius))
        return (0, 0)

    def _fuel(self, centres_, coal):
        self.fuelled.append((list(centres_), coal))
        return str(len(centres_))

    def _wire(self, cmd):
        """Stand in for the wiring /sc: count the pairs it carries and report them made."""
        self.wirecmds.append(cmd)
        body = re.search(r"\[==\[(.*?)\]==\]", cmd, re.S).group(1)
        n = len([e for e in body.split(";") if e.strip()])
        self.wired.append((n, 0, 0))
        return "%d/0/0" % n

    def _record(self, tiles):
        tiles = [tuple(t) for t in tiles]
        self.recorded.append(("record", tiles))
        self.built |= set(tiles)

    def _forget(self, tiles):
        tiles = [tuple(t) for t in tiles]
        self.recorded.append(("forget", tiles))
        self.built -= set(tiles)

    def remover(self):
        """A recording remove_fn: rollback's scope IS the record, so assert on it."""
        def _rm(plan, tiles):
            tiles = [tuple(t) for t in tiles]
            self.removed.append((plan["id"], tiles))
            return {"removed": len(tiles), "not_found": 0,
                    "removed_tiles": [list(t) for t in tiles]}
        return _rm

    def close(self):
        (B.PLANS_DIR, B.DIRTY_PATH, rcon.run, B._protected, B._record_built,
         B._forget_built, B._operator_present, B.KINDS,
         P._place_entity, P._clear_area, P._fuel_boilers, P.read_state,
         P._wire_poles) = self._orig
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


def live_state(plan, override=None):
    """A healthy live reading for every entity verify() looks at. `override` is keyed by
    (name, centre_x, centre_y) - tuples, so it cannot be **kwargs."""
    st = {}
    for name, cx, cy in P._check_spec(plan):
        if name == "boiler":
            st[(name, cx, cy)] = (200, 199, -1)
        elif name == "steam-engine":
            st[(name, cx, cy)] = (13547, 1435, 535)
        elif name == "offshore-pump":
            st[(name, cx, cy)] = (100, -1, -1)
        elif name == "burner-inserter":
            st[(name, cx, cy)] = (36, 1, -1)
        elif name == "transport-belt":
            st[(name, cx, cy)] = (8, -1, -1)
        else:
            st[(name, cx, cy)] = (535, -1, -1)
    st.update(override or {})
    return st


# --------------------------------------------------------------------------- template
def test_template_reproduces_the_measured_plant():
    """plan_plant(4) at the operator's pump tile must emit HIS 27 entities, to the tile
    and to the direction. This is the whole point of the module."""
    p = P.plan_plant(4, water_hint=PUMP)
    assert p["anchor"] == ANCHOR, p["anchor"]
    assert p["n_columns"] == 2 and p["n_engines"] == 4 and p["n_boilers"] == 2
    assert p["spine_x"] == -32                      # Sx = -31.5 centre = the gap column
    got, want = centres(p), set(MEASURED)
    assert got == want, ("planned-but-not-measured: %s | measured-but-not-planned: %s"
                         % (sorted(got - want), sorted(want - got)))
    assert len(p["entities"]) == 27
    assert P.validate(p)["ok"], P.validate(p)["errors"]
    # the three measured backbone rows, in anchor terms
    assert {e["y"] for e in roles(p, "coal_belt")} == {ANCHOR[1] + 3}       # y=48
    assert {e["y"] for e in roles(p, "manifold")} == {ANCHOR[1] + 5}       # y=50
    assert [e["y"] for e in roles(p, "pump")] == [ANCHOR[1] + 6]           # y=51
    # ONE tap pipe feeds BOTH boilers - the reason the column pitch is 4
    tap = roles(p, "tap")
    assert len(tap) == 1 and (tap[0]["x"], tap[0]["y"]) == (-32, 46)


def test_matches_the_after_snapshot():
    """Every planned entity really is in the operator's post-optimization snapshot at the
    identical position and direction (snapshots/after.json, read-only)."""
    f = HERE / "snapshots" / "after.json"
    if not f.is_file():
        return                                       # snapshot not checked out; skip
    live = {(e["n"], round(float(e["x"]), 1), round(float(e["y"]), 1), int(e.get("d", 0)))
            for e in json.load(f.open())["ents"]}
    p = P.plan_plant(4, water_hint=PUMP)
    missing = centres(p) - live
    assert not missing, "planned entities absent from the operator's own plant: %s" % (
        sorted(missing),)


def test_anchor_derivation_and_inverse():
    assert P.anchor_from_pump(*PUMP) == ANCHOR
    assert P.pump_tile(ANCHOR) == PUMP
    assert P.gap_x(ANCHOR, 0) == -32 and P.gap_x(ANCHOR, 1) == -28
    # footprint (4N+2) x 17, and the three reserved rects tile it exactly as measured
    assert P.bbox(ANCHOR, 2) == (-37, 35, -28, 51)
    r1, r2, r3 = P.reserved_rects(ANCHOR, 2)
    assert r1 == (-35, -29, 35, 48) and r2 == (-37, -36, 35, 48) and r3 == (-32, -28, 49, 51)
    x1, y1, x2, y2 = P.bbox(ANCHOR, 2)
    assert (x2 - x1 + 1, y2 - y1 + 1) == (4 * 2 + 2, 17)


def test_ratio_is_enforced_not_assumed():
    """1 boiler : 2 engines, exactly. The bot's _build_boiler_engine stacked a 3rd engine
    onto ONE boiler; principles.plant_ratio_ok flags exactly that."""
    assert P.columns_for(1) == 1 and P.columns_for(2) == 1
    assert P.columns_for(3) == 2 and P.columns_for(4) == 2 and P.columns_for(5) == 3
    for bad in (0, -2):
        try:
            P.columns_for(bad)
            raise AssertionError("n_engines=%r should be rejected" % (bad,))
        except P.PlantError:
            pass
    p = P.plan_plant(3, water_hint=PUMP)
    assert p["n_engines"] == 4 and p["n_engines_requested"] == 3
    assert any("rounded" in w for w in p["warnings"]), p["warnings"]
    assert len(roles(p, "engine")) == 2 * len(roles(p, "boiler"))
    # and validate() catches a hand-doctored orphan
    p["entities"].append({"entity": "steam-engine", "x": -35, "y": 25, "direction": 0,
                          "role": "engine", "column": 0, "stack": 2})
    errs = P.validate(p)["errors"]
    assert any("must be exactly 1:2" in e for e in errs), errs


def test_power_and_consumable_math():
    p = P.plan_plant(8, water_hint=PUMP)
    assert p["n_columns"] == 4
    assert p["power_MW"] == 7.2 and p["boiler_MW"] == 7.2      # 8*0.9 == 4*1.8, exactly
    assert p["coal_per_min"] == 4 * 27.0
    assert p["water_per_s"] == 4 * 60.0
    assert P.coal_intake(p)["demand_per_min"] == 108.0
    # 1 pump : 20 boilers : 40 engines = 36 MW; past that add a SECOND pump
    big = P.plan_plant(44, water_hint=PUMP)
    assert any("second pump" in w.lower() for w in big["warnings"]), big["warnings"]
    # a 50/50 splitter tap ceilings the plant: 120/min mined -> 60/min deliverable
    tight = P.plan_plant(6, water_hint=PUMP, coal_supply_per_min=120)
    assert any("coal:" in w for w in tight["warnings"]), tight["warnings"]
    fine = P.plan_plant(4, water_hint=PUMP, coal_supply_per_min=200)
    assert not any("coal:" in w for w in fine["warnings"]), fine["warnings"]


def test_single_column_still_gets_a_spine():
    """N=1 has no gap BETWEEN columns, but still needs the spine column east of it for the
    tap, the riser, the pump and the spur pole."""
    p = P.plan_plant(2, water_hint=PUMP)
    assert p["n_columns"] == 1
    assert len(roles(p, "tap")) == 1 and len(roles(p, "manifold")) == 1
    assert len(roles(p, "pole_spur")) == 1 and len(roles(p, "riser")) == 2
    assert len(roles(p, "coal_belt")) == 4                      # bx0-2 .. bx0+1
    assert P.validate(p)["ok"], P.validate(p)["errors"]


def test_trunk_extends_at_pitch_seven():
    p = P.plan_plant(4, water_hint=PUMP, trunk_to_y=12)
    ys = sorted(e["y"] for e in roles(p, "pole_trunk"))
    assert ys == [12, 19, 26, 33, 40], ys                       # measured trunk column
    assert {e["x"] for e in roles(p, "pole_trunk")} == {-36}     # straight, axis-aligned
    assert all(b - a == P.TRUNK_PITCH for a, b in zip(ys, ys[1:]))


# --------------------------------------------------------------------------- invariants
def test_key_tiles_are_floored_centres():
    """The buildplan keying contract. world.scan_tiles probes (tile+0.5) at radius 0.6 and
    reports floor(entity.position); buildplan._default_remove re-finds at radius 0.8. Key by
    top-left instead and a 3x2 boiler probes 1.58 tiles off its own centre - every re-apply
    would double-place it and every rollback would report it removed while it stood."""
    p = P.plan_plant(6, water_hint=PUMP)
    for e in p["entities"]:
        cx, cy = P.center(e["entity"], e["x"], e["y"])
        kx, ky = P.key_tile(e["entity"], e["x"], e["y"])
        assert (kx, ky) in P.footprint(e["entity"], e["x"], e["y"])
        d = math.hypot(kx + 0.5 - cx, ky + 0.5 - cy)
        assert d <= 0.6, "%s at (%d,%d): probe point is %.3f from its centre" % (
            e["entity"], e["x"], e["y"], d)
    keys = [tuple(t[:2]) for t in P.plan_tiles(p)]
    assert len(keys) == len(set(keys)) == len(p["entities"])


def test_validate_catches_a_dry_boiler():
    p = P.plan_plant(4, water_hint=PUMP)
    p["entities"] = [e for e in p["entities"] if e["role"] != "tap"]
    errs = P.validate(p)["errors"]
    assert sum("no water tap on either port tile" in e for e in errs) == 2, errs


def test_validate_catches_a_riser_that_ducks_nothing():
    """The 2-tile underground exists to pass the water UNDER the coal belt row. Collapse it
    onto the same side of the belt and the plant's water and fuel rows collide."""
    p = P.plan_plant(4, water_hint=PUMP)
    south = [e for e in roles(p, "riser") if e["direction"] == P.S][0]
    south["y"] -= 2                                     # now north of the coal belt row
    errs = P.validate(p)["errors"]
    assert any("ducks under nothing" in e for e in errs), errs
    q = P.plan_plant(4, water_hint=PUMP)
    a, b = sorted(roles(q, "riser"), key=lambda e: e["y"])
    a["direction"], b["direction"] = P.S, P.N          # openings swapped: nothing connects
    assert any("riser openings" in e for e in P.validate(q)["errors"])


def test_validate_catches_the_bots_dead_inserter():
    """bootstrap.coal_to_boiler put the fuel inserter's pickup on a PIPE tile; it moved
    nothing, ever. An inserter that does not pick off the coal belt is a hard error."""
    p = P.plan_plant(4, water_hint=PUMP)
    ins = roles(p, "inserter")[0]
    ins["x"] -= 1                                       # onto the boiler's west column:
    ins["y"] -= 2                                       # picks the tap pipe row, drops in air
    errs = P.validate(p)["errors"]
    assert any("picks up from" in e for e in errs), errs
    assert any("drops on" in e and "not a boiler" in e for e in errs), errs
    # and a rotated inserter picks from the wrong side entirely
    q = P.plan_plant(4, water_hint=PUMP)
    roles(q, "inserter")[0]["direction"] = P.N
    assert any("pickup side" in e for e in P.validate(q)["errors"])


def test_validate_catches_a_split_pole_network():
    """Script-placed poles do NOT auto-connect, so a lattice that is geometrically split
    never heals. Reject it at PLAN time."""
    p = P.plan_plant(4, water_hint=PUMP)
    for e in roles(p, "pole_trunk"):
        e["x"] -= 12                                    # 16 tiles from the spur: wire 7.5
    errs = P.validate(p)["errors"]
    assert any("separate electric networks" in e for e in errs), errs


def test_validate_catches_footprint_and_pump_errors():
    p = P.plan_plant(4, water_hint=PUMP)
    roles(p, "pump")[0]["y"] -= 1                       # output no longer meets the manifold
    errs = P.validate(p)["errors"]
    assert any("not a manifold pipe" in e for e in errs), errs
    q = P.plan_plant(4, water_hint=PUMP)
    roles(q, "engine")[0]["y"] += 1                     # walk one engine into its neighbour
    assert any("footprint collision" in e for e in P.validate(q)["errors"])


def test_every_engine_is_covered_by_one_spur_pole():
    """The measured claim: ONE pole in the gap column covers all FOUR engines of the pair."""
    p = P.plan_plant(4, water_hint=PUMP)
    spur = roles(p, "pole_spur")[0]
    assert (spur["x"], spur["y"]) == (-32, 40)
    covered = [e for e in roles(p, "engine")
               if P._supplies(spur, "small-electric-pole", e)]
    assert len(covered) == 4, [(e["x"], e["y"]) for e in covered]
    # and at 8 columns every engine is still covered, by max(1, N-1) spur poles at pitch 4
    big = P.plan_plant(16, water_hint=PUMP)
    poles = roles(big, "pole_spur") + roles(big, "pole_trunk")
    assert len(roles(big, "pole_spur")) == big["n_columns"] - 1
    for e in roles(big, "engine"):
        assert any(P._supplies(q, "small-electric-pole", e) for q in poles), (e["x"], e["y"])
    assert P.validate(big)["ok"], P.validate(big)["errors"]


def test_coal_intake_is_the_feeder_column_handoff():
    p = P.plan_plant(4, water_hint=PUMP)
    i = P.coal_intake(p)
    assert i["tile"] == (-37, 47) and i["direction"] == P.S      # measured spur's last tile
    assert i["corner"] == (-37, 48)                              # = the coal row's west end
    belts = {(e["x"], e["y"]) for e in roles(p, "coal_belt")}
    assert i["corner"] in belts and i["tile"] not in belts


# --------------------------------------------------------------------------- scaling
def test_scale_extends_without_moving_anything():
    base = P.plan_plant(4, water_hint=PUMP)
    r = P.scale(base, 2)
    assert r["added_columns"] == 1 and r["plan"]["n_columns"] == 3
    # nothing existing moved: every base entity is present, identical, in the new plan
    sig = lambda e: (e["entity"], e["x"], e["y"], e["direction"])
    assert {sig(e) for e in base["entities"]} <= {sig(e) for e in r["plan"]["entities"]}
    assert len(r["kept"]) == len(base["entities"])
    # the pump, the riser, the feeder column and the pole trunk are UNTOUCHED
    for role in ("pump", "riser", "pole_trunk"):
        assert {sig(e) for e in roles(base, role)} == {sig(e) for e in roles(r["plan"], role)}
    # the delta is exactly the documented per-column extension (spec section 6)
    assert r["bom_delta"] == {"boiler": 1, "steam-engine": 2, "burner-inserter": 1,
                              "pipe": 5, "transport-belt": 4,
                              "small-electric-pole": 1}, r["bom_delta"]
    assert len(r["delta"]) == 14
    # 1 new gap tap + 4 new manifold pipes = the 5 pipes above
    assert len([e for e in r["delta"] if e["role"] == "tap"]) == 1
    assert len([e for e in r["delta"] if e["role"] == "manifold"]) == 4
    assert P.validate(r["plan"])["ok"], P.validate(r["plan"])["errors"]
    assert round(r["plan"]["power_MW"] - base["power_MW"], 6) == 1.8   # +1.8 MW/column


def test_scale_grows_east_only_and_repeatedly():
    base = P.plan_plant(4, water_hint=PUMP)
    p = base
    for _ in range(3):
        p = P.scale(p, 2)["plan"]
    assert p["n_columns"] == 5 and p["n_engines"] == 10
    assert p["anchor"] == base["anchor"] and p["pump"] == base["pump"]
    bx1, by1, bx2, by2 = base["bbox"]
    nx1, ny1, nx2, ny2 = p["bbox"]
    assert (nx1, ny1, ny2) == (bx1, by1, by2), "the plant grew somewhere other than east"
    assert nx2 == bx2 + 4 * 3
    assert P.validate(p)["ok"], P.validate(p)["errors"]


def test_scale_rejects_a_rebuild():
    base = P.plan_plant(4, water_hint=PUMP)
    for bad in (0, -1):
        try:
            P.scale(base, bad)
            raise AssertionError("n_more=%r should be rejected" % (bad,))
        except P.PlantError:
            pass
    moved = json.loads(json.dumps(base))
    moved["entities"][0]["x"] += 1              # pretend the base sat one tile east
    try:
        P.scale(moved, 2)
        raise AssertionError("scale() must refuse to MOVE an existing entity")
    except P.PlantError as e:
        assert "MOVE" in str(e), e


# --------------------------------------------------------------------------- siting
def test_site_valid_predicate():
    t = lake_terrain()
    ok, why = P.site_valid(t, -32, 51, 2)
    assert ok, why
    # no water frontage -> not a shore
    assert not P.site_valid(t, -32, 45, 2)[0]
    # the pump tile itself must be land
    assert not P.site_valid(t, -32, 55, 2)[0]
    # a single water tile inside R1 kills the site (this is how the operator's naive
    # alternatives fail: the lake's narrow tip puts water inside the machine block)
    bad = lake_terrain(extra_water=[(-33, 40)])
    ok2, why2 = P.site_valid(bad, -32, 51, 2)
    assert not ok2 and any("R1" in w for w in why2), why2
    # boilers and engines are NOT ore-safe: a resource tile is a refusal, not a preference
    ore = lake_terrain()
    ore.resource.add((-30, 44))
    assert not P.site_valid(ore, -32, 51, 2)[0]
    # a cliff cannot be mined without explosives
    cliff = lake_terrain()
    cliff.cliff.add((-37, 48))
    assert not P.site_valid(cliff, -32, 51, 2)[0]
    # unscanned ground is refused, never assumed clear
    narrow = P.Terrain((-34, 44, -28, 55),
                       water={(x, y) for x in range(-34, -27) for y in range(52, 56)})
    assert not P.site_valid(narrow, -32, 51, 2)[0]
    # a wider plant needs more shore: N=8 walks R1/R3 east past the lake's scanned edge
    small = lake_terrain(x1=-40, x2=-25)
    assert P.site_valid(small, -32, 51, 2)[0]
    assert not P.site_valid(small, -32, 51, 8)[0]


def test_site_plant_scores_toward_the_coal():
    """Spec section 7: the plant is sited at its FUEL, not at its LOAD. Coal must be belted
    (1 tile each); power leaves over a trunk at 1 pole per 7 tiles."""
    t = lake_terrain()
    cands = P.site_plant((0, 0), terrain=t, n_engines=4, coal_tap=(-37, 20), limit=5)
    assert cands, "a flat shore should yield candidates"
    assert cands[0]["pump"] == PUMP, cands[0]
    assert cands[0]["anchor"] == ANCHOR
    assert [c["cost"] for c in cands] == sorted(c["cost"] for c in cands)
    # pull the coal tap 10 tiles east and the plant follows it, not the base at (0,0)
    east = P.site_plant((0, 0), terrain=t, n_engines=4, coal_tap=(-27, 20), limit=1)
    assert east[0]["pump"] == (-22, 51), east[0]
    # with no coal tap at all it falls back to the electrical destination
    near = P.site_plant((-20, 20), terrain=t, n_engines=4, limit=1)
    assert near[0]["pump"][0] > PUMP[0]


def test_site_plant_penalises_clearance_violations():
    t = lake_terrain()
    avoid = [{"kind": "smelter_array", "bbox": (-50, 20, -45, 34)}]   # the west end
    cands = P.site_plant((0, 0), terrain=t, n_engines=4, coal_tap=(-19, 20), avoid=avoid,
                         limit=64)
    top = cands[0]
    assert not top["clearance"], top
    assert top["pump"] == (-14, 51), top          # the clean site nearest the coal tap
    viol = [c for c in cands if c["clearance"]]
    assert viol and all(c["cost"] >= P.CLEARANCE_PENALTY for c in viol)
    assert viol[0]["clearance"][0]["need"] == P.MIN_CLEARANCE["smelter_array"] == 16
    assert all(c["clearance"][0]["got"] < 16 for c in viol)
    # plan_plant surfaces the same violation as a warning rather than silently building
    p = P.plan_plant(4, water_hint=PUMP, terrain=t, avoid=avoid)
    assert any("clearance to smelter_array" in w for w in p["warnings"]), p["warnings"]


def test_plan_plant_refuses_a_bad_or_unknown_site():
    t = lake_terrain(extra_water=[(-33, 40)])
    try:
        P.plan_plant(4, water_hint=PUMP, terrain=t)
        raise AssertionError("a site failing the predicate must be refused")
    except P.PlantError as e:
        assert "siting predicate" in str(e)
    try:
        P.plan_plant(4)
        raise AssertionError("no water_hint, no terrain, no near -> refuse")
    except P.PlantError as e:
        assert "water_hint" in str(e)
    # and with terrain but no shore anywhere, it says so rather than guessing
    dry = P.Terrain((-50, 30, -10, 60))
    try:
        P.plan_plant(4, terrain=dry, near=(0, 0))
        raise AssertionError("no shore -> refuse")
    except P.PlantError as e:
        assert "no shore site" in str(e)


def test_parse_shore():
    t = P.parse_shore("W:-1,2;-1,3|C:5,5|R:7,8;7,9|B:", (-10, -10, 10, 10))
    assert t.water == {(-1, 2), (-1, 3)} and t.cliff == {(5, 5)}
    assert t.resource == {(7, 8), (7, 9)} and t.blocked == set()
    assert t.is_water(-1, 2) and not t.is_water(0, 0)
    assert t.is_clear(0, 0) and not t.is_clear(5, 5) and not t.is_clear(7, 8)
    assert not t.known(99, 99) and not t.is_clear(99, 99)
    assert P.parse_shore("", (0, 0, 1, 1)).water == set()


def test_scan_shore_is_read_only():
    """One /sc, no create/destroy/rotate anywhere in it, and it round-trips a Terrain."""
    fake = FakeRcon()
    payload = "W:-33,52;-32,52;-31,52|C:|R:|B:-33,45"
    fake.script = [("get_tile", fake.text(payload))]
    orig, rcon.run = rcon.run, fake
    try:
        t = P.scan_shore(-32, 48, radius=3)
    finally:
        rcon.run = orig
    assert t.water == {(-33, 52), (-32, 52), (-31, 52)}
    assert t.blocked == {(-33, 45)} and t.bbox == (-35, 45, -29, 51)
    body = " ".join(fake.calls)
    for banned in ("create_entity", "destroy(", ".rotate", "walking_state", "set_recipe"):
        assert banned not in body, banned


# --------------------------------------------------------------------------- verify
def test_verify_gates_on_fluid_and_energy():
    p = P.plan_plant(4, water_hint=PUMP)
    ok, detail = P.verify(p, state=live_state(p))
    assert ok, detail
    assert "pump w=100" in detail and "coal dead-end items=8" in detail
    # a DRY boiler fails, and says which one
    dry = live_state(p, {("boiler", -29.5, 46.0): (0, 0, -1)})
    ok, detail = P.verify(p, state=dry)
    assert not ok and "boiler@-29.5,46.0 water=0 (dry)" in detail, detail
    # an UNCONNECTED pump reads 0 - the classic false green
    ok, detail = P.verify(p, state=live_state(p, {("offshore-pump", -31.5, 51.5): (0, -1, -1)}))
    assert not ok and "water=0 < 100" in detail
    # an engine with no energy fails
    ok, _ = P.verify(p, state=live_state(p, {("steam-engine", -33.5, 42.5): (0, 0, 535)}))
    assert not ok
    # a MISSING entity is a failure, never a pass
    st = live_state(p)
    st.pop(("boiler", -33.5, 46.0))
    ok, detail = P.verify(p, state=st)
    assert not ok and "MISSING" in detail


def test_verify_warns_without_failing_on_a_cold_but_wet_plant():
    """A cold plant must not be torn down; a dry one must. Steam level, generated_last_tick,
    inserter status and coal on the belt are WARNINGS."""
    p = P.plan_plant(4, water_hint=PUMP)
    st = live_state(p)
    st[("boiler", -33.5, 46.0)] = (200, 12, -1)          # wet but cold
    st[("steam-engine", -33.5, 37.5)] = (5, 0, 535)      # energised, not yet generating
    st[("burner-inserter", -33.5, 47.5)] = (2, 0, -1)    # not 1 or 36
    st[("transport-belt", -29.5, 48.5)] = (0, -1, -1)    # spur not delivering yet
    ok, detail = P.verify(p, state=st)
    assert ok, detail
    assert "warn:" in detail and "FAIL" not in detail
    for want in ("cold or under-fuelled", "generated_last_tick=0", "status=2",
                 "no coal on the belt"):
        assert want in detail, (want, detail)


def test_verify_fails_a_split_grid():
    """P2: a plant whose poles/engines span two networks has stranded generation. place()
    has already wired every pair explicitly by the time verify() runs, so a surviving split
    is a real defect, not a transient - it FAILS, and buildplan tears the plant back out."""
    p = P.plan_plant(4, water_hint=PUMP)
    st = live_state(p, {("steam-engine", -29.5, 37.5): (13547, 1435, 999)})
    ok, detail = P.verify(p, state=st)
    assert not ok and "2 electric networks" in detail and "get_wire_connector" in detail
    # ... and it is downgradeable for a plant deliberately built before its trunk exists
    ok, detail = P.verify(p, state=st, require_single_network=False)
    assert ok and "warn:" in detail and "2 electric networks" in detail


def test_a_tier_swap_cannot_silently_disable_the_grid_check():
    """The checks used to be keyed on the literals 'small-electric-pole'/'transport-belt',
    so ANY belt= or pole= argument deleted them: a medium-pole plant with its poles on one
    network and its engines on another verified clean, with no warning at all."""
    p = P.plan_plant(4, water_hint=PUMP, belt="fast-transport-belt",
                     pole="medium-electric-pole")
    st = {}
    for name, cx, cy in P._check_spec(p):
        st[(name, cx, cy)] = {"boiler": (200, 199, -1),
                              "steam-engine": (13547, 1435, 535),
                              "offshore-pump": (100, -1, -1),
                              "burner-inserter": (36, 1, -1),
                              "fast-transport-belt": (0, -1, -1),
                              "medium-electric-pole": (999, -1, -1)}[name]
    ok, detail = P.verify(p, state=st)
    assert not ok, detail
    assert "2 electric networks" in detail                 # pole 999 vs engine 535
    assert "no coal on the belt" in detail                 # the belt check survives too
    # and the live read dispatches on TYPE, so no branch falls through to a raw property
    lua = P.verify_lua(P._check_spec(p))
    assert "e.type" in lua and "pcall(function() return e.electric_network_id end)" in lua
    assert "n=='transport-belt'" not in lua and "n=='boiler'" not in lua


def test_verify_lua_is_read_only_and_parses_back():
    p = P.plan_plant(4, water_hint=PUMP)
    spec = P._check_spec(p)
    assert ("offshore-pump", -31.5, 51.5) in spec
    assert ("transport-belt", -29.5, 48.5) in spec        # only the dead-end tile
    assert sum(1 for s in spec if s[0] == "transport-belt") == 1
    lua = P.verify_lua(spec)
    assert len(lua) < P.SPEC_BUDGET, len(lua)
    for banned in ("create_entity", "destroy(", ".rotate", "walking_state", "on_event"):
        assert banned not in lua, banned
    raw = "|".join("%s,%.1f,%.1f,1,2,3" % s for s in spec)
    got = P.parse_state(raw)
    assert len(got) == len(spec) and got[spec[0]] == (1, 2, 3)
    assert P.parse_state("") == {} and P.parse_state("garbage") == {}


@_with_ctx()
def test_read_state_batches_under_the_rcon_cap(ctx):
    p = P.plan_plant(60, water_hint=PUMP)                 # 30 columns -> ~120 checks
    spec = P._check_spec(p)
    assert len(P.verify_lua(spec)) > P.SPEC_BUDGET, "expected this plant to need batching"
    calls = []

    def _run(cmd, timeout=10.0):
        calls.append(cmd)
        body = re.search(r"\[==\[(.*?)\]==\]", cmd, re.S).group(1)
        return "|".join("%s,4,5,6" % rec for rec in body.split(";") if rec)

    P.read_state = ctx._orig[11]                          # restore the real read_state
    orig, rcon.run = rcon.run, _run
    try:
        st = P.read_state(p)
    finally:
        rcon.run = orig
    assert len(calls) > 1, "a 30-column plant must be read in batches"
    assert all(len(c) <= P.SPEC_BUDGET for c in calls), [len(c) for c in calls]
    assert len(st) == len(spec)


# --------------------------------------------------------------------------- wiring
def test_wire_pairs_covers_every_pole_in_one_component():
    """GOTCHAS 2026-08-30: script-placed poles do NOT auto-connect - and the pair that was
    measured broken was 4.0 apart, which is EXACTLY this template's spur pitch. So every
    planned pole must appear in an explicit pair, and the pairs must span one component."""
    for n_eng, ty in ((4, None), (2, None), (16, None), (4, 12), (8, 5)):
        p = P.plan_plant(n_eng, water_hint=PUMP, trunk_to_y=ty)
        poles = [(e["x"], e["y"]) for e in p["entities"] if "pole" in e["role"]]
        pairs = P.wire_pairs(p)
        assert pairs, (n_eng, ty)
        seen = {t for pr in pairs for t in pr}
        assert seen == set(poles), (sorted(set(poles) - seen), sorted(seen - set(poles)))
        # the explicit pairs alone must connect the whole set (union-find over the pairs)
        idx = {t: i for i, t in enumerate(poles)}
        parent = list(range(len(poles)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for a, b in pairs:
            parent[find(idx[a])] = find(idx[b])
        assert len({find(i) for i in range(len(poles))}) == 1, (n_eng, ty)
        # no pair may exceed the wire reach, and no pole may saturate its connectors
        reach = P.mine_layout.POLES[p["params"]["pole"]]["wire"]
        deg = {}
        for a, b in pairs:
            assert math.dist(a, b) <= reach, (a, b)
            deg[a] = deg.get(a, 0) + 1
            deg[b] = deg.get(b, 0) + 1
        assert max(deg.values()) <= P.MAX_POLE_DEGREE, deg


def test_wire_lua_is_the_gotchas_incantation_and_places_nothing():
    p = P.plan_plant(8, water_hint=PUMP)
    cmds = P.wire_lua(P.wire_pairs(p), p["params"]["pole"])
    assert cmds and all(len(c) <= P.SPEC_BUDGET for c in cmds)
    body = " ".join(cmds)
    assert "get_wire_connector(W,true)" in body
    assert "defines.wire_connector_id.pole_copper" in body
    assert "connect_to(cq,false)" in body
    for banned in ("create_entity", "destroy(", ".rotate", "walking_state", "on_event",
                   "on_nth_tick"):
        assert banned not in body, banned
    # the pole centres, not the tiles: a 2x2 pole tier would miss its own body otherwise
    assert "-31.5,40.5" in body and "-35.5,40.5" in body
    # batching is by real byte length
    big = P.wire_lua([((x, 0), (x + 4, 0)) for x in range(0, 4000, 4)])
    assert len(big) > 1 and all(len(c) <= P.SPEC_BUDGET for c in big)


@_with_ctx()
def test_place_wires_the_poles_and_verifies_by_network_id(ctx):
    """Placement does not imply connection. place() must wire and then READ BACK."""
    p = P.plan_plant(4, water_hint=PUMP)
    ctx.state = live_state(p)
    rec = P.to_buildplan(p, scan_tick=900)
    out = P.place(rec, [tuple(t[:2]) for t in P.plan_tiles(p)])
    assert len(out["placed"]) == 27
    w = out["wired"]
    assert w is not None and w["made"] == len(P.wire_pairs(p)) >= 1
    assert w["networks"] == {"poles": [535], "engines": [535], "all": [535]}
    assert w["ok"] is True
    assert len(ctx.wirecmds) == 1


@_with_ctx()
def test_a_split_grid_that_survives_rewiring_rolls_the_plant_back(ctx):
    """The retry loop REPAIRS (re-wire is idempotent); a split that survives it is torn out
    rather than left standing with half its generation stranded."""
    B.register(P.KIND, place=P.place, verify=P.verify_record, remove=ctx.remover())
    p = P.plan_plant(4, water_hint=PUMP)
    ctx.state = live_state(p, {("small-electric-pole", -31.5, 40.5): (999, -1, -1)})
    ctx.fake.script = [
        ("find_entities_filtered", ctx.fake.scan([])),        # probe
        ("game.tick", "1000"),
    ]
    rec = P.build(p, scan_tick=900, tries=3, delay=0)
    assert rec["status"] == "failed"
    assert "2 electric networks" in rec["verify"]["check"]["detail"]
    assert len(ctx.wirecmds) >= 3, "each verify attempt must re-wire before giving up"
    assert rec["verify"]["rollback"]["removed"] == 27


# --------------------------------------------------------------------------- build
@_with_ctx()
def test_build_places_verifies_and_records(ctx):
    p = P.plan_plant(4, water_hint=PUMP)
    ctx.state = live_state(p)
    ctx.fake.script = [
        ("find_entities_filtered", ctx.fake.scan([])),        # probe: nothing built yet
        ("game.tick", "1000"),                                # verify.at_tick
        ("find_entities_filtered", ctx.fake.scan([])),        # absorb (self-write)
    ]
    rec = P.build(p, scan_tick=900, tries=1, delay=0)
    assert rec["status"] == "verified", rec["verify"]
    assert rec["verify"]["check"]["ok"] is True
    assert len(rec["verify"]["placed"]) == 27
    assert len(ctx.placed) == 27
    # water first, machines next, fuel and power export last (a partial run leaves the
    # plant CLOSER to working, not further)
    order = [n for n, _x, _y, _d in ctx.placed]
    assert order[0] == "offshore-pump"
    assert order.index("boiler") > order.index("pipe-to-ground")
    assert order.index("steam-engine") > order.index("boiler")
    assert order.index("burner-inserter") > order.index("transport-belt")
    assert order[-1] == "small-electric-pole"
    # ONE clearspace pass over the whole zone, then every placement with clear=0
    assert len(ctx.cleared) == 1
    assert len(ctx.fuelled) == 1 and ctx.fuelled[0][1] == 25
    assert set(ctx.built) == {tuple(t) for t in rec["verify"]["placed"]}


@_with_ctx()
def test_verification_failure_rolls_back_everything_it_placed(ctx):
    """Build Law 2: if the result is nothing, remove what you built - in the SAME pass."""
    B.register(P.KIND, place=P.place, verify=P.verify_record, remove=ctx.remover())
    p = P.plan_plant(4, water_hint=PUMP)
    ctx.state = live_state(p, {("boiler", -33.5, 46.0): (0, 0, -1),
                                 ("boiler", -29.5, 46.0): (0, 0, -1)})
    ctx.fake.script = [
        ("find_entities_filtered", ctx.fake.scan([])),        # probe
        ("game.tick", "1000"),                                # verify.at_tick
    ]
    rec = P.build(p, scan_tick=900, tries=1, delay=0)
    assert rec["status"] == "failed", rec["status"]
    assert rec["verify"]["check"]["ok"] is False
    assert "dry" in rec["verify"]["check"]["detail"]
    assert rec["verify"]["rollback"] == {"removed": 27, "not_found": 0}
    # the rollback's scope is EXACTLY what this plan placed - never area-based
    assert len(ctx.removed) == 1 and len(ctx.removed[0][1]) == 27
    assert set(ctx.removed[0][1]) == {tuple(t[:2]) for t in P.plan_tiles(p)}
    assert rec["verify"]["placed"] == [], "verify.placed must be emptied by the rollback"
    assert ctx.built == set(), "the built ledger must forget what we tore out"


@_with_ctx()
def test_build_is_idempotent_and_only_fills_the_gaps(ctx):
    """A re-apply hands place_fn ONLY the tiles still missing (buildplan probes first)."""
    p = P.plan_plant(4, water_hint=PUMP)
    ctx.state = live_state(p)
    already = [{"n": e["entity"], "x": k[0], "y": k[1], "d": e["direction"]}
               for e, k in ((e, P.key_tile(e["entity"], e["x"], e["y"]))
                            for e in p["entities"]) if e["role"] in ("pump", "manifold")]
    ctx.fake.script = [
        ("find_entities_filtered", ctx.fake.scan(already)),
        ("game.tick", "1000"),
        ("find_entities_filtered", ctx.fake.scan([])),
    ]
    rec = P.build(p, scan_tick=900, tries=1, delay=0)
    assert rec["status"] == "verified"
    assert len(ctx.placed) == 27 - len(already) == 21
    assert {n for n, _x, _y, _d in ctx.placed}.isdisjoint({"offshore-pump"})
    assert len(rec["verify"]["already"]) == len(already)


@_with_ctx()
def test_build_refuses_an_operator_owned_site(ctx):
    """Build Law 3: >=25% operator-protected tiles means he deleted this on purpose."""
    p = P.plan_plant(4, water_hint=PUMP)
    ctx.protected = {tuple(t[:2]) for t in P.plan_tiles(p)[:12]}
    rec = P.build(p, scan_tick=900, tries=1, delay=0)
    assert rec["status"] == "superseded"
    assert "OPERATOR-OWNED ROUTE" in rec["verify"]["refused"]
    assert ctx.placed == [], "nothing may be placed on an operator-owned route"


@_with_ctx()
def test_build_refuses_while_the_operator_is_connected(ctx):
    ctx.operator = True
    p = P.plan_plant(4, water_hint=PUMP)
    rec = P.build(p, scan_tick=900, tries=1, delay=0)
    assert "OPERATOR PRESENT" in rec["verify"]["refused"]
    assert ctx.placed == [] and ctx.cleared == []


@_with_ctx()
def test_build_refuses_an_invalid_plan_before_touching_anything(ctx):
    p = P.plan_plant(4, water_hint=PUMP)
    p["entities"] = [e for e in p["entities"] if e["role"] != "tap"]     # dry boilers
    try:
        P.build(p, scan_tick=900, tries=1, delay=0)
        raise AssertionError("an invalid plan must never reach buildplan")
    except P.PlantError as e:
        assert "invalid plant plan" in str(e)
    assert ctx.placed == [] and B.plans() == []


@_with_ctx()
def test_scaled_plan_builds_only_the_delta(ctx):
    """scale() + build(): buildplan's probe finds the existing plant and places only the
    new column - the extension is a refill, not a rebuild."""
    base = P.plan_plant(4, water_hint=PUMP)
    r = P.scale(base, 2)
    plan = r["plan"]
    ctx.state = live_state(plan)
    existing = [{"n": e["entity"], "x": P.key_tile(e["entity"], e["x"], e["y"])[0],
                 "y": P.key_tile(e["entity"], e["x"], e["y"])[1], "d": e["direction"]}
                for e in r["kept"]]
    ctx.fake.script = [
        ("find_entities_filtered", ctx.fake.scan(existing)),
        ("game.tick", "1200"),
        ("find_entities_filtered", ctx.fake.scan([])),
    ]
    rec = P.build(plan, scan_tick=1100, tries=1, delay=0)
    assert rec["status"] == "verified"
    assert len(ctx.placed) == len(r["delta"]) == 14
    assert {(n, x, y) for n, x, y, _d in ctx.placed} == {
        (e["entity"], e["x"], e["y"]) for e in r["delta"]}


@_with_ctx()
def test_registered_kind_lets_resume_reverify_a_crash(ctx):
    P.register()
    p = P.plan_plant(4, water_hint=PUMP)
    ctx.state = live_state(p)
    rec = P.to_buildplan(p, scan_tick=900)
    rec["status"] = "applying"                            # simulate a crash mid-apply
    rec["verify"]["placed"] = [list(t[:2]) for t in P.plan_tiles(p)]
    B.save(rec)
    ctx.fake.script = [("find_entities_filtered", ctx.fake.scan([]))]   # absorb
    out = B.resume(tries=1, delay=0)
    assert len(out) == 1 and out[0]["status"] == "verified", out[0]["verify"]


# --------------------------------------------------------------------------- output
def test_orders_and_ghosts():
    p = P.plan_plant(4, water_hint=PUMP)
    orders = P.to_orders(p)
    assert len(orders) == 27 and orders[0]["kind"] == "place"
    assert orders[0]["args"] == {"name": "offshore-pump", "tile_x": -32, "tile_y": 51,
                                 "direction": 8}
    ghosts = P.to_ghosts(p)
    assert {(g["name"], round(g["x"], 1), round(g["y"], 1), g["dir"])
            for g in ghosts} == set(MEASURED)
    b = P.bom(p)
    assert sum(b.values()) == 27
    assert b == {"boiler": 2, "steam-engine": 4, "burner-inserter": 2, "pipe": 6,
                 "pipe-to-ground": 2, "offshore-pump": 1, "transport-belt": 8,
                 "small-electric-pole": 2}


def test_record_roundtrip_keeps_the_plan():
    p = P.plan_plant(4, water_hint=PUMP)
    rec = {"id": "p-test", "kind": P.KIND,
           "args": {"plant": json.loads(json.dumps(P._jsonable(p)))}}
    back = P.from_record(rec)
    assert back["anchor"] == p["anchor"] and back["pump"] == p["pump"]
    assert len(back["entities"]) == len(p["entities"])
    assert centres(back) == centres(p)
    try:
        P.from_record({"id": "x", "args": {}})
        raise AssertionError("a record with no plant must raise")
    except P.PlantError:
        pass


# --------------------------------------------------------------------------- runner
TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    passed, failed = 0, []
    for t in TESTS:
        try:
            t()
            passed += 1
            print("  pass  %s" % t.__name__)
        except Exception:
            failed.append(t.__name__)
            print("  FAIL  %s" % t.__name__)
            traceback.print_exc()
    print("\n%d passed, %d failed (%d total)" % (passed, len(failed), len(TESTS)))
    raise SystemExit(1 if failed else 0)
