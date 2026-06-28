# Next build: bus-fed 8+ lab science array (handoff for a fresh session)

## Goal
At least 8 labs, ALL auto-fed red+green science, off a **main copper + iron plate bus**.
Current bottleneck: only ~1 lab is auto-fed; production (1 green asm + 1 red asm) can't
feed more. The fix is scaled bus-fed production, not just more labs.

## Suitable blueprint (already on disk)
`blueprints/nilaus/red-green-science.txt` (Nilaus Master Class) decodes to:
- **"5. Red/Green Science"** (290 ents): asm-2 module, iron+copper plates in off a bus →
  red+green science out. 27x assembling-machine-2, 172 belts, 44 inserter + 14 fast-inserter.
- **"5. Science Labs"** (25 ents): 5 labs + underground-belt science distribution. Tile/
  extend to >=8 labs.

### Tech substitutions needed (these are NOT researched yet)
- `fast-inserter` -> `inserter`
- `fast-underground-belt` -> `underground-belt`
- `small-lamp` -> drop (decorative)
Researched + fine as-is: assembling-machine-2, lab, underground-belt, long-handed-inserter,
inserter, transport-belt, small/medium electric poles.

## Plan (3 parts)
1. **Main bus**: 2 iron-plate + 2 copper-plate belt lines, fed from the smelter stack
   outputs (iron furnaces y=-30 plate belt, copper furnaces y=-42 plate belt). Run the bus
   to a clear plot.
2. **Production**: place the adapted Red/Green Science module tapping the bus (this removes
   the bottleneck). Build as REAL entities (we have no construction bots, so build_blueprint
   ghosts won't auto-build) - parse the BP json, create_entity each with adapted name +
   recipe + direction, OR stamp ghosts then revive them.
3. **Lab array**: >=8 labs in a row, fed red+green from the module output via the
   underground-belt science distribution (long-handed inserters feed the labs).

## Apply these lessons (see GOTCHAS.md "TOP LESSONS")
- Belts: route DIRECT corridors; cross existing belts with UNDERGROUND belts (don't A*-snake
  around them). **`build_belt()` in autopilot.py currently snakes - REWORK IT FIRST** to:
  treat only non-belt buildings as hard obstacles, cross belts via underground pairs.
- Teardown SURGICAL only (track placed tiles); NEVER area-destroy (it wiped the coal line).
- `goto(cx,cy)` to walk to every build site before building (Seth watches).
- Re-`snapshot()` after intentional changes so `rebuild()` (manual only now) doesn't revert them.

## Current state (2026-06-27 session end)
- Research: past automation-2/engine; on the oil chain (fluid-handling -> oil -> ... ->
  advanced-material-processing-2 = electric furnace). queued via research_queue.
- Power: 15 medium poles, optimized, 1 network, all consumers powered. Plant adequate.
- Iron mine->smelter belt: Seth hand-built it clean (direct + undergrounds). Keep it.
- Patrol (`patrol.py`): fuels all burners, crafts belts+packs, feeds labs, cleans orphan
  infra, renders the live GUI note (`tasks.py`). RESTART IT FIRST in the fresh session:
  `cd ~/code/factorio && python3 patrol.py` (run in background).

## Tooling
autopilot.py (RCON helpers), patrol.py (maintenance loop), tasks.py/tasks.json (GUI note),
remove_redundant.py + optimize_poles.py (pole layout), rcon.py (client). All on GitHub
(sutonimh/factorio-rcon-bridge).
