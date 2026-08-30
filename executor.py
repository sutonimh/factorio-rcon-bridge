#!/usr/bin/env python3
"""L2 order executor (MEGABASE-V2-DESIGN section 3).

Declarative orders as dicts: {id, kind, args, phase, role, status, attempts, error}.
Every op runs as: preconditions -> execute -> POST-CONDITION verified via an RCON read
(entity exists / belt tiles present / research actually queued) -> on success register the
result into world.py (role+phase+order_id) -> on failure bounded retry (MAX_ATTEMPTS) then
mark failed with the diagnostic. Never loops forever; escalation beyond retries is the
planner/L4's job.

Kinds:
  place          one entity via autopilot.place (which enforces the GOTCHAS clearspace rule:
                 clear_area first, ABORT on cliffs; and the 1x1/2x2 center math)
  belt_path      bootstrap.lay_belt_path over corner waypoints; verifies + registers the
                 belt tiles actually laid
  research       force.add_research ONE tech at a time + verify it queued (GOTCHAS: the
                 research_queue assignment silently empties; trigger techs can't be queued)
  decon_registry surgical teardown of ONLY registered uids for a role/phase/order_id scope —
                 per-entity destroy with refund; NEVER area-based (GOTCHAS law)
  build          a named builder from BUILDERS (builds_v2 registers wrappers that call the
                 existing bootstrap builds, then diff-scan + hand back entities to register)
  noop           testing

Queue persisted to orders.json (atomic writes; runtime file, gitignored).
autopilot/bootstrap are imported lazily inside handlers so this module (and its offline
tests) load with no live server and no heavyweight imports.
"""
import json
import pathlib
import re

import rcon
import world

HERE = pathlib.Path(__file__).resolve().parent
DB_PATH = HERE / "orders.json"     # tests repoint this at a tmp dir
MAX_ATTEMPTS = 3
KINDS = ("place", "belt_path", "research", "decon_registry", "build", "noop")
BELT_NAMES = ("transport-belt", "underground-belt")
BUILDERS = {}                      # name -> fn(kwargs, order) -> [entity dicts]; builds_v2 fills


class ExecError(Exception):
    """An op failed its precondition, execution, or post-condition; message = diagnostic."""


# --------------------------------------------------------------------------- queue
def _empty():
    return {"seq": 0, "orders": []}


def _load():
    try:
        return json.loads(pathlib.Path(DB_PATH).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return _empty()


def _save(db):
    world.atomic_write(DB_PATH, db)


def _find(db, order_id):
    for o in db["orders"]:
        if o["id"] == order_id:
            return o
    return None


def submit(order):
    """Enqueue an order dict {kind, args?, role?, phase?}. Fills id/status/attempts/error.
    Returns the order id."""
    if order.get("kind") not in KINDS:
        raise ValueError("unknown order kind: %r (kinds: %s)" % (order.get("kind"), ", ".join(KINDS)))
    db = _load()
    db["seq"] += 1
    o = {
        "id": order.get("id") or "o%d" % db["seq"],
        "kind": order["kind"],
        "args": order.get("args", {}),
        "phase": order.get("phase", 0),
        "role": order.get("role"),
        "status": "pending",
        "attempts": 0,
        "error": None,
    }
    if _find(db, o["id"]):
        raise ValueError("duplicate order id: %s" % o["id"])
    db["orders"].append(o)
    _save(db)
    return o["id"]


def run(order_id=None):
    """Run one order (by id, or the first pending). Bounded retry up to MAX_ATTEMPTS, then
    status=failed with the diagnostic in `error`. Returns the final order dict, or None if
    nothing is pending."""
    db = _load()
    if order_id is not None:
        o = _find(db, order_id)
        if o is None:
            raise ValueError("no such order: %s" % order_id)
        if o["status"] not in ("pending", "failed"):
            return dict(o)
    else:
        o = next((x for x in db["orders"] if x["status"] == "pending"), None)
        if o is None:
            return None
    handler = _HANDLERS[o["kind"]]
    while o["attempts"] < MAX_ATTEMPTS:
        o["attempts"] += 1
        o["status"] = "running"
        _save(db)
        try:
            entities = handler(o.get("args") or {}, o)
        except ExecError as e:
            o["error"] = str(e)
        except Exception as e:                      # a bug is a diagnostic too, never a loop
            o["error"] = "%s: %s" % (type(e).__name__, e)
        else:
            if entities:
                world.register(entities, role=o.get("role"), phase=o.get("phase"),
                               order_id=o["id"])
            o["status"] = "done"
            o["error"] = None
            _save(db)
            return dict(o)
    o["status"] = "failed"
    _save(db)
    return dict(o)


def run_next():
    """Run the first pending order. Returns the final order dict or None if queue is empty."""
    return run(None)


def run_all(max_ops=None):
    """Run pending orders in queue order until none are left (or max_ops). A failed order
    does not block the rest. Returns the list of orders run."""
    done = []
    while max_ops is None or len(done) < max_ops:
        o = run_next()
        if o is None:
            break
        done.append(o)
    return done


def status():
    """Queue summary: counts per status + the failed orders' diagnostics."""
    db = _load()
    counts = {}
    for o in db["orders"]:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    failed = [{"id": o["id"], "kind": o["kind"], "error": o["error"]}
              for o in db["orders"] if o["status"] == "failed"]
    return {"counts": counts, "failed": failed, "total": len(db["orders"])}


# --------------------------------------------------------------------------- check helpers
# Reusable post-condition/invariant helpers encoding GOTCHAS rules.

def guarded_remove(target, name_expr, count_expr):
    """Lua for `target.remove{...}` guarded on count>0 (GOTCHAS: remove{count=0} THROWS
    'count must be positive' and aborts the whole /sc — froze the base once). Use this for
    EVERY generated remove whose count comes from an insert's return."""
    return ("if %s>0 then %s.remove{name=%s,count=%s} end"
            % (count_expr, target, name_expr, count_expr))


def check_clearspace(x, y, radius=10):
    """GOTCHAS clearspace law: clear trees/rocks around the site; a remaining CLIFF means the
    site must MOVE (cliffs are unmineable) — raises ExecError. (autopilot.place already runs
    this internally; call it directly for multi-entity sites, once over the whole bbox.)"""
    import autopilot as A
    _, cliffs = A.clear_area(x, y, radius)
    if cliffs:
        raise ExecError("CLIFF x%d within %d of (%s,%s) - move the site" % (cliffs, radius, x, y))


def check_entity_at(name, cx, cy, radius=1.0):
    """Verify an entity of `name` exists near center (cx,cy) via an RCON read. Returns
    (floor_x, floor_y, direction); raises ExecError if absent."""
    out = rcon.run(
        "/sc local s=game.surfaces[1];"
        "local e=s.find_entities_filtered{name='%s',position={%s,%s},radius=%s}[1];"
        "if e then local okd,d=pcall(function() return e.direction end);"
        " rcon.print('ok,'..math.floor(e.position.x)..','..math.floor(e.position.y)..','..((okd and tonumber(d)) or 0))"
        " else rcon.print('missing') end" % (name, cx, cy, radius)).strip()
    if not out.startswith("ok,"):
        raise ExecError("post-condition: no %s at (%s,%s) [%s]" % (name, cx, cy, out))
    _, x, y, d = out.split(",")
    return int(x), int(y), int(d)


def check_research_queued(tech):
    """Verify `tech` is the current research or in the queue (GOTCHAS: verify after every
    research write — queue writes have silently dropped entries)."""
    out = rcon.run(
        "/sc local f=game.forces.player; local q=false;"
        "if f.current_research and f.current_research.name=='%s' then q=true end;"
        "if not q then local ok,rq=pcall(function() return f.research_queue end);"
        " if ok and rq then for _,t in pairs(rq) do local okn,n=pcall(function() return t.name end);"
        "  if (okn and n or tostring(t))=='%s' then q=true break end end end end;"
        "rcon.print(q and 'queued' or 'notqueued')" % (tech, tech)).strip()
    if out != "queued":
        raise ExecError("research %s did not queue (verify read: %s)" % (tech, out))


def path_tiles(waypoints):
    """Expand corner waypoints to the tile list lay_belt_path lays (mirror of its expansion,
    minus per-tile directions)."""
    pts = []
    for i in range(len(waypoints) - 1):
        x1, y1 = waypoints[i]
        x2, y2 = waypoints[i + 1]
        dx = (x2 > x1) - (x2 < x1)
        dy = (y2 > y1) - (y2 < y1)
        for k in range(max(abs(x2 - x1), abs(y2 - y1))):
            pts.append((x1 + dx * k, y1 + dy * k))
    pts.append(tuple(waypoints[-1]))
    return pts


# --------------------------------------------------------------------------- op handlers
def _op_noop(args, order):
    # testing kind: {"fail": true} always fails; {"fail_times": n} fails the first n attempts.
    if args.get("fail"):
        raise ExecError(args.get("diag", "noop forced failure"))
    if order["attempts"] <= int(args.get("fail_times", 0)):
        raise ExecError("noop transient failure (attempt %d)" % order["attempts"])
    return []


def _op_place(args, order):
    import autopilot as A
    name = args["name"]
    tx, ty = int(args["tile_x"]), int(args["tile_y"])
    d = int(args.get("direction", 0))
    # A.place enforces clearspace (clear_area first, CLIFF abort) + 1x1/2x2 center math.
    out = A.place(name, tx, ty, direction=d, clear=int(args.get("clear", 10))).strip()
    m = re.match(r"BUILT (\S+) @\((-?[\d.]+),(-?[\d.]+)\)", out)
    if not m:
        raise ExecError(out or "place returned nothing")
    ename, cx, cy = m.group(1), float(m.group(2)), float(m.group(3))
    x, y, dd = check_entity_at(ename, cx, cy)          # post-condition: it really exists
    return [{"name": ename, "tile_pos": (x, y), "direction": dd}]


def _op_belt_path(args, order):
    import bootstrap
    wps = [tuple(int(v) for v in w) for w in args["waypoints"]]
    if len(wps) < 2:
        raise ExecError("belt_path needs >=2 waypoints")
    bootstrap.lay_belt_path(wps)
    gaps = bootstrap.LAST_LAY_GAPS   # lay_belt_path returns its TILES now (lane registry)
    if gaps:
        raise ExecError("lay_belt_path left %d unbridged gaps" % gaps)
    # post-condition: read back the path tiles; endpoints MUST hold belt (mid-path tiles may
    # legitimately be empty where an underground pair bridges a blocked span).
    tiles = path_tiles(wps)
    live = world.scan_tiles(tiles, BELT_NAMES)
    have = {(e["x"], e["y"]) for e in live}
    for end in (tiles[0], tiles[-1]):
        if tuple(end) not in have:
            raise ExecError("belt path endpoint %s has no belt after lay" % (end,))
    return [{"name": e["n"], "tile_pos": (e["x"], e["y"]), "direction": e["d"]} for e in live]


def _op_research(args, order):
    tech = args["tech"]
    pre = rcon.run(
        "/sc local f=game.forces.player; local t=f.technologies['%s'];"
        "if not t then rcon.print('NO_TECH')"
        " elseif t.researched then rcon.print('DONE')"
        " elseif t.prototype.research_trigger then rcon.print('TRIGGER')"
        " else rcon.print('OK') end" % tech).strip()
    if pre == "NO_TECH":
        raise ExecError("unknown technology: %s" % tech)
    if pre == "TRIGGER":
        # GOTCHAS: trigger techs (e.g. oil-processing = mine crude) auto-complete from play
        # and can NEVER be queued — fail loudly instead of add_research spinning.
        raise ExecError("%s is a TRIGGER tech (auto-completes from play, e.g. mining/crafting)"
                        " - it cannot be queued; satisfy its trigger instead" % tech)
    if pre == "DONE":
        return []                                       # idempotent success
    if pre != "OK":
        raise ExecError("research precheck for %s returned %r" % (tech, pre))
    # GOTCHAS: add_research ONE at a time (f.research_queue={...} silently emptied the queue).
    rcon.run("/sc game.forces.player.add_research('%s')" % tech)
    check_research_queued(tech)                         # verify-after-write, always
    return []


def _op_decon_registry(args, order):
    # Surgical teardown law: destroy ONLY entities this registry attributes to the scope,
    # each by name at its exact recorded tile, with refund into derpface's inventory.
    # NEVER find_entities_filtered{area=...} -> destroy (that once wiped live supply lines).
    role = args.get("role")
    if role is None:
        raise ExecError("decon_registry requires a role scope")
    recs = world.query(role=role, phase=args.get("phase"), order_id=args.get("order_id"))
    if not recs:
        return []
    live = [r for r in recs if not r.get("missing")]
    for batch in [live[i:i + 200] for i in range(0, len(live), 200)]:
        spec = ";".join("%s,%d,%d" % (r["name"], r["tile_pos"][0], r["tile_pos"][1]) for r in batch)
        gr_fuel = guarded_remove("iv", "c.name", "g")
        rcon.run(
            "/sc local s=game.surfaces[1]; local p=storage.derpface;"
            "local inv=(p and p.valid) and p.get_main_inventory() or nil; local removed=0;"
            "for name,a,b in ([==[" + spec + "]==]):gmatch('([%w%-]+),(-?%d+),(-?%d+)') do"
            "  local x,y=tonumber(a),tonumber(b);"
            "  local e=s.find_entities_filtered{name=name,position={x+0.5,y+0.5},radius=1.2}[1];"
            "  if e and e.valid then"
            "    if inv then"
            # drain fuel/output/chest inventories into derpface (refund), guarded removes only
            "      for _,fn in ipairs({'get_fuel_inventory','get_output_inventory'}) do"
            "        local ok,iv=pcall(function() return e[fn](e) end);"
            "        if ok and iv then for _,c in pairs(iv.get_contents()) do"
            "          local g=inv.insert{name=c.name,count=c.count}; " + gr_fuel + " end end end;"
            "      local okc,iv=pcall(function() return e.get_inventory(defines.inventory.chest) end);"
            "      if okc and iv then for _,c in pairs(iv.get_contents()) do"
            "        local g=inv.insert{name=c.name,count=c.count}; " + gr_fuel + " end end;"
            "      inv.insert{name=name,count=1};"
            "    end;"
            "    e.destroy(); removed=removed+1"
            "  end end;"
            "rcon.print(removed)")
    # post-condition: nothing in scope may remain at its recorded tile
    spec = ";".join("%s,%d,%d" % (r["name"], r["tile_pos"][0], r["tile_pos"][1]) for r in live)
    left = rcon.run(
        "/sc local s=game.surfaces[1]; local n=0;"
        "for name,a,b in ([==[" + spec + "]==]):gmatch('([%w%-]+),(-?%d+),(-?%d+)') do"
        "  local x,y=tonumber(a),tonumber(b);"
        "  if s.find_entities_filtered{name=name,position={x+0.5,y+0.5},radius=1.2}[1] then n=n+1 end end;"
        "rcon.print(n)").strip()
    if left not in ("", "0"):
        raise ExecError("decon left %s scoped entities standing" % left)
    world.unregister([r["uid"] for r in recs])
    return []


def _op_build(args, order):
    fn = BUILDERS.get(args.get("fn"))
    if fn is None:
        raise ExecError("no registered builder %r (have: %s)"
                        % (args.get("fn"), ", ".join(sorted(BUILDERS)) or "none"))
    return fn(args.get("kwargs", {}), order)


_HANDLERS = {
    "noop": _op_noop,
    "place": _op_place,
    "belt_path": _op_belt_path,
    "research": _op_research,
    "decon_registry": _op_decon_registry,
    "build": _op_build,
}
