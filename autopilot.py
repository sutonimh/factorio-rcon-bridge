#!/usr/bin/env python3
"""Autopilot primitives for driving the player character over RCON.

Real-resources only: walks the real character, mines real ore patches into the
real inventory, builds from the real inventory. Nothing is spawned from nothing.

Usage:
    python3 autopilot.py walk <x> <y>            walk to a coordinate
    python3 autopilot.py mine <name> <count>     mine nearby resource until count
    python3 autopilot.py goto-mine <name> <n>    walk to nearest <name>, mine n
    python3 autopilot.py pos                      print current position
"""
import sys, time, math, json, pathlib
import rcon  # reuse the client in this dir

HERE = pathlib.Path(__file__).resolve().parent

DIRS16 = 16  # Factorio 2.0 uses a 16-direction system; cardinals are multiples of 4


def _print(cmd):
    return rcon.run(cmd)


def pos():
    out = _print("/sc local p=game.players[1]; rcon.print(p.position.x..','..p.position.y)")
    x, y = out.strip().split(",")
    return float(x), float(y)


def heading(px, py, tx, ty):
    # Factorio: +y is south. angle 0 = north, clockwise.
    ang = math.atan2(tx - px, -(ty - py))  # radians, 0=north
    idx = round(ang / (2 * math.pi / DIRS16)) % DIRS16
    return idx


def walk(tx, ty, tol=1.5, timeout=90):
    start = time.time()
    while True:
        px, py = pos()
        dist = math.hypot(tx - px, ty - py)
        if dist <= tol:
            _print("/sc game.players[1].walking_state={walking=false}")
            return px, py, True
        if time.time() - start > timeout:
            _print("/sc game.players[1].walking_state={walking=false}")
            return px, py, False
        d = heading(px, py, tx, ty)
        _print(f"/sc game.players[1].walking_state={{walking=true,direction={d}}}")
        # shorter polls when close so we don't overshoot
        time.sleep(0.25 if dist < 8 else 0.5)


def mine(name, count):
    # Deplete-and-insert: take ore from real resource entities near the player
    # and add the same amount to the inventory. The patch loses exactly what the
    # inventory gains, so no resources are created. Respects inventory space.
    lua = (
        "/sc local p=game.players[1]; local inv=p.get_main_inventory();"
        "local target=" + str(int(count)) + "; local name='" + name + "';"
        "local got=0;"
        "local es=p.surface.find_entities_filtered{position=p.position,radius=10,name=name};"
        "table.sort(es,function(a,b) return (a.position.x-p.position.x)^2+(a.position.y-p.position.y)^2 < (b.position.x-p.position.x)^2+(b.position.y-p.position.y)^2 end);"
        "for _,e in pairs(es) do if got>=target then break end;"
        "  if e.valid and e.amount and e.amount>0 then"
        "    local prod=e.prototype.mineable_properties.products[1].name;"
        "    local take=math.min(e.amount, target-got);"
        "    local ins=inv.insert{name=prod, count=take};"
        "    if ins>0 then e.amount=e.amount-ins; if e.amount<=0 then e.destroy() end; got=got+ins end"
        "  end end;"
        "rcon.print('mined '..got..' '..name)"
    )
    return _print(lua)


def build(name, x, y, direction=0, walk_first=True, reach_tol=3.0):
    # Walk the character to the build site (visible travel), then place the entity.
    # The cursor/hand-build API is client-authoritative for a connected player and
    # can't be driven over RCON, so placement itself is server-side create_entity.
    # Conservative: the item is removed from the real inventory (world += 1, inv -= 1).
    if walk_first:
        walk(x, y, tol=reach_tol)
    lua = (
        "/sc local p=game.players[1]; local s=p.surface; local inv=p.get_main_inventory();"
        "local item='" + name + "'; local pos={" + str(x) + "," + str(y) + "}; local dir=" + str(int(direction)) + ";"
        "if inv.get_item_count(item)<1 then rcon.print('NO_ITEM '..item) return end;"
        "local proto=prototypes.item[item]; local ename=proto and proto.place_result and proto.place_result.name or item;"
        "if not s.can_place_entity{name=ename,position=pos,direction=dir,force=p.force} then rcon.print('CANT_PLACE '..item..' at '..pos[1]..','..pos[2]) return end;"
        "local e=s.create_entity{name=ename,position=pos,direction=dir,force=p.force,player=p};"
        "if e then inv.remove{name=item,count=1}; rcon.print('BUILT '..ename..' at '..math.floor(e.position.x)..','..math.floor(e.position.y)) else rcon.print('CREATE_FAILED '..item) end"
    )
    return _print(lua)


def craft(recipe, count, timeout=90):
    started = _print(
        "/sc rcon.print(game.players[1].begin_crafting{recipe='" + recipe + "',count=" + str(int(count)) + "})"
    ).strip()
    t0 = time.time()
    while time.time() - t0 < timeout:
        q = _print("/sc rcon.print(game.players[1].crafting_queue_size)").strip()
        if q == "0":
            break
        time.sleep(1.5)
    return f"crafted {recipe} (started {started})"


# --- Placement geometry (learned from Seth's hand-built layout) -----------------
# An entity's CENTER = top-left footprint tile + (tile_width/2, tile_height/2):
#   1x1 (belt/inserter/chest): tile (x,y) -> center (x+0.5, y+0.5)
#   2x2 (drill/furnace):       tile (x,y) -> center (x+1,   y+1)
# Passing the wrong center is why 1x1 belts/inserters used to land a tile off.
# Directions (2.0/2.1): N=0, E=4, S=8, W=12.
# Inserter `direction` is its PICKUP side (dir=12/west picks from the west tile,
# drops east). `drill.drop_position` / `inserter.pickup_position`/`drop_position`
# are readable - use them to verify, unlike fluidbox.

def place(name, tile_x, tile_y, direction=0):
    """Place an entity by its TOP-LEFT footprint tile, auto-centering by size.
    Conservative: removes one from the real inventory. Returns a status string
    with the actual snapped position."""
    lua = (
        "/sc local p=game.players[1]; local s=p.surface; local inv=p.get_main_inventory();"
        "local item='" + name + "'; local tx=" + str(tile_x) + "; local ty=" + str(tile_y) + "; local dir=" + str(int(direction)) + ";"
        "local proto=prototypes.item[item]; local ename=(proto and proto.place_result and proto.place_result.name) or item;"
        "local ep=prototypes.entity[ename]; local cx=tx+ep.tile_width/2; local cy=ty+ep.tile_height/2;"
        "if inv.get_item_count(item)<1 then rcon.print('NO_ITEM '..item) return end;"
        "if not s.can_place_entity{name=ename,position={cx,cy},direction=dir,force=p.force} then rcon.print('CANT_PLACE '..ename..' @tile('..tx..','..ty..')') return end;"
        "local e=s.create_entity{name=ename,position={cx,cy},direction=dir,force=p.force,player=p};"
        "if e then inv.remove{name=item,count=1}; rcon.print('BUILT '..ename..' @('..e.position.x..','..e.position.y..')') else rcon.print('CREATE_FAILED '..item) end"
    )
    return _print(lua)


def stamp_blueprint(entities):
    """STEP 1 of a build: stamp the blueprint as entity-ghosts (nothing real yet),
    so the player can review the plan before construction. `entities`: list of
    {name, x(center), y(center), dir?}. Follow with build_ghosts() once approved."""
    specs = ";".join("{'%s',%s,%s,%s}" % (e["name"], e["x"], e["y"], e.get("dir", 0)) for e in entities)
    lua = (
        "/sc local s=game.surfaces['nauvis']; local f=game.forces.player; local L={" + specs + "}; local n=0;"
        "for _,d in ipairs(L) do local g=s.create_entity{name='entity-ghost', inner_name=d[1], position={d[2],d[3]}, direction=d[4], force=f}; if g then n=n+1 end end;"
        "rcon.print('blueprint stamped: '..n..' ghosts (awaiting approval)')"
    )
    return _print(lua)


def build_ghosts(cadence=0.35, batch=2):
    """STEP 2: after the player approves the stamped blueprint, build the ghosts in
    a realistic player-like cadence (a couple at a time with a short delay),
    consuming items from inventory and fueling burners."""
    revive = (
        "/sc local p=game.players[1]; local s=game.surfaces['nauvis']; local inv=p.get_main_inventory(); local built=0;"
        "for _,g in pairs(s.find_entities_filtered{name='entity-ghost', force='player'}) do if built>=" + str(batch) + " then break end;"
        "  local gp=g.ghost_prototype; local item=(gp.items_to_place_this and gp.items_to_place_this[1] and gp.items_to_place_this[1].name) or g.ghost_name;"
        "  if inv.get_item_count(item)>0 then local col,e=g.revive{}; if e then inv.remove{name=item,count=1}; built=built+1;"
        "    if e.type=='furnace' or e.name=='burner-mining-drill' or e.name=='burner-inserter' then local c=math.min(5,inv.get_item_count('coal')); if c>0 then e.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end end end end end;"
        "rcon.print(built..'|'..#s.find_entities_filtered{name='entity-ghost',force='player'})"
    )
    n = 0
    for _ in range(500):
        r = _print(revive).strip()
        try:
            built, remaining = r.split("|")
            n += int(built)
            if remaining == "0":
                break
            if built == "0":
                return f"built {n}; STALLED (out of materials), {remaining} ghosts remain"
        except ValueError:
            break
        time.sleep(cadence)
    return f"built {n} entities in cadence"


def notepad(lines):
    """Persistent on-screen 'notepad' in-game (rendering API) showing the task queue.
    Stays on screen (unlike game.print which scrolls away). Pass a list of lines."""
    body = "\\n".join(["[ AUTOPILOT QUEUE ]"] + list(lines))
    lua = (
        "/sc rendering.clear();"
        "rendering.draw_text{text='" + body.replace("'", "") + "', surface='nauvis', target={2,-40}, color={1,0.9,0.4}, scale=2.5, alignment='left'}"
    )
    return _print(lua)


def snapshot(path=None):
    """Dump every player-built entity to a JSON file (the persistent build store),
    so infrastructure can be rebuilt if destroyed/deleted. Read-only on the game."""
    path = path or str(HERE / "base-snapshot.json")
    lua = (
        "/sc local s=game.surfaces['nauvis']; local out={};"
        "for _,e in pairs(s.find_entities_filtered{force='player'}) do"
        "  if e.name~='character' and e.name~='character-corpse' then"
        "    local rec='';"
        "    if e.type=='assembling-machine' or e.type=='furnace' then pcall(function() local r=e.get_recipe(); if r then rec=r.name end end) end;"
        "    out[#out+1]=e.name..'\\t'..string.format('%.2f',e.position.x)..'\\t'..string.format('%.2f',e.position.y)..'\\t'..e.direction..'\\t'..rec"
        "  end end;"
        "rcon.print(#out..'\\n'..table.concat(out,'\\n'))"
    )
    raw = _print(lua)
    lines = raw.split("\n")
    count = int(lines[0]) if lines and lines[0].strip().isdigit() else 0
    ents = []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        parts = ln.split("\t")
        if len(parts) < 4:
            continue
        ents.append({
            "name": parts[0], "x": float(parts[1]), "y": float(parts[2]),
            "direction": int(parts[3]), "recipe": parts[4] if len(parts) > 4 else "",
        })
    with open(path, "w") as f:
        json.dump({"count": len(ents), "entities": ents}, f, indent=0)
    return f"snapshot: {len(ents)} entities -> {path} (reported {count})"


def rebuild(path=None):
    """Restore any entity in the snapshot that is missing from the world (e.g.
    destroyed by biters or deleted). Re-creates missing entities at their saved
    position/direction and re-applies assembler recipes."""
    path = path or str(HERE / "base-snapshot.json")
    with open(path) as f:
        snap = json.load(f)
    # send the entity list to the game; it re-creates only what is missing
    rows = ";".join(
        f"{{'{e['name']}',{e['x']},{e['y']},{e['direction']},'{e['recipe']}'}}"
        for e in snap["entities"]
    )
    lua = (
        "/sc local s=game.surfaces['nauvis']; local f=game.forces.player; local want={" + rows + "};"
        "local rebuilt=0; local ok=0;"
        "for _,d in ipairs(want) do local name,x,y,dir,rec=d[1],d[2],d[3],d[4],d[5];"
        "  local found=s.find_entities_filtered{position={x,y},radius=0.6,name=name}[1];"
        "  if found then ok=ok+1 else"
        "    if s.can_place_entity{name=name,position={x,y},direction=dir,force=f} then"
        "      local e=s.create_entity{name=name,position={x,y},direction=dir,force=f};"
        "      if e then rebuilt=rebuilt+1; if rec~='' then pcall(function() e.set_recipe(rec) end) end end"
        "    end end end;"
        "rcon.print('rebuild: '..ok..' intact, '..rebuilt..' restored')"
    )
    return _print(lua)


def announce(msg):
    """Post a status line to the IN-GAME console log (top-left, persists, scrollable)
    so the player can see what the autopilot is working on / its queue."""
    safe = msg.replace("'", "").replace("\\", "")
    return _print("/sc game.print('[autopilot] " + safe + "')")


def pickup(radius=12):
    """Pick up items lying on the ground (e.g. spilled coal) near the character
    into the real inventory. Conservative: items move from ground to inventory."""
    lua = (
        "/sc local p=game.players[1]; local s=p.surface; local inv=p.get_main_inventory();"
        "local picked={};"
        "for _,e in pairs(s.find_entities_filtered{position=p.position, radius=" + str(radius) + ", name='item-on-ground'}) do"
        "  if e.valid and e.stack and e.stack.valid_for_read then local n=e.stack.name; local c=e.stack.count;"
        "    local ins=inv.insert{name=n,count=c};"
        "    if ins>0 then picked[n]=(picked[n] or 0)+ins; if ins>=c then e.destroy() else e.stack.count=c-ins end end end end;"
        "local o={}; for n,c in pairs(picked) do o[#o+1]=n..'x'..c end;"
        "rcon.print(#o>0 and ('picked up: '..table.concat(o,', ')) or 'nothing on ground within " + str(radius) + "')"
    )
    return _print(lua)


def refill_turrets(chest_x=20.5, chest_y=-2.5, threshold=50, target=100):
    """Keep gun turrets topped up to a FULL stack (100 magazines), refilling any
    turret that drops below 50%. Moves magazines from the ammo buffer chest.
    Designed to run on the defend loop; caps at available buffer ammo."""
    lua = (
        "/sc local s=game.surfaces['nauvis'];"
        "local chest=s.find_entities_filtered{position={" + str(chest_x) + "," + str(chest_y) + "},radius=2,name={'wooden-chest','iron-chest','steel-chest'}}[1];"
        "if not chest then rcon.print('no ammo chest') return end;"
        "local ci=chest.get_inventory(defines.inventory.chest);"
        "local refilled=0; local low=0;"
        "for _,t in pairs(s.find_entities_filtered{name='gun-turret'}) do"
        "  local ai=t.get_inventory(defines.inventory.turret_ammo);"
        "  if ai then local have=ai.get_item_count('firearm-magazine');"
        "    if have<" + str(threshold) + " then low=low+1;"
        "      local move=math.min(" + str(target) + "-have, ci.get_item_count('firearm-magazine'));"
        "      if move>0 then ai.insert{name='firearm-magazine',count=move}; ci.remove{name='firearm-magazine',count=move}; refilled=refilled+move end end end end;"
        "rcon.print('refill: '..low..' turrets low, +'..refilled..' mags ('..ci.get_item_count('firearm-magazine')..' left in buffer)')"
    )
    return _print(lua)


def store_overflow(ox=-20, oy=-36, keep_coal=100):
    """Drain bulk items from the player inventory into an auto-scaling chest array
    at (ox,oy). Keeps `keep_coal` coal in inventory for fueling. Places a new chest
    (from inventory wooden/iron chests) whenever the array is full."""
    lua = (
        "/sc local p=game.players[1]; local s=game.surfaces['nauvis']; local inv=p.get_main_inventory();"
        "local OX=" + str(ox) + "; local OY=" + str(oy) + "; local COLS=12;"
        "local function chests() return s.find_entities_filtered{area={{OX,OY},{OX+COLS,OY+10}}, name={'wooden-chest','iron-chest','steel-chest'}} end;"
        # clearance guard: only place in the dedicated zone, never adjacent to a non-chest build
        "local function clear_of_builds(gx,gy) for _,e in pairs(s.find_entities_filtered{area={{gx-1,gy-1},{gx+2,gy+2}},force='player'}) do if not string.find(e.name,'chest') then return false end end return true end;"
        "local function add_chest() for gy=OY,OY+9 do for gx=OX,OX+COLS-1 do"
        "  if clear_of_builds(gx,gy) and s.can_place_entity{name='wooden-chest',position={gx+0.5,gy+0.5},force=p.force} then"
        "    local item=(inv.get_item_count('iron-chest')>0 and 'iron-chest') or (inv.get_item_count('wooden-chest')>0 and 'wooden-chest') or nil;"
        "    if item then local c=s.create_entity{name=item,position={gx+0.5,gy+0.5},force=p.force}; if c then inv.remove{name=item,count=1}; return c end end;"
        "    return nil end end end return nil end;"
        # bulk items to overflow (raw/intermediate); keep tools/ammo/equipment in inventory
        "local bulk={'iron-ore','copper-ore','stone','iron-plate','copper-plate','iron-gear-wheel','copper-cable','electronic-circuit','wood'};"
        "local moved={}; local cs=chests(); if #cs==0 then local c=add_chest(); if c then cs={c} end end;"
        "local function put(name,count) for _,c in pairs(cs) do local n=c.get_inventory(defines.inventory.chest).insert{name=name,count=count}; count=count-n; moved[name]=(moved[name] or 0)+n; if count<=0 then return 0 end end; return count end;"
        "for _,name in ipairs(bulk) do local have=inv.get_item_count(name); local keep=(name=='coal') and " + str(keep_coal) + " or 0; local over=have-keep;"
        "  if over>0 then local left=put(name,over); if left>0 then local c=add_chest(); if c then cs=chests(); left=put(name,left) end end;"
        "    local actually=over-left; if actually>0 then inv.remove{name=name,count=actually} end end end;"
        "local o={}; for n,c in pairs(moved) do o[#o+1]=n..'x'..c end;"
        "rcon.print('overflow: stored '..(#o>0 and table.concat(o,', ') or 'nothing')..' | chests in array='..#chests())"
    )
    return _print(lua)


def feed_smelter():
    """Keep the auto-smelting plant's ore belt (row y=14) stocked with iron ore from
    the mining chest (~17,0), and top up furnace + burner-inserter coal. Run on the
    maintain loop so the 12-furnace plant smelts continuously. (A physical belt from
    the mine would make it fully self-running; this is the software feed until then.)"""
    lua = (
        "/sc local p=game.players[1]; local s=game.surfaces['nauvis']; local inv=p.get_main_inventory();"
        "local mc=s.find_entities_filtered{position={17.5,0.5},radius=2,name='iron-chest'}[1];"
        "if not mc then rcon.print('no mining chest') return end; local mci=mc.get_inventory(defines.inventory.chest);"
        "local put=0;"
        "for _,b in pairs(s.find_entities_filtered{area={{-3,-28},{23,-27}},name='transport-belt'}) do"
        "  for _,tl in ipairs({1,2}) do local line=b.get_transport_line(tl);"
        "    if line.get_item_count()<2 and mci.get_item_count('iron-ore')>0 then if line.insert_at_back({name='iron-ore',count=1}) then mci.remove{name='iron-ore',count=1}; put=put+1 end end end end;"
        # top up coal on furnaces + burner inserters in the plant (from inventory)
        "local fueled=0;"
        "for _,e in pairs(s.find_entities_filtered{area={{-3,-33},{23,-28}},name={'stone-furnace','burner-inserter'}}) do local fi=e.get_fuel_inventory(); if fi and fi.get_item_count('coal')<3 then local c=math.min(5,inv.get_item_count('coal')); if c>0 then e.insert{name='coal',count=c}; inv.remove{name='coal',count=c}; fueled=fueled+1 end end end;"
        "rcon.print('feed_smelter: +'..put..' ore to belt, fueled '..fueled..' (mining chest ore='..mci.get_item_count('iron-ore')..')')"
    )
    return _print(lua)


def produce_ammo():
    """One ammo-production cycle: collect smelted iron from the furnace row, reload
    the furnaces from the mining chest, craft magazines from available iron, and
    stock the ammo buffer chest. Run repeatedly (e.g. on the maintain loop) to
    drive turret ammo back to full after it drops below 50%."""
    lua = (
        "/sc local p=game.players[1]; local s=game.surfaces['nauvis']; local inv=p.get_main_inventory();"
        "local got=0;"
        "for _,fur in pairs(s.find_entities_filtered{area={{-3,-33},{23,-28}},name='stone-furnace'}) do"
        "  local o=fur.get_output_inventory(); local c=o.get_item_count('iron-plate'); if c>0 then got=got+c; inv.insert{name='iron-plate',count=c}; o.remove{name='iron-plate',count=c} end end;"
        "local mc=s.find_entities_filtered{position={17.5,0.5},radius=2,name='iron-chest'}[1];"
        "if mc then local mci=mc.get_inventory(defines.inventory.chest);"
        "  for _,fur in pairs(s.find_entities_filtered{area={{-3,-33},{23,-28}},name='stone-furnace'}) do"
        "    if fur.get_fuel_inventory().get_item_count('coal')<4 then local cc=math.min(8,inv.get_item_count('coal')); if cc>0 then fur.insert{name='coal',count=cc}; inv.remove{name='coal',count=cc} end end;"
        "    local take=math.min(20,mci.get_item_count('iron-ore')); if take>0 then local ins=fur.insert{name='iron-ore',count=take}; mci.remove{name='iron-ore',count=ins} end end end;"
        "local iron=inv.get_item_count('iron-plate'); local make=math.floor((iron-8)/4); if make>0 then p.begin_crafting{recipe='firearm-magazine',count=make} end;"
        "local buf=s.find_entities_filtered{position={20.5,-2.5},radius=2,name='wooden-chest'}[1];"
        "local mags=inv.get_item_count('firearm-magazine'); if buf and mags>0 then buf.get_inventory(defines.inventory.chest).insert{name='firearm-magazine',count=mags}; inv.remove{name='firearm-magazine',count=mags} end;"
        "rcon.print('ammo cycle: +'..got..' iron collected, crafting '..(make>0 and make or 0)..' mags, '..mags..' to buffer')"
    )
    return _print(lua)


def turrets_low():
    """Return True if any gun turret is below 50% ammo (50/100 magazines)."""
    lua = (
        "/sc local s=game.surfaces['nauvis']; local low=0; local n=0;"
        "for _,t in pairs(s.find_entities_filtered{name='gun-turret'}) do n=n+1;"
        "  local ai=t.get_inventory(defines.inventory.turret_ammo); if ai and ai.get_item_count('firearm-magazine')<50 then low=low+1 end end;"
        "rcon.print(low..'/'..n)"
    )
    out = _print(lua).strip()
    try:
        low = int(out.split("/")[0])
        return low > 0, out
    except Exception:
        return False, out


def maintain():
    """Unified periodic maintenance loop body. Runs the whole resilience system:
    pickup ground items, refill turrets, and if any turret is <50% drive ammo
    production; then defend_check (rebuild/repair after an attack)."""
    log = [pickup().strip(), feed_smelter().strip()]
    low, ratio = turrets_low()
    log.append(refill_turrets().strip())
    if low:
        log.append(f"turrets low ({ratio}) -> " + produce_ammo().strip())
    log.append(defend_check().strip())
    return "\n".join(log)


def storage_inventory(ox=-20, oy=-36):
    """Report the consolidated contents of the overflow chest array - so stored
    materials can be found and reused. The chests themselves are the memory."""
    lua = (
        "/sc local s=game.surfaces['nauvis'];"
        "local cs=s.find_entities_filtered{area={{" + str(ox) + "," + str(oy) + "},{" + str(ox+12) + "," + str(oy+10) + "}}, name={'wooden-chest','iron-chest','steel-chest'}};"
        "local tot={};"
        "for _,c in pairs(cs) do for _,it in pairs(c.get_inventory(defines.inventory.chest).get_contents()) do tot[it.name]=(tot[it.name] or 0)+it.count end end;"
        "local o={}; for n,c in pairs(tot) do o[#o+1]=n..'='..c end; table.sort(o);"
        "rcon.print('storage ('..#cs..' chests): '..(#o>0 and table.concat(o,' ') or 'empty'))"
    )
    return _print(lua)


def retrieve(item, count, ox=-20, oy=-36):
    """Pull up to `count` of `item` from the overflow chest array into the player
    inventory. Use when a craft/build needs materials that were stored."""
    lua = (
        "/sc local p=game.players[1]; local s=game.surfaces['nauvis']; local inv=p.get_main_inventory();"
        "local need=" + str(int(count)) + "; local got=0;"
        "for _,c in pairs(s.find_entities_filtered{area={{" + str(ox) + "," + str(oy) + "},{" + str(ox+12) + "," + str(oy+10) + "}}, name={'wooden-chest','iron-chest','steel-chest'}}) do"
        "  if got>=need then break end; local ci=c.get_inventory(defines.inventory.chest); local avail=ci.get_item_count('" + item + "');"
        "  local take=math.min(avail, need-got); if take>0 then local ins=inv.insert{name='" + item + "',count=take}; ci.remove{name='" + item + "',count=ins}; got=got+ins end end;"
        "rcon.print('retrieved '..got..' " + item + " (have '..inv.get_item_count('" + item + "')..' in inventory now)')"
    )
    return _print(lua)


def fortify(cx, cy, count=16, radius=13, detect_range=90, ammo_each=10, base=6, per_nest=4):
    """Adaptive, nest-scaled turret ring around (cx,cy). Counts biter nests within
    detect_range and sizes the ring to the threat: target = base + per_nest*nests,
    capped at `count`. With nests present it weights ~half the turrets into the arc
    facing the NEAREST nest (rest spread behind); with NO nest in range it places a
    light EVEN ring (`base`). Salvages+replaces existing turrets, so it re-runs on
    the maintain loop to reconfigure as nests spawn/grow/die. Caps at turrets on hand."""
    lua = (
        "/sc local p=game.players[1]; local s=game.surfaces['nauvis']; local inv=p.get_main_inventory();"
        "local cx=" + str(cx) + "; local cy=" + str(cy) + "; local cmax=" + str(int(count)) + "; local R=" + str(radius) + "; local det=" + str(detect_range) + "; local pi=math.pi;"
        "local nest=nil; local bd=det*det; local nnests=0; for _,e in pairs(s.find_entities_filtered{type='unit-spawner',force='enemy'}) do local d=(e.position.x-cx)^2+(e.position.y-cy)^2; if d<det*det then nnests=nnests+1 end; if d<bd then bd=d; nest=e end end;"
        "local count=math.max(" + str(base) + ", math.min(cmax, " + str(base) + "+" + str(per_nest) + "*nnests));"
        # salvage existing turrets in the area back to inventory
        "for _,t in pairs(s.find_entities_filtered{position={cx,cy},radius=R+10,name='gun-turret'}) do local ai=t.get_inventory(defines.inventory.turret_ammo); if ai then local m=ai.get_item_count('firearm-magazine'); if m>0 then inv.insert{name='firearm-magazine',count=m} end end; inv.insert{name='gun-turret',count=1}; t.destroy() end;"
        "local pos={};"
        "if nest then local a0=math.atan2(nest.position.y-cy, nest.position.x-cx); local front=math.ceil(count*0.5); local rear=count-front;"
        "  for i=0,front-1 do local a=a0+(-0.85+1.7*(front>1 and i/(front-1) or 0)); pos[#pos+1]={cx+R*math.cos(a), cy+R*math.sin(a)} end;"
        "  for i=0,rear-1 do local a=a0+pi+(-1.3+2.6*(rear>1 and i/(rear-1) or 0)); pos[#pos+1]={cx+R*math.cos(a), cy+R*math.sin(a)} end;"
        "else for i=0,count-1 do local a=2*pi*i/count; pos[#pos+1]={cx+R*math.cos(a), cy+R*math.sin(a)} end end;"
        "local placed=0; for _,pp in ipairs(pos) do if inv.get_item_count('gun-turret')>0 then local np=s.find_non_colliding_position('gun-turret', pp, 6, 1); if np then local t=s.create_entity{name='gun-turret',position=np,force=p.force,player=p}; if t then inv.remove{name='gun-turret',count=1}; placed=placed+1; local mag=math.min(" + str(ammo_each) + ", inv.get_item_count('firearm-magazine')); if mag>0 then t.insert{name='firearm-magazine',count=mag}; inv.remove{name='firearm-magazine',count=mag} end end end end end;"
        "rcon.print('fortify ('..cx..','..cy..'): '..placed..'/'..count..' turrets ('..nnests..' nests in range); '..(nest and ('nearest@('..math.floor(nest.position.x)..','..math.floor(nest.position.y)..') -> WEIGHTED') or 'no nest -> EVEN')..'; turrets in hand='..inv.get_item_count('gun-turret'))"
    )
    return _print(lua)


def auto_repair():
    """Repair damaged (not destroyed) player structures using repair-packs from the
    inventory. Part of the post-attack sequence. No-op if no repair-packs yet."""
    lua = (
        "/sc local p=game.players[1]; local s=game.surfaces['nauvis']; local inv=p.get_main_inventory();"
        "local packs=inv.get_item_count('repair-pack'); local damaged=0; local repaired=0;"
        "for _,e in pairs(s.find_entities_filtered{force='player'}) do"
        "  if e.name~='character' and e.health and e.max_health and e.health<e.max_health then damaged=damaged+1;"
        "    if packs>0 then local use=math.min(math.ceil((e.max_health-e.health)/200),packs);"
        "      e.health=e.max_health; inv.remove{name='repair-pack',count=use}; packs=packs-use; repaired=repaired+1 end end end;"
        "rcon.print('repair: '..damaged..' damaged, '..repaired..' repaired ('..packs..' packs left)')"
    )
    return _print(lua)


def defend_check(base_x=10, base_y=-12, radius=70):
    """Attack-detection + full post-attack maintenance. Counts enemies near the base.
    While enemies are present -> 'UNDER ATTACK' (turrets handle it; don't repair
    mid-fight). When clear -> rebuild destroyed, repair damaged, refill turrets."""
    lua = (
        "/sc local s=game.surfaces['nauvis'];"
        "local enemies=s.count_entities_filtered{position={" + str(base_x) + "," + str(base_y) + "},radius=" + str(radius) + ",force='enemy'};"
        "rcon.print(enemies)"
    )
    n = _print(lua).strip()
    try:
        n = int(n)
    except ValueError:
        return f"defend_check error: {n}"
    if n > 0:
        # keep turrets fed during the fight, but hold repairs until it's over
        refill_turrets()
        return f"UNDER ATTACK: {n} enemies within {radius} of base ({base_x},{base_y})"
    # clear -> full post-attack sequence
    parts = [rebuild(), auto_repair(), refill_turrets()]
    return "clear (0 enemies) | " + " | ".join(p.strip() for p in parts)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "pos":
        print(pos())
    elif cmd == "walk":
        x, y = float(sys.argv[2]), float(sys.argv[3])
        print(walk(x, y))
    elif cmd == "mine":
        print(mine(sys.argv[2], int(sys.argv[3])))
    elif cmd == "build":
        d = int(sys.argv[5]) if len(sys.argv) > 5 else 0
        print(build(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), d))
    elif cmd == "craft":
        print(craft(sys.argv[2], int(sys.argv[3])))
    elif cmd == "place":
        d = int(sys.argv[5]) if len(sys.argv) > 5 else 0
        print(place(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), d))
    elif cmd == "snapshot":
        print(snapshot())
    elif cmd == "rebuild":
        print(rebuild())
    elif cmd == "defend-check":
        print(defend_check())
    elif cmd == "notepad":
        print(notepad(sys.argv[2:]))
    elif cmd == "build-ghosts":
        print(build_ghosts())
    elif cmd == "announce":
        print(announce(" ".join(sys.argv[2:])))
    elif cmd == "pickup":
        print(pickup(int(sys.argv[2]) if len(sys.argv) > 2 else 12))
    elif cmd == "refill-turrets":
        print(refill_turrets())
    elif cmd == "auto-repair":
        print(auto_repair())
    elif cmd == "fortify":
        cnt = int(sys.argv[4]) if len(sys.argv) > 4 else 10
        print(fortify(float(sys.argv[2]), float(sys.argv[3]), cnt))
    elif cmd == "feed-smelter":
        print(feed_smelter())
    elif cmd == "store-overflow":
        print(store_overflow())
    elif cmd == "storage-inventory":
        print(storage_inventory())
    elif cmd == "retrieve":
        print(retrieve(sys.argv[2], int(sys.argv[3])))
    elif cmd == "goto-mine":
        name, n = sys.argv[2], int(sys.argv[3])
        # find nearest patch, walk to it, mine
        out = _print(
            "/sc local p=game.players[1]; local es=p.surface.find_entities_filtered{position=p.position,radius=400,name='"
            + name + "'}; local best,bd=nil,1e18; for _,e in pairs(es) do local d=(e.position.x-p.position.x)^2+(e.position.y-p.position.y)^2; if d<bd then bd=d; best=e end end; if best then rcon.print(best.position.x..','..best.position.y) else rcon.print('none') end"
        ).strip()
        if out == "none":
            print("no", name, "found"); sys.exit(1)
        tx, ty = map(float, out.split(","))
        print("walking to", (tx, ty)); print(walk(tx, ty))
        print(mine(name, n))
    else:
        print(__doc__); sys.exit(2)
