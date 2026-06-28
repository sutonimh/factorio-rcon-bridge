"""Optimize the power-pole layout: place the MINIMUM small poles that (a) supply every
electric consumer and (b) keep them all connected to the generators. Greedy set-cover
for coverage + connector poles for connectivity, then atomically replace all poles."""
import autopilot as a
import math
import sys

# Usage: python3 optimize_poles.py [pole-name] [cover] [wire]
#   small (default):  small-electric-pole  3.0  6.5   (supply 2.5, reach 7.5)
#   medium:           medium-electric-pole 4.0  8.0   (supply 3.5, reach 9.0)  <- after electric-energy-distribution-1
POLE = sys.argv[1] if len(sys.argv) > 1 else 'small-electric-pole'
COVER = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
WIRE = float(sys.argv[3]) if len(sys.argv) > 3 else 6.5
print(f"optimizing with {POLE} (cover={COVER}, wire={WIRE})")

# 1. PULL electric consumers + generators
raw = a._print(
    "/sc local s=game.surfaces['nauvis']; local o={};"
    "for _,e in pairs(s.find_entities_filtered{type={'assembling-machine','lab','inserter','mining-drill','pumpjack','beacon','radar','furnace','lamp','electric-turret'}}) do"
    "  if e.prototype.electric_energy_source_prototype then o[#o+1]='C:'..e.position.x..','..e.position.y end end;"
    "for _,e in pairs(s.find_entities_filtered{name='steam-engine'}) do o[#o+1]='G:'..e.position.x..','..e.position.y end;"
    "rcon.print(table.concat(o,';'))"
).strip()
consumers, generators = [], []
for tok in raw.split(';'):
    if ':' not in tok: continue
    tag, xy = tok.split(':'); x, y = map(float, xy.split(','))
    (consumers if tag == 'C' else generators).append((x, y))
targets = consumers + generators   # generators must be covered too, so engines feed the net
print(f"consumers={len(consumers)} generators={len(generators)}")

# 2. GREEDY SET COVER
cand = set()
for (cx, cy) in targets:
    bx, by = round(cx), round(cy)
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            cand.add((bx + dx + 0.5, by + dy + 0.5))
cand = list(cand)
uncovered = set(range(len(targets)))
chosen = []
while uncovered:
    best, bestcov = None, set()
    for p in cand:
        cov = {i for i in uncovered if abs(targets[i][0]-p[0]) <= COVER and abs(targets[i][1]-p[1]) <= COVER}
        if len(cov) > len(bestcov):
            best, bestcov = p, cov
    if not best:
        break
    chosen.append(best); uncovered -= bestcov
print(f"set-cover poles={len(chosen)} (uncovered left={len(uncovered)})")

# 3. CONNECTIVITY: link all chosen poles into one network with connector poles
def components(poles):
    n = len(poles); parent = list(range(n))
    def find(i):
        while parent[i] != i: parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for i in range(n):
        for j in range(i + 1, n):
            if math.hypot(poles[i][0]-poles[j][0], poles[i][1]-poles[j][1]) <= WIRE:
                parent[find(i)] = find(j)
    comps = {}
    for i in range(n): comps.setdefault(find(i), []).append(i)
    return list(comps.values())

allpoles = list(chosen); connectors = 0
while True:
    comps = components(allpoles)
    if len(comps) <= 1: break
    c0 = comps[0]; best = None
    for ci in range(1, len(comps)):
        for i in c0:
            for j in comps[ci]:
                d = math.hypot(allpoles[i][0]-allpoles[j][0], allpoles[i][1]-allpoles[j][1])
                if best is None or d < best[0]: best = (d, i, j)
    d, i, j = best
    x0, y0 = allpoles[i]; x1, y1 = allpoles[j]
    steps = int(math.ceil(d / WIRE))
    for s in range(1, steps):
        cx = round(x0 + (x1-x0)*s/steps - 0.5) + 0.5
        cy = round(y0 + (y1-y0)*s/steps - 0.5) + 0.5
        allpoles.append((cx, cy)); connectors += 1
print(f"connectors={connectors} total_poles={len(allpoles)}")

# 4. PLACE atomically: remove all old poles, place the optimized set (with small offset fallback)
targ_lua = ",".join(f"{{{x},{y}}}" for (x, y) in allpoles)
res = a._print(
    "/sc local s=game.surfaces['nauvis']; local p=game.players[1];"
    "local old=0; for _,pl in pairs(s.find_entities_filtered{type='electric-pole'}) do pl.destroy(); old=old+1 end;"
    "local targets={" + targ_lua + "}; local placed=0;"
    "for _,t in ipairs(targets) do local done=false;"
    "  for _,off in ipairs({{0,0},{0.5,0},{-0.5,0},{0,0.5},{0,-0.5},{1,0},{-1,0},{0,1},{0,-1}}) do"
    "    if not done then local x,y=t[1]+off[1],t[2]+off[2];"
    "      if s.can_place_entity{name='"+POLE+"',position={x,y},force=p.force} then s.create_entity{name='"+POLE+"',position={x,y},force=p.force}; placed=placed+1; done=true end end end end;"
    "rcon.print('removed '..old..' old poles, placed '..placed..' optimized poles')"
).strip()
print(res)

# 5. VERIFY: all consumers powered? add a pole for any straggler
import time; time.sleep(3)
chk = a._print(
    "/sc local s=game.surfaces['nauvis']; local p=game.players[1]; local fixed=0; local still=0;"
    "for _,e in pairs(s.find_entities_filtered{type={'assembling-machine','lab','inserter','mining-drill','pumpjack','beacon'}}) do"
    "  if e.prototype.electric_energy_source_prototype and e.status==58 then"
    "    local placed=false; for _,off in ipairs({{2,0},{-2,0},{0,2},{0,-2},{2.5,0},{-2.5,0}}) do"
    "      if not placed then local x,y=e.position.x+off[1],e.position.y+off[2];"
    "        if s.can_place_entity{name='"+POLE+"',position={x,y},force=p.force} then s.create_entity{name='"+POLE+"',position={x,y},force=p.force}; placed=true; fixed=fixed+1 end end end;"
    "    if not placed then still=still+1 end end end;"
    "rcon.print('straggler poles added='..fixed..' still_unpowered='..still)"
).strip()
print(chk)
