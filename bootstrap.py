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
import build_gates
import techdb
import feed_planner
import gamedb
import pole_cull
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


SCOUT_RESOURCES = ("iron-ore", "copper-ore", "stone", "coal", "water")


def scout(only=None):
    """Find the RICHEST tile of each ore + nearest water; cache in STATE. Generates chunks
    out to 384 tiles first so resources exist to scan.

    `only` restricts the scan to the named resources, and it is the normal case: a patch we
    already recorded does not move. This used to run in full at the top of EVERY planner pass,
    re-deriving positions that `planner._load` had just restored from phase.json a few lines
    earlier - a 625-chunk force-generate plus five radius-160 scans, per pass, to arrive at the
    same four coordinates. It was also the slowest step in the pass, so the dashboard's "current
    action" was almost always "scouting richest deposits + water" on a base whose whole problem
    was that it needed to BUILD (Seth, 2026-08-30: "we dont need to scout any deposits right now
    we have everything we need").
    """
    want = tuple(only) if only else SCOUT_RESOURCES
    if not want:
        return STATE
    A.now("Bootstrap: scouting %s" % ", ".join(want))
    A._print("/sc local s=game.surfaces[1]; for cx=-12,12 do for cy=-12,12 do s.request_to_generate_chunks({x=cx*32,y=cy*32},0) end end; s.force_generate_chunk_requests()")
    for ore in ("iron-ore", "copper-ore", "stone", "coal"):
        if ore in want:
            STATE[ore] = A.richest_spot(ore, 0, 0, radius=160)
    if "water" not in want:
        return STATE
    w = A._print("/sc local s=game.surfaces[1]; local w; for r=20,200,8 do local t=s.find_tiles_filtered{position={0,0},radius=r,name={'water','deepwater'},limit=1}; if #t>0 then w=t[1]; break end end; rcon.print(w and (math.floor(w.position.x)..','..math.floor(w.position.y)) or 'none')").strip()
    STATE["water"] = tuple(map(int, w.split(","))) if "," in w else None
    return STATE


def fuel(amount=300):
    """Hand-mine the first coal (nothing runs without fuel)."""
    if _count("coal") >= amount:
        return
    ensure("coal", amount)   # AUTOMATION FIRST: chests/belts before ever hand-mining
    if _count("coal") >= amount:
        return
    cx, cy, _ = STATE["coal"]
    A.now(f"Bootstrap: mining first coal @{cx},{cy} (no automated source yet)")
    A.stop(); A.walk(cx + 1, cy, tol=2.5)
    A.mine("coal", amount)


def smelting_base():
    """Build the smelting hub at spawn: 8 iron + 4 copper stone furnaces, then stock ore and
    smelt starting plates. OBSOLETE once the belt-fed arrays run (automation-first): skips
    itself when >=8 array furnaces exist - plates come from the arrays, not hand-stocking."""
    arrays = int(A._print(
        f"/sc local s=game.surfaces[1]; rcon.print(#s.find_entities_filtered{{area={{{{{SMELT_ZONE['iron-ore'][0] - 4},{SMELT_ZONE['iron-ore'][1] - 2}}},{{{SMELT_ZONE['iron-ore'][0] + 36},{SMELT_ZONE['copper-ore'][1] + 8}}}}},name={{'stone-furnace','steel-furnace'}}}})").strip() or "0")
    if arrays >= 8:
        return
    bx, by = SPAWN
    # stone first (furnaces need it); mine generously
    # AUTOMATION FIRST (Seth): pull ore via ensure() - mine-outpost chests/buffers first,
    # hand-mining only as the true last resort (fresh world, no drills yet)
    if _count("stone") < 120:
        ensure("stone", 250)
    if _count("iron-ore") < 200:
        ensure("iron-ore", 250)
    if _count("copper-ore") < 150:
        ensure("copper-ore", 200)
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
                 "for _,it in ipairs({'iron-plate','copper-plate'}) do local a=oi.get_item_count(it); if a>0 then local g=inv.insert{name=it,count=a}; if g>0 then oi.remove{name=it,count=g} end end end end")
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
    # 1) PUMP: place on a land tile adjacent to water, intake dir facing the water. NOTE: do NOT
    #    "verify" the bare pump by get_fluid_count - an UNCONNECTED offshore pump reads 0 (it has no
    #    buffer; it only pumps into connected pipes). Verification happens DOWNSTREAM at the boiler.
    pump = A._print(
        "/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); local g;"
        "local function iw(x,y) return string.find(s.get_tile(x,y).name,'water')~=nil end;"
        f"for _,wt in pairs(s.find_tiles_filtered{{position={{{wx},{wy}}},radius=14,name={{'water','deepwater'}}}}) do if g then break end;"
        "  local x,y=math.floor(wt.position.x),math.floor(wt.position.y);"
        # water-SOUTH shores only (land at y-1, pump dir8) so the proven plant layout applies
        "  for _,nb in ipairs({{x,y-1,8}}) do local lx,ly,d=nb[1],nb[2],nb[3];"
        "    if not g and not iw(lx,ly) and s.can_place_entity{name='offshore-pump',position={lx+0.5,ly+0.5},direction=d} then"
        "      local e=s.create_entity{name='offshore-pump',position={lx+0.5,ly+0.5},direction=d,force=p.force};"
        "      if e then inv.remove{name='offshore-pump',count=1}; g={x=lx,y=ly,d=d} end end end end;"
        "if g then rcon.print(g.x..','..g.y..','..g.d) else rcon.print('none') end").strip()
    if pump == "none" or "," not in pump:
        A.now("power: no placeable shore tile for the pump")
        return None
    px, py, pd = map(int, pump.split(","))
    # output tile is opposite the intake direction; build boiler so an END input meets pump water,
    # bridging with pipes; verify the BOILER gets water + the ENGINE gets energy (downstream).
    return _build_boiler_engine(px, py, pd)


def _build_boiler_engine(px, py, pd, n_engines=2):
    """Replicate Seth's PROVEN steam-plant layout (captured from the live working plant) for a
    water-SOUTH pump (pd=8, output NORTH): a horizontal pipe line at py-1 carrying the pump output
    east to the boiler's water inputs, a boiler at (px,py-3) dir0 (steam exits north), and
    n_engines steam engines chained north (5 tiles apart) + a pole. Verified by get_fluid_count /
    energy. Returns the boiler tile, or None if the boiler never got water.

    Reference offsets from the proven plant (pump tile (44,0) dir8): pipes (43..46,-1), boiler
    center (45.5,-2) dir0, engines (45.5,-5.5/-10.5/-15.5/...) dir0."""
    if pd != 8:
        A.now(f"power: pump dir {pd} not water-south; this layout supports water-south only for now")
        return None
    A.clear_area(px + 1, py - 12, 16)     # clear trees/rocks over the whole plant zone once; then
    #                                       place everything with clear=0 (clear>0 cliff-aborts).
    # 1) pipe line at y=py-1, x=px-1..px+2: pump output is the tile (px,py-1); carry it west, then
    #    UP the west side to the boiler's WEST water input (a dir0 boiler takes water on its E/W
    #    ENDS, not the south - so a pipe at (px-1,py-2) reaching the boiler's west end is required;
    #    omitting it leaves the boiler dry. Proven plant had this exact west pipe at (43,-2)).
    for x in range(px - 1, px + 3):
        A.place("pipe", x, py - 1, clear=0)
    A.place("pipe", px - 1, py - 2, clear=0)        # west riser to the boiler's water input
    # 2) boiler at (px,py-3) dir0 -> center (px+1.5,py-2); west input meets the riser pipe.
    #    clear=0: the pump area is already established, and clear>0 aborts on a CLIFF anywhere in
    #    its radius even when the footprint itself is placeable (it bit us at the shore).
    A.place("boiler", px, py - 3, direction=0, clear=0)
    bcx, bcy = px + 1, py - 2
    # 3) fuel the boiler
    A._print(
        "/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory();"
        f"local b=s.find_entities_filtered{{name='boiler',position={{{bcx},{bcy}}},radius=3}}[1];"
        "if b then local c=math.min(10,inv.get_item_count('coal')); if c>0 then b.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end end")
    time.sleep(3)
    bw = A._print(f"/sc local s=game.surfaces[1]; local b=s.find_entities_filtered{{name='boiler',position={{{bcx},{bcy}}},radius=3}}[1]; rcon.print(b and tostring(math.floor(b.get_fluid_count('water'))) or 'nobl')").strip()
    # 4) engines chained north, 5 apart: A.place(engine,px,py-8-5k) -> center (px+1.5,py-5.5-5k)
    for k in range(n_engines):
        A.place("steam-engine", px, py - 8 - 5 * k, direction=0, clear=0)
    A.place("small-electric-pole", px + 3, py - 9, clear=0)   # wire reach to the engines
    time.sleep(4)
    st = A._print(
        "/sc local s=game.surfaces[1]; local o={};"
        f"local b=s.find_entities_filtered{{name='boiler',position={{{bcx},{bcy}}},radius=3}}[1];"
        "o[#o+1]='boiler w='..(b and math.floor(b.get_fluid_count('water')) or -1)..' st='..(b and math.floor(b.get_fluid_count('steam')) or -1);"
        f"local e=s.find_entities_filtered{{name='steam-engine',position={{{px+1},{py-6}}},radius=5}}[1];"
        "o[#o+1]='engineE='..(e and math.floor(e.energy) or -1); rcon.print(table.concat(o,' '))").strip()
    A.now(f"power plant verify: {st}")
    try:
        return (bcx, bcy) if int(bw) > 0 else None
    except ValueError:
        return None


def _tech_done(name):
    return A._print(f"/sc rcon.print(tostring(game.forces.player.technologies['{name}'].researched))").strip() == "true"


def _collect_plates():
    """Sweep finished plates out of the base furnaces into inventory."""
    A._print("/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); "
             "for _,fu in pairs(s.find_entities_filtered{area={{-1,-18},{17,-9}},name='stone-furnace'}) do local oi=fu.get_output_inventory(); "
             "for _,it in ipairs({'iron-plate','copper-plate'}) do local a=oi.get_item_count(it); if a>0 then local g=inv.insert{name=it,count=a}; if g>0 then oi.remove{name=it,count=g} end end end end")


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
             "for _,it in ipairs({'iron-plate','copper-plate'}) do local a=oi.get_item_count(it); if a>0 then local g=inv.insert{name=it,count=a}; if g>0 then oi.remove{name=it,count=g} end end end end")


def harvest_plate_belts():
    """Pull iron/copper plates OFF the plate belts into derpface's inventory so service_science can
    feed the assembler chain. Seth belt-fed the furnaces, so plates now flow onto a plate belt (and
    pile at its dead-end) instead of sitting in furnace OUTPUTS - `_collect_plates_all` (which reads
    furnace outputs) gets nothing, and the green chain starves at iron=0 (verified: feeding 12 plates
    flipped the logistic-science-pack assembler to working). This bridges the belt->software-shuffle
    gap. Capped at 300/plate so coal still fits; the plate belts dead-end so draining them starves no
    downstream consumer. Insert-first/remove-actual so no plate is lost on a full inventory. Scoped to
    the plate-belt region (the furnace output area), not every belt on the map."""
    A._print(
        "/sc local p=storage.derpface; if not (p and p.valid) then return end; local s=p.surface; local inv=p.get_main_inventory();"
        "for _,plate in ipairs({'iron-plate','copper-plate'}) do local room=300-inv.get_item_count(plate);"
        "  if room>0 then for _,b in pairs(s.find_entities_filtered{type='transport-belt',area={{-10,0},{30,16}}}) do for ln=1,2 do"
        "    if room>0 then local line=b.get_transport_line(ln); local k=math.min(line.get_item_count(plate),room);"
        "      if k>0 then local ins=inv.insert{name=plate,count=k}; if ins>0 then line.remove_item{name=plate,count=ins}; room=room-ins end end end end end end")


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
    if item == "wood":
        # wood isn't a resource entity (trees are type=tree), so the richest-spot path below
        # can never find it; without this branch every pole craft dead-ends once the
        # cleared-land wood runs out (stalled the science-cell power build 2026-08-29)
        tp = A._print(
            "/sc local p=storage.derpface; local s=p.surface;"
            "local t=s.find_entities_filtered{type='tree',position=p.position,radius=250,limit=1}[1];"
            "rcon.print(t and (math.floor(t.position.x)..','..math.floor(t.position.y)) or 'none')").strip()
        if "," in tp:
            tx, ty = map(int, tp.split(","))
            A.now(f"Provision: harvesting wood @{tx},{ty}")
            A.stop(); A.walk(tx, ty + 1, tol=3.0)
            A._print(
                f"/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); local n=0;"
                f"for _,t in pairs(s.find_entities_filtered{{type='tree',position={{{tx},{ty}}},radius=14}}) do"
                f"  if n*4>={count}+8 then break end; inv.insert{{name='wood',count=4}}; t.destroy(); n=n+1 end")
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
    # belt-fed mine (no terminal chest): LIFT off the lane belts before ever hand-mining
    lifted = A._print(
        f"/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); local got=0;"
        f"for _,b in pairs(s.find_entities_filtered{{position={{{sx},{sy}}},radius=30,type='transport-belt'}}) do"
        f"  if got>={count} then break end;"
        "  for li=1,b.get_max_transport_line_index() do local L=b.get_transport_line(li);"
        f"    local n=L.get_item_count('{item}'); if n>0 then local take=math.min(n,{count}-got);"
        f"      local r=L.remove_item{{name='{item}',count=take}}; if r>0 then inv.insert{{name='{item}',count=r}}; got=got+r end end end end;"
        "rcon.print(got)").strip()
    if _count(item) >= count:
        A.now(f"Provision: lifted {item} off the mine belts (no hand-mining)")
        return
    A.now(f"Provision: mining {count} {item} @{sx},{sy} (belts short)")
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
    if cost.get("wood", 0) > _count("wood"):
        ensure("wood", cost["wood"])   # wood is raw but not MINEABLE; poles dead-ended on this
    return _craft_wait(recipe, count)


def craft_real(recipe, count=1, timeout=90):
    """REAL character crafting via begin_crafting (takes in-game seconds, consumes real
    ingredients) - unlike _craft_wait's instant script-craft. Gathers missing ingredients
    with make() first. Returns how many crafts were started."""
    started = 0
    for _ in range(3):
        r = A._print(f"/sc local p=storage.derpface; rcon.print(p.begin_crafting{{recipe='{recipe}', count={count - started}}})").strip()
        try:
            started += int(r)
        except ValueError:
            return started
        if started >= count:
            break
        miss = missing_for(recipe)
        if not miss:
            break
        for item, need in miss.items():
            try:
                make(item, need)
            except Exception:
                pass
    t0 = time.time()
    while time.time() - t0 < timeout:
        if A._print("/sc rcon.print(storage.derpface.crafting_queue_size)").strip() == "0":
            break
        time.sleep(2)
    return started


def fire_craft_trigger(tech_name):
    """Complete a craft-item TRIGGER tech by REALLY crafting its trigger item.

    WHY the scripted completion at the end: verified live on 2.1.17 (2026-08-29) - a
    player-LESS character's begin_crafting genuinely crafts the item (real ingredients, real
    time) but the engine only credits craft-item triggers from PLAYER craft events, so the
    tech never completes headless (a lab was really crafted; automation-science-pack stayed
    locked and ALL research deadlocked). Trigger techs cost no science; after verifying the
    real craft happened we mark the tech researched - a faithful emulation of the intended
    mechanic, not a shortcut. Keep-it-legit: materials + craft time were real."""
    if _tech_done(tech_name):
        return True
    t = techdb.tech(tech_name) or {}
    trig = t.get("trigger") or {}
    if trig.get("type") != "craft-item":
        return False
    item, n = trig.get("item"), int(trig.get("count", 1))
    if not item:
        return False
    before = _count(item)
    craft_real(item, n)
    if _count(item) < max(before + n, n):
        A.now(f"trigger craft {item} x{n} failed (have {_count(item)})")
        return False
    A._print(f"/sc game.forces.player.technologies['{tech_name}'].researched=true")
    status.log(f"trigger tech {tech_name} completed via real character craft of {item} x{n} "
               "(headless characterless crafts don't credit craft-item triggers)")
    return _tech_done(tech_name)


def red_science():
    """Lab + hand-crafted red science -> research 'automation' (unlocks assemblers).
    Robust: loops crafting+feeding until automation actually completes."""
    A.now("Bootstrap: lab + red science -> research automation")
    wx, wy = STATE["water"]
    have_labs = int(A._print(f"/sc rcon.print(#game.surfaces[1].find_entities_filtered{{name='lab',position={{{wx},{wy}}},radius=30}})").strip() or "0")
    if have_labs < 2:            # gate0 needs labs_working>=2; one lab made phase 0 UNSATISFIABLE
        need = 2 - have_labs
        if _count("lab") < need:
            _craft_wait("lab", need)
        A.place("lab", wx + 4, wy - 9, clear=4)
        A.place("lab", wx + 8, wy - 9, clear=2)
    fire_craft_trigger("automation-science-pack")   # headless craft-item triggers never
    # self-complete (see fire_craft_trigger docstring); without this ALL research deadlocks
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
            if trig.get("type") == "mine-entity":
                return False, f"{t} (mine {trig.get('entity','?')})"
            if trig.get("type") == "craft-item":
                # headless craft-item triggers never self-complete (see fire_craft_trigger)
                if not fire_craft_trigger(t):
                    return False, f"{t} (trigger craft {trig.get('item','?')} failed)"
                continue
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


LAST_LAY_GAPS = 0        # unbridged gaps from the most recent lay_belt_path (verification)


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
    laid_tiles = [(x, y) for (x, y, _d) in tiles]
    prot = _protected_load()
    if prot:
        skipped = [(x, y) for (x, y, _d) in tiles if (x, y) in prot]
        if skipped:
            status.log(f"lay_belt_path: skipping {len(skipped)} operator-protected tiles")
        tiles = [(x, y, d) for (x, y, d) in tiles if (x, y) not in prot]
    spec = ";".join(f"{x},{y},{d}" for (x, y, d) in tiles)
    gaps = A._print(
        "/sc local s=game.surfaces[1]; local f=game.forces.player;"
        "local T={}; for a,b,c in ([==[" + spec + "]==]):gmatch('(-?%d+),(-?%d+),(%d+)') do T[#T+1]={tonumber(a),tonumber(b),tonumber(c)} end;"
        # An EXISTING belt on the tile counts as FREE: we replace it with the correct
        # direction. can_place_entity alone returns false there, so a stale-direction tile
        # could never be corrected - every re-lay bridged or gave up around it, which is why
        # the copper lane never converged for hours (2026-08-30).
        "local function freebelt(x,y) for _,e in pairs(s.find_entities_filtered{position={x+0.5,y+0.5},radius=0.6,type={'tree','simple-entity','cliff'}}) do if e.destroy then e.destroy() end end;"
        "  if s.find_entity('transport-belt',{x+0.5,y+0.5}) then return true end;"
        "  return s.can_place_entity{name='transport-belt',position={x+0.5,y+0.5},force=f} end;"
        "local gaps=0; local i=1;"
        "while i<=#T do local x,y,d=T[i][1],T[i][2],T[i][3];"
        "  if freebelt(x,y) then local old=s.find_entity('transport-belt',{x+0.5,y+0.5}); if old then old.destroy() end; s.create_entity{name='transport-belt',position={x+0.5,y+0.5},direction=d,force=f}; i=i+1;"
        "  else local j=i+1; while j<=#T and not freebelt(T[j][1],T[j][2]) do j=j+1 end;"
        "    if i>1 and j<=#T and (j-(i-1))<=5 then local p=T[i-1]; local old=s.find_entity('transport-belt',{p[1]+0.5,p[2]+0.5}); if old then old.destroy() end;"
        "      pcall(function() s.create_entity{name='underground-belt',position={p[1]+0.5,p[2]+0.5},direction=p[3],type='input',force=f} end);"
        "      pcall(function() s.create_entity{name='underground-belt',position={T[j][1]+0.5,T[j][2]+0.5},direction=T[j][3],type='output',force=f} end);"
        "    else gaps=gaps+1 end; i=j+1 end end;"
        "rcon.print(gaps)").strip()
    # callers need the TILES (lane registry); the gap count stays available for verification
    global LAST_LAY_GAPS
    LAST_LAY_GAPS = int(gaps or 0)
    record_built(laid_tiles)      # consent mechanism: we remember what WE placed
    return laid_tiles


def _lanes_load():
    import json as _j
    import pathlib as _pl
    f = _pl.Path(__file__).resolve().parent / "lanes.json"
    try:
        return {k: [tuple(t) for t in v] for k, v in _j.loads(f.read_text()).items()}
    except (OSError, ValueError):
        return {}


def _lanes_save(d):
    import json as _j
    import pathlib as _pl
    f = _pl.Path(__file__).resolve().parent / "lanes.json"
    try:
        f.write_text(_j.dumps({k: [list(t) for t in v] for k, v in d.items()}))
    except OSError:
        pass


def teardown_lane(ore, keep=()):
    """Remove the PREVIOUS registered lane for `ore` (refunding belts), except tiles the new
    route reuses. Root-cause fix for 'two belts from each patch' (Seth, 2026-08-30): every
    re-lay used to leave its predecessor standing, so superseded parallel lanes accumulated -
    a direct violation of the teardown-on-supersede law. No registry entry = nothing removed
    (we never guess at belts we didn't record)."""
    lanes = _lanes_load()
    old = [t2 for t2 in lanes.get(ore, []) if tuple(t2) not in set(keep)]
    if not old:
        return 0
    spec = ";".join(f"{x},{y}" for (x, y) in old[:600])
    out = A._print(
        "/sc local s=game.surfaces[1]; local inv=storage.derpface.get_main_inventory(); local n=0;"
        "for a,b in ([==[" + spec + "]==]):gmatch('(-?%d+),(-?%d+)') do"
        "  local e=s.find_entities_filtered{position={tonumber(a)+0.5,tonumber(b)+0.5},radius=0.4,"
        "    type={'transport-belt','underground-belt'}}[1];"
        "  if e then for li=1,(e.type=='transport-belt' and e.get_max_transport_line_index() or 0) do"
        "      local L=e.get_transport_line(li); for _,it in pairs(L.get_contents()) do inv.insert{name=it.name,count=it.count} end end;"
        "    inv.insert{name=e.name,count=1}; e.destroy(); n=n+1 end end;"
        "rcon.print(n)").strip()
    try:
        n = int(out)
    except ValueError:
        n = 0
    if n:
        forget_built(old)         # our own teardown - do NOT mistake it for an operator edit
        status.log(f"teardown_lane({ore}): removed {n} superseded belt tiles (refunded)")
    return n


def route_is_operator_owned(waypoints, threshold=0.25):
    """True when a meaningful share of a planned route sits on OPERATOR-PROTECTED tiles.
    The operator deleted that path on purpose, so the bot must stop trying to lay it -
    not retry forever (Seth: 'I delete them and you add them back')."""
    prot = _protected_load()
    if not prot:
        return False
    pts = []
    for i in range(len(waypoints) - 1):
        x1, y1 = waypoints[i]
        x2, y2 = waypoints[i + 1]
        dx = (x2 > x1) - (x2 < x1)
        dy = (y2 > y1) - (y2 < y1)
        for k in range(max(abs(x2 - x1), abs(y2 - y1))):
            pts.append((x1 + dx * k, y1 + dy * k))
    if not pts:
        return False
    hit = sum(1 for pt in pts if pt in prot)
    return hit / len(pts) >= threshold


def build_worked(check, tries=6, delay=5):
    """Poll a post-build check (callable -> bool). Seth's law: a build must actually DO
    something; a build that produces nothing gets removed by the caller."""
    import time as _t
    for _ in range(tries):
        try:
            if check():
                return True
        except Exception:
            pass
        _t.sleep(delay)
    return False


def lane_moves_ore(ore):
    """Functional check: is this ore actually MOVING on its lane (not just connected)?"""
    ox, oy = SMELT_ZONE[ore]
    out = A._print(
        f"/sc local s=game.surfaces[1]; local n=0;"
        f"for _,b in pairs(s.find_entities_filtered{{area={{{{{ox - 12},{oy + 3}}},{{{ox + 34},{oy + 7}}}}},type='transport-belt'}}) do"
        f"  for li=1,b.get_max_transport_line_index() do n=n+b.get_transport_line(li).get_item_count('{ore}') end end;"
        "rcon.print(n)").strip()
    try:
        return int(out) > 0
    except ValueError:
        return False


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
    # L-path via a PER-ORE column (iron x=ox-2, copper x=ox-4, ...): both smelt zones share
    # ox, so a single ax-1 column MERGED the ore lanes (mixed-ore law violation, 2026-08-29).
    ax, ay = ox - 1, oy + 5
    lane_x = ox - 2 - 2 * list(SMELT_ZONE).index(ore)
    route = [(rx + 10, ry), (lane_x, ry), (lane_x, ay), (ax, ay)]
    if route_is_operator_owned(route):
        status.log(f"connect_mine_to_array({ore}): route is OPERATOR-OWNED (he deleted it) - not rebuilding")
        return
    laid = lay_belt_path(route)
    tiles = laid if isinstance(laid, list) else []
    # SUPERSEDE: the previous lane for this ore goes away in the SAME pass (never leave a
    # parallel route standing), then the new route is registered as the only lane.
    teardown_lane(ore, keep=tiles)
    if tiles:
        lanes = _lanes_load()
        lanes[ore] = tiles
        _lanes_save(lanes)
    _verify_lane_or_remove(ore, tiles)


def _verify_lane_or_remove(ore, tiles):
    """VERIFY THE RESULT (Seth's law) - BUT DISTINGUISH THE TWO WAYS IT CAN FAIL.

    A lane that does not CONNECT its mine to its array genuinely did nothing and comes out.
    A lane that connects but carries no ore is CORRECT INFRASTRUCTURE starved from somewhere
    else - blocked drills, an array jammed at full_output, an exhausted patch - and tearing it
    out cannot fix any of those. Worse, it guarantees a loop: the next pass lays the same route,
    measures the same zero, and removes it again.

    Live, 2026-08-30: nine cycles of "lane produced NO ore flow - removing what I built" /
    "teardown_lane(copper-ore): removed 83 superseded belt tiles", 83 belts at a time, while
    every copper furnace sat at full_output with nowhere to put a plate. The lane was never the
    problem. It is the same misreading `_fix_lanes` had - a blocked OUTPUT presenting as a
    starved INPUT - and fixing it there and not here is exactly why it kept happening.
    """
    if not tiles:
        return
    if not build_worked(lambda: _lane_connected(ore), tries=3):
        status.log("connect_mine_to_array(%s): the route does not connect the mine to the array "
                   "- removing what I built" % ore)
        teardown_lane(ore)
        lanes = _lanes_load()
        lanes.pop(ore, None)
        _lanes_save(lanes)
        return
    if not build_worked(lambda: lane_moves_ore(ore), tries=3):
        status.log("connect_mine_to_array(%s): lane is CONNECTED but no %s is moving (%s) - "
                   "KEEPING it. A belt cannot fix a stall that is not on the belt."
                   % (ore, ore, no_flow_reason(ore)))


def no_flow_reason(ore):
    """Why is a CONNECTED lane carrying nothing? Names the real stall so the log says something
    actionable instead of blaming the belt that is working correctly."""
    try:
        st = build_gates.sense()
    except Exception:
        return "cause unknown - census unavailable"
    jam = sum(int(((st.get("status") or {}).get(n) or {}).get("full_output", 0))
              for n in getattr(build_gates, "FURNACE_NAMES", ()))
    if jam >= 3:
        return ("%d furnaces are jammed at full_output - the arrays cannot take more ore until "
                "something CONSUMES the plates" % jam)
    drills = (st.get("status_type") or {}).get("mining-drill") or {}
    blocked = int(drills.get("waiting_for_space_in_destination", 0))
    working = int(drills.get("working", 0))
    if blocked and not working:
        return "%d drills are blocked and none is working - the stall is upstream at the mine" % blocked
    if not blocked and not working:
        return "no drill is running on this patch"
    return "drills %d working / %d blocked; the belt itself is intact" % (working, blocked)


def build_belt_supply():
    """Orchestrate the belt-fed supply (Seth): build iron + copper smelter arrays, connect each
    mine to its array by belt (no more character ore-hauling), run a coal belt to the arrays, and
    a plate belt from the arrays to a science feed chest. Large; runs as a queued build task on
    Charon so derpface builds it. Iron array may already exist (built + validated by hand)."""
    if operator_present():
        status.log("build_belt_supply: operator online - deferring layout work")
        return
    build_smelter_array("iron-ore", 16)
    build_smelter_array("copper-ore", 12)
    for _ore in ("iron-ore", "copper-ore"):
        if not _lane_connected(_ore):     # destructive re-lay ONLY when broken (audit #5)
            connect_mine_to_array(_ore)
    # coal belt from the coal mine down to the arrays (codified layer, not build_belt)
    cs = STATE.get("coal")
    if cs:
        ox, oy = SMELT_ZONE["iron-ore"]
        # coal gets its OWN column (ox-6): ox-2/ox-4 are the per-ore ORE lanes now - a shared
        # column merges lanes (the mixed-ore bug all over again, fuel edition)
        laid = lay_belt_path([(int(cs[0]) + 10, int(cs[1])), (ox - 6, int(cs[1])), (ox - 6, oy + 6), (ox - 2, oy + 6)])
        if isinstance(laid, list) and laid:
            teardown_lane("coal", keep=laid)      # same supersede law as the ore lanes
            lanes = _lanes_load(); lanes["coal"] = laid; _lanes_save(lanes)


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


def _mine_is_belt_fed(x, y):
    """True if the mine around (x,y) has ore belts but NO terminal wooden-chest within radius 30.
    build_mine_outpost ALWAYS leaves a terminal chest (for character hauling), so "belts present, no
    terminal chest" is the signature of a human-laid BELT-FED mine (the operator removed the chest
    and ran the belt through to base). Such a mine is operator-managed: never relocate, rebuild, or
    re-cap it (any of those destroy the through-belt and starve the base - the iron-mine bug Seth
    caught 2026-06-29)."""
    out = A._print(f"/sc local s=game.surfaces[1]; local b=#s.find_entities_filtered{{name={{'transport-belt','underground-belt'}},position={{{x},{y}}},radius=30}}; local c=#s.find_entities_filtered{{name='wooden-chest',position={{{x},{y}}},radius=30}}; rcon.print(b..','..c)").strip()
    try:
        nb, nc = int(out.split(",")[0]), int(out.split(",")[1])
    except (ValueError, IndexError):
        return False
    return nb >= 4 and nc == 0


def build_mine_outpost(ore, n=8):
    """Seth's supply architecture: a SCALED row of `n` burner drills all dropping onto ONE belt
    that runs east to a single OUTPUT CHEST loaded by a burner inserter. NO furnaces here -
    smelting stays at the base; the character hauls ore from this chest to the base smelter array
    on maintenance runs (haul_ore). Returns the output-chest tile (cx,cy)."""
    spot = STATE.get(ore) or A.richest_spot(ore, 0, 0, radius=160)
    if not spot:
        return None
    rx, ry, _ = spot
    # GUARD (Seth, 2026-06-29): never re-cap a BELT-FED mine. build_mine_outpost ALWAYS terminates
    # the ore belt with an inserter+wooden-chest (for character hauling). When a human belt-connects
    # the mine to the base smelters, they REMOVE that terminal chest and run the belt through. So
    # "ore belts present but NO terminal chest" == belt-fed: rebuilding/clean-slating it here would
    # destroy the through-belt and re-cap it with a dead-end chest, draining the belt and starving
    # the base (the exact iron-mine bug Seth caught). Leave a belt-fed mine completely untouched.
    if _mine_is_belt_fed(rx, ry):
        A.now(f"Supply: {ore} mine is BELT-FED (belts, no terminal chest) @{rx},{ry} - leaving it intact")
        status.log(f"build_mine_outpost({ore}): mine is belt-fed (no terminal chest) - skipped to avoid re-capping")
        return (rx, ry)        # truthy 'already connected' sentinel; haul_ore no-ops with no chest
    # Already a CLEAN outpost (belt + chest, and NO furnaces - smelting is base-only)? then skip.
    state = A._print(f"/sc local s=game.surfaces[1]; rcon.print(#s.find_entities_filtered{{name='transport-belt',position={{{rx},{ry}}},radius=22}}..','..#s.find_entities_filtered{{name='stone-furnace',position={{{rx},{ry}}},radius=22}})").strip()
    nbelt, nfurn = (int(state.split(",")[0]), int(state.split(",")[1])) if "," in state else (0, 1)
    if nbelt > 0 and nfurn == 0:
        cc = A._print(f"/sc local s=game.surfaces[1]; local c=s.find_entities_filtered{{name='wooden-chest',position={{{rx},{ry}}},radius=24}}[1]; rcon.print(c and (math.floor(c.position.x)..','..math.floor(c.position.y)) or 'none')").strip()
        # no terminal chest within 24 but belts present = connected/belt-fed state, NOT a
        # failure (a stray chest at r24-30 made _mine_is_belt_fed and this branch disagree,
        # so every phase pass errored "returned None" - 2026-08-30)
        return tuple(map(int, cc.split(","))) if "," in cc else (rx, ry)
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


def trim_inventory():
    """Keep derpface's inventory LEAN so there is ALWAYS room for coal. The science chain over-pulls
    intermediates: it hoarded 11,200 copper-cable (56 of 80 slots), filling the inventory so coal
    could not fit -> restock_coal carried nothing -> every distant mine drill + furnace ran dry. Each
    lap: feed science packs to the labs, then trim over-hoarded intermediates and ore (the belts
    deliver ore; derpface must not carry it). Items are regenerated by the chain, so trimming excess
    is safe and keeps the whole base fueled. Server-side, no walk, no building."""
    A._print(
        "/sc local s=game.surfaces[1]; local d=storage.derpface; if not (d and d.valid) then return end; local inv=d.get_main_inventory();"
        "for _,pk in ipairs({'automation-science-pack','logistic-science-pack'}) do for _,l in pairs(s.find_entities_filtered{name='lab'}) do local li=l.get_inventory(defines.inventory.lab_input); if li then local have=inv.get_item_count(pk); if have>0 then local g=li.insert{name=pk,count=math.min(have,20)}; if g>0 then inv.remove{name=pk,count=g} end end end end end;"
        "local function trim(item,keep) local have=inv.get_item_count(item); if have>keep then inv.remove{name=item,count=have-keep} end end;"
        "trim('copper-cable',200); trim('electronic-circuit',200); trim('iron-ore',0); trim('copper-ore',0); trim('automation-science-pack',100); trim('logistic-science-pack',100);"
        # copper-plate over-supplies (it hoarded 6200, jamming out the IRON-plate the green chain needs);
        # cap it so iron-plate always has room. Iron is the chain's limiting input, so keep iron generous.
        "trim('copper-plate',400)")


def restock_coal(low=40, target=150):
    """Keep derpface stocked with coal so fuel_drills/fuel_arrays/keep_power can refuel the mine
    drills, furnaces, and boiler. Pulls coal SERVER-SIDE (NO walk, NO building) from the richest coal
    chest in the base (the operator's coal stock chest, kept full by the self-feeding coal mine),
    then falls back to lifting coal off transport belts near derpface (it parks in the coal). The old
    version walked to a hardcoded coal-mine chest that no longer exists after the operator rebuilt
    the mine, so derpface never restocked and every distant drill ran dry."""
    if _count("coal") >= target:
        return
    A._print(
        "/sc local p=storage.derpface; if not (p and p.valid) then return end; local s=p.surface; local inv=p.get_main_inventory();"
        "local want=" + str(target) + "-inv.get_item_count('coal'); if want<=0 then return end;"
        # richest coal chest anywhere (the operator's stock chest)
        "local best,bn=nil,0; for _,c in pairs(s.find_entities_filtered{name={'wooden-chest','steel-chest','iron-chest'}}) do local ci=c.get_inventory(defines.inventory.chest); local n=ci and ci.get_item_count('coal') or 0; if n>bn then bn=n; best=c end end;"
        "if best then local ci=best.get_inventory(defines.inventory.chest); local k=math.min(want,ci.get_item_count('coal')); if k>0 then local g=inv.insert{name='coal',count=k}; if g>0 then ci.remove{name='coal',count=g} end; want=want-g end end;"
        # fallback: lift coal off belts within reach (derpface parks at the coal mine)
        "if want>0 then for _,b in pairs(s.find_entities_filtered{name='transport-belt',position=p.position,radius=10}) do for ln=1,2 do local line=b.get_transport_line(ln); local n=line.get_item_count('coal'); if n>0 then local k=math.min(want,n); line.remove_item{name='coal',count=k}; inv.insert{name='coal',count=k}; want=want-k end end; if want<=0 then break end end end")


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


_REAP_PAUSE = False        # set True during a relocation build so the reaper doesn't kill new drills
_relocate_cooldown = {}    # ore -> lap index until which not to retry (set after a failed build)


def _ore_under_drills(ore):
    """(density_per_tile, live_count, cx, cy): the per-tile ore density of the patch UNDER the
    drills currently mining `ore`, the count of those drills, and their centroid. density_per_tile
    is the 'how thin is the patch we're on' signal - a sparse-edge outpost reads low even with many
    drills (the iron drought: 11 drills on a 425/tile edge while a 1071/tile field sat adjacent).

    CRITICAL (the 2026-06-29 thrash bug): this MUST measure density the SAME way `richest_spot`
    measures candidate patches - the ore summed over each drill's 5x5 footprint, averaged, then /25
    for per-tile - so on-patch and best-patch are apples-to-apples. The old version summed each
    drill's single actively-depleting `mining_target.amount` tile, which reads far lower than the
    5x5 average on the SAME patch (494 vs 532/tile here). That made a freshly-relocated outpost ON
    the richest patch still read 'thin + a richer patch exists' forever -> relocate every 12th lap
    on a false signal (build_mine_outpost's idempotency made each 'rebuild' a no-op, so it never
    converged). Measuring the 5x5 footprint kills the false trigger while still firing on a genuine
    drought (a sparse edge reads low; a dense field reads high)."""
    out = A._print(
        "/sc local s=game.surfaces[1]; local ds={}; local sx=0; local sy=0; local n=0;"
        "for _,d in pairs(s.find_entities_filtered{type='mining-drill'}) do local mt=d.mining_target;"
        f"  if mt and mt.name=='{ore}' then local x=math.floor(d.position.x); local y=math.floor(d.position.y);"
        "     ds[#ds+1]={x,y}; sx=sx+x; sy=sy+y; n=n+1 end end;"
        "if n==0 then rcon.print('0,0,0,0') return end;"
        "local cx=math.floor(sx/n); local cy=math.floor(sy/n);"
        f"local amt={{}}; for _,e in pairs(s.find_entities_filtered{{name='{ore}',position={{cx,cy}},radius=40}}) do"
        "  amt[math.floor(e.position.x)..','..math.floor(e.position.y)]=e.amount end;"
        "local tot=0; for _,p in pairs(ds) do local sum=0;"
        "  for dx=-2,2 do for dy=-2,2 do local v=amt[(p[1]+dx)..','..(p[2]+dy)]; if v then sum=sum+v end end end;"
        "  tot=tot+sum end;"
        "rcon.print(math.floor(tot/n/25)..','..n..','..cx..','..cy)").strip()
    try:
        avg, c, x, y = out.split(",")
        return int(avg), int(c), int(x), int(y)
    except ValueError:
        return 0, 0, 0, 0


def ensure_ore_supply(ore, lap=0, n=10, thin_tile=500, min_ratio=2.0, min_fresh_tile=400):
    """ARCHITECT-DERIVED self-relocation: when the patch UNDER the `ore` drills is thin AND a
    clearly richer patch exists, tear down the failing outpost and rebuild a fresh `n`-drill
    outpost on the DENSEST patch (`richest_spot`), updating STATE[ore] so haul_ore follows. This
    is the autonomous fix for the 2026-06-29 iron drought (11 drills on a 425/tile sparse EDGE
    while the 1071/tile dense field sat 14 tiles away, undrilled - Seth's 'drill the densest, not
    the sparse edge' rule, enforced continuously). Character-driven, so it runs from the main
    supply strand (NOT the science strand). Returns True if it relocated.

    Trigger (avoids thrash): the per-tile ore under the drills is thin (< thin_tile, or all drills
    dead) AND the best patch is >= min_ratio x richer (and >= min_fresh_tile/tile). A HEALTHY patch
    (copper at ~1054/tile) never relocates even when a richer patch exists elsewhere. The failing
    outpost is torn down (refunded) before the rebuild so build_mine_outpost won't see the old belt
    and skip; drills landing off-ore get reaped next lap (reap_dead_drills), so placement
    self-corrects. Touches only mine outposts, never operator base/power/pole layout."""
    global _REAP_PAUSE
    if lap < _relocate_cooldown.get(ore, 0):
        return False                                   # backed off after a recent failed attempt
    avg, live, cx, cy = _ore_under_drills(ore)
    if live and _mine_is_belt_fed(cx, cy):
        return False                                   # operator-managed belt-fed mine: never relocate
                                                       # (tears down the belt feed; also stops the no-op
                                                       # thrash when the patch dips below thin_tile but
                                                       # the best patch is its own peak)
    best = A.richest_spot(ore, 0, 0, radius=240)
    if not best:
        return False
    fx, fy, sum5 = best
    fresh_tile = sum5 // 25
    if fresh_tile < min_fresh_tile:
        return False                                   # best available patch is itself too thin
    thin = (live == 0) or (avg < thin_tile)
    richer = fresh_tile >= max(min_fresh_tile, int(avg * min_ratio))
    if not (thin and richer):
        return False
    if live and abs(fx - cx) < 6 and abs(fy - cy) < 6:
        return False                                   # best patch is where we already mine - no churn
    status.log(f"ensure_ore_supply({ore}): patch under drills thin ({avg}/tile, {live} drills) and "
               f"a richer patch exists ({fresh_tile}/tile @ {fx},{fy}) -> relocating")
    _note(f"relocating {ore} outpost -> {fx},{fy}")
    # SAFETY (learned the hard way 2026-06-29): build FIRST and only commit if it succeeds; never
    # leave the base with zero supply on a failed build. (a) sweep stranded iron plates into the
    # inventory so build_mine_outpost can craft its burner-inserter even while iron is tight;
    # (b) pause the reaper so the science strand doesn't kill freshly-placed drills mid-build;
    # (c) build on the fresh patch (build_mine_outpost clean-slates it, which also clears an
    # overlapping old sparse outpost); (d) on failure revert STATE + back off; on success tear down
    # any FAR old outpost (an overlapping one was already cleared by the clean-slate).
    _sweep_iron_plates()
    old_state = STATE.get(ore)
    STATE[ore] = (fx, fy, sum5)
    _REAP_PAUSE = True
    try:
        chest = build_mine_outpost(ore, n)
    except Exception as e:
        chest = None
        status.log(f"ensure_ore_supply({ore}): build raised {e}")
    finally:
        _REAP_PAUSE = False
    if not chest:
        STATE[ore] = old_state                         # revert: leave the old outpost as it was
        _relocate_cooldown[ore] = lap + 60             # ~20 min before retrying (don't spam)
        status.log(f"ensure_ore_supply({ore}): build failed, reverted + backing off")
        return False
    if old_state and (abs(old_state[0] - fx) > 30 or abs(old_state[1] - fy) > 30):
        A._print(                                      # remove the now-orphaned FAR old outpost
            "/sc local p=storage.derpface; if not (p and p.valid) then return end; local s=p.surface; local inv=p.get_main_inventory();"
            f"for _,e in pairs(s.find_entities_filtered{{position={{{old_state[0]},{old_state[1]}}},radius=26,name={{'burner-mining-drill','transport-belt','underground-belt','burner-inserter','wooden-chest'}}}}) do "
            "local fb=e.get_fuel_inventory(); if fb then for _,c in pairs(fb.get_contents()) do inv.insert{name=c.name,count=c.count} end end; "
            "local oi=e.get_output_inventory(); if oi then for _,c in pairs(oi.get_contents()) do inv.insert{name=c.name,count=c.count} end end; "
            "inv.insert{name=e.name,count=1}; e.destroy() end")
    status.log(f"ensure_ore_supply({ore}): rebuilt outpost @ {fx},{fy} -> chest {chest}")
    return True


def _sweep_iron_plates():
    """Pull stranded iron-plate from base containers + furnace/assembler outputs into derpface's
    inventory, so a build that needs to craft (e.g. a burner-inserter) isn't blocked while iron is
    tight (the relocate-while-starved trap). Server-side, no walk."""
    A._print(
        "/sc local p=storage.derpface; if not (p and p.valid) then return end; local s=p.surface; local inv=p.get_main_inventory();"
        "for _,e in pairs(s.find_entities_filtered{force='player'}) do local ivs={};"
        "  if e.type=='container' or e.type=='logistic-container' then ivs[#ivs+1]=e.get_inventory(defines.inventory.chest) end;"
        "  if e.type=='furnace' or e.type=='assembling-machine' then ivs[#ivs+1]=e.get_output_inventory() end;"
        "  for _,iv in pairs(ivs) do if iv then local c=iv.get_item_count('iron-plate'); if c>0 then local g=inv.insert{name='iron-plate',count=c}; if g>0 then iv.remove{name='iron-plate',count=g} end end end end end")


def relocate_exhausted_outposts(lap=0):
    """Each lap (main strand, when not gated): keep iron + copper supply alive by relocating any
    outpost whose patch is exhausting. Cheap when supply is healthy (just a density query)."""
    for ore in ("iron-ore", "copper-ore"):
        if ensure_ore_supply(ore, lap=lap):
            return True          # one relocation per lap (it's a long character build)
    return False


# scaled chain (more cable/circuit/inserter/belt/green so green volume keeps all labs running)
SCIENCE_CHAIN = ["iron-gear-wheel", "copper-cable", "copper-cable", "electronic-circuit",
                 "electronic-circuit", "inserter", "inserter", "transport-belt", "transport-belt",
                 "automation-science-pack", "automation-science-pack",
                 "logistic-science-pack", "logistic-science-pack", "logistic-science-pack",
                 "logistic-science-pack"]
SCIENCE_CELL = (0, -34)   # top-left of the I/O-chest science GRID
SCIENCE_COLS = 5          # cells per row -> a compact grid (not one long row) to minimize runs


def have_all(req):
    """ALL-OR-NOTHING build precondition (Seth, 2026-08-30: 'if you're missing materials,
    WAIT - don't build partial nonsense'). req = {item: count}. True only when EVERY item
    is in inventory."""
    return all(_count(item) >= n for item, n in req.items())


def build_io_cell(recipe, x, y):
    """One assembler UNIT with input/output chests + inserters (Seth's rule). Layout (7 wide,
    mid-row y+1): [input chest][in inserter][assembler 3x3][out inserter][output chest], + a
    pole. ALL-OR-NOTHING: materials verified up front and the ASSEMBLER goes first - the old
    order placed chests/inserters even when the assembler failed, littering orphan cells
    (the 2026-08-30 'what is this mess' screenshot). Returns True if the cell was built."""
    if A.on_ore(x, y, 8, 4):
        status.log(f"build_io_cell({recipe}) @({x},{y}) is ON AN ORE PATCH - refusing (mining only)")
        return False
    req = {"assembling-machine-1": 1, "wooden-chest": 2, "inserter": 2, "small-electric-pole": 1}
    if not have_all(req):
        status.log(f"build_io_cell({recipe}): missing materials {[k for k, n in req.items() if _count(k) < n]} - waiting, building NOTHING")
        return False
    r = A.place("assembling-machine-1", x + 2, y, clear=0).strip()   # assembler FIRST (3x3)
    if "BUILT" not in r:
        return False                                                  # site bad: nothing placed
    A.place("wooden-chest", x, y + 1, clear=0)                       # input chest
    A.place("inserter", x + 1, y + 1, direction=12, clear=0)         # in: pick W chest, drop E asm
    A.place("inserter", x + 5, y + 1, direction=12, clear=0)         # out: pick W asm, drop E chest
    A.place("wooden-chest", x + 6, y + 1, clear=0)                   # output chest
    A.place("small-electric-pole", x + 3, y + 3, clear=0)
    A._print(f"/sc local s=game.surfaces[1]; local a=s.find_entities_filtered{{name='assembling-machine-1',position={{{x+3},{y+1}}},radius=2}}[1]; if a then pcall(function() a.set_recipe('{recipe}') end) end")
    # VERIFY: the cell must reach a LIVE state (working, or merely waiting on ingredients -
    # both mean it is wired and powered). no_power / no recipe = it does nothing -> remove it.
    def _alive():
        st = A._print(
            f"/sc local s=game.surfaces[1]; local SN={{}}; for k,v in pairs(defines.entity_status) do SN[v]=k end;"
            f"local a=s.find_entities_filtered{{name='assembling-machine-1',position={{{x + 3},{y + 1}}},radius=2}}[1];"
            "rcon.print(a and (SN[a.status] or tostring(a.status)) or 'gone')").strip()
        return st in ("working", "item_ingredient_shortage", "full_output", "no_ingredients")
    if not build_worked(_alive, tries=3, delay=4):
        status.log(f"build_io_cell({recipe}) @({x},{y}): cell is dead (unpowered/no recipe) - removing it")
        A._print(
            f"/sc local s=game.surfaces[1]; local inv=storage.derpface.get_main_inventory();"
            f"for _,e in pairs(s.find_entities_filtered{{area={{{{{x - 1},{y - 1}}},{{{x + 8},{y + 4}}}}},"
            "  name={'assembling-machine-1','wooden-chest','inserter','small-electric-pole'}}) do"
            "  inv.insert{name=e.name,count=1}; e.destroy() end")
        return False
    return True


def power_row(x1, x2, y, spacing=5):
    """Lay a CONTINUOUS pole line from x1..x2 at row y (poles <= `spacing` apart so the wires
    always chain - the #1 cause of 'new machines unpowered'), bridge it to the base network, then
    verify and patch any still-unpowered machine nearby. Returns count of unpowered remaining."""
    A.purpose("power poles so the new machines get electricity")
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


def ensure_science_cells():
    """DELTA-build science io-cells: for each SCIENCE_CHAIN recipe short of live assemblers,
    build just those cells at the next free grid slots. setup_science_io's all-or-nothing
    idempotency left the two automation-science-pack cells UNBUILT forever (2026-08-29:
    green flowed, red had no assembler, labs idle, research 0%)."""
    import collections
    want = collections.Counter(SCIENCE_CHAIN)
    have_raw = A._print(
        "/sc local s=game.surfaces[1]; local t={};"
        "for _,a in pairs(s.find_entities_filtered{type='assembling-machine'}) do"
        "  local r=a.get_recipe(); if r then t[#t+1]=r.name end end;"
        "rcon.print(table.concat(t,','))").strip()
    have = collections.Counter(x for x in have_raw.split(",") if x)
    missing = []
    for recipe, n in want.items():
        missing += [recipe] * max(0, n - have.get(recipe, 0))
    if not missing:
        return 0
    bx, by = SCIENCE_CELL
    cols = SCIENCE_COLS
    total = len(SCIENCE_CHAIN)
    built = 0
    if _count("assembling-machine-1") < len(missing):
        make("assembling-machine-1", len(missing) - _count("assembling-machine-1"))
    for k, recipe in enumerate(missing):
        slot = total + k          # append past the chain grid so we never overlap live cells
        col, row = slot % cols, slot // cols
        if build_io_cell(recipe, bx + col * 8, by + row * 5):
            built += 1
    if built:
        status.log(f"ensure_science_cells: built {built} missing cell(s): {missing}")
        power_row(bx, bx + cols * 8, by + ((total + len(missing)) // cols) * 5 + 3)
    return built


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
        "      local have=math.min(want, inv.get_item_count(ing.name)); if have>0 then local ins=a.insert{name=ing.name,count=have}; if ins>0 then inv.remove{name=ing.name,count=ins} end end end end;"
        "    local oo=a.get_output_inventory(); if oo then for _,c in pairs(oo.get_contents()) do local g=inv.insert{name=c.name,count=c.count}; if g>0 then oo.remove{name=c.name,count=g} end end end end end;"
        # fill each lab's FEED CHEST (the chest above each lab); its inserter pushes packs into
        # the lab continuously, so all labs run evenly (hardware feed, Seth's rule). Top each
        # feed chest to a buffer of each pack.
        "for _,lab in pairs(s.find_entities_filtered{name='lab'}) do "
        "  local ch=s.find_entities_filtered{name='wooden-chest',position={lab.position.x,lab.position.y-2},radius=1.5}[1]; "
        "  if ch then local ci=ch.get_inventory(defines.inventory.chest); for _,pk in ipairs({'automation-science-pack','logistic-science-pack','chemical-science-pack'}) do "
        "    local want=8-ci.get_item_count(pk); local n=math.min(want,inv.get_item_count(pk)); if n>0 then ci.insert{name=pk,count=n}; inv.remove{name=pk,count=n} end end end end")


def automate_green_science(origin=(30, -16)):
    """Build the science-pack assembler chain - BOTH packs - so research keeps advancing.

    RED IS IN THIS LIST NOW, and its absence was the deadlock. The chain was green-only and
    every green link already existed, so this returned immediately on every pass while NOTHING
    on the map made automation-science-pack. The result on 2026-08-30: 28 furnaces sat at
    full_output because no consumer drained plates, iron fell to 5/min and copper to 6/min,
    all 10 labs read missing_science_packs, and the controller correctly diagnosed it every
    lap - "the base needs a plate CONSUMER, not a lane repair" - with no stage able to act.

    Red is also the cheapest plate sink the base has (1 copper-plate + 1 iron-gear-wheel), so
    the same build unjams the smelters and feeds the labs.

    The generic service_science() shuffles intermediates via inventory, so we just place
    assemblers + set recipes. Powered off the base network. Idempotent: skips recipes already
    running.
    """
    chain = ["copper-cable", "electronic-circuit", "inserter", "transport-belt",
             "automation-science-pack", "automation-science-pack",
             "logistic-science-pack", "logistic-science-pack"]
    existing = A._print("/sc local s=game.surfaces[1]; local r={}; for _,a in pairs(s.find_entities_filtered{type='assembling-machine'}) do local rc=a.get_recipe(); if rc then r[#r+1]=rc.name end end; rcon.print(table.concat(r,','))").strip().split(",")
    need = [r for r in chain if existing.count(r) < chain.count(r)]
    if not need:
        return
    n = len(need)
    if _count("assembling-machine-1") < n:
        make("assembling-machine-1", n - _count("assembling-machine-1"))
    ox, oy = origin
    A.now("phase 0: building the science-pack assembler chain")
    A.stop(); A.walk(ox, oy + 4, tol=3.0)
    # NO BLANKET clear_area HERE. It used to be `clear_area(ox+n*2, oy, n*2+10)` - a destroy
    # whose RADIUS SCALED WITH HOW MUCH WAS MISSING. It never fired while the chain was
    # green-only and complete (`need` was empty, so the function returned above it); the moment
    # red science was added to the chain it fired with n=2 and destroyed the two existing green
    # assemblers it was walking over to reach. More missing -> bigger blast radius -> more of
    # the base destroyed, which is precisely backwards.
    # Each machine now clears only its own footprint, and an occupied slot is SKIPPED rather
    # than cleared: A.place refuses on ground it cannot legally build on.
    placed = 0
    slot = 0
    for recipe in need:
        x = None
        for _ in range(24):                      # walk the row for the next free 3x3 slot
            x = ox + slot * 4
            slot += 1
            r = A.place("assembling-machine-1", x, oy, clear=1).strip()
            if "BUILT" in r:
                A._print(f"/sc local s=game.surfaces[1]; local a=s.find_entities_filtered{{name='assembling-machine-1',position={{{x+1},{oy+1}}},radius=2}}[1]; if a then pcall(function() a.set_recipe('{recipe}') end) end")
                placed += 1
                A.place("small-electric-pole", x + 1, oy + 3, clear=1)
                break
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
        "local b=s.find_entities_filtered{name='boiler'}[1]; if b then local need=25-b.get_fuel_inventory().get_item_count('coal'); local c=math.min(need,inv.get_item_count('coal')); if c>0 then b.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end end;"
        "local bc=nil; if b then bc=s.find_entities_filtered{name='wooden-chest',position=b.position,radius=6}[1] end; if bc then local ci=bc.get_inventory(defines.inventory.chest); local need=120-ci.get_item_count('coal'); local c=math.min(need,inv.get_item_count('coal')); if c>0 then ci.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end end")
    # v2 fresh map: the autopilot OWNS the whole layout (the hands-off rule was for the
    # retired human-built base), so grid self-healing is back ON.
    ensure_grid_connected()
    fix_unpowered()


def fix_unpowered(limit=8):
    """Consumer-side power self-heal: for electric consumers reading no_power, place a small
    pole adjacent (from carried poles; script-crafts a few from plates when short) and bridge
    it to the engine's network if it landed as an island. power_row VERIFIED coverage but
    nothing ACTED on the gaps - the 2026-08-29 science-cell stall (5 assemblers + inserters
    dark on a healthy grid) was exactly this. Server-side, no walk."""
    if operator_present():
        return 0
    A._print(
        "/sc local p=storage.derpface; if not (p and p.valid) then return end; local s=p.surface; local f=p.force; local inv=p.get_main_inventory();"
        "local eng=s.find_entities_filtered{name='steam-engine'}[1]; if not eng or eng.energy<=0 then return end; local main=eng.electric_network_id;"
        # wood fallback: cleared base land has no trees in pocket - harvest a few server-side
        "if inv.get_item_count('wood')<2 and inv.get_item_count('small-electric-pole')<1 then"
        "  local n=0; for _,tr in pairs(s.find_entities_filtered{type='tree',position=p.position,radius=90}) do"
        "    if n>=4 then break end; inv.insert{name='wood',count=4}; tr.destroy(); n=n+1 end end;"
        "if inv.get_item_count('small-electric-pole')<4 then local w=inv.get_item_count('wood'); local cc=inv.get_item_count('copper-cable');"
        "  if cc<2 and inv.get_item_count('copper-plate')>=1 then inv.remove{name='copper-plate',count=1}; inv.insert{name='copper-cable',count=2}; cc=cc+2 end;"
        "  if w>=1 and cc>=2 then inv.remove{name='wood',count=1}; inv.remove{name='copper-cable',count=2}; inv.insert{name='small-electric-pole',count=1} end end;"
        "local fixed=0;"
        "for _,e in pairs(s.find_entities_filtered{type={'assembling-machine','lab','inserter'},position={0,0},radius=250}) do"
        f"  if fixed>={limit} then break end;"
        "  if e.status==defines.entity_status.no_power and inv.get_item_count('small-electric-pole')>0 then"
        "    for _,off in pairs({{2,0},{-2,0},{0,2},{0,-2},{2,2},{-2,-2}}) do"
        "      local x,y=math.floor(e.position.x)+off[1]+0.5,math.floor(e.position.y)+off[2]+0.5;"
        "      if s.can_place_entity{name='small-electric-pole',position={x,y},force=f} then"
        "        local np=s.create_entity{name='small-electric-pole',position={x,y},force=f};"
        "        if np then inv.remove{name='small-electric-pole',count=1}; fixed=fixed+1;"
        "          if np.electric_network_id~=main then local near,bd=nil,1e9;"
        "            for _,q in pairs(s.find_entities_filtered{type='electric-pole'}) do if q.electric_network_id==main then local d=(q.position.x-np.position.x)^2+(q.position.y-np.position.y)^2; if d<bd then bd=d; near=q end end end;"
        "            if near then local ex,ey,tx,ty=np.position.x,np.position.y,near.position.x,near.position.y; local steps=math.ceil(math.sqrt(bd)/6);"
        "              for k=1,steps do if inv.get_item_count('small-electric-pole')<1 then break end; local bx,by=math.floor(ex+(tx-ex)*k/steps)+0.5,math.floor(ey+(ty-ey)*k/steps)+0.5;"
        "                if s.can_place_entity{name='small-electric-pole',position={bx,by},force=f} then s.create_entity{name='small-electric-pole',position={bx,by},force=f}; inv.remove{name='small-electric-pole',count=1} end end end end;"
        "          break end end end end end")


def _lane_connected(ore):
    """BFS the mine's ore lane via belt connectivity: True if it reaches the smelter array's
    ore-belt intake. Catches EVERY break class (gaps, misaligned rows, wrong-direction joins)
    that tile-local checks miss."""
    spot = STATE.get(ore) or A.richest_spot(ore, 0, 0, radius=160)
    if not spot:
        return True                      # nothing to verify yet
    rx, ry = int(spot[0]), int(spot[1])
    ox, oy = SMELT_ZONE[ore]
    ax, ay = ox - 1, oy + 5              # the array intake corner connect_mine_to_array targets
    out = A._print(
        "/sc local s=game.surfaces[1]; local seen={}; local q={}; local best=1e9;"
        f"for _,b in pairs(s.find_entities_filtered{{position={{{rx},{ry}}},radius=26,type='transport-belt'}}) do q[#q+1]=b end;"
        "local n=0;"
        "local DV={[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}};"
        # dedupe BEFORE pushing and raise the cap: the old version pushed duplicates and
        # burned its 800-pop budget before reaching the array, so a PERFECTLY CONTINUOUS
        # copper lane kept reading "NOT connected" and triggering pointless re-lays.
        "while #q>0 and n<6000 do local b=table.remove(q); n=n+1;"
        "  local k=math.floor(b.position.x)..':'..math.floor(b.position.y);"
        "  if not seen[k] then seen[k]=true;"
        f"    local d=math.abs(b.position.x-({ax}))+math.abs(b.position.y-({ay}));"
        "    if d<best then best=d end;"
        "    for _,o in pairs(b.belt_neighbours.outputs) do"
        "      if o.type=='transport-belt' and not seen[math.floor(o.position.x)..':'..math.floor(o.position.y)] then q[#q+1]=o"
        # geometric underground hop: 2.1 belt_neighbours omits the partner entirely
        "      elseif o.type=='underground-belt' and o.belt_to_ground_type=='input' then"
        "        local dv=DV[o.direction];"
        "        for k2=1,6 do local px,py=o.position.x+dv[1]*k2, o.position.y+dv[2]*k2;"
        "          local pr=s.find_entities_filtered{position={px,py},radius=0.4,name='underground-belt'}[1];"
        "          if pr and pr.belt_to_ground_type=='output' and pr.direction==o.direction then"
        "            for _,o2 in pairs(pr.belt_neighbours.outputs) do if o2.type=='transport-belt' then q[#q+1]=o2 end end;"
        f"            local d2=math.abs(px-({ax}))+math.abs(py-({ay})); if d2<best then best=d2 end;"
        "            break end end end end end end;"
        "rcon.print(math.floor(best))").strip()
    try:
        return int(out) <= 6
    except ValueError:
        return True


def scrub_mixed_ore():
    """Remove WRONG-ORE items from each smelter array's intake belts + furnace inputs (the
    shared-column era put copper on the iron lane; a furnace fed the wrong ore mixes plates
    on the output belt - GOTCHAS: never mix ores)."""
    if operator_present():
        return 0
    for ore, (ox, oy) in SMELT_ZONE.items():
        A._print(
            f"/sc local s=game.surfaces[1]; local inv=storage.derpface and storage.derpface.valid and storage.derpface.get_main_inventory(); if not inv then return end; local n=0;"
            f"for _,b in pairs(s.find_entities_filtered{{area={{{{{ox - 3},{oy + 4}}},{{{ox + 34},{oy + 6}}}}},type='transport-belt'}}) do"
            "  for li=1,b.get_max_transport_line_index() do local L=b.get_transport_line(li);"
            "    for _,it in pairs(L.get_contents()) do"
            f"      if it.name~='{ore}' and it.name~='coal' then local r=L.remove_item{{name=it.name,count=it.count}}; if r>0 then inv.insert{{name=it.name,count=r}}; n=n+r end end end end end;"
            f"for _,fu in pairs(s.find_entities_filtered{{area={{{{{ox - 2},{oy}}},{{{ox + 34},{oy + 4}}}}},name={{'stone-furnace','steel-furnace'}}}}) do"
            "  local fi=fu.get_inventory(defines.inventory.furnace_source);"
            "  if fi then for _,it in pairs(fi.get_contents()) do"
            f"    if it.name~='{ore}' then local g=inv.insert{{name=it.name,count=it.count}}; if g>0 then fi.remove{{name=it.name,count=g}}; n=n+g end end end end end;"
            "if n>0 then game.print('scrub_mixed_ore: pulled '..n..' wrong-ore items') end")


def plan_mine_geometry(ore, apply=True):
    """PLAN-THEN-PLACE for a mine row (Seth, 2026-08-30: "check space requirements and outputs
    before placing anything; a plan should be in place to ensure routing").

    Computes the intended layout from the DRILLS themselves - drill row, its drop row, the
    belt lane - then makes the world match it:
      1. the drop row must be a clear BELT lane: anything else there (poles I dropped on it,
         spilled items) is moved/collected, never left to block the lane;
      2. every drill's drop_position must land ON that lane; a drill that doesn't is rotated
         or nudged so it does - the drill is ADJUSTED, never reverted to a worse tier;
      3. missing lane tiles are filled, all pointing at the lane exit.
    Returns a dict describing the plan and what changed."""
    spot = STATE.get(ore) or A.richest_spot(ore, 0, 0, radius=160)
    if not spot:
        return {"error": "no patch"}
    rx, ry = int(spot[0]), int(spot[1])
    raw = A._print(
        f"/sc local s=game.surfaces[1]; local o={{}};"
        f"for _,d in pairs(s.find_entities_filtered{{position={{{rx},{ry}}},radius=26,type='mining-drill'}}) do"
        "  local dp=d.drop_position;"
        "  o[#o+1]=d.name..'|'..math.floor(d.position.x)..'|'..math.floor(d.position.y)..'|'..d.direction"
        "        ..'|'..math.floor(dp.x)..'|'..math.floor(dp.y) end;"
        "rcon.print(table.concat(o,';'))").strip()
    drills = []
    for rec in raw.split(";"):
        f = rec.split("|")
        if len(f) == 6:
            drills.append({"name": f[0], "x": int(f[1]), "y": int(f[2]), "d": int(f[3]),
                           "dx": int(f[4]), "dy": int(f[5])})
    if not drills:
        return {"error": "no drills"}
    # the lane row is where most drills already drop
    from collections import Counter
    lane_y = Counter(d["dy"] for d in drills).most_common(1)[0][0]
    xs = [d["x"] for d in drills]
    lo, hi = min(xs) - 1, max(xs) + 2
    plan = {"ore": ore, "lane_y": lane_y, "span": [lo, hi], "drills": len(drills)}
    if not apply:
        return plan
    # 1) clear the lane row of NON-belt obstructions (my own poles ended up here), collect items
    A._print(
        f"/sc local s=game.surfaces[1]; local inv=storage.derpface.get_main_inventory(); local moved=0;"
        "local kept=0; local keptnames='';"
        f"for _,e in pairs(s.find_entities_filtered{{area={{{{{lo},{lane_y}}},{{{hi},{lane_y + 1}}}}}}}) do"
        "  local ty=e.type;"
        "  if ty=='item-entity' then local st=e.stack; if st and st.valid_for_read then inv.insert{name=st.name,count=st.count} end; e.destroy(); moved=moved+1"
        "  elseif ty=='electric-pole' then"
        # relocate the pole two tiles off the lane instead of deleting the power link
        "    local px,py=e.position.x,e.position.y; local nm=e.name; inv.insert{name=nm,count=1}; e.destroy();"
        "    for _,off in pairs({{0,-2},{0,2},{-2,0},{2,0}}) do"
        "      if inv.get_item_count(nm)>0 and s.can_place_entity{name=nm,position={px+off[1],py+off[2]},force='player'} then"
        "        local np=s.create_entity{name=nm,position={px+off[1],py+off[2]},force='player'};"
        "        if np then inv.remove{name=nm,count=1}; break end end end; moved=moved+1"
        # WHITELIST WHAT MAY BE DESTROYED; NEVER BLACKLIST WHAT MAY NOT. This clause used to
        # read "anything that is not a belt / drill / resource / character", which silently
        # included INSERTERS - and it removed every inserter taking finished plates out of the
        # iron smelters (Seth, 2026-08-30: "another fucking dumb mistake"). An area clear that
        # names what it SPARES will always eat something nobody thought to name. Debris comes
        # out; MACHINERY never does - the lane routes around it or the caller re-sites.
        "  elseif ty=='tree' or ty=='simple-entity' then"
        "    inv.insert{name=e.name,count=1}; e.destroy(); moved=moved+1"
        "  elseif ty~='transport-belt' and ty~='underground-belt' and ty~='mining-drill'"
        "         and ty~='resource' and e.name~='character' then"
        "    kept=kept+1; keptnames=keptnames..e.name..' ' end end;"
        "if moved>0 then game.print('lane row cleared: '..moved..' debris') end;"
        "if kept>0 then game.print('lane row: LEFT '..kept..' machine(s) standing ('..keptnames"
        "..')- a lane routes AROUND machinery, it does not bulldoze it') end")
    # 2) fill the lane across the span (direction set later by fix_mine_row_flow)
    A._print(
        f"/sc local s=game.surfaces[1]; local f=game.forces.player; local inv=storage.derpface.get_main_inventory(); local made=0;"
        f"for x={lo},{hi} do"
        f"  if #s.find_entities_filtered{{position={{x+0.5,{lane_y}+0.5}},radius=0.4,type={{'transport-belt','underground-belt'}}}}==0 then"
        "    if inv.get_item_count('transport-belt')<1 then"
        "      local g=inv.get_item_count('iron-gear-wheel'); local pl=inv.get_item_count('iron-plate');"
        "      if g>=1 and pl>=1 then inv.remove{name='iron-gear-wheel',count=1}; inv.remove{name='iron-plate',count=1}; inv.insert{name='transport-belt',count=2}"
        "      elseif pl>=3 then inv.remove{name='iron-plate',count=3}; inv.insert{name='transport-belt',count=2} end end;"
        f"    if inv.get_item_count('transport-belt')>0 and s.can_place_entity{{name='transport-belt',position={{x+0.5,{lane_y}+0.5}},direction=4,force=f}} then"
        f"      s.create_entity{{name='transport-belt',position={{x+0.5,{lane_y}+0.5}},direction=4,force=f}};"
        "      inv.remove{name='transport-belt',count=1}; made=made+1 end end end;"
        "if made>0 then game.print('lane tiles filled: '..made) end")
    # 3) ADJUST each drill (rotate, then nudge) until its drop lands on the lane - never revert
    fixed = A._print(
        f"/sc local s=game.surfaces[1]; local f=game.forces.player; local inv=storage.derpface.get_main_inventory(); local n=0;"
        f"local function onlane(p) return math.floor(p.y)=={lane_y} and #s.find_entities_filtered{{position=p,radius=0.5,type={{'transport-belt','underground-belt'}}}}>0 end;"
        f"for _,d in pairs(s.find_entities_filtered{{position={{{rx},{ry}}},radius=26,type='mining-drill'}}) do"
        "  if not onlane(d.drop_position) then"
        "    local nm,pos,dir=d.name,d.position,d.direction; local done=false;"
        "    for _,nd in pairs({0,4,8,12}) do if not done then"
        "      d.direction=nd; if onlane(d.drop_position) then done=true; n=n+1 end end end;"
        "    if not done then"                       # rotation failed: nudge the drill one row
        "      d.direction=dir; inv.insert{name=nm,count=1}; d.destroy();"
        "      for _,off in pairs({{0,-1},{0,1},{-1,0},{1,0}}) do"
        "        if inv.get_item_count(nm)>0 then"
        "          local np={pos.x+off[1], pos.y+off[2]};"
        "          if s.can_place_entity{name=nm,position=np,direction=8,force=f} then"
        "            local e=s.create_entity{name=nm,position=np,direction=8,force=f};"
        "            if e and onlane(e.drop_position) then inv.remove{name=nm,count=1}; n=n+1; break"
        "            elseif e then e.destroy() end end end end end end end;"
        "rcon.print(n)").strip()
    plan["drills_adjusted"] = fixed
    fix_mine_row_flow(ore)
    status.log(f"plan_mine_geometry({ore}): lane y={lane_y} span={lo}..{hi}, drills adjusted={fixed}")
    return plan


def fix_mine_row_flow(ore):
    """FLOW-DIRECTION self-heal for a mine's drop row (GOTCHAS: belt flow must point AT the
    consumer). The iron row was half-west half-east (drills fed both; the east half ran ore
    to a dead end and the array starved with every belt EMPTY). Find the row's EXIT (the belt
    whose output leaves the row - the lane toward base) and point every row belt at it."""
    spot = STATE.get(ore) or A.richest_spot(ore, 0, 0, radius=160)
    if not spot:
        return
    rx, ry = int(spot[0]), int(spot[1])
    bx, by = SMELT_ZONE["iron-ore"]        # the base direction every lane ultimately serves
    A._print(
        f"/sc local s=game.surfaces[1]; local row={{}};"
        # radius 42: the true exit (corner into the lane column) sat OUTSIDE the old radius-26
        # scan, so the fallback picked the WESTMOST belt and pointed the whole coal row AWAY
        # from the base - repeatedly, fighting manual fixes (2026-08-30)
        f"for _,b in pairs(s.find_entities_filtered{{position={{{rx},{ry}}},radius=42,type='transport-belt'}}) do"
        "  row[#row+1]=b end;"
        "if #row<4 then return end;"
        "local ycnt={}; for _,b in pairs(row) do local y=math.floor(b.position.y); ycnt[y]=(ycnt[y] or 0)+1 end;"
        "local ry2,best=nil,0; for y,c in pairs(ycnt) do if c>best then best=c; ry2=y end end;"
        # exit = a row belt whose output leaves the row; fallback = the row end NEAREST THE
        # BASE (never a compass assumption)
        "local exitx=nil;"
        "for _,b in pairs(row) do local y=math.floor(b.position.y);"
        "  if y==ry2 then for _,o in pairs(b.belt_neighbours.outputs) do"
        "    if math.floor(o.position.y)~=ry2 then exitx=math.floor(b.position.x) end end end end;"
        "if not exitx then local mn,mx=1e9,-1e9; for _,b in pairs(row) do local y=math.floor(b.position.y);"
        "  if y==ry2 then local x=math.floor(b.position.x); if x<mn then mn=x end; if x>mx then mx=x end end end;"
        f"  local bx={bx};"
        "  exitx=(math.abs(mn-bx)<=math.abs(mx-bx)) and mn or mx end;"
        # X-SPAN GUARD: only belts within the DRILL span (+6) belong to this mine's row.
        # Without it the iron row (y=-42, radius 42) swept in the COPPER column's crossing
        # tile at (-10,-42) and re-pointed it east every cycle - the invisible hand that kept
        # breaking the copper lane all evening (2026-08-30).
        "local dminx,dmaxx=1e9,-1e9;"
        f"for _,d in pairs(s.find_entities_filtered{{position={{{rx},{ry}}},radius=30,type='mining-drill'}}) do"
        "  local dx=math.floor(d.position.x); if dx<dminx then dminx=dx end; if dx>dmaxx then dmaxx=dx end end;"
        "if dminx>dmaxx then dminx,dmaxx=exitx,exitx end;"
        # span = THE DRILLS ONLY (+2). Including exitx dragged the span across other lanes:
        # the iron row's exit at x=-8 made its span reach x=-14, so it re-pointed the COPPER
        # column's tile at (-10,-42) east on every cycle - the copper lane's true killer.
        "local lo,hi=dminx-2, dmaxx+2;"
        "local n=0; local skipped=0;"
        "for _,b in pairs(row) do local y=math.floor(b.position.y);"
        "  if y==ry2 then local x=math.floor(b.position.x);"
        "    if x>=lo and x<=hi then"
        "    local want=(x>exitx) and 12 or ((x<exitx) and 4 or b.direction);"
        # never touch the exit/corner zone (exitx +-1): a bulk re-point once swept the corner
        # east and the full row dead-ended at it (copper, 2026-08-30)
        # A BELT CARRYING ITEMS IS ALREADY DOING A JOB, AND ROTATING IT ENDS THAT JOB. Only
        # re-point a tile that is EMPTY: a loaded belt is evidence that something upstream is
        # feeding it and something downstream is meant to receive it, and neither of those is
        # visible from a row scan. Seth, 2026-08-30: "you turned a belt for some reason, never
        # do that without measuring the outcome" - the turn that put raw copper ore onto the
        # PLATE OUTPUT belt. Turning an empty tile is recoverable; turning a live one silently
        # re-routes a working line into the wrong destination.
        "    local carrying=0;"
        "    for li=1,b.get_max_transport_line_index() do carrying=carrying+#b.get_transport_line(li) end;"
        "    if b.direction~=want and math.abs(x-exitx)>1 then"
        "      if carrying>0 then skipped=skipped+1"
        "      else b.direction=want; n=n+1 end end end end end;"
        "if n>0 then game.print('fix_mine_row_flow: pointed '..n..' EMPTY belts at the row exit') end;"
        "if skipped>0 then game.print('fix_mine_row_flow: left '..skipped..' LOADED belt(s) alone"
        " - a belt with items on it is already carrying for someone') end")


_LANE_RELAYS = {}


def ensure_lanes(lap=0):
    """SOURCE-TO-DESTINATION lane law (Seth, 2026-08-29): a mine belt must reach its smelter
    array by belt connectivity - no chest shuttles where a belt belongs, no trusting that a lay
    finished. Verify each ore lane by BFS; a broken lane gets fully re-laid via
    connect_mine_to_array (exact-tile path layer joins misaligned rows with a corner)."""
    if operator_present():
        return 0                            # operator truce: never fight manual edits
    fixed = 0
    scrub_mixed_ore()
    fix_mine_row_flow("coal")               # no dedicated array lane yet; flow fix only
    for ore in ("iron-ore", "copper-ore"):
        try:
            fix_mine_row_flow(ore)
            if _LANE_RELAYS.get(ore, 0) >= 4:
                continue                      # already gave up / operator-owned
            if not _lane_connected(ore):
                n = _LANE_RELAYS.get(ore, 0)
                if n >= 3:
                    if n == 3:              # converged on failure: stop churning, flag it once
                        status.log(f"lane {ore}: re-lay NOT converging after 3 attempts - escalation needed")
                        import lessons as _l
                        _l.add(condition=f"lane {ore} re-lay does not converge",
                               mistake="connect_mine_to_array laid 3x, BFS still disconnected",
                               rule="architect must diagnose the routing (obstacle/geometry)",
                               tags=("controller", "belts"), key=f"lane-stuck:{ore}")
                        _LANE_RELAYS[ore] = 4
                    continue
                status.log(f"lane {ore}: NOT connected - re-laying (attempt {n + 1}/3)")
                A.purpose(f"re-laying the {ore} belt so it reaches the smelters")
                connect_mine_to_array(ore)
                _LANE_RELAYS[ore] = n + 1
                fixed += 1
            else:
                _LANE_RELAYS.pop(ore, None)
        except Exception as e:
            status.log(f"ensure_lanes({ore}): {e}")
    return fixed


_OP_CACHE = {"t": 0.0, "present": False}


def operator_present():
    """True while a real player is connected (Seth editing by hand). Layout-modifying
    self-heals SUSPEND during manual edits - the repairer un-deleting his deletions
    ("something keeps putting things back") was the final straw, 2026-08-30."""
    import time as _t
    if _t.monotonic() - _OP_CACHE["t"] < 10:
        return _OP_CACHE["present"]
    out = A._print("/sc rcon.print(#game.connected_players)").strip()
    _OP_CACHE["t"] = _t.monotonic()
    try:
        _OP_CACHE["present"] = int(out) > 0
    except ValueError:
        pass
    return _OP_CACHE["present"]


def repair_plate_rows():
    """Fill missing PLATE-output belt tiles on each smelter array's top row (the furnaces'
    output inserters were dropping onto bare ground - partial array builds; Seth's screenshot
    2026-08-30). Lays east-flowing belts from the array start to the drain chest column."""
    fixed = 0
    for ore, (ox, oy) in SMELT_ZONE.items():
        out = A._print(
            f"/sc local s=game.surfaces[1]; local p=storage.derpface; local inv=p.get_main_inventory(); local f=p.force; local n=0;"
            f"for x={ox - 1},{ox + 33} do"
            "  local tx=x+0.5;"
            f"  local has=#s.find_entities_filtered{{position={{tx,{oy}+0.5}},radius=0.4,type={{'transport-belt','underground-belt','splitter'}}}}>0;"
            f"  local blocked=#s.find_entities_filtered{{position={{tx,{oy}+0.5}},radius=0.4}}>0;"
            "  if not has and not blocked then"
            "    if inv.get_item_count('transport-belt')<1 then break end;"
            f"    local e=s.create_entity{{name='transport-belt',position={{tx,{oy}+0.5}},direction=4,force=f}};"
            "    if e then inv.remove{name='transport-belt',count=1}; n=n+1 end end end;"
            "rcon.print(n)").strip()
        try:
            n = int(out)
        except ValueError:
            n = 0
        if n:
            status.log(f"repair_plate_rows: {ore} array plate row +{n} belts")
            fixed += n
    return fixed





def _built_load():
    import json as _j
    import pathlib as _pl
    f = _pl.Path(__file__).resolve().parent / "built-tiles.json"
    try:
        return set(tuple(x) for x in _j.loads(f.read_text()))
    except (OSError, ValueError):
        return set()


def _built_save(tiles):
    import json as _j
    import pathlib as _pl
    f = _pl.Path(__file__).resolve().parent / "built-tiles.json"
    try:
        f.write_text(_j.dumps(sorted(list(tiles))))
    except OSError:
        pass


def record_built(tiles):
    """Remember every belt tile the BOT placed. Paired with reconcile_removals() this is the
    durable consent mechanism - it needs no login/logoff edge, survives restarts, and cannot
    be bypassed by a manual or architect-driven call."""
    if not tiles:
        return
    _built_save(_built_load() | set(tuple(t) for t in tiles))


def forget_built(tiles):
    """The bot removed these itself (teardown/supersede) - not an operator deletion."""
    if not tiles:
        return
    _built_save(_built_load() - set(tuple(t) for t in tiles))


def reconcile_removals():
    """THE RULE (Seth, 2026-08-30, after I rebuilt his deletions twice): if the bot built a
    tile, that tile is now EMPTY, and the bot did not remove it, then a human removed it -
    protect it forever and never rebuild. Runs continuously, so it does not depend on
    catching a logoff edge or on any in-memory snapshot surviving a restart."""
    built = _built_load()
    if not built:
        return 0
    known = sorted(built)[:900]
    spec = ";".join(f"{x},{y}" for (x, y) in known)
    out = A._print(
        "/sc local s=game.surfaces[1]; local gone={};"
        "for a,b in ([==[" + spec + "]==]):gmatch('(-?%d+),(-?%d+)') do"
        "  local x,y=tonumber(a),tonumber(b);"
        "  if #s.find_entities_filtered{position={x+0.5,y+0.5},radius=0.4,"
        "     type={'transport-belt','underground-belt','splitter'}}==0 then gone[#gone+1]=x..','..y end end;"
        "rcon.print(table.concat(gone,';'))").strip()
    gone = set(tuple(map(int, s.split(","))) for s in out.split(";") if "," in s)
    if not gone:
        return 0
    _protected_save(_protected_load() | gone)
    _built_save(built - gone)
    status.log(f"reconcile: {len(gone)} bot-built tiles were removed by the OPERATOR "
               f"-> protected forever (never rebuild)")
    return len(gone)


def _protected_load():
    """Tiles the OPERATOR deliberately cleared - the bot must never rebuild them."""
    import json as _j
    import pathlib as _pl
    f = _pl.Path(__file__).resolve().parent / "protected-tiles.json"
    try:
        return set(tuple(x) for x in _j.loads(f.read_text()))
    except (OSError, ValueError):
        return set()


def _protected_save(tiles):
    import json as _j
    import pathlib as _pl
    f = _pl.Path(__file__).resolve().parent / "protected-tiles.json"
    try:
        f.write_text(_j.dumps(sorted(list(tiles))))
    except OSError:
        pass


def belt_tiles_now():
    """Every belt/underground/splitter tile currently in the world (for edit diffing)."""
    out = A._print(
        "/sc local s=game.surfaces[1]; local o={};"
        "for _,b in pairs(s.find_entities_filtered{type={'transport-belt','underground-belt','splitter'}}) do"
        "  o[#o+1]=math.floor(b.position.x)..','..math.floor(b.position.y) end;"
        "rcon.print(table.concat(o,';'))").strip()
    return set(tuple(map(int, x.split(","))) for x in out.split(";") if "," in x)


def world_snapshot():
    """Compact snapshot of every player entity (name, tile, direction) for edit diffing."""
    out = A._print(
        "/sc local s=game.surfaces[1]; local o={};"
        "for _,e in pairs(s.find_entities_filtered{force='player'}) do"
        "  if e.name~='character' and #o<4000 then"
        "    o[#o+1]=e.name..'|'..math.floor(e.position.x)..'|'..math.floor(e.position.y)..'|'..(e.direction or 0) end end;"
        "storage._snap=table.concat(o,';'); rcon.print(#storage._snap)").strip()
    try:
        n = int(out)
    except ValueError:
        return {}
    parts, i = [], 1
    while i <= n:
        parts.append(A._print(f"/sc rcon.print(storage._snap:sub({i},{i + 2999}))").rstrip("\r\n"))
        i += 3000
    A._print("/sc storage._snap=nil")
    snap = {}
    for rec in "".join(parts).split(";"):
        f = rec.split("|")
        if len(f) == 4:
            snap[(f[0], int(f[1]), int(f[2]))] = int(f[3])
    return snap


def learn_from_operator_edits(before, after=None):
    """WHY did the operator change this? (Seth, 2026-08-30). Diff the base across his session,
    hand the change summary to the local 35B, and store its inferred INTENT as durable rules.
    The operator is the strongest signal available - he only touches what the bot got wrong."""
    import collections
    import json as _j
    if not before:
        return 0
    after = after or world_snapshot()
    removed = collections.Counter(k[0] for k in before if k not in after)
    added = collections.Counter(k[0] for k in after if k not in before)
    rotated = collections.Counter(k[0] for k in before if k in after and before[k] != after[k])
    if not (removed or added or rotated):
        return 0
    # a few concrete coordinates make the intent legible to the model
    ex_rm = [f"{k[0]}@{k[1]},{k[2]}" for k in list(before)[:4000] if k not in after][:12]
    ex_add = [f"{k[0]}@{k[1]},{k[2]}" for k in list(after)[:4000] if k not in before][:12]
    summary = {
        "removed_counts": dict(removed.most_common(10)),
        "added_counts": dict(added.most_common(10)),
        "rotated_counts": dict(rotated.most_common(6)),
        "removed_examples": ex_rm, "added_examples": ex_add,
    }
    status.log(f"operator edits: -{sum(removed.values())} +{sum(added.values())} "
               f"rot{sum(rotated.values())} - asking the architect why")
    try:
        import llm
        out = llm.chat_json(
            [{"role": "system", "content":
              "You analyze what an EXPERT Factorio player changed in a base that an autopilot "
              "built, and infer WHY. The player only touches things the bot got wrong, so each "
              "change is evidence of a bot mistake. Reply ONLY with a JSON array of at most 3 "
              'objects: [{"condition":"when the bot is doing X","mistake":"what the bot did '
              'wrong","rule":"the durable rule the bot must follow instead"}]. Be concrete and '
              "actionable (name entities/patterns, not vague advice). No prose outside the JSON."},
             {"role": "user", "content": "Base changes made by the operator this session:\n"
              + _j.dumps(summary, separators=(",", ":"))}],
            model=llm.ARCHITECT, max_tokens=700, tag="operator-learn")
    except Exception as e:
        status.log(f"learn_from_operator_edits: {e}")
        return 0
    rows = out if isinstance(out, list) else ([out] if isinstance(out, dict) else [])
    import lessons
    n = 0
    for r in rows[:3]:
        if not isinstance(r, dict) or not r.get("rule"):
            continue
        lessons.add(condition=r.get("condition", "operator edited the base"),
                    mistake=r.get("mistake", "?"), rule=r["rule"],
                    evidence=_j.dumps(summary)[:1500],
                    tags=("operator", "triage", "architect"),
                    key="operator:" + str(r.get("condition", ""))[:40])
        status.log(f"LEARNED from operator: {r['rule'][:160]}")
        n += 1
    return n


def record_operator_deletions(before):
    """Diff the world against a pre-session snapshot: tiles the operator REMOVED become
    PROTECTED forever (Seth, 2026-08-30: 'the belts I deleted seem to have returned').
    The bot cannot distinguish a deliberate deletion from damage, so the operator's edits
    are recorded as intent, not damage."""
    if not before:
        return 0
    after = belt_tiles_now()
    removed = before - after
    if not removed:
        return 0
    # A dead line here referenced an undefined `tiles` and raised NameError on EVERY operator
    # logoff - "record deletions: name 'tiles' is not defined" in the live log - so no deletion
    # was ever recorded, and the learn-from-edits hook behind it never ran either. It computed
    # nothing anything used, so it is deleted rather than repaired. THE LESSON is not the typo:
    # the logoff hook's only report of failure was one status line nobody read, in the one code
    # path whose whole job is to notice what the operator changed.
    prot = _protected_load() | removed
    _protected_save(prot)
    status.log(f"protected {len(removed)} operator-deleted tiles (never rebuild); total {len(prot)}")
    return len(removed)


def cleanup_orphan_cells():
    """Remove orphan io-cell furniture (chests/inserters/poles with NO assembler within 3
    tiles) in the science region - the partial cells the old build order littered. Runs on
    operator logoff (Seth: 'once I log off clean this shit up'). Refunds everything."""
    bx, by = SCIENCE_CELL
    out = A._print(
        f"/sc local s=game.surfaces[1]; local inv=storage.derpface.get_main_inventory(); local n=0;"
        f"for _,e in pairs(s.find_entities_filtered{{area={{{{{bx - 4},{by - 4}}},{{{bx + 90},{by + 40}}}}},name={{'wooden-chest','inserter','small-electric-pole'}}}}) do"
        "  local a=s.find_entities_filtered{type='assembling-machine',position=e.position,radius=3.5}[1];"
        "  local l=s.find_entities_filtered{name='lab',position=e.position,radius=3.5}[1];"
        "  if not a and not l then"
        "    local ci=e.get_inventory and (e.get_inventory(defines.inventory.chest) or e.get_inventory(defines.inventory.fuel));"
        "    if ci then for _,c in pairs(ci.get_contents()) do inv.insert{name=c.name,count=c.count} end end;"
        "    inv.insert{name=e.name,count=1}; e.destroy(); n=n+1 end end;"
        "rcon.print(n)").strip()
    try:
        n = int(out)
    except ValueError:
        n = 0
    if n:
        status.log(f"cleanup_orphan_cells: removed {n} orphaned cell pieces (refunded)")
    return n


def coal_to_boiler():
    """SELF-SUSTAINING POWER (Seth, 2026-08-30): belt coal from the coal lane to the boiler
    with a splitter tap + burner inserter, so the plant feeds itself and keep_power's
    hand-feed becomes a backup. Idempotent: skips when a fed boiler inserter exists."""
    if operator_present():
        return False
    b = A._print(
        "/sc local s=game.surfaces[1]; local b=s.find_entities_filtered{name='boiler',limit=1}[1];"
        "if not b then rcon.print('none') return end;"
        "local bi=s.find_entities_filtered{position=b.position,radius=3,name='burner-inserter'}[1];"
        "local fed=false;"
        "if bi then local pp=bi.pickup_position;"
        "  fed=#s.find_entities_filtered{position=pp,radius=0.5,type='transport-belt'}>0 end;"
        "rcon.print(math.floor(b.position.x)..','..math.floor(b.position.y)..','..tostring(fed))").strip()
    if b == "none":
        return False
    bx, by, fed = b.split(",")
    if fed == "true":
        return True
    bx, by = int(bx), int(by)
    A.purpose("belting coal to the boiler so power self-sustains")
    # materials
    if _count("transport-belt") < 40:
        make("transport-belt", 45)
    if _count("splitter") < 1:
        make("splitter", 1)
    if _count("burner-inserter") < 1:
        make("burner-inserter", 1)
    # splitter tap on the coal row (y=15, flowing east): span y15-16 at x=-36
    A._print(
        "/sc local s=game.surfaces[1]; local f=game.forces.player; local inv=storage.derpface.get_main_inventory();"
        "if #s.find_entities_filtered{position={-35.5,16.0},radius=0.6,name='splitter'}==0 then"
        "  local old=s.find_entities_filtered{position={-35.5,15.5},radius=0.4,type='transport-belt'}[1]; if old then old.destroy() end;"
        "  local sp=s.create_entity{name='splitter',position={-35.5,16.0},direction=4,force=f};"
        "  if sp then inv.remove{name='splitter',count=1} end end")
    # branch belt: from the splitter's south output east one tile, then a column south to the
    # boiler row, then west to the inserter pickup tile
    lay_belt_path([(-35, 16), (-34, 16), (-34, by - 1), (-36, by - 1), (-36, by), (-36, by + 1)])
    # burner inserter: picks the belt column (west), drops into the boiler (east)
    A._print(
        f"/sc local s=game.surfaces[1]; local f=game.forces.player; local inv=storage.derpface.get_main_inventory();"
        f"local b2=s.find_entities_filtered{{name='boiler',limit=1}}[1];"
        f"if #s.find_entities_filtered{{position={{{bx - 1},{by}}},radius=1.2,name='burner-inserter'}}==0 then"
        f"  local i=s.create_entity{{name='burner-inserter',position={{{bx - 1}+0.5,{by}+0.5}},direction=4,force=f}};"
        f"  if i then inv.remove{{name='burner-inserter',count=1}};"
        f"    i.pickup_position={{{bx - 2}+0.5,{by}+0.5}}; i.drop_position=b2.position;"
        "    local c=math.min(5,inv.get_item_count('coal')); if c>0 then i.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end end end")
    status.log("coal_to_boiler: splitter tap + belt + inserter placed")
    return True


def electrify_mines():
    """ELECTRIC DRILLS (Seth's priority): once electric-mining-drill is researched, swap each
    burner drill for an electric one AT THE EXACT POSITION/DIRECTION (GOTCHAS swap rule),
    after ensuring pole coverage at the mine. Idempotent + incremental (a few per pass)."""
    if not _tech_done("electric-mining-drill"):
        return 0
    burners = int(A._print("/sc rcon.print(#game.surfaces[1].find_entities_filtered{name='burner-mining-drill'})").strip() or "0")
    if burners == 0:
        return 0
    A.purpose("swapping burner drills for electric (mines self-power, no more coal fueling)")
    need = min(burners, 6)
    if _count("electric-mining-drill") < need:
        make("electric-mining-drill", need)
    for ore in ("iron-ore", "copper-ore", "coal"):
        spot = STATE.get(ore)
        if not spot:
            continue
        rx, ry = int(spot[0]), int(spot[1])
        # pole coverage first (electric drills on an unpowered mine = dead mine)
        import fle_tools
        try:
            fle_tools.connect((0, 0), (rx, ry), "pole")
        except Exception as e:
            status.log(f"electrify {ore}: pole run failed: {e}")
            continue
        if _count("small-electric-pole") < 6:
            make("small-electric-pole", 10)
        A._print(
            f"/sc local s=game.surfaces[1]; local f=game.forces.player; local inv=storage.derpface.get_main_inventory(); local n=0;"
            f"for _,d in pairs(s.find_entities_filtered{{position={{{rx},{ry}}},radius=26,name='burner-mining-drill'}}) do"
            "  if n>=3 or inv.get_item_count('electric-mining-drill')<1 then break end;"
            "  local pos,dir=d.position,d.direction;"
            "  local fi=d.get_fuel_inventory(); if fi then for _,c in pairs(fi.get_contents()) do inv.insert{name=c.name,count=c.count} end end;"
            "  inv.insert{name='burner-mining-drill',count=1}; d.destroy();"
            "  local e=s.create_entity{name='electric-mining-drill',position=pos,direction=dir,force=f};"
            # DROP-TARGET CHECK: electric drills are 3x3 vs burner 2x2, so the drop tile
            # MOVES - six copper drills silently dumped ore on the ground and the mine died
            # (2026-08-30). If the new drop tile isn't a belt/chest, undo the swap here.
            "  if e then local dp=e.drop_position; local ok=false;"
            "    for _,q in pairs(s.find_entities_filtered{position=dp,radius=0.5}) do"
            "      if q.type=='transport-belt' or q.type=='underground-belt' or q.type=='container' then ok=true end end;"
            "    if not ok then e.destroy(); e=nil end end;"   # geometry is repaired below, not reverted
            "  if e then inv.remove{name='electric-mining-drill',count=1}; n=n+1"
            "  else local rb=s.create_entity{name='burner-mining-drill',position=pos,direction=dir,force=f};"
            "    if rb then inv.remove{name='burner-mining-drill',count=1} end end end;"
            "if n>0 then game.print('electrified '..n..' drills') end")
        # VERIFY (build law #1/#2): an electric drill with no power MINES NOTHING. Patch the
        # power in; if it still can't be powered, REVERT that drill to burner rather than
        # leaving a dead machine on the map (this exact miss killed copper supply 2026-08-30).
        dead = A._print(
            f"/sc local s=game.surfaces[1]; local o={{}};"
            f"for _,d in pairs(s.find_entities_filtered{{position={{{rx},{ry}}},radius=26,name='electric-mining-drill'}}) do"
            "  if d.status==defines.entity_status.no_power then o[#o+1]=math.floor(d.position.x)..','..math.floor(d.position.y) end end;"
            "rcon.print(table.concat(o,';'))").strip()
        for tok in [s2 for s2 in dead.split(";") if "," in s2]:
            dx, dy = map(int, tok.split(","))
            try:
                import fle_tools
                fle_tools.connect((dx - 4, dy), (dx, dy), "pole")
            except Exception as e:
                status.log(f"electrify {ore}: pole patch failed at {dx},{dy}: {e}")
        still = A._print(
            f"/sc local s=game.surfaces[1]; local n=0;"
            f"for _,d in pairs(s.find_entities_filtered{{position={{{rx},{ry}}},radius=26,name='electric-mining-drill'}}) do"
            "  if d.status==defines.entity_status.no_power then n=n+1 end end; rcon.print(n)").strip()
        plan_mine_geometry(ore)        # ADJUST the lane/drills to the new footprint first
        if still not in ("0", ""):
            status.log(f"electrify {ore}: {still} drills STILL unpowered - reverting them to burner")
            A._print(
                f"/sc local s=game.surfaces[1]; local f=game.forces.player; local inv=storage.derpface.get_main_inventory();"
                f"for _,d in pairs(s.find_entities_filtered{{position={{{rx},{ry}}},radius=26,name='electric-mining-drill'}}) do"
                "  if d.status==defines.entity_status.no_power then local pos,dir=d.position,d.direction;"
                "    inv.insert{name='electric-mining-drill',count=1}; d.destroy();"
                "    if inv.get_item_count('burner-mining-drill')>0 then"
                "      local b=s.create_entity{name='burner-mining-drill',position=pos,direction=dir,force=f};"
                "      if b then inv.remove{name='burner-mining-drill',count=1};"
                "        local c=math.min(5,inv.get_item_count('coal')); if c>0 then b.insert{name='coal',count=c}; inv.remove{name='coal',count=c} end end end end end")
    return 1


def repair_belt_gaps(max_span=30):
    """BELT CONTINUITY self-heal: a lane with a mid-route break starves everything downstream
    (GOTCHAS: a belt lane must be CONTINUOUS). Interrupted lay_belt_path runs (restart mid-lay,
    out of belts) left dead-end lanes that idled all 39 furnaces on 2026-08-29. Each pass:
    find dead-end belts, and where the SAME lane resumes within max_span tiles in the belt's
    direction, bridge the span with belts from inventory (script-crafting from plates/gears if
    short). No continuation found = leave it (could be a legit terminus) - log only."""
    if operator_present():
        return 0
    A._print(
        "/sc local p=storage.derpface; if not (p and p.valid) then return end; local s=p.surface; local f=p.force; local inv=p.get_main_inventory();"
        "local D={[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}};"
        "local PROT={" + ",".join("['%d:%d']=true" % (x, y) for (x, y) in sorted(_protected_load())[:400]) + "};"
        # keep a small belt stock: craft from plates+gears server-side (2 belts per gear+plate)
        "if inv.get_item_count('transport-belt')<10 then"
        "  local g=inv.get_item_count('iron-gear-wheel'); local pl=inv.get_item_count('iron-plate');"
        "  while g<5 and pl>=2 do inv.remove{name='iron-plate',count=2}; inv.insert{name='iron-gear-wheel',count=1}; g=g+1; pl=pl-2 end;"
        "  local n=math.min(g, math.floor(pl/1), 8); if n>0 then inv.remove{name='iron-gear-wheel',count=n}; inv.remove{name='iron-plate',count=n}; inv.insert{name='transport-belt',count=n*2} end end;"
        "local fixed=0;"
        "for _,b in pairs(s.find_entities_filtered{type='transport-belt'}) do"
        "  if fixed>=12 then break end;"
        "  if #b.belt_neighbours.outputs==0 then"
        "    local d=D[b.direction];"
        "    if d then"
        "      local bx,by=b.position.x,b.position.y;"
        "      local resume=nil;"
        f"      for k=1,{max_span} do"
        "        local tx,ty=bx+d[1]*k, by+d[2]*k;"
        "        local nb=s.find_entities_filtered{position={tx,ty},radius=0.4,type='transport-belt'}[1];"
        "        if nb and nb.direction==b.direction then resume=k break end;"
        "        local hard=s.find_entities_filtered{position={tx,ty},radius=0.4}; local blocked=false;"
        "        for _,e in pairs(hard) do"
        "          if e.type=='item-entity' then local st=e.stack; if st and st.valid_for_read then inv.insert{name=st.name,count=st.count} end; e.destroy()"
        "          elseif e.type=='tree' then inv.insert{name='wood',count=4}; e.destroy()"
        "          elseif e.type=='simple-entity' then e.destroy()"
        "          elseif e.name=='entity-ghost' then e.destroy()"
        "          elseif e.type~='resource' and e.name~='character' and e.type~='transport-belt' then blocked=true end end;"
        "        if blocked then break end;"
        "        if string.find(s.get_tile(math.floor(tx),math.floor(ty)).name,'water') then break end end;"
        "      if resume then"
        "        for k=1,resume-1 do"
        "          if inv.get_item_count('transport-belt')<1 then break end;"
        "          local tx,ty=bx+d[1]*k, by+d[2]*k;"
        "          if PROT[math.floor(tx)..':'..math.floor(ty)] then break end;"
        "          local e=s.create_entity{name='transport-belt',position={tx,ty},direction=b.direction,force=f};"
        "          if e then inv.remove{name='transport-belt',count=1}; fixed=fixed+1 end end end end end end;"
        "if fixed>0 then game.print('repair_belt_gaps: bridged '..fixed..' tiles') end")


def ensure_grid_connected():
    """SELF-HEAL the electric grid: if a steam engine ends up on a DIFFERENT network than the main
    grid (the pole network with the most poles), bridge it back with a pole line. The recurring
    fragmented-generator failure - engine islanded from the base, so the whole base browns out and
    the smelter arrays lose power - now repairs ITSELF each power cycle instead of needing a human
    to re-bridge. Pairs with dedupe_poles no longer deleting connector poles. Server-side, no walk."""
    if operator_present():
        return
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


def reap_dead_drills():
    """Surgically remove burner mining drills that have EXHAUSTED their patch (status
    no_minable_resources - mining nothing), refunding the drill + its leftover coal to derpface's
    inventory. Server-side, no walk; a no-op when nothing is exhausted.

    WHY (architect, 2026-06-29): a depleted patch leaves its drills standing producing zero, so
    the outpost silently starves the base (the iron drought: iron furnaces went no_ingredients,
    19 dead drills sat at one old patch) AND it litters the map. This is the safe half of
    Seth's 'patrol removes unneeded infrastructure' rule + the architect's 'mine ONLY the
    depleted drills, never area-delete' cleanup: it touches ONLY drills the engine itself reports
    as out of ore, so it can never hit a working drill or any operator-built base/power/pole.
    Refunded drills + coal are reused by the relocation pass (ensure_ore_supply) on a fresh patch.
    Returns the number reaped."""
    if _REAP_PAUSE:
        return 0                   # a relocation build is placing drills; don't reap them mid-build
    out = A._print(
        "/sc local p=storage.derpface; if not (p and p.valid) then rcon.print('0'); return end;"
        "local s=p.surface; local inv=p.get_main_inventory(); local n=0;"
        "for _,d in pairs(s.find_entities_filtered{type='mining-drill'}) do"
        "  if d.status==defines.entity_status.no_minable_resources then"
        "    local fb=d.get_fuel_inventory(); if fb then for _,c in pairs(fb.get_contents()) do inv.insert{name=c.name,count=c.count} end end;"
        "    inv.insert{name=d.name,count=1}; d.destroy(); n=n+1 end end;"
        "rcon.print(tostring(n))").strip()
    try:
        n = int(out)
    except ValueError:
        n = 0
    if n:
        status.log(f"reaped {n} exhausted mining drill(s) (no_minable_resources)")
    return n


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
        "    local n=math.min(si.get_item_count(item), cap-have); if n>0 then local ins=inv.insert{name=item,count=n}; if ins>0 then si.remove{name=item,count=ins} end; have=have+ins end end end;"
        # CONTENT-BASED: any chest holding plates near the arrays is a drain (the old
        # hardcoded areas were the RETIRED map's - copper harvest silently collected 0)
        "local function sweep(item, cap) local have=inv.get_item_count(item); if have>=cap then return end;"
        "  for _,src in pairs(s.find_entities_filtered{area={{-14,-2},{34,22}},name={'iron-chest','wooden-chest'}}) do"
        "    local si=src.get_inventory(defines.inventory.chest);"
        "    local n=math.min(si.get_item_count(item), cap-have); if n>0 then local ins=inv.insert{name=item,count=n};"
        "      if ins>0 then si.remove{name=item,count=ins} end; have=have+ins end end end;"
        "sweep('iron-plate',300); sweep('copper-plate',300)")


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
    out = A._print(
        "/sc if not (storage.derpface and storage.derpface.valid) then local s=game.surfaces[1];"
        # fixed {6,-10} failed on the fresh v2 map (tile blocked -> nil character -> every
        # inventory read crashed the phase pass); find a non-colliding tile near spawn instead
        "  local pos=s.find_non_colliding_position('character', {6,-10}, 40, 1) or {x=0.5,y=0.5};"
        "  local c=s.create_entity{name='character', position=pos, force='player'};"
        "  if c then storage.derpface=c; c.character_running_speed_modifier=0;"
        "  end end;"   # no nameplate render: the map carries NO autopilot text (dashboard only)
        "rcon.print('derpface valid='..tostring(storage.derpface and storage.derpface.valid))").strip()
    if "valid=true" not in out:
        raise RuntimeError(f"ensure_derpface failed: {out[:200]}")
    return out


def maintain(laps=0, lap_hook=None):
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
                trim_inventory()              # keep derpface lean so coal always fits (over-hoarded cable blocked coal)
                restock_coal()                # keep derpface stocked with coal (server-side, from the stock chest/belts)
                fuel_arrays()                 # keep the belt-fed smelter array furnaces fueled (server-side)
                # ensure_coal_restock()       # DISABLED: the coal mine is human-built (self-feeding). The autopilot must NOT
                #                               rebuild base layout the operator manages - it kept rebuilding Seth's coal
                #                               buffer/inserter. Autopilot = fuel/harvest/research ONLY, never auto-build layout.
                fuel_drills()                 # keep all burner mining drills fueled (server-side) so mines never stall
                repair_belt_gaps()            # bridge dead-end lanes (belt continuity law)
                reap_dead_drills()            # remove EXHAUSTED drills (no_minable_resources) - they produce nothing + litter
                harvest_array_plates()        # array drain chests -> science buffer chests
                _collect_plates_all()         # furnace plates -> inventory (pre-belt-feed path)
                harvest_plate_belts()         # belt-fed plates -> inventory (post-belt-feed: plates ride belts now)
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
                A.purpose("maintenance: a fuel/supply gate is blocking - clearing it")
                refill_buffers()
                haul_ore()
            elif i % 10 == 5 and ensure_lanes(i):
                pass               # a lane re-lay is this lap's work (source->destination law)
            elif i % 12 == 0 and relocate_exhausted_outposts(i):
                # periodic supply self-heal (not gated): if an outpost is on a thinning patch and a
                # richer one exists, it relocated this lap (a long character build) - that's the work
                pass
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
            if i % 15 == 0:
                # Pole cleanup is the BOT's job. This was disabled because the old
                # dedupe_poles fought the operator's layout: it guessed redundancy by
                # proximity (<2.0 tiles), which both missed the real duplicates (a small pole
                # covers 5x5 and wires 7.5, so poles FOUR apart can be redundant) and deleted
                # connectors, islanding the steam engine. Calling it "a human decision" then
                # let the count drift to 165 poles on a base that needs ~100.
                # pole_cull decides by proof: a pole goes only when everything it supplies is
                # still supplied and the grid can still be made whole. It re-wires the chains
                # the removal breaks, stands down while the operator is logged in, and puts
                # the entire batch back if the game disagrees with the plan.
                try:
                    pole_cull.apply(A, log=status.log)
                except Exception as e:
                    status.log(f"pole cull error: {e}")
            if i % 20 == 7:
                # A product piling up at a dead end while something that eats it goes hungry
                # is a connection the bot can work out and build. It was doing the analysis
                # and then writing it into a document for a person to action, which is how
                # red science came to sit on a belt while every lab read missing_science_packs.
                try:
                    feed_planner.feed_stalled(A, log=status.log)
                except Exception as e:
                    status.log(f"feed error: {e}")
            if i % 10 == 0:
                gamedb.dump_excess()   # overflow inventory -> buffer chests (server-side)
                gamedb.snapshot()      # refresh the structures + chest-inventory DB
            status.write_status(BUILD_QUEUE)   # heartbeat for a Claude session to read
            _note()
            if lap_hook:
                try:
                    lap_hook(i)        # learning loop: triage / architect (planner.lap_hook)
                except Exception as e:
                    status.log(f"lap_hook error: {e}")
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


# --------------------------------------------------------------- THE DEPOT (Seth, 2026-08-30)
# "anytime derpface inventory is full just build a chest and dump stuff in there, but make sure
# to keep track of whats in the chest so he can use it later if he needs to. keep these chests
# in a central location."
#
# A FULL INVENTORY IS NOT UNTIDINESS, IT IS A HARD STOP. At 80/80 stacks `can_insert` returns
# false for every item, so the script-crafter cannot produce anything and `A.place` refuses with
# NO_ITEM before it touches the world. Nothing above the placement layer can tell that apart from
# a gating problem: on 2026-08-30 the base sat idle behind "power headroom 1.014 < 1.50" while the
# real cause was that derpface was carrying 1374 belts, 833 gears and 82 wooden chests and had
# nowhere to put a boiler. Three rounds of planner fixes could not have produced a build.
#
# `autopilot.manage_inventory` was supposed to prevent exactly this and never ran: it lives in
# `maintain()`, which only `patrol.py` calls, and nothing imports patrol - it died with the
# maintain loop (GOTCHAS "THE SWEEP"). It also only ever offloaded five hardcoded item names,
# none of which were the belts and gears that actually filled the bag.
# A DEPOT THAT CANNOT GROW IS A DEPOT THAT FILLS. The first six chests filled within hours, and
# the offload then reported "6145 items did NOT fit - the depot needs another chest" on every
# pass while free_slots sat at 0 - and at 0 free slots nothing can be crafted or placed at all,
# which is the hard stop this whole mechanism exists to prevent. Detecting a condition and not
# acting on it is not a guard, it is a log line.
#
# The block grows ROW BY ROW southward from the same corner, so it stays the one findable place
# the operator asked for. Chests are only ever PLACED as needed - ensure_inventory_room walks
# these tiles in order and stops at the first one that can take the overflow.
DEPOT_TILES = [(x, y) for y in range(20, 32) for x in (2, 3, 4)]

# The working set: what a build actually needs in hand. Everything above this goes to the depot.
# Generous on purpose - the point is free SLOTS, not a minimal loadout, and a build that has to
# re-craft its own belts has traded one stall for another.
DEPOT_KEEP = {
    "assembling-machine-1": 4, "inserter": 40, "fast-inserter": 20, "transport-belt": 200,
    "underground-belt": 8, "splitter": 4, "small-electric-pole": 40, "medium-electric-pole": 10,
    "iron-plate": 300, "copper-plate": 150, "steel-plate": 50, "iron-gear-wheel": 100,
    "copper-cable": 100, "electronic-circuit": 100, "coal": 100, "stone": 50,
    "stone-furnace": 4, "iron-chest": 8, "boiler": 2, "steam-engine": 4, "pipe": 20,
    "offshore-pump": 1, "lab": 2, "electric-mining-drill": 6, "radar": 1,
}

DEPOT_MIN_FREE = 8        # below this many free stacks, offload; a build needs room to craft into


def _depot_manifest_path():
    import pathlib as _pl
    return _pl.Path(__file__).resolve().parent / "depot-manifest.json"


def depot_manifest(write=True):
    """Read what the depot actually holds and (by default) persist it.

    The manifest is the "so he can use it later" half of the rule: a blind dump loses track of
    materials the bot then re-crafts from raws. It is written from the WORLD, never from what we
    think we put there, so an operator taking something out is reflected on the next pass."""
    spec = ";".join("%d,%d" % (x, y) for x, y in DEPOT_TILES)
    rows = A._print(
        "/sc local s=game.surfaces[1] local o={} "
        "for a,b in ([==[" + spec + "]==]):gmatch('(-?%d+),(-?%d+)') do "
        "  local e=s.find_entities_filtered{position={tonumber(a)+0.5,tonumber(b)+0.5},"
        "radius=0.4,type='container'}[1] "
        "  if e then for _,it in pairs(e.get_inventory(defines.inventory.chest).get_contents()) "
        "    do o[#o+1]=a..','..b..'|'..it.name..'|'..it.count end end end "
        "rcon.print(table.concat(o,';'))").strip()
    by_chest, totals = {}, {}
    for rec in [r for r in rows.split(";") if r and "|" in r]:
        pos, name, cnt = rec.split("|")
        by_chest.setdefault(pos, {})[name] = int(cnt)
        totals[name] = totals.get(name, 0) + int(cnt)
    out = {"depot": "central surplus depot for derpface",
           "tiles": [list(t) for t in DEPOT_TILES],
           "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "by_chest": by_chest, "totals": totals}
    if write:
        import json as _json
        _depot_manifest_path().write_text(_json.dumps(out, indent=1))
    return out


def ensure_inventory_room(min_free=DEPOT_MIN_FREE, keep=None, force=False):
    """Offload surplus to the central depot so there is always room to craft and place.

    Idempotent and cheap: returns immediately when there are already `min_free` free stacks, so
    it is safe to call at the top of every planner pass. Chests are placed only as needed, from
    derpface's own stock, on the fixed central tiles - a scattered chest is one nobody finds
    again, which is why the tiles are constant rather than "wherever he happens to stand".

    NO `--` COMMENTS IN THE LUA BELOW: `/sc` is sent as one line, so a Lua comment swallows the
    rest of the command and the whole thing silently does nothing. That cost a debugging cycle.
    """
    keep = dict(DEPOT_KEEP if keep is None else keep)
    free = A._print("/sc rcon.print(storage.derpface.get_main_inventory().count_empty_stacks())").strip()
    try:
        free_n = int(free)
    except ValueError:
        status.log("depot: could not read inventory free space (%r) - not offloading blind" % free[:80])
        return None
    if free_n >= int(min_free) and not force:
        return None

    keepspec = ";".join("%s=%d" % (k, v) for k, v in keep.items())
    place = ";".join("%d,%d" % (x, y) for x, y in DEPOT_TILES)
    out = A._print(
        "/sc local s=game.surfaces[1] local f=game.forces.player "
        "local d=storage.derpface local inv=d.get_main_inventory() "
        "local keep={} "
        "for pair in ([==[" + keepspec + "]==]):gmatch('([^;]+)') do "
        "  local n,c=pair:match('([^=]+)=(%d+)') if n then keep[n]=tonumber(c) end end "
        "local chests={} "
        "for a,b in ([==[" + place + "]==]):gmatch('(-?%d+),(-?%d+)') do "
        "  local x,y=tonumber(a),tonumber(b) "
        "  local e=s.find_entities_filtered{position={x+0.5,y+0.5},radius=0.4,type='container'}[1] "
        # GROW WITH WHATEVER CONTAINER HE ACTUALLY HAS. This used to place iron-chest only, so
        # a character carrying 38 WOODEN chests and no iron ones could not extend a full depot:
        # every pass logged "items did NOT fit - the depot needs another chest" while holding
        # the chests to build it. Inventory then stayed at 0 free stacks, and a full inventory
        # makes can_insert false for every item, which silently blocks EVERY build in the base.
        "  if not e then "
        "    for _,cn in pairs({'steel-chest','iron-chest','wooden-chest'}) do "
        "      if inv.get_item_count(cn)>0 "
        "         and s.can_place_entity{name=cn,position={x+0.5,y+0.5},force=f} then "
        "        e=s.create_entity{name=cn,position={x+0.5,y+0.5},force=f} "
        "        if e then inv.remove{name=cn,count=1} break end end end end "
        "  if e then chests[#chests+1]=e end end "
        "if #chests==0 then rcon.print('ERR no depot chest') return end "
        "local moved={} local left=0 "
        "for _,it in pairs(inv.get_contents()) do "
        "  local surplus=it.count-(keep[it.name] or 0) "
        "  if surplus>0 then local put=0 "
        "    for _,c in pairs(chests) do if put<surplus then "
        "      put=put+c.get_inventory(defines.inventory.chest).insert{name=it.name,count=surplus-put} "
        "    end end "
        "    if put>0 then inv.remove{name=it.name,count=put} moved[#moved+1]=it.name..'='..put end "
        "    left=left+(surplus-put) end end "
        "rcon.print(string.format('%d %d %d %s', #chests, inv.count_empty_stacks(), left, "
        "  table.concat(moved,' ')))").strip()
    if out.startswith("ERR"):
        status.log("depot: %s - inventory stays full, builds will fail with NO_ITEM" % out)
        return out
    parts = out.split(" ", 3)
    now_free = parts[1] if len(parts) > 1 else "?"
    overflow = parts[2] if len(parts) > 2 else "0"
    status.log("depot: %s free stacks (was %d)%s -> %s"
               % (now_free, free_n,
                  ("; %s items did NOT fit - the depot needs another chest" % overflow)
                  if overflow not in ("0", "?") else "",
                  parts[3] if len(parts) > 3 else "nothing to offload"))
    depot_manifest()
    return out


def depot_take(item, count):
    """Pull `count` of `item` back out of the depot into derpface's hands. The other half of
    "so he can use it later": material that went to the depot must be retrievable, or the dump
    is just a slower way of throwing it away. Returns how many actually moved."""
    place = ";".join("%d,%d" % (x, y) for x, y in DEPOT_TILES)
    got = A._print(
        "/sc local s=game.surfaces[1] local inv=storage.derpface.get_main_inventory() "
        "local want=" + str(int(count)) + " local got=0 "
        "for a,b in ([==[" + place + "]==]):gmatch('(-?%d+),(-?%d+)') do "
        "  local e=s.find_entities_filtered{position={tonumber(a)+0.5,tonumber(b)+0.5},"
        "radius=0.4,type='container'}[1] "
        "  if e and got<want then local ci=e.get_inventory(defines.inventory.chest) "
        "    local have=ci.get_item_count('" + item + "') "
        "    if have>0 then local n=inv.insert{name='" + item + "',count=math.min(have,want-got)} "
        "      if n>0 then ci.remove{name='" + item + "',count=n} got=got+n end end end end "
        "rcon.print(got)").strip()
    try:
        n = int(got)
    except ValueError:
        n = 0
    if n:
        status.log("depot: took %d %s back out" % (n, item))
        depot_manifest()
    return n


if __name__ == "__main__":
    print(bootstrap())


# ------------------------------------------------- THE OPERATOR BASELINE (durable, on disk)
# The login/logoff hook in controller.py can only see a transition it is RUNNING to observe.
# Every time the bot is stopped - which is exactly when the operator logs in to repair
# something - no snapshot is taken, no diff is computed, and his changes are invisible.
# 2026-08-30: he rebuilt both smelter-array output belts while the container was down and the
# bot never noticed; the next session then "discovered" the same facts from scratch and
# reported them back to him as news.
#
# So the baseline lives ON DISK and is refreshed continuously. A diff is then available at any
# time, including across a restart, and anything that changed while we were down is by
# definition not ours.
def _baseline_path():
    import pathlib as _pl
    return _pl.Path(__file__).resolve().parent / "operator-baseline.json"


def save_baseline(snap=None):
    """Persist the world snapshot so a later diff survives a restart. Cheap; call on a slow
    clock. Returns the number of entities recorded."""
    import json as _json
    snap = world_snapshot() if snap is None else snap
    if not snap:
        return 0
    _baseline_path().write_text(_json.dumps(
        {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "ents": sorted(snap)}))
    return len(snap)


def load_baseline():
    import json as _json
    p = _baseline_path()
    if not p.exists():
        return None, None
    try:
        d = _json.loads(p.read_text())
        return set(tuple(e) if isinstance(e, list) else e for e in d.get("ents", [])), d.get("at")
    except Exception:
        return None, None


def diff_since_baseline(protect=True):
    """What changed since the stored baseline. Anything here happened while WE were not
    building, so it is the operator's - his removals are INTENT and get protected forever.

    Returns a dict, and logs a readable summary. Safe to call at any time; on the first run
    (no baseline yet) it just records one."""
    before, at = load_baseline()
    now = world_snapshot()
    if not now:
        return {"error": "could not read the world"}
    if before is None:
        n = save_baseline(now)
        status.log("operator baseline: first run, recorded %d entities" % n)
        return {"first_run": True, "recorded": n}
    removed, added = before - now, now - before
    if not removed and not added:
        save_baseline(now)
        return {"removed": 0, "added": 0}

    def summarise(s):
        k = {}
        for e in s:
            nm = e.split("|")[0] if isinstance(e, str) else str(e[0])
            k[nm] = k.get(nm, 0) + 1
        return ", ".join("%s x%d" % kv for kv in sorted(k.items(), key=lambda kv: -kv[1])[:6])

    status.log("OPERATOR EDITS since %s: %d removed (%s) | %d added (%s)"
               % (at or "?", len(removed), summarise(removed) or "-",
                  len(added), summarise(added) or "-"))
    if protect and removed:
        try:
            gone = {(int(p[1]), int(p[2])) for p in
                    (e.split("|") for e in removed if isinstance(e, str)) if len(p) >= 3}
            prot = _protected_load() | gone
            _protected_save(prot)
            status.log("protected %d operator-removed tiles (never rebuild); total %d"
                       % (len(gone), len(prot)))
        except Exception as e:
            status.log("baseline: could not protect removals (%s)" % e)
    save_baseline(now)
    return {"removed": len(removed), "added": len(added),
            "removed_summary": summarise(removed), "added_summary": summarise(added)}
