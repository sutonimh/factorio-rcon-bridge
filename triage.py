#!/usr/bin/env python3
"""Residual-anomaly triage (v2, rebuilt per the 2026-08-29 AI-loop audit).

The controller's deterministic rule battery owns every KNOWN failure signature; the 4B only
judges what the rules did not already claim, on the controller's rich sense() dict WITH the
previous sample for trend. The audit's replay showed the old prompt failing on all six real
cases (class never emitted, wake always true, a fatal coal-death classed 'watch'): this one
documents sentinels, forces class/actuator from enums, and carries two few-shots from the
real failures. wake_architect is honored only for stall/anomaly."""
import json

import lessons
import llm

STATES = ("healthy", "watch", "stall", "anomaly")
CLASSES = ("power", "coal", "lane", "supply", "inventory", "research", "science", "build", "none")
ACTUATORS = ("keep_power", "fix_unpowered", "ensure_grid_connected", "fix_lanes",
             "fix_science", "fix_research", "trim_inventory", "none")

SYSTEM = (
    "You triage ONE Factorio base state sample (cur) with the previous sample (prev) for "
    "trend. The deterministic rule engine already handles known signatures - you judge "
    "RESIDUAL weirdness only.\n"
    "Field notes: -1 means 'no such entity yet' (NOT an error); engines/labs/asm/drills/"
    "furnaces are counts; *_working are how many run now; research_pct -1 = nothing queued; "
    "science_pm / iron_pm are per-minute production flows.\n"
    "Known signatures (rules handle these - if one plainly applies say so in reason but "
    "still classify): boiler_fuel=0+engine_energy=0 = power death (class power, "
    "keep_power); power_networks>1 = grid split (power, ensure_grid_connected); "
    "drills_blocked high + furnaces_starved high = broken ore lane (lane, fix_lanes); "
    "free_slots<5 = inventory clog (inventory, trim_inventory); iron_pm falling toward 0 "
    "with furnaces_starved rising = lane/supply break (lane); "
    "furnaces_full_output high + drills_blocked high + iron_pm 0 = DOWNSTREAM BACK-PRESSURE "
    "(the terminal chest or the consumer is full), class supply, actuator none - fix_lanes "
    "CANNOT help, the lane is intact and saturated; the relief is a consumer, a build "
    "decision.\n"
    "STARVED AND FULL ARE OPPOSITES. furnaces_starved = no input (a supply break); "
    "furnaces_full_output = no OUTPUT room (a demand problem). Never answer 'lane' when "
    "furnaces_starved is 0 and furnaces_full_output is high.\n"
    'Reply ONLY JSON: {"state":"healthy|watch|stall|anomaly","class":"power|coal|lane|'
    'supply|inventory|research|science|build|none","actuator":"keep_power|fix_unpowered|'
    'ensure_grid_connected|fix_lanes|fix_science|fix_research|trim_inventory|none",'
    '"reason":"one line","wake_architect":true|false}\n'
    "wake_architect=true ONLY for stall/anomaly you cannot map to an actuator.\n"
    "Example 1 - cur {engines:2,engine_energy:0,boiler_fuel:0,drills:20,furnaces:39} -> "
    '{"state":"stall","class":"power","actuator":"keep_power","reason":"engines dead and '
    'boiler dry - power death","wake_architect":false}\n'
    "Example 2 - cur {drills_blocked:8,furnaces_starved:36,iron_pm:0} prev {iron_pm:90} -> "
    '{"state":"stall","class":"lane","actuator":"fix_lanes","reason":"iron flow collapsed '
    '90->0 with drills blocked: ore lane broken","wake_architect":false}\n'
    "Example 3 (the 2026-08-29 misdiagnosis) - cur {drills:16,drills_blocked:12,furnaces:28,"
    "furnaces_starved:0,furnaces_full_output:28,asm:0,labs_working:0,iron_pm:0} prev "
    '{iron_pm:0} -> {"state":"stall","class":"supply","actuator":"none","reason":"every '
    "furnace is full_output and nothing consumes plates (0 assemblers) - the lane is "
    'saturated, not broken","wake_architect":true}'
)


def heuristic(d):
    if d.get("engines", 0) and d.get("engine_energy", 1) <= 0:
        return {"state": "stall", "class": "power", "actuator": "keep_power",
                "reason": "engines dead (heuristic)", "wake_architect": False}
    if 0 <= d.get("free_slots", -1) < 3:
        return {"state": "stall", "class": "inventory", "actuator": "trim_inventory",
                "reason": "inventory full (heuristic)", "wake_architect": False}
    return {"state": "healthy", "class": "none", "actuator": "none",
            "reason": "no fatal signature (heuristic)", "wake_architect": False}


def classify(cur, prev=None):
    """cur/prev: controller.sense() dicts. Never raises; falls back to the heuristic."""
    try:
        system = SYSTEM
        lb = lessons.prompt_block(tags=("triage", "controller"), k=5)
        if lb:
            system += "\n" + lb
        out = llm.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": json.dumps({"cur": cur, "prev": prev or {}},
                                                    separators=(",", ":"))}],
            model=llm.TRIAGE, max_tokens=220, timeout=60, tag="triage")
        if out and out.get("state") in STATES:
            if out.get("class") not in CLASSES:
                out["class"] = "none"
            if out.get("actuator") not in ACTUATORS:
                out["actuator"] = "none"
            out["wake_architect"] = bool(out.get("wake_architect")) and out["state"] in ("stall", "anomaly")
            out["_source"] = "llm"
            return out
    except Exception as e:
        v = heuristic(cur)
        v["_source"] = "heuristic:" + str(e)[:100]
        return v
    v = heuristic(cur)
    v["_source"] = "heuristic:unparseable"
    return v


if __name__ == "__main__":
    cur = {"engines": 2, "engine_energy": 0, "boiler_fuel": 0, "labs": 1, "labs_working": 0,
           "drills": 20, "drills_blocked": 8, "furnaces": 39, "furnaces_starved": 36,
           "free_slots": 40, "iron_pm": 0}
    print(json.dumps(classify(cur, {"iron_pm": 90}), indent=2))
