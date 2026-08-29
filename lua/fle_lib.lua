-- fle_lib.lua — belt/pipe/pole auto-routing + nearest_buildable, vendored from the
-- Factorio Learning Environment (FLE).
--
-- Upstream: https://github.com/JackHopkins/factorio-learning-environment  (MIT License)
-- Commit:   f748ec452dfa79f6a57a12ddcff1ff9102cdb11f  (shallow clone, 2026-08-29)
-- Files:    fle/env/tools/agent/connect_entities/server.lua
--           fle/env/tools/agent/nearest_buildable/server.lua
--           fle/env/mods/utils.lua + fle/env/mods/serialize.lua (direction helpers)
--
-- MIT License (c) Jack Hopkins et al. — see the upstream repo's LICENSE.
-- Adapted for this repo; every adaptation is listed in LUA-VENDORING.md (the big
-- ones: Factorio 2.1 fluidbox removal, no agent character, functions kept OUT of
-- storage so saves don't break, direct-route policy per GOTCHAS.md).
--
-- LOAD MODEL: fle_tools.py init() sends each `-- @chunk <name>` section below as
-- one /sc command (each section must stay under ~3.5KB — the RCON command limit).
-- Functions land in the plain Lua global `fle`, NOT in storage: Factorio cannot
-- serialize functions in storage, so storing them there would break every save
-- and autosave. The global survives across /sc calls for the life of the server
-- process; fle_tools re-pushes automatically when its version probe says the
-- global is missing (server restart / save reload). The only storage use is
-- storage.fle_out — a JSON *string* (serializable) used for chunked reads.

-- @chunk core
fle = fle or {}
local F = fle
F.VERSION = 1
-- vendored tables from connect_entities/server.lua
F.wire_reach = {['small-electric-pole'] = 4, ['medium-electric-pole'] = 9,
                ['big-electric-pole'] = 30, ['substation'] = 18}
F.underground_range = {['pipe-to-ground'] = 8, ['underground-belt'] = 4,
                       ['fast-underground-belt'] = 4, ['express-underground-belt'] = 4}
F.underground_for = {['transport-belt'] = 'underground-belt',
                     ['fast-transport-belt'] = 'fast-underground-belt',
                     ['express-transport-belt'] = 'express-underground-belt',
                     ['pipe'] = 'pipe-to-ground'}
F.water_tiles = {['water'] = true, ['deepwater'] = true, ['water-green'] = true,
                 ['deepwater-green'] = true, ['water-shallow'] = true, ['water-mud'] = true}
-- chunked-read output: store JSON in storage (strings serialize fine), print the
-- length; fle_tools reads it back in slices (GOTCHAS: >4KB RCON responses truncate)
F.out = function(t)
  storage.fle_out = helpers.table_to_json(t)
  rcon.print(#storage.fle_out)
end

-- @chunk placeable
local F = fle
-- Tile classification, adapted from FLE is_placeable() (connect_entities/server.lua).
-- FLE returned a boolean; we classify instead so existing belts can be crossed with
-- undergrounds rather than detoured around (GOTCHAS: route belts DIRECT, dip under
-- existing belts, never route through buildings).
F.classify = function(x, y)
  local s = game.surfaces[1]
  if F.water_tiles[s.get_tile(x, y).name] then return 'hard' end
  local has_belt, has_pipe, has_clear = false, false, false
  for _, e in pairs(s.find_entities_filtered{area = {{x + 0.05, y + 0.05}, {x + 0.95, y + 0.95}}}) do
    local t = e.type
    if t == 'transport-belt' or t == 'underground-belt' or t == 'splitter' then
      has_belt = true
    elseif t == 'pipe' or t == 'pipe-to-ground' then
      has_pipe = true
    elseif t == 'tree' or t == 'simple-entity' then
      has_clear = true
    elseif t == 'resource' or t == 'character' or t == 'item-entity' or t == 'corpse'
        or t == 'beam' or t == 'entity-ghost' then
      -- never blocks a build
    else
      return 'hard'   -- a real building (or cliff): never build through it
    end
  end
  if has_belt then return 'belt' end
  if has_pipe then return 'pipe' end
  if has_clear then return 'clearable' end
  return 'free'
end
-- clear trees/rocks on a tile before placing (same as lay_belt_path's freebelt)
F.clear_tile = function(x, y)
  local s = game.surfaces[1]
  for _, e in pairs(s.find_entities_filtered{area = {{x + 0.05, y + 0.05}, {x + 0.95, y + 0.95}},
                                             type = {'tree', 'simple-entity'}}) do
    if e.destroy then e.destroy() end
  end
end

-- @chunk path
local F = fle
-- Direct L-path candidates (x-first / y-first); pick the one crossing fewer hard
-- tiles. This REPLACES FLE's async surface.request_path + normalise_path pipeline:
-- request_path needs an on_script_path_request_finished handler registered at
-- scenario load time, which a pure /sc model doesn't have — and GOTCHAS explicitly
-- prefers a short direct corridor with underground crossings over pathfinder snakes.
-- Coordinates are integer TILE coords (top-left corner); centers are +0.5.
F.lpath = function(ax, ay, bx, by, xfirst)
  local pts = {}
  local x, y = ax, ay
  local sx = (bx > ax) and 1 or -1
  local sy = (by > ay) and 1 or -1
  if xfirst then
    while x ~= bx do pts[#pts + 1] = {x = x, y = y}; x = x + sx end
    while y ~= by do pts[#pts + 1] = {x = x, y = y}; y = y + sy end
  else
    while y ~= by do pts[#pts + 1] = {x = x, y = y}; y = y + sy end
    while x ~= bx do pts[#pts + 1] = {x = x, y = y}; x = x + sx end
  end
  pts[#pts + 1] = {x = bx, y = by}
  return pts
end
F.direct_path = function(ax, ay, bx, by)
  local a = F.lpath(ax, ay, bx, by, true)
  local function hard_count(p)
    local h = 0
    for _, q in ipairs(p) do if F.classify(q.x, q.y) == 'hard' then h = h + 1 end end
    return h
  end
  local ha = hard_count(a)
  if ha == 0 then return a, 0 end
  local b = F.lpath(ax, ay, bx, by, false)
  local hb = hard_count(b)
  if hb < ha then return b, hb end
  return a, ha
end

-- @chunk lay
local F = fle
-- Belt/pipe line layer, adapted from FLE connect_entities/server.lua
-- (place_at_position + the underground-segment splitting logic). Differences: the
-- tile model comes from bootstrap.lay_belt_path — each tile's direction points at
-- the NEXT tile so corners turn; undergrounds bridge blocked spans on straight
-- runs; create_entity WITHOUT player= (GOTCHAS: player=<character> errors); no
-- inventory deduction; no character teleports.
F.lay_line = function(ax, ay, bx, by, name, dry)
  local s = game.surfaces[1]
  local f = game.forces.player
  local ug = F.underground_for[name]
  local range = F.underground_range[ug] or 4
  local is_pipe = (name == 'pipe')
  local path, hard = F.direct_path(ax, ay, bx, by)
  local placed, gaps, ents = 0, 0, {}
  local dirs = {}
  for i = 1, #path - 1 do
    local dx = path[i + 1].x - path[i].x
    local dy = path[i + 1].y - path[i].y
    dirs[i] = (dy < 0 and 0) or (dx > 0 and 4) or (dy > 0 and 8) or 12
  end
  dirs[#path] = dirs[#path - 1] or 0
  local function state(i)
    local c = F.classify(path[i].x, path[i].y)
    if c == 'belt' then
      if not is_pipe then
        local e = s.find_entities_filtered{area = {{path[i].x + 0.05, path[i].y + 0.05},
          {path[i].x + 0.95, path[i].y + 0.95}}, name = name}[1]
        if e and e.direction == dirs[i] then return 'adopt' end
      end
      return 'blocked'   -- someone else's lane: dip under it, never overwrite
    end
    if c == 'pipe' then
      if is_pipe then return 'adopt' end   -- pipes join regardless of direction
      return 'blocked'                     -- a belt dips under an existing pipe run
    end
    if c == 'hard' then return 'blocked' end
    return 'ok'
  end
  local function put(i, ename, d, utype)
    local x, y = path[i].x, path[i].y
    if dry then placed = placed + 1; return true end
    F.clear_tile(x, y)
    local old = s.find_entity(name, {x + 0.5, y + 0.5})
    if old then old.destroy() end
    local e = s.create_entity{name = ename, position = {x + 0.5, y + 0.5},
                              direction = d, force = f, type = utype}
    if e then
      placed = placed + 1
      ents[#ents + 1] = {name = ename, x = x, y = y, d = d}
      return true
    end
    return false
  end
  local i = 1
  while i <= #path do
    local st = state(i)
    if st == 'adopt' then
      ents[#ents + 1] = {name = name, x = path[i].x, y = path[i].y, d = dirs[i], existing = true}
      i = i + 1
    elseif st == 'ok' then
      if not put(i, name, dirs[i]) then gaps = gaps + 1 end
      i = i + 1
    else
      local j = i + 1
      while j <= #path and state(j) == 'blocked' do j = j + 1 end
      local pi = i - 1
      local straight = (pi >= 1) and (j <= #path)
      if straight then
        for k = pi, j - 1 do if dirs[k] ~= dirs[pi] then straight = false end end
      end
      if straight and ug and (j - pi) <= range + 1 then
        local d = dirs[pi]
        local ed = is_pipe and (d + 8) % 16 or d   -- pipe-to-ground entrance faces BACK along travel
        put(pi, ug, ed, (not is_pipe) and 'input' or nil)   -- replaces the surface piece at pi
        if not put(j, ug, d, (not is_pipe) and 'output' or nil) then gaps = gaps + 1 end
        i = j + 1
      else
        gaps = gaps + (j - i)
        i = j
      end
    end
  end
  return {placed = placed, gaps = gaps, hard = hard, entities = ents,
          a = {x = ax, y = ay}, b = {x = bx, y = by}}
end

-- @chunk beltcheck
local F = fle
-- Vendored from FLE are_positions_belt_connected(): BFS over belt_neighbours.
-- 2.1 ADAPTATION: FLE hopped underground segments via LuaEntity.neighbours, but
-- on 2.1.17 that key is GONE ("LuaEntity doesn't contain key neighbours") and
-- belt_neighbours does NOT include the underground partner (both verified live),
-- so the partner is found geometrically: scan along the travel axis for the
-- matching entrance/exit within the prototype's max_underground_distance.
F.ug_partner = function(b)
  local s = game.surfaces[1]
  local d = b.direction
  local vx = (d == 4 and 1) or (d == 12 and -1) or 0
  local vy = (d == 8 and 1) or (d == 0 and -1) or 0
  if b.belt_to_ground_type == 'output' then vx, vy = -vx, -vy end
  local maxd = prototypes.entity[b.name].max_underground_distance or 5
  local want = (b.belt_to_ground_type == 'input') and 'output' or 'input'
  for k = 1, maxd do
    local e = s.find_entity(b.name, {b.position.x + vx * k, b.position.y + vy * k})
    if e and e.belt_to_ground_type == want and e.direction == d then return e end
  end
  return nil
end
F.belt_connected = function(ax, ay, bx, by)
  local s = game.surfaces[1]
  local start = s.find_entities_filtered{position = {ax + 0.5, ay + 0.5}, radius = 0.4,
    type = {'transport-belt', 'underground-belt', 'splitter'}}[1]
  if not start then return false end
  local seen, queue, n = {}, {start}, 0
  while #queue > 0 and n < 500 do
    n = n + 1
    local b = table.remove(queue, 1)
    if b and b.valid and not seen[b.unit_number] then
      seen[b.unit_number] = true
      if math.abs(b.position.x - (bx + 0.5)) < 0.6 and math.abs(b.position.y - (by + 0.5)) < 0.6 then
        return true
      end
      local nb = b.belt_neighbours
      if nb then
        for _, set in pairs({nb.outputs or {}, nb.inputs or {}}) do
          for _, o in pairs(set) do
            if o and o.valid and not seen[o.unit_number] then queue[#queue + 1] = o end
          end
        end
      end
      if b.type == 'underground-belt' then
        local partner = F.ug_partner(b)
        if partner and partner.valid and not seen[partner.unit_number] then
          queue[#queue + 1] = partner
        end
      end
    end
  end
  return false
end

-- @chunk poles
local F = fle
-- Vendored from FLE connect_entities/server.lua pole logic: wire-reach network
-- lookup (get_electric_network_at_position), supply-saturation skip
-- (is_position_saturated), step-by-wire-reach placement along the line with an
-- early stop once both ends share an electric_network_id (works fine on 2.1).
F.pole_net_at = function(px, py)
  local s = game.surfaces[1]
  for _, p in pairs(s.find_entities_filtered{position = {px, py}, radius = 9, type = 'electric-pole'}) do
    local r = F.wire_reach[p.name] or 4
    local dx, dy = px - p.position.x, py - p.position.y
    if dx * dx + dy * dy <= r * r then return p.electric_network_id end
  end
  return nil
end
F.pole_saturated = function(px, py, reach)
  local s = game.surfaces[1]
  local poles = s.find_entities_filtered{position = {px, py}, radius = 9, type = 'electric-pole'}
  local corners = {{px - reach / 2, py - reach / 2}, {px - reach / 2, py + reach / 2},
                   {px + reach / 2, py + reach / 2}, {px + reach / 2, py - reach / 2}}
  for _, c in pairs(corners) do
    local covered = false
    for _, p in pairs(poles) do
      local r = F.wire_reach[p.name] or 4
      local dx, dy = c[1] - p.position.x, c[2] - p.position.y
      if dx * dx + dy * dy <= r * r then covered = true; break end
    end
    if not covered then return false end
  end
  return true
end
F.connect_poles = function(ax, ay, bx, by, name, dry)
  local s = game.surfaces[1]
  local f = game.forces.player
  local reach = F.wire_reach[name] or 4
  local dx, dy = bx - ax, by - ay
  local dist = math.sqrt(dx * dx + dy * dy)
  local n = math.max(1, math.ceil(dist / reach))
  local placed, ents = 0, {}
  local function connected()
    local na = F.pole_net_at(ax, ay)
    return na ~= nil and na == F.pole_net_at(bx, by)
  end
  for k = 0, n do
    if not dry and connected() then break end
    local px, py = ax + dx * k / n, ay + dy * k / n
    if not F.pole_saturated(px, py, reach) then
      local pp = s.find_non_colliding_position(name, {px, py}, 3, 0.5)
      if pp then
        if dry then
          placed = placed + 1
        else
          local e = s.create_entity{name = name, position = pp, force = f}
          if e then
            placed = placed + 1
            ents[#ents + 1] = {name = name, x = pp.x, y = pp.y}
          end
        end
      end
    end
  end
  local conn = false
  if not dry then conn = connected() end   -- dry runs always report connected=false
  return {placed = placed, gaps = 0, entities = ents, connected = conn,
          a = {x = ax, y = ay}, b = {x = bx, y = by}}
end

-- @chunk nearest
local F = fle
-- Vendored from FLE nearest_buildable/server.lua (spiral search + full resource
-- coverage for drills, crude-oil presence for pumpjacks). Adaptations: no agent
-- character (the center is a required argument), the bounding box is derived from
-- the prototype collision_box here instead of being passed in from Python, the
-- chunk-resource cache became a cheap count-first + per-tile verify, a final
-- can_place_entity guard was added, and it returns a table instead of error()ing.
F.nearest_buildable = function(entity_name, near_x, near_y, max_radius)
  local s = game.surfaces[1]
  local proto = prototypes.entity[entity_name]
  if not proto then return {found = false, error = 'unknown entity ' .. tostring(entity_name)} end
  local needs_resources = proto.resource_categories ~= nil
  local needs_oil = (entity_name == 'pumpjack')
  local box = proto.collision_box
  local MAXR = max_radius or 30
  local water = {'water', 'deepwater', 'water-green', 'deepwater-green', 'water-shallow', 'water-mud'}
  local function buildable_at(pos)
    local lt = {x = pos.x + box.left_top.x, y = pos.y + box.left_top.y}
    local rb = {x = pos.x + box.right_bottom.x, y = pos.y + box.right_bottom.y}
    if s.count_tiles_filtered{area = {lt, rb}, name = water} > 0 then return false end
    if s.count_entities_filtered{area = {lt, rb}, type = {'character', 'resource'}, invert = true} > 0 then
      return false
    end
    if needs_resources then
      if needs_oil then
        if s.count_entities_filtered{area = {lt, rb}, name = 'crude-oil'} == 0 then return false end
      else
        local minx, miny = math.floor(lt.x), math.floor(lt.y)
        local maxx, maxy = math.ceil(rb.x) - 1, math.ceil(rb.y) - 1
        local need = (maxx - minx + 1) * (maxy - miny + 1)
        if s.count_entities_filtered{area = {lt, rb}, type = 'resource'} < need then return false end
        for x = minx, maxx do
          for y = miny, maxy do
            if s.count_entities_filtered{area = {{x, y}, {x + 1, y + 1}}, type = 'resource'} == 0 then
              return false
            end
          end
        end
      end
    end
    if not s.can_place_entity{name = entity_name, position = pos, force = game.forces.player} then
      return false
    end
    return true, lt, rb
  end
  local dx, dy = 0, 0
  local seg_len, seg_passed, dir = 1, 0, 0
  while math.max(math.abs(dx), math.abs(dy)) <= MAXR do
    local pos = {x = near_x + dx, y = near_y + dy}
    local ok, lt, rb = buildable_at(pos)
    if ok then
      return {found = true, x = pos.x, y = pos.y, left_top = lt, right_bottom = rb}
    end
    seg_passed = seg_passed + 1
    if dir == 0 then dx = dx + 1
    elseif dir == 1 then dy = dy + 1
    elseif dir == 2 then dx = dx - 1
    else dy = dy - 1 end
    if seg_passed == seg_len then
      seg_passed = 0
      dir = (dir + 1) % 4
      if dir % 2 == 0 then seg_len = seg_len + 1 end
    end
  end
  return {found = false, error = 'no buildable position for ' .. entity_name .. ' within r=' .. MAXR}
end

-- @chunk api
local F = fle
-- Entry points called by fle_tools.py (always through F.out for chunked JSON reads).
F.connect = function(ax, ay, bx, by, kind, name, dry)
  local r
  if kind == 'pole' then
    r = F.connect_poles(ax, ay, bx, by, name or 'small-electric-pole', dry)
  elseif kind == 'pipe' then
    r = F.lay_line(ax, ay, bx, by, 'pipe', dry)
    -- 2.1: LuaFluidBox is GONE, so FLE's fluid-segment check is impossible.
    -- Contiguity (gaps==0) is the proxy; F.fluid_probe is the live verification
    -- (GOTCHAS: get_fluid_count still works even though .fluidbox is blocked).
    r.connected = (r.gaps == 0)
  elseif kind == 'belt' then
    r = F.lay_line(ax, ay, bx, by, name or 'transport-belt', dry)
    if dry then
      r.connected = (r.gaps == 0)
    else
      r.connected = F.belt_connected(ax, ay, bx, by)
    end
  else
    r = {error = 'unknown kind ' .. tostring(kind)}
  end
  return r
end
F.fluid_probe = function(x, y, fluid)
  local e = game.surfaces[1].find_entities_filtered{position = {x + 0.5, y + 0.5}, radius = 0.6,
    type = {'pipe', 'pipe-to-ground', 'storage-tank', 'boiler', 'offshore-pump'}}[1]
  if not e then return {found = false} end
  return {found = true, name = e.name, count = e.get_fluid_count(fluid)}
end
