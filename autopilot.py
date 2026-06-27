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
