#!/usr/bin/env python3
"""L3 planner: the phase state machine + top-level play() sequencer (MEGABASE-V2-DESIGN §5).

play() drives a FRESH map with no user assistance: run the current phase's build program
(idempotent steps), evaluate its exit gate, advance; between build passes run the maintain
loop with the learning-loop lap hook (triage every lap batch, architect on escalation,
failures codified into lessons).

Phase programs build through bootstrap.py primitives + builds_v2 (registered into world.py).
Phase state persists to phase.json (runtime file, gitignored) so a container restart resumes.
"""
import json
import pathlib
import time
import traceback

import autopilot as A
import bootstrap as B
import builds_v2
import lessons
import status
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
        if v.get("wake_architect") and time.time() - _last_arch["t"] > ARCH_COOLDOWN_S:
            _last_arch["t"] = time.time()
            _run_architect(d, v)
    except Exception as e:
        status.log(f"lap_hook error: {e}")


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
    """scout() with the ROADMAP guard: widen + regenerate on missing resources, fail loudly."""
    B.scout()
    missing = [k for k in ("iron-ore", "copper-ore", "stone", "coal", "water") if not B.STATE.get(k)]
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


def phase0(p):
    """Crash site -> automated red+green science + registered mines + oil scouted.
    Every step is idempotent; failures raise (play() records the lesson and retries next pass)."""
    B.setup_world()
    _scout_guarded(p)
    B.fuel()
    B.smelting_base()
    if B.power() is None and not B._find("steam-engine", B.STATE["water"][0], B.STATE["water"][1], 30):
        raise RuntimeError("power(): no working steam engine after build attempt")
    B.red_science()
    for ore, n in (("iron-ore", 8), ("copper-ore", 6), ("coal", 6)):
        r = builds_v2.mine_outpost_v2(ore, n)
        if r.get("status") == "failed":
            raise RuntimeError(f"mine_outpost_v2({ore}): {r.get('error')}")
    B.build_belt_supply()
    B.automate_green_science()
    B.setup_science_io()
    scout_oil(p)


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
    """Oil trigger + research drive to construction-robotics. v1 scope: fire the oil-processing
    TRIGGER (pumpjack on the scouted patch) and keep research advancing; the oil-economy build
    (blueprint-stamped refinery/chem modules) lands after the phase-0 proving run validates."""
    oil = p.get("oil")
    if not oil:
        oil = scout_oil(p, max_radius=640)
        if not oil:
            raise RuntimeError("phase1: no crude oil located")
    if not techdb.is_trigger("oil-processing") or B._tech_done("oil-processing"):
        pass
    else:
        ox, oy = int(oil[0]), int(oil[1])
        have = B._count("pumpjack")
        if not have and not B._find("pumpjack", ox, oy, 20):
            B.make("pumpjack", 1)
        if not B._find("pumpjack", ox, oy, 20):
            A.stop(); A.walk(ox + 2, oy, tol=3.0)
            A.clear_area(ox, oy, 6)
            A.place("pumpjack", ox - 1, oy - 1)   # 3x3: center lands on the patch tile
            status.log(f"pumpjack placed at oil patch {ox},{oy} (fires oil-processing trigger on first crude)")
    # research keeps advancing via the science strand (_advance_research targets robotics chain)


def gate1():
    checks = {"construction-robotics": B._tech_done("construction-robotics")}
    return all(checks.values()), checks


PHASES = {0: (phase0, gate0), 1: (phase1, gate1)}


# ------------------------------------------------------------------ top level
def play():
    """The autonomous top loop: build pass -> gate check -> maintain burst (with lap hook) ->
    repeat. Survives restarts via phase.json. Phase programs are idempotent so a crashed pass
    just reruns."""
    p = load()
    _restore_state(p)
    status.log(f"play(): resuming at phase {p['phase']}")
    while True:
        B.ensure_derpface()    # every pass: the character can vanish on an un-autosaved restart
        phase = p["phase"]
        if phase not in PHASES:
            status.log(f"play(): phase {phase} has no program yet - holding in maintain")
            B.maintain(laps=500, lap_hook=lap_hook)
            continue
        program, gate = PHASES[phase]
        try:
            program(p)
        except Exception as e:
            status.log(f"phase {phase} program error: {e}")
            lessons.add(condition=f"phase {phase} build pass", mistake=str(e)[:200],
                        rule="see traceback in autopilot.log", evidence=traceback.format_exc()[-1500:],
                        phase=phase, tags=("phase-program",))
        ok, checks = gate()
        p["gates"][str(phase)] = checks
        _persist_state(p)
        if ok:
            status.log(f"PHASE {phase} GATE PASSED: {checks} -> advancing to {phase + 1}")
            p["phase"] = phase + 1
            save(p)
            continue
        status.log(f"phase {phase} gate not met: " +
                   ", ".join(k for k, v in checks.items() if not v) + " - maintain burst")
        B.maintain(laps=150, lap_hook=lap_hook)


if __name__ == "__main__":
    play()
