#!/usr/bin/env python3
"""L3 build orchestration: plan -> apply -> verify -> (verified | rollback+failed).

Tonight's failure mode: builds were fire-and-forget. A half-built lane left litter nobody
owned, and nothing could tell "the operator deleted this" from "this was never built".
A BuildPlan record is both the INPUT to a build and its AUDIT TRAIL: apply() writes the
full result back into the same file as `verify` (ported from factorio-planning-agent's
daemon.ts autoApply verify-writeback). Statuses:

    planned -> applying -> verified | failed | superseded

Four protections, in the order apply() enforces them (the ORDER is the crash safety —
do not reorder):
  1. TRUCE      zero construction while a human is connected (GOTCHAS Build Law 6).
  2. STALENESS  a plan whose area changed after its scan_tick is refused with
                "re-scan and re-plan" - this is exactly what would have stopped the bot
                re-laying a lane the operator had just deleted.
  3. PROTECTED  operator-deleted tiles are skipped; a route >=25% protected is
                OPERATOR-OWNED and is superseded, never laid (Build Law 3).
  4. status="applying" is saved BEFORE the first placement, so every later crash is
     recoverable by resume().
(Ahead of all four, a plan already marked "superseded" is refused outright: its route was
retired, and re-applying it lays the old lane beside its replacement - the "two belts from
each patch" bug bootstrap.teardown_lane exists to fix. verified/failed plans stay
re-appliable; that is the idempotent refill path.)
Then: place -> record_built (before the check, so a crash still knows we built it) ->
functional check -> verified, or failed + scoped rollback in the same pass (Build Law 2:
"if the result is nothing, remove what you built").

STALENESS WITHOUT EVENT HANDLERS: upstream stamps dirty cells from on_built_entity /
on_player_mined_entity. We may NEVER register runtime handlers - it mutates the level's
handler set and Factorio then refuses every joining client ("mod event handlers are not
identical"), which locked the operator out of his own server. So the dirty map is POLLED:
refresh_dirty() does one read-only world.scan_area, buckets entities into 16-tile cells
(upstream's granularity), fingerprints each cell, and stamps the cells whose fingerprint
moved. Consequences, honored in the tests:
  - the FIRST observation of a cell is never dirty (there is no baseline to differ from),
    otherwise every plan would be stale forever;
  - a change is attributed to the DETECTING SCAN's tick, not the change's tick, so a
    plan's scan_tick must come from plan_scan() (the same clock), never a bare game_tick();
  - a change-and-revert between two scans is invisible (the ledger + bootstrap.
    reconcile_removals are the second line of defense);
  - after a successful apply we SELF-ABSORB (absorb()): re-fingerprint our own cells
    WITHOUT bumping their tick, or the bot's own build makes its next plan stale and it
    thrashes (upstream's `selfWrites` map, daemon.ts).

Persistence: plans/<id>.json, atomic (world.atomic_write - other sessions share this
worktree). plans/_dirty.json is shared state: last-writer-wins, and the RCON read always
happens BEFORE the file is opened, never across a round trip.

The protected/built-tile ledger lives in bootstrap.py and is USED here, never duplicated
(_protected/_record_built/_forget_built/_operator_present are thin lazy-import wrappers so
offline tests can monkeypatch them without touching built-tiles.json or the live server).
"""
import hashlib
import json
import pathlib
import random
import re
import time

import executor
import rcon
import world

HERE = pathlib.Path(__file__).resolve().parent
PLANS_DIR = HERE / "plans"                  # tests repoint this at a tmp dir
DIRTY_PATH = PLANS_DIR / "_dirty.json"
CELL = 16                                   # upstream's cell size (control.lua mark_dirty)
OWNED_THRESHOLD = 0.25                      # mirrors bootstrap.route_is_operator_owned
STATUSES = ("planned", "applying", "verified", "failed", "superseded")

# kind -> {"place":fn, "verify":fn, "remove":fn}; mirrors executor.BUILDERS. resume()
# resolves a crashed plan's verifier by kind, so a restart needs no caller context.
KINDS = {}


# --------------------------------------------------------------------------- bootstrap
# Thin wrappers doing a lazy `import bootstrap` (cheap + side-effect-free offline, but its
# ledger paths are hardcoded to the repo dir). Tests monkeypatch THESE, not the ledger.
def _protected():
    import bootstrap
    return bootstrap._protected_load()


def _record_built(tiles):
    import bootstrap
    bootstrap.record_built(tiles)


def _forget_built(tiles):
    import bootstrap
    bootstrap.forget_built(tiles)


def _operator_present():
    import bootstrap
    return bootstrap.operator_present()


def _build_worked(check, tries, delay):
    import bootstrap
    return bootstrap.build_worked(check, tries=tries, delay=delay)


# --------------------------------------------------------------------------- helpers
def _xy(t):
    return (int(t[0]), int(t[1]))


def _xys(tiles):
    return [_xy(t) for t in tiles or ()]


def _pairs(tiles):
    return [[int(x), int(y)] for (x, y) in tiles]


def cell(x, y):
    """The 16-tile dirty cell a tile falls in. Python // floors toward -inf, matching Lua's
    math.floor(x/16) - the two must agree or negative-coordinate cells desync."""
    return (int(x) // CELL, int(y) // CELL)


def _key(x, y):
    cx, cy = cell(x, y)
    return "%d|%d" % (cx, cy)


def _bbox(tiles, pad=0):
    pts = _xys(tiles)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


# --------------------------------------------------------------------------- records
def path_for(pid):
    return pathlib.Path(PLANS_DIR) / ("%s.json" % pid)


def _new_id():
    """16 random bits inside a one-second bucket is a plausible birthday collision for a
    planner emitting plans in a loop - and for two sessions sharing plans/. save() would then
    silently OVERWRITE the older record, losing the verify.placed that IS rollback's scope
    (litter nobody owns - the exact failure this module exists to end). So: never hand back an
    id whose file already exists."""
    pid = "p-%s-%04x" % (time.strftime("%Y%m%d-%H%M%S"), random.getrandbits(16))
    if not path_for(pid).exists():
        return pid
    n = 2
    while path_for("%s-%d" % (pid, n)).exists():
        n += 1
    return "%s-%d" % (pid, n)


def save(plan):
    """Persist atomically. The record is both the plan and its audit trail, so every
    mutation goes through here."""
    d = pathlib.Path(PLANS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    world.atomic_write(path_for(plan["id"]), plan)
    return plan


def load(pid):
    p = path_for(pid)
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        raise KeyError("no such plan: %s" % pid)


def plans(status=None):
    """Every persisted plan (optionally filtered by status), newest id last. Files starting
    with '_' are shared runtime state (_dirty.json), not plans."""
    d = pathlib.Path(PLANS_DIR)
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if status is None or rec.get("status") == status:
            out.append(rec)
    return out


def new_plan(kind, args, tiles, scan_tick=None, names=None, id=None):
    """Create + persist a plan in status "planned" (inert - only apply() touches the world;
    upstream's `auto_apply:false` draft opt-out).

    tiles: [(x,y)] or [(x,y,d)] - the third element is a 16-way direction (autopilot DIRS16;
    cardinals are multiples of 4) and is carried through to place_fn untouched.
    names: the entity names this plan lays. The idempotence probe and the default remover
    both key off it - without it apply() cannot tell "already built" from "empty ground".
    scan_tick: MUST come from plan_scan() over this plan's area. A bare game_tick() taken at
    some other moment is not the same clock as the dirty map's attribution.
    """
    tiles = [list(_xy(t)) + ([int(t[2])] if len(t) > 2 else []) for t in tiles]
    if scan_tick is None:
        # Enforce the law above instead of quietly breaking it: a bare game_tick() is NOT the
        # dirty map's clock, and every stamped cell tick is <= now, so a plan carrying one can
        # never be stale - gate 2 would silently default to OFF. Scan this plan's own area.
        scan_tick = plan_scan(_bbox(tiles)) if tiles else game_tick()
    plan = {
        "id": id or _new_id(),
        "kind": kind,
        "args": dict(args or {}),
        "tiles": tiles,
        "names": list(names or ()),
        "created_tick": int(scan_tick),
        "scan_tick": int(scan_tick),
        "status": "planned",
        "verify": {},
    }
    return save(plan)


def register(kind, place=None, verify=None, remove=None):
    """Register a kind's fns so resume() can re-verify a crashed plan with no caller
    context. Mirrors executor.BUILDERS."""
    KINDS[kind] = {"place": place, "verify": verify, "remove": remove}
    return KINDS[kind]


# --------------------------------------------------------------------------- dirty map
def game_tick():
    """RCON READ ONLY."""
    out = rcon.run("/sc rcon.print(game.tick)").strip()
    try:
        return int(out)
    except ValueError:
        raise RuntimeError("game_tick: unparseable RCON response %r" % out[:80])


def _dirty_load():
    try:
        return json.loads(pathlib.Path(DIRTY_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return {"cells": {}}


def _dirty_save(d):
    pathlib.Path(DIRTY_PATH).parent.mkdir(parents=True, exist_ok=True)
    world.atomic_write(DIRTY_PATH, d)


def _fingerprint(bbox):
    """{cell_key: fingerprint} for every cell FULLY covered by bbox. RCON READ ONLY.

    The bbox is snapped OUT to cell boundaries first: a partially-observed cell would
    fingerprint differently every time the scan window moved, and read as dirty forever.
    An emptied cell still gets a key (the empty-set hash), so a total clear is detected.
    """
    x1, y1, x2, y2 = bbox
    cx1, cy1 = cell(min(x1, x2), min(y1, y2))
    cx2, cy2 = cell(max(x1, x2), max(y1, y2))
    ents = world.scan_area(cx1 * CELL, cy1 * CELL, (cx2 + 1) * CELL, (cy2 + 1) * CELL)
    buckets = {}
    for e in ents:
        cx, cy = cell(e["x"], e["y"])
        if not (cx1 <= cx <= cx2 and cy1 <= cy <= cy2):
            continue                          # spill: find_entities_filtered areas are inclusive
        buckets.setdefault("%d|%d" % (cx, cy), []).append(
            "%s,%d,%d,%d" % (e["n"], e["x"], e["y"], e.get("d", 0)))
    out = {}
    for cy in range(cy1, cy2 + 1):
        for cx in range(cx1, cx2 + 1):
            k = "%d|%d" % (cx, cy)
            blob = ";".join(sorted(buckets.get(k, ())))
            out[k] = hashlib.sha1(blob.encode()).hexdigest()[:16]
    return out


def refresh_dirty(bbox, tick=None):
    """Re-fingerprint an area and stamp the cells that moved. RCON READ ONLY.
    Returns {"scanned":n, "dirtied":[cell keys], "tick":t}."""
    fps = _fingerprint(bbox)                  # RCON round trip FIRST - never hold the shared
    t = int(tick) if tick is not None else game_tick()   # _dirty.json open across one
    d = _dirty_load()
    cells = d.setdefault("cells", {})
    dirtied = []
    for k, fp in fps.items():
        prev = cells.get(k)
        if prev is None:
            cells[k] = {"fp": fp, "tick": 0}  # first observation is NEVER dirty: no baseline
        elif prev.get("fp") != fp:
            prev["fp"] = fp
            prev["tick"] = t
            dirtied.append(k)
    _dirty_save(d)
    return {"scanned": len(fps), "dirtied": dirtied, "tick": t}


def plan_scan(bbox):
    """Refresh the dirty map over the area a plan is about to be computed for and return the
    tick to use as its scan_tick. Use THIS, not game_tick(), or the plan's clock and the
    dirty map's attribution clock disagree."""
    return refresh_dirty(bbox)["tick"]


def absorb(plan):
    """Re-fingerprint the plan's own cells WITHOUT bumping their tick. Mandatory after a
    successful apply (upstream's selfWrites map): our own placements must never read back as
    "the operator changed this", or the next plan over the same ground is instantly stale and
    the bot thrashes. RCON READ ONLY.

    Hazard, accepted: an operator edit landing inside the same window is absorbed as ours.
    The truce gate means he is not connected during an apply, and reconcile_removals() still
    catches his deletions.
    """
    if not plan.get("tiles"):
        return {}
    fps = _fingerprint(_bbox(plan["tiles"]))
    d = _dirty_load()
    cells = d.setdefault("cells", {})
    for k, fp in fps.items():
        prev = cells.get(k)
        if prev is None:
            cells[k] = {"fp": fp, "tick": 0}
        else:
            prev["fp"] = fp                   # tick deliberately untouched
    _dirty_save(d)
    return fps


def is_stale(plan):
    """None, or why the plan must not be applied. Pure file read - the staleness gate never
    needs the server, so a refusal costs nothing. (control.lua place_ghosts staleness gate.)"""
    scan_tick = plan.get("scan_tick")
    if scan_tick is None:
        return None
    cells = _dirty_load().get("cells", {})
    seen, bad, latest = set(), [], 0
    for t in plan.get("tiles") or ():
        k = _key(*_xy(t))
        if k in seen:
            continue
        seen.add(k)
        rec = cells.get(k)
        if rec and int(rec.get("tick", 0)) > int(scan_tick):
            bad.append(k)
            latest = max(latest, int(rec["tick"]))
    if not bad:
        return None
    return {
        "cells": bad,
        "last_change_tick": latest,
        "advice": "re-scan and re-plan",
        "error": ("STALE PLAN: %d cell(s) changed at tick %d but this plan is based on state "
                  "from tick %d. The world moved on - re-scan and re-plan (plan_scan the area, "
                  "rebuild the tile list, then apply)." % (len(bad), latest, int(scan_tick))),
    }


# --------------------------------------------------------------------------- default fns
SPEC_BUDGET = 3800          # keep every generated /sc under the 4KB RCON command cap
SCAN_BATCH = 200            # tiles per scan_tiles call (~12 spec chars each + ~700 fixed)


def _scan_tiles(tiles, names):
    """world.scan_tiles builds ONE /sc holding every tile spec, so a few hundred tiles blows
    the 4KB cap and the read silently truncates (a truncated probe reads as 'not built' and
    would double-place). Chunk it. RCON READ ONLY."""
    out = []
    for i in range(0, len(tiles), SCAN_BATCH):
        out.extend(world.scan_tiles(tiles[i:i + SCAN_BATCH], names))
    return out


def probe(plan, tiles):
    """Which of `tiles` ALREADY hold one of the plan's entity names -> set of (x,y).
    RCON READ ONLY. This is what makes re-apply fill only what is missing: upstream probes
    radius 0.2 around each target and counts a matching entity as satisfied, not as a
    collision (control.lua idempotent resubmission). We place real entities from inventory,
    not ghosts, so a match is a REAL entity."""
    names = plan.get("names") or []
    pts = _xys(tiles)
    if not names or not pts:
        return set()
    return {(e["x"], e["y"]) for e in _scan_tiles(pts, names)}


def _default_remove(plan, tiles):
    """Refunding, registry-scoped teardown of exactly `tiles`. RCON WRITE - only ever called
    from rollback()/supersede(). Never area-based: it destroys ONLY entities matching this
    plan's own `names` at this plan's own tiles, so a foreign entity that drifted onto a tile
    is left alone and counted as not_found (executor._op_decon_registry pattern).

    Returns {"removed", "not_found", "removed_tiles"}. removed_tiles is what rollback() drops
    from the built ledger, so a tile we could NOT find (the operator deleted it) stays recorded
    as ours and reconcile_removals can still protect it - which is what rollback promises.
    """
    names = plan.get("names") or []
    if not names:
        raise ValueError("plan %s has no `names`: the default remover cannot tell our entities "
                         "from the operator's - pass remove_fn=" % plan.get("id"))
    want = _xys(tiles)
    if not want:
        return {"removed": 0, "not_found": 0, "removed_tiles": []}
    if len(names) == 1:
        # ONE name = no attribution ambiguity, so name every tile directly rather than probing
        # for it first. That also side-steps world.scan_tiles, whose radius-0.6 lookup around
        # tile+0.5 is BLIND to any entity whose footprint is even on BOTH axes: a 2x2's center
        # lands on integers, 0.707 away (verified live 2026-08-29 - all 28 stone-furnaces in
        # the base read MISS at 0.6, HIT at 0.8). Probing first would have left every furnace
        # and burner-drill a plan placed standing in the ground while rollback reported done.
        hits = [(names[0], x, y) for (x, y) in want]
    else:
        hits = [(e["n"], e["x"], e["y"]) for e in _scan_tiles(want, names)]
    gr = executor.guarded_remove("iv", "c.name", "g")   # remove{count=0} THROWS and aborts /sc

    def _lua(spec):
        return (
            "/sc local s=game.surfaces[1]; local p=storage.derpface;"
            "local inv=(p and p.valid) and p.get_main_inventory() or nil; local gone={};"
            "for name,a,b in ([==[" + spec + "]==]):gmatch('([%w%-]+),(-?%d+),(-?%d+)') do"
            "  local x,y=tonumber(a),tonumber(b);"
            # 0.8 clears an even-footprint center (0.707) but stays under the 1.0 spacing of a
            # same-name 1x1 neighbour - executor's 1.2 would reach the neighbouring belt tile.
            "  local e=s.find_entities_filtered{name=name,position={x+0.5,y+0.5},radius=0.8}[1];"
            "  if e and e.valid then"
            "    if inv then"
            # belts carry items ON the line, not in an inventory (teardown_lane pattern)
            "      if e.type=='transport-belt' then"
            "        for li=1,e.get_max_transport_line_index() do"
            "          local L=e.get_transport_line(li);"
            "          for _,it in pairs(L.get_contents()) do inv.insert{name=it.name,count=it.count} end end end;"
            "      for _,fn in ipairs({'get_fuel_inventory','get_output_inventory'}) do"
            "        local ok,iv=pcall(function() return e[fn](e) end);"
            "        if ok and iv then for _,c in pairs(iv.get_contents()) do"
            "          local g=inv.insert{name=c.name,count=c.count}; " + gr + " end end end;"
            "      inv.insert{name=name,count=1};"
            "    end;"
            # echo the tiles actually destroyed, not a bare count: rollback must forget exactly
            # these and no more. Always shorter than the spec that produced it (it drops the
            # name), so it cannot outgrow the batch budget or truncate.
            "    e.destroy(); gone[#gone+1]=x..','..y"
            "  end end;"
            "rcon.print(table.concat(gone,';'))")

    # Batch by the REAL command length, not a magic tile count: entity names vary from
    # 'pipe' to 'electric-mining-drill', and a spec that pushes the /sc past 4KB is silently
    # truncated mid-tile - which would destroy an entity the caller never named.
    overhead = len(_lua(""))
    batches, cur, size = [], [], 0
    for h in hits:
        ent = "%s,%d,%d" % h
        add = len(ent) + (1 if cur else 0)          # +1 for the ';' separator
        if cur and overhead + size + add > SPEC_BUDGET:
            batches.append(cur)
            cur, size, add = [], 0, len(ent)
        cur.append(ent)
        size += add
    if cur:
        batches.append(cur)
    gone, seen = [], set()
    for batch in batches:
        out = rcon.run(_lua(";".join(batch))).strip()
        for a, b in re.findall(r"(-?\d+),(-?\d+)", out):
            t = (int(a), int(b))
            if t not in seen:
                seen.add(t)
                gone.append(t)
    return {"removed": len(gone), "not_found": len(want) - len(gone), "removed_tiles": gone}


def _norm_check(r):
    """verify_fn may return bool | (ok, detail) | {"ok":..,"detail":..}."""
    if isinstance(r, dict):
        return bool(r.get("ok")), str(r.get("detail") or "")
    if isinstance(r, (tuple, list)) and len(r) == 2:
        return bool(r[0]), str(r[1] or "")
    return bool(r), ""


def _verify_block(plan):
    v = plan.setdefault("verify", {})
    v.setdefault("attempts", 0)
    v.setdefault("placed", [])
    v.setdefault("already", [])
    v.setdefault("failed", [])
    v.setdefault("protected_skipped", [])
    return v


def _refuse(plan, msg, status=None):
    v = _verify_block(plan)
    v["refused"] = msg
    v["ts"] = time.time()
    if status:
        plan["status"] = status
    save(plan)
    return plan


# --------------------------------------------------------------------------- lifecycle
def apply(plan, place_fn=None, verify_fn=None, probe_fn=None, tries=6, delay=5,
          rollback_on_fail=True, force=False):
    """Apply a plan and write the full result back into it as `verify` (daemon.ts autoApply).

    Idempotent: the world is probed first, so a re-apply hands place_fn ONLY the tiles that
    are still missing. verify.placed is the UNION across every apply - it is rollback's scope.

    place_fn(plan, tiles)  -> {"placed":[(x,y)], "already":[(x,y)],
                               "failed":[{"tile":(x,y),"reason":str}]}
    verify_fn(plan)        -> bool | (ok, detail) | {"ok":bool,"detail":str}
    probe_fn(plan, tiles)  -> set of (x,y) already satisfied (defaults to probe()).

    force=True bypasses the STALENESS gate only ("I know, re-apply anyway"). The truce and
    protected gates are laws and are never bypassable.
    """
    kind = KINDS.get(plan.get("kind")) or {}
    place_fn = place_fn or kind.get("place")
    verify_fn = verify_fn or kind.get("verify")
    probe_fn = probe_fn or probe

    # gate 0a: a superseded plan is RETIRED - its route was torn out or handed to a successor.
    # Re-applying one re-lays the old route beside the new one ("two belts from each patch",
    # bootstrap.teardown_lane) or re-lays a route the operator owns. verified/failed plans stay
    # re-appliable: that is the idempotent refill path.
    if plan.get("status") == "superseded":
        return _refuse(plan, "SUPERSEDED PLAN: this plan was retired (%s); it must not be "
                             "re-applied - plan the replacement route instead."
                       % ((plan.get("verify") or {}).get("superseded", {}).get("reason")
                          or "no reason recorded"))

    # gate 0b: Build Law 1 - never build what cannot be verified. Refusing UP FRONT (before
    # anything is placed) beats placing and then tearing it down for a missing argument.
    if verify_fn is None:
        return _refuse(plan, "NO FUNCTIONAL CHECK: pass verify_fn= or register(%r, verify=...). "
                             "A build that is not verified against 'does it actually do "
                             "something' is not allowed to happen." % plan.get("kind"))
    if place_fn is None:
        return _refuse(plan, "no place_fn for kind %r (pass place_fn= or register it)"
                       % plan.get("kind"))

    # gate 1: the truce. Zero construction while a human is connected - builder AND heals.
    if _operator_present():
        return _refuse(plan, "OPERATOR PRESENT: a human is connected; zero construction until "
                             "he logs off (truce).")

    # gate 2: staleness. Costs no RCON, so a refusal is free.
    if not force:
        st = is_stale(plan)
        if st:
            _refuse(plan, st["error"])       # status stays "planned"; the world is untouched
            plan["verify"]["stale"] = {"cells": st["cells"],
                                       "last_change_tick": st["last_change_tick"]}
            return save(plan)

    # gate 3: operator-protected tiles. He deleted them on purpose; a route that is mostly
    # his deletions is OPERATOR-OWNED and must never be laid again (Build Law 3).
    prot = _protected()
    tiles = plan.get("tiles") or []
    skipped = [t for t in tiles if _xy(t) in prot]
    if tiles and len(skipped) / len(tiles) >= OWNED_THRESHOLD:
        v = _verify_block(plan)
        v["protected_skipped"] = _pairs(_xys(skipped))
        return _refuse(plan, "OPERATOR-OWNED ROUTE: %d/%d tiles are operator-protected "
                             "(>=%d%%) - the operator deleted this on purpose; not laying it."
                       % (len(skipped), len(tiles), int(OWNED_THRESHOLD * 100)),
                       status="superseded")
    todo0 = [t for t in tiles if _xy(t) not in prot]

    # ---- the crash-safety hinge: "applying" is on disk before anything is placed.
    v = _verify_block(plan)
    v["protected_skipped"] = _pairs(_xys(skipped))
    v["attempts"] = int(v.get("attempts", 0)) + 1
    plan["status"] = "applying"
    save(plan)

    already = probe_fn(plan, todo0) or set()
    already = {_xy(t) for t in already}
    todo = [t for t in todo0 if _xy(t) not in already]
    res = place_fn(plan, todo) if todo else {}
    res = res or {}
    placed = _xys(res.get("placed"))
    already |= {_xy(t) for t in _xys(res.get("already"))}   # place-time races count too
    failed = [{"tile": list(_xy(f["tile"])), "reason": str(f.get("reason", ""))}
              for f in (res.get("failed") or [])]

    # ledger BEFORE the functional check: a crash in the check window must still leave the
    # ledger able to tell "we built it" from "the operator built it".
    if placed:
        _record_built(placed)

    union = {tuple(p) for p in v.get("placed", [])} | set(placed)
    v["placed"] = _pairs(sorted(union))
    v["already"] = _pairs(sorted(already))
    v["failed"] = failed
    v["at_tick"] = game_tick()
    v["ts"] = time.time()
    v.pop("refused", None)
    v.pop("stale", None)
    save(plan)

    # functional check: "does ore actually move", not "did create_entity return ok".
    box = {"detail": ""}

    def _check():
        try:
            ok, detail = _norm_check(verify_fn(plan))
        except Exception as e:                # a bug is a diagnostic too, never a pass
            box["detail"] = "%s: %s" % (type(e).__name__, e)
            return False
        box["detail"] = detail
        return ok

    ok = bool(_build_worked(_check, tries, delay))
    v["check"] = {"ok": ok, "detail": box["detail"]}
    if ok:
        # advance the plan's own scan clock and absorb our own cells, so the next batch over
        # this ground is not self-stale (daemon.ts: data.as_of_tick = res.tick).
        absorb(plan)
        plan["scan_tick"] = v["at_tick"]
        plan["status"] = "verified"           # the ONLY path that sets "verified"
        return save(plan)
    plan["status"] = "failed"
    save(plan)
    if rollback_on_fail:
        # Build Law 2: if the result is nothing, remove what you built - in the SAME pass.
        # (rollback_on_fail=False is for callers with a repair path, e.g. plan_mine_geometry:
        # adjust beats revert.)
        v["rollback"] = rollback(plan, remove_fn=kind.get("remove"))
        save(plan)
    return plan


def rollback(plan, remove_fn=None, tiles=None):
    """Remove ONLY what this plan placed (verify.placed, or the `tiles` subset), refunding.
    Registry-scoped, never area-based - the scope IS the record.

    Pairs forget_built with the removal exactly as teardown_lane does: our own teardown must
    never be misread by reconcile_removals as an operator deletion. Tiles we placed but can
    no longer find are counted not_found and deliberately LEFT in the built ledger - if the
    operator removed them, reconcile_removals must still be able to protect them.
    """
    v = _verify_block(plan)
    scope = _xys(tiles) if tiles is not None else _xys(v.get("placed"))
    if not scope:
        return {"removed": 0, "not_found": 0}
    fn = remove_fn or (KINDS.get(plan.get("kind")) or {}).get("remove") or _default_remove
    out = fn(plan, scope)
    if isinstance(out, dict) and "removed_tiles" in out:
        removed = int(out.get("removed", 0))
        not_found = int(out.get("not_found", len(scope) - removed))
        gone = _xys(out["removed_tiles"])     # exact attribution: forget ONLY these
    elif isinstance(out, dict):
        removed = int(out.get("removed", 0))
        not_found = int(out.get("not_found", len(scope) - removed))
        gone = None
    else:                                     # remove_fn may return a bare count
        removed = int(out or 0)
        not_found = len(scope) - removed
        gone = None
    # An EMPTY removed_tiles means "we removed nothing" and must forget nothing - only a
    # remove_fn that reports no tiles at all leaves the whole scope as the best attribution.
    _forget_built(scope if gone is None else gone)
    left = {tuple(p) for p in v.get("placed", [])} - {tuple(t) for t in scope}
    v["placed"] = _pairs(sorted(left))
    save(plan)
    return {"removed": removed, "not_found": not_found}


def supersede(plan, keep=(), reason=""):
    """Retire a plan: tear down the tiles it placed that the replacement does NOT reuse, and
    mark it superseded. `keep` is the new route's tile list - shared tiles stay in the ground
    (upstream's declarative group teardown: anything in the group not matching the new file
    is removed, and only that)."""
    keepset = {_xy(t) for t in keep or ()}
    v = _verify_block(plan)
    ours = _xys(v.get("placed"))
    scope = [t for t in ours if t not in keepset]
    kept = len(ours) - len(scope)
    out = rollback(plan, tiles=scope) if scope else {"removed": 0, "not_found": 0}
    v = _verify_block(plan)                   # rollback rewrote verify.placed
    plan["status"] = "superseded"
    v["superseded"] = {"reason": reason, "kept": kept, "removed": out["removed"],
                       "not_found": out["not_found"], "ts": time.time()}
    return save(plan)


def resume(tries=1, delay=5):
    """Crash recovery: every plan stuck in "applying" is re-verified and either completed or
    rolled back. Call this at controller startup, before anything else builds.

    A rollback is a WRITE, so it defers while the operator is connected: the plan stays
    "applying" and the next resume() picks it up. Verification is read-only and always runs.
    """
    out = []
    present = None
    for plan in plans(status="applying"):
        kind = KINDS.get(plan.get("kind")) or {}
        vfn = kind.get("verify")
        v = _verify_block(plan)
        if vfn is None:
            # Cannot know whether it worked, so do NOT tear it down blind - fail it loudly and
            # leave the litter for a human/planner decision.
            v["check"] = {"ok": False, "detail": "resume: no verifier registered for kind %r "
                                                 "- cannot re-verify a crashed apply"
                                                 % plan.get("kind")}
            plan["status"] = "failed"
            out.append(save(plan))
            continue
        box = {"detail": ""}

        def _check(_v=vfn, _p=plan, _b=box):
            try:
                ok, detail = _norm_check(_v(_p))
            except Exception as e:
                _b["detail"] = "%s: %s" % (type(e).__name__, e)
                return False
            _b["detail"] = detail
            return ok

        ok = bool(_build_worked(_check, tries, delay))
        v["check"] = {"ok": ok, "detail": box["detail"]}
        v["resumed_ts"] = time.time()
        if ok:
            absorb(plan)
            plan["status"] = "verified"
            out.append(save(plan))
            continue
        if present is None:
            present = _operator_present()
        if present:
            v["check"]["detail"] += " | rollback deferred: operator connected"
            out.append(save(plan))            # stays "applying"; next resume() finishes it
            continue
        plan["status"] = "failed"
        save(plan)
        v["rollback"] = rollback(plan, remove_fn=kind.get("remove"))
        out.append(save(plan))
    return out
