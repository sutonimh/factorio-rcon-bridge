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
import sys, time, math, json
import rcon  # reuse the client in this dir

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
