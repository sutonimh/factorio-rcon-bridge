#!/usr/bin/env python3
"""Lap triage: the fast lane of the learning loop (MEGABASE-V2-DESIGN §6).

Every maintain lap hands a compact state delta to halo's 4B, which classifies it as
healthy / watch / stall(<class>) / anomaly with a one-line reason. Cheap enough to run every
cycle; an escalation wakes the architect early. If halo is unreachable the HEURISTIC fallback
classifies instead — the maintain loop must never block on an LLM.
"""
import json

import lessons
import llm

STATES = ("healthy", "watch", "stall", "anomaly")

SYSTEM = (
    "You triage one Factorio base-state delta per message. States: healthy (all producing), "
    "watch (degrading trend but producing), stall (production stopped; name the class: power / "
    "coal / supply / inventory / research / build), anomaly (numbers that contradict each other "
    "or a state no rule explains). Known cascade signatures: boiler_fuel=0 or engine_energy=0 "
    "= power death; drills=0 or all chests coal=0 = coal death spiral; free_slots=0 = inventory "
    "clog freezing material flow; full_output+ingredient_shortage+missing_science_packs with "
    "power OK = a material-flow break, check character free slots first. Reply with ONLY JSON: "
    '{"state": "...", "class": "..."|null, "reason": "one line", "wake_architect": true|false}'
)


def heuristic(delta):
    """LLM-free fallback: catch the known-fatal signatures."""
    if delta.get("engine_energy", 1) == 0 or delta.get("boiler_fuel", 1) == 0:
        return {"state": "stall", "class": "power", "reason": "engines/boiler dead (heuristic)",
                "wake_architect": True}
    if delta.get("free_slots", 1) == 0:
        return {"state": "stall", "class": "inventory", "reason": "character inventory full (heuristic)",
                "wake_architect": True}
    if delta.get("labs_working", 1) == 0 and delta.get("assemblers_working", 1) == 0:
        return {"state": "stall", "class": "supply", "reason": "nothing producing (heuristic)",
                "wake_architect": True}
    return {"state": "healthy", "class": None, "reason": "no fatal signature (heuristic)",
            "wake_architect": False}


def classify(delta):
    """delta: small dict of lap metrics. Returns the triage verdict dict (never raises)."""
    try:
        system = SYSTEM
        lb = lessons.prompt_block(tags=("triage",), k=5)
        if lb:
            system += "\n" + lb
        out = llm.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": json.dumps(delta, separators=(",", ":"))}],
            model=llm.TRIAGE, max_tokens=200, timeout=60,
        )
        if out and out.get("state") in STATES:
            out.setdefault("class", None)
            out.setdefault("wake_architect", out["state"] in ("stall", "anomaly"))
            out["_source"] = "llm"
            return out
    except Exception as e:  # halo down / timeout: fall through, never block the lap
        err = str(e)[:120]
        v = heuristic(delta)
        v["_source"] = "heuristic:" + err
        return v
    v = heuristic(delta)
    v["_source"] = "heuristic:unparseable"
    return v


if __name__ == "__main__":
    sample = {"engine_energy": 0, "boiler_fuel": 0, "labs_working": 0, "labs": 3,
              "assemblers_working": 0, "drills": 0, "free_slots": 63, "research_pct": 0}
    print(json.dumps(classify(sample), indent=2))
