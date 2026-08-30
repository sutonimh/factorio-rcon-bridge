#!/usr/bin/env python3
"""Offline tests for the phase-0 planner pipeline + the controller's INVARIANT issue class.

Run with either:
    python3 test_planner_integration.py
    python3 -m pytest test_planner_integration.py

NOTHING here touches the live server. `rcon.run` is replaced by a raiser for the whole
session, so a stray RCON call is a hard test failure rather than a write to Seth's base, and
every planner/bootstrap entry point a stage would call is stubbed. Two kinds of test:

  STRUCTURAL   AST/source assertions that pin the properties the integration exists to give:
               no legacy ad-hoc builder is reachable from the phase program, every placing
               stage calls a gate, the controller's audit battery contains no mutating Lua,
               and BUILDER_ENABLED=0 safe mode is still in play().
  BEHAVIOURAL  each stage driven with fakes: gate blocks -> nothing is built; the build fails
               -> nothing is recorded; a duplicate lane is never laid beside its predecessor.
"""
import ast
import json
import pathlib
import shutil
import tempfile
import traceback

import rcon

# ---- the whole module is offline. Install the raiser BEFORE importing anything that might
# probe the server at import time (nothing does - this is the guard that proves it).
_REAL_RCON = rcon.run


def _no_rcon(cmd, timeout=10.0):
    raise AssertionError("offline test issued RCON: %s" % str(cmd)[:160])


rcon.run = _no_rcon

import build_gates                                                        # noqa: E402
import buildplan                                                          # noqa: E402
import controller                                                         # noqa: E402
import triage                                                             # noqa: E402
import planner                                                            # noqa: E402
import supply_planner                                                     # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent


# --------------------------------------------------------------------------- harness
class Log:
    """Collects status.log lines instead of writing the autopilot log."""

    def __init__(self):
        self.lines = []

    def __call__(self, msg):
        self.lines.append(str(msg))

    def has(self, *subs):
        return any(all(s in ln for s in subs) for ln in self.lines)


class Lessons:
    def __init__(self):
        self.rows = []

    def add(self, **kw):
        self.rows.append(kw)


class Ctx:
    """tmp plan/lane registries + a captured log + patched module attributes."""

    def __init__(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="planner-int-"))
        self.log = Log()
        self.lessons = Lessons()
        self._saved = []
        self.patch(buildplan, "PLANS_DIR", self.tmp / "plans")
        self.patch(buildplan, "DIRTY_PATH", self.tmp / "plans" / "_dirty.json")
        self.patch(supply_planner, "LANES_PATH", self.tmp / "supply-lanes.json")
        self.patch(planner, "PHASE_FILE", self.tmp / "phase.json")
        # buildplan's four laws are exercised by test_buildplan; here they must not reach
        # bootstrap's real ledger files.
        self.patch(buildplan, "_protected", lambda: set())
        self.patch(buildplan, "_operator_present", lambda: False)
        self.patch(buildplan, "_record_built", lambda tiles: None)
        self.patch(buildplan, "_forget_built", lambda tiles: None)
        self.patch(planner.status, "log", self.log)
        self.patch(planner, "lessons", self.lessons)
        self.patch(controller, "lessons", self.lessons)
        self.patch(planner.A, "purpose", lambda *a, **k: None)
        self.patch(planner.B, "operator_present", lambda: False)
        self.patch(controller.B, "operator_present", lambda: False)
        planner.gate_reset()

    def patch(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def state(self, **kw):
        """A synthetic build_gates census; the REAL gate logic then runs against it."""
        st = {"tick": 1, "counts": {}, "status": {}, "recipes": {}, "flows": {},
              "ghosts": {}, "networks": 0}
        st.update(kw)
        self.patch(planner.build_gates, "sense", lambda force=False, **k: st)
        planner.gate_reset()
        return st

    def plan_file(self, pid, status_="verified", **kw):
        """Write a buildplan record straight to the tmp registry."""
        d = pathlib.Path(buildplan.PLANS_DIR)
        d.mkdir(parents=True, exist_ok=True)
        rec = {"id": pid, "kind": "test", "status": status_, "tiles": [], "args": {},
               "verify": {}}
        rec.update(kw)
        (d / ("%s.json" % pid)).write_text(json.dumps(rec))
        return rec

    def close(self):
        for obj, name, value in reversed(self._saved):
            setattr(obj, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)
        planner.gate_reset()


def _with_ctx(fn):
    def wrapper():
        ctx = Ctx()
        try:
            fn(ctx)
        finally:
            ctx.close()
    wrapper.__name__ = fn.__name__
    return wrapper


def _src():
    return (HERE / "planner.py").read_text()


def _fn_nodes(path):
    tree = ast.parse((HERE / path).read_text())
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def _calls(node):
    """Every call in `node` as a dotted name string ('B.power', 'gate', ...)."""
    out = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if isinstance(f, ast.Name):
            out.append(f.id)
        elif isinstance(f, ast.Attribute):
            parts, cur = [f.attr], f.value
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            out.append(".".join(reversed(parts)))
    return out


# ===================================================== STRUCTURAL: the legacy builders are gone
# The four builders the operator's optimization condemned. Each is left standing in
# bootstrap.py on purpose (operator2's command catalog still reaches them); what must be true
# is that the PHASE PROGRAM no longer calls them.
LEGACY = ("B.build_mine_outpost", "B.connect_mine_to_array", "B.power_row", "B.coal_to_boiler",
          "B.build_belt_supply",          # calls connect_mine_to_array
          "B.ensure_lanes",               # its re-lay path calls connect_mine_to_array
          "B.power",                      # superseded by plant_planner
          "B.electrify_mines",            # superseded by upgrade_to_electric
          "builds_v2.mine_outpost_v2")

PHASE_FNS = ("phase0",) + tuple("stage_" + s for s in
                                ("world", "plant", "spine", "red_science", "mines", "arrays",
                                 "array_grid", "ore_lanes", "coal_lane", "science",
                                 "electrify", "oil"))


def test_phase_program_calls_no_legacy_builder():
    fns = _fn_nodes("planner.py")
    for name in PHASE_FNS:
        assert name in fns, "phase program lost %s" % name
        called = set(_calls(fns[name]))
        bad = called & set(LEGACY)
        assert not bad, "%s still calls the superseded builder(s) %s" % (name, sorted(bad))


def test_builds_v2_no_longer_imported_by_the_planner():
    tree = ast.parse(_src())
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            names.add(n.module)
    assert "builds_v2" not in names


BUILD_ENTRY_POINTS = {"mine_planner_v2.build", "mine_planner_v2.upgrade_to_electric",
                      "plant_planner.build", "power_planner.apply", "supply_planner.build",
                      "B.build_smelter_array", "B.red_science", "B.smelting_base",
                      "B.automate_green_science", "B.setup_science_io",
                      "B.ensure_science_cells"}
# gate_largest IS a gate: one real gate() call per n, and 0 when every one of them refused. A
# stage may reach a build through it and through nothing else.
GATE_CALLS = {"gate", "gate_bootstrap", "gate_largest"}


def test_every_function_that_places_also_gates():
    """Any function that reaches a build entry point must reach a gate in the same function.

    EVERY function in planner.py, not only the stages. The moment a stage delegates placement
    to a helper - stage_plant_expand hands the extension to _plant_add - checking the stages
    alone would let the gate be dropped inside the helper and still pass.
    """
    fns = _fn_nodes("planner.py")
    placing = {n: set(_calls(node)) for n, node in fns.items()}
    placing = {n: c for n, c in placing.items() if c & BUILD_ENTRY_POINTS}
    assert len(placing) >= 8, \
        "only %d placing functions found - the build entry point list went stale" % len(placing)
    for name, called in placing.items():
        assert called & GATE_CALLS, ("%s places (%s) without calling a gate"
                                     % (name, sorted(called & BUILD_ENTRY_POINTS)))


def _print_lua(path):
    """Every literal Lua string handed to A._print - the module's only RCON verb."""
    tree = ast.parse((HERE / path).read_text())
    out = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_print"):
            for a in n.args:
                for s in ast.walk(a):
                    if isinstance(s, ast.Constant) and isinstance(s.value, str):
                        out.append(s.value)
    return out


def test_the_planner_itself_never_writes_to_the_world():
    """planner.py's own RCON is A._print, and only for READS. Placement belongs to the planner
    modules, and they place through buildplan (truce -> staleness -> protected -> rollback)."""
    for lua in _print_lua("planner.py"):
        for banned in ("create_entity", "destroy", ".insert{", "remove_item", "walking_state",
                       "script.on_event", "on_nth_tick", "set_recipe", ".rotate"):
            assert banned not in lua, "A._print emits %r: %s" % (banned, lua[:140])
    src = _src()
    for call in ("A.place(", "A.build(", "rcon.run("):
        assert call not in src, "planner.py calls %s directly" % call


def test_phase0_builds_through_the_planner_modules():
    src = _src()
    for mod, fn in (("plant_planner", "build"), ("mine_planner_v2", "build"),
                    ("supply_planner", "build"), ("power_planner", "apply")):
        assert "%s.%s(" % (mod, fn) in src, "phase 0 does not build through %s.%s" % (mod, fn)


def test_builder_enabled_safe_mode_intact():
    """BUILDER_ENABLED=0 is the default and still short-circuits the build pass."""
    play = _fn_nodes("planner.py")["play"]
    guards = []
    for node in ast.walk(play):
        if not isinstance(node, ast.If):
            continue
        test = ast.dump(node.test)
        if "BUILDER_ENABLED" in test:
            assert "'0'" in test and "'1'" in test, "the default is no longer OFF"
            guards.append(node)
            assert any(isinstance(s, ast.Continue) for s in node.body), \
                "the BUILDER_ENABLED guard no longer skips the build pass"
    assert len(guards) == 1, "expected exactly one BUILDER_ENABLED guard, found %d" % len(guards)
    src = ast.get_source_segment(_src(), play)
    assert "B.operator_present()" in src            # the truce still pauses the builder


def test_phase0_checks_the_truce_between_stages():
    src = ast.get_source_segment(_src(), _fn_nodes("planner.py")["phase0"])
    assert "operator_present()" in src


# ===================================================== BEHAVIOURAL: the gate
@_with_ctx
def test_gate_allows_the_operator_build_order(ctx):
    """Fresh base: power yes, mines no. After the plant: mines yes. The staircase is real."""
    ctx.state()
    assert planner.gate("power_capacity", 3,
                        params={"projected_load_kw": planner.PROJECTED_LOAD_KW})
    assert not planner.gate("mine_outpost", 8, params={"drills": 8})
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    assert planner.gate("mine_outpost", 8, params={"drills": 8})


@_with_ctx
def test_gate_refuses_when_the_census_fails(ctx):
    """A gate that cannot see the world must never allow - that is build_gates' whole point."""
    def boom(force=False, **k):
        raise RuntimeError("build_gates.sense failed (RCON/Lua): (empty)")
    ctx.patch(planner.build_gates, "sense", boom)
    planner.gate_reset()
    assert planner.gate("power_capacity", 1) is False
    assert ctx.log.has("REFUSED", "census failed")


@_with_ctx
def test_gate_refuses_an_unknown_structure(ctx):
    ctx.state()
    assert planner.gate("nonexistent_structure", 1) is False
    assert ctx.log.has("REFUSED")


@_with_ctx
def test_gate_refuses_an_unknown_recipe_instead_of_crashing(ctx):
    ctx.state(counts={"boiler": 3, "steam-engine": 6, "lab": 1}, networks=1)
    assert planner.gate("science_assembler", 1, params={"recipe": "no-such-recipe"}) is False


@_with_ctx
def test_gate_bootstrap_exemption_closes(ctx):
    ctx.state()
    assert planner.gate_bootstrap("lab", 1, exempt_while=True, why="first hand-fed lab")
    assert ctx.log.has("gate EXEMPT", "lab")
    # once the exemption closes, the REAL gate binds (and blocks a lab with no pack flow)
    assert planner.gate_bootstrap("lab", 1, exempt_while=False, why="x") is False


@_with_ctx
def test_gate_caches_one_census_per_pass(ctx):
    calls = {"n": 0}

    def sense(force=False, **k):
        calls["n"] += 1
        return {"tick": 1, "counts": {}, "status": {}, "recipes": {}, "flows": {},
                "ghosts": {}, "networks": 0}
    ctx.patch(planner.build_gates, "sense", sense)
    planner.gate_reset()
    planner.gate("power_grid", 1)
    planner.gate("power_grid", 1)
    assert calls["n"] == 1
    planner.gate_reset()
    planner.gate("power_grid", 1)
    assert calls["n"] == 2


# ===================================================== BEHAVIOURAL: verified() / build_done()
@_with_ctx
def test_verified_only_accepts_a_verified_record(ctx):
    assert planner.verified({"status": "verified", "verify": {"check": {"ok": True,
                                                                       "detail": "moving"}}}, "x")
    assert not planner.verified({"status": "failed",
                                 "verify": {"check": {"ok": False, "detail": "no flow"},
                                            "rollback": {"removed": 12, "not_found": 0}}}, "x")
    assert ctx.log.has("NOT BUILT", "rolled back 12")
    assert not planner.verified(None, "x")
    assert not planner.verified({"status": "planned",
                                 "verify": {"refused": "OPERATOR PRESENT"}}, "x")
    assert ctx.log.has("OPERATOR PRESENT")


@_with_ctx
def test_build_done_tracks_the_record_not_a_flag(ctx):
    p = {"builds": {}}
    assert not planner.build_done(p, "plant")
    ctx.plan_file("p1", "verified")
    p["builds"]["plant"] = "p1"
    assert planner.build_done(p, "plant")
    ctx.plan_file("p2", "failed")
    p["builds"]["plant"] = "p2"
    assert not planner.build_done(p, "plant"), "a failed plan must not read as done"
    ctx.plan_file("p3", "superseded")
    p["builds"]["plant"] = "p3"
    assert not planner.build_done(p, "plant"), "a superseded plan must be re-planned"
    p["builds"]["plant"] = "missing"
    assert not planner.build_done(p, "plant")


# ===================================================== BEHAVIOURAL: stage_plant
def _fake_plant(ctx, build_result, entities=()):
    calls = {"scan": 0, "plan": 0, "build": 0}

    def scan_shore(cx, cy, radius=30):
        calls["scan"] += 1
        return object()

    def plan_plant(n_engines, **kw):
        calls["plan"] += 1
        return {"anchor": (-32, 46), "bbox": [-38, 36, -26, 54], "n_columns": n_engines // 2,
                "power_MW": 0.9 * n_engines, "warnings": ["no terrain supplied"],
                "intake": {"tile": (-34, 48)}, "entities": list(entities)}

    def build(plan, **kw):
        calls["build"] += 1
        return build_result
    ctx.patch(planner.plant_planner, "scan_shore", scan_shore)
    ctx.patch(planner.plant_planner, "plan_plant", plan_plant)
    ctx.patch(planner.plant_planner, "build", build)
    ctx.patch(planner.plant_planner, "plan_poles",
              lambda plan: [("small-electric-pole", -36, 40),
                            ("small-electric-pole", -36, 47)])
    ctx.patch(planner.B, "STATE", {"water": (-32, 52), "coal": (-40, 15)})
    return calls


@_with_ctx
def test_stage_plant_gates_plans_builds_and_records(ctx):
    ctx.state()
    calls = _fake_plant(ctx, {"id": "plant1", "status": "verified",
                              "verify": {"check": {"ok": True, "detail": "engines energised"}}})
    p = {}
    planner.stage_plant(p)
    assert calls == {"scan": 1, "plan": 1, "build": 1}
    assert p["builds"]["plant"] == "plant1"
    assert p["plant"]["coal_intake"] == [-34, 48]
    assert p["plant"]["poles"] == [[-36, 40], [-36, 47]]
    assert ctx.log.has("plant plan: no terrain supplied")


@_with_ctx
def test_stage_plant_builds_nothing_when_the_gate_blocks(ctx):
    # a boiler already stands but has no water source -> power_capacity BLOCKS
    ctx.state(counts={"boiler": 1, "steam-engine": 1})
    calls = _fake_plant(ctx, {"id": "x", "status": "verified", "verify": {}})
    p = {}
    planner.stage_plant(p)
    assert calls["plan"] == 0 and calls["build"] == 0, "planned/built through a closed gate"
    assert "builds" not in p
    assert ctx.log.has("gate BLOCK")


@_with_ctx
def test_stage_plant_records_nothing_when_the_build_is_rolled_back(ctx):
    ctx.state()
    calls = _fake_plant(ctx, {"id": "plant1", "status": "failed",
                              "verify": {"check": {"ok": False, "detail": "boiler dry"},
                                         "rollback": {"removed": 9, "not_found": 0}}})
    p = {}
    planner.stage_plant(p)
    assert calls["build"] == 1
    assert "builds" not in p and "plant" not in p, "a rolled-back build was recorded as done"
    assert ctx.log.has("NOT BUILT")


@_with_ctx
def test_stage_plant_skips_when_capacity_already_leads_the_load(ctx):
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    calls = _fake_plant(ctx, {"id": "x", "status": "verified", "verify": {}})
    planner.stage_plant({})
    assert calls["plan"] == 0
    assert ctx.log.has("already leads")


@_with_ctx
def test_stage_plant_skips_when_its_record_is_still_verified(ctx):
    ctx.state()
    calls = _fake_plant(ctx, {"id": "x", "status": "verified", "verify": {}})
    ctx.plan_file("plant1", "verified")
    planner.stage_plant({"builds": {"plant": "plant1"}})
    assert calls["plan"] == 0 and calls["build"] == 0


def _fake_inventory(ctx, have, craftable=True, order=None):
    """derpface's main inventory + B.make, recorded. `craftable=False` = the ingredients are
    not there, which is the live case: 0 boiler, 0 steam-engine, 15 pipe."""
    inv, made = dict(have), []

    def make(item, n):
        made.append((item, int(n)))
        if order is not None:
            order.append(("make", item, int(n)))
        if craftable:
            inv[item] = inv.get(item, 0) + int(n)
    ctx.patch(planner.B, "_count", lambda item: inv.get(item, 0))
    ctx.patch(planner.B, "make", make)
    return made


_COLUMN = [{"entity": "boiler", "role": "boiler", "x": 0, "y": 0, "direction": 0},
           {"entity": "steam-engine", "role": "engine", "x": 2, "y": 0, "direction": 0},
           {"entity": "steam-engine", "role": "engine", "x": 5, "y": 0, "direction": 0},
           {"entity": "pipe", "role": "riser", "x": 0, "y": 2, "direction": 0}]


@_with_ctx
def test_stage_plant_CRAFTS_the_parts_before_it_places_anything(ctx):
    """REGRESSION 2026-08-30 - the column could not be placed at all. autopilot.place refuses
    with `NO_ITEM <item>` when the item is not in the character's main inventory, and NOTHING
    on the planner path crafted: bom() and bom_delta were computed and never read. Live
    inventory that day: 0 boiler, 0 steam-engine, 15 pipe. So the +1 column placed its belts
    and poles, failed verify on `MISSING: boiler@...`, and buildplan rolled the whole thing
    back - a full plan/apply/verify/rollback cycle for zero builds, every pass, forever.

    The superseded bootstrap.power() provisioned exactly this way (a craft per part); the
    rewrite to plant_planner dropped it and nothing replaced it.
    """
    order = []
    ctx.state()
    calls = _fake_plant(ctx, {"id": "plant1", "status": "verified", "verify": {}},
                        entities=_COLUMN)
    faked_build = planner.plant_planner.build
    ctx.patch(planner.plant_planner, "build",
              lambda plan, **kw: (order.append("build"), faked_build(plan, **kw))[1])
    made = _fake_inventory(ctx, {"pipe": 15}, order=order)
    p = {}
    planner.stage_plant(p)
    assert calls["build"] == 1, "the plant was never built"
    assert made == [("boiler", 1), ("steam-engine", 2)], made
    assert "pipe" not in [i for i, _n in made], "15 pipe in stock: 1 is already provisioned"
    assert order[-1] == "build", "items must be crafted BEFORE the first placement: %s" % order
    assert p["builds"]["plant"] == "plant1"


@_with_ctx
def test_stage_plant_places_NOTHING_when_it_cannot_provision_the_column(ctx):
    """ALL OR NOTHING, and that is right HERE (unlike the gate, which steps down). A boiler
    column cannot step down to "the belts but not the boiler": half a column is a structure
    that does nothing, standing in the way of the one that would. So the shortfall is found
    BEFORE the first placement and nothing is placed at all."""
    ctx.state()
    calls = _fake_plant(ctx, {"id": "plant1", "status": "verified", "verify": {}},
                        entities=_COLUMN)
    made = _fake_inventory(ctx, {"pipe": 15}, craftable=False)
    p = {}
    planner.stage_plant(p)
    assert made == [("boiler", 1), ("steam-engine", 2)], "it did not even try to craft"
    assert calls["build"] == 0, "a column was placed with no boiler to place"
    assert "builds" not in p and "plant" not in p
    assert ctx.log.has("NOT PLACED", "boiler 0/1")


@_with_ctx
def test_plant_expansion_provisions_only_the_DELTA(ctx):
    """scale() keeps every standing entity, so `bom_delta` is exactly what the extension will
    place. Provisioning the whole plant's bom would craft a second plant's worth of parts."""
    ctx.state(counts={"boiler": 2, "steam-engine": 4, "offshore-pump": 1},
              networks=1, boiler_water_min=200, boiler_coal_min=5, flows={"coal": 400.0})
    ctx.patch(planner, "plant_existing", lambda p, st: {"stub": True})
    ctx.patch(planner.plant_planner, "scale", lambda existing, engines: {
        "added_columns": 1, "warnings": (), "bom_delta": {"boiler": 1, "steam-engine": 2},
        "plan": {"anchor": (0, 0), "bbox": (0, 0, 1, 1), "n_columns": 3, "power_MW": 5.4,
                 "intake": {"tile": (1, 2)}, "pump": (-32, 51), "entities": _COLUMN * 9}})
    ctx.patch(planner.plant_planner, "plan_poles", lambda plan: [])
    built = []
    ctx.patch(planner.plant_planner, "build",
              lambda plan, **kw: built.append(plan) or
              {"id": "bp9", "status": "verified", "verify": {}})
    made = _fake_inventory(ctx, {})
    planner.stage_plant_expand({})
    assert made == [("boiler", 1), ("steam-engine", 2)], made
    assert len(built) == 1


def test_plant_is_sized_to_the_load_it_will_carry():
    """LAW 2: capacity LEADS the load. The column count is derived, never a constant."""
    fresh = {"counts": {}}
    cols = planner.plant_columns_needed(fresh)
    cap = cols * build_gates.BOILER_MW
    assert cap >= (planner.PROJECTED_LOAD_KW / 1000.0) * build_gates.POWER_HEADROOM_MIN
    assert planner.plant_columns_needed({"counts": {"boiler": 9, "steam-engine": 18}}) == 0


# ===================================================== BEHAVIOURAL: stage_spine
def _fake_spine(ctx, apply_result, trunk=None):
    calls = {"plan": 0, "apply": 0, "applied": None}

    def plan_trunk(a, b, **kw):
        calls["plan"] += 1
        calls["from_to"] = (a, b)
        return trunk if trunk is not None else [
            {"x": -36, "y": 40, "entity": "small-electric-pole"},   # == the anchor tile
            {"x": -29, "y": 26, "entity": "small-electric-pole"},
            {"x": -15, "y": 3, "entity": "small-electric-pole"}]

    def apply_(plan, **kw):
        calls["apply"] += 1
        calls["applied"] = plan
        calls["kw"] = kw
        return apply_result
    ctx.patch(planner.power_planner, "obstacles_for", lambda area, **k: object())
    ctx.patch(planner.power_planner, "blocked_tiles", lambda obs, **k: set())
    ctx.patch(planner.power_planner, "plan_trunk", plan_trunk)
    ctx.patch(planner.power_planner, "apply", apply_)
    ctx.patch(planner.power_planner, "LAST_WARNINGS", [])
    return calls


@_with_ctx
def test_stage_spine_never_replaces_the_anchor_pole(ctx):
    """The anchor is an EXISTING pole apply() wires TO. Leaving it in the plan tiles makes
    wire_pairs pair it with itself."""
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    calls = _fake_spine(ctx, {"id": "spine1", "status": "verified", "verify": {}})
    p = {"plant": {"poles": [[-36, 40], [-36, 47]]}}
    planner.stage_spine(p)
    assert calls["apply"] == 1
    # (-36,40) is the plant pole NEAREST the spine end (-15,3), so it is the anchor...
    assert calls["kw"]["anchor"] == (-36, 40)
    assert calls["from_to"][0] == (-36, 40) and calls["from_to"][1] == (planner.SPINE_X, 3)
    # ...and being an EXISTING pole it must not also appear as a tile the plan places.
    tiles = {(t["x"], t["y"]) for t in calls["applied"]}
    assert (-36, 40) not in tiles, "the anchor pole was re-placed by the trunk plan"
    assert tiles == {(-29, 26), (-15, 3)}
    assert p["builds"]["spine"] == "spine1"
    assert p["spine"]["poles"] == [[-29, 26], [-15, 3]]


@_with_ctx
def test_stage_spine_waits_for_the_plant(ctx):
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    calls = _fake_spine(ctx, {"id": "x", "status": "verified", "verify": {}})
    planner.stage_spine({})
    assert calls["plan"] == 0
    assert ctx.log.has("no plant poles")


@_with_ctx
def test_stage_spine_survives_an_unroutable_trunk(ctx):
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)

    def boom(*a, **k):
        raise planner.power_planner.GridError("no legal small-electric-pole trunk")
    _fake_spine(ctx, {"id": "x", "status": "verified", "verify": {}})
    ctx.patch(planner.power_planner, "plan_trunk", boom)
    p = {"plant": {"poles": [[-36, 47]]}}
    planner.stage_spine(p)                      # must not raise
    assert "builds" not in p
    assert ctx.log.has("no legal")


# ===================================================== BEHAVIOURAL: stage_mines
def _fake_mines(ctx, build_result, researched=True):
    calls = {"plans": [], "builds": 0}

    def plan_outpost(ore, n, **kw):
        calls["plans"].append((ore, n, kw))
        return {"ore": ore, "lane_y": -42, "from_xy": (-30, -42), "to_xy": (-14, -42),
                "warnings": [], "entities": []}

    def build(plan, **kw):
        calls["builds"] += 1
        return build_result
    ctx.patch(planner.mine_planner_v2, "plan_outpost", plan_outpost)
    ctx.patch(planner.mine_planner_v2, "build", build)
    ctx.patch(planner.B, "_tech_done", lambda t: researched)
    ctx.patch(planner.B, "STATE", {"iron-ore": (-30, -42), "copper-ore": (10, -40),
                                   "coal": (-40, 15)})
    return calls


@_with_ctx
def test_stage_mines_plans_electric_and_never_lays_the_hookup_itself(ctx):
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    calls = _fake_mines(ctx, {"id": "m1", "status": "verified", "verify": {}})
    p = {"spine": {"poles": [[-15, 3]]}}
    planner.stage_mines(p)
    assert calls["builds"] == 3
    for ore, n, kw in calls["plans"]:
        assert kw["drill"] == planner.ELECTRIC_DRILL
        assert kw["trunk"] is None, "the mine plan must not lay its own hookup leg"
        assert kw["power_trunk_x"] == planner.SPINE_X
        assert kw["grid_anchor"] == (-15, 3)
    assert p["mines"]["coal"]["to_xy"] == [-14, -42]


@_with_ctx
def test_stage_mines_refuses_electric_drills_with_no_grid_to_join(ctx):
    """An electric drill on an islanded network mines nothing - that is net 405 exactly."""
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    calls = _fake_mines(ctx, {"id": "m1", "status": "verified", "verify": {}})
    p = {}
    planner.stage_mines(p)
    assert calls["plans"] == [] and calls["builds"] == 0
    assert ctx.log.has("no base spine")


@_with_ctx
def test_stage_mines_falls_back_to_burner_before_the_tech(ctx):
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    calls = _fake_mines(ctx, {"id": "m1", "status": "verified", "verify": {}},
                        researched=False)
    planner.stage_mines({})            # no spine needed: a burner drill needs no grid
    assert [kw["drill"] for _o, _n, kw in calls["plans"]] == [planner.BURNER_DRILL] * 3


@_with_ctx
def test_stage_mines_records_nothing_on_a_rolled_back_mine(ctx):
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    _fake_mines(ctx, {"id": "m1", "status": "failed",
                      "verify": {"check": {"ok": False, "detail": "connected=True moving=False"},
                                 "rollback": {"removed": 31, "not_found": 0}}})
    p = {"spine": {"poles": [[-15, 3]]}}
    planner.stage_mines(p)
    assert "mines" not in p and "builds" not in p
    assert ctx.log.has("NOT BUILT", "rolled back 31")


@_with_ctx
def test_stage_mines_gate_block_places_nothing(ctx):
    ctx.state()                                  # no power at all
    calls = _fake_mines(ctx, {"id": "m1", "status": "verified", "verify": {}})
    planner.stage_mines({"spine": {"poles": [[-15, 3]]}})
    assert calls["plans"] == [] and calls["builds"] == 0


@_with_ctx
def test_stage_mines_does_NOT_mark_a_PARTIAL_outpost_done(ctx):
    """REGRESSION 2026-08-30. `gate_largest` may shrink the ask - 2 drills of 6, when that is
    all the gate allows - and `mark_build` was called unconditionally, so `build_done()`
    reported the ore FINISHED forever and the MINE_DRILLS target was abandoned at whatever one
    pass happened to permit. stage_arrays already gets this right ("if after >= n"); this is
    that convention.

    The mine itself is still recorded - the lanes need from_xy/to_xy and stage_electrify needs
    a record id to supersede - but the STAGE stays open, so the next pass re-plans for the
    full target and buildplan places only the missing drills.
    """
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    _fake_mines(ctx, {"id": "m1", "status": "verified", "verify": {}})
    real = planner.gate
    ctx.patch(planner, "gate",
              lambda s, n=1, **kw: int(n) <= 2 and real(s, n, **kw))   # the gate allows 2
    p = {"spine": {"poles": [[-15, 3]]}}
    planner.stage_mines(p)
    target = dict(planner.MINE_DRILLS)["iron-ore"]
    assert target > 2, "fixture assumes a target the gate has to shrink"
    assert p["mines"]["iron-ore"]["n"] == 2, p["mines"]
    assert p["mines"]["iron-ore"]["plan"] == "m1", "the record must stay findable"
    assert planner._mine_key("iron-ore") not in (p.get("builds") or {}), \
        "a 2-of-%d outpost was marked done and the ore is now skipped forever" % target
    assert not planner.build_done(p, planner._mine_key("iron-ore"))
    assert ctx.log.has("leaving the stage OPEN")
    # ...and a FULL outpost still closes the stage, or nothing would ever finish
    ctx.patch(planner, "gate", real)
    p2 = {"spine": {"poles": [[-15, 3]]}}
    planner.stage_mines(p2)
    assert p2["builds"][planner._mine_key("iron-ore")] == "m1", p2.get("builds")


@_with_ctx
def test_stage_mines_survives_a_layout_error(ctx):
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    _fake_mines(ctx, {"id": "m1", "status": "verified", "verify": {}})

    def boom(ore, n, **kw):
        raise planner.mine_planner_v2.LayoutError("empty %s patch" % ore)
    ctx.patch(planner.mine_planner_v2, "plan_outpost", boom)
    p = {"spine": {"poles": [[-15, 3]]}}
    planner.stage_mines(p)                       # must not raise
    assert ctx.log.has("plan refused")


# ===================================================== BEHAVIOURAL: the supply lanes
def _fake_supply(ctx, plan_result, build_result):
    calls = {"plans": [], "builds": []}

    def plan_supply(item, a, b, **kw):
        calls["plans"].append((item, a, b))
        return plan_result

    def build(plan, **kw):
        calls["builds"].append(plan)
        return build_result
    ctx.patch(planner.supply_planner, "plan_supply", plan_supply)
    ctx.patch(planner.supply_planner, "build", build)
    return calls


def _mined_state(ctx):
    return ctx.state(counts={"boiler": 3, "steam-engine": 6, "electric-mining-drill": 20},
                     status={"mining-drill": {"working": 20}}, networks=1,
                     drills_by_ore={"iron-ore": {"electric-mining-drill": 8}})


@_with_ctx
def test_ore_lane_routes_from_the_mine_lane_end_to_the_array(ctx):
    _mined_state(ctx)
    calls = _fake_supply(ctx, {"ok": True, "code": None, "reason": "22 tiles, 1 crossing",
                               "plan": {"id": "L1"}},
                         {"id": "L1", "status": "verified", "verify": {}})
    p = {"mines": {"iron-ore": {"to_xy": [-14, -42]}}}
    planner.stage_ore_lanes(p)
    assert calls["plans"] == [("iron-ore", (-14, -42), planner._array_ore_belt("iron-ore"))]
    assert p["builds"]["lane:iron-ore"] == "L1"


@_with_ctx
def test_a_duplicate_lane_is_never_laid_beside_its_predecessor(ctx):
    """72.4% of everything the operator deleted was a parallel duplicate. On DUPLICATE the
    stage FINISHES the lane that already owns the pair; it never plans a second route."""
    _mined_state(ctx)
    ctx.plan_file("L0", "planned")
    calls = _fake_supply(ctx,
                         {"ok": False, "code": supply_planner.DUPLICATE,
                          "reason": "iron-ore already reaches (-7,8)",
                          "lane": {"id": "L0", "plan_id": "L0", "status": "planned"}},
                         {"id": "L0", "status": "verified", "verify": {}})
    p = {"mines": {"iron-ore": {"to_xy": [-14, -42]}}}
    planner.stage_ore_lanes(p)
    assert calls["builds"] == ["L0"], "the EXISTING lane should have been finished"
    assert len(calls["plans"]) == 1
    assert p["builds"]["lane:iron-ore"] == "L0"


@_with_ctx
def test_a_verified_duplicate_lane_is_simply_adopted(ctx):
    _mined_state(ctx)
    ctx.plan_file("L0", "verified")
    calls = _fake_supply(ctx,
                         {"ok": False, "code": supply_planner.DUPLICATE, "reason": "dup",
                          "lane": {"id": "L0", "plan_id": "L0", "status": "active"}},
                         {"id": "L0", "status": "verified", "verify": {}})
    p = {"mines": {"iron-ore": {"to_xy": [-14, -42]}}}
    planner.stage_ore_lanes(p)
    assert calls["builds"] == [], "a verified lane must not be re-applied"
    assert p["builds"]["lane:iron-ore"] == "L0"


@_with_ctx
def test_a_refused_route_builds_nothing(ctx):
    _mined_state(ctx)
    calls = _fake_supply(ctx, {"ok": False, "code": supply_planner.PROTECTED_TILES,
                               "reason": "8 of 22 routed tiles are operator-protected"},
                         {"id": "x", "status": "verified", "verify": {}})
    p = {"mines": {"iron-ore": {"to_xy": [-14, -42]}}}
    planner.stage_ore_lanes(p)
    assert calls["builds"] == []
    assert "builds" not in p
    assert ctx.log.has("PROTECTED_TILES")


@_with_ctx
def test_coal_lane_targets_the_plant_intake_not_the_ore_patch(ctx):
    """bootstrap.coal_to_boiler tapped ON the ore patch and dead-ended in the engines."""
    _mined_state(ctx)
    calls = _fake_supply(ctx, {"ok": True, "code": None, "reason": "ok", "plan": {"id": "C1"}},
                         {"id": "C1", "status": "verified", "verify": {}})
    p = {"mines": {"coal": {"to_xy": [-30, 15]}},
         "plant": {"coal_intake": [-34, 48]}}
    planner.stage_coal_lane(p)
    assert calls["plans"] == [("coal", (-30, 15), (-34, 48))]
    assert p["builds"]["lane:coal"] == "C1"


@_with_ctx
def test_coal_lane_waits_for_both_ends(ctx):
    _mined_state(ctx)
    calls = _fake_supply(ctx, {"ok": True, "plan": {"id": "C1"}, "code": None, "reason": ""},
                         {"id": "C1", "status": "verified", "verify": {}})
    planner.stage_coal_lane({"mines": {"coal": {"to_xy": [-30, 15]}}})   # no plant yet
    planner.stage_coal_lane({"plant": {"coal_intake": [-34, 48]}})       # no coal mine yet
    assert calls["plans"] == []


# ===================================================== BEHAVIOURAL: the crash-site stages
@_with_ctx
def test_stage_world_exempts_the_spawn_furnaces_only_until_a_drill_exists(ctx):
    ran = []
    for name in ("setup_world", "fuel", "smelting_base"):
        ctx.patch(planner.B, name, (lambda nm: lambda *a, **k: ran.append(nm))(name))
    ctx.patch(planner, "_scout_guarded", lambda p: ran.append("scout"))
    ctx.state()                                   # nothing built, no drills
    planner.stage_world({})
    assert ran == ["setup_world", "scout", "fuel", "smelting_base"]
    assert ctx.log.has("gate EXEMPT", "smelter_array")

    # Once a drill is mining, the exemption CLOSES and the real gate decides. Here the mine
    # is far too small to justify 12 more furnaces, so it refuses.
    ran[:] = []
    ctx.log.lines[:] = []
    ctx.state(counts={"electric-mining-drill": 1},
              drills_by_ore={"iron-ore": {"electric-mining-drill": 1}})
    planner.stage_world({})
    assert not ctx.log.has("gate EXEMPT"), "the crash-site exemption never closed"
    assert ctx.log.has("gate BLOCK", "smelter_array")
    assert "smelting_base" not in ran


@_with_ctx
def test_stage_red_science_exempts_exactly_one_hand_fed_lab(ctx):
    ran = []
    ctx.patch(planner.B, "red_science", lambda: ran.append("lab"))
    ctx.state()
    planner.stage_red_science({})
    assert ran == ["lab"]
    assert ctx.log.has("gate EXEMPT", "lab")

    ran[:] = []
    ctx.state(counts={"lab": 1})                  # a lab exists -> the real gate binds
    planner.stage_red_science({})
    assert ran == [], "a second lab was built with no pack flow behind it"


@_with_ctx
def test_stage_arrays_asks_for_the_deficit_and_verifies_the_result(ctx):
    counts = {"iron-ore": 0, "copper-ore": 0}
    asked = []
    ctx.patch(planner, "_array_furnaces", lambda ore, n: counts[ore])
    ctx.patch(planner.B, "build_smelter_array",
              lambda ore, n: (asked.append((ore, n)), counts.__setitem__(ore, n))[0])
    ctx.state(counts={"boiler": 3, "steam-engine": 6, "electric-mining-drill": 12},
              status={"mining-drill": {"working": 12}}, networks=1,
              drills_by_ore={"iron-ore": {"electric-mining-drill": 8},
                             "copper-ore": {"electric-mining-drill": 4}})
    p = {}
    planner.stage_arrays(p)
    assert asked == [("iron-ore", 16), ("copper-ore", 12)]
    assert p["arrays"] == {"iron-ore": 16, "copper-ore": 12}
    # 16 on 8 drills = 1.25x and 12 on 4 = 1.88x: both inside the measured 2.0 ceiling
    assert ctx.log.has("gate ALLOW", "1.25x")
    # second pass: the rows stand, so nothing is asked for again
    asked[:] = []
    planner.stage_arrays({})
    assert asked == []
    assert ctx.log.has("already standing")


@_with_ctx
def test_stage_arrays_refuses_a_row_with_no_mine_behind_it(ctx):
    """'furnaces are free but a row with no mine behind it is not a stage' - LAW 3 is a
    licence with a budget, and the budget's denominator is DRILL capacity."""
    asked = []
    ctx.patch(planner, "_array_furnaces", lambda ore, n: 0)
    ctx.patch(planner.B, "build_smelter_array", lambda ore, n: asked.append(ore))
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1,
              drills_by_ore={"iron-ore": {}})
    planner.stage_arrays({})
    assert asked == []
    assert ctx.log.has("no drill capacity")


@_with_ctx
def test_stage_arrays_never_builds_on_a_failed_census_read(ctx):
    asked = []
    ctx.patch(planner, "_array_furnaces", lambda ore, n: -1)
    ctx.patch(planner.B, "build_smelter_array", lambda ore, n: asked.append(ore))
    _mined_state(ctx)
    planner.stage_arrays({})
    assert asked == []
    assert ctx.log.has("never build blind")


@_with_ctx
def test_stage_arrays_does_not_mark_an_array_that_never_appeared(ctx):
    ctx.patch(planner, "_array_furnaces", lambda ore, n: 0)      # build is a silent no-op
    ctx.patch(planner.B, "build_smelter_array", lambda ore, n: None)
    _mined_state(ctx)
    p = {}
    planner.stage_arrays(p)
    assert p["arrays"] == {}
    assert ctx.log.has("did not move")


@_with_ctx
def test_stage_array_grid_covers_the_live_consumers_and_anchors_to_the_spine(ctx):
    calls = {}

    def plan_grid(area, cons, **kw):
        calls["grid"] = dict(kw, area=area, consumers=cons)
        return [{"x": -6, "y": 2, "entity": "small-electric-pole"}]

    def apply_(plan, **kw):
        calls["apply"] = kw
        calls["applied"] = plan
        return {"id": "G1", "status": "verified", "verify": {}}
    ctx.patch(planner.power_planner, "scan", lambda area: [{"n": "inserter", "x": 0.5, "y": 4.5}])
    ctx.patch(planner.power_planner, "from_entities", lambda ents: [(0, 4, 0, 4)])
    ctx.patch(planner.power_planner, "obstacles_for", lambda area, **k: object())
    ctx.patch(planner.power_planner, "LAST_WARNINGS", [])
    ctx.patch(planner.power_planner, "plan_grid", plan_grid)
    ctx.patch(planner.power_planner, "apply", apply_)
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    p = {"spine": {"poles": [[-15, 3]]}}
    planner.stage_array_grid(p)
    assert calls["grid"]["anchor"] == (-15, 3)
    assert calls["apply"]["anchor"] == (-15, 3)
    assert calls["apply"]["consumers"] == [(0, 4, 0, 4)]
    assert p["builds"]["array_grid"] == "G1"


@_with_ctx
def test_stage_array_grid_lays_nothing_where_a_pole_already_stands(ctx):
    """OPERATOR RULE: never build anything unless it ACTUALLY DOES SOMETHING.

    build_smelter_array had already laid a pitch-4 lattice in the inserter rows, and
    power_planner could only see those poles as blocked TILES - so the stage planned 32 more
    on the next free phase, 2.0 tiles away, covering zero new consumers and failing the
    operator's own 3.0 separation standard 57 times over. The stage now drops consumers that
    are already powered, and when none are left it lays nothing.
    """
    laid = []
    ctx.patch(planner.power_planner, "scan", lambda area: [
        {"n": "inserter", "t": "inserter", "x": 0.5, "y": 4.5},
        {"n": "small-electric-pole", "t": "electric-pole", "x": 0.5, "y": 4.5}])
    ctx.patch(planner.power_planner, "from_entities", lambda ents: [(0, 4, 0, 4)])
    ctx.patch(planner.power_planner, "plan_grid", lambda *a, **k: 1 / 0)
    ctx.patch(planner.power_planner, "apply", lambda *a, **k: laid.append(1))
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    p = {"spine": {"poles": [[-15, 3]]}}
    planner.stage_array_grid(p)
    assert laid == [], "a lattice was laid over consumers that are already powered"
    assert ctx.log.has("already covered", "nothing to lay")
    assert "array_grid" not in (p.get("builds") or {}), \
        "the stage must stay re-checkable so it can extend the lattice when the arrays grow"


@_with_ctx
def test_stage_array_grid_passes_the_standing_poles_to_the_planner(ctx):
    """An UNCOVERED consumer still gets a lattice - and the poles already in the ground go to
    plan_grid and apply, or the planner re-derives the interleave it just avoided."""
    seen = {}
    ctx.patch(planner.power_planner, "scan", lambda area: [
        {"n": "inserter", "t": "inserter", "x": 20.5, "y": 4.5},
        {"n": "small-electric-pole", "t": "electric-pole", "x": 0.5, "y": 4.5}])
    ctx.patch(planner.power_planner, "from_entities", lambda ents: [(20, 4, 20, 4)])
    ctx.patch(planner.power_planner, "obstacles_for", lambda area, **k: object())
    ctx.patch(planner.power_planner, "LAST_WARNINGS", [])
    ctx.patch(planner.power_planner, "plan_grid",
              lambda area, cons, **kw: seen.update(plan=kw) or
              [{"x": 20, "y": 5, "entity": "small-electric-pole"}])
    ctx.patch(planner.power_planner, "apply",
              lambda plan, **kw: seen.update(apply=kw) or
              {"id": "G2", "status": "verified", "verify": {}})
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    planner.stage_array_grid({"spine": {"poles": [[-15, 3]]}})
    assert seen["plan"]["existing"] == [(0, 4)], seen["plan"]
    assert seen["apply"]["existing"] == [(0, 4)], seen["apply"]


@_with_ctx
def test_stage_array_grid_lays_nothing_before_the_arrays_exist(ctx):
    laid = []
    ctx.patch(planner.power_planner, "scan", lambda area: [])
    ctx.patch(planner.power_planner, "from_entities", lambda ents: [])
    ctx.patch(planner.power_planner, "apply", lambda *a, **k: laid.append(1))
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    planner.stage_array_grid({"spine": {"poles": [[-15, 3]]}})
    assert laid == []
    assert ctx.log.has("no electric consumers")


def _grid_spy(ctx, consumers, ents):
    """stage_array_grid with power_planner stubbed at its planning edge -> what it was told."""
    seen = {}
    ctx.patch(planner.power_planner, "scan", lambda area: ents)
    ctx.patch(planner.power_planner, "from_entities", lambda e: list(consumers))
    ctx.patch(planner.power_planner, "obstacles_for", lambda area, **k: object())
    ctx.patch(planner.power_planner, "LAST_WARNINGS", [])
    ctx.patch(planner.power_planner, "plan_grid",
              lambda area, cons, **kw: seen.update(plan=dict(kw, consumers=list(cons))) or
              [{"x": 20, "y": 5, "entity": "small-electric-pole"}])
    ctx.patch(planner.power_planner, "apply",
              lambda plan, **kw: seen.update(apply=kw) or
              {"id": "G9", "status": "verified", "verify": {}})
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    return seen


@_with_ctx
def test_stage_array_grid_floors_pole_tiles_and_never_truncates_them(ctx):
    """REGRESSION, measured live 2026-08-30. `int(e["x"])` read power_planner.probe_lua's
    `e.position.x` - the entity CENTRE, a float - and int() truncates TOWARD ZERO, so every
    NEGATIVE coordinate landed one tile east/north: a 1x1 pole on tile (-15,3) reports
    (-14.5,3.5) and was read as (-14,3). 15 of the 40 poles in area (-15,0,32,20) came back on
    the wrong tile, and those tiles feed `_too_near`, plan_grid's connectivity nodes,
    validate's separation and split checks, and apply's join / tie_in / wire_pairs.

    The repo's two conventions, and both are floors: the probe's own `bb`, or
    floor(centre - size/2). Lua's math.floor is a true floor, which is why `_live_poles()` -
    the identical read done in Lua - was never wrong.
    """
    ents = [{"n": "inserter", "t": "inserter", "x": 20.5, "y": 4.5},
            {"n": "small-electric-pole", "t": "electric-pole", "x": -14.5, "y": 3.5},
            {"n": "small-electric-pole", "t": "electric-pole", "x": -6.5, "y": -0.5},
            # the bb form the probe actually supplies is preferred when it is there
            {"n": "small-electric-pole", "t": "electric-pole", "x": -2.5, "y": 3.5,
             "bb": [-3, 3, -3, 3]},
            {"n": "small-electric-pole", "t": "electric-pole", "x": 9.5, "y": 4.5}]
    seen = _grid_spy(ctx, [(20, 4, 20, 4)], ents)
    planner.stage_array_grid({"spine": {"poles": [[-15, 3]]}})
    want = [(-15, 3), (-7, -1), (-3, 3), (9, 4)]
    assert seen["plan"]["existing"] == want, seen["plan"]["existing"]
    assert seen["apply"]["existing"] == want, seen["apply"]["existing"]


@_with_ctx
def test_stage_array_grid_credits_a_pole_only_its_OWN_tiers_supply_area(ctx):
    """The coverage test used THIS stage's pole tier for every standing pole, whatever tier it
    actually was. Per mine_layout.POLES a BIG pole supplies 2.0 and is 2x2, while a small one
    supplies 2.5 and is 1x1 - so a big pole's supply area is SMALLER, and crediting it the
    small window marks a consumer beside it as powered when it is not. A consumer marked
    powered never gets a pole, which is a dark machine dressed up as a finished stage.

    Geometry: big pole on tiles (0,0)-(1,1) supplies x in [-1,2]; a small pole there would
    supply x in [-2,2]. The inserter at (-2,0) is covered by exactly one of those.
    """
    big = {"n": "big-electric-pole", "t": "electric-pole", "x": 1.0, "y": 1.0,
           "bb": [0, 0, 1, 1]}
    ins = {"n": "inserter", "t": "inserter", "x": -1.5, "y": 0.5}
    seen = _grid_spy(ctx, [(-2, 0, -2, 0)], [ins, big])
    planner.stage_array_grid({"spine": {"poles": [[-15, 3]]}})
    assert seen.get("plan"), ("the inserter at (-2,0) is OUTSIDE the big pole's 4x4 supply "
                              "area and was credited as powered: %s" % ctx.log.lines)
    assert seen["plan"]["consumers"] == [(-2, 0, -2, 0)]
    assert seen["plan"]["existing"] == [(0, 0)], seen["plan"]["existing"]
    # ...and a pole that genuinely DOES cover it still stops the stage laying anything
    seen2 = _grid_spy(ctx, [(-2, 0, -2, 0)],
                      [ins, {"n": "small-electric-pole", "t": "electric-pole",
                             "x": 0.5, "y": 0.5}])
    planner.stage_array_grid({"spine": {"poles": [[-15, 3]]}})
    assert not seen2.get("plan"), "a covered consumer must not buy a second pole"
    assert ctx.log.has("already covered", "nothing to lay")


@_with_ctx
def test_stage_electrify_replans_the_outpost_and_frees_its_lane(ctx):
    seen = {}

    def upgrade(ore, **kw):
        seen[ore] = kw
        return {"plan": {"lane_y": -42, "from_xy": (-30, -42), "to_xy": (-12, -42)},
                "build": {"id": "E1", "status": "verified", "verify": {}}}
    ctx.patch(planner.mine_planner_v2, "upgrade_to_electric", upgrade)
    ctx.patch(planner.B, "_tech_done", lambda t: True)
    ctx.patch(planner.B, "STATE", {"iron-ore": (-30, -42)})
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    p = {"spine": {"poles": [[-15, 3]]},
         "mines": {"iron-ore": {"drill": planner.BURNER_DRILL, "to_xy": [-20, -42]}},
         "builds": {"mine:iron-ore": "M0", "lane:iron-ore": "L0"}}
    planner.stage_electrify(p)
    assert "pole" not in seen["iron-ore"], "pole= would arrive twice inside upgrade_to_electric"
    assert seen["iron-ore"]["old_record_id"] == "M0"
    assert seen["iron-ore"]["trunk"] is None
    assert p["mines"]["iron-ore"]["drill"] == planner.ELECTRIC_DRILL
    assert p["builds"]["mine:iron-ore"] == "E1"
    assert "lane:iron-ore" not in p["builds"], "the stale lane record survived the re-plan"


@_with_ctx
def test_stage_electrify_is_a_noop_without_the_tech(ctx):
    called = []
    ctx.patch(planner.mine_planner_v2, "upgrade_to_electric",
              lambda *a, **k: called.append(1))
    ctx.patch(planner.B, "_tech_done", lambda t: False)
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    planner.stage_electrify({"spine": {"poles": [[-15, 3]]},
                             "mines": {"iron-ore": {"drill": planner.BURNER_DRILL}}})
    assert called == []


@_with_ctx
def test_stage_electrify_leaves_an_already_electric_mine_alone(ctx):
    called = []
    ctx.patch(planner.mine_planner_v2, "upgrade_to_electric",
              lambda *a, **k: called.append(1))
    ctx.patch(planner.B, "_tech_done", lambda t: True)
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)
    planner.stage_electrify({"spine": {"poles": [[-15, 3]]},
                             "mines": {"iron-ore": {"drill": planner.ELECTRIC_DRILL}}})
    assert called == []


@_with_ctx
def test_stage_science_is_gated_on_the_converter_not_the_lab(ctx):
    ran = []
    for name in ("automate_green_science", "setup_science_io", "ensure_science_cells"):
        ctx.patch(planner.B, name, (lambda nm: lambda *a, **k: ran.append(nm))(name))
    ctx.state(counts={"boiler": 3, "steam-engine": 6}, networks=1)   # no flow, no labs
    planner.stage_science({})
    assert ran == [], "science cells were built with no pack flow and no sink"
    ctx.state(counts={"boiler": 9, "steam-engine": 18, "lab": 2}, networks=1,
              flows={"iron-plate": 200.0, "copper-plate": 120.0},
              status={"stone-furnace": {"working": 20}})
    planner.stage_science({})
    assert ran == ["automate_green_science", "setup_science_io", "ensure_science_cells"]


# ===================================================== BEHAVIOURAL: phase0 orchestration
@_with_ctx
def test_one_failing_stage_does_not_abandon_the_pass(ctx):
    ran = []

    def ok(name):
        return lambda p: ran.append(name)

    def boom(p):
        raise RuntimeError("stage exploded")
    ctx.patch(planner, "PHASE0_STAGES", (("a", ok("a")), ("b", boom), ("c", ok("c"))))
    ctx.patch(planner.build_gates, "sense", lambda **k: {})
    planner.phase0({})
    assert ran == ["a", "c"], "a raising stage aborted the ones behind it"
    assert ctx.log.has("phase 0 stage b", "stage exploded")
    assert ctx.lessons.rows and ctx.lessons.rows[0]["condition"] == "phase 0 stage b"


@_with_ctx
def test_phase0_stops_the_moment_the_operator_connects(ctx):
    ran = []
    seen = {"n": 0}

    def present():
        seen["n"] += 1
        return seen["n"] > 1                     # online from the second stage on
    ctx.patch(planner.B, "operator_present", present)
    ctx.patch(planner, "PHASE0_STAGES",
              (("a", lambda p: ran.append("a")), ("b", lambda p: ran.append("b"))))
    ctx.patch(planner.build_gates, "sense", lambda **k: {})
    planner.phase0({})
    assert ran == ["a"]
    assert ctx.log.has("operator online mid-pass")


@_with_ctx
def test_phase0_resenses_the_world_each_pass(ctx):
    n = {"c": 0}

    def sense(force=False, **k):
        n["c"] += 1
        return {"tick": 1, "counts": {}, "status": {}, "recipes": {}, "flows": {},
                "ghosts": {}, "networks": 0}
    ctx.patch(planner.build_gates, "sense", sense)
    ctx.patch(planner, "PHASE0_STAGES", (("a", lambda p: planner.gate("power_grid", 1)),))
    planner.phase0({})
    planner.phase0({})
    assert n["c"] == 2, "the second pass gated against a cached world"


# ===================================================== the controller's INVARIANT class
def _inv_ctx(ctx, poles=(), lanes_=(), lint=(), obsolete=(), protected=(), built=None):
    ctx.patch(controller.B, "_built_load",
              lambda: built if built is not None else {(x, 0) for x in range(40)})
    ctx.patch(controller.B, "_protected_load", lambda: set(protected))
    ctx.patch(controller.status, "log", ctx.log)
    import lane_lint
    import power_planner
    ctx.patch(power_planner, "audit", lambda area, **k: list(poles))
    ctx.patch(supply_planner, "lanes", lambda **k: list(lanes_))
    ctx.patch(supply_planner, "retire_obsolete", lambda **k: list(obsolete))
    ctx.patch(lane_lint, "trace", lambda x, y, **k: {"tiles": [{"x": x, "y": y}]})
    ctx.patch(lane_lint, "lint_lane", lambda tr, expect=None: list(lint))


@_with_ctx
def test_invariants_collect_from_all_three_audits(ctx):
    _inv_ctx(ctx,
             poles=[{"check": "islanded_pole", "severity": "error", "pos": [-3, 15],
                     "msg": "pole is on electric_network_id 405, the base grid is 1"}],
             lanes_=[{"item": "iron-ore", "from": [-30, -42], "status": "active"}],
             lint=[{"code": "MIXED_ITEMS", "sev": 1, "x": -10, "y": -42,
                    "detail": "iron-ore + copper-ore on one lane"}],
             obsolete=[{"id": "L9", "item": "coal", "reason": "no consumer draws from it"}])
    fs = controller._run_invariants()
    codes = sorted(f["code"] for f in fs)
    assert codes == ["MIXED_ITEMS", "OBSOLETE_LANE", "islanded_pole"], codes
    assert {f["src"] for f in fs} == {"power", "lane:iron-ore", "obsolete"}
    assert min(f["sev"] for f in fs) == 1


@_with_ctx
def test_invariants_never_report_an_operator_protected_tile(ctx):
    """BUILD LAW 3: a finding on a tile he deliberately cleared is intent, not a defect."""
    _inv_ctx(ctx,
             poles=[{"check": "off_lattice", "severity": "warn", "pos": [7, 7], "msg": "x"},
                    {"check": "off_lattice", "severity": "warn", "pos": [8, 8], "msg": "y"}],
             protected=[(7, 7)])
    fs = controller._run_invariants()
    assert [f["pos"] for f in fs] == [[8, 8]]
    assert ctx.log.has("dropped on operator-protected tiles")


@_with_ctx
def test_invariants_abort_when_the_protected_registry_is_unreadable(ctx):
    """Better no audit than an audit that reports his deletions back at him as defects."""
    _inv_ctx(ctx, poles=[{"check": "off_lattice", "severity": "warn", "pos": [7, 7],
                          "msg": "x"}])

    def boom():
        raise OSError("protected-tiles.json is corrupt")
    ctx.patch(controller.B, "_protected_load", boom)
    assert controller._run_invariants() == []
    assert ctx.log.has("aborting the audit")


@_with_ctx
def test_invariants_drop_info_level_findings(ctx):
    _inv_ctx(ctx, poles=[{"check": "note", "severity": "info", "pos": [1, 1], "msg": "fyi"}])
    assert controller._run_invariants() == []


@_with_ctx
def test_one_audit_failing_does_not_lose_the_others(ctx):
    _inv_ctx(ctx, lanes_=[{"item": "coal", "from": [-30, 15], "status": "active"}],
             lint=[{"code": "DEAD_END", "sev": 1, "x": -20, "y": 15, "detail": "orphan run"}])
    import power_planner

    def boom(area, **k):
        raise RuntimeError("probe command is 4100 bytes")
    ctx.patch(power_planner, "audit", boom)
    fs = controller._run_invariants()
    assert [f["code"] for f in fs] == ["DEAD_END"]
    assert ctx.log.has("power audit failed")


@_with_ctx
def test_invariant_area_needs_a_built_base_and_is_clamped(ctx):
    _inv_ctx(ctx, built={(0, 0), (1, 1)})
    assert controller._invariant_area() is None, "audited a base with nothing built"
    _inv_ctx(ctx, built={(x, 0) for x in range(0, 4000, 4)})
    a = controller._invariant_area()
    assert a is not None
    assert a[2] - a[0] <= controller.INVARIANT_MAX_SPAN
    assert a[3] - a[1] <= controller.INVARIANT_MAX_SPAN


@_with_ctx
def test_findings_become_a_prioritized_issue(ctx):
    controller._INV["findings"] = [
        {"src": "power", "code": "islanded_pole", "sev": 1, "pos": [-3, 15], "detail": "net 405"},
        {"src": "obsolete", "code": "OBSOLETE_LANE", "sev": 2, "pos": None, "detail": "L9"}]
    try:
        issues = controller.detect({"engines": 2, "drills": 6, "labs": 1})
        inv = [i for i in issues if i.id == controller.INVARIANT_ID]
        assert len(inv) == 1
        assert inv[0].sev == 1, "severity must come from the WORST finding"
        assert "islanded_pole" in inv[0].evidence
    finally:
        controller._INV["findings"] = []


@_with_ctx
def test_no_invariant_issue_on_an_empty_world(ctx):
    controller._INV["findings"] = [{"src": "power", "code": "x", "sev": 1, "pos": None,
                                    "detail": "d"}]
    try:
        assert controller.detect({"engines": 0, "drills": 0, "labs": 0}) == []
    finally:
        controller._INV["findings"] = []


@_with_ctx
def test_reporting_clears_the_findings_and_writes_one_lesson_per_source(ctx):
    ctx.patch(controller.status, "log", ctx.log)
    controller._INV["findings"] = [
        {"src": "power", "code": "islanded_pole", "sev": 1, "pos": [-3, 15], "detail": "net 405"},
        {"src": "power", "code": "off_lattice", "sev": 2, "pos": [4, 4], "detail": "one-off"},
        {"src": "lane:coal", "code": "DRAIN", "sev": 1, "pos": [2, 2], "detail": "mid-lane chest"}]
    n = controller._report_invariants()
    assert n == 3
    assert controller._INV["findings"] == [], "the fixer must clear what it reported"
    assert ctx.log.has("invariant[1]", "islanded_pole", "@-3,15")
    assert len(ctx.lessons.rows) == 2                     # one per source, not one per finding
    assert {r["condition"] for r in ctx.lessons.rows} == {
        "invariant violated: power", "invariant violated: lane:coal"}
    assert controller._report_invariants() == 0


@_with_ctx
def test_the_invariant_fixer_repairs_nothing(ctx):
    """It reports and learns. Every remediation an invariant implies is CONSTRUCTION, and
    construction is the builder's - the loop BUILDER_ENABLED actually gates."""
    fn = _fn_nodes("controller.py")["_report_invariants"]
    called = set(_calls(fn))
    for banned in ("B.ensure_lanes", "B.repair_belt_gaps", "B.ensure_grid_connected",
                   "B.fix_unpowered", "supply_planner.retire_obsolete", "power_planner.apply",
                   "buildplan.apply", "_fix_lanes"):
        assert banned not in called, "_report_invariants calls %s" % banned


def test_retire_obsolete_is_only_ever_called_dry():
    fn = _fn_nodes("controller.py")["_run_invariants"]
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "retire_obsolete"):
            kw = {k.arg: k.value for k in node.keywords}
            assert "dry_run" in kw and kw["dry_run"].value is True, \
                "retire_obsolete must be called dry_run=True from the controller"
            return
    raise AssertionError("_run_invariants no longer runs retire_obsolete")


def test_the_invariant_class_is_suspended_by_the_truce():
    assert controller.INVARIANT_ID in controller.LAYOUT_ISSUES


def test_the_audit_battery_contains_no_mutating_lua_and_no_event_handler():
    src = (HERE / "controller.py").read_text()
    for banned in ("create_entity", "destroy()", "remove_item", "walking_state",
                   "script.on_event", "on_nth_tick", "set_recipe", ".rotate("):
        assert banned not in src, "controller.py contains %r" % banned


def test_the_controller_no_longer_rebuilds_the_superseded_coal_spur():
    fn = _fn_nodes("controller.py")["controller_loop"]
    src = ast.get_source_segment((HERE / "controller.py").read_text(), fn)
    assert '"coal_to_boiler"' not in src and "'coal_to_boiler'" not in src


# ------------------------------------------- the saturated chain that read as a broken lane
#
# Live 2026-08-29. 16 drills, 12 of them waiting_for_space; 28 stone furnaces, ALL of them
# full_output; both terminal chests at 3200 plates; 0 assembling machines and 9 labs all at
# missing_science_packs. iron_pm and copper_pm were genuinely 0 - and the chain was SATURATED,
# not broken. sense() bucketed furnaces only into no_ingredients and working, so the 28
# full_output were counted in the total and reported nowhere; the triage prompt had no
# signature for back-pressure; and the 4B answered class=lane / actuator=fix_lanes three times
# running, which dispatched scrub_mixed_ore + repair_belt_gaps + ensure_lanes every ~20s
# against a lane that was working perfectly and merely full.


def _saturated(**kw):
    """The live sense() dict, with the bucket the census used to omit."""
    d = {"engines": 4, "engine_energy": 1, "drills": 16, "drills_blocked": 12,
         "furnaces": 28, "furnaces_starved": 0, "furnaces_working": 0,
         "furnaces_full_output": 28, "labs": 9, "labs_working": 0, "asm": 0,
         "iron_pm": 0, "copper_pm": 0, "science_pm": 0, "free_slots": 40,
         "boiler_fuel": 5, "power_networks": 1}
    d.update(kw)
    return d


def test_sense_reports_the_full_output_bucket_at_all():
    """The bucket that did not exist. Without it the census says 28 furnaces, 0 starved, 0
    working - a base doing nothing, with no field saying why."""
    src = (HERE / "controller.py").read_text()
    assert "defines.entity_status.full_output" in src, \
        "sense() still buckets furnaces into no_ingredients/working only"
    assert "furnaces_full_output=fo" in src, "the bucket is counted but never emitted"


@_with_ctx
def test_a_saturated_chain_is_backpressure_and_never_routed_to_fix_lanes(ctx):
    """The bug, as a rule. fix_lanes cannot clear a lane that is intact and full, so routing
    this to it is unrequested work that can never succeed - and it ran every ~20s for hours."""
    ids = {i.id: i for i in controller.detect(_saturated())}
    assert "backpressure" in ids, sorted(ids)
    assert "lane_stalled" not in ids and "arrays_starved" not in ids, sorted(ids)
    assert ids["backpressure"].fixer is controller._report_backpressure
    assert "full_output" in ids["backpressure"].evidence
    for issue in ids.values():
        assert issue.fixer is not controller._fix_lanes, \
            "%s dispatches lane work at a saturated lane" % issue.id


@_with_ctx
def test_a_genuinely_starved_array_still_reads_as_a_lane_fault(ctx):
    """The guard must not swallow the real signature it sits in front of. STARVED (no input)
    and FULL_OUTPUT (no room) are opposite failures and keep opposite actuators."""
    ids = {i.id: i for i in
           controller.detect(_saturated(furnaces_starved=20, furnaces_full_output=0))}
    assert "lane_stalled" in ids, sorted(ids)
    assert "backpressure" not in ids, sorted(ids)
    assert ids["lane_stalled"].fixer is controller._fix_lanes


@_with_ctx
def test_the_progress_watchdog_does_not_answer_backpressure_with_lane_work(ctx):
    """no_progress is severity 0 - it PREEMPTS the builder. Under back-pressure the flow
    really is flat, but lane work is the one actuator that certainly will not restart it."""
    ctx.patch(controller, "_HIST", [(0.0, _saturated())])
    ids = {i.id: i for i in controller.detect(_saturated())}
    assert "no_progress" in ids, sorted(ids)
    assert ids["no_progress"].fixer is controller._report_backpressure


def test_the_triage_prompt_can_tell_saturated_from_broken():
    """The LLM had only two mappings for a collapsed plate flow, both 'lane'. Given
    iron_pm=0 + drills_blocked=12 it had no other bucket to reach for."""
    s = triage.SYSTEM
    assert "furnaces_full_output" in s, "the prompt never mentions the new field"
    assert "BACK-PRESSURE" in s
    assert "fix_lanes" in s and "CANNOT help" in s
    assert "STARVED AND FULL ARE OPPOSITES" in s


def test_status_counts_drills_by_TYPE_and_never_argues_for_burner_drills():
    """status.json reported drills:0 on a map running 16 electric ones: it counted the literal
    name 'burner-mining-drill', of which this map has had none since the operator converted
    every drill and deleted the fuel belts. The fuel metric is now NAMED for what it measures,
    so a -1 can never be read as 'the drills are out of coal' - a tier regression argument."""
    src = (HERE / "status.py").read_text()
    assert "parts['drills']=#s.find_entities_filtered{type='mining-drill'}" in src
    assert "name='burner-mining-drill'" not in src, "the stale name filter is still there"
    assert "min_burner_drill_fuel" in src and '"min_drill_fuel"' not in src, \
        "the burner-era metric must be named for what it measures"


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
