#!/usr/bin/env python3
"""Game state database (Seth's rule: track structures + chest inventories in code for fast
reference, so builds/refills can be planned without re-querying the world each time).

state-db.json holds:
  structures: [{name, type, x, y, w, h}]   - every player entity incl. inserters/poles/chests
  chests:     [{x, y, contents:{item:count}}]
  ts:         game tick the snapshot was taken

Buffer chests: a small adjacent array in the base used as the character's inventory overflow -
dump excess there, pull from there for builds. `buffer_totals()` sums what's available.
"""
import json
import pathlib
import autopilot as A

HERE = pathlib.Path(__file__).resolve().parent
DB_PATH = HERE / "state-db.json"
BUFFER_ROW = [(16, -7), (17, -7), (18, -7), (19, -7)]   # 4 adjacent buffer chests in the base


def snapshot():
    """Dump every player structure (name/type/tile-size/location) + all chest inventories to
    state-db.json. Returns (n_structures, n_chests)."""
    out = A._print(
        "/sc local s=game.surfaces[1]; local structs={}; local chests={};"
        "for _,e in pairs(s.find_entities_filtered{force='player'}) do if e.name~='character' and e.name~='character-corpse' then"
        "  local ep=e.prototype; structs[#structs+1]={name=e.name,type=e.type,x=e.position.x,y=e.position.y,w=ep.tile_width,h=ep.tile_height};"
        "  if e.type=='container' or e.type=='logistic-container' then local inv=e.get_inventory(defines.inventory.chest); local c={};"
        "    if inv then for _,it in pairs(inv.get_contents()) do c[it.name]=it.count end end;"
        "    chests[#chests+1]={x=e.position.x,y=e.position.y,contents=c} end end end;"
        "rcon.print(helpers.table_to_json({structures=structs, chests=chests, ts=game.tick}))")
    try:
        data = json.loads(out)
    except Exception:
        return (0, 0)
    DB_PATH.write_text(json.dumps(data))
    return (len(data.get("structures", [])), len(data.get("chests", [])))


def load():
    try:
        return json.loads(DB_PATH.read_text())
    except Exception:
        return {"structures": [], "chests": [], "ts": 0}


def find(name=None, etype=None):
    """Query the cached DB for structures by name and/or type -> list of {name,type,x,y,w,h}."""
    return [st for st in load().get("structures", [])
            if (name is None or st["name"] == name) and (etype is None or st["type"] == etype)]


# ----------------------------------------------------------------- buffer chests
def build_buffer_chests(n=4):
    """Place `n` adjacent buffer chests in the base (idempotent: place() no-ops on an occupied
    tile). Assumes wooden-chest is in inventory (provision before calling)."""
    A.now("Building base buffer chest array")
    for (x, y) in BUFFER_ROW[:n]:
        A.place("wooden-chest", x, y, clear=1)


def buffer_totals():
    """Sum item counts across the buffer chests (live) -> {item:count}. The 'balance' available
    for builds/refills."""
    out = A._print(
        "/sc local s=game.surfaces[1]; local t={};"
        "for _,c in pairs(s.find_entities_filtered{name='wooden-chest',area={{14,-9},{22,-5}}}) do local inv=c.get_inventory(defines.inventory.chest); if inv then for _,it in pairs(inv.get_contents()) do t[it.name]=(t[it.name] or 0)+it.count end end end;"
        "local o={}; for k,v in pairs(t) do o[#o+1]=k..'='..v end; rcon.print(table.concat(o,';'))").strip()
    d = {}
    for tok in out.split(";"):
        if "=" in tok:
            k, v = tok.split("="); d[k] = int(v)
    return d


def dump_excess(keep=None, keep_count=100):
    """Dump inventory items into the buffer chests, keeping up to `keep_count` of each KEEP item
    in hand (build essentials) and dumping everything else (overflow) so the inventory never
    clogs (Seth's rule). Returns items dumped."""
    keep = keep or ["coal", "iron-plate", "copper-plate", "iron-ore", "copper-ore", "stone"]
    keeplua = "{" + ",".join("['%s']=true" % k for k in keep) + "}"
    return A._print(
        "/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); local KEEP=" + keeplua + ";"
        "local chests=s.find_entities_filtered{name='wooden-chest',area={{14,-9},{22,-5}}}; if #chests==0 then return end;"
        "for _,it in pairs(inv.get_contents()) do local keepn = KEEP[it.name] and " + str(keep_count) + " or 0;"
        "  local excess=it.count-keepn; if excess>0 then for _,c in pairs(chests) do if excess<=0 then break end; local put=c.insert{name=it.name,count=excess}; if put>0 then inv.remove{name=it.name,count=put}; excess=excess-put end end end end;"
        "rcon.print('dumped excess')").strip()


def pull_from_buffer(item, count):
    """Take up to `count` of `item` from the buffer chests into inventory. Returns amount taken."""
    out = A._print(
        f"/sc local p=storage.derpface; local s=p.surface; local inv=p.get_main_inventory(); local need={int(count)}; local got=0;"
        "for _,c in pairs(s.find_entities_filtered{name='wooden-chest',area={{14,-9},{22,-5}}}) do if got>=need then break end; local ci=c.get_inventory(defines.inventory.chest);"
        f"  local n=math.min(need-got, ci.get_item_count('{item}')); if n>0 then local ins=inv.insert{{name='{item}',count=n}}; ci.remove{{name='{item}',count=ins}}; got=got+ins end end;"
        "rcon.print(got)").strip()
    try:
        return int(out)
    except ValueError:
        return 0


if __name__ == "__main__":
    print("snapshot:", snapshot())
    print("buffer:", buffer_totals())
