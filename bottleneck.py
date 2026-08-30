#!/usr/bin/env python3
"""Missing-ingredient attribution: turn "37/39 furnaces starved" into a ROOT CAUSE.

controller.sense() counts starved crafting machines but never says WHICH input is missing, so
triage.classify and architect.analyze_local both guess. This module answers the question the
controller cannot: for every starved assembling-machine/furnace, compare its recipe's
ingredients against what is actually in its input inventory, and report the largest deficit.

Live shape (verified read-only against 2.1.17, 100.100.199.83):
    iron-plate|no_ingredients|iron-ore x12 ; copper-plate|no_ingredients|copper-ore x16

Design constraints (all learned the expensive way — see GOTCHAS.md):
  - RCON READ ONLY. No create/destroy/insert/remove/set_recipe/walking_state, ever.
  - NO event handlers. The upstream prior art (fle's alerts.lua) drives this off on_tick with a
    storage cache; registering runtime handlers locks human players out of the server
    ("mod event handlers are not identical"). We PULL with one /sc instead.
  - 2.1 renamed the machine inventories: `furnace_source` / `assembling_machine_input` are GONE,
    it is `crafter_input` now. Passing the old (nil) define to get_inventory dies.
  - A starved machine has NO recipe: get_recipe() returned nil for 28/28 starved furnaces live.
    `previous_recipe` is the fallback, and on 2.0+ it is a RecipeIDAndQualityIDPair whose `.name`
    is LuaRecipePrototype userdata — the string is `p.name.name`.
  - Fluids: test `ing.type=='fluid'` off the INGREDIENT. prototypes.item[<a fluid>] is nil and
    indexing `.type` on it throws (alerts.lua's bug). get_fluid_count still works on 2.1.
  - e.get_item_count(x) sums EVERY inventory including fuel, so a coal-fuelled furnace would
    read 5 coal and mask a coal ingredient. crafter_input is the correct read; the entity-level
    count is only a fallback when the input inventory is unavailable.
  - Large RCON responses truncate: build the payload into storage._bn (a key of our own so we
    never race architect's storage._arch or world's storage._world), print its length, read it
    back in CHUNK slices, and rstrip each slice (rcon.print appends a newline per response).

Attribution is deliberately split: the game reports raw per-ingredient DEFICITS, and the pick
(argmax, ties broken alphabetically) happens here in Python, because Lua `pairs` order is
nondeterministic and the same world state must always produce the same attribution.

Usage:
    python3 bottleneck.py sample          one live scan, ranked groups
    python3 bottleneck.py record          scan + append to the history ring
    python3 bottleneck.py report [secs]   windowed ranking over the ring
    python3 bottleneck.py top             the single most-limiting input, quotable
"""
import json
import pathlib
import sys
import time

import rcon
from world import atomic_write   # tempfile + os.replace; other sessions share this worktree

HERE = pathlib.Path(__file__).resolve().parent
HIST_PATH = HERE / "bottleneck-history.json"   # runtime file (gitignored); tests repoint it
CHUNK = 3000        # chars per RCON read slice
MAX_SAMPLES = 720   # ring capacity (~2h at a 10s cadence)
MAX_ROWS = 400      # per-machine rows carried back per scan (starved rows fill it FIRST)
TYPES = ("assembling-machine", "furnace")

# Statuses that mean "this machine wants an input it does not have". 2.1 also has no_recipe /
# recipe_not_researched / recipe_is_parameter / frozen / paused — recorded as-is, never folded
# in here: those are configuration problems, not supply problems.
STARVED = frozenset({"no_ingredients", "item_ingredient_shortage",
                     "fluid_ingredient_shortage", "missing_required_fluid",
                     "no_input_fluid", "low_input_fluid"})


# --------------------------------------------------------------------------- the scan
def scan_lua(bbox=None, types=TYPES, store="storage._bn"):
    """Build the read-only /sc body. Emits, per machine: name, tile pos, recipe (with the
    previous_recipe fallback), status name, and for a STARVED machine the list of ingredients
    it is short of as {n=<item>, d=<deficit>}. Starved rows are collected first so the MAX_ROWS
    cap only ever drops uninteresting (working/full) machines."""
    st = "{" + ",".join("%s=1" % k for k in sorted(STARVED)) + "}"
    ty = "{" + ",".join("'%s'" % t for t in types) + "}"
    area = ""
    if bbox:
        area = "area={{%d,%d},{%d,%d}}," % (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
    return (
        "local s=game.surfaces[1];"
        "local SN={} for k,v in pairs(defines.entity_status) do SN[v]=k end;"
        "local ST=" + st + ";"
        "local MX=" + str(MAX_ROWS) + ";"
        "local tot,stv=0,0; local hot,cold={},{};"
        "for _,e in pairs(s.find_entities_filtered{" + area + "type=" + ty + ",force='player'}) do"
        "  tot=tot+1;"
        "  local st='?'; local oks,sv=pcall(function() return e.status end);"
        "  if oks and sv~=nil then st=SN[sv] or tostring(sv) end;"
        "  local rn=nil; local okr,r=pcall(function() return e.get_recipe() end);"
        "  if okr and r then rn=r.name else"
        # 2.0+ previous_recipe: {name=<LuaRecipePrototype>, quality=...}; the string is name.name
        "    local okp,p=pcall(function() return e.previous_recipe end);"
        "    if okp and p then local nm=p.name;"
        "      if type(nm)=='string' then rn=nm elseif nm then rn=nm.name end end end;"
        "  local row={n=e.name,x=math.floor(e.position.x),y=math.floor(e.position.y),r=rn,s=st};"
        "  if ST[st] then stv=stv+1;"
        "    local pr=rn and prototypes.recipe[rn];"
        "    if pr then"
        # crafter_input (2.1 rename of furnace_source/assembling_machine_input); pcall so a nil
        # define degrades to the entity-level count instead of killing the whole /sc
        "      local inv=nil; local oki,iv=pcall(function()"
        "        return e.get_inventory(defines.inventory.crafter_input) end);"
        "      if oki then inv=iv end;"
        "      local g={};"
        "      for _,ing in pairs(pr.ingredients) do"
        "        local have=0;"
        "        if ing.type=='fluid' then"
        "          local okf,fc=pcall(function() return e.get_fluid_count(ing.name) end);"
        "          have=(okf and fc) or 0"
        "        elseif inv then have=inv.get_item_count(ing.name)"
        "        else have=e.get_item_count(ing.name) end;"
        "        local d=ing.amount-have;"
        "        if d>0 then g[#g+1]={n=ing.name,d=d} end end;"
        "      if #g>0 then row.g=g end end;"
        "    if #hot<MX then hot[#hot+1]=row end"
        "  elseif #cold<MX then cold[#cold+1]=row end end;"
        "for i=1,#cold do if #hot>=MX then break end hot[#hot+1]=cold[i] end;"
        + "%s=helpers.table_to_json({t=game.tick,tot=tot,stv=stv,rows=hot});"
          "rcon.print(#%s)" % (store, store)
    )


def _chunked(build_lua):
    """rcon.read_chunked on a PRIVATE buffer key. `build_lua(store)` returns the Lua body.

    A failed scan RAISES (architect.snapshot's precedent) - it must never degrade into an empty
    sample. record() would persist that as a healthy 0-starved lap, and every such lap grows
    report()'s window_samples without growing `present`, silently diluting a real bottleneck
    below the ranking. A monitor that lies when it breaks is worse than one that stops - and a
    monitor that reassembles two different scans into one is worse still, which is why the
    buffer key is minted per read rather than the fixed storage._bn."""
    try:
        raw = rcon.read_chunked(lambda store: "/sc " + build_lua(store), chunk=CHUNK, empty="")
    except rcon.ChunkedReadError as e:
        raise RuntimeError("bottleneck scan failed (RCON/Lua): %s" % str(e)[:220])
    if not raw:
        raise RuntimeError("bottleneck scan built a 0-length payload")
    return raw


# --------------------------------------------------------------------------- attribution
def pick_missing(gaps):
    """(item, deficit) of the ingredient a machine is shortest of -> the thing to go fix.
    Ties break ALPHABETICALLY: Lua pairs order is nondeterministic, and the same world state
    must always yield the same attribution or the ranking flaps."""
    best = None
    for g in gaps or ():
        d = g.get("d", 0)
        n = g.get("n")
        if d is None or d <= 0 or not n:
            continue
        if best is None or d > best[1] or (d == best[1] and n < best[0]):
            best = (n, d)
    return best if best else (None, 0)


def _group(machines):
    """Aggregate attributed machines by (recipe, status, missing). Machines with no recipe, or
    no missing ingredient, are counted upstream but form no group — there is nothing to blame."""
    agg = {}
    for m in machines:
        if not m["recipe"] or not m["missing"]:
            continue
        k = (m["recipe"], m["status"], m["missing"])
        g = agg.get(k)
        if g is None:
            agg[k] = {"recipe": m["recipe"], "status": m["status"], "missing": m["missing"],
                      "n": 1, "deficit": m["deficit"], "example": [m["name"], m["x"], m["y"]]}
        else:
            g["n"] += 1
            g["deficit"] += m["deficit"]
    return sorted(agg.values(), key=lambda g: (-g["n"], -g["deficit"], g["recipe"], g["missing"]))


def sample(bbox=None, types=TYPES):
    """One read-only scan of every crafting machine -> per-machine attribution + groups.

    {tick, ts, total, starved, truncated, machines:[{name,x,y,recipe,status,missing,deficit}],
     groups:[{recipe,status,missing,n,deficit,example:[name,x,y]}]}

    total/starved are exact counts taken server-side over every machine; `machines` is capped at
    MAX_ROWS (starved rows first), and `truncated` says whether the cap bit into the starved set.
    """
    d = json.loads(_chunked(lambda store: scan_lua(bbox, types, store=store)))
    rows = d.get("rows") or []
    machines = []
    for r in rows:
        miss, deficit = pick_missing(r.get("g"))
        machines.append({"name": r.get("n"), "x": r.get("x"), "y": r.get("y"),
                         "recipe": r.get("r"), "status": r.get("s"),
                         "missing": miss, "deficit": deficit})
    starved = int(d.get("stv") or 0)
    return {"tick": int(d.get("t") or 0), "ts": time.time(),
            "total": int(d.get("tot") or 0), "starved": starved,
            "truncated": starved > MAX_ROWS,
            "machines": machines, "groups": _group(machines)}


# --------------------------------------------------------------------------- history ring
def load_history():
    """The sample ring, newest last. Never raises: a missing or half-written file reads empty."""
    try:
        h = json.loads(pathlib.Path(HIST_PATH).read_text())
    except (OSError, ValueError):
        return []
    return h if isinstance(h, list) else []


def record(s=None):
    """Scan (unless handed a sample) and append it to the ring. Only GROUPS are persisted —
    storing per-machine rows would make the file O(machines x samples) and a megabase would
    blow it up. Returns the full sample."""
    if s is None:
        s = sample()
    hist = load_history()
    hist.append({"tick": s["tick"], "ts": s["ts"], "total": s["total"],
                 "starved": s["starved"], "groups": s["groups"]})
    atomic_write(HIST_PATH, hist[-MAX_SAMPLES:])
    return s


# --------------------------------------------------------------------------- ranking
def report(window_s=600, hist=None):
    """Rank (recipe, missing) causes over the last `window_s` seconds of the ring.

    Sorted by SHARE = persistence x breadth ((samples present / samples in window) * mean
    machines), so a 16-furnace group starved in 9 of 10 samples outranks a 2-machine group that
    flaps. Ties fall back to total deficit, then the key, so the order is deterministic."""
    hist = load_history() if hist is None else hist
    cutoff = time.time() - window_s
    win = [h for h in hist if (h.get("ts") or 0) >= cutoff]
    if not win:
        return []
    agg = {}
    for h in win:
        # COLLAPSE WITHIN THE SAMPLE FIRST. _group keys on (recipe,status,missing) but the
        # ranking keys on (recipe,missing), so one sample can carry several groups that map to
        # the same cause - a machine bank split across no_ingredients and
        # item_ingredient_shortage is the normal partially-fed case. Folding those straight
        # into `agg` counted the sample twice: starved_pct went over 100% (a 200%-of-1-sample
        # headline quoted verbatim into the architect prompt) and `machines` reported only the
        # last group's count instead of the bank.
        per_sample = {}
        for g in h.get("groups") or []:
            recipe, missing = g.get("recipe"), g.get("missing")
            if not recipe or not missing:
                continue                              # nothing to blame; never a ranked cause
            c = per_sample.setdefault((recipe, missing),
                                      {"n": 0, "deficit": 0, "example": None})
            c["n"] += g.get("n") or 0
            c["deficit"] += g.get("deficit") or 0
            c["example"] = c["example"] or g.get("example")
        for k, c in per_sample.items():
            a = agg.setdefault(k, {"present": 0, "counts": [], "deficit": 0,
                                   "machines": 0, "example": None})
            a["present"] += 1                         # once per SAMPLE, never per group
            a["counts"].append(c["n"])
            a["deficit"] += c["deficit"]
            a["machines"] = c["n"]                    # newest sample carrying it wins
            a["example"] = c["example"] or a["example"]
    n = len(win)
    rows = []
    for (recipe, missing), a in agg.items():
        pct = 100.0 * a["present"] / n
        mean = sum(a["counts"]) / float(len(a["counts"]))
        rows.append({
            "recipe": recipe, "missing": missing, "machines": a["machines"],
            "peak": max(a["counts"]), "samples": a["present"], "window_samples": n,
            "starved_pct": round(pct, 1), "share": round((a["present"] / float(n)) * mean, 3),
            "deficit": a["deficit"], "example": a["example"],
            "text": "recipe %s was starved %.0f%% of samples, missing %s (%d machines)"
                    % (recipe, pct, missing, a["machines"]),
        })
    rows.sort(key=lambda r: (-r["share"], -r["deficit"], r["recipe"], r["missing"]))
    return rows


def top_cause(window_s=600):
    """The single most-limiting input across the base, plus a one-line `headline` written to be
    quoted verbatim into a triage/architect prompt (architect.analyze_local wants entities,
    positions and statuses in its evidence). None when the ring has nothing in window."""
    rows = report(window_s)
    if not rows:
        return None
    r = dict(rows[0])
    hist = load_history()
    total = next((h.get("total") for h in reversed(hist) if h.get("total")), r["machines"])
    ex = r.get("example") or ["machine", 0, 0]
    r["headline"] = ("BOTTLENECK: %s starved in %.0f%% of the last %d samples (%ds) - "
                     "missing %s, %d/%d machines, e.g. %s @(%s,%s)"
                     % (r["recipe"], r["starved_pct"], r["window_samples"], window_s,
                        r["missing"], r["machines"], total, ex[0], ex[1], ex[2]))
    return r


def format_report(rows, k=5):
    """Human/dashboard block for the top-k causes."""
    if not rows:
        return "bottleneck: no starved machines in window"
    out = []
    for r in rows[:k]:
        ex = r.get("example") or ["?", 0, 0]
        out.append("%-28s missing %-18s %3d machines (peak %d)  %5.1f%% of %d samples  "
                   "short %d  e.g. %s @(%s,%s)"
                   % (r["recipe"], r["missing"], r["machines"], r["peak"], r["starved_pct"],
                      r["window_samples"], r["deficit"], ex[0], ex[1], ex[2]))
    return "\n".join(out)


# --------------------------------------------------------------------------- cli
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sample"
    if cmd == "sample":
        s = sample()
        print("tick %d  %d/%d machines starved%s"
              % (s["tick"], s["starved"], s["total"], "  (rows truncated)" if s["truncated"] else ""))
        for g in s["groups"]:
            print("  %s|%s|%s x%d  n=%d  e.g. %s @(%s,%s)"
                  % (g["recipe"], g["status"], g["missing"], g["deficit"], g["n"],
                     g["example"][0], g["example"][1], g["example"][2]))
    elif cmd == "record":
        s = record()
        print("recorded tick %d: %d groups, %d/%d starved (ring %d)"
              % (s["tick"], len(s["groups"]), s["starved"], s["total"], len(load_history())))
    elif cmd == "report":
        w = int(sys.argv[2]) if len(sys.argv) > 2 else 600
        print(format_report(report(w)))
    elif cmd == "top":
        t = top_cause()
        print(t["headline"] if t else "no bottleneck in window")
    else:
        print(__doc__.strip().splitlines()[0])
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
