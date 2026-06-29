#!/usr/bin/env python3
"""FRESH-WORLD BOOTSTRAP SEQUENCE (Seth's rule: codify everything that successfully boots a
base so there's minimal thinking time on a new world; on load, immediately run bootstrap()).

Every step here is a PROVEN move from a live session, captured as an idempotent function:
re-running skips work already done (checks live entities) so it resumes after interruption.
Resource positions are scouted live (they differ per world); the LOGIC is fixed.

Order (each unblocks the next):
  setup_world  -> peaceful, clear crash debris
  scout        -> richest iron/copper/stone/coal tiles + nearest water (cached in STATE)
  fuel         -> hand-mine coal (first fuel)
  smelting_base-> furnace rows at spawn + first plate stock (hand-mined ore)
  power        -> offshore pump -> boiler -> steam engine (self-verifying geometry)
  red_science  -> lab at the plant, craft red packs, research 'automation'
Then onward (green/oil/blue/robotics) builds on this.

Usage:  python3 bootstrap.py            # run the whole sequence on the current world
        python3 -c 'import bootstrap; bootstrap.power()'   # one step
"""
import time
import autopilot as A
import techdb
import gamedb
import status

SPAWN = (6, -14)          # base hub: spawn is the most central point on a fresh world
STATE = {}                # scouted positions, filled by scout()


# ----------------------------------------------------------------------------- helpers
def _count(item):
    return int(A._print(f"/sc rcon.print(storage.derpface.get_main_inventory().get_item_count('{item}'))").strip() or 0)


def _find(name, x, y, r=3):
    """True if an entity `name` exists near (x,y) (idempotency probe)."""
    out = A._print(f"/sc local s=game.surfaces[1]; rcon.print(#s.find_entities_filtered{{name='{name}',position={{{x},{y}}},radius={r}}})").strip()
    try:
        return int(out) > 0
    except ValueError:
        return False


# derpface is a player-LESS character, which can't `begin_crafting`/`get_craftable_count` (those
# are LuaPlayer methods). So we SCRIPT-CRAFT: a recursive hand-craft over derpface's inventory
# that consumes ingredients and produces outputs per the recipe, auto-crafting hand-craftable
# intermediates (category 'crafting') and stopping at raws (plates/ore = provided by the
# provisioner). Instant + deterministic, like the deplete-and-insert mining the project already uses.
_RAW = "{['iron-plate']=true,['copper-plate']=true,['steel-plate']=true,['stone']=true,['coal']=true,['iron-ore']=true,['copper-ore']=true,['plastic-bar']=true,['sulfur']=true}"
_SC = (
    "local D=storage.derpface; local INV=D.get_main_inventory(); local F=D.force; local STOP=" + _RAW + ";"
    "local function cnt(n) return INV.get_item_count(n) end;"
    "local sc; sc=function(name,count) if STOP[name] then return 0 end; local r=F.recipes[name];"
    "  if not r or not r.enabled then return 0 end;"
    "  for _,fi in pairs(r.ingredients) do if fi.type=='fluid' then return 0 end end;"
    "  local made=0;"
    "  for i=1,count do local ok=true;"
    "    for _,ing in pairs(r.ingredients) do if ing.type=='item' then"
    "      if cnt(ing.name)<ing.amount then sc(ing.name, ing.amount-cnt(ing.name)) end;"
    "      if cnt(ing.name)<ing.amount then ok=false; break end end end;"
    "    if not ok then break end;"
    "    for _,ing in pairs(r.ingredients) do if ing.type=='item' then INV.remove{name=ing.name,count=ing.amount} end end;"
    "    for _,prod in pairs(r.products) do if prod.type=='item' then INV.insert{name=prod.name,count=(prod.amount or prod.amount_max or 1)} end end;"
    "    made=made+1 end; return made end;"
)


def craftable(recipe):
    """How many of `recipe` derpface could hand-craft from its CURRENT inventory (recursive,
    non-destructive estimate). Returns a count; 0 if not hand-craftable / missing raws."""
    out = A._print(
        "/sc " + _SC +
        "local function can(name) if STOP[name] then return cnt(name) end; local r=F.recipes[name]; if not r or not r.enabled then return cnt(name) end;"
        "  local m=1/0; for _,i in pairs(r.ingredients) do if i.type=='item' then m=math.min(m, math.floor(cnt(i.name)/i.amount)) end end;"
        "  if m==1/0 then m=0 end; return m end;"
        "rcon.print(can('" + recipe + "'))").strip()
    try:
        return int(out)
    except ValueError:
        return 0


def missing_for(recipe):
    """Which DIRECT ingredients derpface is short on (for diagnostics)."""
    out = A._print(
        "/sc local D=storage.derpface; local INV=D.get_main_inventory(); local r=D.force.recipes['" + recipe + "']; local s={};"
        "if r then for _,i in pairs(r.ingredients) do if i.type=='item' then local have=INV.get_item_count(i.name); "
        "if have<i.amount then s[#s+1]=i.name..' need'..i.amount..'/have'..have end end end end;"
        "rcon.print(#s>0 and table.concat(s,', ') or 'ok')").strip()
    return out


def _craft_wait(recipe, count, timeout=120):
    """SCRIPT-CRAFT `count` of `recipe` on derpface (instant, recursive). Returns how many it made
    (self-limits to available ingredients, so it never errors / blind-fires)."""
    made = A._print(f"/sc {_SC} rcon.print(sc('{recipe}',{int(count)}))").strip()
    try:
        n = int(made)
    except ValueError:
        n = 0
    if n < count:
        A.now(f"craft {recipe}: made {n}/{count} (short: {missing_for(recipe)})")
    return n


# ----------------------------------------------------------------------------- steps
def setup_world():
    """Peaceful mode + clear the crash-site spaceship debris (always, on a fresh world)."""
    A.now("Bootstrap: world setup (peaceful, clear crash debris)")
    A._print("/sc local s=game.surfaces[1]; s.peaceful_mode=true; game.map_settings.enemy_expansion.enabled=false; "
             "for _,e in pairs(s.find_entities_filtered{force='enemy'}) do e.destroy() end")
    A.clear_spaceship_debris()


def scout():
    """Find the RICHEST tile of each ore + nearest water; cache in STATE. Generates chunks
    out to 384 tiles first so resources exist to scan."""
    A.now("Bootstrap: scouting richest deposits + water")
    A._print("/sc local s=game.surfaces[1]; for cx=-12,12 do for cy=-12,12 do s.request_to_generate_chunks({x=cx*32,y=cy*32},0) end end; s.force_generate_chunk_requests()")
    for ore in ("iron-ore", "copper-ore", "stone", "coal"):
        STATE[ore] = A.richest_spot(ore, 0, 0, radius=160)
    w = A._print("/sc local s=game.surfaces[1]; local w; for r=20,200,8 do local t=s.find_tiles_filtered{position={0,0},radius=r,name={'water','deepwater'},limit=1}; if #t>0 then w=t[1]; break end end; rcon.print(w and (math.floor(w.position.x)..','..math.floor(w.position.y)) or 'none')").strip()
    STATE["water"] = tuple(map(int, w.split(","))) if "," in w else None
    return STATE


def fuel(amount=300):
    """Hand-mine the first coal (nothing runs without fuel)."""
    if _count("coal") >= amount:
        return
    cx, cy, _ = STATE["coal"]
    A.now(f"Bootstrap: mining first coal @{cx},{cy}")
    A.stop(); A.walk(cx + 1, cy, tol=2.5)
    A.mine("coal", amount)
    A.mine("coal", amount)


def smelting_base():
    """Build the smelting hub at spawn: 8 iron + 4 copper stone furnaces, then hand-mine
    iron/copper/stone and smelt a starting plate stock. Idempotent on the furnace rows."""
    bx, by = SPAWN
    # stone first (furnaces need it); mine generously
    if _count("stone") < 120:
        sx, sy, _ = STATE["stone"]
        A.now(f"Bootstrap: mining stone @{sx},{sy}")
        A.stop(); A.walk(sx + 1, sy, tol=2.5); A.mine("stone", 250)
    # iron + copper ore stock
    if _count("iron-ore") < 200:
        ix, iy, _ = STATE["iron-ore"]
        A.now(f"Bootstrap: mining iron ore @{ix},{iy}")
        A.stop(); A.walk(ix + 1, iy, tol=2.5); A.mine("iron-ore", 250)
    if _count("copper-ore") < 150:
        cx, cy, _ = STATE["copper-ore"]
        A.now(f"Bootstrap: mining copper ore @{cx},{cy}")
        A.stop(); A.walk(cx + 1, cy, tol=2.5); A.mine("copper-ore", 200)
    # furnaces
    if _count("stone-furnace") < 12 and not _find("stone-furnace", bx, by - 1, 12):
        A.now("Bootstrap: crafting 12 stone furnaces")
        _craft_wait("stone-furnace", 12)
    A.now("Bootstrap: building smelting rows at spawn")
    A.stop(); A.walk(bx, by + 4, tol=3.0)
    A.clear_area(bx, by, 18)
    if not _find("stone-furnace", 1, -15, 2):
        for x in range(0, 16, 2):
            A.place("stone-furnace", x, -16, clear=0)
    if not _find("stone-furnace", 1, -10, 2):
        for x in range(0, 8, 2):
            A.place("stone-furnace", x, -11, clear=0)
    _smelt_rows()


def _smelt_rows():
    """Load coal + ore into the base furnaces and collect finished plates."""
    A.now("Bootstrap: smelting iron + copper plates")
    A._print("/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); "
             "for _,fu in pairs(s.find_entities_filtered{area={{0,-17},{16,-14}},name='stone-furnace'}) do "
             "  local c=math.min(5,inv.get_item_count('coal')); if c>0 then fu.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end; "
             "  local o=math.min(30,inv.get_item_count('iron-ore')); if o>0 then fu.insert{name='iron-ore',count=o}; inv.remove{name='iron-ore',count=o} end end; "
             "for _,fu in pairs(s.find_entities_filtered{area={{0,-12},{8,-9}},name='stone-furnace'}) do "
             "  local c=math.min(5,inv.get_item_count('coal')); if c>0 then fu.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end; "
             "  local o=math.min(45,inv.get_item_count('copper-ore')); if o>0 then fu.insert{name='copper-ore',count=o}; inv.remove{name='copper-ore',count=o} end end")
    for _ in range(18):
        time.sleep(6)
        A._print("/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); "
                 "for _,fu in pairs(s.find_entities_filtered{area={{-1,-18},{17,-9}},name='stone-furnace'}) do local oi=fu.get_output_inventory(); "
                 "for _,it in ipairs({'iron-plate','copper-plate'}) do local a=oi.get_item_count(it); if a>0 then local g=inv.insert{name=it,count=a}; oi.remove{name=it,count=g} end end end")
        if A._print("/sc local s=game.surfaces[1]; local n=0; for _,fu in pairs(s.find_entities_filtered{area={{-1,-18},{17,-9}},name='stone-furnace'}) do if fu.status==1 then n=n+1 end end; rcon.print(n)").strip() == "0":
            break


def power():
    """Build a steam plant at the nearest water: offshore pump -> boiler -> steam engine,
    each step VERIFIED by fluid/energy reads (the resilient way - geometry is finicky).
    Idempotent: skips if a working steam engine already exists."""
    if _find("steam-engine", STATE["water"][0], STATE["water"][1], 30):
        return
    wx, wy = STATE["water"]
    A.now(f"Bootstrap: steam power plant @ water {wx},{wy}")
    # craft parts
    for r, c, item in [("offshore-pump", 1, "offshore-pump"), ("boiler", 1, "boiler"),
                       ("steam-engine", 2, "steam-engine"), ("pipe", 20, "pipe"),
                       ("small-electric-pole", 8, "small-electric-pole")]:
        if _count(item) < c:
            _craft_wait(r, c)
    A.stop(); A.walk(wx - 4, wy, tol=3.0)
    # 1) PUMP: place on a land tile adjacent to water, intake facing the water; verify by a pipe.
    pump = A._print(
        "/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); local g;"
        "local function iw(x,y) return string.find(s.get_tile(x,y).name,'water')~=nil end;"
        f"for _,wt in pairs(s.find_tiles_filtered{{position={{{wx},{wy}}},radius=10,name={{'water','deepwater'}}}}) do if g then break end;"
        "  local x,y=math.floor(wt.position.x),math.floor(wt.position.y);"
        "  for _,nb in ipairs({{x,y-1,8},{x,y+1,0},{x-1,y,4},{x+1,y,12}}) do local lx,ly,d=nb[1],nb[2],nb[3];"
        "    if not g and not iw(lx,ly) and s.can_place_entity{name='offshore-pump',position={lx+0.5,ly+0.5},direction=d,force=p.force} then"
        "      local e=s.create_entity{name='offshore-pump',position={lx+0.5,ly+0.5},direction=d,force=p.force,player=p};"
        "      if e then inv.remove{name='offshore-pump',count=1}; g={x=lx,y=ly,d=d} end end end end;"
        "if g then rcon.print(g.x..','..g.y..','..g.d) else rcon.print('none') end").strip()
    px, py, pd = map(int, pump.split(","))
    # output tile is opposite the intake direction; build boiler so an END input meets pump water.
    # Place boiler just past the pump output, then bridge water via pipes on its ends until it gets water.
    A._build_boiler_engine(px, py, pd)


def _build_boiler_engine(px, py, pd):
    """Place a boiler near the pump, bridge water to whichever side actually feeds it
    (probed live), fuel it, then seat a steam engine on the steam output - all verified."""
    # output of pump is opposite intake dir; put boiler a couple tiles out along that axis
    dirvec = {0: (0, -1), 8: (0, 1), 4: (1, 0), 12: (-1, 0)}[pd]
    ox, oy = px + dirvec[0], py + dirvec[1]      # pump output tile (gets water)
    # bridge: ensure a water pipe at the output tile
    A.place("pipe", ox, oy, clear=0)
    # boiler one more tile out, then ring its near side with pipes connected to the output pipe
    bx, by = px + dirvec[0] * 3, py + dirvec[1] * 3
    A.place("boiler", bx - 1, by - 1, direction=0, clear=4)
    # probe: place pipes on the boiler's 4 mid-end tiles bridged from the output pipe; keep watered ones
    A._print(
        "/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory();"
        "local b=s.find_entities_filtered{name='boiler',position={" + f"{bx},{by}" + "},radius=3}[1]; if not b then rcon.print('nobl') return end;"
        "local bb=b.bounding_box; local x1,y1,x2,y2=math.floor(bb.left_top.x),math.floor(bb.left_top.y),math.ceil(bb.right_bottom.x)-1,math.ceil(bb.right_bottom.y)-1;"
        "local ring={};"
        "for x=x1-1,x2+1 do ring[#ring+1]={x,y1-1}; ring[#ring+1]={x,y2+1} end;"
        "for y=y1,y2 do ring[#ring+1]={x1-1,y}; ring[#ring+1]={x2+1,y} end;"
        "for _,t in ipairs(ring) do if not string.find(s.get_tile(t[1],t[2]).name,'water') and s.can_place_entity{name='pipe',position={t[1]+0.5,t[2]+0.5},force=p.force} and inv.get_item_count('pipe')>0 then s.create_entity{name='pipe',position={t[1]+0.5,t[2]+0.5},force=p.force}; inv.remove{name='pipe',count=1} end end")
    # fuel boiler
    A._print("/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); local b=s.find_entities_filtered{name='boiler',position={" + f"{bx},{by}" + "},radius=3}[1]; if b then local c=math.min(10,inv.get_item_count('coal')); b.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end")
    time.sleep(6)
    # seat a steam engine adjacent to the boiler's steam side (north) and a pole; verify energy
    A.place("steam-engine", bx - 1, by - 6, direction=0, clear=4)
    A.place("small-electric-pole", bx + 2, by - 4, clear=2)
    time.sleep(4)
    st = A._print("/sc local s=game.surfaces[1]; local e=s.find_entities_filtered{name='steam-engine'}[1]; rcon.print(e and (e.status..'/'..string.format('%.0f',e.energy)) or 'none')").strip()
    A.now(f"Bootstrap: steam engine status/energy = {st}")


def _tech_done(name):
    return A._print(f"/sc rcon.print(tostring(game.forces.player.technologies['{name}'].researched))").strip() == "true"


def _collect_plates():
    """Sweep finished plates out of the base furnaces into inventory."""
    A._print("/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); "
             "for _,fu in pairs(s.find_entities_filtered{area={{-1,-18},{17,-9}},name='stone-furnace'}) do local oi=fu.get_output_inventory(); "
             "for _,it in ipairs({'iron-plate','copper-plate'}) do local a=oi.get_item_count(it); if a>0 then local g=inv.insert{name=it,count=a}; oi.remove{name=it,count=g} end end end")


def _feed_lab_until(tech, packs=("automation-science-pack",), need_each=10, tries=12):
    """Set `tech` researching and keep crafting+feeding the given packs to all labs until it
    completes (robust: tolerates plates not being ready on the first pass)."""
    A._print(f"/sc game.forces.player.add_research('{tech}')")
    for _ in range(tries):
        if _tech_done(tech):
            return True
        _collect_plates()
        for pk in packs:
            _craft_wait(pk, need_each * 2)
        A._print("/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); "
                 "for _,lab in pairs(s.find_entities_filtered{name='lab'}) do for _,pk in ipairs({'" + "','".join(packs) + "'}) do "
                 "local n=math.min(inv.get_item_count(pk),10); if n>0 then lab.insert{name=pk,count=n}; inv.remove{name=pk,count=n} end end end")
        time.sleep(8)
    return _tech_done(tech)


# --------------------------------------------------------------- provisioning (gather-then-craft)
# Seth's rule: NEVER attempt a craft without the ingredients. Figure out the raw needs, gather
# them into inventory (mine ore -> smelt plates -> collect), THEN craft.
MINEABLE = {"coal", "stone", "iron-ore", "copper-ore"}
SMELTED = {"iron-plate": "iron-ore", "copper-plate": "copper-ore"}
BASE_FURNACE_AREA = "{{-1,-18},{17,-9}}"
# Dedicated stacks (Seth's rule): the 8-furnace row smelts IRON, the 4-row below smelts COPPER.
IRON_FURNACE_AREA = "{{-1,-18},{17,-14}}"
COPPER_FURNACE_AREA = "{{-1,-13},{9,-9}}"
FURNACE_AREA = {"iron-ore": IRON_FURNACE_AREA, "copper-ore": COPPER_FURNACE_AREA}


def raw_cost(recipe, count):
    """Recursively expand a recipe into the RAW materials we gather (plates/ores/coal/stone).
    Returns {item: amount}. Stops expanding at smelted plates + mineables."""
    stop = "{" + ",".join("['%s']=true" % r for r in (set(SMELTED) | MINEABLE)) + "}"
    out = A._print(
        "/sc local f=game.forces.player; local STOP=" + stop + "; local acc={};"
        "local function need(name,n) local r=f.recipes[name];"
        "  if STOP[name] or not r or #r.ingredients==0 then acc[name]=(acc[name] or 0)+n; return end;"
        "  for _,i in pairs(r.ingredients) do if i.type=='item' then need(i.name, i.amount*n) end end end;"
        "need('" + recipe + "'," + str(int(count)) + ");"
        "local s={}; for k,v in pairs(acc) do s[#s+1]=k..'='..math.ceil(v) end; rcon.print(table.concat(s,';'))").strip()
    d = {}
    for tok in out.split(";"):
        if "=" in tok:
            k, v = tok.split("="); d[k] = int(v)
    return d


def _collect_plates_all():
    """Sweep finished plates out of EVERY stone furnace on the surface (base rows + the iron/
    copper outpost pairs) into inventory, so accumulated plates are never stranded."""
    A._print("/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); "
             "for _,fu in pairs(s.find_entities_filtered{name='stone-furnace'}) do local oi=fu.get_output_inventory(); "
             "for _,it in ipairs({'iron-plate','copper-plate'}) do local a=oi.get_item_count(it); if a>0 then local g=inv.insert{name=it,count=a}; oi.remove{name=it,count=g} end end end")


def mine_chest(item):
    """Locate the mine-outpost OUTPUT CHEST for a mineable item (a wooden chest near its richest
    patch). Returns (cx, cy, count_in_chest) or None - so the character can HAUL from the chest
    instead of hand-mining (Seth's architecture)."""
    spot = STATE.get(item) or A.richest_spot(item, 0, 0, radius=160)
    if not spot:
        return None
    rx, ry = spot[0], spot[1]
    info = A._print(f"/sc local s=game.surfaces[1]; local c=s.find_entities_filtered{{name='wooden-chest',position={{{rx},{ry}}},radius=26}}[1]; if c then rcon.print(math.floor(c.position.x)..','..math.floor(c.position.y)..','..c.get_inventory(defines.inventory.chest).get_item_count('{item}')) else rcon.print('none') end").strip()
    if "," not in info:
        return None
    cx, cy, n = map(int, info.split(","))
    return (cx, cy, n)


def ensure(item, count):
    """Make sure `count` of a MINEABLE raw (coal/stone/ore) is in inventory. PREFERS hauling from
    the automated mine outpost's output chest (walk + pick up); only hand-mines the richest patch
    if there's no chest / not enough in it (Seth's architecture: mines feed chests, character
    hauls from chests)."""
    if _count(item) >= count:
        return
    gamedb.pull_from_buffer(item, count - _count(item))   # use buffered stock before mining
    if _count(item) >= count:
        return
    mc = mine_chest(item)
    if mc and mc[2] >= (count - _count(item)):
        cx, cy, _ = mc
        A.now(f"Haul: picking up {item} from mine chest @{cx},{cy}")
        A.stop(); A.walk(cx, cy + 1, tol=3.0)
        take = count - _count(item) + 50
        A._print(f"/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); local c=s.find_entities_filtered{{name='wooden-chest',position={{{cx},{cy}}},radius=1}}[1]; if c then local ci=c.get_inventory(defines.inventory.chest); local n=math.min({take},ci.get_item_count('{item}')); local g=inv.insert{{name='{item}',count=n}}; ci.remove{{name='{item}',count=g}} end")
        return
    spot = STATE.get(item) or A.richest_spot(item, 0, 0, radius=160)
    if not spot:
        return
    sx, sy, _ = spot
    A.now(f"Provision: mining {count} {item} @{sx},{sy}")
    A.stop(); A.walk(sx + 1, sy, tol=2.5)
    A.mine(item, count - _count(item) + 20)


def ensure_plates(iron=0, copper=0):
    """Guarantee `iron` iron-plate + `copper` copper-plate in inventory: collect from base
    furnaces, and if still short, mine ore and smelt at the base furnaces until satisfied."""
    _collect_plates_all()
    # use buffered plates first, then furnace collection, then smelting
    if iron > _count("iron-plate"):
        gamedb.pull_from_buffer("iron-plate", iron - _count("iron-plate"))
    if copper > _count("copper-plate"):
        gamedb.pull_from_buffer("copper-plate", copper - _count("copper-plate"))
    for plate, want in (("iron-plate", iron), ("copper-plate", copper)):
        if want <= 0:
            continue
        ore = SMELTED[plate]
        for _ in range(8):
            _collect_plates_all()
            if _count(plate) >= want:
                break
            short = want - _count(plate)
            ensure(ore, short + 20)
            A.now(f"Provision: smelting {short} {plate}")
            A._print("/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); local left=" + str(short + 20) + "; "
                     "for _,fu in pairs(s.find_entities_filtered{area=" + BASE_FURNACE_AREA + ",name='stone-furnace'}) do "
                     "local c=math.min(5,inv.get_item_count('coal')); if c>0 then fu.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end; "
                     "local o=math.min(20,left,inv.get_item_count('" + ore + "')); if o>0 then fu.insert{name='" + ore + "',count=o}; inv.remove{name='" + ore + "',count=o}; left=left-o end end")
            time.sleep(12)
        _collect_plates_all()


def make(recipe, count):
    """PROVISION then craft: compute the raw materials `recipe`x`count` needs, gather them into
    inventory (collect/mine/smelt), then craft. Never blind-fires a craft (Seth's rule)."""
    cost = raw_cost(recipe, count)
    ensure_plates(iron=cost.get("iron-plate", 0), copper=cost.get("copper-plate", 0))
    for raw in MINEABLE:
        if cost.get(raw, 0) > _count(raw):
            ensure(raw, cost[raw])
    return _craft_wait(recipe, count)


def red_science():
    """Lab + hand-crafted red science -> research 'automation' (unlocks assemblers).
    Robust: loops crafting+feeding until automation actually completes."""
    A.now("Bootstrap: lab + red science -> research automation")
    wx, wy = STATE["water"]
    if not _find("lab", wx, wy, 30):
        if _count("lab") < 1:
            _craft_wait("lab", 1)
        A.place("lab", wx + 4, wy - 9, clear=4)
    _feed_lab_until("automation", ("automation-science-pack",))


# --------------------------------------------------------------- generic research driver
def research_chain(target_tech, packs_available=("automation-science-pack",)):
    """Drive research all the way to `target_tech` using the tech DB: walk the prereq chain
    (deps first), and for each tech either (a) auto-pass if it's a craft-item trigger already
    satisfied, (b) flag mine/build triggers that need a physical action, or (c) feed labs the
    required science packs until done. `packs_available` = the science types we can currently
    PRODUCE (extend as green/blue come online). Returns (done, blocked_on)."""
    chain = techdb.prereq_chain(target_tech)
    for t in chain:
        if _tech_done(t):
            continue
        info = techdb.tech(t) or {}
        trig = info.get("trigger")
        if trig:
            # craft-item triggers usually auto-complete from normal play; mine/build need action
            if trig.get("type") == "mine-entity":
                return False, f"{t} (mine {trig.get('entity','?')})"
            time.sleep(2)
            if not _tech_done(t):
                return False, f"{t} (trigger {trig.get('type')})"
            continue
        need = list((info.get("packs") or {}).keys())
        if any(pk not in packs_available for pk in need):
            return False, f"{t} (needs {','.join(need)})"
        A.now(f"Research: {t}")
        if not _feed_lab_until(t, tuple(need)):
            return False, f"{t} (research stalled)"
    return True, None


# --------------------------------------------------------------- remaining phases (proven live, then codified)
# NOTE (Seth's rule): full coverage to the base build is built out phase-by-phase as each is
# proven in a live run, then captured here. Status:
#   DONE+CODED : setup_world, scout, fuel, smelting_base, power, red_science (-> automation)
#   NEXT       : power_to_base (pole line spawn<-plant), automate_science (assemblers: gears,
#                circuits, red+green packs off a small bus), then research_chain to oil-gathering
#   THEN       : oil_economy (pumpjack@oil, refinery, chem plants -> plastic/sulfur, blue science)
#   FINALLY    : research_chain('construction-robotics') + stamp/build the robot-factory blueprint
SMELT_ZONE = {"iron-ore": (-6, 3), "copper-ore": (-6, 12)}   # top-left (plate-belt row) per ore


def build_smelter_array(ore, n=8):
    """Belt-FED smelter array (Seth's design, validated): a row of `n` stone furnaces with a PLATE
    belt above (inserters unload furnaces -> belt) and an ORE belt below (inserters load furnaces
    from belt). Powered by a pole row through the middle. Rows from top-left (ox,oy):
      oy plate-belt | oy+1 plate-inserters | oy+2..oy+3 furnaces | oy+4 ore-inserters | oy+5 ore-belt.
    The ore belt's WEST end is where a mine belt connects; the plate belt's EAST end feeds science.
    Idempotent: skips if furnaces already exist at the zone."""
    ox, oy = SMELT_ZONE[ore]
    if A._print(f"/sc rcon.print(#game.surfaces[1].find_entities_filtered{{name='stone-furnace',area={{{{{ox},{oy+2}}},{{{ox+n*2+2},{oy+3}}}}}}})").strip() not in ("0", ""):
        return
    A.now(f"Belt supply: building {ore} belt-fed smelter array ({n} furnaces)")
    if _count("stone-furnace") < n:
        make("stone-furnace", n - _count("stone-furnace"))
    if _count("inserter") < n * 2:
        make("inserter", n * 2)
    if _count("transport-belt") < n * 4 + 6:
        make("transport-belt", n * 4 + 6)
    if _count("small-electric-pole") < n + 12:
        make("small-electric-pole", n + 12)
    if _count("iron-chest") < 1:
        make("iron-chest", 1)
    A.stop(); A.walk(ox + n, oy - 2, tol=3.0)
    A._print(
        f"/sc local s=game.surfaces[1]; local f=game.forces.player; local ox={ox}; local oy={oy}; local n={n};"
        "for _,e in pairs(s.find_entities_filtered{area={{ox-2,oy-2},{ox+n*2+4,oy+7}},type={'tree','simple-entity'}}) do e.destroy() end;"
        "for x=ox-1,ox+n*2 do s.create_entity{name='transport-belt',position={x+0.5,oy+0.5},direction=4,force=f}; s.create_entity{name='transport-belt',position={x+0.5,oy+5.5},direction=4,force=f} end;"
        # furnaces + inserters with EXPLICIT pickup/drop (direction semantics bit us repeatedly):
        # plate inserter furnace->plate-belt, ore inserter ore-belt->furnace.
        "for k=0,n-1 do local fx=ox+k*2; s.create_entity{name='stone-furnace',position={fx+1,oy+3},force=f};"
        "  local pi=s.create_entity{name='inserter',position={fx+0.5,oy+1.5},direction=8,force=f}; pi.pickup_position={fx+0.5,oy+2.5}; pi.drop_position={fx+0.5,oy+0.5};"
        "  local oi=s.create_entity{name='inserter',position={fx+0.5,oy+4.5},direction=8,force=f}; oi.pickup_position={fx+0.5,oy+5.5}; oi.drop_position={fx+0.5,oy+3.5} end;"
        # FLANKING pole rows: poles CANNOT sit on the furnace row (oy+2..oy+3) - they get refused
        # silently. Put them above the plate belt (oy-1) and below the ore belt (oy+6), every 3, so
        # both inserter rows are in supply range; plus a vertical spine to the base grid (y -2).
        "for x=ox-1,ox+n*2,3 do s.create_entity{name='small-electric-pole',position={x+0.5,oy-0.5},force=f}; s.create_entity{name='small-electric-pole',position={x+0.5,oy+6.5},force=f} end;"
        "for y=-2,oy-1,3 do if s.can_place_entity{name='small-electric-pole',position={ox-0.5,y+0.5},force=f} then s.create_entity{name='small-electric-pole',position={ox-0.5,y+0.5},force=f} end end;"
        # plate-belt DRAIN: chest + inserter (explicit pickup/drop) at the east end so plates don't
        # back up and stall the furnaces (full_output). The autopilot pulls plates from this chest.
        "local ex=ox+n*2; if s.can_place_entity{name='iron-chest',position={ex+2.5,oy+0.5},force=f} then s.create_entity{name='iron-chest',position={ex+2.5,oy+0.5},force=f}; local di=s.create_entity{name='inserter',position={ex+1.5,oy+0.5},direction=12,force=f}; di.pickup_position={ex+0.5,oy+0.5}; di.drop_position={ex+2.5,oy+0.5} end;"
        "rcon.print('array built')")
    ensure_grid_connected()


def lay_belt_path(waypoints):
    """Lay a transport-belt along an L-path of (x,y) CORNER waypoints, SERVER-SIDE (no walk),
    auto-undergrounding blocked spans up to 5 tiles. REPLACES autopilot.build_belt for long
    cross-base runs: build_belt's A* walker snaked and left gaps over 70+ tiles, so the iron/coal
    mine->array belts silently never connected; this lays exact tiles and connects reliably.

    Each tile's direction points toward the NEXT tile, so a corner tile automatically takes the new
    segment's direction. (The bug that silently broke the iron belt: the corner was left in the OLD
    direction, sending items straight past the turn instead of around it. Verified fix: derive the
    direction per-tile from the path, never per-segment.) Returns the count of unbridged gaps."""
    DIRS = {(0, -1): 0, (1, 0): 4, (0, 1): 8, (-1, 0): 12}
    pts = []
    for i in range(len(waypoints) - 1):
        x1, y1 = waypoints[i]
        x2, y2 = waypoints[i + 1]
        dx = (x2 > x1) - (x2 < x1)
        dy = (y2 > y1) - (y2 < y1)
        for k in range(max(abs(x2 - x1), abs(y2 - y1))):
            pts.append((x1 + dx * k, y1 + dy * k))
    pts.append(tuple(waypoints[-1]))
    tiles = []
    for i in range(len(pts) - 1):
        x, y = pts[i]
        nx, ny = pts[i + 1]
        tiles.append((x, y, DIRS[((nx > x) - (nx < x), (ny > y) - (ny < y))]))
    if tiles:
        tiles.append((pts[-1][0], pts[-1][1], tiles[-1][2]))   # last tile keeps prior direction
    spec = ";".join(f"{x},{y},{d}" for (x, y, d) in tiles)
    gaps = A._print(
        "/sc local s=game.surfaces[1]; local f=game.forces.player;"
        "local T={}; for a,b,c in ([==[" + spec + "]==]):gmatch('(-?%d+),(-?%d+),(%d+)') do T[#T+1]={tonumber(a),tonumber(b),tonumber(c)} end;"
        "local function freebelt(x,y) for _,e in pairs(s.find_entities_filtered{position={x+0.5,y+0.5},radius=0.6,type={'tree','simple-entity','cliff'}}) do if e.destroy then e.destroy() end end; return s.can_place_entity{name='transport-belt',position={x+0.5,y+0.5},force=f} end;"
        "local gaps=0; local i=1;"
        "while i<=#T do local x,y,d=T[i][1],T[i][2],T[i][3];"
        "  if freebelt(x,y) then local old=s.find_entity('transport-belt',{x+0.5,y+0.5}); if old then old.destroy() end; s.create_entity{name='transport-belt',position={x+0.5,y+0.5},direction=d,force=f}; i=i+1;"
        "  else local j=i+1; while j<=#T and not freebelt(T[j][1],T[j][2]) do j=j+1 end;"
        "    if i>1 and j<=#T and (j-(i-1))<=5 then local p=T[i-1]; local old=s.find_entity('transport-belt',{p[1]+0.5,p[2]+0.5}); if old then old.destroy() end;"
        "      pcall(function() s.create_entity{name='underground-belt',position={p[1]+0.5,p[2]+0.5},direction=p[3],type='input',force=f} end);"
        "      pcall(function() s.create_entity{name='underground-belt',position={T[j][1]+0.5,T[j][2]+0.5},direction=T[j][3],type='output',force=f} end);"
        "    else gaps=gaps+1 end; i=j+1 end end;"
        "rcon.print(gaps)").strip()
    return int(gaps or 0)


def connect_mine_to_array(ore):
    """Reconfigure a mine's output to feed a BELT to its smelter array's ore belt: remove the
    output inserter+chest, then lay_belt_path from the mine belt end to the array's ore-belt west
    end. Frees derpface from hauling this ore. Uses the codified server-side layer (NOT build_belt,
    which left the iron/coal belts disconnected)."""
    ox, oy = SMELT_ZONE[ore]
    spot = STATE.get(ore) or A.richest_spot(ore, 0, 0, radius=160)
    if not spot:
        return
    rx, ry = int(spot[0]), int(spot[1])
    # remove the mine's output inserter + chest (refund) so the belt runs through instead
    A._print(f"/sc local s=game.surfaces[1]; local inv=storage.derpface.get_main_inventory(); "
             f"for _,e in pairs(s.find_entities_filtered{{position={{{rx},{ry}}},radius=26,name={{'burner-inserter','inserter','wooden-chest'}}}}) do "
             "local ci=e.get_inventory and e.get_inventory(defines.inventory.chest); if ci then for _,c in pairs(ci.get_contents()) do inv.insert{name=c.name,count=c.count} end end; inv.insert{name=e.name,count=1}; e.destroy() end")
    # L-path: from the mine output, run to the array's column, then into the ore-belt west end.
    # Two-segment L via the array's x just outside the array, then up/down to the ore belt row.
    ax, ay = ox - 1, oy + 5
    lay_belt_path([(rx + 10, ry), (ax - 1, ry), (ax - 1, ay), (ax, ay)])


def build_belt_supply():
    """Orchestrate the belt-fed supply (Seth): build iron + copper smelter arrays, connect each
    mine to its array by belt (no more character ore-hauling), run a coal belt to the arrays, and
    a plate belt from the arrays to a science feed chest. Large; runs as a queued build task on
    Charon so derpface builds it. Iron array may already exist (built + validated by hand)."""
    build_smelter_array("iron-ore", 16)
    build_smelter_array("copper-ore", 12)
    connect_mine_to_array("iron-ore")
    connect_mine_to_array("copper-ore")
    # coal belt from the coal mine down to the arrays (codified layer, not build_belt)
    cs = STATE.get("coal")
    if cs:
        ox, oy = SMELT_ZONE["iron-ore"]
        lay_belt_path([(int(cs[0]) + 10, int(cs[1])), (ox - 2, int(cs[1])), (ox - 2, oy + 6)])


STEEL_STACK = (14, 6)   # (ox,oy) of the steel-processing stack: output belt oy, input belt oy+5


def build_steel_stack(n=4):
    """Build the STEEL-PROCESSING stack (Seth's design): a belt-fed array fed IRON PLATES (any
    furnace smelts iron-plate -> steel-plate automatically), tapped off the iron array's plate belt
    by a SPLITTER so the existing plate routing is unchanged. Same geometry as build_smelter_array
    (output belt oy / furnaces oy+2..3 / input belt oy+5), flank poles, a steel-plate drain chest.
    Starts with stone furnaces (upgrade_furnaces_to_steel converts them once steel plates flow).
    Idempotent: skips if furnaces already exist at the zone. fuel_arrays fuels it; harvest_array_
    plates pulls its steel output."""
    ox, oy = STEEL_STACK
    if A._print(f"/sc rcon.print(#game.surfaces[1].find_entities_filtered{{name={{'stone-furnace','steel-furnace'}},area={{{{{ox},{oy+2}}},{{{ox+n*2+2},{oy+3}}}}}}})").strip() not in ("0", ""):
        return
    A.now(f"Steel: building steel-processing stack ({n} furnaces, iron-plate -> steel-plate)")
    A._print(
        f"/sc local s=game.surfaces[1]; local f=game.forces.player; local ox={ox}; local oy={oy}; local n={n};"
        "for _,e in pairs(s.find_entities_filtered{area={{ox-2,oy-2},{ox+n*2+4,oy+7}},type={'tree','simple-entity'}}) do e.destroy() end;"
        "for x=ox-1,ox+n*2 do s.create_entity{name='transport-belt',position={x+0.5,oy+0.5},direction=4,force=f}; s.create_entity{name='transport-belt',position={x+0.5,oy+5.5},direction=4,force=f} end;"
        "for k=0,n-1 do local fx=ox+k*2; s.create_entity{name='stone-furnace',position={fx+1,oy+3},force=f};"
        "  local pi=s.create_entity{name='inserter',position={fx+0.5,oy+1.5},direction=8,force=f}; pi.pickup_position={fx+0.5,oy+2.5}; pi.drop_position={fx+0.5,oy+0.5};"
        "  local oi=s.create_entity{name='inserter',position={fx+0.5,oy+4.5},direction=8,force=f}; oi.pickup_position={fx+0.5,oy+5.5}; oi.drop_position={fx+0.5,oy+3.5} end;"
        "for x=ox-1,ox+n*2,3 do s.create_entity{name='small-electric-pole',position={x+0.5,oy-0.5},force=f}; s.create_entity{name='small-electric-pole',position={x+0.5,oy+6.5},force=f} end;"
        "local ex=ox+n*2; s.create_entity{name='iron-chest',position={ex+2.5,oy+0.5},force=f}; local di=s.create_entity{name='inserter',position={ex+1.5,oy+0.5},direction=12,force=f}; di.pickup_position={ex+0.5,oy+0.5}; di.drop_position={ex+2.5,oy+0.5};"
        "rcon.print('steel stack built')")
    # splitter on the iron plate belt (y3, east end) -> one output continues, the other branches here
    ipy = SMELT_ZONE["iron-ore"][0]   # iron plate belt row is SMELT_ZONE iron oy = 3
    A._print(
        "/sc local s=game.surfaces[1]; local f=game.forces.player;"
        "for _,e in pairs(s.find_entities_filtered{area={{10,3},{12,5}},name='transport-belt'}) do e.destroy() end;"
        "if s.can_place_entity{name='splitter',position={10.5,4.0},direction=4,force=f} then s.create_entity{name='splitter',position={10.5,4.0},direction=4,force=f};"
        "  for x=11,13 do s.create_entity{name='transport-belt',position={x+0.5,3.5},direction=4,force=f} end;"
        "  s.create_entity{name='iron-chest',position={14.5,3.5},force=f}; local di=s.create_entity{name='inserter',position={13.5,2.5},direction=0,force=f}; di.pickup_position={13.5,3.5}; di.drop_position={14.5,3.5} end")
    lay_belt_path([(11, 4), (11, oy + 5), (ox - 1, oy + 5)])   # splitter branch -> steel input belt
    ensure_grid_connected()


def upgrade_furnaces_to_steel():
    """Convert the belt-fed array + steel-stack STONE furnaces to STEEL furnaces IN-PLACE (2x speed,
    2x fuel efficiency; identical 2x2 burner footprint, so belts/inserters/coal are unchanged).
    Captures each furnace's fuel + output, destroys it, creates a steel-furnace at the exact
    position, restores the items. Consumes steel-furnace items from derpface's inventory (craft from
    steel plates the steel stack produces), so it converts gradually as steel furnaces become
    available - call every maintenance lap; it no-ops when derpface has none."""
    A._print(
        "/sc local p=storage.derpface; if not (p and p.valid) then return end; local s=p.surface; local f=p.force; local inv=p.get_main_inventory();"
        "for _,z in ipairs({{{-8,4},{12,7}},{{-8,13},{12,16}},{{13,7},{24,10}}}) do"
        "  for _,fc in pairs(s.find_entities_filtered{name='stone-furnace',area=z}) do"
        "    if inv.get_item_count('steel-furnace')>0 then local pos=fc.position; local keep={};"
        "      local fi=fc.get_fuel_inventory(); if fi then for _,it in ipairs({'coal'}) do local c=fi.get_item_count(it); if c>0 then keep[it]=(keep[it] or 0)+c end end end;"
        "      local oi=fc.get_output_inventory(); if oi then for it,c in pairs(oi.get_contents()) do local nm=(type(it)=='table' and it.name or it); keep[nm]=(keep[nm] or 0)+(type(c)=='table' and c.count or c) end end;"
        "      fc.destroy(); local nf=s.create_entity{name='steel-furnace',position=pos,force=f};"
        "      if nf then for it,c in pairs(keep) do pcall(function() nf.insert{name=it,count=c} end) end; inv.remove{name='steel-furnace',count=1} end end end end")


def build_mine_outpost(ore, n=8):
    """Seth's supply architecture: a SCALED row of `n` burner drills all dropping onto ONE belt
    that runs east to a single OUTPUT CHEST loaded by a burner inserter. NO furnaces here -
    smelting stays at the base; the character hauls ore from this chest to the base smelter array
    on maintenance runs (haul_ore). Returns the output-chest tile (cx,cy)."""
    spot = STATE.get(ore) or A.richest_spot(ore, 0, 0, radius=160)
    if not spot:
        return None
    rx, ry, _ = spot
    # Already a CLEAN outpost (belt + chest, and NO furnaces - smelting is base-only)? then skip.
    state = A._print(f"/sc local s=game.surfaces[1]; rcon.print(#s.find_entities_filtered{{name='transport-belt',position={{{rx},{ry}}},radius=22}}..','..#s.find_entities_filtered{{name='stone-furnace',position={{{rx},{ry}}},radius=22}})").strip()
    nbelt, nfurn = (int(state.split(",")[0]), int(state.split(",")[1])) if "," in state else (0, 1)
    if nbelt > 0 and nfurn == 0:
        cc = A._print(f"/sc local s=game.surfaces[1]; local c=s.find_entities_filtered{{name='wooden-chest',position={{{rx},{ry}}},radius=24}}[1]; rcon.print(c and (math.floor(c.position.x)..','..math.floor(c.position.y)) or 'none')").strip()
        return tuple(map(int, cc.split(","))) if "," in cc else None
    A.now(f"Supply: scaled MINE outpost for {ore} ({n} drills -> belt -> chest) @{rx},{ry}")
    # PROVISION FIRST (while any old furnaces at the patch still produce plates to craft from)
    if _count("burner-mining-drill") < n:
        make("burner-mining-drill", n - _count("burner-mining-drill"))
    if _count("transport-belt") < n * 2 + 8:
        make("transport-belt", n * 2 + 8)
    if _count("burner-inserter") < 1:
        make("burner-inserter", 1)
    if _count("wooden-chest") < 1:
        make("wooden-chest", 1)
    ensure("coal", n * 15 + 60)
    A.stop(); A.walk(rx, ry - 5, tol=3.0)
    # CLEAN SLATE (Seth's rule: no furnaces at mine outposts, base smelts exclusively). Refund
    # any pre-existing furnaces/drills/belts/inserters/chests at the patch so old tangled builds
    # don't block the belt; then build a fresh consolidated mine.
    A._print(f"/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); "
             f"for _,e in pairs(s.find_entities_filtered{{position={{{rx},{ry}}},radius=24,name={{'stone-furnace','burner-mining-drill','transport-belt','burner-inserter','wooden-chest'}}}}) do "
             "local fb=e.get_fuel_inventory(); if fb then for _,c in pairs(fb.get_contents()) do inv.insert{name=c.name,count=c.count} end end; "
             "local oi=e.get_output_inventory(); if oi then for _,c in pairs(oi.get_contents()) do inv.insert{name=c.name,count=c.count} end end; "
             "inv.insert{name=e.name,count=1}; e.destroy() end")
    A.clear_area(rx, ry, n + 18)
    # place drills facing south in a row; read each drop tile
    drops = []
    for k in range(n):
        dx = rx - n + 2 * k
        A.place("burner-mining-drill", dx, ry - 2, direction=8, clear=0)
        d = A._print(f"/sc local s=game.surfaces[1]; local dr=s.find_entities_filtered{{name='burner-mining-drill',position={{{dx+1},{ry-1}}},radius=2}}[1]; if dr then rcon.print(math.floor(dr.drop_position.x)..','..math.floor(dr.drop_position.y)) else rcon.print('none') end").strip()
        if "," in d:
            drops.append(tuple(map(int, d.split(","))))
    if not drops:
        return None
    belt_y = max(set(fy for _, fy in drops), key=[fy for _, fy in drops].count)
    x0 = min(fx for fx, _ in drops)
    x1 = max(fx for fx, _ in drops) + 3                    # extend east for the inserter+chest
    for x in range(x0, x1 + 1):                            # ONE continuous east belt under the drops
        A.place("transport-belt", x, belt_y, direction=4, clear=0)
    A.place("burner-inserter", x1 + 1, belt_y, direction=12, clear=0)   # picks ore off belt (west), drops east
    A.place("wooden-chest", x1 + 2, belt_y, clear=0)
    # fuel all burners at the outpost
    A._print("/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); "
             f"for _,e in pairs(s.find_entities_filtered{{area={{{{{rx-n-3},{ry-4}}},{{{x1+3},{belt_y+2}}}}},name={{'burner-mining-drill','burner-inserter'}}}}) do "
             "local fb=e.get_fuel_inventory(); local need=20-(fb and fb.get_item_count('coal') or 0); local c=math.min(need,inv.get_item_count('coal')); if c>0 then e.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end end")
    return (x1 + 2, belt_y)


def build_outpost(ore, n=6):
    """Automated SUPPLY: a row of `n` burner drill -> stone furnace pairs on the richest patch
    of `ore`, so plates are produced CONTINUOUSLY (no electricity needed - burner powered). The
    maintain loop's _collect_plates_all sweeps the plates. Drills+furnaces are coal-loaded now;
    coal delivery to remote outposts is the next supply step. Idempotent-ish: skips if a furnace
    row already exists at the patch."""
    spot = STATE.get(ore) or A.richest_spot(ore, 0, 0, radius=160)
    if not spot:
        return
    rx, ry, _ = spot
    plate = "iron-plate" if ore == "iron-ore" else "copper-plate"
    have = int(A._print(f"/sc local s=game.surfaces[1]; rcon.print(#s.find_entities_filtered{{name='stone-furnace',position={{{rx},{ry}}},radius=16}})").strip() or 0)
    if have >= n:
        return  # outpost already has enough pairs
    A.now(f"Supply: building {n}x drill->furnace outpost for {ore} @{rx},{ry} (have {have})")
    # provision the buildings + fuel
    if _count("burner-mining-drill") < n:
        make("burner-mining-drill", n - _count("burner-mining-drill"))
    if _count("stone-furnace") < n:
        make("stone-furnace", n - _count("stone-furnace"))
    ensure("coal", n * 20 + 40)
    A.stop(); A.walk(rx, ry - 4, tol=3.0)
    A.clear_area(rx + n, ry + 2, n + 12)
    for k in range(n):
        dx = rx - n + k * 2          # drills in a row, each 2 wide, centred on the patch
        A.place("burner-mining-drill", dx, ry - 1, direction=8, clear=0)
        drop = A._print(f"/sc local s=game.surfaces[1]; local d=s.find_entities_filtered{{name='burner-mining-drill',position={{{dx+1},{ry}}},radius=2}}[1]; if d then rcon.print(math.floor(d.drop_position.x)..','..math.floor(d.drop_position.y)) else rcon.print('none') end").strip()
        if "," in drop:
            fx, fy = map(int, drop.split(","))
            A.place("stone-furnace", fx - 1, fy, clear=0)
    # fuel everything in the outpost from carried coal
    A._print("/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); "
             f"for _,e in pairs(s.find_entities_filtered{{area={{{{{rx-n-2},{ry-4}}},{{{rx+n+2},{ry+4}}}}},name={{'burner-mining-drill','stone-furnace'}}}}) do "
             "local fb=e.get_fuel_inventory(); local need=15-(fb and fb.get_item_count('coal') or 0); local c=math.min(need,inv.get_item_count('coal')); if c>0 then e.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end end")


def power_to_base(spacing=6):
    """Run a small-electric-pole line from the steam-plant POLE straight to the spawn base so
    assemblers/labs there get power. Poles every `spacing` tiles (< 7.5 wire reach) ON the line
    from the plant pole to the base, so it never gaps (a y-only line missed the plant by ~9)."""
    bx, by = SPAWN
    # anchor on an existing plant pole so we extend the same network, not a parallel one
    src = A._print("/sc local s=game.surfaces[1]; local eng=s.find_entities_filtered{name='steam-engine'}[1]; if not eng then rcon.print('none') return end; local p=s.find_entities_filtered{name='small-electric-pole',position=eng.position,radius=6}[1]; rcon.print(p and (math.floor(p.position.x)..','..math.floor(p.position.y)) or 'none')").strip()
    if "," not in src:
        return
    sx, sy = map(int, src.split(","))
    A.now("Bootstrap: power line plant -> spawn base")
    import math
    dist = math.hypot(bx - sx, by - sy)
    steps = max(1, int(dist // spacing))
    if _count("small-electric-pole") < steps + 2:
        _craft_wait("small-electric-pole", steps + 2)
    for i in range(1, steps + 1):
        x = round(sx + (bx - sx) * i / steps)
        y = round(sy + (by - sy) * i / steps)
        A.place("small-electric-pole", x, y, clear=2)


def coal_buffer():
    """Give the boiler a COAL BUFFER so it never starves before auto-mining exists (Seth's
    rule): a chest + a burner inserter that feeds coal from the chest into the boiler. The
    burner inserter self-fuels from the coal it carries. Auto-finds a free adjacent tile pair.
    Idempotent (skips if a burner inserter already feeds the boiler)."""
    bx, by = A._print("/sc local s=game.surfaces[1]; local b=s.find_entities_filtered{name='boiler'}[1]; rcon.print(b and (math.floor(b.position.x)..','..math.floor(b.position.y)) or 'none')").strip().split(",") if _find("boiler", STATE["water"][0], STATE["water"][1], 30) else (None, None)
    if bx is None:
        return
    bx, by = int(bx), int(by)
    if _find("burner-inserter", bx, by, 4):
        return
    A.now("Bootstrap: coal buffer (chest + burner inserter) on boiler")
    # The inserter MUST sit ON a tile adjacent to the boiler (so it drops INTO the boiler),
    # with the chest one tile further out (so it picks FROM the chest). dir = away-from-boiler
    # = the chest side. LESSON (Seth): an earlier version placed the inserter a tile off (drop
    # landed in a gap) and a NEW empty chest instead of reusing one - so reuse a chest if one is
    # already adjacent, and verify inserter adjacency to the boiler tile.
    spot = A._print(
        "/sc local s=game.surfaces[1]; local p=storage.derpface; local b=s.find_entities_filtered{name='boiler'}[1]; local bb=b.bounding_box;"
        "local x1,y1,x2,y2=math.floor(bb.left_top.x),math.floor(bb.left_top.y),math.ceil(bb.right_bottom.x)-1,math.ceil(bb.right_bottom.y)-1;"
        "local function placeable(name,x,y) return s.can_place_entity{name=name,position={x+0.5,y+0.5},force=p.force} end;"
        "local function chestat(x,y) return s.find_entities_filtered{name='wooden-chest',position={x+0.5,y+0.5},radius=0.6}[1] end;"
        "local cand={};"  # {inserter_x, inserter_y, chest_x, chest_y, dir(toward chest)}
        "for x=x1,x2 do cand[#cand+1]={x,y1-1,x,y1-2,0}; cand[#cand+1]={x,y2+1,x,y2+2,8} end;"
        "for y=y1,y2 do cand[#cand+1]={x1-1,y,x1-2,y,12}; cand[#cand+1]={x2+1,y,x2+2,y,4} end;"
        "for _,c in ipairs(cand) do local ins_ok=placeable('burner-inserter',c[1],c[2]); local ex=chestat(c[3],c[4]);"
        "  if ins_ok and (ex or placeable('wooden-chest',c[3],c[4])) then rcon.print(c[1]..','..c[2]..','..c[3]..','..c[4]..','..c[5]..','..(ex and 1 or 0)) return end end;"
        "rcon.print('none')").strip()
    if spot == "none":
        return
    ix, iy, cx, cy, d, reuse = map(int, spot.split(","))
    make("burner-inserter", 1)
    A.stop(); A.walk(cx, cy + 1, tol=3.0)
    if not reuse:
        make("wooden-chest", 1)
        A.place("wooden-chest", cx, cy, clear=0)
    A.place("burner-inserter", ix, iy, direction=d, clear=0)
    refill_buffers()
    # starter fuel for the inserter (it self-fuels from the coal it then carries)
    A._print("/sc local s=game.surfaces[1]; local inv=storage.derpface.get_main_inventory(); local ins=s.find_entities_filtered{name='burner-inserter',position={" + f"{ix},{iy}" + "},radius=1}[1]; if ins and inv.get_item_count('coal')>=2 then ins.insert{name='coal',count=2}; inv.remove{name='coal',count=2} end")


def refill_buffers(threshold=0.2):
    """Top up every buffer chest that is below `threshold` full of its resource (Seth's rule:
    refill at <20%). Currently: coal buffer chests next to boilers. Mines coal if the player is
    short. Designed to be called every maintenance lap so buffers never run dry."""
    # which chests are buffers + how full (a chest next to a boiler is a coal buffer)
    low = A._print(
        "/sc local s=game.surfaces[1]; local out={};"
        "for _,ch in pairs(s.find_entities_filtered{name='wooden-chest'}) do "
        "  local nearb=#s.find_entities_filtered{name='boiler',position=ch.position,radius=3}>0;"
        "  if nearb then local inv=ch.get_inventory(defines.inventory.chest); local coal=inv.get_item_count('coal'); local cap=inv.get_bar()>0 and (inv.get_bar()-1)*50 or #inv*50;"
        "    if coal < cap*" + str(threshold) + " then out[#out+1]=math.floor(ch.position.x)..','..math.floor(ch.position.y)..','..(cap-coal) end end end;"
        "rcon.print(table.concat(out,';'))").strip()
    if not low or low == "":
        return
    for tok in low.split(";"):
        if "," not in tok:
            continue
        cx, cy, need = map(int, tok.split(","))
        if _count("coal") < need:
            ensure("coal", need)
        A._print("/sc local s=game.surfaces[1]; local inv=storage.derpface.get_main_inventory(); local ch=s.find_entities_filtered{name='wooden-chest',position={" + f"{cx},{cy}" + "},radius=1}[1]; if ch then local n=math.min(" + str(need) + ",inv.get_item_count('coal')); if n>0 then ch.insert{name='coal',count=n}; inv.remove{name='coal',count=n} end end")


def restock_coal(low=40, target=150):
    """Keep 6-12 stacks of coal in the character's inventory (Seth's rule) by hauling from the
    COAL MINE output chest, so refueling everything is one local insert (no per-refuel trip).
    Falls back to hand-mining only if there's no coal mine chest yet."""
    # visit if the character is low on coal OR the coal mine's own drills are low on fuel
    need = _outpost_needs("coal")
    coal_drills_low = need and need[3] < 8
    if _count("coal") >= low and not coal_drills_low:
        return
    mc = mine_chest("coal")
    if mc and mc[2] > 0:
        cx, cy, _ = mc
        rx, ry = STATE["coal"][0], STATE["coal"][1]
        A.now(f"Restock: ALL coal from coal mine chest @{cx},{cy} (+refuel coal drills)")
        A.stop(); A.walk(cx, cy + 1, tol=3.0)
        # take ALL the coal in the chest (Seth: minimize trips back), AND refuel the coal mine's
        # own drills (they don't self-fuel reliably)
        A._print(f"/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); "
                 f"local c=s.find_entities_filtered{{name='wooden-chest',position={{{cx},{cy}}},radius=1}}[1]; if c then local ci=c.get_inventory(defines.inventory.chest); local n=ci.get_item_count('coal'); if n>0 then local g=inv.insert{{name='coal',count=n}}; ci.remove{{name='coal',count=g}} end end; "
                 f"for _,d in pairs(s.find_entities_filtered{{position={{{rx},{ry}}},radius=24,name={{'burner-mining-drill','burner-inserter'}}}}) do local fb=d.get_fuel_inventory(); local need=25-(fb and fb.get_item_count('coal') or 0); local k=math.min(need,inv.get_item_count('coal')); if k>0 then d.insert{{name='coal',count=k}}; inv.remove{{name='coal',count=k}} end end")
    else:
        ensure("coal", target)


def _outpost_needs(ore):
    """Return (chest_x, chest_y, ore_in_chest, min_drill_fuel) for an ore outpost, or None."""
    spot = STATE.get(ore)
    if not spot:
        return None
    rx, ry = spot[0], spot[1]
    info = A._print(f"/sc local s=game.surfaces[1]; local c=s.find_entities_filtered{{name='wooden-chest',position={{{rx},{ry}}},radius=26}}[1]; if not c then rcon.print('none') return end; "
                    f"local mf=999; for _,d in pairs(s.find_entities_filtered{{position={{{rx},{ry}}},radius=26,name='burner-mining-drill'}}) do local fb=d.get_fuel_inventory(); local f=fb and fb.get_item_count('coal') or 0; if f<mf then mf=f end end; "
                    f"rcon.print(math.floor(c.position.x)..','..math.floor(c.position.y)..','..c.get_inventory(defines.inventory.chest).get_item_count('{ore}')..','..mf)").strip()
    if "," not in info:
        return None
    return tuple(map(int, info.split(",")))


def haul_ore(min_trip=30, per_trip=600, low_fuel=8):
    """Maintenance run (Seth's design): keep coal stocked, then visit each mine outpost when it
    has ore to haul OR its drills are low on fuel, REFUEL all its burners from carried coal, and
    carry the ore back to the base smelter array. Refueling proactively (not only on ore trips)
    avoids the starve deadlock (dry drill -> no ore -> no trip -> never refueled)."""
    restock_coal()
    for ore in ("iron-ore", "copper-ore"):
        need = _outpost_needs(ore)
        if not need:
            continue
        cx, cy, have, minfuel = need
        rx, ry = STATE[ore][0], STATE[ore][1]
        if have < min_trip and minfuel >= low_fuel:
            continue                                   # nothing to haul + fuel is fine
        A.now(f"Haul+refuel {ore} outpost (ore={have}, minfuel={minfuel})")
        A.stop(); A.walk(cx, cy + 1, tol=3.0)
        # refuel ALL outpost burners to ~25, and take the ore
        A._print(f"/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); "
                 f"for _,e in pairs(s.find_entities_filtered{{position={{{rx},{ry}}},radius=26,name={{'burner-mining-drill','burner-inserter'}}}}) do local fb=e.get_fuel_inventory(); local need=25-(fb and fb.get_item_count('coal') or 0); local k=math.min(need,inv.get_item_count('coal')); if k>0 then e.insert{{name='coal',count=k}}; inv.remove{{name='coal',count=k}} end end; "
                 f"local c=s.find_entities_filtered{{name='wooden-chest',position={{{cx},{cy}}},radius=1}}[1]; if c then local ci=c.get_inventory(defines.inventory.chest); local n=math.min({per_trip},ci.get_item_count('{ore}')); local g=inv.insert{{name='{ore}',count=n}}; ci.remove{{name='{ore}',count=g}} end")
        bx, by = SPAWN
        A.stop(); A.walk(bx, by + 4, tol=3.0)
        # load the ore into ITS dedicated furnace stack (iron-> 8-row, copper-> 4-row)
        A._print(f"/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); "
                 f"for _,fu in pairs(s.find_entities_filtered{{area={FURNACE_AREA[ore]},name='stone-furnace'}}) do "
                 "local cc=math.min(5,inv.get_item_count('coal')); if cc>0 then fu.insert{name='coal',count=cc}; inv.remove{name='coal',count=cc} end; "
                 f"local o=math.min(50,inv.get_item_count('{ore}')); if o>0 then fu.insert{{name='{ore}',count=o}}; inv.remove{{name='{ore}',count=o}} end end")


# scaled chain (more cable/circuit/inserter/belt/green so green volume keeps all labs running)
SCIENCE_CHAIN = ["iron-gear-wheel", "copper-cable", "copper-cable", "electronic-circuit",
                 "electronic-circuit", "inserter", "inserter", "transport-belt", "transport-belt",
                 "automation-science-pack", "automation-science-pack",
                 "logistic-science-pack", "logistic-science-pack", "logistic-science-pack",
                 "logistic-science-pack"]
SCIENCE_CELL = (0, -34)   # top-left of the I/O-chest science GRID
SCIENCE_COLS = 5          # cells per row -> a compact grid (not one long row) to minimize runs


def build_io_cell(recipe, x, y):
    """One assembler UNIT with input/output chests + inserters (Seth's rule). Layout (7 wide,
    mid-row y+1): [input chest][in inserter][assembler 3x3][out inserter][output chest], + a
    pole. The inserters push/pull from the chests (hardware); maintenance fills inputs + empties
    outputs. Returns True if the assembler was built."""
    A.place("wooden-chest", x, y + 1, clear=0)                       # input chest
    A.place("inserter", x + 1, y + 1, direction=12, clear=0)         # in: pick W chest, drop E asm
    r = A.place("assembling-machine-1", x + 2, y, clear=0).strip()   # assembler (3x3)
    A.place("inserter", x + 5, y + 1, direction=12, clear=0)         # out: pick W asm, drop E chest
    A.place("wooden-chest", x + 6, y + 1, clear=0)                   # output chest
    A.place("small-electric-pole", x + 3, y + 3, clear=0)
    if "BUILT" in r:
        A._print(f"/sc local s=game.surfaces[1]; local a=s.find_entities_filtered{{name='assembling-machine-1',position={{{x+3},{y+1}}},radius=2}}[1]; if a then pcall(function() a.set_recipe('{recipe}') end) end")
        return True
    return False


def power_row(x1, x2, y, spacing=5):
    """Lay a CONTINUOUS pole line from x1..x2 at row y (poles <= `spacing` apart so the wires
    always chain - the #1 cause of 'new machines unpowered'), bridge it to the base network, then
    verify and patch any still-unpowered machine nearby. Returns count of unpowered remaining."""
    if _count("small-electric-pole") < (x2 - x1) // spacing + 6:
        make("small-electric-pole", (x2 - x1) // spacing + 6)
    A.stop(); A.walk(x1, y + 2, tol=3.0)
    for x in range(x1, x2 + 1, spacing):
        A.place("small-electric-pole", x, y, clear=1)
    # bridge the new line to the nearest base-network pole if it's a separate network
    A._print(
        "/sc local s=game.surfaces[1]; local p=storage.derpface; local inv=p.get_main_inventory();"
        "local eng=s.find_entities_filtered{name='steam-engine'}[1]; if not eng then return end; local enet=eng.electric_network_id;"
        "local rp=s.find_entities_filtered{type='electric-pole',position={" + f"{(x1+x2)//2},{y}" + "},radius=8}[1]; if not rp or rp.electric_network_id==enet then return end;"
        # walk a chain of poles from the row pole toward a base-network pole
        "local bp,bd; for _,q in pairs(s.find_entities_filtered{type='electric-pole'}) do if q.electric_network_id==enet then local d=(q.position.x-rp.position.x)^2+(q.position.y-rp.position.y)^2; if not bd or d<bd then bd=d; bp=q end end end;"
        "if bp then local steps=math.ceil(math.sqrt(bd)/6); for i=1,steps do local x=math.floor(rp.position.x+(bp.position.x-rp.position.x)*i/steps); local yy=math.floor(rp.position.y+(bp.position.y-rp.position.y)*i/steps); if s.can_place_entity{name='small-electric-pole',position={x+0.5,yy+0.5},force=p.force} and inv.get_item_count('small-electric-pole')>0 then s.create_entity{name='small-electric-pole',position={x+0.5,yy+0.5},force=p.force}; inv.remove{name='small-electric-pole',count=1} end end end")
    time.sleep(2)
    np = int(A._print(f"/sc local s=game.surfaces[1]; local n=0; for _,e in pairs(s.find_entities_filtered{{area={{{{{x1-2},{y-5}}},{{{x2+2},{y+2}}}}},type={{'assembling-machine','lab','inserter'}}}}) do if e.prototype.electric_energy_source_prototype and e.status==58 then n=n+1 end end; rcon.print(n)").strip() or 0)
    return np


def setup_science_io():
    """Rebuild the science chain as I/O-chest cells (Seth's directive). Builds a fresh spaced row
    where every assembler has input+output chests/inserters, sets recipes, powers it, then tears
    down the old tightly-packed assemblers (refund). Idempotent: skips if cells already exist."""
    bx, by = SCIENCE_CELL
    if A._print(f"/sc local s=game.surfaces[1]; rcon.print(#s.find_entities_filtered{{name='assembling-machine-1',position={{{bx+30},{by+1}}},radius=40}})").strip() not in ("0", ""):
        # count cells with adjacent chests as the marker we've already converted
        done = A._print(f"/sc local s=game.surfaces[1]; local n=0; for _,a in pairs(s.find_entities_filtered{{name='assembling-machine-1',area={{{{{bx-2},{by-2}}},{{{bx+80},{by+5}}}}}}}) do n=n+1 end; rcon.print(n)").strip()
        if int(done or 0) >= len(SCIENCE_CHAIN):
            return
    n = len(SCIENCE_CHAIN)
    A.now("Build task: rebuild science as I/O-chest cells")
    need_asm = n - _count("assembling-machine-1")
    if need_asm > 0:
        make("assembling-machine-1", need_asm)
    if _count("wooden-chest") < n * 2:
        make("wooden-chest", n * 2)
    if _count("inserter") < n * 2:
        make("inserter", n * 2)
    if _count("small-electric-pole") < n:
        make("small-electric-pole", n)
    cols = SCIENCE_COLS
    nrows = (n + cols - 1) // cols
    A.stop(); A.walk(bx, by + 5, tol=3.0)
    A.clear_area(bx + cols * 4, by + nrows * 2, cols * 4 + 12)
    # COMPACT GRID (Seth: stacked rows, not one long row) - minimizes character run distance.
    for k, recipe in enumerate(SCIENCE_CHAIN):
        col, row = k % cols, k // cols
        build_io_cell(recipe, bx + col * 8, by + row * 5)
    # CONNECT POWER per row: continuous pole lines (<=5 apart so wires chain), bridged + verified.
    for row in range(nrows):
        power_row(bx, bx + cols * 8, by + row * 5 + 3)
    # tear down the OLD scattered science assemblers (anything making a chain recipe outside the
    # new cell row) + their stray chests/inserters, refunding to inventory
    chainset = "{" + ",".join("['%s']=true" % r for r in set(SCIENCE_CHAIN)) + "}"
    A._print(f"/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); local CH={chainset}; "
             f"for _,a in pairs(s.find_entities_filtered{{type='assembling-machine'}}) do local r=a.get_recipe(); "
             f"if r and CH[r.name] and a.position.y > {by+6} then "
             "local oi=a.get_output_inventory(); if oi then for _,c in pairs(oi.get_contents()) do inv.insert{name=c.name,count=c.count} end end; "
             "inv.insert{name='assembling-machine-1',count=1}; a.destroy() end end")
    dedupe_poles()


def _service_assembler_chests():
    """Fill each science assembler's INPUT chest with its recipe ingredients (from inventory) and
    EMPTY its OUTPUT chest back to inventory (Seth's rule). The inserters do the assembler I/O;
    this just keeps the chests stocked/drained so the chain flows. Input chest = the wooden chest
    ~3 tiles west of the assembler; output chest = ~3 east."""
    A._print(
        "/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory();"
        "for _,a in pairs(s.find_entities_filtered{type='assembling-machine'}) do local r=a.get_recipe();"
        "  if r then local ax,ay=a.position.x,a.position.y;"
        "    local ic=s.find_entities_filtered{name='wooden-chest',position={ax-3,ay},radius=1.4}[1];"
        "    local oc=s.find_entities_filtered{name='wooden-chest',position={ax+3,ay},radius=1.4}[1];"
        "    if ic then local ci=ic.get_inventory(defines.inventory.chest); for _,ing in pairs(r.ingredients) do if ing.type=='item' then "
        "      local want=ing.amount*8-ci.get_item_count(ing.name); local have=math.min(want,inv.get_item_count(ing.name)); if have>0 then ci.insert{name=ing.name,count=have}; inv.remove{name=ing.name,count=have} end end end end;"
        "    if oc then local co=oc.get_inventory(defines.inventory.chest); for _,c in pairs(co.get_contents()) do local g=inv.insert{name=c.name,count=c.count}; if g>0 then co.remove{name=c.name,count=g} end end end end end")


def service_science():
    """LIGHTWEIGHT logistics for the automated science cell - pure server-side item SHUFFLING,
    NO mining/crafting/character movement (that caused timeouts + the character running off).
    Production is hardware: gear assemblers make gears, science assemblers make packs, base
    furnaces make plates (collected by _collect_plates_all). This just moves items between them
    and into the labs. Supply (plates) comes from the automated outposts, not from mining here."""
    # keep plates stocked from the BUFFER chests so the assembler chain never starves (the 300
    # plates that pile up in the buffer must flow back to the cell when derpface's inv runs dry).
    if _count("iron-plate") < 100:
        gamedb.pull_from_buffer("iron-plate", 200)
    if _count("copper-plate") < 100:
        gamedb.pull_from_buffer("copper-plate", 200)
    A._print(
        "/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory();"
        # GENERIC assembler servicing: for every assembler, feed each recipe ingredient from
        # inventory (up to a small buffer) and pull its finished output back to inventory. This
        # makes ANY chain work (cable->circuit->inserter->belt->green pack, gear->red pack, ...)
        # with the inventory as the shared 'bus'. NOTE: a.get_item_count(ingredient) reads the
        # INPUT (an ingredient is never the product); outputs come from get_output_inventory.
        "for _,a in pairs(s.find_entities_filtered{type='assembling-machine'}) do local r=a.get_recipe();"
        "  if r then for _,ing in pairs(r.ingredients) do if ing.type=='item' then"
        "      local want=math.max(0, (ing.amount*4) - a.get_item_count(ing.name));"
        "      local have=math.min(want, inv.get_item_count(ing.name)); if have>0 then local ins=a.insert{name=ing.name,count=have}; inv.remove{name=ing.name,count=ins} end end end;"
        "    local oo=a.get_output_inventory(); if oo then for _,c in pairs(oo.get_contents()) do local g=inv.insert{name=c.name,count=c.count}; if g>0 then oo.remove{name=c.name,count=g} end end end end end;"
        # fill each lab's FEED CHEST (the chest above each lab); its inserter pushes packs into
        # the lab continuously, so all labs run evenly (hardware feed, Seth's rule). Top each
        # feed chest to a buffer of each pack.
        "for _,lab in pairs(s.find_entities_filtered{name='lab'}) do "
        "  local ch=s.find_entities_filtered{name='wooden-chest',position={lab.position.x,lab.position.y-2},radius=1.5}[1]; "
        "  if ch then local ci=ch.get_inventory(defines.inventory.chest); for _,pk in ipairs({'automation-science-pack','logistic-science-pack','chemical-science-pack'}) do "
        "    local want=8-ci.get_item_count(pk); local n=math.min(want,inv.get_item_count(pk)); if n>0 then ci.insert{name=pk,count=n}; inv.remove{name=pk,count=n} end end end end")


def automate_green_science(origin=(30, -16)):
    """Build the GREEN (logistic) science assembler chain so research advances past the green
    wall. The generic service_science() shuffles intermediates via inventory, so we just place
    assemblers + set recipes: copper-cable -> electronic-circuit -> inserter + transport-belt ->
    logistic-science-pack (plus the existing gear assembler feeds gears). Powered off the base
    network. Idempotent: skips recipes already running."""
    chain = ["copper-cable", "electronic-circuit", "inserter", "transport-belt",
             "logistic-science-pack", "logistic-science-pack"]
    existing = A._print("/sc local s=game.surfaces[1]; local r={}; for _,a in pairs(s.find_entities_filtered{type='assembling-machine'}) do local rc=a.get_recipe(); if rc then r[#r+1]=rc.name end end; rcon.print(table.concat(r,','))").strip().split(",")
    need = [r for r in chain if existing.count(r) < chain.count(r)]
    if not need:
        return
    n = len(need)
    if _count("assembling-machine-1") < n:
        make("assembling-machine-1", n - _count("assembling-machine-1"))
    ox, oy = origin
    A.now("Oil phase: building GREEN science assembler chain")
    A.stop(); A.walk(ox, oy + 4, tol=3.0)
    A.clear_area(ox + n * 2, oy, n * 2 + 10)
    placed = 0
    for k, recipe in enumerate(need):
        x = ox + k * 4
        r = A.place("assembling-machine-1", x, oy, clear=0).strip()
        if "BUILT" in r:
            A._print(f"/sc local s=game.surfaces[1]; local a=s.find_entities_filtered{{name='assembling-machine-1',position={{{x+1},{oy+1}}},radius=2}}[1]; if a then pcall(function() a.set_recipe('{recipe}') end) end")
            placed += 1
        A.place("small-electric-pole", x + 1, oy + 3, clear=1)
    # dedupe any pole overlap we just created
    dedupe_poles()
    return placed


def _network_count():
    """Number of DISTINCT electric networks among all poles. A unified grid is 1; fragmentation
    (generator islanded from the base) shows up as >1. dedupe_poles uses this to refuse any removal
    that SPLITS the grid."""
    return int(A._print("/sc local s=game.surfaces[1]; local seen={}; local n=0; for _,p in pairs(s.find_entities_filtered{type='electric-pole'}) do local id=p.electric_network_id; if id and not seen[id] then seen[id]=true; n=n+1 end end; rcon.print(n)").strip() or 0)


def dedupe_poles():
    """Remove ONLY genuinely REDUNDANT poles (another pole within ~2 tiles covering the same area),
    and ONLY when removal neither unpowers a consumer NOR SPLITS the grid.

    IMPORTANT (root-cause fix, 2026-06-28): this used to also remove 'orphan' poles - any pole with
    no machine within 3 tiles. But a pole powering nothing is almost always a load-bearing CONNECTOR
    (the bridge tying the steam engine to the base, the spine linking a smelter array to the grid).
    Deleting connectors fragmented the electric grid EVERY maintenance lap - the engine kept getting
    islanded from the base and the belt-fed smelter arrays lost power repeatedly. The old
    'power-verified' guard missed it because 0.3s was too short for the brownout to register and it
    never checked for a network SPLIT. We now: (1) never touch orphans, (2) revert any removal that
    raises the electric-network count, (3) settle 0.6s before judging. See GOTCHAS 'power grid'."""
    import math
    raw = A._print("/sc local s=game.surfaces[1]; local o={}; for _,p in pairs(s.find_entities_filtered{type='electric-pole'}) do o[#o+1]=string.format('%.2f,%.2f',p.position.x,p.position.y) end; rcon.print(table.concat(o,';'))").strip()
    P = [tuple(map(float, t.split(","))) for t in raw.split(";") if "," in t]

    def unpowered():
        return int(A._print("/sc local s=game.surfaces[1]; local n=0; for _,e in pairs(s.find_entities_filtered{type={'assembling-machine','lab','inserter','mining-drill','furnace'}}) do if e.prototype.electric_energy_source_prototype and e.status==58 then n=n+1 end end; rcon.print(n)").strip() or 0)

    # candidates = ONLY redundant poles (another pole within 2.0 tiles). Orphans are NEVER removed:
    # they are connectors/spines and deleting them splits the grid.
    near = [P[i] for i in range(len(P)) for j in range(len(P)) if i != j and math.hypot(P[i][0] - P[j][0], P[i][1] - P[j][1]) < 2.0]
    cand = list(dict.fromkeys(near))
    removed = 0
    for (x, y) in cand:
        base_unpow, base_nets = unpowered(), _network_count()
        A._print(f"/sc local s=game.surfaces[1]; for _,p in pairs(s.find_entities_filtered{{type='electric-pole',position={{{x},{y}}},radius=0.4}}) do p.destroy() end")
        time.sleep(0.6)
        if unpowered() > base_unpow or _network_count() > base_nets:
            A._print(f"/sc local s=game.surfaces[1]; local p=storage.derpface; s.create_entity{{name='small-electric-pole',position={{{x},{y}}},force=p.force}}")   # browned out or SPLIT the grid -> revert
        else:
            removed += 1
    return removed


def _advance_research(goal="construction-robotics"):
    """Research ALL non-gated tech (Seth): when current research is empty, pick ANY unresearched
    technology that is researchable NOW - prerequisites all researched, NOT a mine/build trigger,
    and its science packs are ones we PRODUCE (red + green). This uses the idle labs on every tech
    reachable with current science (military, logistics-2, productivity, upgrades, ...), not just
    the construction-robotics chain; it only stalls when everything left needs oil/blue science.
    Goal-chain techs are preferred first so we still progress toward robotics."""
    found = A._print(
        "/sc local f=game.forces.player; if f.current_research then rcon.print(f.current_research.name) return end;"
        "local PRODUCE={['automation-science-pack']=true,['logistic-science-pack']=true};"
        "local function ready(t) if t.researched or t.prototype.research_trigger then return false end;"
        "  for pn,_ in pairs(t.prototype.prerequisites) do if not f.technologies[pn].researched then return false end end;"
        "  for _,u in pairs(t.research_unit_ingredients) do if not PRODUCE[u.name] then return false end end; return true end;"
        # prefer a tech on the goal chain, else any researchable one
        "local GOAL={" + ",".join("['%s']=true" % g for g in techdb.prereq_chain(goal)) + "};"
        "local pick;"
        "for name,t in pairs(f.technologies) do if GOAL[name] and ready(t) then pick=name; break end end;"
        "if not pick then for name,t in pairs(f.technologies) do if ready(t) then pick=name; break end end end;"
        "if pick then f.add_research(pick); rcon.print(pick) else rcon.print('none') end").strip()
    return None if found == "none" else found


def _note(extra=""):
    """Refresh the on-screen note's pending line with what we're WAITING ON (the current
    research + %), plus an optional sub-activity. Keeps the note live every maintenance lap."""
    info = A._print("/sc local f=game.forces.player; rcon.print(f.current_research and (f.current_research.name..' '..math.floor((f.research_progress or 0)*100)..'%') or 'no research set')").strip()
    A.now(("Researching " + info) + (" | " + extra if extra else ""))


BUILD_QUEUE = []   # pending build tasks (callables). The loop does these FIRST when not gated.


def keep_power():
    """KEEP POWER ONLINE - top priority (Seth). The recurring power death was the character not
    reaching the distant boiler in time, so this distributes coal to the plant SERVER-SIDE (no
    walk) from the character's carried coal: tops the boiler fuel and its buffer chest. The
    character keeps coal stocked (restock_coal); as long as it carries coal, the plant never dies.
    Run every fast cycle."""
    A._print(
        "/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory();"
        "local b=s.find_entities_filtered{name='boiler'}[1]; if b then local need=5-b.get_fuel_inventory().get_item_count('coal'); local c=math.min(need,inv.get_item_count('coal')); if c>0 then b.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end end;"
        "local bc=s.find_entities_filtered{name='wooden-chest',position={45,-2},radius=6}[1]; if bc then local ci=bc.get_inventory(defines.inventory.chest); local need=120-ci.get_item_count('coal'); local c=math.min(need,inv.get_item_count('coal')); if c>0 then ci.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end end")
    # ensure_grid_connected() DISABLED: it adds bridge poles -> modifies the operator's hand-built power layout.
    # Power is human-managed now; the autopilot must not auto-place poles.


def ensure_grid_connected():
    """SELF-HEAL the electric grid: if a steam engine ends up on a DIFFERENT network than the main
    grid (the pole network with the most poles), bridge it back with a pole line. The recurring
    fragmented-generator failure - engine islanded from the base, so the whole base browns out and
    the smelter arrays lose power - now repairs ITSELF each power cycle instead of needing a human
    to re-bridge. Pairs with dedupe_poles no longer deleting connector poles. Server-side, no walk."""
    A._print(
        "/sc local s=game.surfaces[1]; local f=game.forces.player;"
        "local cnt={}; for _,p in pairs(s.find_entities_filtered{type='electric-pole'}) do cnt[p.electric_network_id]=(cnt[p.electric_network_id] or 0)+1 end;"
        "local main,best=nil,-1; for id,c in pairs(cnt) do if c>best then best=c; main=id end end; if not main then return end;"
        "for _,e in pairs(s.find_entities_filtered{name='steam-engine'}) do if e.electric_network_id~=main then"
        "  local near,bd=nil,1e9; for _,p in pairs(s.find_entities_filtered{type='electric-pole'}) do if p.electric_network_id==main then local d=(p.position.x-e.position.x)^2+(p.position.y-e.position.y)^2; if d<bd then bd=d; near=p end end end;"
        "  if near then local ex,ey,tx,ty=e.position.x,e.position.y,near.position.x,near.position.y; local dist=math.sqrt((tx-ex)^2+(ty-ey)^2); local steps=math.ceil(dist/6);"
        "    for k=1,steps do local x=math.floor(ex+(tx-ex)*k/steps)+0.5; local y=math.floor(ey+(ty-ey)*k/steps)+0.5; for _,t in pairs(s.find_entities_filtered{position={x,y},radius=0.4,type={'tree','simple-entity'}}) do t.destroy() end; if s.can_place_entity{name='small-electric-pole',position={x,y},force=f} then s.create_entity{name='small-electric-pole',position={x,y},force=f} end end end end end")


def fuel_arrays():
    """Keep the belt-fed SMELTER ARRAY furnaces fueled SERVER-SIDE from derpface's carried coal.
    The compact arrays have no room for coal inserters, and threading a dedicated coal belt past the
    congested coal mine + base proved fragile; this is the reliable mechanism (like keep_power tops
    the boiler). derpface restocks coal from the mine (restock_coal) and this distributes it to the
    array furnaces with no walk. Tops each array furnace to ~5 coal. The OLD base stacks keep their
    own supply. (A true dedicated coal belt remains the eventual upgrade once the arrays are
    re-spaced for coal inserters.)"""
    A._print(
        "/sc local p=storage.derpface; if not (p and p.valid) then return end; local s=p.surface; local inv=p.get_main_inventory();"
        # iron array (16 furnaces, x-5..25) + copper array (12 furnaces, x-5..17)
        "for _,z in ipairs({{-8,4,27,7},{-8,13,20,16}}) do"
        "  for _,fc in pairs(s.find_entities_filtered{name={'stone-furnace','steel-furnace'},area={{z[1],z[2]},{z[3],z[4]}}}) do"
        "    local fi=fc.get_fuel_inventory(); if fi then local need=5-fi.get_item_count('coal'); local c=math.min(need,inv.get_item_count('coal'));"
        "    if c>0 then fi.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end end end end")


def ensure_coal_restock():
    """Guarantee derpface can ALWAYS restock coal, preventing the coal death spiral. The coal mine's
    burner drills output to a BELT (there's no power line that far north for an electric inserter),
    so a self-fueling BURNER inserter must move coal belt -> chest for restock_coal to pull. Without
    it, derpface eventually hits 0 coal -> can't fuel the coal mine's OWN burner drills -> the coal
    mine stops -> nothing can be fueled -> total deadlock (the spiral that froze the whole base).
    Idempotent: builds the burner-inserter + chest at the coal belt's east end if missing, keeps the
    burner inserter lit. NOTE: never use an ELECTRIC inserter here (no power at the coal mine)."""
    A._print(
        "/sc local s=game.surfaces[1]; local f=game.forces.player;"
        "local mx=-1e9; local belt; for _,b in pairs(s.find_entities_filtered{name='transport-belt',area={{-18,-93},{-2,-88}}}) do if b.position.x>mx then mx=b.position.x; belt=b end end;"
        "if not belt then return end; local bx,by=math.floor(belt.position.x),math.floor(belt.position.y);"
        "local bi=s.find_entities_filtered{name='burner-inserter',area={{bx,by-2},{bx+3,by+2}}}[1];"
        "if not bi then if not s.find_entities_filtered{name='wooden-chest',area={{bx+1,by-1},{bx+4,by+1}}}[1] then s.create_entity{name='wooden-chest',position={bx+2.5,by+0.5},force=f} end;"
        "  bi=s.create_entity{name='burner-inserter',position={bx+1.5,by+0.5},direction=12,force=f}; bi.pickup_position={bx+0.5,by+0.5}; bi.drop_position={bx+2.5,by+0.5} end;"
        "if bi and bi.get_fuel_inventory().get_item_count('coal')<1 then bi.get_fuel_inventory().insert{name='coal',count=2} end")


def fuel_drills():
    """Keep all BURNER mining drills fueled SERVER-SIDE from derpface's carried coal. The mine drills
    are burner-powered and derpface can't be everywhere; when it parks at the coal mine the distant
    iron/copper drills run dry, the mines STOP, and the whole chain starves (furnaces no_ingredients
    -> labs missing_science_packs -> research stalls - the exact stall that froze the base). Like
    fuel_arrays for furnaces: tops each burner drill to ~5 coal, no walk. (Electrifying the drills is
    the eventual upgrade; this is the reliable mechanism meanwhile.)"""
    A._print(
        "/sc local p=storage.derpface; if not (p and p.valid) then return end; local s=p.surface; local inv=p.get_main_inventory();"
        "for _,d in pairs(s.find_entities_filtered{name='burner-mining-drill'}) do local fb=d.get_fuel_inventory();"
        "  if fb then local need=5-fb.get_item_count('coal'); local c=math.min(need,inv.get_item_count('coal'));"
        "    if c>0 then fb.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end end end")


def harvest_array_plates():
    """Move smelted plates from the belt-fed array DRAIN chests into DERPFACE's inventory (the
    conduit service_science feeds the science assemblers from). Without this the arrays produce
    plates that pile up in their drain chests and NEVER reach the assemblers, so research stalls
    (labs go missing_science_packs) even though plates are abundant. NOTE: do NOT target the
    gamedb.BUFFER_ROW chests - those double as the dump_excess junk dump and are full. Keeps
    derpface topped to ~300 of each plate. Server-side, no walk."""
    A._print(
        "/sc local p=storage.derpface; if not (p and p.valid) then return end; local s=p.surface; local inv=p.get_main_inventory();"
        "local function move(item, area, cap) local have=inv.get_item_count(item); if have>=cap then return end;"
        "  for _,src in pairs(s.find_entities_filtered{name='iron-chest',area=area}) do local si=src.get_inventory(defines.inventory.chest);"
        "    local n=math.min(si.get_item_count(item), cap-have); if n>0 then local ins=inv.insert{name=item,count=n}; si.remove{name=item,count=ins}; have=have+ins end end end;"
        # iron + copper plates for science; steel plates (steel stack drain ~x26,y6) for steel-furnace builds + recipes
        "move('iron-plate',{{10,1},{28,6}},300); move('copper-plate',{{2,10},{22,16}},300)")


def _gated():
    """True ONLY for a CRITICAL refill/refuel gate that would stall production (so we pause
    builds to clear it). Kept lenient so chronic-but-OK scarcity doesn't starve build tasks:
    char nearly out of coal, boiler buffer near-empty (power dying), or a drill actually at 0."""
    if _count("coal") < 30:
        return True
    low = A._print(
        "/sc local s=game.surfaces[1]; local g=false;"
        "for _,b in pairs(s.find_entities_filtered{name='wooden-chest'}) do if #s.find_entities_filtered{name='boiler',position=b.position,radius=3}>0 then local inv=b.get_inventory(defines.inventory.chest); if inv.get_item_count('coal') < 60 then g=true end end end;"
        "rcon.print(tostring(g))").strip()
    if low == "true":
        return True
    for ore in ("iron-ore", "copper-ore", "coal"):
        need = _outpost_needs(ore)
        if need and need[3] <= 0:        # a drill ACTUALLY at 0 fuel (stopped) - critical
            return True
    return False


def ensure_derpface():
    """Make sure the autonomous character `derpface` exists (recreate if missing/invalid, e.g.
    after a server restart before it was autosaved). Player-LESS character the autopilot drives;
    independent of any connected player, so it runs 24/7. Labelled "derpface" in-world."""
    return A._print(
        "/sc if not (storage.derpface and storage.derpface.valid) then local s=game.surfaces[1];"
        "  local c=s.create_entity{name='character', position={6,-10}, force='player'};"
        "  if c then storage.derpface=c; c.character_running_speed_modifier=0;"
        "    rendering.draw_text{text='derpface', surface=s, target=c, target_offset={0,-2.2}, color={1,0.82,0.25}, scale=1.6, alignment='center'} end end;"
        "rcon.print('derpface valid='..tostring(storage.derpface and storage.derpface.valid))").strip()


def maintain(laps=0):
    """SELF-RUNNING loop with Seth's PRIORITY model: do PENDING BUILD TASKS first when able;
    only switch to refuel/refill when a GATE blocks; resolve it; resume builds. Two concurrent
    strands (RCON is thread-safe - fresh socket per call):
      - SCIENCE strand (thread, server-side, fast): collect plates -> service assemblers ->
        feed labs -> advance research. This is continuous task PROGRESS, never blocked by walks.
      - SUPPLY strand (main, character): each lap resolve any gate (boiler coal <20%, drill out
        of fuel, furnace stack out of ore, character low on coal) by hauling/refueling; the
        haul/restock functions already no-op when nothing is gated, so the character only moves
        when there's a real gate to clear."""
    import threading
    ensure_derpface()          # the autonomous character must exist before we drive it
    flag = {"run": True}

    def science_strand():
        while flag["run"]:
            try:
                keep_power()                  # TOP PRIORITY: keep the steam plant fueled (server-side)
                fuel_arrays()                 # keep the belt-fed smelter array furnaces fueled (server-side)
                # ensure_coal_restock()       # DISABLED: the coal mine is human-built (self-feeding). The autopilot must NOT
                #                               rebuild base layout the operator manages - it kept rebuilding Seth's coal
                #                               buffer/inserter. Autopilot = fuel/harvest/research ONLY, never auto-build layout.
                fuel_drills()                 # keep all burner mining drills fueled (server-side) so mines never stall
                harvest_array_plates()        # array drain chests -> science buffer chests
                _collect_plates_all()         # furnace plates -> inventory
                _service_assembler_chests()   # fill assembler INPUT chests, empty OUTPUT chests
                service_science()             # lab feed chests (+ direct-feed any chest-less asm)
                _advance_research()           # target next fuelable tech
                status.write_status(BUILD_QUEUE)   # fresh heartbeat even while the main loop hauls
            except Exception as e:
                status.log(f"science strand error: {e}")
            time.sleep(3)

    th = threading.Thread(target=science_strand, daemon=True)
    th.start()
    i = 0
    try:
        while flag["run"] and (laps == 0 or i < laps):
            i += 1
            if _gated():
                # PRIORITY override: a fuel/refill gate -> clear it before anything else
                refill_buffers()
                haul_ore()
            elif BUILD_QUEUE:
                # not gated -> do the next pending BUILD task first (Seth's rule)
                task = BUILD_QUEUE.pop(0)
                status.log(f"building: {getattr(task, '__name__', 'task')}")
                _note(f"building: {getattr(task, '__name__', 'task')}")
                try:
                    task()
                except Exception as e:
                    status.log(f"build task error: {e}")
            else:
                # nothing gated, nothing to build -> light upkeep; science strand drives research
                refill_buffers()
                haul_ore()
            # if i % 15 == 0:
            #     dedupe_poles()             # DISABLED: removes poles -> fights the operator's hand-built power/pole layout.
            #                                  Pole cleanup is a human decision now.
            if i % 10 == 0:
                gamedb.dump_excess()   # overflow inventory -> buffer chests (server-side)
                gamedb.snapshot()      # refresh the structures + chest-inventory DB
            status.write_status(BUILD_QUEUE)   # heartbeat for a Claude session to read
            _note()
            time.sleep(2)
    finally:
        flag["run"] = False
        time.sleep(0.2)


def bootstrap():
    """Proven fresh-world sequence through power + automation. Idempotent; resumes on rerun."""
    setup_world()
    scout()
    fuel()
    smelting_base()
    power()
    red_science()
    A.now("Bootstrap: power + automation DONE")
    return STATE


if __name__ == "__main__":
    print(bootstrap())
