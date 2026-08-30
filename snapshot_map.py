#!/usr/bin/env python3
"""Full map snapshot + diff — for learning from what the OPERATOR changes.

Captures every player-force entity with the attributes that make an edit legible (position,
direction, type, recipe, belt lane contents, fuel, network id), so two snapshots can be
diffed into "what he added / removed / rotated / re-recipe'd" and, from that, WHY.

    python3 snapshot_map.py save before          # write snapshots/before.json
    python3 snapshot_map.py save after
    python3 snapshot_map.py diff before after    # human-readable change report

Read-only: it never modifies the world.
"""
import json
import pathlib
import sys
import time

import rcon

HERE = pathlib.Path(__file__).resolve().parent
SNAPDIR = HERE / "snapshots"
CHUNK = 3000

DUMP_LUA = r"""/sc
local s=game.surfaces[1]
local SN={} for k,v in pairs(defines.entity_status) do SN[v]=k end
local o={}
for _,e in pairs(s.find_entities_filtered{force='player'}) do
  if e.name~='character' then
    local d={n=e.name,t=e.type,
             x=math.floor(e.position.x*2)/2, y=math.floor(e.position.y*2)/2}
    local okd,dir=pcall(function() return e.direction end) if okd and dir then d.d=dir end
    local oks,st=pcall(function() return e.status end) if oks and st~=nil then d.s=SN[st] or st end
    if e.type=='assembling-machine' or e.type=='furnace' then
      local okr,r=pcall(function() return e.get_recipe() end) if okr and r then d.r=r.name end
    end
    local oke,eid=pcall(function() return e.electric_network_id end) if oke and eid then d.e=eid end
    if e.type=='underground-belt' then d.bg=e.belt_to_ground_type end
    if e.type=='transport-belt' then
      local n=0
      for li=1,e.get_max_transport_line_index() do n=n+#e.get_transport_line(li) end
      if n>0 then d.items=n end
    end
    local okf,fi=pcall(function() return e.get_fuel_inventory() end)
    if okf and fi then d.fuel=fi.get_item_count('coal') end
    o[#o+1]=d
  end
end
local f=game.forces.player
local g={tick=game.tick,
         research=(f.current_research and f.current_research.name or ''),
         research_pct=(f.current_research and math.floor(f.research_progress*100) or -1)}
local ps=f.get_item_production_statistics(s)
local function pm(n) return math.floor(ps.get_flow_count{name=n,category='input',precision_index=defines.flow_precision_index.one_minute}) end
g.iron_pm=pm('iron-plate') g.copper_pm=pm('copper-plate') g.coal_pm=pm('coal')
g.red_pm=pm('automation-science-pack') g.green_pm=pm('logistic-science-pack')
__STORE__=helpers.table_to_json({ents=o,globals=g})
rcon.print(#__STORE__)
""".replace("\n", " ")


def capture():
    raw = rcon.read_chunked(lambda store: DUMP_LUA.replace("__STORE__", store),
                            chunk=CHUNK, empty="")
    if not raw:
        raise RuntimeError("snapshot returned nothing")
    data = json.loads(raw)
    data["captured_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return data


def save(name):
    SNAPDIR.mkdir(exist_ok=True)
    data = capture()
    p = SNAPDIR / f"{name}.json"
    p.write_text(json.dumps(data, indent=1))
    ents = data["ents"]
    from collections import Counter
    kinds = Counter(e["n"] for e in ents)
    print(f"saved {p} — {len(ents)} entities @ {data['captured_at']}")
    print("  top:", ", ".join(f"{k}×{v}" for k, v in kinds.most_common(8)))
    print("  globals:", data["globals"])
    return p


def _key(e):
    return (e["n"], e["x"], e["y"])


def diff(a_name, b_name):
    a = json.loads((SNAPDIR / f"{a_name}.json").read_text())
    b = json.loads((SNAPDIR / f"{b_name}.json").read_text())
    A = {_key(e): e for e in a["ents"]}
    B = {_key(e): e for e in b["ents"]}
    removed = [A[k] for k in A if k not in B]
    added = [B[k] for k in B if k not in A]
    rotated = [(A[k], B[k]) for k in A if k in B and A[k].get("d") != B[k].get("d")]
    recipe = [(A[k], B[k]) for k in A if k in B and A[k].get("r") != B[k].get("r")]
    from collections import Counter
    print(f"=== {a_name} -> {b_name} ===")
    print(f"entities {len(a['ents'])} -> {len(b['ents'])}")
    print(f"\nREMOVED ({len(removed)}):")
    for k, v in Counter(e["n"] for e in removed).most_common():
        pos = [f"({e['x']:.0f},{e['y']:.0f})" for e in removed if e["n"] == k][:8]
        print(f"  {k} ×{v}   e.g. {' '.join(pos)}")
    print(f"\nADDED ({len(added)}):")
    for k, v in Counter(e["n"] for e in added).most_common():
        pos = [f"({e['x']:.0f},{e['y']:.0f})" for e in added if e["n"] == k][:8]
        print(f"  {k} ×{v}   e.g. {' '.join(pos)}")
    print(f"\nROTATED ({len(rotated)}):")
    for k, v in Counter(x["n"] for x, _ in rotated).most_common():
        print(f"  {k} ×{v}")
    if recipe:
        print(f"\nRECIPE CHANGED ({len(recipe)}):")
        for x, y in recipe[:20]:
            print(f"  {x['n']} @({x['x']:.0f},{x['y']:.0f}): {x.get('r')} -> {y.get('r')}")
    print("\nGLOBALS:", a["globals"], "->", b["globals"])
    return {"removed": removed, "added": added, "rotated": rotated, "recipe": recipe}


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "save":
        save(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "diff":
        diff(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
