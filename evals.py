#!/usr/bin/env python3
"""Eval ladder: scripted scenario regression harness (MEGABASE-V2-DESIGN §6, build-order §9.9).

FLE-inspired ladder: one saved game per era rung (burner / bus / robot / rail). A run loads a
rung's save on a SECOND factorio instance, lets the autopilot do a bounded amount of work
(maintain laps -- no unbounded phase programs), then scores the world with one /sc snippet
(cumulative production counts + entity census + research). Scores are compared against
evals-baseline.json so a "learned" change (a Coder-30B patch, a threshold tweak) has to prove
it didn't regress an earlier era before it ships.

This module does NOT manage servers. FACTORIO_RCON_HOST/PORT must already point at an
instance running the scenario save. Intended charon usage (a wrapper cycles the saves;
ports offset so the proving-run server on 34197/27015 is untouched):

    # one rung, run from the autopilot dir on charon:
    sudo docker run -d --name factorio-eval \
      -p 34198:34198/udp -p 27016:27016/tcp \
      -v /mnt/user/appdata/factorio-eval:/factorio \
      -e PORT=34198 -e RCON_PORT=27016 \
      factoriotools/factorio:2.1.17 \
      --start-server /factorio/saves/eval-burner.zip
    FACTORIO_RCON_HOST=127.0.0.1 FACTORIO_RCON_PORT=27016 \
      python3 -c "import evals, json; print(json.dumps(evals.run_scenario('burner')))"
    sudo docker rm -f factorio-eval    # then next save

Scenario saves live on charon at /mnt/user/appdata/factorio-eval/saves/ (eval-burner.zip
etc.); they are frozen snapshots taken at each era's phase gate during proving runs -- the
first proving run is what creates them, so missing saves are expected until then.

Scores are honest: cumulative force production stats + entity counts read from the live
world. No score is synthesized; an unreachable server raises.
"""
import json
import os
import pathlib
import threading
import time

import rcon

HERE = pathlib.Path(__file__).resolve().parent
BASELINE = pathlib.Path(os.environ.get("EVALS_BASELINE", HERE / "evals-baseline.json"))

# A regression = a scored metric dropping below (1 - TOLERANCE) * baseline. Production
# counters are cumulative and the lap budget is fixed, so runs are comparable; the margin
# absorbs biter/tick noise.
TOLERANCE = 0.10

_LUA_PREFIX = (
    "/sc local s=game.surfaces[1]; local f=game.forces.player;"
    "local ps=f.get_item_production_statistics(s);"
    "local function tot(n) return ps.get_input_count(n) end;"
    "local function cnt(n) return s.count_entities_filtered{name=n, force='player'} end;"
    "local r=f.current_research;"
)

_BASE_FIELDS = {
    "tick": "game.tick",
    "iron_plates": "tot('iron-plate')",
    "copper_plates": "tot('copper-plate')",
    "red_science": "tot('automation-science-pack')",
    "entities": "s.count_entities_filtered{force='player'}",
    "research_pct": "(r and math.floor(f.research_progress*100) or 0)",
}


def _score_lua(extra=None):
    fields = dict(_BASE_FIELDS)
    fields.update(extra or {})
    body = ",".join("%s=%s" % (k, v) for k, v in sorted(fields.items()))
    return _LUA_PREFIX + "rcon.print(helpers.table_to_json({" + body + "}))"


# name / save / timeout_s / laps (bounded maintain work) / score_lua. The ladder tracks the
# §5 phase gates: each rung's save is the world frozen AT that gate.
SCENARIOS = [
    {"name": "burner", "save": "eval-burner", "timeout_s": 600, "laps": 30,
     "score_lua": _score_lua({
         "drills": "cnt('burner-mining-drill')+cnt('electric-mining-drill')",
         "furnaces": "cnt('stone-furnace')+cnt('steel-furnace')",
     })},
    {"name": "bus", "save": "eval-bus", "timeout_s": 900, "laps": 40,
     "score_lua": _score_lua({
         "green_science": "tot('logistic-science-pack')",
         "assemblers": "s.count_entities_filtered{type='assembling-machine', force='player'}",
     })},
    {"name": "robot", "save": "eval-robot", "timeout_s": 900, "laps": 40,
     "score_lua": _score_lua({
         "green_science": "tot('logistic-science-pack')",
         "roboports": "cnt('roboport')",
         "construction_robots": "cnt('construction-robot')",
     })},
    {"name": "rail", "save": "eval-rail", "timeout_s": 1200, "laps": 50,
     "score_lua": _score_lua({
         "blue_science": "tot('chemical-science-pack')",
         "locomotives": "cnt('locomotive')",
         "train_stops": "cnt('train-stop')",
     })},
]


def scenario(name):
    for sc in SCENARIOS:
        if sc["name"] == name or sc["save"] == name:
            return sc
    raise KeyError("no scenario %r (have: %s)" % (name, [s["name"] for s in SCENARIOS]))


def score(sc):
    """Run the scenario's score snippet against the live server. Raises if unreachable."""
    out = rcon.run(sc["score_lua"]).strip()
    got = json.loads(out)  # a non-JSON reply is a real failure -- let it raise
    return got


def run_scenario(name):
    """Bounded autopilot work on an already-running scenario save, then score it.

    The lap budget is the work bound; timeout_s is the wall-clock backstop (maintain runs in
    a daemon thread we join with the timeout -- a wedged lap can't hang the harness, and the
    wrapper's process exit reaps the thread). Returns {name, save, laps, duration_s,
    timed_out, before, after} -- before/after are score dicts so deltas are per-run, not
    save-age artifacts."""
    sc = scenario(name)
    before = score(sc)
    t0 = time.time()
    err = {}

    def work():
        try:
            import bootstrap
            import planner
            bootstrap.maintain(laps=sc["laps"], lap_hook=planner.lap_hook)
        except Exception as e:  # scored anyway; the error is part of the result
            err["error"] = "%s: %s" % (type(e).__name__, e)

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(sc["timeout_s"])
    result = {
        "name": sc["name"], "save": sc["save"], "laps": sc["laps"],
        "duration_s": round(time.time() - t0, 1),
        "timed_out": th.is_alive(),
        "before": before, "after": score(sc),
    }
    if err:
        result["error"] = err["error"]
    return result


def _gained(res):
    """Per-run metric gains (after - before for counters; after for gauges)."""
    cumulative = {"iron_plates", "copper_plates", "red_science", "green_science",
                  "blue_science", "tick"}
    out = {}
    for k, v in res["after"].items():
        if not isinstance(v, (int, float)):
            continue
        out[k] = v - res["before"].get(k, 0) if k in cumulative else v
    return out


def compare(baseline_file, result):
    """Compare one run against the stored baseline; create the baseline if absent.

    Returns {"status": "baseline-created" | "ok" | "regressed", "deltas": {...},
    "regressions": [...]}. Only an explicit update_baseline() moves the bar -- a passing run
    never silently rewrites it."""
    baseline_file = pathlib.Path(baseline_file)
    gains = _gained(result)
    base = json.loads(baseline_file.read_text()) if baseline_file.exists() else {}
    if result["name"] not in base:
        base[result["name"]] = gains
        baseline_file.write_text(json.dumps(base, indent=2) + "\n")
        return {"status": "baseline-created", "deltas": {}, "regressions": []}
    ref = base[result["name"]]
    deltas, regressions = {}, []
    for k, v in gains.items():
        if k == "tick" or k not in ref:
            continue
        deltas[k] = v - ref[k]
        if ref[k] > 0 and v < (1 - TOLERANCE) * ref[k]:
            regressions.append("%s: %s < baseline %s" % (k, v, ref[k]))
    return {"status": "regressed" if regressions else "ok",
            "deltas": deltas, "regressions": regressions}


def update_baseline(result, baseline_file=BASELINE):
    """Explicitly promote a run's gains to be the new baseline for its scenario."""
    baseline_file = pathlib.Path(baseline_file)
    base = json.loads(baseline_file.read_text()) if baseline_file.exists() else {}
    base[result["name"]] = _gained(result)
    baseline_file.write_text(json.dumps(base, indent=2) + "\n")


if __name__ == "__main__":
    import sys
    res = run_scenario(sys.argv[1] if len(sys.argv) > 1 else "burner")
    verdict = compare(BASELINE, res)
    print(json.dumps({"result": res, "verdict": verdict}, indent=2))
    sys.exit(1 if verdict["status"] == "regressed" else 0)
