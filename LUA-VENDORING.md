# LUA-VENDORING — FLE tool layer vendored into this repo

## What / where from

`lua/fle_lib.lua` + `fle_tools.py` vendor the server-side Lua tooling of the
**Factorio Learning Environment** (FLE), MIT licensed:

- Upstream: https://github.com/JackHopkins/factorio-learning-environment
- Commit vendored: `f748ec452dfa79f6a57a12ddcff1ff9102cdb11f` (shallow clone, 2026-08-29)
- Source files drawn from:
  - `fle/env/tools/agent/connect_entities/server.lua` — belt/pipe/pole auto-routing
    (wire-reach tables, underground ranges/segmenting, placeability, pole network +
    saturation checks, belt-BFS connectivity)
  - `fle/env/tools/agent/nearest_buildable/server.lua` — spiral search for the nearest
    origin where an entity fits (incl. full-resource-coverage for drills, crude-oil for
    pumpjacks)
  - `fle/env/mods/utils.lua`, `fle/env/mods/serialize.lua` — direction helpers
    (`get_direction`, `get_entity_direction` semantics: belts/undergrounds face the
    travel direction; a pipe-to-ground entrance faces BACK along travel, the exit faces
    forward)
- `place_entity_next_to/server.lua` was studied but NOT vendored (its smart-position
  scoring is deeply tied to FLE's agent-character model; our `autopilot.place` +
  `drop_position` conventions already cover the need).

## How init / reload works

FLE loads its Lua by streaming whole `server.lua` files over RCON into function tables
inside `storage` (`storage.actions.*`, `storage.utils.*`). We keep the streaming idea
but change two things:

1. **Chunked load.** `fle_tools.init()` splits `lua/fle_lib.lua` on `-- @chunk <name>`
   markers and sends each section as ONE `/sc` command (each must stay < 3.5KB — the
   RCON command size limit, per GOTCHAS "RCON client protocol"). Each chunk is
   self-contained (`local F = fle` re-derived per chunk) because separate `/sc` calls
   share no locals. Multi-line commands with comments verified working over our client.
2. **Functions live in the plain Lua global `fle`, NOT in `storage`.** Factorio cannot
   serialize functions in `storage` — FLE gets away with it only because its scenarios
   never save normally; on our live server it would break every autosave. The global
   survives across `/sc` calls for the life of the server process and is lost on
   restart/save-reload; every `fle_tools` wrapper call probes `fle.VERSION` and
   re-pushes automatically (`_ensure()`), so reload is transparent. Bump `F.VERSION`
   in `fle_lib.lua` whenever the Lua changes so live servers pick it up.

Results return via the architect.py chunked-read pattern: `fle.out(t)` stores compact
JSON in `storage.fle_out` (a string — serializable, the only storage use) and prints
its length; Python reads it back in 3000-char `:sub()` slices, `.rstrip()`ing each
(rcon.print appends a newline per response).

## Adaptations (upstream → here), and why

- **No agent character / player.** FLE routes everything through
  `storage.agent_characters[player_index]` (teleports it out of the way, deducts items
  from its inventory, uses `player.force`). Removed entirely: placement is
  `surface.create_entity` WITHOUT `player=` (GOTCHAS: passing a character entity as
  `player=` errors and silently broke all builds), force is `game.forces.player`, no
  inventory deduction (same model as `bootstrap.lay_belt_path`), no teleports.
- **Pathfinding replaced.** FLE feeds `connect_entities` an async
  `surface.request_path` result (`request_path`/`get_path` tools + an
  `on_script_path_request_finished` handler registered at scenario load). A pure
  `/sc` model has no load-time event registration, and GOTCHAS explicitly wants
  direct routes ("Route belts DIRECT + cross with undergrounds"), so `F.direct_path`
  builds the two L-path candidates (x-first / y-first) and picks the one crossing
  fewer hard obstacles. FLE's `normalise_path`/`interpolate_manhattan` machinery
  became unnecessary with exact integer tile paths.
- **Belt policy per GOTCHAS.** FLE's `is_placeable` treated every colliding entity as
  a hard obstacle (which is what made their belts detour); our `F.classify` returns
  free / clearable (trees+rocks, auto-cleared) / belt / hard. Existing belts are never
  overwritten or detoured around: a collinear same-name same-direction belt is adopted,
  anything else is dipped under with an underground pair on the straight run; buildings
  are always hard. Per-tile direction points at the NEXT tile so corners turn (the
  `lay_belt_path` corner lesson). Blocked spans that can't be bridged (corner inside
  the span, span > range, path end) are counted in `gaps`, never routed around.
- **Underground ranges kept from FLE** (belt 4, pipe 8) — conservative vs. the real
  2.1.17 prototype limits (verified live: `max_underground_distance` = 5 / 10).
- **Pole logic vendored near-verbatim** (wire-reach table, network-at-position lookup,
  4-corner saturation skip, step-by-wire-reach with early stop on shared
  `electric_network_id`, `find_non_colliding_position` nudging). Dropped: the
  serialize-the-whole-pole-group result (we return placed positions), rendering draws.
- **`nearest_buildable`:** center position is a required argument (no character
  fallback); the bounding box is derived in Lua from `prototypes.entity[name]
  .collision_box` instead of being passed from Python; the chunk-resource cache became
  a cheap total-count early-exit + per-tile verify; added a final `can_place_entity`
  guard (default build check — GOTCHAS: never `build_check_type.manual`); returns
  `{found=false, error=...}` instead of `error()`.
- **Result surface:** FLE serialized full entity dumps via `storage.utils
  .serialize_entity`; we return `{placed, gaps, connected, entities=[{name,x,y,d}]}`.

## Factorio 2.1 compatibility (FLE targets 2.0.x; our server is 2.1.17)

Checked every vendored path against 2.1-removed APIs, with live read-only probes:

- **`LuaEntity.fluidbox` / `LuaFluidBox` are GONE.** FLE verifies pipe connections via
  `fluidbox.get_fluid_segment_id` (`are_fluidboxes_connected`,
  `are_positions_pipe_connected`, `serialize_pipe_group`) — all impossible on 2.1
  (probe: accessing `.fluidbox` throws). Adaptation: pipe `connected` means the lay
  was contiguous (`gaps == 0`); real flow verification is `fle.fluid_probe(x, y,
  fluid)` → `entity.get_fluid_count()`, which still works (verified live, returns
  100 on a drawing pipe; settle 2-3s before trusting a 0, per GOTCHAS).
- **`LuaEntity.neighbours` is GONE** (probe: "LuaEntity doesn't contain key
  neighbours") and `belt_neighbours` does NOT include an underground partner (probe:
  an entrance lists only its surface input). FLE's belt BFS would therefore stop at
  every underground dip. Adaptation: `F.ug_partner` finds the partner geometrically —
  scan along the travel axis up to `max_underground_distance` for the matching
  entrance/exit with the same name+direction.
- **`game.active_mods` is GONE in 2.1** — FLE's Lua never uses it (grepped); nothing
  vendored touches it.
- Confirmed present on 2.1.17 (live probes): `prototypes.entity[...]`
  (+ `max_underground_distance`, `resource_categories`, `collision_box`),
  `helpers.table_to_json`, `count_tiles_filtered`, `count_entities_filtered`,
  `find_non_colliding_position`, `electric_network_id`, `belt_neighbours`,
  `belt_to_ground_type`, `get_fluid_count`.
- FLE's own "Factorio 2.0" comments (16-direction constants, collision-mask formats,
  `get_contents` list shape) match what this repo already codified in GOTCHAS.

## Validation done / known risks

Validated without writing to the live game:

- `luac -p lua/fle_lib.lua` — syntax clean.
- `python3 fle_tools.py selftest` — chunk split dry-run: 8 chunks, all < 3.5KB,
  splitter error paths exercised. No RCON traffic.
- Read-only RCON probes (above) for every API name the vendored code calls.

NOT yet exercised (live writes were out of scope for the vendoring pass):

- No belt/pipe/pole has actually been laid via `fle.connect` yet — first live use
  should be a `dry_run=True` pass, then a short belt in a clear area, checking
  `connected` and the `entities` list.
- The `init()` push + `storage.fle_out` chunked read-back path (multi-line `/sc` and
  the slice-read pattern are proven individually, but not this exact pipeline).
- Underground bridging placement (entrance/exit direction + `type='input'/'output'`
  semantics match `lay_belt_path` and FLE, but are untested here), pipe-to-ground
  entrance-faces-back orientation, and pole saturation skipping.
- `nearest_buildable` resource coverage on a real patch.
