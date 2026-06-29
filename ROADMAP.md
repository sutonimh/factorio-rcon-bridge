# Factorio autopilot ROADMAP

Tracks fixes, tweaks, additions, and optimizations. Source: a full code review (2026-06-28) +
Seth's directives. Priority: HIGH (do next) / MED / LOW. Mark `[x]` when done.

## FRESH-MAP AUTONOMY (the goal: load in -> bootstrap -> drive to the Nilaus robotics base)
Decided 2026-06-29 with Seth: POWER via a self-built verified fluid routine (not human-placed);
approach = STAGED PROVING RUN (build early-bootstrap autonomy, start fresh map, watch + fix gaps,
then build the oil->robotics tail). The current patchwork base is THROWAWAY - don't repair it.
PREREQUISITE DONE TODAY: autonomous building works again (the create_entity{player=p} fix - it had
been silently broken for the player-less derpface, so NOTHING could be built; that was the blocker).
Ordered work:
- [ ] **Verified auto fluid-build for power** (offshore pump -> boilers -> engines). NOTE: `power()`
      + `_build_boiler_engine()` ALREADY exist and do this (pump brute-force + pipe-bridging + fluid
      checks) - but their boiler/engine placement uses `A.place`, which the create_entity{player=p}
      bug silently broke, so the historical "4 failed fluid builds" were largely THAT bug. NEXT:
      test `power()` live against the current lake now that placement works; harden the geometry
      (boiler water-row aligned to pump output row; engines chained off the steam side) with a
      get_fluid_count retry/repair loop (pump water==100, boiler steam>0, engine energy>0) until
      verified; scale to N boiler+engine pairs. Likely much closer to working than its reputation.
- [ ] **Top-level `play()` sequencer**: clear_spaceship_debris -> setup_world/scout -> first
      smelting + `build_power_plant` -> red+green science (automated lines) -> research driver.
      The container entry is currently just `scout(); maintain()` (no from-scratch build) - play()
      is what makes a fresh map self-bootstrap. Wire it as the container CMD (run once, then maintain).
- [ ] **Unify servicer coords**: haul_ore / fuel_arrays / science feeders use hardcoded literals
      tied to the retired hand-built base (today's "haul into furnaces that don't exist" bug). Derive
      every servicing coord from what bootstrap actually built (a registry), so a fresh map works.
- [ ] **Oil economy -> blue science -> construction-robotics** (the long pole; see ADDITIONS).
- [ ] **Stamp the Nilaus blueprint + let bots build it** once construction-robotics is reached.
- [ ] PROVE each stage live on the fresh map (staged run); the full bootstrap has never run
      autonomously from zero, especially since the build fix.

## Now / in progress
- [x] FIXED 2026-06-29 base-wide freeze: `inv.remove{count=0}` ("count must be positive") aborted
      `trim_inventory` before it trimmed the copper-cable clog -> derpface inventory free=0 ->
      plates couldn't drain from furnaces -> assemblers/labs starved. Guarded every insert-return
      remove (`if g>0`). See GOTCHAS.
- [ ] **Cap green-chain copper-cable over-production.** The cable assembler ran the inventory to
      8400 cable; `trim_inventory` just DELETES the excess each lap (wasteful, ~4200 copper-plate
      lost). Cap the cable assembler (circuit-limited) or offload excess to a buffer chest instead
      of deleting it.
- [ ] **Redeploy science as a COMPACT GRID** (code fixed: `SCIENCE_COLS` grid in `setup_science_io`).
      The live cells are still a ~100-tile row; re-run `setup_science_io` once the base is stable.
- [x] Scale green science (4 green-pack assemblers + extra inserter/belt).
- [x] Power-keeping: `keep_power()` tops boiler + buffer SERVER-SIDE every fast cycle (no walk).
- [x] `restock_coal` takes ALL coal from the coal-mine chest (fewer trips).
- [x] Coal mine doubled (12 drills, double-sided belt) + electric output inserter on routed power.

## HIGH priority (correctness / power / unblocks)
- [ ] **Power never dies** — verify `keep_power` holds across long builds; add generation scaling
      when the boiler steam buffer drains (only ~1 boiler/2 engines now). Belt/inserter-feed the
      boiler from a topped buffer chest; top the buffer on maintenance runs.
- [ ] **`autopilot.keep_fueled()` drains the boiler buffer into the stock chest** (pulls coal from
      every container incl. boiler-adjacent buffers). Exclude boiler-adjacent chests. (Only bites
      if `autopilot.maintain` is used; `bootstrap.maintain` is authoritative — but fix anyway.)
- [ ] **Two parallel base architectures.** `autopilot.py` servicers (`feed_smelter`, `keep_fueled`,
      `science_factory`, `manage_inventory`, ...) are hardcoded to the retired hand-built base;
      `bootstrap.py` builds a different one. Collapse to ONE; derive servicing coords from
      scouted/`gamedb` state, not literals. (Root cause of most fragility.)
- [ ] **`scout()` can leave `STATE[ore]/STATE["water"]` = None** → `bootstrap()` crashes mid-run.
      Guard, widen radius + regen chunks, fail loudly with which resource is missing.
- [ ] **`setup_science_io` idempotency/teardown over-match** — radius-40 "already built?" probe and
      `y > by+6` teardown can skip the build or tear down the green chain/cluster. Gate on a
      tile-exact marker; scope teardown to outside the new grid bbox only.
- [ ] **Persistent authenticated RCON socket** (`rcon.run` reconnects+auths+0.25s drain PER call).
      One locked socket + shorter drain → big speedup for the walk loop and maintain.

## MED priority
- [ ] Wire belt-based logistics (outpost belt → base) to replace character ore hauling (the
      smelt-at-base shuttle is the slowest part; makes supply offline-proof).
- [ ] Persist `STATE` to JSON (merge with `gamedb`) so a fresh `maintain` process has coords.
- [ ] `_gated()` boiler check uses absolute `<60` but `refill_buffers` uses `0.2*cap` — unify.
- [ ] `coal_buffer()` tuple-unpack can `ValueError` on 'none'; split into a guarded 2-step.
- [ ] Check `A.place()` return everywhere; `build_io_cell` builds chests/inserters even when the
      assembler fails → orphan cells `service_science` feeds forever. Bail/clean up on failure.
- [ ] `dedupe_poles` O(n²) + per-candidate full-base scan + 0.3s settle; batch the scan, compare
      `electric_network_id` instead of `status==58` polling (safer for bridges).
- [ ] RCON large-read truncation (`gamedb.snapshot` returns (0,0) silently on >4KB late packets).
- [ ] Electric mining drills + bigger power, in lockstep (verify pole coverage before swapping).

## LOW priority / cleanup
- [ ] Remove dead/forbidden primitives: `heading()` (unused), `belt_path()` (the snaker GOTCHAS
      bans), `build_outpost()` (furnaces-at-patch, superseded by `build_mine_outpost`).
- [ ] Consolidate 3 overflow/storage zones + 2 snapshot systems + 2 lab feeders into one each.
- [ ] Promote magic numbers (radii, thresholds, sleeps) to named constants.
- [ ] `mine()` can spin on a full inventory; add a free-space guard.
- [ ] Replace silent `except Exception: pass` with logging the maintain loop can surface.

## ADDITIONS — the endgame
- [ ] **Automated oil economy** (the long pole). Scout crude oil; pumpjack to fire the
      `oil-processing` trigger; refinery + chem plants → petroleum/plastic/sulfur → advanced
      circuits → **blue (chemical) science**.
- [ ] Research driver to `construction-robotics`, then STAMP the robot-factory blueprint and let
      bots build it (`stamp_blueprint`/`build_ghosts` + `blueprints/` already exist; wire them).
- [ ] Top-level `play()` that sequences bootstrap → research → oil → blue → robotics → factory.

## Architect (Claude-API strategic layer)
- [x] `architect.py`: rich live snapshot (positions/dir/status/recipe/fuel/power/chests, chunked
      truncation-proof RCON read) → Claude (Opus 4.8, adaptive thinking) → structured report
      (bottlenecks/messes/layout-recs/prioritized-actions), with all GOTCHAS/BUILD-CONVENTIONS
      rules + the coordinate map encoded in the system prompt so every rec stays legal. Snapshot
      (container, no deps) and API call (Mac venv + key) are decoupled via `--snapshot-only` /
      `--from-snapshot` so the key never touches the server.
- ARCHITECT-DERIVED CODE (the standing goal: distill API findings into autopilot code so a fresh
  map drives to robots with the API run only occasionally to find blind spots, never in the loop):
  - [x] `reap_dead_drills()` (in the maintain science strand): surgically refund+remove burner
        drills reading `no_minable_resources` — they produce nothing and litter the map (the
        architect found 19 dead drills @ -46,-12 feeding the iron drought). Touches ONLY
        engine-confirmed-exhausted drills, never working drills or operator base/power/poles.
  - [x] `ensure_ore_supply(ore)` + `relocate_exhausted_outposts()` (wired into the maintain main
        loop, every 12th lap when not gated): re-anchors a mine onto the densest patch when the ore
        UNDER its drills goes thin (per-tile signal, not drill count) and a >=2x-richer patch exists.
        Fixes the iron drought (11 drills on a 425/tile sparse edge while the 1071/tile field sat
        14 tiles away). Healthy patches (copper ~1054/tile) never relocate -> no thrash. Tears down
        the failing outpost (refund) then `build_mine_outpost` on the fresh patch; off-ore drills get
        reaped, so placement self-corrects.
        - [x] FIXED 2026-06-29 thrash: `_ore_under_drills` now measures the on-patch density the same
          way `richest_spot` measures candidates (per-drill 5x5 footprint avg, /25), not the depleting
          single-tile `mining_target.amount`. The old per-tile read (494) under the 500 threshold while
          the true density (532) was above it, so it relocated onto its OWN richest patch every lap.
        - [ ] FOLLOW-UP: `build_mine_outpost`'s radius-22 idempotency makes an edge->dense-core
          relocation WITHIN one patch a no-op (it returns the existing chest). Not biting now (the
          measurement fix stops false triggers), but a genuine same-patch re-center can't rebuild.
          Gate idempotency on a tile-exact drill-row marker, or re-center drills onto the peak.

- AUTO-UPGRADE TO CURRENT TECH (Seth's standing directive; build toward replacing old tier):
  - [ ] FURNACES: a steel-furnace crafter (craft when steel-smelting researched + steel plates
        available) + wire `upgrade_furnaces_to_steel()` into the maintain science strand (it already
        does the in-place stone->steel swap and self-gates). Steel is strictly better -> always upgrade.
  - [ ] ASSEMBLERS: throughput/complexity-gated upgrade a-m-1 -> 2 -> 3. Upgrade a line's assemblers
        only when (a) a recipe needs a higher tier, or (b) that line is the throughput bottleneck.
        Needs a per-line throughput/bottleneck metric; do NOT blanket-upgrade for speed.
  - [ ] Consolidate the sprawled logistic-science assembler line (x=8..103) into one compact
        cluster near the gear/circuit feeders (`SCIENCE_COLS` grid already in `setup_science_io`).
  - [ ] VERIFY the boiler coal-feed inserter (43.5,-2.5) is burner before any power change
        (human/live-supervised; never blind-edit the fluid/power build).
- [ ] Make the architect runnable server-side too (add `anthropic` to the autopilot container +
      an `ANTHROPIC_API_KEY` in its env) so it can run unattended; optionally feed its
      `prioritized_actions` back into a guarded execution pass.
