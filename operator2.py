#!/usr/bin/env python3
"""Operator console: Seth's dashboard prompts -> safe autopilot commands.

The dashboard appends instructions to operator-inbox.jsonl; each maintain lap the planner
calls process_inbox(), which has the LOCAL 35B translate a pending instruction into commands
from a FIXED verb set, validates them, and queues them through bootstrap.BUILD_QUEUE (so the
maintain priority model - gates first - still governs execution). Results are written back to
the inbox row for the dashboard to display.

Prompts are OPERATOR input (trusted user, tailnet-only page), but the verb allowlist keeps
the LLM translation honest: nothing outside the catalog can execute, and every command is
validated (tech names against techdb, items against recipes, coordinates as ints).
"""
import json
import pathlib
import time

import status
import techdb

HERE = pathlib.Path(__file__).resolve().parent
INBOX = HERE / "operator-inbox.jsonl"

CATALOG = """Available commands (JSON array, each one object):
- {"cmd":"research","tech":"<tech-name>"}            queue a technology
- {"cmd":"craft","item":"<item-name>","count":N}     gather materials + craft
- {"cmd":"mine_outpost","ore":"iron-ore|copper-ore|coal|stone","drills":N}   build/expand a mine
- {"cmd":"walk","x":X,"y":Y}                         send derpface somewhere
- {"cmd":"stamp","lib":"<library-name>","x":X,"y":Y} ghost-stamp a blueprint library entry
- {"cmd":"plan_note","text":"..."}                   add a line to the on-screen plan/queue
- {"cmd":"repair","target":"belts|power|all"}        run the self-heal battery (belt gaps, lanes, flow, poles, grid)
- {"cmd":"connect_mine","ore":"iron-ore|copper-ore"} re-lay the ore lane from that mine to its smelter array
- {"cmd":"reject","reason":"..."}                    the instruction cannot be mapped safely"""


def _rows():
    if not INBOX.exists():
        return []
    return [json.loads(x) for x in INBOX.read_text().splitlines() if x.strip()]


def _write(rows):
    tmp = INBOX.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(INBOX)


def _interpret(text):
    import llm
    out = llm.chat_json(
        [{"role": "system", "content":
          "You translate a Factorio base operator's instruction into commands for an RCON "
          "autopilot. Reply with ONLY a JSON array of command objects from this catalog - "
          "nothing else. Use reject when the instruction doesn't map.\n" + CATALOG},
         {"role": "user", "content": text}],
        model=llm.ARCHITECT, max_tokens=600, tag="operator")
    return out if isinstance(out, list) else ([out] if isinstance(out, dict) else None)


def _validate(c):
    """Returns (callable_description, thunk) or raises ValueError."""
    import autopilot as A
    import bootstrap as B
    cmd = c.get("cmd")
    if cmd == "research":
        tech = c.get("tech", "")
        if not techdb.tech(tech):
            raise ValueError(f"unknown tech {tech!r}")
        return f"research {tech}", lambda: A._print(
            f"/sc rcon.print(game.forces.player.add_research('{tech}'))")
    if cmd == "craft":
        item, n = c.get("item", ""), min(int(c.get("count", 1)), 200)
        if n < 1:
            raise ValueError("count must be >= 1")
        t = techdb.unlocking_tech(item)
        if t and not B._tech_done(t):
            raise ValueError(f"{item} is gated behind unresearched {t}")
        return f"craft {item} x{n}", lambda: B.make(item, n)
    if cmd == "mine_outpost":
        ore, n = c.get("ore"), min(int(c.get("drills", 6)), 20)
        if ore not in ("iron-ore", "copper-ore", "coal", "stone"):
            raise ValueError(f"bad ore {ore!r}")
        import builds_v2
        return f"mine_outpost {ore} x{n}", lambda: builds_v2.mine_outpost_v2(ore, n)
    if cmd == "walk":
        x, y = int(c.get("x", 0)), int(c.get("y", 0))
        if abs(x) > 2000 or abs(y) > 2000:
            raise ValueError("coordinates out of range")
        return f"walk to {x},{y}", lambda: (A.stop(), A.walk(x, y, tol=3.0))
    if cmd == "stamp":
        import bplib
        import modules
        lib, x, y = c.get("lib", ""), int(c.get("x", 0)), int(c.get("y", 0))
        bplib.load(lib)                      # raises if unknown
        s = modules.child_string(lib)
        return f"stamp {lib} @{x},{y}", lambda: modules.stamp_at(s, x, y)
    if cmd == "plan_note":
        import autopilot as A2
        txt = str(c.get("text", ""))[:80]
        return f"note: {txt}", lambda: A2.now(txt)
    if cmd == "repair":
        target = c.get("target", "all")
        def _repair():
            if target in ("belts", "all"):
                B.scrub_mixed_ore(); B.repair_belt_gaps(); B.ensure_lanes()
            if target in ("power", "all"):
                B.keep_power(); B.fix_unpowered(); B.ensure_grid_connected()
        return f"repair {target}", _repair
    if cmd == "connect_mine":
        ore = c.get("ore")
        if ore not in ("iron-ore", "copper-ore"):
            raise ValueError(f"bad ore {ore!r}")
        return f"connect_mine {ore}", lambda: B.connect_mine_to_array(ore)
    if cmd == "reject":
        raise ValueError(c.get("reason", "rejected"))
    raise ValueError(f"unknown command {cmd!r}")


def validate_commands(cmds):
    """Shared actuator pipeline (audit item 2): validate a command list, queue the valid ones
    on bootstrap.BUILD_QUEUE. Returns (accepted_descs, rejected_strs). Used by operator
    prompts AND architect prioritized_actions - one catalog, one gate."""
    import bootstrap as B
    accepted, rejected = [], []
    for c in cmds or []:
        try:
            desc, thunk = _validate(c)
            thunk.__name__ = f"cmd:{desc}"
            B.BUILD_QUEUE.append(thunk)
            accepted.append(desc)
        except Exception as e:
            rejected.append(f"{(c or {}).get('cmd', '?')}: {str(e)[:100]}")
    return accepted, rejected


def process_inbox():
    """Called from the planner lap hook. Interprets ONE pending prompt per call (LLM budget),
    queues its commands onto bootstrap.BUILD_QUEUE, writes status back. Never raises."""
    try:
        rows = _rows()
        pend = next((r for r in rows if r.get("status") == "pending"), None)
        if not pend:
            return
        pend["status"] = "interpreting"
        _write(rows)
        cmds = _interpret(pend["text"])
        if not cmds:
            pend["status"] = "failed"
            pend["result"] = "could not interpret (LLM returned no commands)"
            _write(rows)
            return
        accepted, rejected = validate_commands(cmds)
        pend["status"] = "queued" if accepted else "failed"
        pend["result"] = "; ".join(accepted + (["REJECTED: " + "; ".join(rejected)] if rejected else []))[:400]
        _write(rows)
        status.log(f"operator prompt -> {pend['result']}")
    except Exception as e:
        status.log(f"operator inbox error: {e}")
