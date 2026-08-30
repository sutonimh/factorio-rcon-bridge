#!/usr/bin/env python3
"""Realtime issue-driven controller — the v2 control loop (supersedes bootstrap.maintain).

Seth's directive (2026-08-29): problems are found and fixed IN REALTIME, prioritized above
routine work; the old maintenance loop should not exist. This module is that inversion:

    SENSE -> DETECT (rule battery) -> PRIORITIZE (severity) -> FIX (one actuator) ->
    VERIFY (re-detect) -> ESCALATE (35B architect on repeated failure) -> LEARN (lessons)
    ... and only when NO issue is pending does the BUILDER advance the phase program.

Two threads (RCON is thread-safe - fresh socket per call):
  - controller thread: the loop above, ~3s cadence; all its fixers are SERVER-SIDE (instant,
    no character). A fixer that needs the character posts a preempt request instead.
  - builder thread: runs the phase program steps (walks/mines/builds). autopilot.walk polls
    controller.PREEMPT between legs, so a severity-0/1 issue interrupts a long walk within
    seconds; the builder services the request, then resumes its step.

Every fixer's trigger and outcome is recorded; an issue that keeps recurring writes a lesson
(the automated GOTCHAS) and repeated fixer FAILURE escalates to the local 35B architect.
"""
import json
import threading
import time
import traceback

import autopilot as A
import bootstrap as B
import lessons
import status

import pathlib

PREEMPT = {"want": None}            # set to an Issue id when a character fixer is needed
_COOLDOWN = {}                      # issue id -> monotonic ts of last fix attempt (in-proc)
_FAILS = {}                         # issue id -> consecutive verify failures
_last_arch = {"t": 0.0}
ARCH_COOLDOWN_S = 600
_STATE_PATH = pathlib.Path(__file__).resolve().parent / "controller-state.json"
_HIST = []                          # (wallclock, sense dict) ring for the progress watchdog
_PREV = {"d": None}
_LAST_VERDICT = {"s": "", "n": 0}
_TRIAGE_BUSY = {"b": False}
_OP_PREV = {"p": False}
_OP_SNAP = {"belts": None, "world": None}


def _load_state():
    """Restart-churn survival (audit: 18 restarts/108min reset every cooldown/counter)."""
    try:
        st = json.loads(_STATE_PATH.read_text())
        _FAILS.update(st.get("fails", {}))
        _last_arch["t"] = time.monotonic() - max(0.0, time.time() - st.get("last_arch_wall", 0))
    except (OSError, ValueError):
        pass


def _save_state():
    try:
        _STATE_PATH.write_text(json.dumps({
            "fails": _FAILS,
            "last_arch_wall": time.time() - max(0.0, time.monotonic() - _last_arch["t"]),
        }))
    except OSError:
        pass


# --------------------------------------------------------------------- sensing
def sense():
    """One compact server-side read: everything the detectors need."""
    out = A._print(
        "/sc local s=game.surfaces[1]; local f=game.forces.player; local d=storage.derpface;"
        "local SN={}; for k,v in pairs(defines.entity_status) do SN[v]=k end;"
        "local eng,engl=0,0; for _,e in pairs(s.find_entities_filtered{name='steam-engine'}) do engl=engl+1; eng=eng+e.energy end;"
        "local b=s.find_entities_filtered{name='boiler',limit=1}[1];"
        "local labs,lw=0,0; for _,l in pairs(s.find_entities_filtered{name='lab'}) do labs=labs+1; if l.status==defines.entity_status.working then lw=lw+1 end end;"
        "local am,aw,anp=0,0,0; for _,a in pairs(s.find_entities_filtered{type='assembling-machine'}) do am=am+1;"
        "  if a.status==defines.entity_status.working then aw=aw+1 elseif a.status==defines.entity_status.no_power then anp=anp+1 end end;"
        "local dr,dw=0,0; for _,x in pairs(s.find_entities_filtered{type='mining-drill'}) do dr=dr+1;"
        "  if x.status==defines.entity_status.waiting_for_space_in_destination then dw=dw+1 end end;"
        "local fu,fs,fw=0,0,0; for _,x in pairs(s.find_entities_filtered{name={'stone-furnace','steel-furnace'}}) do fu=fu+1;"
        "  if x.status==defines.entity_status.no_ingredients then fs=fs+1 elseif x.status==defines.entity_status.working then fw=fw+1 end end;"
        "local free=d and d.valid and d.get_main_inventory().count_empty_stacks() or -1;"
        "local packs=d and d.valid and (d.get_main_inventory().get_item_count('automation-science-pack')+d.get_main_inventory().get_item_count('logistic-science-pack')) or 0;"
        "local nets={}; local nc=0; for _,p2 in pairs(s.find_entities_filtered{type='electric-pole'}) do local id=p2.electric_network_id; if id and not nets[id] then nets[id]=true; nc=nc+1 end end;"
        "local ps=f.get_item_production_statistics(s);"
        "local function pm(n) return math.floor(ps.get_flow_count{name=n,category='input',precision_index=defines.flow_precision_index.one_minute}) end;"
        "rcon.print(helpers.table_to_json({tick=game.tick,"
        "iron_pm=pm('iron-plate'),copper_pm=pm('copper-plate'),"
        "science_pm=pm('automation-science-pack')+pm('logistic-science-pack'),"
        "engines=engl,engine_energy=math.floor(eng),boiler_fuel=(b and b.get_fuel_inventory().get_item_count('coal') or -1),"
        "labs=labs,labs_working=lw,asm=am,asm_working=aw,asm_no_power=anp,"
        "drills=dr,drills_blocked=dw,furnaces=fu,furnaces_starved=fs,furnaces_working=fw,"
        "free_slots=free,packs_carried=packs,power_networks=nc,"
        "research=(f.current_research and f.current_research.name or ''),"
        "research_pct=(f.current_research and math.floor(f.research_progress*100) or -1)}))").strip()
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return {}


# --------------------------------------------------------------------- issues
class Issue:
    def __init__(self, iid, sev, evidence, fixer, character=False, cooldown=20):
        self.id, self.sev, self.evidence = iid, sev, evidence
        self.fixer, self.character, self.cooldown = fixer, character, cooldown


def detect(d):
    """Rule battery -> prioritized issue list. Severity: 0 fatal, 1 supply-chain, 2 progress."""
    issues = []
    add = issues.append
    if not d:
        return issues
    now = time.time()
    _HIST.append((now, d))
    del _HIST[:-600]
    # PROGRESS WATCHDOG: the only detector that would have caught the silent dead month
    # (Jul->Aug: one log line/day, zero production, nothing noticed). 15 min of zero plate
    # AND zero science flow with a built base = severity 0, straight to escalation.
    past = [h for h in _HIST if now - h[0] > 900]
    if past and d.get("furnaces", 0) >= 8:
        p0 = past[-1][1]
        flat = (d.get("iron_pm", 0) == 0 and p0.get("iron_pm", 0) == 0
                and d.get("science_pm", 0) == 0 and p0.get("science_pm", 0) == 0)
        if flat:
            add(Issue("no_progress", 0, "zero plate+science flow for 15+ min on a built base",
                      _fix_lanes, cooldown=300))
    built = d.get("engines", 0) or d.get("drills", 0) or d.get("labs", 0)
    if not built:
        return issues                       # nothing exists yet: builder's job, no issues
    if d.get("engines", 0) and d.get("engine_energy", 0) <= 0:
        add(Issue("power_dead", 0, f"engines={d['engines']} energy=0", B.keep_power))
    if 0 <= d.get("boiler_fuel", -1) < 3:
        add(Issue("boiler_dry", 0, f"boiler_fuel={d['boiler_fuel']}", B.keep_power))
    if d.get("power_networks", 1) > 1:
        add(Issue("grid_split", 1, f"{d['power_networks']} electric networks", B.ensure_grid_connected))
    if d.get("asm_no_power", 0) > 0:
        add(Issue("consumers_unpowered", 1, f"{d['asm_no_power']} assemblers no_power", B.fix_unpowered))
    if d.get("drills_blocked", 0) >= 3 and d.get("furnaces_starved", 0) >= 3:
        add(Issue("lane_stalled", 1,
                  f"{d['drills_blocked']} drills blocked + {d['furnaces_starved']} furnaces starved",
                  _fix_lanes))
    elif d.get("furnaces_starved", 0) > d.get("furnaces", 1) * 0.6 and d.get("drills", 0) >= 6:
        add(Issue("arrays_starved", 1, f"{d['furnaces_starved']}/{d['furnaces']} furnaces no_ingredients",
                  _fix_lanes, cooldown=45))
    if d.get("furnaces_working", 0) and not d.get("labs_working", 0) and d.get("labs", 0):
        add(Issue("labs_idle", 2, f"labs 0/{d['labs']} working", _fix_science, cooldown=30))
    if d.get("research") == "" and d.get("labs", 0):
        add(Issue("research_idle", 2, "no current research", _fix_research, cooldown=30))
    if 0 <= d.get("free_slots", -1) < 5:
        add(Issue("inventory_clogged", 2, f"free_slots={d['free_slots']}", B.trim_inventory))
    return sorted(issues, key=lambda i: i.sev)


# composite fixers (server-side)
def _fix_lanes():
    B.scrub_mixed_ore()
    B.repair_belt_gaps()
    return B.ensure_lanes()


def _fix_science():
    B.harvest_array_plates()
    B._collect_plates_all()
    B.harvest_plate_belts()
    B._service_assembler_chests()
    B.service_science()
    return 1


def _fix_research():
    B._advance_research()
    return 1


# --------------------------------------------------------------------- loop
def controller_loop(stop_flag):
    """The realtime evaluator. Runs forever; ~3s cadence."""
    lap = 0
    while not stop_flag["stop"]:
        t0 = time.monotonic()
        lap += 1
        try:
            d = sense()
            issues = detect(d)
            live = [i for i in issues if time.monotonic() - _COOLDOWN.get(i.id, 0) > i.cooldown]
            if live:
                top = live[0]
                _COOLDOWN[top.id] = time.monotonic()
                status.log(f"issue[{top.sev}] {top.id}: {top.evidence} -> fixing")
                A.purpose(f"fixing {top.id}: {top.evidence}")
                if top.sev == 0:
                    # sev-0 ONLY: every controller fixer is server-side, so preempting the
                    # builder for sev-1 issues was pure harm - walks died every ~20s and the
                    # character never got anywhere (the stuck-at-(6,-339) incident)
                    PREEMPT["want"] = top.id
                try:
                    top.fixer()
                except Exception as e:
                    status.log(f"fixer {top.id} error: {e}")
                # verify by re-sense on the next lap; track repeat failures
                d2 = sense()
                still = any(i.id == top.id for i in detect(d2))
                if still:
                    _FAILS[top.id] = _FAILS.get(top.id, 0) + 1
                    _save_state()
                    if _FAILS[top.id] in (2, 5):
                        lessons.add(condition=f"issue {top.id} recurs",
                                    mistake=f"fixer did not clear it ({top.evidence})",
                                    rule="fixer insufficient - see architect escalation",
                                    tags=("controller", "triage", top.id),
                                    key=f"issue:{top.id}")
                    if _FAILS[top.id] >= 3:
                        _escalate(top, d2)
                else:
                    if _FAILS.pop(top.id, 0):
                        _save_state()
                        status.log(f"issue {top.id} cleared after retries")
                PREEMPT["want"] = None
            else:
                PREEMPT["want"] = None
                # quiet lap: cheap upkeep that keeps automation fed (server-side only)
                if lap % 3 == 0:
                    _fix_science()
                if lap % 5 == 0:
                    B.fuel_drills()
                    B.fuel_arrays()
                    B.restock_coal()
                if lap % 10 == 0:
                    B.reap_dead_drills()
                    B.keep_power()
                if lap % 7 == 0 and d.get("engines") and not _TRIAGE_BUSY["b"]:
                    # residual-anomaly triage (4B, v2): rules own known signatures; the model
                    # judges the residue WITH trend, routes to a real actuator, and identical
                    # verdicts dedupe into a count instead of a 29-line scream (audit item 5)
                    def _triage_worker(dd, prev):
                      try:
                        import triage
                        v = triage.classify(dd, prev)
                        sig = f"{v.get('state')}|{v.get('class')}|{v.get('reason','')[:60]}"
                        if sig == _LAST_VERDICT["s"]:
                            _LAST_VERDICT["n"] += 1
                            if _LAST_VERDICT["n"] in (5, 20):
                                lessons.add(condition=f"triage verdict repeats: {v.get('class')}",
                                            mistake=v.get("reason", "?"),
                                            rule=f"actuator {v.get('actuator')} not clearing it",
                                            tags=("triage",), key=f"triage:{v.get('class')}")
                        else:
                            _LAST_VERDICT["s"], _LAST_VERDICT["n"] = sig, 1
                            if v.get("state") not in ("healthy", None):
                                status.log(f"triage[{v.get('_source','?')[:9]}]: {v['state']}/{v.get('class')} - {v['reason']}")
                        act = v.get("actuator")
                        if v.get("state") in ("stall", "anomaly") and act and act != "none":
                            fn = {"keep_power": B.keep_power, "fix_unpowered": B.fix_unpowered,
                                  "ensure_grid_connected": B.ensure_grid_connected,
                                  "fix_lanes": _fix_lanes, "fix_science": _fix_science,
                                  "fix_research": _fix_research, "trim_inventory": B.trim_inventory}.get(act)
                            if fn:
                                status.log(f"triage -> actuator {act}")
                                fn()
                        elif v.get("wake_architect"):
                            _escalate(Issue("triage_" + str(v.get("class") or v["state"]), 2,
                                            v.get("reason", ""), lambda: None), d)
                      except Exception as e:
                        status.log(f"triage: {e}")
                      finally:
                        _TRIAGE_BUSY["b"] = False
                    _TRIAGE_BUSY["b"] = True
                    threading.Thread(target=_triage_worker, args=(d, _PREV["d"]), daemon=True).start()
                _PREV["d"] = d
            # OPERATOR LOGOFF HOOK: when Seth disconnects, run the cleanup he ordered
            # ('once I log off clean this shit up') + deferred layout work, once
            op_now = B.operator_present()
            if op_now and not _OP_PREV["p"]:
                try:                       # snapshot so we can learn what he changes
                    _OP_SNAP["belts"] = B.belt_tiles_now()
                    _OP_SNAP["world"] = B.world_snapshot()
                    status.log(f"operator online - layout heals suspended (snapshot {len(_OP_SNAP['belts'])} belts / {len(_OP_SNAP['world'])} entities)")
                except Exception as e:
                    status.log(f"operator snapshot: {e}")
            if _OP_PREV["p"] and not op_now:
                try:                       # his deletions are INTENT: protect them forever
                    B.record_operator_deletions(_OP_SNAP.get("belts"))
                except Exception as e:
                    status.log(f"record deletions: {e}")
                # ...and his edits are TEACHING: infer why, store durable rules (threaded so
                # the 35B call never delays the resumed heals)
                _w = _OP_SNAP.get("world")
                _OP_SNAP["belts"] = None
                _OP_SNAP["world"] = None
                if _w:
                    threading.Thread(target=lambda: B.learn_from_operator_edits(_w),
                                     daemon=True).start()
                status.log("operator logged off - running cleanup + deferred layout work")
                for fn in ("cleanup_orphan_cells", "repair_plate_rows", "coal_to_boiler"):
                    try:
                        getattr(B, fn)()
                    except Exception as e:
                        status.log(f"logoff {fn}: {e}")
            _OP_PREV["p"] = op_now
            # operator prompts are realtime, always
            try:
                import operator2
                operator2.process_inbox()
            except Exception as e:
                status.log(f"operator inbox: {e}")
            if lap % 4 == 0:
                status.write_status(B.BUILD_QUEUE)
        except Exception as e:
            status.log(f"controller lap error: {e}\n{traceback.format_exc()[-400:]}")
        time.sleep(max(0.5, 3.0 - (time.monotonic() - t0)))


def _escalate(issue, d):
    """Repeated fixer failure -> focused local-35B architect analysis in a thread."""
    if time.monotonic() - _last_arch["t"] < ARCH_COOLDOWN_S:
        return
    _last_arch["t"] = time.monotonic()

    def run():
        try:
            import architect
            snap = architect.snapshot()
            rep = architect.analyze_local(
                snap, focus=f"issue '{issue.id}' persists after 3+ fixes: {issue.evidence}. "
                            f"Diagnose the ROOT CAUSE the fixer misses.")
            architect.REPORT_PATH.write_text(json.dumps(rep, indent=2))
            status.log("architect(escalation): " + rep.get("summary", "")[:180])
            for b in rep.get("bottlenecks", []):
                if b.get("severity") == "high":
                    # per-bottleneck rule (the old code stuffed action[0] into EVERY lesson)
                    lessons.add(condition=f"{issue.id}: {b.get('area', '?')}",
                                mistake=b.get("root_cause", "?"),
                                rule=b.get("fix") or b.get("root_cause", "see report"),
                                evidence=b.get("evidence", ""), tags=("architect", "triage", issue.id),
                                key=f"arch:{issue.id}:{b.get('area', '?')[:30]}")
            # CLOSE THE LOOP (audit's core finding): the architect's commands EXECUTE via
            # the same validated actuator pipeline operator prompts use
            cmds = rep.get("commands") or []
            if cmds:
                import operator2
                accepted, rejected = operator2.validate_commands(cmds[:4])
                status.log(f"architect commands: queued {accepted or 'none'}"
                           + (f"; rejected {rejected}" if rejected else ""))
                for r in rejected:
                    lessons.add(condition="architect emitted off-catalog command",
                                mistake=r, rule="tighten the catalog prompt or add the verb",
                                tags=("architect",), key="arch:off-catalog")
        except Exception as e:
            status.log(f"architect escalation error: {e}")

    threading.Thread(target=run, daemon=True).start()


def start():
    """Start the controller thread; returns the stop flag."""
    _load_state()
    flag = {"stop": False}
    th = threading.Thread(target=controller_loop, args=(flag,), daemon=True)
    th.start()
    status.log("controller: realtime issue loop started (maintain loop retired)")
    return flag
