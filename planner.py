#!/usr/bin/env python3
"""L3 planner: the phase state machine + top-level play() sequencer (MEGABASE-V2-DESIGN §5).

play() drives a FRESH map with no user assistance: run the current phase's build program
(idempotent steps), evaluate its exit gate, advance; between build passes run the maintain
loop with the learning-loop lap hook (triage every lap batch, architect on escalation,
failures codified into lessons).

PHASE 0 IS NOW A PLANNER PIPELINE, NOT A LIST OF AD-HOC BUILDERS (2026-08-30). Every stage
that places anything obeys the same three-step contract:

    build_gates.gate(structure, n)     ADMISSION - is this stage allowed to exist yet?
    <planner>.plan_*(...)              PLAN THE WHOLE THING, purely, before placing a tile
    <planner>.build/apply(plan)        buildplan: plan -> apply -> VERIFY -> rollback

so a build that fails its functional check leaves NOTHING behind (BUILD LAW 2 - the rollback
is buildplan's, in the same pass), and a build that has no business existing yet is never
started (the operator deleted 2 labs, 1 assembler and 9 chest+inserter pairs that were all
admissible-looking placements of things nothing consumed).

    stage            planner module        gate structure     supersedes
    plant            plant_planner         power_capacity     bootstrap.power / power_row
    spine            power_planner         power_grid         bootstrap.power_row
    relief           (dispatches)          the blocking one   nothing - new (LAW 5)
    mines            mine_planner_v2       mine_outpost       bootstrap.build_mine_outpost
    array grid       power_planner         power_grid         build_smelter_array's pole rows
    ore/coal lanes   supply_planner        ore_lane           bootstrap.connect_mine_to_array
                                                              + bootstrap.coal_to_boiler
    plant expand     plant_planner.scale   power_capacity     nothing - new
    electrify        mine_planner_v2       mine_outpost       bootstrap.electrify_mines

The superseded bootstrap functions are LEFT IN PLACE and simply not called from here: they
are still reachable from operator2's command catalog and the controller's own heals, and
deleting them is a separate change.

THE STAGE ORDER IS THE DEPENDENCY GRAPH (2026-08-30). Gates that were each individually
correct produced a DEADLOCK on the operator's base - the plant refused for want of coal, the
coal stage sitting downstream of the plant and never reached, science refused for want of
power, power refused for want of coal. Three things fix it and all three are here:
PHASE0_STAGES puts every no-power build ahead of every power-gated one and the coal lane
ahead of plant expansion; STAGE_SPEC turns "only meaningful after X" into a precondition that
is SKIPPED WITH A REASON instead of a silent return; and a pass that verifies nothing while
refusing something logs one DEADLOCK line naming the binding constraint and the relief build,
which stage_relief then attempts (build_gates LAW 5 / next_relief).

Phase state persists to phase.json (runtime file, gitignored) so a container restart resumes;
`builds` maps a stage key -> the buildplan id it produced, which is how a stage knows it is
already done without re-scanning the world.
"""
import json
import math
import os
import pathlib
import time
import traceback

import autopilot as A
import bootstrap as B
import build_gates
import buildplan
import lessons
import mine_planner_v2
import plant_planner
import power_planner
import principles
import status
import supply_planner
import techdb

HERE = pathlib.Path(__file__).resolve().parent
PHASE_FILE = HERE / "phase.json"

ARCH_COOLDOWN_S = 900          # min seconds between architect calls (halo budget: 1 in flight)
_last_arch = {"t": 0.0}


# ------------------------------------------------------------------ persistence
def load():
    if PHASE_FILE.exists():
        return json.loads(PHASE_FILE.read_text())
    return {"phase": 0, "state": {}, "oil": None, "gates": {}, "started": int(time.time())}


def save(p):
    tmp = PHASE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, indent=1))
    tmp.replace(PHASE_FILE)


def _restore_state(p):
    """Rehydrate bootstrap.STATE from phase.json (survives restarts; ROADMAP MED item)."""
    for k, v in p.get("state", {}).items():
        B.STATE[k] = tuple(v) if isinstance(v, list) else v


def _persist_state(p):
    p["state"] = {k: list(v) if isinstance(v, tuple) else v for k, v in B.STATE.items() if v}
    save(p)


# ------------------------------------------------------------------ lap metrics + hook
def delta():
    """Compact lap metrics for triage (small on purpose)."""
    out = A._print(
        "/sc local s=game.surfaces[1]; local f=game.forces.player;"
        "local eng,engl=0,0; for _,e in pairs(s.find_entities_filtered{name='steam-engine'}) do engl=engl+1; eng=eng+e.energy end;"
        "local b=s.find_entities_filtered{name='boiler',limit=1}[1];"
        "local labs,lw=0,0; for _,l in pairs(s.find_entities_filtered{name='lab'}) do labs=labs+1; if l.status==defines.entity_status.working then lw=lw+1 end end;"
        "local am,aw=0,0; for _,a in pairs(s.find_entities_filtered{type='assembling-machine'}) do am=am+1; if a.status==defines.entity_status.working then aw=aw+1 end end;"
        "local dr=0; for _,d in pairs(s.find_entities_filtered{type='mining-drill'}) do dr=dr+1 end;"
        "local p=storage.derpface; local free=p and p.valid and p.get_main_inventory().count_empty_stacks() or -1;"
        "local rp=f.current_research and math.floor(f.research_progress*100) or -1;"
        "rcon.print(helpers.table_to_json({engine_energy=math.floor(eng),engines=engl,boiler_fuel=(b and b.get_fuel_inventory().get_item_count('coal') or -1),labs=labs,labs_working=lw,assemblers=am,assemblers_working=aw,drills=dr,free_slots=free,research_pct=rp}))"
    ).strip()
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return {"parse_error": out[:120]}


def lap_hook(i):
    """Wired into bootstrap.maintain: triage every 5th lap; architect on escalation."""
    if i % 5:
        return
    try:
        import operator2 as _op
        _op.process_inbox()
    except Exception as e:
        status.log(f"operator inbox error: {e}")
    try:
        import triage
        d = delta()
        if not (d.get("engines") or d.get("labs") or d.get("drills")):
            # nothing built yet (fresh-world bootstrap in progress) - triage/architect on an
            # empty world is pure noise (learned live 2026-08-29: 'anomaly' every lap + an
            # architect run that diagnosed 'total loss of game state')
            return
        v = triage.classify(d)
        status.log(f"triage[{v.get('_source','?')[:9]}]: {v['state']}"
                   + (f"/{v['class']}" if v.get("class") else "") + f" - {v['reason']}")
        if v.get("state") in ("stall", "anomaly"):
            # BOTTLENECK FIRST (Seth): stop what we're doing, run the heal battery NOW,
            # and record what each heal fixed as a lesson (learning, not just logging)
            import autopilot as A
            A.stop()
            heal_battery("triage " + v["state"])
        if v.get("wake_architect") and time.time() - _last_arch["t"] > ARCH_COOLDOWN_S:
            _last_arch["t"] = time.time()
            # daemon thread: a 35B call can take minutes and must never block the
            # maintain strand (it froze hauling for the duration when synchronous)
            import threading
            threading.Thread(target=_run_architect_safe, args=(d, v), daemon=True).start()
    except Exception as e:
        status.log(f"lap_hook error: {e}")


def heal_battery(reason):
    """Run every self-heal immediately; heals that FIX something become lessons (the
    automated GOTCHAS - Seth: learn from mistakes, don't just repair them)."""
    import bootstrap as B
    for fn, tag in (("keep_power", "power"), ("fix_unpowered", "power"),
                    ("repair_belt_gaps", "belts"), ("ensure_lanes", "belts")):
        try:
            r = getattr(B, fn)()
            if isinstance(r, int) and r > 0:
                lessons.add(condition=f"heal {fn} fired ({reason})",
                            mistake=f"{fn} found {r} defect(s) the build left behind",
                            rule=f"whatever built this must verify; {fn} covers the gap meanwhile",
                            tags=("self-heal", tag))
                status.log(f"heal_battery: {fn} fixed {r} ({reason})")
        except Exception as e:
            status.log(f"heal_battery {fn}: {e}")


def _run_architect_safe(d, v):
    try:
        _run_architect(d, v)
    except Exception as e:
        status.log(f"architect error: {e}")


def _run_architect(d, verdict):
    import architect
    status.log("architect: escalated by triage, running local 35B analysis...")
    snap = architect.snapshot()
    if not snap.get("ents"):
        status.log("architect: empty world snapshot - skipping (nothing to analyze)")
        return
    rep = architect.analyze_local(snap, focus=f"triage says {verdict['state']}: {verdict['reason']}")
    architect.REPORT_PATH.write_text(json.dumps(rep, indent=2))
    for b in rep.get("bottlenecks", []):
        if b.get("severity") == "high":
            lessons.add(condition=b.get("area", "?"), mistake=b.get("root_cause", "?"),
                        rule=(rep.get("prioritized_actions") or [{}])[0].get("action", "see report"),
                        evidence=b.get("evidence", ""), tags=("architect",))
    status.log("architect: " + rep.get("summary", "")[:200])


# ------------------------------------------------------------------ phase 0
def _scout_guarded(p):
    """scout() with the ROADMAP guard: widen + regenerate on missing resources, fail loudly.

    SCOUT ONLY WHAT IS NOT ALREADY KNOWN. `_load` restores every recorded patch from phase.json
    into B.STATE at startup, and a patch does not move, so re-running the full scan each pass
    spent a 625-chunk generate and five radius-160 scans to recompute the same coordinates -
    at the front of every pass, ahead of all the work that actually builds anything.
    """
    known = [k for k in B.SCOUT_RESOURCES if B.STATE.get(k)]
    missing = [k for k in B.SCOUT_RESOURCES if not B.STATE.get(k)]
    if not missing:
        status.log("scout: all %d resources already recorded (%s) - nothing to scan"
                   % (len(known), ", ".join(known)))
        return
    A.purpose("phase 0: scouting %s" % ", ".join(missing))
    B.scout(only=missing)
    missing = [k for k in B.SCOUT_RESOURCES if not B.STATE.get(k)]
    if missing:
        status.log(f"scout: missing {missing}, widening to radius 320")
        A._print("/sc local s=game.surfaces[1]; for cx=-20,20 do for cy=-20,20 do s.request_to_generate_chunks({x=cx*32,y=cy*32},0) end end; s.force_generate_chunk_requests()")
        for ore in ("iron-ore", "copper-ore", "stone", "coal"):
            if not B.STATE.get(ore):
                B.STATE[ore] = A.richest_spot(ore, 0, 0, radius=320)
        B.scout() if not B.STATE.get("water") else None
        missing = [k for k in ("iron-ore", "copper-ore", "stone", "coal", "water") if not B.STATE.get(k)]
        if missing:
            raise RuntimeError(f"scout failed even at radius 320: missing {missing}")
    _persist_state(p)


def scout_oil(p, max_radius=480):
    """Find crude oil early (FRESH-START: it shaped the last map at 440 tiles). Ring-generates
    chunks outward until a patch is found; records the richest tile in phase.json."""
    if p.get("oil"):
        return tuple(p["oil"])
    for r in range(160, max_radius + 1, 160):
        cr = r // 32 + 1
        A._print(f"/sc local s=game.surfaces[1]; for cx=-{cr},{cr} do for cy=-{cr},{cr} do s.request_to_generate_chunks({{x=cx*32,y=cy*32}},0) end end; s.force_generate_chunk_requests()")
        spot = A.richest_spot("crude-oil", 0, 0, radius=r)
        if spot:
            p["oil"] = list(spot)
            save(p)
            status.log(f"oil scouted @ {spot[0]},{spot[1]} (density {spot[2]})")
            return spot
    status.log(f"oil NOT found within {max_radius} tiles - flagged for phase 1")
    return None


# ------------------------------------------------------------------ phase 0 build spec
# The operator's own numbers (snapshots/after.json), not invented ones: 1 boiler : 2 engines
# exactly, 16 iron furnaces on 6 drills (1.67x) and 12 copper on 4 (1.88x), both under the
# measured 2.0 pre-build ceiling.
MINE_DRILLS = (("iron-ore", 8), ("copper-ore", 6), ("coal", 6))
SMELTERS = (("iron-ore", 16), ("copper-ore", 12))
ELECTRIC_DRILL = "electric-mining-drill"
BURNER_DRILL = "burner-mining-drill"
POLE = "small-electric-pole"

# The operator's own N-S transmission trunk column. Measured off his relay: x=-15 from y=-65
# to y=26 is 91 tiles / 14 poles / 13 gaps of exactly 7 (power_planner's TRUNK_SPACING).
SPINE_X = -15

# What the plant is being built FOR. LAW 2 is "capacity LEADS the load it will carry", so the
# plant is sized to the load the stages BEHIND it will add, not to the load standing today:
# every drill the mine stage places, plus the two inserters per furnace the arrays need.
PROJECTED_LOAD_KW = (
    sum(n for _o, n in MINE_DRILLS) * build_gates.CONSUMER_KW[ELECTRIC_DRILL]
    + 2 * sum(n for _o, n in SMELTERS) * build_gates.CONSUMER_KW["inserter"])


def plant_columns_needed(st):
    """Boiler columns still missing to lead PROJECTED_LOAD_KW by POWER_HEADROOM_MIN.

    This is the arithmetic the operator did by hand: he ADDED a second boiler column BEFORE
    electrifying 16 drills, because without it headroom would have gone 3.6/2.246 -> 0.80 and
    the whole base would have browned out.

    IT MUST SOLVE THE SAME INEQUALITY AS build_gates._pr_headroom_after, or the stage asks for
    a column count its own gate can never approve. It did not: this treated PROJECTED_LOAD_KW
    as the TOTAL load and ignored everything already standing, so on the operator's
    hand-optimized base (3.55 MW of load already drawing) it asked for 1 column while the gate
    wanted 4 and refused every one of them. On a fresh map the two agree - current load is
    zero - which is exactly why it survived until a base existed to break it.
    """
    have = build_gates.capacity_mw(st)
    need = build_gates.load_mw(st, PROJECTED_LOAD_KW) * build_gates.POWER_HEADROOM_MIN
    return max(0, int(math.ceil((need - have) / build_gates.BOILER_MW)))


# ------------------------------------------------------------------ gate + build plumbing
GATE_TTL_S = 20.0
_GATE = {"st": None, "t": 0.0}


def gate_state(force=False):
    """ONE build_gates.sense() (a READ-ONLY census) per build pass, shared by every gate in it.

    sense() RAISES when the read fails, deliberately: a gate that silently saw an empty world
    would ALLOW everything, which is the exact failure build_gates exists to stop.
    """
    now = time.time()
    if not force and _GATE["st"] is not None and now - _GATE["t"] < GATE_TTL_S:
        return _GATE["st"]
    _GATE["st"] = build_gates.sense(force=True)
    _GATE["t"] = now
    return _GATE["st"]


def gate_reset():
    """Drop the cached census so the next pass gates against a fresh world."""
    _GATE["st"], _GATE["t"] = None, 0.0


# ------------------------------------------------------------------ pass bookkeeping
# What THIS build pass actually did. A builder that verifies nothing and refuses something is
# not idle, it is DEADLOCKED, and the only way to tell the two apart is to count both.
_PASS = {"built": 0, "blocked": [], "relief_done": False}


def pass_reset():
    _PASS["built"], _PASS["blocked"], _PASS["relief_done"] = 0, [], False


def gate(structure, n=1, params=None, state=None, relieves=None):
    """ADMISSION CONTROL in front of every build (build_gates, LAWS 1-5). True = proceed.

    A refusal is the NORMAL outcome early in a base and is never raised: raising would
    abandon the rest of the pass, and the stages behind this one that ARE allowed would never
    run. A gate that cannot EVALUATE - the census read failed, the structure is unknown, the
    recipe is unknown - REFUSES. It never falls back to allow.

    `relieves` names the constraint(s) this build INCREASES (LAW 5). It is how a relief build
    gets through the check that is only failing because the relief has not been built yet -
    and it is logged as `gate RELIEF:`, never as a plain ALLOW, because an exemption that
    reads like an ordinary pass is an exemption nobody can audit.

    Every refusal is recorded for the pass, so the deadlock detector can name the binding
    constraint from what the builder actually tried rather than from a speculative x1 of each.
    """
    try:
        st = gate_state() if state is None else state
    except Exception as e:
        status.log("gate %s x%d: census failed (%s) - REFUSED (a gate that cannot see the "
                   "world must never allow)" % (structure, n, e))
        return False
    try:
        ok, why, rep = build_gates.gate_report(structure, n, st, params, relieves)
    except Exception as e:
        status.log("gate %s x%d: %s: %s - REFUSED" % (structure, n, type(e).__name__, e))
        return False
    if not ok:
        _PASS["blocked"].append({"structure": structure, "n": n,
                                 "params": dict(params or {}),
                                 "blocking": list(rep.get("blocking") or []), "why": why})
        status.log("gate BLOCK: " + why[:240])
        return False
    key = build_gates.relief_key_of(rep)
    if key:
        status.log("gate RELIEF: allowing %s x%d because it increases %s - %s"
                   % (structure, n, key, why[:200]))
    else:
        status.log("gate ALLOW: " + why[:240])
    return True


def gate_bootstrap(structure, n, exempt_while, why, params=None):
    """gate() with a NAMED crash-site exemption that CLOSES on a measured condition.

    Two steps place entities before any gate they own can possibly pass: the hand-mined spawn
    furnaces (there is no drill yet to size an overbuild budget against) and the first
    hand-fed lab (the pack FLOW the lab gate demands is precisely what this lab's research
    unlocks). Skipping the gate quietly for them would make "every build is gated" a lie, so
    the exemption is explicit, is logged every single time it fires, and `exempt_while` is the
    measured predicate that ends it - after which the real gate is binding like everywhere else.
    """
    if not exempt_while:
        return gate(structure, n, params=params)
    status.log("gate EXEMPT %s x%d (crash-site bootstrap): %s" % (structure, n, why))
    return True


def verified(rec, what):
    """True only for a buildplan record at status 'verified'.

    Any other status means the build left NOTHING behind, which is the point: buildplan.apply
    rolls its own placements back out of the ground in the SAME pass when the functional check
    fails (BUILD LAW 2), and a refusal - truce, staleness, operator-protected route, superseded
    plan - never placed anything at all. So a non-verified stage is logged and retried next
    pass; it is never recorded as progress.
    """
    st = (rec or {}).get("status")
    v = (rec or {}).get("verify") or {}
    if st == "verified":
        _PASS["built"] += 1          # the ONE place a build counts as progress this pass
        status.log("%s: VERIFIED - %s"
                   % (what, str((v.get("check") or {}).get("detail", ""))[:180]))
        return True
    rb = v.get("rollback") or {}
    reason = (v.get("refused") or (v.get("check") or {}).get("detail")
              or ("no record" if rec is None else "status=%s" % st))
    status.log("%s: NOT BUILT (%s) - %s%s"
               % (what, st or "none", str(reason)[:220],
                  (" [rolled back %s placed, %s not found]"
                   % (rb.get("removed"), rb.get("not_found"))) if rb else ""))
    return False


def build_done(p, key):
    """A stage is complete when the buildplan record it produced is STILL `verified`.

    Keyed off the record rather than a boolean in phase.json so the answer survives a restart
    and stays honest: a failed or rolled-back plan never reads done, and a plan the operator
    superseded reads not-done, which lets the stage plan a fresh route instead of re-applying
    a retired one (buildplan.apply refuses a superseded plan outright).
    """
    pid = (p.get("builds") or {}).get(key)
    if not pid:
        return False
    try:
        rec = buildplan.load(pid)
    except (OSError, ValueError, KeyError):
        return False
    return (rec or {}).get("status") == "verified"


def mark_build(p, key, rec):
    """Record which buildplan owns a stage, so the next pass can skip it."""
    p.setdefault("builds", {})[key] = (rec or {}).get("id")
    save(p)


def _pt(v, default=None):
    return (int(v[0]), int(v[1])) if v else default


# ------------------------------------------------------------------ adopt what is LIVE
def _census_poles(st):
    return int(sum(build_gates._f(st.get("counts", {}), n)
                   for n in build_gates.POLE_NAMES))


def _live_poles(area):
    """Pole TILES inside an inclusive tile bbox. READ ONLY; [] on any failure."""
    try:
        out = (A._print(
            "/sc local t={} for _,e in pairs(game.surfaces[1].find_entities_filtered"
            "{area={{%d,%d},{%d,%d}},name={'small-electric-pole','medium-electric-pole',"
            "'big-electric-pole','substation'}}) do t[#t+1]=math.floor(e.position.x)..','"
            "..math.floor(e.position.y) end rcon.print(table.concat(t,';'))"
            % (int(area[0]), int(area[1]), int(area[2]), int(area[3]))) or "").strip()
    except Exception as e:
        status.log("live pole read failed (%s) - treating as none" % e)
        return []
    poles = []
    for tok in out.split(";"):
        bits = tok.split(",")
        if len(bits) == 2:
            try:
                poles.append([int(bits[0]), int(bits[1])])
            except ValueError:
                pass
    return poles


def _live_pump():
    """The offshore pump's tile. READ ONLY; None on any failure.

    It is the ONE measurement plant_planner needs to reconstruct a standing plant, because
    `anchor_from_pump` is defined off it - and defined off THIS plant: its docstring records
    "pump (-32,51) -> anchor (-35,45)", which is the operator's own shore.
    """
    try:
        out = (A._print(
            "/sc local p=game.surfaces[1].find_entities_filtered{name='offshore-pump'}[1] "
            "rcon.print(p and (math.floor(p.position.x)..','..math.floor(p.position.y)) "
            "or '')") or "").strip()
    except Exception as e:
        status.log("live pump read failed (%s) - nothing to adopt" % e)
        return None
    bits = out.split(",")
    if len(bits) != 2:
        return None
    try:
        return (int(bits[0]), int(bits[1]))
    except ValueError:
        return None


def _plant_standing(plan):
    """Is every entity of `plan` ACTUALLY IN THE GROUND? (found, missing) - a READ.

    This is the adoption test, and it is deliberately narrower than plant_planner.verify():
    verify() also fails a plant that is merely dry or cold or has no coal on its belt, and
    the live plant fails exactly those (pump water 99/100, "coal dead-end") - which is the
    condition we are adopting it in order to FIX. Identity is `read_state` finding the
    entity; a missing one reports -2 (plant_planner.verify_lua).
    """
    state = plant_planner.read_state(plan)                       # READ ONLY
    missing = [k for k in plant_planner._check_spec(plan)
               if (state.get(k) is None or state[k][0] == -2)]
    return (not missing), missing


def adopt_plant(p, st):
    """The STANDING plant, RECONSTRUCTED from the world when phase.json has no record.

    phase.json is bookkeeping, not evidence. On the operator's hand-optimized base the plant
    physically exists (2 boilers, 4 engines, 3.60 MW, one energized network) and `p["plant"]`
    was empty because HE built it. Adopting only its POLES was not enough, and the half-fix
    was its own dead end: `stage_plant` saw capacity and deferred to `stage_plant_expand`,
    which needs a buildplan record; `stage_coal_lane` needs `coal_intake`, which only a plan
    names. So on the base this was written for, boiler columns 2..N AND the coal lane were
    both unreachable forever - the gate said ALLOW and nothing could build. What the plant
    stage needs adopted is the PLAN, not the poles.

    Reconstruction is exact and self-checking: plant_planner's geometry is defined off this
    very shore, and `_plant_standing` reads the world back before a single field is recorded.
    A wrong anchor lists its MISSING entities and adopts NOTHING - and `scale()` refuses a
    second time later, because it re-plans and raises if any existing entity would move.
    """
    rec = p.get("plant") or {}
    if rec.get("anchor") and rec.get("coal_intake"):
        return rec
    if (build_gates.capacity_mw(st) <= 0 or _census_poles(st) <= 0
            or int(build_gates._f(st, "networks")) != 1):
        return {}
    counts = st.get("counts") or {}
    n_columns = min(int(build_gates._f(counts, "boiler")),
                    int(build_gates._f(counts, "steam-engine"))
                    // plant_planner.ENGINES_PER_BOILER)
    if n_columns < 1:
        return {}
    pump = _live_pump()
    if pump is None:
        return {}
    try:
        plan = plant_planner.plan_plant(n_columns * plant_planner.ENGINES_PER_BOILER,
                                        water_hint=pump)
        standing, missing = _plant_standing(plan)
    except Exception as e:
        status.log("plant: cannot reconstruct the standing plant from pump %s (%s: %s) - "
                   "refusing to adopt a plant it cannot see" % (pump, type(e).__name__,
                                                                str(e)[:140]))
        return {}
    if not standing:
        status.log("plant: a %d-column plant reconstructed from pump %s is NOT in the ground "
                   "(%d entities missing, e.g. %s) - this plant is not on plant_planner's "
                   "lattice and cannot be extended by scale(); leaving it alone"
                   % (n_columns, tuple(pump), len(missing),
                      ", ".join("%s@%.1f,%.1f" % m for m in missing[:3])))
        return {}
    intake = plant_planner.coal_intake(plan)
    out = {"anchor": list(plan["anchor"]), "bbox": list(plan["bbox"]),
           "n_columns": plan["n_columns"], "power_MW": plan["power_MW"],
           "pump": [int(pump[0]), int(pump[1])],
           "coal_intake": [int(intake["tile"][0]), int(intake["tile"][1])],
           "poles": [[x, y] for (_n, x, y) in plant_planner.plan_poles(plan)],
           "adopted": True}
    status.log("plant: ADOPTED the standing %d-column plant (%.1f MW) - pump %s -> anchor "
               "%s, coal intake %s. phase.json had no record; the world did, and every "
               "entity of the reconstruction was read back before this was written."
               % (plan["n_columns"], plan["power_MW"], tuple(pump),
                  tuple(plan["anchor"]), tuple(out["coal_intake"])))
    p["plant"] = out
    save(p)
    return out


def plant_existing(p, st):
    """The plant plan `scale()` must extend: the buildplan record when THIS planner built it,
    else the reconstruction of the standing one. None when there is nothing to extend."""
    pid = (p.get("builds") or {}).get("plant")
    if pid:
        return plant_planner.from_record(buildplan.load(pid))
    rec = adopt_plant(p, st)
    if not rec.get("pump"):
        return None
    return plant_planner.plan_plant(int(rec["n_columns"]) * plant_planner.ENGINES_PER_BOILER,
                                    water_hint=tuple(rec["pump"]))


def plant_poles(p, st):
    """The plant's poles - from phase.json when THIS planner built it, else FROM THE WORLD.

    MEASURED, NOT REMEMBERED, is the same rule that makes a functional check beat
    create_entity's return value. The plan's OWN poles are used where the plant could be
    adopted; the wide pole sweep below is the fallback for a plant that stands but is not on
    plant_planner's lattice, and it is deliberately last - it returns every pole for 40 tiles
    around the shore, which is most of a built base, not a plant.
    """
    poles = (p.get("plant") or {}).get("poles") or []
    if poles:
        return [list(t) for t in poles]
    water = B.STATE.get("water")
    if (build_gates.capacity_mw(st) <= 0 or _census_poles(st) <= 0
            or int(build_gates._f(st, "networks")) != 1 or not water):
        return []
    adopted = adopt_plant(p, st)
    if adopted.get("poles"):
        return [list(t) for t in adopted["poles"]]
    wx, wy = int(water[0]), int(water[1])
    found = _live_poles((wx - 40, wy - 40, wx + 40, wy + 40))
    if not found:
        return []
    status.log("plant: adopting %d live pole(s) near the standing plant at (%d,%d) - "
               "phase.json had no plant record, the world did" % (len(found), wx, wy))
    p.setdefault("plant", {})["poles"] = found
    p["plant"]["adopted"] = True
    save(p)
    return found


# ------------------------------------------------------------------ phase 0 stages
def stage_world(p):
    """Crash site: world setup, scouting, the first coal, the hand-fed spawn furnaces.

    Nothing here is a PLANNED build - it is the character mining by hand so there is enough
    iron to craft a boiler at all. The spawn furnaces are burners (LAW 3 passive: 0 kW, 0
    items locked) and are gated on `smelter_array`, under the one crash-site exemption that
    closes the moment a real belt-fed array exists.
    """
    A.purpose("phase 0 bootstrap: world setup + crash-site cleanup")
    B.setup_world()
    # The purpose line moved INSIDE _scout_guarded: announcing "scouting the richest ore
    # patches" before checking whether anything needs scouting made the dashboard report the
    # bot's current action as scouting on every pass of a base that had every patch recorded
    # months of game-time ago. A status line that is set unconditionally is not status.
    _scout_guarded(p)
    A.purpose("phase 0: first coal so the plant can be hand-seeded")
    B.fuel()
    try:
        st = gate_state()
    except Exception as e:
        status.log("stage_world: census failed (%s) - skipping the spawn furnaces" % e)
        return
    drills = int(build_gates._f(st.get("counts", {}), ELECTRIC_DRILL)
                 + build_gates._f(st.get("counts", {}), BURNER_DRILL))
    if not gate_bootstrap("smelter_array", 12, exempt_while=(drills == 0),
                          why="0 drills exist, so the overbuild budget has no denominator; "
                              "these 12 spawn furnaces are hand-fed burners and the gate "
                              "binds again as soon as one drill is mining",
                          params={"ore": "iron-ore"}):
        return
    A.purpose("phase 0: starter smelting rows at spawn")
    B.smelting_base()


def stage_plant(p):
    """POWER FIRST (LAW 2): the operator's SCALABLE steam plant, through plant_planner.

    Supersedes bootstrap.power() / _build_boiler_engine - a single non-scalable column whose
    west-side surface pipe run occupies the two rows the scalable design needs. plant_planner
    plans the whole thing (4-tile column pitch, one riser chaining water through every boiler,
    coal belt row, pole trunk + spurs), buildplan places it, and the acceptance test is a
    FLUID/ENERGY read - pump 100 / boiler water > 0 / engine energy > 0 - not "create_entity
    returned ok".
    """
    if build_done(p, "plant"):
        return
    water = B.STATE.get("water")
    if not water:
        raise RuntimeError("stage_plant: water not scouted (scout() found no shore)")
    try:
        st = gate_state()
    except Exception as e:
        status.log("stage_plant: census failed (%s)" % e)
        return
    cols = plant_columns_needed(st)
    if cols <= 0:
        status.log("plant: %.2f MW installed already leads the %.2f MW projected load"
                   % (build_gates.capacity_mw(st), PROJECTED_LOAD_KW / 1000.0))
        return
    if not gate("power_capacity", cols, params={"projected_load_kw": PROJECTED_LOAD_KW},
                state=st):
        return
    if build_gates.capacity_mw(st) > 0:
        # THE FIRST COLUMN IS UNCONDITIONAL; EVERY LATER ONE BURNS 27 MORE COAL/MIN. A plant
        # already stands, so this is an EXPANSION, and an expansion belongs behind the coal
        # lane that pays for it - which is where stage_plant_expand sits in the stage order.
        status.log("plant: %d more column(s) wanted, but a plant already stands - expansion "
                   "runs after the coal lane (stage_plant_expand)" % cols)
        return
    A.purpose("phase 0: steam plant at the lake (%d column(s), planned whole)" % cols)
    wx, wy = int(water[0]), int(water[1])
    terrain = plant_planner.scan_shore(wx, wy, radius=30)            # READ ONLY
    coal = B.STATE.get("coal")
    plan = plant_planner.plan_plant(
        cols * plant_planner.ENGINES_PER_BOILER, terrain=terrain, near=(wx, wy),
        coal_tap=_pt(coal), pole=POLE)
    for w in plan.get("warnings", ()):
        status.log("plant plan: " + str(w)[:200])
    rec = plant_planner.build(plan)
    if not verified(rec, "power plant"):
        return
    mark_build(p, "plant", rec)
    intake = plan["intake"]
    p["plant"] = {
        "anchor": list(plan["anchor"]), "bbox": list(plan["bbox"]),
        "n_columns": plan["n_columns"], "power_MW": plan["power_MW"],
        "coal_intake": [int(intake["tile"][0]), int(intake["tile"][1])],
        "poles": [[x, y] for (_n, x, y) in plant_planner.plan_poles(plan)],
    }
    save(p)


def stage_plant_expand(p):
    """Boiler columns 2..N - AFTER the coal that will feed them.

    The split from stage_plant is the whole point: the first column is unconditional (nothing
    precedes power), every later one commits another BOILER_COAL_PER_MIN to the fire, and
    `power_capacity`'s own `coal_at_boiler` check says so. Running expansion ahead of the coal
    lane is what produced the live deadlock - the plant refused for want of coal, the coal
    stage sitting downstream of the plant and never reached.

    EXTENSION, NEVER A REBUILD: plant_planner.scale() refuses outright if the new layout would
    move a single existing entity, and buildplan probes the world first so only the delta is
    placed.

    The plant it extends may be one THIS planner never built. Requiring a buildplan record
    made every column after the first unreachable on the operator's own base - see
    adopt_plant(), which reconstructs the standing plant and reads every entity back before
    handing it here.
    """
    try:
        st = gate_state()
    except Exception as e:
        status.log("stage_plant_expand: census failed (%s)" % e)
        return
    more = plant_columns_needed(st)
    if more <= 0:
        return
    if not gate("power_capacity", more, params={"projected_load_kw": PROJECTED_LOAD_KW},
                state=st):
        return
    try:
        existing = plant_existing(p, st)
    except Exception as e:
        status.log("plant expand: cannot reconstruct the standing plant (%s: %s) - "
                   "refusing to re-plan a plant blind" % (type(e).__name__, str(e)[:160]))
        return
    if existing is None:
        status.log("plant expand: no plant to extend - neither a buildplan record nor a "
                   "standing plant this planner can reconstruct")
        return
    A.purpose("phase 0: +%d boiler column(s), now that coal leads them" % more)
    try:
        out = plant_planner.scale(existing, more * plant_planner.ENGINES_PER_BOILER)
    except plant_planner.PlantError as e:
        status.log("plant expand: %s" % str(e)[:220])
        return
    for w in out.get("warnings", ()):
        status.log("plant expand: " + str(w)[:200])
    rec = plant_planner.build(out["plan"])
    if not verified(rec, "plant expansion (+%d column(s))" % out["added_columns"]):
        return
    mark_build(p, "plant", rec)
    plan = out["plan"]
    intake = plan["intake"]
    p["plant"] = {
        "anchor": list(plan["anchor"]), "bbox": list(plan["bbox"]),
        "n_columns": plan["n_columns"], "power_MW": plan["power_MW"],
        "coal_intake": [int(intake["tile"][0]), int(intake["tile"][1])],
        "poles": [[x, y] for (_n, x, y) in plant_planner.plan_poles(plan)],
        "pump": [int(plan["pump"][0]), int(plan["pump"][1])],   # keeps it reconstructable
    }
    save(p)


def _spine_anchor(p, end):
    """The plant pole the base spine hangs off: whichever of the plant's own poles is nearest
    the spine's far end.

    Nearest, not "the north-most": the plant sits wherever the shore is, so which end of its
    trunk column faces the base is not knowable in advance. Picking by distance keeps the
    trunk short in every orientation and never leaves it doubling back through the plant.
    """
    poles = (p.get("plant") or {}).get("poles") or []
    if not poles:
        try:
            poles = plant_poles(p, gate_state())      # adopt the STANDING plant, if any
        except Exception as e:
            status.log("spine: could not look for a live plant pole (%s)" % str(e)[:120])
            poles = []
    if not poles:
        return None
    return min((tuple(t) for t in poles),
               key=lambda t: (abs(t[0] - end[0]) + abs(t[1] - end[1]), t[1], t[0]))


def stage_spine(p):
    """ALL POLE WORK outside a module template goes through power_planner.

    Supersedes bootstrap.power_row: a 5-spaced chain plus a "walk poles toward the nearest
    base-network pole" heuristic that interpolated diagonal staircases and never arrived. That
    structure is what produced the split the operator found - net 405 holding all six electric
    drills and no generator at all, 8.06 tiles from the nearest pole against a 7.5 wire reach.

    Here it is ONE straight, axis-aligned trunk at spacing exactly 7 from the plant's own
    trunk column to the operator's measured spine column x=-15 beside the smelting zone, and
    apply() WIRES EVERY PAIR EXPLICITLY and verifies by comparing electric_network_id - never
    "placement implies connection" (GOTCHAS 2026-08-30).
    """
    if build_done(p, "spine"):
        return
    _ox, oy = B.SMELT_ZONE["iron-ore"]
    end = (SPINE_X, int(oy))
    anchor = _spine_anchor(p, end)
    if anchor is None:
        status.log("spine: no plant poles recorded yet - the plant stage runs first")
        return
    if not gate("power_grid", 1):
        return
    A.purpose("phase 0: power trunk from the plant to the smelting zone")
    area = (min(anchor[0], end[0]) - 4, min(anchor[1], end[1]) - 4,
            max(anchor[0], end[0]) + 4, max(anchor[1], end[1]) + 4)
    obs = power_planner.obstacles_for(area)                          # READ ONLY
    blocked = power_planner.blocked_tiles(obs)
    try:
        trunk = power_planner.plan_trunk(anchor, end, pole=POLE, blocked=blocked, area=area)
    except power_planner.GridError as e:
        status.log("spine: no legal trunk (%s) - retrying next pass" % e)
        return
    # The anchor is an EXISTING pole: it is a virtual node apply() wires TO, never a tile the
    # plan re-places (a plan tile that is also the join tile makes wire_pairs pair it with
    # itself).
    plan = [t for t in trunk if (t["x"], t["y"]) != anchor]
    if not plan:
        status.log("spine: the plant trunk already reaches %s - nothing to lay" % (end,))
        return
    for w in power_planner.LAST_WARNINGS:
        status.log("spine plan: " + str(w)[:200])
    rec = power_planner.apply(plan, area=area, pole=POLE, anchor=anchor, obstacles=obs)
    if not verified(rec, "power spine"):
        return
    mark_build(p, "spine", rec)
    p["spine"] = {"anchor": list(anchor), "end": list(end),
                  "poles": [[t["x"], t["y"]] for t in plan]}
    save(p)


def stage_red_science(p):
    """The BOOTSTRAP lab: hand-fed, and the one consumer on this map that legitimately
    precedes its own supply.

    build_gates' `lab` gate demands measured pack FLOW and a live pack producer. Neither can
    exist before this lab, because automation-2 - the assembler that makes packs - is what
    this lab is built to research. So the gate is applied and a block is RESPECTED for every
    lab after the first: the exemption is exactly one hand-fed lab and it closes the instant a
    lab exists.
    """
    try:
        st = gate_state()
    except Exception as e:
        status.log("stage_red_science: census failed (%s)" % e)
        return
    labs = int(build_gates._f(st.get("counts", {}), "lab"))
    if not gate_bootstrap("lab", 1, exempt_while=(labs == 0),
                          why="0 labs exist; the automation-science-pack flow this gate wants "
                              "is what this lab's own research unlocks"):
        return
    A.purpose("phase 0: lab + red science to unlock assemblers")
    B.red_science()


def _mine_key(ore):
    return "mine:" + ore


def _drill_for(ore):
    """All-electric WHERE RESEARCHED. The operator converted every burner drill on the map and
    deleted the coal fuel belts that fed them as dead weight; a burner outpost is something we
    would have to tear out, so it is only planned while the tech is still missing."""
    return ELECTRIC_DRILL if B._tech_done(ELECTRIC_DRILL) else BURNER_DRILL


def stage_mines(p):
    """Mine outposts through mine_planner_v2: plan whole -> validate -> build -> verify by
    lane_lint -> roll back a mine that moves no ore.

    Supersedes bootstrap.build_mine_outpost / builds_v2.mine_outpost_v2, whose outpost ends in
    the terminal chest + inserter the operator deleted nine of - a chest is a hard stop where
    throughput becomes a human walking.

    `trunk=None` on purpose: the hookup leg from the last drop tile to the smelter array is
    NOT laid here. mine_planner_v2's own docstring says that leg is laid blind (it has no
    model of what is already standing), and routing it is exactly supply_planner's job - so
    the plan ends at the lane's downstream tile and stage_ore_lanes routes onward from it with
    belt_router's obstacle-aware A*.

    The gate is `mine_outpost` for BOTH drill tiers, and the TIER IS PASSED. It used to be
    withheld, on the reasoning that charging a burner the electric drill's 90 kW is
    conservative and conservative is the safe direction for an admission gate to be wrong in.
    That reasoning is what LAW 5 overturns: a burner outpost is one of the no-power builds
    this stage order puts FIRST precisely because it draws nothing, and refusing it for want
    of headroom it does not consume is not caution, it is the deadlock.
    """
    spine = p.get("spine") or {}
    anchor = _pt((spine.get("poles") or [None])[-1]) if spine.get("poles") else None
    for ore, n in MINE_DRILLS:
        key = _mine_key(ore)
        if build_done(p, key):
            continue
        spot = B.STATE.get(ore)
        if not spot:
            status.log("mine %s: patch not scouted - skipping" % ore)
            continue
        drill = _drill_for(ore)
        if drill == ELECTRIC_DRILL and anchor is None:
            status.log("mine %s: no base spine to join - an electric drill on an islanded "
                       "grid mines nothing (net 405). Spine first." % ore)
            continue
        # THE TIER AND THE ORE GO TO THE GATE. Without `drill` the gate charges the electric
        # drill's 90 kW against a BURNER, which draws none - the exact miscount that helped
        # close the live deadlock, and adds_kw_for() only fixes it for a caller that says
        # which drill it means. Without `ore` the relief keys degrade to the
        # `overbuild_within_budget:*` wildcard instead of naming the ore they raise.
        if not gate("mine_outpost", n, params={"drills": n, "ore": ore, "drill": drill}):
            continue
        A.purpose("phase 0: %s outpost (%d %s)" % (ore, n, drill))
        try:
            plan = mine_planner_v2.plan_outpost(
                ore, n, center=_pt(spot), drill=drill, pole=POLE,
                trunk=None, power_trunk_x=(SPINE_X if anchor else None),
                grid_anchor=anchor)
        except mine_planner_v2.LayoutError as e:
            status.log("mine %s: plan refused - %s" % (ore, str(e)[:220]))
            continue
        for w in plan.get("warnings", ()):
            status.log("mine %s plan: %s" % (ore, str(w)[:200]))
        rec = mine_planner_v2.build(plan)
        if not verified(rec, "%s outpost" % ore):
            continue
        mark_build(p, key, rec)
        p.setdefault("mines", {})[ore] = {
            "drill": drill, "n": n, "lane_y": plan["lane_y"],
            "from_xy": list(plan["from_xy"]), "to_xy": list(plan["to_xy"]),
        }
        save(p)


def stage_arrays(p):
    """Belt-fed smelter arrays. Burner furnaces, so LAW 3 lets them be PRE-built - the
    operator kept 11 of 28 idle at `no_ingredients` and deleted none of them - but the licence
    has a budget: `overbuild_within_budget` sizes the row against what the MINE can deliver,
    not against what it currently produces (his 16 iron on 6 drills is 1.67x, ceiling 2.0).

    KNOWN GAP: this is still bootstrap.build_smelter_array. There is no array planner module
    yet, so it is the one placing stage in phase 0 that does NOT go through buildplan - it is
    gated, and it is checked afterwards against the census (did the furnace count actually
    rise), but there is no registry-scoped rollback behind it the way there is for the plant,
    the mines, the poles and the lanes.
    """
    built = p.setdefault("arrays", {})
    for ore, n in SMELTERS:
        if built.get(ore):
            continue
        have = _array_furnaces(ore, n)
        if have < 0:
            status.log("%s array: furnace census read failed - skipping (never build blind)"
                       % ore)
            continue
        # Ask for the DEFICIT in THIS array's own band, not the target: once the row stands,
        # asking for the full 16 again reads as "may I double it" and the overbuild budget
        # blocks it forever; counting furnaces map-wide instead would fold in the 12 hand-fed
        # spawn furnaces and ask for far too few.
        want = n - have
        if want <= 0:
            status.log("%s array: %d furnaces already standing" % (ore, have))
            built[ore] = have
            save(p)
            continue
        if not gate("smelter_array", want, params={"ore": ore}):
            continue
        A.purpose("phase 0: %s smelter array (%d more furnaces)" % (ore, want))
        B.build_smelter_array(ore, n)
        after = _array_furnaces(ore, n)
        if after <= have:
            status.log("%s array: furnace count did not move (%d) - not marking it built"
                       % (ore, after))
            continue
        status.log("%s array: %d -> %d furnaces" % (ore, have, after))
        if after >= n:
            built[ore] = after
            save(p)


def _array_furnaces(ore, n):
    """Furnaces standing in THIS array's own furnace band (oy+2..oy+3) - the same box
    bootstrap.build_smelter_array uses for its own idempotence check, so the two agree on
    what "already built" means. READ ONLY; -1 when the read fails."""
    ox, oy = B.SMELT_ZONE[ore]
    out = (A._print("/sc rcon.print(#game.surfaces[1].find_entities_filtered"
                    "{area={{%d,%d},{%d,%d}},name={'stone-furnace','steel-furnace'}})"
                    % (ox, oy + 2, ox + n * 2 + 2, oy + 3)) or "").strip()
    try:
        return int(out)
    except (ValueError, TypeError):
        return -1


def _array_ore_belt(ore):
    """West end of an array's ORE belt - where a supply lane hands off (build_smelter_array
    lays that row at oy+5, from ox-1 east)."""
    ox, oy = B.SMELT_ZONE[ore]
    return (ox - 1, oy + 5)


def stage_array_grid(p):
    """A REGULAR LATTICE over the smelting block, anchored to the spine.

    build_smelter_array flanks its rows with poles every 3 and then spines them north at 3;
    that is the opportunistic chain the operator called a hack and relaid. power_planner picks
    ONE pitch and ONE phase for the whole area - which is what makes the rows wire to each
    other for free - places them in the INSERTER rows rather than beside the belts, and wires
    every pair explicitly.
    """
    if build_done(p, "array_grid"):
        return
    anchor = _pt(((p.get("spine") or {}).get("poles") or [None])[-1])
    if anchor is None:
        return
    ox, oy = B.SMELT_ZONE["iron-ore"]
    _cx, cy = B.SMELT_ZONE["copper-ore"]
    area = (SPINE_X, oy - 3, ox + 2 * max(n for _o, n in SMELTERS) + 6, cy + 8)
    ents = power_planner.scan(area)                                  # READ ONLY
    consumers = power_planner.from_entities(principles.World(ents).powered)
    if not consumers:
        status.log("array grid: no electric consumers in the smelting block yet")
        return
    if not gate("power_grid", 1):
        return
    A.purpose("phase 0: pole lattice over the smelting block")
    obs = power_planner.obstacles_for(area)                          # READ ONLY
    try:
        plan = power_planner.plan_grid(area, consumers, anchor=anchor, pole=POLE,
                                       obstacles=obs)
    except power_planner.GridError as e:
        status.log("array grid: %s - retrying next pass" % str(e)[:220])
        return
    for w in power_planner.LAST_WARNINGS:
        status.log("array grid plan: " + str(w)[:200])
    if not plan:
        return
    rec = power_planner.apply(plan, consumers=consumers, area=area, pole=POLE, anchor=anchor,
                              obstacles=obs)
    if verified(rec, "array pole lattice"):
        mark_build(p, "array_grid", rec)


def _supply(p, key, item, from_xy, to_xy, what, drills=1, relieves=None):
    """gate -> plan_supply -> build, for ONE lane. Shared by the ore and coal stages.

    plan_supply refuses a SECOND lane into the same destination and hands back the one that
    already serves it - the creation-side half of the fix for the duplicate lanes that were
    72.4%% of everything the operator deleted. That refusal is SUCCESS here, not a failure.
    """
    if build_done(p, key):
        return True
    if not gate("ore_lane", 1, relieves=relieves,
                params={"producer": "mining-drill", "drills": drills, "item": item}):
        return False
    A.purpose("phase 0: %s" % what)
    res = supply_planner.plan_supply(item, from_xy, to_xy)
    if not res.get("ok"):
        lane = res.get("lane") or {}
        if res.get("code") == supply_planner.DUPLICATE and lane.get("plan_id"):
            # the (item, destination) pair is already OWNED. FINISH that lane; never lay a
            # second one beside it - parallel duplicates were 72.4% of what the operator deleted.
            try:
                old = buildplan.load(lane["plan_id"])
            except (OSError, ValueError, KeyError):
                old = None
            if (old or {}).get("status") == "verified":
                status.log("%s: already served by lane %s" % (what, lane["id"]))
                p.setdefault("builds", {})[key] = lane["plan_id"]
                save(p)
                return True
            status.log("%s: owned by lane %s (%s) - finishing THAT lane"
                       % (what, lane["id"], lane.get("status")))
            rec = supply_planner.build(lane["plan_id"])
            if verified(rec, what):
                mark_build(p, key, rec)
                return True
            return False
        status.log("%s: not planned [%s] - %s"
                   % (what, res.get("code"), str(res.get("reason"))[:220]))
        return False
    status.log("%s: %s" % (what, res.get("reason")))
    rec = supply_planner.build(res)
    if verified(rec, what):
        mark_build(p, key, rec)
        return True
    return False


def stage_ore_lanes(p):
    """One lane per ore, mine -> its array's ore belt, through supply_planner.

    Supersedes bootstrap.connect_mine_to_array (and the ensure_lanes re-lay that called it):
    every re-lay there laid a FRESH route and left its predecessor standing, which is how
    three iron rows and two copper rows ended up side by side. supply_planner registers the
    lane, refuses a duplicate at plan time, routes with belt_router (so crossings are 2-tile
    undergrounds and nothing is ever laid through a machine, through another lane or onto a
    tile the operator cleared) and verifies with lane_lint - connected AND moving.
    """
    for ore, n in MINE_DRILLS:
        if ore not in B.SMELT_ZONE:
            continue                       # coal has no array of its own; it feeds the plant
        mine = (p.get("mines") or {}).get(ore)
        if not mine:
            continue
        _supply(p, "lane:" + ore, ore, _pt(mine["to_xy"]), _array_ore_belt(ore),
                "%s lane: mine -> smelter array" % ore, drills=n)


def stage_coal_lane(p):
    """Coal from the coal mine to the PLANT's own coal-belt intake.

    Supersedes bootstrap.coal_to_boiler, which put a splitter tap ON the ore patch, ran the
    spur descending INTO the engine footprint, and left an inserter whose pickup_position was
    a pipe tile. plant_planner.coal_intake() names the one tile an external spur may hand off
    at (the feeder column's last tile, flowing south into the belt row's west corner), and
    belt_router routes to it.

    Only the boiler is belted. The arrays' furnaces are burners too, but a second lane out of
    the same mine head would have to merge with this one, and mixed/merged lanes are the
    defect this whole module set exists to stop - array fuel stays with the controller's
    item-moving upkeep (fuel_arrays), which is servicing, not construction.
    """
    mine = (p.get("mines") or {}).get("coal")
    intake = (p.get("plant") or {}).get("coal_intake")
    if not mine or not intake:
        return
    _supply(p, "lane:coal", "coal", _pt(mine["to_xy"]), _pt(intake),
            "coal lane: mine -> boiler intake", drills=dict(MINE_DRILLS)["coal"])


def stage_science(p):
    """Green science: the CONVERTER first, then the cells. Gated on `science_assembler`,
    whose sink half is not optional - the operator deleted an iron-gear-wheel assembler that
    was sitting at full_output because nothing consumed gears."""
    if not gate("science_assembler", 1, params={"recipe": "automation-science-pack"}):
        return
    A.purpose("phase 0: automating green science assemblers")
    B.automate_green_science()
    A.purpose("phase 0: science I/O cells + powering them")
    B.setup_science_io()
    B.ensure_science_cells()   # delta-build any recipe cells the all-or-nothing pass missed


def stage_electrify(p):
    """Burner outpost -> all-electric, by RE-PLANNING the outpost, never by swapping tiers in
    place.

    Supersedes bootstrap.electrify_mines, which swapped 2x2 burner drills for 3x3 electric
    ones AT THE SAME POSITION: the bigger footprint moved drop_position by half a tile and six
    copper drills dumped ore onto bare ground while every status read looked plausible.
    upgrade_to_electric re-plans the column lattice, every drop tile, the lane span and the
    whole pole lattice from the prototype, supersedes the burner plan (tearing out only what
    the new layout cannot reuse) and builds through buildplan like everything else.
    """
    if not B._tech_done(ELECTRIC_DRILL):
        return
    anchor = _pt(((p.get("spine") or {}).get("poles") or [None])[-1])
    if anchor is None:
        return
    for ore, n in MINE_DRILLS:
        mine = (p.get("mines") or {}).get(ore)
        if not mine or mine.get("drill") == ELECTRIC_DRILL:
            continue
        # ELECTRIC by definition here - this stage exists to convert - so the gate is charged
        # the full 90 kW per drill, which is the whole point of the check it has to pass.
        if not gate("mine_outpost", n,
                    params={"drills": n, "ore": ore, "drill": ELECTRIC_DRILL}):
            continue
        A.purpose("phase 0: re-planning the %s outpost as all-electric" % ore)
        # NB: no pole= here. upgrade_to_electric passes OPERATOR_MINE_SPEC["pole"] itself and
        # forwards **kw, so a pole= would arrive twice as the same keyword.
        out = mine_planner_v2.upgrade_to_electric(
            ore, patch=None, n_drills=n, center=_pt(B.STATE.get(ore)),
            old_record_id=(p.get("builds") or {}).get(_mine_key(ore)),
            trunk=None, power_trunk_x=SPINE_X, grid_anchor=anchor)
        rec = out.get("build")
        if not verified(rec, "%s outpost (electric)" % ore):
            continue
        mark_build(p, _mine_key(ore), rec)
        plan = out["plan"]
        p["mines"][ore] = {"drill": ELECTRIC_DRILL, "n": n, "lane_y": plan["lane_y"],
                           "from_xy": list(plan["from_xy"]), "to_xy": list(plan["to_xy"])}
        # The lane's head moved with the drills, so the old lane record no longer describes
        # it. Forget it here and let stage_ore_lanes deal with it: plan_supply still sees the
        # old lane owning this (item, destination) pair, so it hands that lane back rather
        # than laying a parallel one, the re-apply fails its lane_lint check (its head is a
        # tile with no drill behind it any more), and a failed lane is retired - which frees
        # the pair for a fresh route on the next pass. Never a second belt beside the first.
        p.get("builds", {}).pop("lane:" + ore, None)
        save(p)


def stage_oil(p):
    A.purpose("phase 0: locating crude oil for phase 1")
    scout_oil(p)


# ------------------------------------------------------------------ relief (LAW 5)
def _relief_mine(p, ore, n, drill, relieves):
    """Expand a mine by `n` drills at the tier that is LEGAL NOW.

    The structural gap this fills: MINE_DRILLS is a one-shot target and stage_mines
    short-circuits on build_done, so the dependency graph contained the edge "power_capacity
    requires coal" and NO edge that raised coal in response. A gate that says "mine more coal
    first" to a builder with no way to mine more coal is a dead end with good manners.

    The plan is for the TOTAL (existing + n) drills: buildplan probes the world first, finds
    what already stands and places only the delta - the same mechanism plant_planner.scale
    relies on - so this EXTENDS the outpost instead of laying a second one beside it.
    """
    spot = B.STATE.get(ore)
    if not spot:
        status.log("relief mine %s: patch not scouted - nothing to expand" % ore)
        return False
    drill = drill or _drill_for(ore)
    if drill is None:
        status.log("relief mine %s: electric is the standard on this base and the grid cannot "
                   "carry another drill - the relief is POWER, not a burner outpost" % ore)
        return False
    anchor = _pt(((p.get("spine") or {}).get("poles") or [None])[-1])
    if drill == ELECTRIC_DRILL and anchor is None:
        status.log("relief mine %s: an electric drill needs a grid to join (net 405) and no "
                   "spine is recorded" % ore)
        return False
    # COUNT WHAT ACTUALLY STANDS, not what phase.json remembers. The operator rebuilt these
    # outposts by hand - 4 electric coal drills stand on the coal patch while phase.json still
    # says n=0 - so sizing `total` from the ledger plans a SECOND outpost on top of a working
    # one. buildplan only places the delta, and the delta is only right if `have` is real.
    rx, ry = int(spot[0]), int(spot[1])
    try:
        have = int(A._print(
            "/sc rcon.print(#game.surfaces[1].find_entities_filtered{type='mining-drill',"
            "position={%d,%d},radius=30})" % (rx, ry)).strip())
    except ValueError:
        have = int(((p.get("mines") or {}).get(ore) or {}).get("n") or 0)
    total = have + int(n)
    if not gate("mine_outpost", int(n), relieves=relieves,
                params={"drills": total, "ore": ore, "drill": drill}):
        return False
    A.purpose("phase 0 relief: %s mine -> %d %s" % (ore, total, drill))
    try:
        plan = mine_planner_v2.plan_outpost(
            ore, total, center=_pt(spot), drill=drill, pole=POLE,
            trunk=None, power_trunk_x=(SPINE_X if anchor else None), grid_anchor=anchor)
    except mine_planner_v2.LayoutError as e:
        status.log("relief mine %s: plan refused - %s" % (ore, str(e)[:220]))
        return False
    for w in plan.get("warnings", ()):
        status.log("relief mine %s plan: %s" % (ore, str(w)[:200]))
    rec = mine_planner_v2.build(plan)
    if not verified(rec, "%s mine relief (+%d %s)" % (ore, n, drill)):
        return False
    mark_build(p, _mine_key(ore), rec)
    p.setdefault("mines", {})[ore] = {
        "drill": drill, "n": total, "lane_y": plan["lane_y"],
        "from_xy": list(plan["from_xy"]), "to_xy": list(plan["to_xy"]),
    }
    save(p)
    return True


def _relief_lane(p, item, relieves):
    """Lay the lane that delivers `item` - the cheapest relief there is: belts only, 0 kW, no
    new machine, and plan_supply refuses a duplicate outright."""
    mine = (p.get("mines") or {}).get(item)
    if not mine:
        status.log("relief lane %s: no %s mine recorded - the mine is the relief here, not "
                   "the lane" % (item, item))
        return False
    if item == "coal":
        intake = (p.get("plant") or {}).get("coal_intake")
        if not intake:
            status.log("relief lane coal: the standing plant has no recorded coal intake - "
                       "plant_planner.coal_intake() names the one tile an external spur may "
                       "hand off at, and only a plant planned through it has one")
            return False
        return bool(_supply(p, "lane:coal", "coal", _pt(mine["to_xy"]), _pt(intake),
                            "coal lane: mine -> boiler intake (RELIEF)",
                            drills=dict(MINE_DRILLS).get("coal", 1), relieves=relieves))
    if item not in B.SMELT_ZONE:
        status.log("relief lane %s: no destination array for it" % item)
        return False
    return bool(_supply(p, "lane:" + item, item, _pt(mine["to_xy"]), _array_ore_belt(item),
                        "%s lane: mine -> smelter array (RELIEF)" % item,
                        drills=dict(MINE_DRILLS).get(item, 1), relieves=relieves))


def _relief_plant(p, r, rel):
    stage_plant(p)
    stage_plant_expand(p)
    return build_done(p, "plant")


def _relief_spine(p, r, rel):
    stage_spine(p)
    return build_done(p, "spine")


RELIEF_EXECUTORS = {
    "mine_outpost": lambda p, r, rel: _relief_mine(p, (r.get("params") or {}).get("ore"),
                                                   r.get("n", 1),
                                                   (r.get("params") or {}).get("drill"), rel),
    "ore_lane": lambda p, r, rel: _relief_lane(p, (r.get("params") or {}).get("item"), rel),
    "plate_lane": lambda p, r, rel: _relief_lane(p, (r.get("params") or {}).get("item"), rel),
    "power_capacity": _relief_plant,
    "power_grid": _relief_spine,
}


def stage_relief(p):
    """THE LEGAL MOVE A STUCK BASE STILL HAS.

    The previous pass ended with ZERO verified builds and at least one refusal, named the
    binding constraint and recorded the build that increases it (see _detect_deadlock). This
    stage executes exactly that build - gated, planned and VERIFIED like every other, with
    `relieves=` set so the check that is failing only FOR WANT OF THIS BUILD does not refuse
    it (LAW 5). Every other refusal still stands, and a build that fails verification still
    leaves nothing behind.

    It runs EARLY, ahead of the stages it exists to unblock, and it is a no-op on a healthy
    base: nothing is ever recorded unless a whole pass produced nothing at all.
    """
    r = p.get("relief")
    if not r:
        return
    p.pop("relief", None)                    # one attempt per record: never a retry loop
    _PASS["relief_done"] = True
    structure, key = r.get("structure"), r.get("key")
    status.log("relief: attempting %s x%s [%s] - it increases %s, which is blocking %s"
               % (structure, r.get("n"), key, r.get("constraint"),
                  ", ".join(r.get("unblocks") or []) or "the pass"))
    fn = RELIEF_EXECUTORS.get(structure)
    if fn is None:
        status.log("relief: no executor for %s - that constraint needs a stage, not a "
                   "one-off build" % structure)
        ok = False
    else:
        try:
            ok = bool(fn(p, r, (r.get("constraint"),)))
        except Exception as e:
            # A relief that RAISES is a relief that did not execute, and it must land in the
            # ledger like any other failure or the detector re-proposes it every 90 s forever.
            # `relief_tried` is cleared the moment any pass verifies a build, so a transient
            # failure retires a rung for exactly as long as the world stays stuck.
            status.log("relief %s: %s: %s" % (key, type(e).__name__, str(e)[:180]))
            ok = False
    # Recorded either way. `relief_tried` is what makes the ladder ESCALATE: next_relief skips
    # a rung already handed out, so "lay the coal lane" that cannot be executed becomes "mine
    # more coal" on the next pass instead of the same impossible move forever.
    ledger = "relief_done" if ok else "relief_tried"
    if key and key not in p.setdefault(ledger, []):
        p[ledger].append(key)
    status.log("relief: %s %s" % (key, "BUILT" if ok else "not executable - escalating"))
    save(p)


PHASE0_STAGES = (
    ("world", stage_world),                # setup / scout / hand-fed spawn furnaces
    ("plant", stage_plant),                # plant_planner   | power_capacity (FIRST column)
    ("spine", stage_spine),                # power_planner   | power_grid
    ("relief", stage_relief),              # LAW 5: the move a deadlocked pass named
    ("mines", stage_mines),                # mine_planner_v2 | mine_outpost
    ("arrays", stage_arrays),              # (no planner yet) | smelter_array
    ("array_grid", stage_array_grid),      # power_planner   | power_grid
    ("ore_lanes", stage_ore_lanes),        # supply_planner  | ore_lane
    ("coal_lane", stage_coal_lane),        # supply_planner  | ore_lane
    ("plant_expand", stage_plant_expand),  # plant_planner   | power_capacity (columns 2..N)
    ("electrify", stage_electrify),        # mine_planner_v2 | mine_outpost
    ("red_science", stage_red_science),    # the one hand-fed lab
    ("science", stage_science),            # (no planner yet) | science_assembler
    ("oil", stage_oil),                    # scout only
)
# THE ORDER IS THE DEPENDENCY GRAPH, NOT A HABIT (2026-08-30). Two inversions produced the
# live deadlock and both are corrected above:
#   1. EVERY BUILD THAT NEEDS NO POWER IS ATTEMPTED FIRST. Boilers, engines, the offshore
#      pump, poles, burner drills, stone furnaces, belts, undergrounds and splitters are all
#      in build_gates.NON_ELECTRIC - they draw nothing, so no headroom can gate them. They
#      hold stages 1-10; the three stages that genuinely need power headroom (electrify,
#      red_science, science) come last, where a refusal costs the rest of the pass nothing.
#   2. THE COAL LANE PRECEDES PLANT EXPANSION. A boiler column burns 27 coal/min and
#      power_capacity's own gate refuses one with no coal behind it, so the fuel has to arrive
#      first. `plant` (the unconditional first column) stays at the front; `plant_expand` -
#      every column after it - now sits behind `coal_lane`, where it belongs.
# Ordering alone is not a dependency, so a stage only meaningful after another says so in
# STAGE_SPEC and is SKIPPED WITH A REASON instead of returning silently: nine stages used to
# die without a log line, so the operator saw four blocked gates and no hint that five more
# stages had never run at all.


def _pre_power(p, st):
    if build_gates.capacity_mw(st) > 0:
        return True, ""
    return False, "no generation installed - the plant stage runs first"


def _pre_plant_poles(p, st):
    if plant_poles(p, st):
        return True, ""
    return False, ("no plant poles recorded and none standing near the shore - the plant "
                   "stage runs first")


def _pre_spine(p, st):
    if (p.get("spine") or {}).get("poles"):
        return True, ""
    return False, "no power spine recorded - the spine stage runs first"


def _pre_plant_record(p, st):
    """A plant to extend: a buildplan record, or a STANDING plant that can be reconstructed
    and read back. Requiring the record alone made columns 2..N unreachable on the operator's
    own base - the plant was there, the gate said ALLOW, and nothing could build."""
    if (p.get("builds") or {}).get("plant"):
        return True, ""
    if adopt_plant(p, st).get("pump"):
        return True, ""
    return False, ("no plant record to extend and no standing plant this planner can "
                   "reconstruct - the first column is stage_plant's, and an expansion is "
                   "never a blind re-plan")


def _pre_coal_lane(p, st):
    if not (p.get("mines") or {}).get("coal"):
        return False, "no coal mine recorded - the mines stage runs first"
    if not (p.get("plant") or {}).get("coal_intake"):
        adopt_plant(p, st)          # a plant HE built still names an intake once reconstructed
    if not (p.get("plant") or {}).get("coal_intake"):
        return False, ("the standing plant has no recorded coal intake tile - only a plant "
                       "planned through plant_planner.coal_intake() names one")
    return True, ""


STAGE_SPEC = {
    "spine":        {"pre": _pre_plant_poles, "power": False},
    "array_grid":   {"pre": _pre_spine, "power": False},
    "coal_lane":    {"pre": _pre_coal_lane, "power": False},
    "plant_expand": {"pre": _pre_plant_record, "power": False},
    "electrify":    {"pre": _pre_spine, "power": True},
    "red_science":  {"pre": _pre_power, "power": True},
    "science":      {"pre": _pre_power, "power": True},
    "oil":          {"power": False, "builds": False},   # scouts; places nothing
}
# The no-power BUILDS, in order. `oil` is excluded because it constructs nothing at all: it
# neither needs power nor competes for the front of the pass.
NO_POWER_STAGES = tuple(n for n, _fn in PHASE0_STAGES
                        if (STAGE_SPEC.get(n) or {}).get("builds", True)
                        and not (STAGE_SPEC.get(n) or {}).get("power"))


def _detect_deadlock(p):
    """ZERO verified builds AND at least one refusal = DEADLOCKED, not idle.

    One line, once per stuck pass, naming the constraint that is actually binding and the
    build that increases it - and that build is RECORDED, so stage_relief attempts it on the
    next pass. The alternative is what ran live: 90 s of "phase 0 gate not met" forever, with
    four true statements about why nothing may be built and no statement at all about what may.
    """
    if _PASS["built"] or not _PASS["blocked"]:
        p.pop("relief", None)                 # progress: no stale relief left to chase
        if _PASS["built"]:
            # ...and the LADDER RESETS. The ledgers record judgements about a world that just
            # changed ("the coal lane could not be laid", "that rung is already built"), and a
            # verified build is exactly the event that can make them wrong.
            for k in ("relief_tried", "relief_done"):
                if p.pop(k, None) is not None:
                    save(p)
        return None
    try:
        st = gate_state()
        d = build_gates.deadlock(st, blocked=_PASS["blocked"],
                                 done=(set(p.get("relief_done") or ())
                                       | set(p.get("relief_tried") or ())))
    except Exception as e:
        status.log("DEADLOCK: %d gate(s) blocked, 0 builds verified, and the relief search "
                   "itself failed (%s) - this needs a human"
                   % (len(_PASS["blocked"]), str(e)[:160]))
        return None
    if d is None:
        return None
    status.log(d["line"][:400])
    if d.get("relief"):
        p["relief"] = d["relief"]
        save(p)
    elif _PASS["relief_done"]:
        status.log("DEADLOCK: the relief attempted this pass did not clear it and no further "
                   "relief is legal - escalating to the operator")
    return d


def phase0(p):
    """Crash site -> automated red+green science + all-electric mines + oil scouted.

    Every stage is idempotent and INDEPENDENT: one stage raising does not abandon the pass,
    because a gate refusal upstream is the normal state of a young base and the stages behind
    it may well be allowed. The error is logged and codified as a lesson, exactly as play()
    would have done, and the next stage runs.

    A stage whose PRECONDITION is unmet is skipped WITH A REASON. "Only meaningful after X" is
    a dependency, and a dependency that shows up as nothing in the log is indistinguishable
    from a stage that ran and found nothing to do.
    """
    gate_reset()                # every pass gates against a freshly sensed world
    pass_reset()                # ...and counts what this pass actually built and refused
    # ROOM TO BUILD, BEFORE ANY GATE IS ASKED. A full inventory makes can_insert false for every
    # item, so the crafter produces nothing and A.place refuses with NO_ITEM - and none of that
    # is visible from up here: the pass just looks gate-blocked. Cheap and idempotent (a free-
    # space read, then nothing) so it belongs at the top of the pass rather than behind a
    # condition someone has to remember to check. See bootstrap.ensure_inventory_room.
    try:
        B.ensure_inventory_room()
    except Exception as e:
        status.log("depot: offload failed (%s: %s) - continuing; builds may hit NO_ITEM"
                   % (type(e).__name__, str(e)[:120]))
    for name, fn in PHASE0_STAGES:
        if B.operator_present():
            status.log("operator online mid-pass - stopping phase 0 before stage %s" % name)
            return
        pre = (STAGE_SPEC.get(name) or {}).get("pre")
        if pre is not None:
            try:
                ok, why = pre(p, gate_state())
            except Exception as e:
                ok, why = False, "precondition could not be evaluated (%s)" % str(e)[:120]
            if not ok:
                status.log("stage %s: SKIPPED - %s" % (name, why))
                continue
        try:
            fn(p)
        except Exception as e:
            status.log("phase 0 stage %s: %s: %s" % (name, type(e).__name__, e))
            lessons.add(condition="phase 0 stage %s" % name, mistake=str(e)[:200],
                        rule="see traceback in autopilot.log",
                        evidence=traceback.format_exc()[-1200:],
                        phase=0, tags=("phase-program", name))
    _detect_deadlock(p)


def gate0():
    """Exit gate for phase 0 (design §5): science running, power headroom, key techs, oil known."""
    d = delta()
    checks = {
        "power": d.get("engine_energy", 0) > 0,
        "labs": d.get("labs_working", 0) >= 2,
        "automation-2": B._tech_done("automation-2"),
        "logistics-2": B._tech_done("logistics-2"),
        "oil": load().get("oil") is not None,
    }
    return all(checks.values()), checks


# ------------------------------------------------------------------ phase 1 (v1 scope)
def phase1(p):
    """Oil economy, blueprint-first: powered pumpjack fires the oil-processing trigger, then
    the Nilaus Basic Oil Processing Block is ghost-stamped and revived incrementally with real
    materials (phase1_oil.advance, idempotent per pass). Research keeps advancing toward
    construction-robotics via the science strand."""
    if not p.get("oil") and not scout_oil(p, max_radius=640):
        raise RuntimeError("phase1: no crude oil located")
    import phase1_oil
    phase1_oil.advance(p)
    save(p)


def gate1():
    checks = {"construction-robotics": B._tech_done("construction-robotics")}
    return all(checks.values()), checks


# ------------------------------------------------------------------ phases 2-3
def phase2(p):
    import phase2_mall
    phase2_mall.advance(p)
    save(p)


def gate2():
    import phase2_mall
    return phase2_mall.gate(load())


def phase3(p):
    import phase3_grid
    phase3_grid.advance(p)
    save(p)


def gate3():
    import phase3_grid
    return phase3_grid.gate(load())


PHASES = {0: (phase0, gate0), 1: (phase1, gate1), 2: (phase2, gate2), 3: (phase3, gate3)}


# ------------------------------------------------------------------ top level
def play():
    """v2 control structure (Seth's sweep, 2026-08-29): controller.py owns realtime -
    sensing, issue detection, prioritized fixing, learning, operator prompts - on its own
    thread. THIS loop is only the BUILDER: advance the current phase's program in idempotent
    passes, evaluate gates, move on. No maintain bursts, no lap hooks: fixing problems is no
    longer a phase of the loop, it IS the other loop."""
    import controller
    p = load()
    _restore_state(p)
    A.stop()                    # clear any stuck walking_state from a mid-walk restart
    controller.start()
    # WHAT CHANGED WHILE WE WERE DOWN IS THE OPERATOR'S. The login/logoff hook can only see a
    # transition it is running to observe, and the bot is most often stopped exactly when he
    # logs in to repair something - on 2026-08-30 he rebuilt both smelter-array output belts
    # while the container was down, and the next session rediscovered those facts from scratch
    # and reported them back to him as news. Diffing the durable baseline at startup is the
    # only moment that can catch an edit made in our absence, and his removals are INTENT:
    # they go straight into the protected set so nothing rebuilds over them.
    try:
        d = B.diff_since_baseline()
        if d.get("removed") or d.get("added"):
            status.log("startup: the world changed while the builder was down - treating it as "
                       "the operator's work, not as damage to repair")
    except Exception as e:
        status.log("startup baseline diff failed (%s: %s)" % (type(e).__name__, str(e)[:120]))
    status.log(f"play(): builder resuming at phase {p['phase']} (controller running)")
    while True:
      try:                       # EVERYTHING in the try: builder crashes outside a try were
        B.ensure_derpface()      # the 18-restarts-in-108-min churn (audit item 1/8)
        phase = p["phase"]
        if phase not in PHASES:
            status.log(f"play(): phase {phase} has no program yet - holding (controller keeps the base alive)")
            time.sleep(60)
            continue
        program, gate = PHASES[phase]
        try:
            status.write_status(B.BUILD_QUEUE)
            # FULL BUILDER PAUSE while the operator is in-game (Seth, 2026-08-30: 'you are
            # rebuilding shit I've deleted while I'm logged in'). Gating only the self-heals
            # was not enough - the phase PROGRAM builds too. Nothing is constructed while a
            # human is connected; the controller still services fuel/feed/research.
            if B.operator_present():
                status.log("operator online - builder paused (no construction while you play)")
                time.sleep(20)
                continue
            if os.environ.get("BUILDER_ENABLED", "0") != "1":
                # SAFE MODE (default since 2026-08-30): the controller keeps the base alive
                # (fuel/feed/research/power) but NOTHING is constructed until the operator
                # explicitly enables the builder. He asked for zero unrequested building.
                status.log("builder disabled (BUILDER_ENABLED=0) - controller-only safe mode")
                time.sleep(60)
                continue
            program(p)
        except Exception as e:
            status.log(f"phase {phase} program error: {e}")
            lessons.add(condition=f"phase {phase} build pass", mistake=str(e)[:200],
                        rule="see traceback in autopilot.log", evidence=traceback.format_exc()[-1500:],
                        phase=phase, tags=("phase-program",))
        # operator-queued build tasks run between passes (controller only queues them)
        while B.BUILD_QUEUE and not B.operator_present():
            task = B.BUILD_QUEUE.pop(0)
            name = getattr(task, "__name__", "task")
            status.log(f"builder: operator task {name}")
            A.purpose(f"operator request: {name}")
            try:
                task()
            except Exception as e:
                status.log(f"operator task {name} error: {e}")
        ok, checks = gate()
        p["gates"][str(phase)] = checks
        _persist_state(p)
        if ok:
            status.log(f"PHASE {phase} GATE PASSED: {checks} -> advancing to {phase + 1}")
            p["phase"] = phase + 1
            save(p)
            continue
        status.log(f"phase {phase} gate not met: " +
                   ", ".join(k for k, v in checks.items() if not v) +
                   " - builder idles 90s (controller keeps working)")
        time.sleep(90)
      except Exception as e:
        status.log(f"builder loop error (recovering in 30s): {e}\n{traceback.format_exc()[-500:]}")
        time.sleep(30)


if __name__ == "__main__":
    play()
