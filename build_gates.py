#!/usr/bin/env python3
"""ADMISSION CONTROL for builds: the operator's staging discipline, enforced BEFORE placement.

`principles.py` audits a base that already exists. This module is the other half: it refuses
a build that has no business existing yet. Both halves come from the same measurement — Seth's
2026-08-29 hand-optimization (snapshots/before.json 713 ents -> after.json 619 ents) — but this
one runs at the *front* of a builder, not after it.

    allowed, why = gate("lab", n=9)          # live, READ-ONLY
    if not allowed: log(why); return

WHAT HE DELETED, AND WHY IT IS A GATE AND NOT A LINT
  - 2 labs      -> both `missing_science_packs`; zero assemblers made packs anywhere.
  - 1 assembler -> recipe iron-gear-wheel, `full_output`; nothing consumed gears.
  - 9 chests + 9 inserters -> a chest->chest shuttle standing in for a belt nobody laid;
    statuses alternated waiting_for_source_items / waiting_for_space_in_destination, ZERO
    working. Every one of the 9 had no belt within 2.0 and no machine within 2.5; the 2
    survivors both had a belt within 2.0. That separator is exact (9/9 vs 2/2).
  - and he ADDED a second boiler column BEFORE electrifying 16 drills. Without it, headroom
    would have gone 3.6/2.246 -> 1.8/2.246 = 0.80 and the whole base would have browned out.

The four laws those deletions encode (see the four LAW_* constants below):
  LAW 1  TWO-SIDED GATE. A stage needs a verified live INPUT *and* a verified live SINK.
         The bot's inverse failure is measured too: 9 labs with 0 pack assemblers took iron
         174->37/min (-79%), copper 90->51 (-43%), coal 120->12 (-90%). Building a sink with
         no converter collapses supply exactly as hard as a converter with no supply.
  LAW 2  POWER LEADS THE LOAD. Capacity is doubled *before* the load that needs it.
         POWER_HEADROOM_MIN = 1.5; measured 1.079 (broken) -> 1.603 (working).
         Corollary GRID_NETWORKS_MAX = 1 (before had 2, with all 6 electric drills on the
         generator-less island, net 405).
  LAW 3  PASSIVE MAY BE PRE-BUILT; ACTIVE MAY NOT. He kept 11/28 stone furnaces idle at
         `no_ingredients` and deleted 3/3 idle ELECTRIC consumers. The discriminator is the
         only property that separates the two sets: a burner draws 0 kW and locks 0 items.
         The way to pre-build an active consumer is GHOSTS -> `reserve()`.
  LAW 4  A CHEST IS LEGITIMATE ONLY AS A LANE TERMINUS. One per lane, nothing downstream.
  LAW 5  A GATE THAT BLOCKS X BECAUSE CONSTRAINT C IS UNSATISFIED MUST NOT ALSO BLOCK THE
         BUILD THAT INCREASES C. Added 2026-08-30 after these gates DEADLOCKED a live base:
         `power_capacity` refused on `coal_at_boiler` (coal 120/min < 178/min) while
         `mine_outpost` — the only build that raises coal/min — was refused on
         `power_headroom`, which only `power_capacity` can raise. Four gates, zero legal
         moves, 90 s of idling, forever. Each law was individually right and the base still
         starved: refusing every relief build is how a safe gate becomes a stuck one.
         `gate(..., relieves=("coal_at_boiler",))` is the exemption, and it is NARROW — see
         RELIEF below, where all three conditions must hold before a single check is waived.

READ-ONLY. `sense()` issues find_entities_filtered + property reads and one `storage._gates`
scratch string it clears afterwards (the bottleneck.py / principles.py / dashboard.py pattern,
with its own key so it never races theirs). Nothing here creates, destroys, rotates or moves
anything. `reserve()` RETURNS a ghost plan and NEVER executes it. That plan is NOT a
buildplan plan (it has no id/tiles/names — `buildplan.apply()` on it raises KeyError 'id'):
hand `ghost_lua` to the executor and wrap `revive` in your own `buildplan.new_plan()`, which
is what owns placement, the functional check and registry-scoped rollback.
NO EVENT HANDLERS, ever: a runtime handler locks human players out of the server.

Every check is a pure function of a `state` dict, so the whole rule set unit-tests offline
against synthetic states and against snapshots/{before,after}.json (see test_build_gates.py).

    python3 build_gates.py status                 live: what is allowed, what is blocked
    python3 build_gates.py gate lab 9             one structure
    python3 build_gates.py flows science_assembler
    python3 build_gates.py snapshot after         the operator's own base, offline
"""
import json
import math
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
SNAPDIR = HERE / "snapshots"
CHUNK = 3000

# =========================================================================== the laws
LAW_TWO_SIDED = "LAW 1: a stage needs a live input AND a live sink"
LAW_POWER_FIRST = "LAW 2: power capacity leads the load it will carry"
LAW_PASSIVE_ONLY = "LAW 3: passive (burner) structures may be pre-built; active ones may not"
LAW_CHEST_TERMINUS = "LAW 4: a chest is legitimate only as a lane terminus"

POWER_HEADROOM_MIN = 1.5      # measured: 1.079 broken -> 1.603 working
COAL_HEADROOM_MIN = 1.5       # measured: 120 supplied / 77 demanded = 1.56
GRID_NETWORKS_MAX = 1         # before had 2; net 405 held 6 drills and no generator
SUPPLY_HEADROOM = 2.0         # "flow >= 2.0 * recipe demand" (order-spec DEPENDENCY_GATES)
BACKUP_ALARM_FRAC = 0.25      # full_output / waiting_for_space fraction that blocks a new sink
PREBUILD_OVERBUILD_MAX = 2.0  # furnaces per unit of supported flow; measured 1.67 / 1.88

# =========================================================================== constants
# Prototype numbers. MEASURED = read off this map's prototypes on 2.1.17 (order-spec §0.4,
# plant-spec §6). INFERRED = vanilla arithmetic, never probed here — labelled so a wrong
# number is findable.
BOILER_MW = 1.8               # MEASURED boiler.get_max_energy_usage() 30000 J/tick
ENGINE_MW = 0.9               # MEASURED steam-engine.get_max_energy_production() 15000
BOILER_ENGINE_RATIO = 2       # MEASURED exactly 1 boiler : 2 engines
COAL_FUEL_MJ = 4.0            # MEASURED coal.fuel_value 4 MJ
FURNACE_KW = 90.0             # MEASURED stone-furnace 1500 J/tick
BOILER_COAL_PER_MIN = round(BOILER_MW * 60.0 / COAL_FUEL_MJ, 6)                # 27.0
FURNACE_COAL_PER_MIN = round((FURNACE_KW / 1000.0) * 60.0 / COAL_FUEL_MJ, 6)   # 1.35

CONSUMER_KW = {               # MEASURED on this map (order-spec §0.3 arithmetic reproduces
    "electric-mining-drill": 90.0,      # both the 1.079 and the 1.603 headroom exactly)
    "inserter": 13.9,
    "lab": 60.0,
    "assembling-machine-1": 77.5,       # 75 + 2.5 drain
}
CONSUMER_KW_INFERRED = {      # [INFERENCE] vanilla figures; nothing of these exists on the map
    "long-handed-inserter": 13.9, "fast-inserter": 13.9, "bulk-inserter": 13.9,
    "assembling-machine-2": 155.0, "assembling-machine-3": 390.0,
    "electric-furnace": 186.0, "pump": 30.0, "radar": 300.0, "beacon": 480.0,
}
# Burners and generators draw NOTHING from the grid. offshore-pump has no electric energy
# source at all in 2.1.17 (electric_energy_source_prototype == nil) — that is what lets the
# plant black-start off coal alone.
NON_ELECTRIC = {"stone-furnace", "steel-furnace", "burner-mining-drill", "burner-inserter",
                "boiler", "steam-engine", "offshore-pump", "solar-panel", "accumulator",
                "small-electric-pole", "medium-electric-pole", "big-electric-pole",
                "substation", "wooden-chest", "iron-chest", "steel-chest",
                "transport-belt", "underground-belt", "splitter", "pipe", "pipe-to-ground"}

ELECTRIC_DRILL_ORE_PER_MIN = 30.0    # MEASURED mining_speed 0.5
BURNER_DRILL_ORE_PER_MIN = 15.0      # MEASURED mining_speed 0.25
DRILL_ORE_PER_MIN = {"electric-mining-drill": ELECTRIC_DRILL_ORE_PER_MIN,
                     "burner-mining-drill": BURNER_DRILL_ORE_PER_MIN}
STONE_FURNACE_PLATES_PER_MIN = 18.75  # MEASURED speed 1 / 3.2 s (= its ore draw, 1:1 recipe)
DRILL_TO_FURNACE_RATIO = ELECTRIC_DRILL_ORE_PER_MIN / STONE_FURNACE_PLATES_PER_MIN   # 1.6
BELT_ITEMS_PER_MIN_TOTAL = 900.0     # MEASURED transport-belt 15/s
BELT_ITEMS_PER_MIN_PER_LANE = 450.0
# One mine lane feeds one belt LANE, so the drill count per lane is a hard ceiling.
MAX_DRILLS_PER_LANE = int(BELT_ITEMS_PER_MIN_PER_LANE / ELECTRIC_DRILL_ORE_PER_MIN)   # 15
MAX_STONE_FURNACES_PER_LANE = int(BELT_ITEMS_PER_MIN_PER_LANE
                                  / STONE_FURNACE_PLATES_PER_MIN)                     # 24

LAB_PACKS_PER_MIN = 2.0       # [INFERENCE] lab speed 1, 30 s research unit; research was
                              # unqueued live so this was never observed directly
ASM1_CRAFTS_PER_MIN = 30.0    # MEASURED assembling-machine-1 speed 0.5 -> 0.5*60/recipe_s
RECIPE_SECONDS = {"automation-science-pack": 5.0,     # MEASURED (6 packs/min at speed 0.5)
                  "logistic-science-pack": 6.0,       # [INFERENCE] vanilla
                  "iron-gear-wheel": 0.5}             # [INFERENCE] vanilla
# A cell is sized to what it FEEDS, not to its own top speed. A gear assembler left to run at
# its 60 gears/min ceiling is precisely the machine he deleted (recipe iron-gear-wheel,
# status full_output, nothing downstream). So an intermediate cell's rate is the pull of the
# one cell it serves, not ASM1_CRAFTS_PER_MIN / recipe_seconds.
CELL_RATE_PER_MIN = {"iron-gear-wheel": 6.0}          # = one red cell's gear draw
# Plate cost per CRAFT, gears/circuits expanded to plates.
CELL_PLATE_COST = {
    "automation-science-pack": {"iron-plate": 2.0, "copper-plate": 1.0},   # MEASURED
    # [INFERENCE] 1 belt (0.5 gear + 0.5 plate = 1.5 Fe) + 1 inserter (1 gear + 1 plate +
    # 1 circuit = 4 Fe, 1.5 Cu) = 5.5 Fe / 1.5 Cu per pack.
    "logistic-science-pack": {"iron-plate": 5.5, "copper-plate": 1.5},
    "iron-gear-wheel": {"iron-plate": 2.0},                                # [INFERENCE] vanilla
}
SCIENCE_PACKS = ("automation-science-pack", "logistic-science-pack")
# recipe -> its input items. [INFERENCE] vanilla recipe structure, not probed on this map.
# Used ONLY to answer "does anything built consume X" — the sink half of LAW 1. It is what
# makes the deleted iron-gear-wheel assembler (full_output, no gear consumer) a gate and not
# a post-mortem.
RECIPE_INPUTS = {
    "automation-science-pack": ("copper-plate", "iron-gear-wheel"),
    "logistic-science-pack": ("transport-belt", "inserter"),
    "iron-gear-wheel": ("iron-plate",),
    "transport-belt": ("iron-gear-wheel", "iron-plate"),
    "inserter": ("iron-gear-wheel", "iron-plate", "electronic-circuit"),
    "electronic-circuit": ("iron-plate", "copper-cable"),
    "copper-cable": ("copper-plate",),
    "iron-plate": ("iron-ore",),
    "copper-plate": ("copper-ore",),
}
CONTAINER_NAMES = ("wooden-chest", "iron-chest", "steel-chest")
FURNACE_NAMES = ("stone-furnace", "steel-furnace", "electric-furnace")
ORE_PLATE = {"iron-ore": "iron-plate", "copper-ore": "copper-plate"}

# Items whose one-minute flow sense() pulls back. Anything a gate can ask for must be here.
FLOW_ITEMS = ("iron-plate", "copper-plate", "coal", "stone", "iron-ore", "copper-ore",
              "iron-gear-wheel", "electronic-circuit", "transport-belt", "inserter",
              "automation-science-pack", "logistic-science-pack")

# =========================================================================== build order
# The implied stage sequence. planner.phase0 currently runs smelting_base -> power ->
# red_science(2 labs) -> mine_outpost -> ... -> electrify_mines, which inverts this at three
# points (red_science before any ore lane, smelting_base before power, electrify_mines last)
# and builds precisely the 2 labs + 1 assembler the operator deleted.
BUILD_ORDER = (
    ("power_capacity", "boiler+engine columns sized to the NEXT stage's load, column pitch 4"),
    ("power_grid", "one pole network, explicitly wired, 0 islands"),
    ("mine_outpost", "electric drills, only once power_grid covers them"),
    ("ore_lane", "continuous, single-direction, source->destination verified"),
    ("smelter_array", "burner furnaces; may be overbuilt, they are passive when unfed"),
    ("plate_lane", "plate belt out of the array with a real downstream sink"),
    ("science_assembler", "gear + red/green cells — the CONVERTER, before any lab"),
    ("lab", "revive only as many labs as pack flow feeds; the rest stay GHOSTS"),
    ("mall_assembler", "last"),
)


# =========================================================================== gate table
def _gate(law, requires, per_unit_flows=None, flow_headroom=1.0, adds_kw=0.0, note=""):
    # per_unit_flows is either {item: per_min_per_unit} or the sentinel "recipe", meaning
    # "derive it from the recipe the caller names" (a gear cell and a red cell are the same
    # structure with different appetites).
    if per_unit_flows != "recipe":
        per_unit_flows = dict(per_unit_flows or {})
    return {"law": law, "requires": tuple(requires), "per_unit_flows": per_unit_flows,
            "flow_headroom": flow_headroom, "adds_kw": adds_kw, "note": note}


GATES = {
    # ---- power. The only stage with no upstream: it is allowed to go first, and must.
    "power_capacity": _gate(
        LAW_POWER_FIRST, ("water_source", "coal_at_boiler", "headroom_after"),
        note="1 boiler : 2 engines, +1.8 MW per column. First column is unconditional "
             "(nothing precedes power); every later one needs water + coal on a belt."),
    "power_grid": _gate(
        LAW_POWER_FIRST, ("grid_single",),
        note="One network. Script-placed poles do NOT auto-connect: wire every pair within "
             "7.5 explicitly, then compare electric_network_id."),

    # ---- mining. Electric drills are ACTIVE consumers: power and grid must exist first.
    "mine_outpost": _gate(
        LAW_POWER_FIRST, ("power_headroom", "grid_energized", "lane_capacity"),
        adds_kw=CONSUMER_KW["electric-mining-drill"],
        note="n = number of NEW electric drills. Also verify off-gate that the new 3x3 "
             "drop_position lands on belt/underground/container (a 2x2->3x3 tier swap MOVES "
             "it) and that a main-network pole is within supply radius 2.5."),
    "electric_mining_drill": _gate(
        LAW_POWER_FIRST, ("power_headroom", "grid_energized"),
        adds_kw=CONSUMER_KW["electric-mining-drill"], note="alias of mine_outpost, per drill"),

    "ore_lane": _gate(
        LAW_TWO_SIDED, ("producer_live", "lane_capacity"),
        note="A lane with no live drill upstream is a duplicate lane waiting to happen — "
             "72.4%% of the 127 belts he deleted were exactly that."),

    # ---- LAW 3: the ONE structure exempt from the FLOW gate (not from every gate).
    "smelter_array": _gate(
        LAW_PASSIVE_ONLY, ("overbuild_within_budget",),
        note="EXEMPT from every flow gate: an unfed stone furnace is a BURNER — 0 kW, 0 items "
             "locked. He kept 11/28 furnaces at no_ingredients and deleted 0. Overbuild up to "
             "PREBUILD_OVERBUILD_MAX; reserve the ore row and plate row even while unfed."),
    "plate_lane": _gate(
        LAW_TWO_SIDED, ("sink_exists",),
        note="The plate belt needs a real downstream sink, or it is a 33-tile conveyor to a "
             "chest that back-pressures the whole chain."),

    # ---- LAW 1: the converter, before any lab.
    "science_assembler": _gate(
        LAW_TWO_SIDED, ("flows", "sink_exists", "power_headroom", "grid_energized"),
        per_unit_flows="recipe", flow_headroom=SUPPLY_HEADROOM,
        adds_kw=CONSUMER_KW["assembling-machine-1"],
        note="Flow requirement is derived from the recipe: pass recipe='automation-science-"
             "pack'. He deleted an iron-gear-wheel assembler sitting at full_output because "
             "nothing consumed gears — the sink half of the gate is not optional."),
    "mall_assembler": _gate(
        LAW_TWO_SIDED, ("flows", "labs_satisfied", "power_headroom", "grid_energized"),
        per_unit_flows={"iron-plate": 30.0}, adds_kw=CONSUMER_KW["assembling-machine-1"],
        note="Last. 30 iron-plate/min of SURPLUS above what science already commits."),

    # ---- LAW 1 again, the sink side.
    "lab": _gate(
        LAW_TWO_SIDED,
        ("flows", "pack_producer_live", "research_queued", "upstream_not_backed_up",
         "power_headroom", "grid_energized"),
        per_unit_flows={"automation-science-pack": LAB_PACKS_PER_MIN},
        adds_kw=CONSUMER_KW["lab"],
        note="n = labs to REVIVE, not labs to reserve. Reserve the whole array as ghosts "
             "(reserve()) and revive floor(pack_flow / 2.0)."),

    # ---- LAW 4.
    "overflow_chest": _gate(
        LAW_CHEST_TERMINUS, ("chest_is_terminus",),
        note="One per lane, at belt_end+2, loaded by one inserter at belt_end+1, nothing "
             "downstream. Requires params={'lane_chests': n, 'is_terminus': bool} — the gate "
             "refuses to guess lane topology it cannot see."),
}
GATES["lab_array"] = GATES["lab"]
GATES["power_column"] = GATES["power_capacity"]


# =========================================================================== state helpers
def _f(d, k, default=0.0):
    try:
        return float(d.get(k, default) or 0.0)
    except (TypeError, ValueError):
        return float(default)


def capacity_mw(st):
    """Installed generation. 1 boiler : 2 engines exactly — the binding side is the minimum,
    so 3 boilers behind 2 engines is 1.8 MW, not 5.4."""
    b = _f(st.get("counts", {}), "boiler")
    e = _f(st.get("counts", {}), "steam-engine")
    return min(b * BOILER_MW, e * ENGINE_MW)


def load_mw(st, extra_kw=0.0):
    """Nominal electric draw from the entity census. Reproduces the measured table exactly:
    before.json 6*90 + 67*13.9 + 2*60 + 1*77.5 = 1.6688 MW; after.json 16*90 + 58*13.9 =
    2.2462 MW. Burners, generators and the offshore pump contribute nothing."""
    kw = float(extra_kw)
    for name, n in (st.get("counts") or {}).items():
        if name in NON_ELECTRIC:
            continue
        rate = CONSUMER_KW.get(name, CONSUMER_KW_INFERRED.get(name))
        if rate is None:
            continue                           # belts, poles, pipes, ghosts: no draw
        kw += rate * float(n)
    return kw / 1000.0


def headroom(st, extra_kw=0.0):
    """capacity / load. inf when nothing draws power yet (an empty grid is not starved)."""
    cap, load = capacity_mw(st), load_mw(st, extra_kw)
    if load <= 0:
        return float("inf")
    return cap / load


def status_frac(st, key, status):
    """Fraction of a class in a status. `key` may be an entity TYPE ('mining-drill') or a
    NAME ('stone-furnace'); types are tried first because that is how the operator's own
    evidence is quoted (status_frac('stone-furnace','full_output') is a name)."""
    for table in ("status_type", "status"):
        hist = (st.get(table) or {}).get(key)
        if hist:
            tot = sum(hist.values())
            return (hist.get(status, 0) / float(tot)) if tot else 0.0
    return 0.0


def status_count(st, key, status):
    for table in ("status_type", "status"):
        hist = (st.get(table) or {}).get(key)
        if hist:
            return int(hist.get(status, 0))
    return 0


def flow(st, item):
    return _f(st.get("flows", {}), item)


def coal_demand_per_min(st, extra_boilers=0, projected_kw=0.0):
    """Coal the base actually burns: LOAD-driven, not boiler-count-driven, plus fed furnaces.

    MEASURED on the live map 2026-08-29 — 2 boilers, 4 engines, 405.2 kW generated, and the
    game's own consumption statistic reading 6.0 coal/min. `boilers * BOILER_COAL_PER_MIN`
    predicted 54. A boiler is not a fire that burns whether you use it or not: it converts
    fuel in proportion to the steam its engines are asked for, so 405.2 kW * 60 s / 4 MJ =
    6.08 coal/min, which is the 6.0 the game reports.

    Modelling idle capacity as full-tilt burn is what produced the coal deadlock: the gate
    demanded 178 coal/min against 120 supplied and refused every plant expansion, while the
    plant it was protecting sat at 11% load. Worse, it made capacity SELF-BLOCKING - each
    boiler column raised the demand it had to satisfy, so the base could never grow.

    Two different questions, so two different treatments:

      EXISTING plant burns what it is actually asked for - the load, capped by what the
      engines can convert, because you cannot burn coal you have no engines to turn into work.

      Plant being ADDED is charged at full tilt. You do not build a boiler column to leave it
      idle, so the honest planning question is "if this new capacity were used, could the mine
      feed it?" That keeps the protection the old model got right - four columns behind one
      drill is still refused - without the self-blocking the old model got wrong."""
    cap_mw = capacity_mw(st)
    load = load_mw(st, projected_kw)
    burn_mw = min(load, cap_mw) if cap_mw > 0 else load
    burn_mw += max(0, int(extra_boilers)) * BOILER_MW
    return burn_mw * 60.0 / COAL_FUEL_MJ + furnace_coal_per_min(st)


def furnace_coal_per_min(st):
    """Coal the burner furnaces burn. Only the FED ones: a furnace at `no_ingredients` burns
    nothing, which is the same property that makes an array safe to pre-build (LAW 3)."""
    fed = 0.0
    for name in FURNACE_NAMES:
        hist = (st.get("status") or {}).get(name) or {}
        if hist:
            fed += sum(v for k, v in hist.items() if k != "no_ingredients")
        else:
            fed += _f(st.get("counts", {}), name)      # no status info: assume all are fed
    return fed * FURNACE_COAL_PER_MIN


def drill_capacity_per_min(st, ore=None):
    """Ore/min the built drills can produce, for one ore or all of them. None when the census
    carries no per-ore breakdown (a snapshot has no mining_target), which callers must treat
    as "unknown", never as zero."""
    by_ore = st.get("drills_by_ore")
    if not by_ore:
        return None
    cap = 0.0
    for res, names in by_ore.items():
        if ore and res != ore:
            continue
        for name, k in (names or {}).items():
            cap += DRILL_ORE_PER_MIN.get(name, ELECTRIC_DRILL_ORE_PER_MIN) * float(k)
    return cap


def containers(st):
    return int(sum(_f(st.get("counts", {}), n) for n in CONTAINER_NAMES))


def adds_kw_for(structure, params=None):
    """kW ONE unit of `structure` adds to the grid, with the caller's TIER honoured.

    GATES[..]["adds_kw"] is a default the table has to pick without knowing which prototype
    the caller intends, and it picks the expensive one. `mine_outpost` is the case that
    deadlocked the live base: its table entry charges the electric drill's 90 kW against a
    BURNER drill, which is in NON_ELECTRIC and draws exactly nothing. Pass
    params={'drill': 'burner-mining-drill'} and the gate charges 0 — which is the difference
    between "mine more coal first" being an instruction and being a dead end.
    """
    g = GATES.get(structure) or {}
    base = float(g.get("adds_kw", 0.0) or 0.0)
    p = params or {}
    name = p.get("entity") or p.get("drill") or p.get("machine")
    if not name:
        return base
    if name in NON_ELECTRIC:
        return 0.0
    rate = CONSUMER_KW.get(name, CONSUMER_KW_INFERRED.get(name))
    return float(rate) if rate is not None else base


# =========================================================================== flow table
def required_flows(structure, n=1, recipe=None):
    """The measured dependency table, as {item: per_minute_required} for n units.

    This is the whole point of the module made inspectable: a builder can print exactly what
    it is waiting for, and a dashboard can show the gap instead of "blocked"."""
    g = GATES.get(structure)
    if g is None:
        raise KeyError("unknown structure %r (known: %s)" % (structure, ", ".join(sorted(GATES))))
    spec = g["per_unit_flows"]
    if spec == "recipe":
        recipe = recipe or "automation-science-pack"
        cost = CELL_PLATE_COST.get(recipe)
        if cost is None:
            raise KeyError("no plate cost recorded for recipe %r (known: %s)"
                           % (recipe, ", ".join(sorted(CELL_PLATE_COST))))
        per_min = CELL_RATE_PER_MIN.get(recipe)
        if per_min is None:
            per_min = ASM1_CRAFTS_PER_MIN / RECIPE_SECONDS[recipe]  # crafts/min at speed 0.5
        spec = {item: amt * per_min for item, amt in cost.items()}
    h = g["flow_headroom"]
    return {item: round(amt * n * h, 3) for item, amt in spec.items()}


def affordable_count(structure, st, recipe=None, cap=None):
    """How many units the LIVE flows and power headroom actually support.

    This is LAW 3's revive batch: `n = floor(input_flow_per_min / consumer_rate_per_min)`,
    then clipped by the power a new unit would add. Reserve the whole design as ghosts;
    revive this many."""
    g = GATES.get(structure)
    if g is None:
        raise KeyError("unknown structure %r" % structure)
    per_unit = required_flows(structure, 1, recipe)
    n = None
    for item, need in per_unit.items():
        k = int(math.floor(flow(st, item) / need)) if need > 0 else 10 ** 6
        n = k if n is None else min(n, k)
    if n is None:
        n = 10 ** 6                                  # no flow requirement (e.g. smelter_array)
    kw = g["adds_kw"]
    if kw > 0:
        c, l0 = capacity_mw(st), load_mw(st)
        room_mw = (c / POWER_HEADROOM_MIN) - l0      # MW we may still commit
        n = min(n, int(math.floor(max(0.0, room_mw) * 1000.0 / kw)))
    if cap is not None:
        n = min(n, int(cap))
    return max(0, int(n))


# =========================================================================== predicates
# Each returns (ok, message). The message is written to be quoted verbatim into a log line.
def _recipe_of(p):
    """The recipe a cell gate is being asked about: explicit, else the product if that product
    is itself a cell recipe (so params={'product':'iron-gear-wheel'} sizes the GEAR cell, not
    the red cell), else the default."""
    r = p.get("recipe")
    if r:
        return r
    prod = p.get("product")
    return prod if prod in CELL_PLATE_COST else None


def _pr_flows(st, n, g, p):
    need = required_flows(p.get("structure"), n, _recipe_of(p))
    short, keys = [], []
    for item, req in sorted(need.items()):
        have = flow(st, item)
        if have < req:
            short.append("%s %.1f/min < %.1f/min needed" % (item, have, req))
            keys.append("flows:" + item)          # the CONSTRAINT, per item: a relief build
    if short:                                     # relieves `flows:coal`, never "flows"
        return False, "; ".join(short) + (" (n=%d)" % n), keys
    return True, "flows ok: " + ", ".join("%s %.1f/min>=%.1f" % (i, flow(st, i), r)
                                          for i, r in sorted(need.items()))


def _pr_power_headroom(st, n, g, p):
    add = adds_kw_for(p.get("structure"), p) * n
    h = headroom(st, add)
    cap, load = capacity_mw(st), load_mw(st, add)
    if add <= 0:
        # MARGINAL, not absolute. This check exists to stop a build from EATING the headroom
        # its own load needs; a build that adds 0 kW cannot lower a ratio it does not appear
        # in. Testing it absolutely is what refused every zero-cost relief build — a burner
        # coal outpost, a belt lane, a pole trunk — on a base whose headroom only a boiler
        # column could fix, and only more coal could pay for.
        if h < POWER_HEADROOM_MIN:
            return True, ("headroom %.3f is ALREADY below %.2f (%.2f MW / %.2f MW) — this "
                          "build adds 0 kW and cannot make it worse; the relief is a boiler "
                          "column, not a refusal"
                          % (h, POWER_HEADROOM_MIN, cap, load))
        return True, "power headroom %.3f (%.2f MW / %.2f MW), 0 kW added" % (h, cap, load)
    if h < POWER_HEADROOM_MIN:
        return False, ("power headroom %.3f < %.2f (%.2f MW installed / %.2f MW load incl. "
                       "%.0f kW new) — %s" % (h, POWER_HEADROOM_MIN, cap, load, add,
                                              "build the boiler column FIRST"))
    return True, "power headroom %.3f (%.2f MW / %.2f MW)" % (h, cap, load)


def _pr_headroom_after(st, n, g, p):
    """power_capacity's own gate: capacity AFTER this column vs the load it is being built for."""
    cap_after = capacity_mw(st) + n * BOILER_MW
    load_after = load_mw(st, _f(p, "projected_load_kw"))
    col = "+%d column(s) = %d boiler(s) + %d engine(s)" % (n, n, n * BOILER_ENGINE_RATIO)
    if load_after <= 0:
        return True, "%.2f MW installed against no load yet (%s)" % (cap_after, col)
    h = cap_after / load_after
    if h < POWER_HEADROOM_MIN:
        return False, ("projected headroom %.3f < %.2f after %s (%.2f MW / %.2f MW)"
                       % (h, POWER_HEADROOM_MIN, col, cap_after, load_after))
    return True, "projected headroom %.3f after %s" % (h, col)


def _pr_grid_single(st, n, g, p):
    nets = int(_f(st, "networks"))
    if nets > GRID_NETWORKS_MAX:
        return False, ("%d electric networks — the grid is SPLIT; wire the islands before "
                       "anything else (before.json had net 405 holding 6 drills and no "
                       "generator)" % nets)
    return True, "%d electric network(s)" % nets


def _pr_grid_energized(st, n, g, p):
    ok, msg = _pr_grid_single(st, n, g, p)
    if not ok:
        return ok, msg
    # A build with no electric consumer in it has nothing to energize. The SPLIT check above
    # still binds (P2 is about not making a second network, which a pole row can do), but
    # demanding generation before a burner outpost or a belt lane is the same false negative
    # as testing headroom absolutely.
    if adds_kw_for(p.get("structure"), p) * n <= 0:
        return True, msg + "; this build has no electric consumer to energize"
    if capacity_mw(st) <= 0:
        return False, "no generation installed — an electric consumer would be born unpowered"
    if int(_f(st, "networks")) < 1:
        return False, "no electric network exists yet — place and WIRE poles first"
    return True, msg + ", energized"


def _pr_producer_live(st, n, g, p):
    cls = p.get("producer", "mining-drill")
    if status_count(st, cls, "working") > 0:
        return True, "%d %s working upstream" % (status_count(st, cls, "working"), cls)
    return False, ("no %s is `working` — a lane laid ahead of its producer is the duplicate "
                   "lane he deleted 92 belts of" % cls)


def _pr_lane_capacity(st, n, g, p):
    """One mine lane is one belt LANE: 450 items/min / 30 per drill = 15 drills. Past that the
    lane is saturated and the extra drills only sit at waiting_for_space_in_destination."""
    drills = int(p.get("drills", n))
    if drills > MAX_DRILLS_PER_LANE:
        return False, ("%d drills on one lane > %d (a belt lane carries %.0f items/min and a "
                       "drill makes %.0f) — split the outpost across two lanes"
                       % (drills, MAX_DRILLS_PER_LANE, BELT_ITEMS_PER_MIN_PER_LANE,
                          ELECTRIC_DRILL_ORE_PER_MIN))
    return True, "%d/%d drills on the lane" % (drills, MAX_DRILLS_PER_LANE)


def _pr_overbuild_within_budget(st, n, g, p):
    """LAW 3 is a licence, not a blank cheque. He ran 16 iron furnaces on 6 drills (1.67x) and
    12 copper on 4 (1.88x) — both under 2.0. The denominator is DRILL CAPACITY, not measured
    plate flow: pre-building is sized to what the mine can deliver, not to what it does now."""
    ore = p.get("ore")
    supply = p.get("supply_per_min")
    if supply is None:
        supply = drill_capacity_per_min(st, ore)
    if supply is None:
        return True, ("overbuild unbounded: no per-ore drill census (pass params={'ore':..} "
                      "live, or {'supply_per_min':..}) — a burner furnace is still free")
    supported = supply / STONE_FURNACE_PLATES_PER_MIN
    # The ratio is PER ORE (16 iron on 6 drills, 12 copper on 4). Scope the existing furnaces
    # to the same ore via their recipe; a furnace that has never been fed has no recipe at all
    # and is invisible here, which only ever makes the gate more permissive, never less.
    plate = ORE_PLATE.get(ore or "")
    if plate and (st.get("recipes") or {}).get(plate):
        existing = float(sum(st["recipes"][plate].values()))
    else:
        existing = sum(_f(st.get("counts", {}), f) for f in FURNACE_NAMES)
    total = existing + n
    if supported <= 0:
        return False, ("no drill capacity for %s yet — mine first; furnaces are free but a "
                       "row with no mine behind it is not a stage" % (ore or "this ore"))
    ratio = total / supported
    if ratio > PREBUILD_OVERBUILD_MAX:
        return False, ("%d %sfurnaces would be %.2fx the %.1f the mine can feed (%.0f ore/min "
                       "/ %.2f per furnace); the measured ceiling is %.1fx"
                       % (total, (ore + " ") if ore else "", ratio, supported, supply,
                          STONE_FURNACE_PLATES_PER_MIN, PREBUILD_OVERBUILD_MAX))
    if total > MAX_STONE_FURNACES_PER_LANE:
        return False, ("%d furnaces on one feed lane > %d (%.0f items/min per lane / %.2f per "
                       "furnace)" % (total, MAX_STONE_FURNACES_PER_LANE,
                                     BELT_ITEMS_PER_MIN_PER_LANE, STONE_FURNACE_PLATES_PER_MIN))
    return True, ("%d %sfurnaces = %.2fx the %.1f the mine can feed (ceiling %.1fx)"
                  % (total, (ore + " ") if ore else "", ratio, supported,
                     PREBUILD_OVERBUILD_MAX))


def _pr_pack_producer_live(st, n, g, p):
    recipes = st.get("recipes") or {}
    for pack in SCIENCE_PACKS:
        hist = recipes.get(pack) or {}
        if hist.get("working", 0) > 0:
            return True, "%d assembler(s) working on %s" % (hist["working"], pack)
    built = [k for k in recipes if k in SCIENCE_PACKS]
    return False, ("no assembler is WORKING on a science pack (%s) — build the CONVERTER "
                   "first; 9 labs with 0 pack assemblers took iron -79%%, copper -43%%, "
                   "coal -90%%" % (("built but idle: " + ", ".join(sorted(built)))
                                   if built else "none built"))


def _pr_research_queued(st, n, g, p):
    r = (st.get("research") or "").strip()
    if r:
        return True, "research queued: %s" % r
    return False, ("no research queued — every lab would read no_research_in_progress "
                   "(9/9 did, live)")


def _pr_upstream_not_backed_up(st, n, g, p):
    ff = status_frac(st, "stone-furnace", "full_output")
    dw = status_frac(st, "mining-drill", "waiting_for_space_in_destination")
    if ff >= BACKUP_ALARM_FRAC or dw >= BACKUP_ALARM_FRAC:
        return False, ("upstream is BACKED UP (%.0f%% furnaces full_output, %.0f%% drills "
                       "waiting_for_space) — a new sink cannot help; drain the terminal chest"
                       % (100 * ff, 100 * dw))
    return True, "upstream clear (%.0f%% furnaces full, %.0f%% drills blocked)" % (100 * ff, 100 * dw)


# Ghost machines that count as a RESERVED sink for a non-pack product: the ones a recipe
# consuming that product can actually be set on. Deliberately excludes `lab` (packs only,
# handled by the pack branch) and every passive container (LAW 4: a chest is a buffer).
RESERVABLE_SINKS = ("assembling-machine-1", "assembling-machine-2", "assembling-machine-3",
                    "electric-furnace")


def _pr_sink_exists(st, n, g, p):
    """LAW 1's sink half. `product` names what this stage will emit; something built must
    consume it and not already be choked."""
    product = p.get("product")
    if product is None:
        product = {"science_assembler": "automation-science-pack",
                   "plate_lane": "iron-plate"}.get(p.get("structure"))
    if product is None:
        return False, "no product named — pass params={'product': ...} so the sink is checkable"
    if product in SCIENCE_PACKS:
        # The deadlock this would otherwise create (assembler needs a lab, lab needs pack
        # flow) is exactly what `reserve()` dissolves: a GHOST lab array is a committed sink.
        # It costs nothing, draws nothing, locks nothing, and holds the ground — which is why
        # the 36-lab print went down whole and only 9 were revived.
        labs = int(_f(st.get("counts", {}), "lab"))
        ghosts = int(_f(st.get("ghosts", {}), "lab"))
        if labs > 0:
            return True, "%d lab(s) consume %s" % (labs, product)
        if ghosts > 0:
            return True, ("%d ghost lab(s) reserve the sink for %s — revive them as pack flow "
                          "arrives" % (ghosts, product))
        return False, ("nothing consumes %s: 0 labs built and 0 reserved. reserve() the lab "
                       "array as ghosts first — that is the committed sink, and it is free"
                       % product)
    if p.get("terminal_chest"):
        # LAW 4: exactly one overflow chest at the lane terminus is a legitimate sink, but it
        # is a BUFFER, not a consumer. Live proof: iron-chest(28.5,3.5) filled to 3200 plates
        # -> 25/28 furnaces full_output -> 13/16 drills waiting_for_space -> iron_pm 174 -> 0.
        return True, ("terminal overflow chest declared (LAW 4). WARNING: a full terminal "
                      "chest back-pressures the whole chain — build the real consumer before "
                      "it fills")
    consumers = sorted(r for r, ins in RECIPE_INPUTS.items() if product in ins)
    recipes = st.get("recipes") or {}
    for r in consumers:
        hist = recipes.get(r) or {}
        live = sum(v for k, v in hist.items() if k != "full_output")
        if live > 0:
            return True, "%d machine(s) on %s consume %s" % (live, r, product)
    # The SAME rule the pack branch above already applies: a GHOST is a committed sink. It
    # costs nothing, draws nothing and holds the ground, so a lane laid toward a reserved
    # converter block is a lane with a destination — which is the whole difference between
    # the plate belt he kept and the 33-tile conveyor-to-a-chest he deleted.
    #
    # A GHOST IS A SINK ONLY FOR WHAT THAT MACHINE COULD CONSUME. `lab` belongs to the pack
    # branch above and NOWHERE ELSE: a lab takes science packs and nothing else. Counting
    # ghost labs here made the live base's 25 reserved labs a "sink" for IRON-PLATE and
    # ALLOWED `plate_lane` with nothing at the far end — the 33-tile conveyor-to-a-chest
    # wearing LAW 3's clothes (measured 2026-08-30: ghosts {lab: 25}, 0 assemblers). Only a
    # machine one of `consumers` could actually be set to craft counts.
    gh = int(sum(_f(st.get("ghosts", {}), m) for m in RESERVABLE_SINKS))
    if gh > 0:
        return True, ("%d ghost crafting machine(s) reserve the sink for %s — revive them as "
                      "flow arrives (reserve(), LAW 3)" % (gh, product))
    return False, ("nothing built consumes %s and is not already full_output — he deleted an "
                   "iron-gear-wheel assembler for exactly this" % product)


def _pr_labs_satisfied(st, n, g, p):
    frac = status_frac(st, "lab", "working")
    if int(_f(st.get("counts", {}), "lab")) == 0:
        return False, "no labs built — the mall is LAST, after science is running"
    if frac < 0.8:
        return False, "only %.0f%% of labs working (< 80%%) — finish science before the mall" % (100 * frac)
    return True, "%.0f%% of labs working" % (100 * frac)


def _pr_water_source(st, n, g, p):
    if int(_f(st.get("counts", {}), "boiler")) == 0:
        return True, "first power column: no upstream to verify (power leads)"
    if int(_f(st.get("counts", {}), "offshore-pump")) < 1:
        return False, "no offshore-pump — a boiler column with no water source"
    w = _f(st, "boiler_water_min", -1)
    if 0 <= w < 100:
        return False, "boiler water %.0f < 100 — the manifold is not reaching every boiler" % w
    return True, "water ok (pump present, min boiler water %s)" % ("n/a" if w < 0 else int(w))


def _pr_coal_at_boiler(st, n, g, p):
    if int(_f(st.get("counts", {}), "boiler")) == 0:
        return True, "first power column: fuel it by hand, then belt it"
    c = _f(st, "boiler_coal_min", -1)
    if 0 <= c < 1:
        return False, ("no coal in a boiler — belt coal to the plant BEFORE adding a column "
                       "(a chest buffer is forbidden: he replaced it with a belt + "
                       "burner-inserter)")
    # THE BOUND IS FUELABILITY, NOT A MULTIPLIER. The question a coal gate exists to answer is
    # "can the mine actually run the plant I am about to have?" — so price the whole plant
    # AFTER the build at full tilt (every boiler burning flat out, forever) plus the fed
    # furnaces, and require the mine to cover it.
    #
    # This is strictly conservative: real burn tracks load, and the live plant runs at 11% of
    # it. But it is conservative in the direction that is physically meaningful, and it needs
    # no safety factor on top — which matters, because COAL_HEADROOM_MIN was calibrated
    # against the old full-tilt demand model (120 supplied / 77 "demanded" = 1.56x) and so was
    # a margin derived from the very error it was multiplying. Applying it here refused a
    # column on a base with a 1.32x coal surplus whose power was saturated at 98% load, which
    # is the opposite of what a player does at that moment.
    cap_after_mw = capacity_mw(st) + max(0, int(n)) * BOILER_MW
    need = cap_after_mw * 60.0 / COAL_FUEL_MJ + furnace_coal_per_min(st)
    have = flow(st, "coal")
    # SUPPLY IS WHAT THE MINE CAN DELIVER, NOT WHAT IT DID LAST MINUTE. `flows['coal']` is a
    # one-minute production average, and on a BACK-PRESSURED base production equals
    # consumption, not capacity: measured live 2026-08-30 at 2 coal/min off four drills whose
    # belts were 100% full and whose boilers were refusing coal. Reading a full pipe as an
    # empty one is how this gate demanded "mine more coal" from a mine that was already
    # standing idle. `_pr_overbuild_within_budget` states the same rule for furnaces in so
    # many words ("the denominator is DRILL CAPACITY, not measured plate flow"); this is that
    # rule applied to the one place it was missing.
    cap_mined = drill_capacity_per_min(st, "coal")
    supply = have if cap_mined is None else max(have, cap_mined)
    src = "mined" if cap_mined is None or have >= cap_mined else "the coal drills can deliver"
    # A ZERO reading is the worst reading, not a missing one. Treating `have == 0` as "no data"
    # let this gate ALLOW a boiler column on before.json — a base mining 0 coal/min — while its
    # own approval line printed "0/min mined vs 54/min demand". Absence of the key is the only
    # thing that counts as unknown; sense() and state_from_snapshot both always set it.
    known = "coal" in (st.get("flows") or {}) or cap_mined is not None
    if known and supply < need:
        return False, ("coal %.0f/min < %.0f/min — a %.1f MW plant (%d column(s) added) burns "
                       "%.0f/min at full tilt plus %.0f/min in fed furnaces, and the mine "
                       "cannot fuel that; mine more coal first (a 50/50 splitter tap can only "
                       "deliver half the mine)"
                       % (supply, need, cap_after_mw, max(0, int(n)),
                          cap_after_mw * 60.0 / COAL_FUEL_MJ, furnace_coal_per_min(st)))
    if not known:
        return True, ("coal at the boiler (%s in inventory); coal/min NOT MEASURED in this "
                      "state — the %.0f/min demand for %d boilers is UNCHECKED"
                      % (int(c), need, int(_f(st.get("counts", {}), "boiler")) + n))
    return True, ("coal at the boiler (%s in inventory, %.0f/min %s vs %.0f/min demand)"
                  % (int(c), supply, src, need))


def _pr_chest_is_terminus(st, n, g, p):
    lane_chests = p.get("lane_chests")
    is_term = p.get("is_terminus")
    if lane_chests is None or is_term is None:
        return False, ("cannot evaluate: pass params={'lane_chests': n, 'is_terminus': bool}. "
                       "Lane topology is not visible in a census, and guessing is how 9 "
                       "chest-shuttle pairs got built")
    if not is_term:
        return False, ("this chest is mid-lane — it would DRAIN the lane. Containers appear "
                       "only at a lane's terminus as the final drain (2 on the whole map)")
    if int(lane_chests) >= 1:
        return False, "this lane already has %d chest(s); the budget is 1" % int(lane_chests)
    return True, ("lane terminus, 0 existing chests on the lane (%d container(s) on the whole "
                  "map; his finished base had exactly 2, both terminal)" % containers(st))


# =========================================================================== RELIEF (LAW 5)
# A CONSTRAINT KEY is finer than a predicate name, and it has to be: `flows` is one predicate
# covering twelve items, and "this build relieves flows" would have let a science assembler
# through with no plates because it makes packs. The key carries the item — `flows:coal`,
# `overbuild_within_budget:iron-ore`, `sink_exists:iron-plate` — so a relief can only ever
# cover the exact quantity it raises.
KEYED_BY_ORE = ("overbuild_within_budget",)
KEYED_BY_PRODUCT = ("sink_exists",)
DEFAULT_PRODUCT = {"science_assembler": "automation-science-pack", "plate_lane": "iron-plate"}


def constraint_key(check, params=None):
    """The default constraint key for a failing check. `_pr_flows` overrides this with one key
    per short item; everything else is keyed by the parameter that scopes it."""
    p = params or {}
    if check in KEYED_BY_ORE:
        return "%s:%s" % (check, p.get("ore") or "*")
    if check in KEYED_BY_PRODUCT:
        prod = p.get("product") or DEFAULT_PRODUCT.get(p.get("structure")) or "*"
        return "%s:%s" % (check, prod)
    return check


def constraint_check(key):
    """The predicate a constraint key belongs to ('flows:coal' -> 'flows')."""
    return key.split(":", 1)[0]


def constraint_item(key):
    """The item/ore a constraint key is scoped to, or None."""
    return key.split(":", 1)[1] if ":" in key else None


def _covers(relieves, key):
    """Does a declared relief set cover this constraint key? EXACT keys, plus the explicit
    '<check>:*' wildcard. A bare check name never covers a keyed constraint — that looseness
    is precisely what would turn the exemption into a bypass."""
    if not relieves:
        return None
    for r in relieves:
        if r == key:
            return r
        if r.endswith(":*") and constraint_check(r) == constraint_check(key):
            return r
    return None


def relieves_for(structure, params=None):
    """{constraint key -> why} — the constraints a completed `structure` MEASURABLY increases.

    This is the honest half of LAW 5, and it is short on purpose. A build appears here only
    where the quantity it raises is the quantity the key names; "it helps eventually" is not
    a relief. Note what is ABSENT: `headroom_after` (power_capacity's own sufficiency test on
    the very column being asked for — waiving it would authorise unlimited columns),
    `water_source`, `research_queued`, `lane_capacity`, `chest_is_terminus`. Nothing this
    module can build raises those, so nothing may claim to.
    """
    p = dict(params or {})
    out = {}
    if structure in ("power_capacity", "power_column"):
        out["power_headroom"] = "a boiler column adds %.1f MW of installed capacity" % BOILER_MW
        out["grid_energized"] = "a boiler column IS generation where there was none"
    elif structure == "power_grid":
        out["grid_single"] = "the trunk wires the islands into one network (net 405)"
        out["grid_energized"] = "the trunk is how generation reaches the consumers"
    elif structure in ("mine_outpost", "electric_mining_drill"):
        ore = p.get("ore")
        out[constraint_key("overbuild_within_budget", p)] = (
            "more drills raise the supply the furnace row is measured against")
        out["producer_live"] = "a new outpost is a live producer for the lane behind it"
        if ore:
            out["flows:" + ore] = "more %s drills raise %s/min at the source" % (ore, ore)
            if ore == "coal":
                out["coal_at_boiler"] = "coal drills are the only thing that raises coal/min"
    elif structure in ("ore_lane", "plate_lane"):
        item = p.get("item") or p.get("product")
        if item:
            out["flows:" + item] = "a lane is how %s reaches the machine that consumes it" % item
            if item == "coal":
                out["coal_at_boiler"] = "the coal lane is how mined coal reaches the boiler"
        out["upstream_not_backed_up"] = ("a lane DRAINS the buffer the array is choked on — "
                                         "back-pressure is a missing lane, not a full mine")
        # A drill with nowhere to drop reads waiting_for_space_in_destination, NOT working —
        # so `producer_live` refuses the very lane that would make the drill work. Measured
        # live 2026-08-30: 15 of 16 drills waiting_for_space, one lane short.
        out["producer_live"] = "the lane is what gives a jammed producer somewhere to drop"
    elif structure == "science_assembler":
        recipe = _recipe_of(p) or "automation-science-pack"
        out["flows:" + recipe] = "the converter is the only thing that makes %s" % recipe
        out["pack_producer_live"] = "this IS the pack producer"
        out["upstream_not_backed_up"] = ("a converter consumes the plates the furnaces are "
                                         "full_output on")
        for item in ("iron-plate", "copper-plate"):
            out.setdefault("sink_exists:" + item, "a converter cell consumes %s" % item)
    elif structure == "smelter_array":
        ore = p.get("ore") or ""
        plate = ORE_PLATE.get(ore)
        if plate:
            out["flows:" + plate] = "furnaces are what turn %s into %s" % (ore, plate)
            out["sink_exists:" + ore] = "a furnace row consumes the %s" % ore
    elif structure in ("lab", "lab_array"):
        out["sink_exists:automation-science-pack"] = "a lab is the pack sink"
        out["labs_satisfied"] = "the mall waits on labs actually working"
    return out


# A relief may waive a check only if proceeding CANNOT make that check worse. This is the
# condition that keeps LAW 5 an exemption rather than a bypass: it is measured per predicate
# against the same census, never asserted by the caller. A predicate with no entry here is
# NEVER waivable — `sink_exists`, `grid_single`, `producer_live`, `research_queued`,
# `water_source`, `lane_capacity`, `chest_is_terminus`, `labs_satisfied`, `headroom_after`,
# `pack_producer_live`. Those are facts about the world, not budgets a build can be neutral
# toward.
CONSUMES_BACKLOG = ("science_assembler", "mall_assembler", "plate_lane", "ore_lane",
                    "smelter_array", "lab")


def _inert_power(st, structure, n, p):
    kw = adds_kw_for(structure, p) * n
    if kw > 0:
        return False, "this build would add %.0f kW of load" % kw
    return True, "this build draws 0 kW, so it cannot lower headroom"


def _inert_coal(st, structure, n, p):
    if structure in ("power_capacity", "power_column"):
        return False, "a boiler column BURNS %.0f coal/min per boiler" % BOILER_COAL_PER_MIN
    if structure == "smelter_array":
        return False, "a fed furnace burns %.2f coal/min" % FURNACE_COAL_PER_MIN
    return True, "this build burns no coal"


def _inert_overbuild(st, structure, n, p):
    if structure == "smelter_array":
        return False, "this build IS more furnaces — it raises the ratio it would waive"
    return True, "this build adds no furnaces"


def _inert_backup(st, structure, n, p):
    if structure in CONSUMES_BACKLOG:
        return True, "this build CONSUMES the backed-up product — it is the drain, not a sink"
    return False, "this build does not consume what upstream is choked on"


def _inert_flows(st, structure, n, p, item):
    try:
        need = required_flows(structure, n, _recipe_of(p))
    except KeyError:
        need = {}
    if item in need:
        return False, "this build itself consumes %.1f %s/min" % (need[item], item)
    return True, "this build consumes no %s" % item


def _inert_producer(st, structure, n, p):
    """`producer_live` may be waived for a LANE, and only when the producer exists and is
    JAMMED ON ITS OUTPUT. That is the exact discriminator between the two cases the gate
    cannot otherwise tell apart: a drill reading waiting_for_space_in_destination is not
    working BECAUSE this lane is missing, whereas no drill at all means the lane would be
    the duplicate-lane-ahead-of-its-producer that was 72.4% of the 127 belts he deleted."""
    if structure not in ("ore_lane", "plate_lane"):
        return False, "only a lane can make a jammed producer live again"
    cls = (p or {}).get("producer", "mining-drill")
    jammed = status_count(st, cls, "waiting_for_space_in_destination")
    if jammed <= 0:
        return False, ("no %s is waiting_for_space_in_destination — with no producer behind "
                       "it this is a duplicate lane, not a relief" % cls)
    return True, ("%d %s read waiting_for_space_in_destination: they are idle BECAUSE this "
                  "lane is missing" % (jammed, cls))


RELIEF_INERT = {
    "power_headroom": _inert_power,
    "coal_at_boiler": _inert_coal,
    "overbuild_within_budget": _inert_overbuild,
    "upstream_not_backed_up": _inert_backup,
    "producer_live": _inert_producer,
    "flows": None,          # keyed: handled by _inert_flows(item)
}


def relief_inert(key, st, structure, n, params):
    """(inert, why) — can this build possibly make `key` WORSE? Condition 3 of LAW 5."""
    check = constraint_check(key)
    if check == "flows":
        return _inert_flows(st, structure, n, params or {}, constraint_item(key) or "?")
    fn = RELIEF_INERT.get(check)
    if fn is None:
        return False, ("%s is a fact about the world, not a budget — no build is neutral "
                       "toward it, so it is never waivable" % check)
    return fn(st, structure, n, params or {})


PREDICATES = {
    "flows": _pr_flows,
    "lane_capacity": _pr_lane_capacity,
    "overbuild_within_budget": _pr_overbuild_within_budget,
    "power_headroom": _pr_power_headroom,
    "headroom_after": _pr_headroom_after,
    "grid_single": _pr_grid_single,
    "grid_energized": _pr_grid_energized,
    "producer_live": _pr_producer_live,
    "pack_producer_live": _pr_pack_producer_live,
    "research_queued": _pr_research_queued,
    "upstream_not_backed_up": _pr_upstream_not_backed_up,
    "sink_exists": _pr_sink_exists,
    "labs_satisfied": _pr_labs_satisfied,
    "water_source": _pr_water_source,
    "coal_at_boiler": _pr_coal_at_boiler,
    "chest_is_terminus": _pr_chest_is_terminus,
}


# =========================================================================== the gate
RELIEF_SEARCH_DEPTH = 4       # constraint -> relief -> its blocks -> ... ; a cycle is short


def _waive(row, st, structure, n, p, declared):
    """LAW 5, all three conditions, or no waiver.

    1. THIS BUILD RAISES IT.  The failing constraint key is covered by `declared`, which is
       `relieves_for()` unless the caller narrowed it. Keys carry their item, so relieving
       `flows:coal` never touches `flows:iron-plate`.
    2. THE CONSTRAINT IS DEADLOCKED.  Every build that would raise the FAILING check is
       itself blocked, directly or transitively, by the constraint this build relieves. No
       cycle, no waiver: if there is a legal move that fixes the check properly, take that
       move instead. This is the condition that keeps a relief from being a general excuse.
    3. THIS BUILD CANNOT MAKE IT WORSE.  `relief_inert`, measured against the same census.
       A predicate with no inertness rule is never waivable at all.
    """
    for key in row["keys"]:
        for rel, why in sorted(declared.items()):
            if _covers((rel,), key) is None:
                continue
            inert, iwhy = relief_inert(key, st, structure, n, p)
            if not inert:
                continue
            cyc = deadlock_cycle(key, rel, st, structure)
            if cyc is None:
                continue
            return {"key": rel,
                    "msg": ("RELIEF: [%s] is waived — this build increases %s (%s), %s, and "
                            "%s. Blocked reason was: %s"
                            % (row["check"], rel, why, iwhy, cyc, row["msg"]))}
    return None


def deadlock_cycle(key, relieved, st, structure=None, depth=RELIEF_SEARCH_DEPTH):
    """Condition 2 of LAW 5: is `key` unfixable except through `relieved`?

    Walks the relief graph forward from `key` — every OTHER build that raises `key`, gated
    with no relief of its own, and every constraint those builds are in turn blocked on — and
    reports the path when `relieved` is reached. A string (the path, for the log) or None.

    THE FIRST LEGAL MOVE ENDS THE SEARCH WITH None. If something can fix `key` properly right
    now, that is the build to make, and no waiver is granted — including in the trivial
    `relieved == key` case, where the question is exactly "is there another way to make this
    true". That is what keeps the exemption from becoming a general excuse.
    """
    seen, frontier = {key}, [(key, [key])]
    for level in range(max(1, int(depth))):
        nxt, tried = [], []
        for cur, trail in frontier:
            for cand in relief_candidates(cur, st):
                if cand["structure"] == structure:
                    continue                       # a build cannot justify itself
                tried.append(cand["structure"])
                rep = explain(cand["structure"], cand.get("n", 1), st,
                              cand.get("params"), relieves=())     # NO relief inside the search
                if rep["allowed"]:
                    return None                    # a legal move exists: make THAT move
                for k in rep["blocking"]:
                    if k == relieved and relieved != key:
                        return ("%s -> %s -> %s, which only this build raises"
                                % (" -> ".join(trail), cand["structure"], relieved))
                    if k not in seen:
                        seen.add(k)
                        nxt.append((k, trail + [cand["structure"], k]))
        if relieved == key and level == 0:
            return ("no other build can raise %s right now (tried: %s) — this build is the "
                    "only move left" % (key, ", ".join(sorted(set(tried))) or "none exists"))
        frontier = nxt
        if not frontier:
            break
    return None


def explain(structure, n=1, state=None, params=None, relieves=None):
    """Full per-predicate verdict. `gate()` is the one-line form of this.

    `relieves` is the caller's declaration of what this build INCREASES (LAW 5). Pass None to
    have it derived from `relieves_for(structure, params)`, an explicit tuple to narrow it,
    or `()` to switch relief off entirely (which is what the cycle search does, so a waiver
    can never justify itself).
    """
    g = GATES.get(structure)
    if g is None:
        raise KeyError("unknown structure %r (known: %s)" % (structure, ", ".join(sorted(GATES))))
    st = sense() if state is None else state
    p = dict(params or {})
    p.setdefault("structure", structure)
    declared = relieves_for(structure, p) if relieves is None else {r: "declared by the caller"
                                                                   for r in relieves}
    checks = []
    for name in g["requires"]:
        fn = PREDICATES[name]
        keys = None
        try:
            res = fn(st, n, g, p)
            ok, msg = res[0], res[1]
            keys = list(res[2]) if len(res) > 2 else None
        except KeyError as e:                       # a missing recipe/flow entry is a BLOCK,
            ok, msg = False, "cannot evaluate %s: %s" % (name, e)   # never a silent pass
        if not keys:
            keys = [constraint_key(name, p)]
        row = {"check": name, "ok": bool(ok), "msg": msg, "keys": keys}
        if not ok and declared:
            w = _waive(row, st, structure, n, p, declared)
            if w is not None:
                row.update(ok=True, waived=True, relief=w["key"], msg=w["msg"])
        checks.append(row)
    failed = [c for c in checks if not c["ok"]]
    try:                                            # the same KeyError the predicates treat as
        rf = required_flows(structure, n, _recipe_of(p)) if g["per_unit_flows"] else {}
    except KeyError:                                # a BLOCK must not become a CRASH here: an
        rf = {}                                     # unknown recipe made gate() raise instead
    waived = [c for c in checks if c.get("waived")]
    return {"structure": structure, "n": n, "law": g["law"], "allowed": not failed,   # of refuse
            "checks": checks, "failed": [c["check"] for c in failed],
            "blocking": [k for c in failed for k in c["keys"]],
            "waived": [c["check"] for c in waived],
            "relieves": sorted(declared), "required_flows": rf,
            "note": g["note"], "tick": st.get("tick")}


def gate(structure, n=1, state=None, params=None, relieves=None):
    """(allowed, reason) for building n of `structure` RIGHT NOW.

    Refusing is the normal outcome early in a base and is not an error: the caller logs the
    reason and moves to a stage that IS allowed. `reason` names the first failing check in
    full, because the first failure is the one to go fix.

    When a check is WAIVED under LAW 5 the verdict reads `RELIEF ALLOWED`, never a plain
    `ALLOWED`: a relief build is a deliberate, logged exception and it must never be
    indistinguishable in the log from a build that simply passed.
    """
    return gate_report(structure, n, state, params, relieves)[:2]


def gate_report(structure, n=1, state=None, params=None, relieves=None):
    """(allowed, reason, report) — gate() plus the full explain(), for a caller that needs the
    blocking constraint KEYS as well as the sentence (the deadlock detector does)."""
    rep = explain(structure, n, state, params, relieves)
    if rep["allowed"]:
        ok = [c["msg"] for c in rep["checks"]]
        if rep["waived"]:
            w = next(c for c in rep["checks"] if c.get("waived"))
            return True, ("RELIEF ALLOWED %s x%d — it increases %s, which is what [%s] is "
                          "blocked on: %s"
                          % (structure, n, w["relief"], w["check"], w["msg"])), rep
        return True, ("ALLOWED %s x%d — %s" % (structure, n, "; ".join(ok)) if ok
                      else "ALLOWED %s x%d — %s (no preconditions: %s)"
                           % (structure, n, rep["law"], rep["note"].split(".")[0])), rep
    first = next(c for c in rep["checks"] if not c["ok"])
    extra = len(rep["failed"]) - 1
    return False, ("BLOCKED %s x%d [%s] — %s%s"
                   % (structure, n, first["check"], first["msg"],
                      ("; +%d more (%s)" % (extra, ", ".join(rep["failed"][1:]))) if extra
                      else "")), rep


def relief_key_of(rep):
    """The constraint a report's waiver was granted for, or None."""
    w = next((c for c in (rep or {}).get("checks", ()) if c.get("waived")), None)
    return w["relief"] if w else None


def why_blocked(state=None, structures=None, plan=None):
    """Human-readable list of everything currently refused — for the dashboard and the log.

    `plan` optionally maps structure -> n (and structure -> {"n":.., "params":..}) so the
    report answers "why can't I build the thing I actually want" rather than "x1 of each"."""
    st = sense() if state is None else state
    names = list(structures or [s for s, _ in BUILD_ORDER])
    out = []
    for s in names:
        if s not in GATES:
            continue
        spec = (plan or {}).get(s, 1)
        n, params = (spec, None) if isinstance(spec, int) else (spec.get("n", 1), spec.get("params"))
        ok, why = gate(s, n, st, params)
        if not ok:
            out.append(why)
    return out


# =========================================================================== relief builds
# The INVERSE of relieves_for(): given a constraint, which build raises it. This is the table
# that gives a blocked base a legal move — "mine more coal first" stops being an instruction
# nobody can follow the moment something can name the build that follows it.
MINE_RESOURCES = ("iron-ore", "copper-ore", "coal", "stone")
PLATE_ORE = {v: k for k, v in ORE_PLATE.items()}


def _cand(structure, params, n, key, why):
    return {"structure": structure, "params": dict(params), "n": max(1, int(n)),
            "key": key, "why": why}


def relief_drill(st, n=1):
    """The drill tier a relief mine may actually use RIGHT NOW.

    Electric where the grid can carry it. Where it cannot, the answer is NOT a burner outpost:
    the operator converted every burner drill on this map to electric and deleted the fuel
    belts that fed them, so burners are a tier this base has already left behind. Building 12
    of them to relieve a coal shortage would hand him back the exact infrastructure he removed,
    and we would have to tear it out again to get where he already is.

    So once a single electric drill stands, a relief that cannot be electric returns None and
    the caller must escalate to the real constraint — more POWER — instead of a worse mine.
    Burner remains legal only on a base that has never had an electric drill (a fresh start),
    where it genuinely is the only tier available."""
    add = CONSUMER_KW["electric-mining-drill"] * max(1, int(n))
    if (capacity_mw(st) > 0 and int(_f(st, "networks")) == 1
            and headroom(st, add) >= POWER_HEADROOM_MIN):
        return "electric-mining-drill"
    if int(_f(st.get("counts", {}), "electric-mining-drill")) > 0:
        return None
    return "burner-mining-drill"


def drills_needed(st, ore, deficit_per_min, drill=None):
    """How many drills close a per-minute deficit, at the tier that is legal now."""
    drill = drill or relief_drill(st) or "electric-mining-drill"
    rate = DRILL_ORE_PER_MIN.get(drill, ELECTRIC_DRILL_ORE_PER_MIN)
    return max(1, int(math.ceil(max(0.0, float(deficit_per_min)) / rate)))


def coal_deficit(st, extra_boilers=0):
    """coal/min still missing to clear the gate. Supply is what the drills CAN deliver (see
    _pr_coal_at_boiler); 0 when there is no gap. The threshold is computed the SAME way the
    gate computes it — the plant after the build, at full tilt, plus fed furnaces — so a
    relief sized from this deficit is one the gate will actually pass."""
    cap_after = capacity_mw(st) + max(0, int(extra_boilers)) * BOILER_MW
    need = cap_after * 60.0 / COAL_FUEL_MJ + furnace_coal_per_min(st)
    cap = drill_capacity_per_min(st, "coal")
    have = flow(st, "coal") if cap is None else max(flow(st, "coal"), cap)
    return max(0.0, need - have)


def columns_for_headroom(st, projected_kw=0.0):
    """Boiler columns needed for capacity to lead the load by POWER_HEADROOM_MIN. The same
    inequality _pr_headroom_after solves, so a relief column count is one the gate can pass."""
    load = load_mw(st, projected_kw)
    if load <= 0:
        return 1
    return max(1, int(math.ceil((POWER_HEADROOM_MIN * load - capacity_mw(st)) / BOILER_MW)))


def _asked_for(requests, structures):
    """The largest n any BLOCKED request wanted of these structures, so a quantity-shaped
    relief is sized to the build it has to unblock rather than to a placeholder. Sizing the
    coal relief for "one more boiler" while the plant stage is asking for four is how a relief
    gets planned, built, VERIFIED - and still leaves the gate refusing."""
    best = 0
    for s, n in (requests or ()):
        if s in structures:
            best = max(best, int(n or 0))
    return best


def relief_candidates(key, st=None, requests=None):
    """Builds that raise the constraint `key`, cheapest-and-most-legal first.

    ORDER MATTERS AND IS DELIBERATE: delivery before production. A belt lane costs 0 kW, 0
    ore and no new machines, and `supply_planner.plan_supply` refuses a duplicate outright
    (handing back the lane that already serves the pair), so proposing a lane that already
    exists is free and self-correcting. Only when the lane is done — the caller passes its
    key in `done` — does the ladder escalate to the expensive fix, more drills.

    `requests` is [(structure, n)] of what is actually BLOCKED, so the relief can be sized to
    close the real gap.
    """
    st = st or {}
    check, item = constraint_check(key), constraint_item(key)
    out = []

    def mine(ore, n, why):
        """A mine-outpost candidate, or the POWER candidate when no legal drill tier exists.

        relief_drill returns None on a base that already runs electric drills but cannot
        currently carry another one. That is not "no relief available" — it is the relief
        being one step further up the chain: the mine is waiting on capacity, so propose the
        boiler column instead of silently dropping the candidate (which would re-create the
        dead end this whole ladder exists to remove)."""
        drill = relief_drill(st, n)
        if drill is None:
            return [_cand("power_capacity", {"projected_load_kw":
                                             CONSUMER_KW["electric-mining-drill"] * n},
                          columns_for_headroom(
                              st, CONSUMER_KW["electric-mining-drill"] * n), "plant",
                          "%s needs more drills, and this base mines with ELECTRIC drills — "
                          "so the binding constraint is generation, not the mine" % ore)]
        return [_cand("mine_outpost", {"ore": ore, "drill": drill}, n, "mine:" + ore, why)]

    if check == "coal_at_boiler":
        out.append(_cand("ore_lane", {"item": "coal", "producer": "mining-drill", "drills": 1},
                         1, "lane:coal",
                         "the coal lane is how mined coal reaches the boiler — belts only, "
                         "0 kW, no new machine"))
        cols = max(1, _asked_for(requests, ("power_capacity", "power_column")))
        out += mine("coal", drills_needed(st, "coal", coal_deficit(st, cols)),
                    "coal drills are the only thing that raises coal/min at the source")
    elif check in ("power_headroom", "grid_energized"):
        out.append(_cand("power_capacity", {"projected_load_kw": 0.0},
                         columns_for_headroom(st), "plant",
                         "a boiler column is the only build that raises installed capacity"))
        if check == "grid_energized":
            out.append(_cand("power_grid", {}, 1, "spine",
                             "the trunk carries that generation to the consumers"))
    elif check == "grid_single":
        out.append(_cand("power_grid", {}, 1, "spine",
                         "one wired trunk is what collapses the islands into one network"))
    elif check == "overbuild_within_budget":
        ore = item if item in MINE_RESOURCES else "iron-ore"
        out += mine(ore, 1, "the overbuild ratio's denominator is DRILL CAPACITY — raise it")
    elif check == "producer_live":
        # WHICH build fixes "no drill is working" depends on WHY none is. A drill jammed at
        # waiting_for_space_in_destination is idle for want of the lane; no drill at all is a
        # different problem with a different build, and conflating the two is how a lane gets
        # laid ahead of its producer.
        if status_count(st, "mining-drill", "waiting_for_space_in_destination") > 0:
            out.append(_cand("ore_lane", {"producer": "mining-drill", "drills": 1}, 1, "lane:*",
                             "a jammed producer needs somewhere to drop, which is the lane"))
        else:
            out += mine("iron-ore", 1, "a lane needs a live producer behind it")
    elif check == "pack_producer_live":
        out.append(_cand("science_assembler", {"recipe": "automation-science-pack"}, 1,
                         "science", "the converter IS the pack producer"))
    elif check == "upstream_not_backed_up":
        out.append(_cand("science_assembler", {"recipe": "automation-science-pack"}, 1,
                         "science", "a converter drains the plates the furnaces choke on"))
        out.append(_cand("plate_lane", {"item": "iron-plate", "product": "iron-plate"}, 1,
                         "lane:iron-plate", "a plate lane drains the array into its consumer"))
    elif check == "sink_exists":
        if item in SCIENCE_PACKS:
            out.append(_cand("lab", {}, 1, "labs", "a lab is the pack sink"))
        else:
            out.append(_cand("science_assembler", {"recipe": "automation-science-pack"}, 1,
                             "science", "a converter cell is what consumes plates"))
    elif check == "flows":
        if item in MINE_RESOURCES:
            out.append(_cand("ore_lane", {"item": item, "producer": "mining-drill", "drills": 1},
                             1, "lane:" + item, "a lane delivers %s to its consumer" % item))
            out += mine(item, 1, "more drills raise %s/min at the source" % item)
        elif item in PLATE_ORE:
            ore = PLATE_ORE[item]
            out.append(_cand("smelter_array", {"ore": ore}, 1, "array:" + ore,
                             "furnaces are what turn %s into %s" % (ore, item)))
            out.append(_cand("plate_lane", {"item": item, "product": item}, 1, "lane:" + item,
                             "a plate lane delivers %s to its consumer" % item))
        elif item in SCIENCE_PACKS:
            out.append(_cand("science_assembler", {"recipe": item}, 1, "science",
                             "the converter is the only thing that makes %s" % item))
    return out


def blocking_constraints(state=None, plan=None, structures=None, blocked=None):
    """Every constraint currently refusing a build, ranked by how many builds it refuses.

    `blocked` (a list of explain()-shaped dicts, i.e. what a build pass actually tried) is
    preferred over re-gating BUILD_ORDER, because the pass knows the n and the params it
    wanted and a speculative x1 of each does not.
    """
    agg = {}
    if blocked:
        rows = [(b.get("structure"), b.get("n", 1), b.get("blocking") or []) for b in blocked]
    else:
        st = sense() if state is None else state
        rows = []
        for s in (structures or [s for s, _ in BUILD_ORDER]):
            if s not in GATES:
                continue
            spec = (plan or {}).get(s, 1)
            n, params = ((spec, None) if isinstance(spec, int)
                         else (spec.get("n", 1), spec.get("params")))
            rep = explain(s, n, st, params, relieves=())      # the RAW verdict, no waivers
            if not rep["allowed"]:
                rows.append((s, n, rep["blocking"]))
    for s, n, keys in rows:
        for k in keys:
            a = agg.setdefault(k, {"constraint": k, "check": constraint_check(k),
                                   "blocks": [], "requests": []})
            if s and s not in a["blocks"]:
                a["blocks"].append(s)
            if s:
                a["requests"].append((s, n))
    out = list(agg.values())
    for r in out:
        r["n_blocks"] = len(r["blocks"])
    out.sort(key=lambda r: (-r["n_blocks"], r["constraint"]))
    return out


def _attribution_keys(attribution):
    """bottleneck.top_cause() -> the constraint keys its missing ingredient corresponds to."""
    miss = (attribution or {}).get("missing")
    if not miss:
        return ()
    keys = ["flows:" + miss]
    if miss == "coal":
        keys.append("coal_at_boiler")
    if miss in ORE_PLATE:
        keys.append("overbuild_within_budget:" + miss)
    return tuple(keys)


def _live_attribution(window_s=600):
    """bottleneck.py's ranking, if the ring has anything in it. Imported LAZILY and never
    allowed to raise: attribution SHARPENS the choice of relief, it does not gate it."""
    try:
        import bottleneck
        return bottleneck.top_cause(window_s)
    except Exception:
        return None


ATTRIBUTION_WEIGHT = 10.0     # a measured bottleneck outranks any breadth count


def next_relief(state=None, plan=None, blocked=None, done=(), attribution=None,
                structures=None, window_s=600):
    """THE SINGLE HIGHEST-VALUE RELIEF BUILD — the legal move a stuck base still has.

    Ranking is breadth (how many structures this constraint refuses) plus bottleneck.py's
    attribution, which is the only input here measured at the machines rather than derived
    from a census: if the base's starved machines say they are missing coal, the coal
    constraint outranks a constraint that merely blocks more gate rows.

    `done` carries the relief keys already built (or already proved un-buildable), so the
    ladder ESCALATES instead of proposing the same lane forever. Returns None when nothing is
    legal — which is a real answer, and the caller must say so rather than idle silently.
    """
    st = sense() if state is None else state
    rows = blocking_constraints(st, plan=plan, structures=structures, blocked=blocked)
    if not rows:
        return None
    if attribution is None:
        attribution = _live_attribution(window_s)
    hot = set(_attribution_keys(attribution))
    for r in rows:
        r["score"] = r["n_blocks"] + (ATTRIBUTION_WEIGHT if r["constraint"] in hot else 0.0)
    rows.sort(key=lambda r: (-r["score"], -r["n_blocks"], r["constraint"]))
    done = set(done or ())
    for r in rows:
        for cand in relief_candidates(r["constraint"], st, r.get("requests")):
            if cand["key"] in done:
                continue
            ok, why = gate(cand["structure"], cand["n"], st, cand["params"],
                           relieves=(r["constraint"],))
            if not ok:
                continue
            out = dict(cand)
            out.update(constraint=r["constraint"], unblocks=list(r["blocks"]),
                       score=r["score"], gate_reason=why,
                       attributed=r["constraint"] in hot,
                       reason=("%s x%d increases %s, which is blocking %s"
                               % (cand["structure"], cand["n"], r["constraint"],
                                  ", ".join(r["blocks"]) or "the pass")))
            return out
    return None


def deadlock(state=None, plan=None, blocked=None, done=(), attribution=None, structures=None):
    """The one line a stuck pass owes the log, plus the relief to attempt next pass.

    A builder that produces nothing and refuses something is not "idle" — it is DEADLOCKED,
    and the difference is the whole point of this function. Returns None when nothing is
    blocked (there is no deadlock to report).
    """
    st = sense() if state is None else state
    rows = blocking_constraints(st, plan=plan, structures=structures, blocked=blocked)
    if not rows:
        return None
    relief = next_relief(st, plan=plan, blocked=blocked, done=done, attribution=attribution,
                         structures=structures)
    binding = relief["constraint"] if relief else rows[0]["constraint"]
    row = next((r for r in rows if r["constraint"] == binding), rows[0])
    if relief:
        move = ("%s x%d %s — %s" % (relief["structure"], relief["n"],
                                    json.dumps(relief["params"], sort_keys=True), relief["why"]))
    else:
        cands = [c["structure"] for c in relief_candidates(binding, st, row.get("requests"))]
        move = ("NONE IS LEGAL (candidates: %s) — this needs a human"
                % (", ".join(cands) if cands else "none known"))
    return {"constraint": binding, "blocks": row["blocks"], "relief": relief,
            "candidates": rows[:4],
            "line": ("DEADLOCK: %s is the binding limit; it blocks %s; relief build = %s"
                     % (binding, ", ".join(row["blocks"]) or "every gated stage", move))}


# =========================================================================== clearance
# Measured as CLEAR TILES between the occupied-tile bounding boxes of his functional areas.
MIN_CLEARANCE = {
    ("smelter_array", "smelter_array"): 3,     # rows 9,10,11 inside the machine x-span
    ("smelter_array", "consumer_block"): 12,   # copper array y=17 -> lab pole row y=30
    ("smelter_array", "power_plant"): 16,      # copper 16, iron 25
    ("power_plant", "consumer_block"): 21,
    ("mine_outpost", "mine_outpost"): 14,      # coal <-> iron
    ("mine_outpost", "any_base_block"): 23,    # min observed (copper mine <-> copper array)
    ("mine_outpost", "power_plant"): 14,
    ("any_block", "any_block"): 12,            # floor for any pair not listed
}
# A clearance corridor is NOT empty — it is where the pole trunk and the transit lanes live.
CLEARANCE_MAY_CONTAIN = ("electric-pole trunk", "transit belt lane", "underground crossing")
CLEARANCE_MAY_NOT_CONTAIN = ("machine", "chest", "assembler", "lab", "furnace")

KIND_ALIAS = {"lab_array": "consumer_block", "labs": "consumer_block",
              "mall": "consumer_block", "science_block": "consumer_block",
              "assembler_block": "consumer_block", "mine": "mine_outpost",
              "mine_outpost": "mine_outpost", "smelter": "smelter_array",
              "smelting_base": "smelter_array", "plant": "power_plant",
              "power": "power_plant", "power_plant": "power_plant"}
BASE_BLOCKS = ("smelter_array", "consumer_block", "power_plant")
# Corridors are exempt: they are what the clearance is FOR.
CORRIDOR_KINDS = ("corridor", "bus", "grid", "trunk", "lane")
ROLE_KIND = {"mine": "mine_outpost", "smelter": "smelter_array", "power": "power_plant",
             "science": "consumer_block", "lab": "consumer_block", "mall": "consumer_block",
             "bus": "corridor", "grid": "corridor", "rail": "corridor"}


def norm_kind(kind):
    return KIND_ALIAS.get(kind, kind)


def min_clearance(a, b):
    """Required clear tiles between two area kinds, with the measured fallbacks."""
    a, b = norm_kind(a), norm_kind(b)
    for key in ((a, b), (b, a)):
        if key in MIN_CLEARANCE:
            return MIN_CLEARANCE[key]
    for mine, other in ((a, b), (b, a)):
        if mine == "mine_outpost" and other in BASE_BLOCKS:
            return MIN_CLEARANCE[("mine_outpost", "any_base_block")]
    return MIN_CLEARANCE[("any_block", "any_block")]


def separation(a, b):
    """Clear tiles between two INCLUSIVE tile bboxes (x1,y1,x2,y2). Negative = they overlap."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    gx = max(bx1 - ax2 - 1, ax1 - bx2 - 1)
    gy = max(by1 - ay2 - 1, ay1 - by2 - 1)
    return max(gx, gy)


def known_areas():
    """Functional areas already on the map, from the world registry (role -> kind). Corridors
    are omitted: a pole trunk or a transit lane is allowed inside a clearance gap."""
    try:
        import world
    except ImportError:
        return []
    areas, seen = [], {}
    for rec in world.query():
        role = rec.get("role")
        kind = ROLE_KIND.get(role, "any_block")
        if kind in CORRIDOR_KINDS:
            continue
        tp = rec.get("tile_pos") or [rec.get("x"), rec.get("y")]
        if tp is None or tp[0] is None:
            continue
        x, y = int(tp[0]), int(tp[1])
        b = seen.get(role)
        seen[role] = (x, y, x, y) if b is None else (min(b[0], x), min(b[1], y),
                                                     max(b[2], x), max(b[3], y))
    for role, box in sorted(seen.items()):
        areas.append({"name": role, "kind": ROLE_KIND.get(role, "any_block"), "bbox": box})
    return areas


def clearance_ok(area, kind, existing=None):
    """(ok, reason) for siting a `kind` area at inclusive tile bbox `area`.

    No cramming: the operator's blocks are DENSE inside and generously separated outside,
    because the gap is not waste — it carries the pole trunk, the transit lanes and the
    underground crossings, and it is what let him add a second boiler column without
    deleting anything. (He had to delete a lab to place one engine; that lab sat exactly on
    the engine's 3x5 footprint.)"""
    area = tuple(int(v) for v in area)
    if area[0] > area[2] or area[1] > area[3]:
        return False, "bbox %s is inverted (want x1<=x2, y1<=y2)" % (area,)
    others = known_areas() if existing is None else list(existing)
    worst = None
    for o in others:
        okind = norm_kind(o.get("kind", "any_block"))
        if okind in CORRIDOR_KINDS:
            continue
        need = min_clearance(kind, okind)
        sep = separation(area, tuple(int(v) for v in o["bbox"]))
        if sep < need and (worst is None or (sep - need) < worst[0]):
            worst = {"slack": sep - need, "o": o, "okind": okind, "need": need, "sep": sep}
    if worst is None:
        n = len(others)
        return True, ("clear: %d existing area(s), every gap >= its measured minimum" % n
                      if n else "clear: no existing functional areas")
    o, okind, need, sep = worst["o"], worst["okind"], worst["need"], worst["sep"]
    if sep < 0:
        return False, ("%s %s OVERLAPS %s (%s) %s — never cram: a consumer built in an "
                       "expansion lane has to be DELETED to scale (a lab sat exactly on the "
                       "3x5 footprint of the engine that replaced it)"
                       % (norm_kind(kind), area, o.get("name", "?"), okind, tuple(o["bbox"])))
    return False, ("%s %s is %d tile(s) from %s (%s) %s; the measured minimum is %d. "
                   "The gap carries %s — it is not spare space"
                   % (norm_kind(kind), area, sep, o.get("name", "?"), okind,
                      tuple(o["bbox"]), need, "/".join(CLEARANCE_MAY_CONTAIN)))


# =========================================================================== reserve
UNIT_STRUCTURE = {"lab": "lab", "assembling-machine-1": "science_assembler",
                  "electric-mining-drill": "mine_outpost", "stone-furnace": "smelter_array"}
POLE_NAMES = ("small-electric-pole", "medium-electric-pole", "big-electric-pole", "substation")


def _size(name):
    try:
        from principles import PROTO_SIZE
    except ImportError:
        PROTO_SIZE = {}
    return PROTO_SIZE.get(name, (1, 1))


def _bbox_of(ents):
    x1 = y1 = 10 ** 9
    x2 = y2 = -10 ** 9
    for e in ents:
        w, h = _size(e["name"])
        lx = int(math.floor(e["x"] - w / 2.0))
        ly = int(math.floor(e["y"] - h / 2.0))
        x1, y1 = min(x1, lx), min(y1, ly)
        x2, y2 = max(x2, lx + w - 1), max(y2, ly + h - 1)
    return (x1, y1, x2, y2) if x2 >= x1 else (0, 0, -1, -1)


def _ghost_lua(ents, batch=40):
    """/sc commands that stamp entity-ghosts. RETURNED, NEVER RUN — placement belongs to
    buildplan/executor, which own the truce check, the protected-tile ledger and rollback."""
    cmds = []
    for i in range(0, len(ents), batch):
        rows = ",".join("{'%s',%s,%s,%d}" % (e["name"], e["x"], e["y"], int(e.get("direction", 0)))
                        for e in ents[i:i + batch])
        cmds.append("/sc local s=game.surfaces[1];local n=0;"
                    "for _,g in pairs({%s}) do "
                    "if s.create_entity{name='entity-ghost',inner_name=g[1],"
                    "position={g[2],g[3]},direction=g[4],force='player'} then n=n+1 end end;"
                    "rcon.print(n)" % rows)
    return cmds


def _ghost_count_lua(bbox, names):
    """READ-ONLY: count the entity-ghosts in `bbox` whose ghost_name is one of `names`.
    Compare against plan["expect_ghosts"] — a bare "there are ghosts here" also counts the
    neighbouring build's."""
    want = "".join("['%s']=1," % n for n in names)
    return ("/sc local s=game.surfaces[1];local W={%s};local n=0;"
            "for _,g in pairs(s.find_entities_filtered{area={{%d,%d},{%d,%d}},"
            "name='entity-ghost'}) do local ok,gn=pcall(function() return g.ghost_name end);"
            " if ok and W[gn] then n=n+1 end end;rcon.print(n)"
            % (want, bbox[0], bbox[1], bbox[2] + 1, bbox[3] + 1))


def reserve(footprint, unit=None, n=None, state=None, feed=None, support_radius=3.0,
            recipe=None, kind=None):
    """Stamp the FULL design as ghosts; build only the fraction the flows afford.

    This is LAW 3's escape hatch. An unfed ELECTRIC consumer is deleted on sight, so the way
    to hold ground for one is a ghost: it locks the footprint, costs nothing, draws nothing
    and reads as no progress. The operator's own 36-lab print went down whole at (10,40) and
    exactly 9 labs were revived — 110 ghosts ARE the reservation, and expansion is just
    reviving more as pack flow arrives.

    footprint : [{"name","x","y",["direction"]}] — x,y are ENTITY CENTRES (create_entity
                positions), the same convention as snapshot_map and the measured specs.
    unit      : the entity name that counts as one unit of capacity (e.g. "lab").
    n         : units to revive now; default = affordable_count() from the live/given state.
    feed      : (x,y) the supply arrives from; the revived units are the n nearest to it,
                which reproduces "the 3 westmost in the 3 rows nearest the feed".

    Returns a PLAN. Nothing is placed. Hand `ghost_lua` to the executor, then revive
    `revive` through the normal place-and-verify path — build a `buildplan.new_plan()` around
    it (this dict is not one) so rollback is scoped to exactly what you placed, and honour
    `wire_required`: script-placed poles do not auto-connect.

    `revive` is gated: the caller-visible `gate_ok`/`gate_reason` carry gate()'s verdict over
    the revive set, and a refusal empties `revive` while leaving every ghost standing. Ghosts
    are free; reviving is what LAW 1 governs.
    """
    ents = footprint.get("entities") if isinstance(footprint, dict) else footprint
    ents = [dict(e) for e in (ents or [])]
    if not ents:
        raise ValueError("empty footprint: nothing to reserve")
    for e in ents:
        if "name" not in e or "x" not in e or "y" not in e:
            raise ValueError("footprint entry needs name/x/y: %r" % (e,))
        # `dir` is what autopilot.stamp_blueprint and mine_layout.to_ghosts emit; `direction`
        # is what executor orders carry. Accepting only one silently reserved every belt,
        # inserter and drill of a mine_layout plan facing NORTH.
        if "direction" not in e and "dir" in e:
            e["direction"] = e["dir"]
        e.setdefault("direction", 0)

    units = [e for e in ents if unit and e["name"] == unit]
    structure = UNIT_STRUCTURE.get(unit or "", unit if unit in GATES else None)
    reason_bits = []
    st = state
    if n is None:
        if structure and units:
            st = sense() if st is None else st
            n = affordable_count(structure, st, recipe=recipe, cap=len(units))
            reason_bits.append("affordable_count(%s)=%d of %d %s"
                               % (structure, n, len(units), unit))
        else:
            n = 0
            reason_bits.append("no unit gate given: reserving everything, reviving nothing")
    else:
        n = max(0, min(int(n), len(units) if units else int(n)))
        reason_bits.append("caller asked for %d" % n)

    if feed is not None:
        units.sort(key=lambda e: (abs(e["x"] - feed[0]) + abs(e["y"] - feed[1]), e["y"], e["x"]))
    else:
        units.sort(key=lambda e: (e["y"], e["x"]))
    chosen = units[:n]
    # affordable_count() only reads FLOWS and POWER. The other half of LAW 1 — is there a live
    # pack producer, is research queued, is upstream already backed up — is not in it, so
    # reserve() used to hand back a revive list that gate() refuses: 9 labs on a base with no
    # pack assembler and no research, which is the exact build this module exists to stop.
    # Ghosting the whole print is still free and still correct; only the REVIVE is gated.
    gate_ok, gate_reason = None, "revive gate not evaluated (no state and no live sense)"
    if chosen and structure and st is not None:
        gate_ok, gate_reason = gate(structure, len(chosen), st,
                                    {"recipe": recipe} if recipe else None)
        if not gate_ok:
            chosen, n = [], 0
            reason_bits.append("REVIVE REFUSED: %s" % gate_reason)
    elif not chosen:
        gate_ok, gate_reason = True, "nothing to revive"
    chosen_ids = {id(e) for e in chosen}
    revive = list(chosen)
    # A support entity belongs to its NEAREST unit, and is revived only if that unit is.
    # Ownership matters: radius alone would drag in the inserter of the lab next door, and an
    # inserter with nothing to feed reads `waiting_for_target_to_be_built` — the debt status
    # that is exactly what "built something that does nothing" looks like.
    if chosen and support_radius and units:
        for e in ents:
            if id(e) in chosen_ids or (unit and e["name"] == unit):
                continue
            d2 = [((e["x"] - u["x"]) ** 2 + (e["y"] - u["y"]) ** 2, u) for u in units]
            best = min(d for d, _u in d2)
            # A lattice puts seam cells exactly between two units. Ownership is genuinely
            # shared there, so an equidistant tie resolves toward a CHOSEN unit — that pole
            # really does power the revived lab.
            owners = [u for d, u in d2 if d == best and id(u) in chosen_ids]
            if owners and math.sqrt(best) <= support_radius:
                revive.append(e)
    revive_ids = {id(e) for e in revive}
    reserved = [e for e in ents if id(e) not in revive_ids]
    bbox = _bbox_of(ents)
    poles = [(e["x"], e["y"]) for e in revive if e["name"] in POLE_NAMES]
    if poles:
        reason_bits.append("WIRE THE %d REVIVED POLE(S)" % len(poles))
    plan = {
        "kind": "reserve", "unit": unit, "structure": structure,
        "bbox": list(bbox), "total": len(ents),
        "unit_total": len(units), "affordable_units": n,
        "ghosts": ents, "revive": revive, "reserved": reserved,
        "gate_ok": gate_ok, "gate_reason": gate_reason,
        "ghost_lua": _ghost_lua(ents),
        # Scoped to THIS plan's prototypes and compared against a number, not a bare "some
        # ghosts exist": an unscoped count in a bbox also counts a neighbouring build's ghosts.
        "expect_ghosts": len(ents),
        "verify_lua": _ghost_count_lua(bbox, sorted({e["name"] for e in ents})),
        # CRITICAL API GOTCHA: script-placed poles do NOT reliably auto-connect. reserve() ships
        # poles inside `revive`, so the caller inherits that obligation — it is recorded here
        # rather than left implicit, because an unwired pole row reads as a built, silent grid.
        "revive_poles": poles,
        "wire_required": bool(poles),
        "wire_hint": ("after reviving, wire every pole pair within reach explicitly "
                      "(power_planner.wire_pairs -> wire_lua, i.e. get_wire_connector("
                      "defines.wire_connector_id.pole_copper,true).connect_to(other,false)) "
                      "and VERIFY by comparing electric_network_id — not by 'the pole exists'"),
        "clearance_kind": kind or ("consumer_block" if unit == "lab" else "any_block"),
        "reason": ("reserve %d entities as ghosts (%s), revive %d unit(s) + %d support = %d; "
                   "%d ghosts stay standing as the reservation"
                   % (len(ents), "; ".join(reason_bits), n, len(revive) - n, len(revive),
                      len(reserved))),
        "law": LAW_PASSIVE_ONLY,
    }
    return plan


# =========================================================================== live state
_CACHE = {"t": 0.0, "state": None}

SENSE_LUA = r"""/sc
local s=game.surfaces[1]
local f=game.forces.player
local SN={} for k,v in pairs(defines.entity_status) do SN[v]=k end
local counts,ctype,stat,stype,recipes,ghosts,byore={},{},{},{},{},{},{}
local nets={} local nn=0
local bwater,bcoal=-1,-1
local gen=0
local function bump(t,k,v) if k then t[k]=(t[k] or 0)+(v or 1) end end
for _,e in pairs(s.find_entities_filtered{force='player'}) do
 local n=e.name
 if n=='entity-ghost' then
  local ok,inm=pcall(function() return e.ghost_name end)
  bump(ghosts, ok and inm or '?')
 elseif n~='character' then
  bump(counts,n) bump(ctype,e.type)
  local st='?'
  local oks,sv=pcall(function() return e.status end)
  if oks and sv~=nil then st=SN[sv] or tostring(sv) end
  stat[n]=stat[n] or {} bump(stat[n],st)
  stype[e.type]=stype[e.type] or {} bump(stype[e.type],st)
  local oke,eid=pcall(function() return e.electric_network_id end)
  if oke and eid and not nets[eid] then nets[eid]=true nn=nn+1 end
  if e.type=='assembling-machine' or e.type=='furnace' then
   local rn=nil
   local okr,r=pcall(function() return e.get_recipe() end)
   if okr and r then rn=r.name else
    local okp,p=pcall(function() return e.previous_recipe end)
    if okp and p then local nm=p.name
     if type(nm)=='string' then rn=nm elseif nm then rn=nm.name end end end
   if rn then recipes[rn]=recipes[rn] or {} bump(recipes[rn],st) end
  end
  if e.type=='mining-drill' then
   local okm,mt=pcall(function() return e.mining_target end)
   local res=(okm and mt and mt.name) or '?'
   byore[res]=byore[res] or {} bump(byore[res],n)
  end
  if n=='boiler' then
   local okw,w=pcall(function() return e.get_fluid_count('water') end)
   if okw and w then if bwater<0 or w<bwater then bwater=w end end
   local okf,fi=pcall(function() return e.get_fuel_inventory() end)
   if okf and fi then local c=fi.get_item_count('coal')
    if bcoal<0 or c<bcoal then bcoal=c end end
  end
  if n=='steam-engine' then
   local okg,g=pcall(function() return e.energy_generated_last_tick end)
   if okg and g then gen=gen+g end
  end
 end
end
local ps=f.get_item_production_statistics(s)
local function pm(n) return ps.get_flow_count{name=n,category='input',
  precision_index=defines.flow_precision_index.one_minute} end
local flows={}
for _,it in pairs({__ITEMS__}) do flows[it]=pm(it) end
storage._gates=helpers.table_to_json({tick=game.tick,counts=counts,counts_type=ctype,
 status=stat,status_type=stype,recipes=recipes,ghosts=ghosts,networks=nn,drills_by_ore=byore,
 boiler_water_min=bwater,boiler_coal_min=bcoal,generated_kw=math.floor(gen*60/1000),
 research=(f.current_research and f.current_research.name or ''),flows=flows})
rcon.print(#storage._gates)
"""


def sense(ttl=5.0, force=False, items=FLOW_ITEMS):
    """One READ-ONLY census + flow read -> the state every gate is a pure function of.

    Chunked through `storage._gates` (its own key, so it never races bottleneck's _bn,
    architect's _arch, world's _world, principles' _principles or dashboard's _dash), and the
    key is cleared afterwards. A failed scan RAISES: a gate that silently sees an empty world
    would allow everything, which is the exact failure this module exists to stop."""
    now = time.time()
    if not force and _CACHE["state"] is not None and now - _CACHE["t"] < ttl:
        return _CACHE["state"]
    import rcon
    lua = SENSE_LUA.replace("__ITEMS__", ",".join("'%s'" % i for i in items)).replace("\n", " ")
    raw = (rcon.run(lua) or "").strip()
    if not raw.lstrip("-").isdigit():
        raise RuntimeError("build_gates.sense failed (RCON/Lua): %s" % (raw[:200] or "(empty)"))
    n = int(raw)
    if n <= 0:
        raise RuntimeError("build_gates.sense built a %d-length payload" % n)
    parts, i = [], 1
    try:
        while i <= n:
            parts.append(rcon.run("/sc rcon.print(storage._gates:sub(%d,%d))"
                                  % (i, i + CHUNK - 1)).rstrip("\r\n"))
            i += CHUNK
    finally:
        # ALWAYS clear the scratch key, including when a chunk read throws mid-way — a stale
        # storage._gates would otherwise be re-read as a fresh census by the next caller.
        rcon.run("/sc storage._gates=nil")
    st = json.loads("".join(parts))
    st.setdefault("ts", now)
    _CACHE["t"], _CACHE["state"] = now, st
    return st


def state_from_snapshot(name):
    """Offline state from a snapshot_map.py capture — the fixture that proves the arithmetic:
    before.json reproduces headroom 1.079 and 2 networks, after.json 1.603 and 1."""
    p = pathlib.Path(name)
    if not p.exists():
        p = SNAPDIR / ("%s.json" % name)
    data = json.loads(p.read_text())
    g = data.get("globals", {})
    st = {"tick": g.get("tick"), "research": (g.get("research") or "").strip(),
          "counts": {}, "counts_type": {}, "status": {}, "status_type": {},
          "recipes": {}, "ghosts": {}, "networks": 0,
          "boiler_water_min": -1, "boiler_coal_min": -1,
          "flows": {"iron-plate": _f(g, "iron_pm"), "copper-plate": _f(g, "copper_pm"),
                    "coal": _f(g, "coal_pm"),
                    "automation-science-pack": _f(g, "red_pm"),
                    "logistic-science-pack": _f(g, "green_pm")}}
    nets = set()
    for e in data.get("ents", []):
        n, t, s = e.get("n"), e.get("t"), e.get("s") or "?"
        st["counts"][n] = st["counts"].get(n, 0) + 1
        st["counts_type"][t] = st["counts_type"].get(t, 0) + 1
        st["status"].setdefault(n, {})
        st["status"][n][s] = st["status"][n].get(s, 0) + 1
        st["status_type"].setdefault(t, {})
        st["status_type"][t][s] = st["status_type"][t].get(s, 0) + 1
        if e.get("e"):
            nets.add(e["e"])
        if e.get("r"):
            st["recipes"].setdefault(e["r"], {})
            st["recipes"][e["r"]][s] = st["recipes"][e["r"]].get(s, 0) + 1
        if n == "boiler":
            c = _f(e, "fuel", -1)
            st["boiler_coal_min"] = c if st["boiler_coal_min"] < 0 else min(st["boiler_coal_min"], c)
    st["networks"] = len(nets)
    return st


# =========================================================================== CLI
def format_status(st):
    lines = ["tick %s  research %s" % (st.get("tick"), st.get("research") or "(none)"),
             "power  %.2f MW installed / %.2f MW load  -> headroom %.3f (min %.2f)"
             % (capacity_mw(st), load_mw(st), headroom(st), POWER_HEADROOM_MIN),
             "grid   %d network(s) (max %d)" % (int(_f(st, "networks")), GRID_NETWORKS_MAX),
             "flows  " + ", ".join("%s %.0f/min" % (k, v)
                                   for k, v in sorted((st.get("flows") or {}).items()) if v)]
    lines.append("")
    for s, _why in BUILD_ORDER:
        if s not in GATES:
            continue
        _ok, msg = gate(s, 1, st)
        lines.append("  %-18s %s" % (s, msg))
    return "\n".join(lines)


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        print(format_status(sense()))
    elif cmd == "snapshot" and len(argv) > 1:
        print(format_status(state_from_snapshot(argv[1])))
    elif cmd == "gate" and len(argv) > 1:
        n = int(argv[2]) if len(argv) > 2 else 1
        print(json.dumps(explain(argv[1], n), indent=2))
    elif cmd == "flows" and len(argv) > 1:
        n = int(argv[2]) if len(argv) > 2 else 1
        print(json.dumps(required_flows(argv[1], n), indent=2))
    elif cmd == "blocked":
        for line in why_blocked():
            print(line)
    else:
        print(__doc__.strip())
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
