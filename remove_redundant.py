"""Remove redundant power poles: a pole is removable if every electric consumer it
powers is ALSO powered by another kept pole, AND removing it doesn't split the network
(and a generator stays covered). Greedy removal, then atomic replace + verify."""
import autopilot as a
import math

WIRE = 7.0  # small pole reach 7.5; margin

raw = a._print(
    "/sc local s=game.surfaces['nauvis']; local o={};"
    "for _,pl in pairs(s.find_entities_filtered{type='electric-pole'}) do o[#o+1]='P:'..pl.position.x..','..pl.position.y end;"
    "for _,e in pairs(s.find_entities_filtered{type={'assembling-machine','lab','inserter','mining-drill','pumpjack','beacon'}}) do"
    "  if e.prototype.electric_energy_source_prototype then o[#o+1]=((e.tile_width>=3) and 'B:' or 'S:')..e.position.x..','..e.position.y end end;"
    "for _,e in pairs(s.find_entities_filtered{name='steam-engine'}) do o[#o+1]='G:'..e.position.x..','..e.position.y end;"
    "rcon.print(table.concat(o,';'))"
).strip()

poles, cons, gens = [], [], []
for tok in raw.split(';'):
    if ':' not in tok: continue
    t, xy = tok.split(':'); x, y = map(float, xy.split(','))
    if t == 'P': poles.append([x, y])
    elif t == 'B': cons.append((x, y, 4.0))   # 3x3 consumer: supply 2.5 + half-size 1.5
    elif t == 'S': cons.append((x, y, 3.0))   # 1x1 consumer: supply 2.5 + 0.5
    elif t == 'G': gens.append((x, y, 4.0))
allcons = cons + gens
print(f"start: {len(poles)} poles, {len(cons)} consumers, {len(gens)} generators")

def covers(p, c):
    return abs(p[0]-c[0]) <= c[2] and abs(p[1]-c[1]) <= c[2]

kept = [True]*len(poles)

def covered_elsewhere(c, exclude):
    return any(kept[i] and i != exclude and covers(poles[i], c) for i in range(len(poles)))

def connected_without(exclude):
    idx = [i for i in range(len(poles)) if kept[i] and i != exclude]
    if not idx: return False
    parent = {i: i for i in idx}
    def find(i):
        while parent[i] != i: parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for a_ in range(len(idx)):
        for b_ in range(a_+1, len(idx)):
            i, j = idx[a_], idx[b_]
            if math.hypot(poles[i][0]-poles[j][0], poles[i][1]-poles[j][1]) <= WIRE:
                parent[find(i)] = find(j)
    if len({find(i) for i in idx}) != 1: return False
    return any(any(covers(poles[i], g) for i in idx) for g in gens)  # a generator stays covered

# try removing the poles that cover the fewest consumers first
order = sorted(range(len(poles)), key=lambda i: sum(1 for c in allcons if covers(poles[i], c)))
removed = 0
for i in order:
    myc = [c for c in allcons if covers(poles[i], c)]
    if all(covered_elsewhere(c, i) for c in myc) and connected_without(i):
        kept[i] = False; removed += 1
keptpoles = [poles[i] for i in range(len(poles)) if kept[i]]
print(f"removable redundant poles: {removed} -> keeping {len(keptpoles)}")

# atomic replace
targ = ",".join(f"{{{x},{y}}}" for (x, y) in keptpoles)
print(a._print(
    "/sc local s=game.surfaces['nauvis']; local p=game.players[1];"
    "for _,pl in pairs(s.find_entities_filtered{type='electric-pole'}) do pl.destroy() end;"
    "local t={" + targ + "}; local n=0;"
    "for _,xy in ipairs(t) do if s.can_place_entity{name='small-electric-pole',position={xy[1],xy[2]},force=p.force} then s.create_entity{name='small-electric-pole',position={xy[1],xy[2]},force=p.force}; n=n+1 end end;"
    "rcon.print('placed '..n..' poles')"
).strip())

# verify + re-add stragglers
import time; time.sleep(3)
print(a._print(
    "/sc local s=game.surfaces['nauvis']; local p=game.players[1]; local fixed=0; local still=0;"
    "for _,e in pairs(s.find_entities_filtered{type={'assembling-machine','lab','inserter','mining-drill','pumpjack','beacon'}}) do"
    "  if e.prototype.electric_energy_source_prototype and e.status==58 then local done=false;"
    "    for _,off in ipairs({{2,0},{-2,0},{0,2},{0,-2}}) do if not done then local x,y=e.position.x+off[1],e.position.y+off[2];"
    "      if s.can_place_entity{name='small-electric-pole',position={x,y},force=p.force} then s.create_entity{name='small-electric-pole',position={x,y},force=p.force}; done=true; fixed=fixed+1 end end end;"
    "    if not done then still=still+1 end end end;"
    "local tot=#s.find_entities_filtered{type='electric-pole'};"
    "rcon.print('stragglers re-added='..fixed..' still_unpowered='..still..' | FINAL pole count='..tot)"
).strip())
