#!/usr/bin/env python3
"""Architect: use the Claude API to diagnose the base's supply chain + layout and propose
concrete, rules-legal cleanups and redesigns.

The autopilot drives the base tactically (fuel/feed/maintain); the architect is the strategic
layer. It takes a rich live snapshot of every player entity (positions, directions, status,
recipes, fuel, power network, chest contents) and asks Claude (Opus 4.8, adaptive thinking) to:
  - find supply-chain bottlenecks (what's starved, why, where it backs up),
  - find MESSES (orphaned belts/poles, scattered builds, mixed ore lanes, misaligned inserters),
  - propose better layouts + a prioritized action list, every recommendation constrained by the
    hard-won rules in GOTCHAS.md / "BUILD CONVENTIONS" (encoded in RULES below).

The output is structured JSON (a report) so a follow-up pass can act on it; it is also printed
as a human-readable summary and written to architect-report.json.

Usage:
    python3 architect.py                 snapshot -> Claude -> report (needs ANTHROPIC_API_KEY)
    python3 architect.py --snapshot-only dump the live snapshot JSON, no API call (no key needed)
    python3 architect.py --focus "labs"  steer the analysis at one area

Run server-side in the autopilot container (RCON is container-local), or from the Mac with
FACTORIO_RCON_HOST=charon. Needs the `anthropic` package and ANTHROPIC_API_KEY in the env.
"""
import argparse
import json
import os
import pathlib
import sys

import rcon

HERE = pathlib.Path(__file__).resolve().parent
REPORT_PATH = HERE / "architect-report.json"
MODEL = "claude-opus-4-8"
CHUNK = 3000  # chars per RCON read (Factorio truncates large single responses; read in slices)

# --------------------------------------------------------------------------- snapshot
# Build a rich entity snapshot into storage._arch as a JSON string, print its byte length.
# Positions rounded to 0.1; status ints mapped to readable names; type-specific extras attached
# only where meaningful to keep the payload compact.
SNAPSHOT_LUA = r"""
local s=game.surfaces[1]
local SN={} for k,v in pairs(defines.entity_status) do SN[v]=k end
local ents={}
for _,e in pairs(s.find_entities_filtered{force='player'}) do
  local t=e.type local n=e.name
  if n~='character' and n~='character-corpse' then
    local d={n=n,t=t,x=math.floor(e.position.x*10)/10,y=math.floor(e.position.y*10)/10}
    local okd,dir=pcall(function() return e.direction end) if okd and dir and dir~=0 then d.d=dir end
    local oks,st=pcall(function() return e.status end) if oks and st~=nil then d.s=SN[st] or st end
    if t=='assembling-machine' or t=='furnace' then
      local okr,r=pcall(function() return e.get_recipe() end) if okr and r then d.r=r.name end
    end
    local okf,fi=pcall(function() return e.get_fuel_inventory() end)
    if okf and fi then d.coal=fi.get_item_count('coal') end
    if t=='inserter' then
      d.pp=math.floor(e.pickup_position.x)..','..math.floor(e.pickup_position.y)
      d.dp=math.floor(e.drop_position.x)..','..math.floor(e.drop_position.y)
    end
    local oke,eid=pcall(function() return e.electric_network_id end) if oke and eid then d.eid=eid end
    if t=='container' or t=='logistic-container' then
      local inv=e.get_inventory(defines.inventory.chest)
      if inv then local c={} for _,it in pairs(inv.get_contents()) do c[#c+1]=it.name..':'..it.count end
        if #c>0 then d.c=table.concat(c,',') end end
    end
    ents[#ents+1]=d
  end
end
local g={tick=game.tick}
local f=game.forces.player
if f.current_research then g.research=f.current_research.name g.research_pct=math.floor(f.research_progress*100) end
local nets={}
for _,p in pairs(s.find_entities_filtered{type='electric-pole'}) do
  if p.electric_network_id then nets[p.electric_network_id]=true end
end
local nc=0 for _ in pairs(nets) do nc=nc+1 end g.power_networks=nc
local eng=0 for _,e in pairs(s.find_entities_filtered{name='steam-engine'}) do eng=eng+e.energy end
g.engine_energy=math.floor(eng)
storage._arch=helpers.table_to_json({ents=ents,globals=g})
rcon.print(tostring(#storage._arch))
"""


def snapshot():
    """Gather the rich live snapshot. Builds it server-side into storage._arch, then reads it
    back in CHUNK-sized slices (Factorio truncates a single large RCON response, so we paginate)
    -> parsed dict {ents:[...], globals:{...}}."""
    n = int((rcon.run("/sc " + SNAPSHOT_LUA).strip() or "0"))
    if n == 0:
        raise RuntimeError("snapshot build returned 0 length (RCON or Lua error)")
    parts = []
    i = 1
    while i <= n:
        # rcon.print appends a trailing newline to each response; strip it so the slices
        # rejoin into exactly the original JSON (compact JSON has no other trailing whitespace).
        parts.append(rcon.run("/sc rcon.print(storage._arch:sub(%d,%d))" % (i, i + CHUNK - 1)).rstrip("\r\n"))
        i += CHUNK
    raw = "".join(parts)
    return json.loads(raw)


# --------------------------------------------------------------------------- the rules
# Distilled from GOTCHAS.md "TOP LESSONS" + "BUILD CONVENTIONS". Every recommendation Claude
# makes must obey these; they are the difference between a legal build and one that froze the
# base in past sessions.
RULES = """\
HARD RULES for any recommendation (learned the expensive way; violating them stalls the base):

ZONING
- Only mining infrastructure and turrets go on/at ore patches. Smelting, assembly, labs, science,
  storage all go at the BASE (~10,-30). Never put a smelter/assembler on an ore patch.
- Consolidate like buildings into one cluster; never scatter repeated structures.
- >=10 tiles clear around every building. Never build in trees/rocks/cliffs. Cliffs can't be
  mined without explosives, so a site with cliffs must be MOVED, not built on.

POWER (the #1 recurring failure is the grid fragmenting / the plant dying)
- POWER MUST NEVER DIE. The boiler coal-feed inserters MUST stay BURNER (electric there =
  deadlock: if power dips they can't restart the plant). Scale generation (boiler+engine) BEFORE
  adding furnaces/electric inserters, ~1 boiler+engine pair per ~8-10 furnaces of inserter load.
- A healthy grid is ONE electric_network_id (plus tiny dead stubs). Engine buffer ~95% while
  consumers read no_power = a network SPLIT (islanded engine), NOT a generation shortage.
- Never delete a pole that powers "nothing": it is almost always a load-bearing CONNECTOR
  bridging the generator to the base. Removing connectors splits the grid.
- Do NOT swap burner inserters for electric without a VERIFIED powered pole covering them.

SUPPLY / SMELTING
- NEVER mix ores: keep iron and copper ore belts strictly separate (a crossed lane smelts copper
  in iron furnaces). Coal goes on a SEPARATE LANE from ore, never the same lane (jams the belt).
- Every furnace stack needs coal, including the COPPER stack. Keep coal flowing or backed up to
  the mine (= full supply).
- The coal mine's drills are BURNER (need coal to mine coal): it must SELF-FEED (inserters loop
  coal from its own output back into its drills) AND deliver to the base AND keep a stock chest.
  Never leave the coal mine dependent on the character; that death-spirals the whole base.
- Mine outposts = a row of drills onto ONE belt -> inserter -> output chest. No furnaces at
  patches. Drill the DENSEST part of a deposit, never the nearest edge.

BELTS
- Route belts DIRECT up a clear corridor; where they must cross another belt, dip UNDER with an
  underground-belt pair. A convoluted snaking belt that avoids everything is as bad as one
  through a building. A belt must never run through a building or overlap/cross another belt.
- A belt lane must be CONTINUOUS (no gaps) or items stop. At a corner, the corner tile must take
  the NEW direction or items run straight past the turn.
- Belt flow direction must point AT the consumer.
- Don't tap a collinear same-direction belt with an inserter; just connect the belts. Inserters
  are only for crossing OFF a belt into a machine/chest.

ASSEMBLERS / LABS / SCIENCE
- Hand-crafting can't sustain multiple labs; science needs AUTOMATED assembler lines.
- Assemblers use input/output chests + inserters; the software shuffler feeds them from inventory.
- Inserter direction is the PICKUP side and is error-prone: set pickup_position + drop_position
  EXPLICITLY. A belt-side output inserter sits ADJACENT to the chest, not ON the belt.

TEARDOWN / CLEANUP
- NEVER area-destroy to tear down your own build: it deletes existing infrastructure in the box
  too (this wiped a coal line + iron feeder once). Teardown must be SURGICAL: only the exact
  tiles/entities you placed.
- Clean up every mess in the SAME pass: orphaned belts (stray stubs), island/redundant poles,
  abandoned ghosts, superseded furnaces. But pole cleanup must PRESERVE connectivity bridges.

PROCESS
- Don't blind-build multi-entity FLUID builds (pump->boiler->engine pipework) over RCON; they are
  too finicky without eyes on the fluid network. Defer power-plant / pipe builds to a human or
  live supervision. Poles, belts, inserters, and software logic ARE safe to build blind.
- Verify the real cause before proposing a fix (e.g. FULL boiler steam buffer = adequate power;
  DRAINED = deficient). Don't infer "undersized" from one unpowered consumer.
"""

KNOWN_LAYOUT = """\
Known coordinate conventions (interpret the snapshot against these):
- Base (smelting/assembly/labs/storage): around (10,-30).
- Iron smelter stack feeds an iron distribution belt at y=-28 (runs east); copper stack feeds a
  copper belt at y=-40 (runs east). Iron storage chest ~(-1.5,-25.5); copper storage ~(-1.5,-37.5).
- Coal stock chest ~(20.5,-1.5). Character inventory-overflow buffer chests at (16..19,-7).
- Overflow/storage zone ~(-20,-36).
- Mine output chests (drill output): iron ~(17.5,0.5), copper ~(1.5,6.5); coal mine further north.
- Direction encoding is Factorio 2.x 16-way: N=0, E=4, S=8, W=12 (field 'd'; omitted means 0/N).
- status field 's': working / item_ingredient_shortage / no_power / full_output /
  waiting_for_space_in_destination / no_fuel etc. 'coal' = coal units in a burner's fuel slot.
"""

SYSTEM_PROMPT = (
    "You are a Factorio 2.1 (Space Age) base architect. You are handed a live snapshot of a "
    "single bootstrap base driven over RCON by an autopilot. Your job is to diagnose the supply "
    "chain, find layout messes, and propose concrete, rules-legal fixes that the autopilot can "
    "execute. You reason from the snapshot evidence, never from assumptions: cite the specific "
    "entities/positions/statuses that justify each finding. Prefer the smallest, safest change "
    "that unblocks the most. Respect every hard rule; if a desirable change would violate one "
    "(e.g. a fluid build, or electrifying boiler-feed inserters), say so and propose the legal "
    "alternative. Be specific with coordinates and directions so a script can act on your output.\n\n"
    + RULES + "\n\n" + KNOWN_LAYOUT
)

# Structured-output schema for the report (json_schema; additionalProperties:false everywhere).
REPORT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "description": "2-4 sentence health assessment of the base."},
        "bottlenecks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "area": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "evidence": {"type": "string", "description": "Specific entities/positions/statuses."},
                    "root_cause": {"type": "string"},
                },
                "required": ["area", "severity", "evidence", "root_cause"],
            },
        },
        "messes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "description": "orphan-belt / island-pole / scattered / mixed-lane / misaligned-inserter / ..."},
                    "location": {"type": "string", "description": "Coordinates or region."},
                    "recommended_cleanup": {"type": "string", "description": "Surgical fix (exact tiles/entities)."},
                },
                "required": ["kind", "location", "recommended_cleanup"],
            },
        },
        "layout_recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "rationale": {"type": "string"},
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "rules_respected": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "rationale", "steps", "rules_respected"],
            },
        },
        "prioritized_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "rank": {"type": "integer"},
                    "type": {"type": "string", "enum": ["cleanup", "supply", "power", "layout", "verify"]},
                    "action": {"type": "string", "description": "Concrete, scriptable instruction with coordinates."},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["rank", "type", "action", "risk"],
            },
        },
    },
    "required": ["summary", "bottlenecks", "messes", "layout_recommendations", "prioritized_actions"],
}


def analyze(snap, focus=None):
    """Send the snapshot to Claude and return the structured report dict."""
    try:
        import anthropic
    except ImportError:
        sys.exit("error: the `anthropic` package is not installed (pip install anthropic).")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("error: ANTHROPIC_API_KEY is not set in the environment.")

    client = anthropic.Anthropic()
    ask = (
        "Here is the live base snapshot (JSON). Analyze it per your instructions and return the "
        "structured report.\n\n"
    )
    if focus:
        ask += "Focus especially on: %s\n\n" % focus
    ask += "```json\n" + json.dumps(snap, separators=(",", ":")) + "\n```"

    with client.messages.stream(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": REPORT_SCHEMA}},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": ask}],
    ) as stream:
        msg = stream.get_final_message()

    if msg.stop_reason == "refusal":
        sys.exit("error: request refused (%s)" % (getattr(msg, "stop_details", None)))
    text = next((b.text for b in msg.content if b.type == "text"), "")
    report = json.loads(text)
    report["_usage"] = {"input": msg.usage.input_tokens, "output": msg.usage.output_tokens}
    return report


# --------------------------------------------------------------------------- rendering
def render(report):
    """Pretty-print the report for a human reader."""
    out = []
    out.append("=" * 72)
    out.append("ARCHITECT REPORT")
    out.append("=" * 72)
    out.append(report.get("summary", ""))
    out.append("")
    bn = report.get("bottlenecks", [])
    if bn:
        out.append("BOTTLENECKS")
        for b in bn:
            out.append("  [%s] %s" % (b["severity"].upper(), b["area"]))
            out.append("      cause: %s" % b["root_cause"])
            out.append("      evidence: %s" % b["evidence"])
    ms = report.get("messes", [])
    if ms:
        out.append("")
        out.append("MESSES")
        for m in ms:
            out.append("  %s @ %s" % (m["kind"], m["location"]))
            out.append("      -> %s" % m["recommended_cleanup"])
    lr = report.get("layout_recommendations", [])
    if lr:
        out.append("")
        out.append("LAYOUT RECOMMENDATIONS")
        for r in lr:
            out.append("  %s" % r["title"])
            out.append("      why: %s" % r["rationale"])
            for st in r.get("steps", []):
                out.append("      - %s" % st)
    pa = report.get("prioritized_actions", [])
    if pa:
        out.append("")
        out.append("PRIORITIZED ACTIONS")
        for a in sorted(pa, key=lambda x: x.get("rank", 99)):
            out.append("  %2d. [%s/%s-risk] %s" % (a.get("rank", 0), a["type"], a["risk"], a["action"]))
    u = report.get("_usage")
    if u:
        out.append("")
        out.append("(tokens: %d in / %d out)" % (u["input"], u["output"]))
    return "\n".join(out)


# --------------------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description="Claude-powered Factorio base architect.")
    ap.add_argument("--snapshot-only", action="store_true", help="dump the live snapshot JSON, no API call")
    ap.add_argument("--from-snapshot", metavar="FILE", help="analyze a pre-gathered snapshot JSON (skip RCON; lets the API call run somewhere the game/key aren't co-located)")
    ap.add_argument("--focus", help="steer the analysis at one area (e.g. 'green science chain')")
    args = ap.parse_args()

    if args.from_snapshot:
        snap = json.loads(pathlib.Path(args.from_snapshot).read_text())
    else:
        snap = snapshot()
    if args.snapshot_only:
        print(json.dumps(snap, indent=2))
        print("\n# %d entities, globals=%s" % (len(snap.get("ents", [])), snap.get("globals")), file=sys.stderr)
        return

    report = analyze(snap, focus=args.focus)
    REPORT_PATH.write_text(json.dumps(report, indent=2))
    print(render(report))
    print("\n(full report written to %s)" % REPORT_PATH)


if __name__ == "__main__":
    main()
