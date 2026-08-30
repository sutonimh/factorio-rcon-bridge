#!/usr/bin/env python3
"""Dump the live tech tree -> tech-tree.json (techdb.py's source of truth).

Re-run after every Factorio version bump (GOTCHAS: "Re-dump after big version changes").
Builds the dump server-side into storage._techdump, then reads it back in slices
(Factorio truncates large single RCON responses; same pattern as architect.py).

Usage:  FACTORIO_RCON_HOST=100.100.199.83 python3 dump_techtree.py
"""
import json
import pathlib

import rcon

CHUNK = 3000

BUILD = r"""/sc
local out={techs={},recipe_to_tech={}}
for name,t in pairs(game.forces.player.technologies) do
  local p=t.prototype
  local e={prerequisites={},count=0,packs={},unlocks={}}
  for pn,_ in pairs(t.prerequisites) do table.insert(e.prerequisites,pn) end
  local okc,c=pcall(function() return t.research_unit_count end)
  if okc and c then e.count=c end
  local oki,ings=pcall(function() return t.research_unit_ingredients end)
  if oki and ings then for _,ing in pairs(ings) do e.packs[ing.name]=ing.amount end end
  local okt,trig=pcall(function() return p.research_trigger end)
  if okt and trig then
    local tt={type=trig.type}
    local oke,ev=pcall(function() return trig.entity and (trig.entity.name or trig.entity) end)
    if oke and ev then tt.entity=ev end
    local okv,iv=pcall(function() return trig.item and (trig.item.name or trig.item) end)
    if okv and iv then tt.item=iv end
    if trig.count then tt.count=trig.count end
    e.trigger=tt
  end
  for _,eff in pairs(p.effects or {}) do
    if eff.type=='unlock-recipe' then
      table.insert(e.unlocks,eff.recipe)
      out.recipe_to_tech[eff.recipe]=name
    end
  end
  out.techs[name]=e
end
__STORE__=helpers.table_to_json(out)
rcon.print(#__STORE__)
""".replace("\n", " ")


def dump():
    data = json.loads(rcon.read_chunked(lambda store: BUILD.replace("__STORE__", store),
                                        chunk=CHUNK))
    # table_to_json turns empty lua tables into []; techdb expects {} for packs
    for t in data["techs"].values():
        if t.get("packs") == []:
            t["packs"] = {}
    return data


if __name__ == "__main__":
    data = dump()
    out = pathlib.Path(__file__).resolve().parent / "tech-tree.json"
    out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print("wrote %s: %d techs, %d recipe mappings"
          % (out, len(data["techs"]), len(data["recipe_to_tech"])))
