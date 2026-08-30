#!/usr/bin/env python3
"""Supply LANES the operator's way: one lane per item per destination, planned before placed.

The measured law this module exists to enforce (OPERATOR-PRINCIPLES lane-spec, derived from
`snapshots/before.json` -> `after.json`, 121 transport-belts + 6 undergrounds deleted):

    Every belt tile must be on a path from a producer's drop_position to a consumer's
    pickup_position - exactly ONE such path per commodity per destination, crossing only by
    underground, buffered only at the terminus.

72.4% of everything the operator deleted (92 of 127 belts) was a DUPLICATE PARALLEL LANE:
three iron rows and two copper rows, each pair source->same sink, because every re-lay left
its predecessor standing (GOTCHAS "ROOT CAUSE of duplicate lanes"). 15.0% more (19 belts) was
an orphan spur with no producer at its head and no consumer at its tail. Those two numbers are
the whole design brief:

  plan_supply()     refuses to plan a SECOND lane for an (item, destination) pair. It returns
                    the lane that already serves it instead of laying a parallel one - the
                    creation-side fix. Routing is belt_router's A*, so crossings are 2-tile
                    undergrounds (all 3 of the operator's pairs measure exactly span 2) and a
                    lane can never be planned through a machine, through another lane, or onto
                    a tile the operator deliberately cleared.
  build()           buildplan.apply with lane_lint.verify_supply as the acceptance test:
                    "connected AND items arriving", not "create_entity returned ok"
                    (BUILD LAW 1). A lane that moves nothing is rolled back in the same pass
                    (BUILD LAW 2) and its registry entry retired, freeing the pair for a
                    different route.
  retire_obsolete() the deletion side. Finds lanes with no consumer (the coal-to-drill fuel
                    lanes after electrify_mines: the burner drills they fed are gone, so no
                    inserter draws from them and nothing consumes) and lanes made redundant by
                    a parallel duplicate, then tears out exactly the tiles WE placed,
                    refunding, through buildplan.supersede.

WHY adopt=False IS THE DEFAULT. belt_router will happily reuse an existing collinear belt of
the same name. For a supply lane that is a MERGE: item A's route would join item B's line and
both commodities would share a transport line, which is the MIXED_ITEMS defect (spec L7:
33/33 tiles of feed_row_y8 and 25/25 of feed_row_y17 are single-item per lane). With
adopt=False every existing belt is a hard tile, so the router does the operator's thing -
crosses UNDER it (spec L10: "lanes cross by underground, never by detour and never by merge";
zero detour tiles on the whole map).

Protected tiles are added to the router's HARD set, not its reserved set: hard tiles are
span-passable, so an underground may tunnel beneath a tile the operator cleared (nothing is
built there and the tile stays empty) but nothing may ever be placed ON it (BUILD LAW 3).

REGISTRY. supply-lanes.json, one record per lane, holding the tiles the lane occupies and the
buildplan id that owns them. Teardown is always registry-scoped: `retire_obsolete` removes
only entities this bot recorded placing, at this plan's own tiles, refunded into the
character's inventory - never an area destroy (bootstrap.teardown_lane's rule, generalised).

RCON: plan_supply is READ-ONLY (belt_router.scan_obstacles). probe_consumers is READ-ONLY.
Only build() and retire_obsolete() write, and both gate on the operator truce first.
No runtime event handler is ever registered.

CLI (all read-only):
    supply_planner.py list [item]
    supply_planner.py plan <item> <fx> <fy> <tx> <ty>     # scans + routes, builds NOTHING
    supply_planner.py obsolete                            # dry-run retire_obsolete
"""
import json
import pathlib
import time

import belt_router
import buildplan
import lane_lint
import rcon
import world

HERE = pathlib.Path(__file__).resolve().parent
LANES_PATH = HERE / "supply-lanes.json"     # tests repoint this at a tmp dir
KIND = "supply_lane"                        # buildplan kind (registered at import)
CHUNK = 3000                                # chars per chunked RCON read slice
STORE = "storage._supply"                   # private read buffer for probe_consumers

ACTIVE = ("planned", "active")              # a lane that still owns its (item, destination)
STATUSES = ("planned", "active", "retired")

# --------------------------------------------------------------------------------------
# MEASURED SPEC NUMBERS (OPERATOR-PRINCIPLES lane-spec; live map tick 1111150 + snapshots).
# Only the entries this module actually enforces are listed - the rest of the lane spec is
# enforced by lane_lint / principles.py and is deliberately NOT duplicated here.
# --------------------------------------------------------------------------------------
LANE_SPEC = {
    # L2 no_parallel_duplicate. "two runs carrying the same item, axis-parallel separation
    # <= 3 tiles over >= 8 consecutive tiles, terminating at the same consumer, are
    # duplicates. Keep exactly ONE." 92/127 deleted belts (72.4%) were this class.
    "dup_sep_max_tiles": 3,
    "dup_overlap_min_tiles": 8,
    # One lane per item per DESTINATION. The operator's two feed rows compose at a single
    # tile each; a second lane into the same destination is a merge, which L12 forbids
    # ("a splitter exists ONLY to fan one supply out to N consumers... never to merge").
    # tol 2 because a destination is usually named as a consumer's pickup tile and callers
    # disagree by a tile about which end of it they mean (verify_supply uses tol=1).
    "dest_tol_tiles": 2,
    # L10 crossings. All three of the operator's underground pairs span exactly 2 (input at
    # t, output at t+2*dirvec, bridging one occupied perpendicular tile) = 40% of the
    # engine's max_underground_distance 5. belt_router re-reads the live prototype value, so
    # 5 here is only the offline fallback.
    "ug_span_max": 5,
    # L1 no_dead_run: every belt must belong to a run with a producer at its head and a
    # consumer at its tail. The operator's own map still carries 15/429 dead belts (3.5%) -
    # occlusion under drill sprites, NOT endorsement - so the planner's threshold is 0.
    "consumers_min": 1,
    # L13 no_midlane_chests: containers only at a terminus. Enforced by refusing to treat a
    # container as a consumer anywhere except the lane's last tile.
    "midlane_containers_allowed": 0,
}
DEST_TOL = LANE_SPEC["dest_tol_tiles"]
DUP_SEP_MAX = LANE_SPEC["dup_sep_max_tiles"]
DUP_OVERLAP_MIN = LANE_SPEC["dup_overlap_min_tiles"]
CONSUMERS_MIN = LANE_SPEC["consumers_min"]

CORRIDOR_PAD = 8            # tiles of slack around the from->to bbox that plan_supply scans
CONSUMER_PAD = 3            # long-handed-inserter reach is 2; 3 keeps its body in the scan

# refusal codes returned by plan_supply
DUPLICATE = "DUPLICATE_LANE"
PROTECTED_ENDPOINT = "PROTECTED_ENDPOINT"
PROTECTED_TILES = "PROTECTED_TILES"
THROUGH_MACHINE = "THROUGH_MACHINE"
THROUGH_LANE = "THROUGH_LANE"
NO_ROUTE = "NO_ROUTE"
EMPTY_ROUTE = "EMPTY_ROUTE"
OPERATOR_PRESENT = "OPERATOR_PRESENT"


# --------------------------------------------------------------------------- bootstrap
# Lazy-import wrappers, exactly buildplan's pattern: cheap and side-effect-free offline, but
# the ledger paths inside bootstrap are hardcoded to the repo dir. Tests monkeypatch THESE.
def _protected():
    import bootstrap
    return bootstrap._protected_load()


def _operator_present():
    import bootstrap
    return bootstrap.operator_present()


# --------------------------------------------------------------------------- helpers
def _xy(t):
    return (int(t[0]), int(t[1]))


def _tset(rec):
    return {_xy(t) for t in (rec or {}).get("tiles") or ()}


def _corridor(a, b, pad):
    return (min(a[0], b[0]) - pad, min(a[1], b[1]) - pad,
            max(a[0], b[0]) + pad, max(a[1], b[1]) + pad)


def _cheb(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


# --------------------------------------------------------------------------- registry
def _load():
    """The lane registry. Never raises - a corrupt or absent file reads as empty, exactly
    like bootstrap._lanes_load, because a registry read must never block a heal."""
    try:
        db = json.loads(pathlib.Path(LANES_PATH).read_text())
    except (OSError, ValueError):
        return {"lanes": []}
    if not isinstance(db, dict) or not isinstance(db.get("lanes"), list):
        return {"lanes": []}
    return db


def _save(db):
    p = pathlib.Path(LANES_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    world.atomic_write(p, db)       # other sessions share this worktree - never a partial file
    return db


def lanes(item=None, status=None, db=None):
    """Registry records, newest first. `status` is a str or a tuple."""
    db = db if db is not None else _load()
    want = (status,) if isinstance(status, str) else status
    out = [r for r in db["lanes"]
           if (item is None or r.get("item") == item)
           and (want is None or r.get("status") in want)]
    return sorted(out, key=lambda r: r.get("created_ts") or 0, reverse=True)


def get_lane(lane_id, db=None):
    db = db if db is not None else _load()
    for r in db["lanes"]:
        if r.get("id") == lane_id:
            return r
    return None


def find_lane(item, to_xy, tol=DEST_TOL, status=ACTIVE, db=None):
    """The lane that ALREADY serves (item, destination), or None.

    Keyed on the destination only, never on the source: two sources feeding one destination
    with the same commodity is a MERGE, and the operator used a splitter to fan out, never to
    merge (spec L12, both live splitters unbiased and unfiltered). So a second source into an
    already-served destination is the duplicate this refuses.
    """
    to_xy = _xy(to_xy)
    best = None
    for r in lanes(item=item, status=status, db=db):
        if _cheb(_xy(r["to"]), to_xy) <= tol:
            if best is None or (r.get("created_ts") or 0) > (best.get("created_ts") or 0):
                best = r
    return best


def _put(rec, db=None):
    db = db if db is not None else _load()
    for i, r in enumerate(db["lanes"]):
        if r.get("id") == rec.get("id"):
            db["lanes"][i] = rec
            break
    else:
        db["lanes"].append(rec)
    _save(db)
    return rec


def _foreign_tiles(exclude=None, db=None):
    """Every tile owned by a live registered lane other than `exclude`. Added to the router's
    hard set so a new route can never be planned ON an existing lane even when the live scan
    did not reach it (a planned-but-unbuilt lane has no entities to scan)."""
    out = set()
    for r in lanes(status=ACTIVE, db=db):
        if exclude is not None and r.get("id") == exclude:
            continue
        out |= _tset(r)
    return out


# --------------------------------------------------------------------------- planning
def _result(ok, code=None, reason="", **kw):
    # EVERY documented key is present on EVERY path, refusals included: a caller reading
    # res["crossings"] to log a plan must not get a KeyError on the one branch that refused.
    # None means "not computed", which is not the same as 0 crossings.
    out = {"ok": bool(ok), "code": code, "reason": reason, "lane": None, "plan": None,
           "route": None, "existing": False, "conflicts": None, "crossings": None}
    out.update(kw)
    return out


def plan_supply(item, from_xy, to_xy, *, kind="belt", name=None, obstacles=None,
                protected=None, scan_tick=None, adopt=False, dest_tol=DEST_TOL,
                reserved=None, goal_dir=None, pad=CORRIDOR_PAD, max_underground=None):
    """PLAN one supply lane `item` from tile `from_xy` to tile `to_xy`. Builds NOTHING.

    Returns a result dict:
        {"ok", "code", "reason", "lane", "plan", "route", "existing", "conflicts",
         "crossings"}
    On a duplicate it returns ok=False, code=DUPLICATE_LANE and `lane` set to the EXISTING
    record (and existing=True) - the caller gets the lane that already serves the pair
    instead of a second one beside it.

    `obstacles` may be passed to plan offline; omitted, the from->to corridor is scanned
    READ-ONLY (belt_router.scan_obstacles). `scan_tick` must come from buildplan.plan_scan
    over the same area - omitted, new_plan takes it for us.
    """
    from_xy, to_xy = _xy(from_xy), _xy(to_xy)
    prot = {_xy(t) for t in protected} if protected is not None else {_xy(t) for t in _protected()}

    # ---- gate 1: ONE LANE PER ITEM PER DESTINATION. The 72.4% rule, on the creation side.
    existing = find_lane(item, to_xy, tol=dest_tol)
    if existing is not None:
        return _result(False, DUPLICATE, existing=True, lane=existing,
                       reason=("%s already reaches (%d,%d) on lane %s (from %s, status %s) - "
                               "a second lane into the same destination is the parallel "
                               "duplicate the operator deleted 92 belts' worth of. Use that "
                               "lane, or retire it first."
                               % (item, to_xy[0], to_xy[1], existing["id"],
                                  tuple(existing["from"]), existing["status"])))

    # ---- gate 2: operator-protected endpoints. He cleared these on purpose (BUILD LAW 3);
    # an endpoint cannot be routed around, so this is a refusal, not a detour.
    bad_ends = [t for t in (from_xy, to_xy) if t in prot]
    if bad_ends:
        return _result(False, PROTECTED_ENDPOINT,
                       reason=("endpoint(s) %s are operator-protected tiles - the operator "
                               "deleted them deliberately and they are never rebuilt "
                               "(BUILD LAW 3). Pick a different endpoint." % (bad_ends,)))

    # ---- routing. Protected tiles and foreign lane tiles join HARD (span-passable: an
    # underground may tunnel beneath them, nothing may be BUILT on them).
    if obstacles is None:
        obstacles = belt_router.scan_obstacles(*_corridor(from_xy, to_xy, pad))
    foreign = _foreign_tiles()
    obs = belt_router.Obstacles(
        hard=set(obstacles.hard) | prot | foreign,
        reserved=set(obstacles.reserved),
        belts=dict(obstacles.belts),
        bounds=obstacles.bounds,
        under_max=dict(obstacles.under_max))
    route = belt_router.plan_route(from_xy, to_xy, kind=kind, obstacles=obs, reserved=reserved,
                                   adopt=adopt, name=name, goal_dir=goal_dir,
                                   max_underground=max_underground)
    if route is None:
        return _result(False, NO_ROUTE,
                       reason="no legal route %s -> %s: %s" % (from_xy, to_xy,
                                                               belt_router.LAST_ERROR))

    # ---- post-checks. Belt-and-braces: the router already forbids all three, so a hit here
    # is a router bug or a stale obstacle set - either way nothing gets laid.
    ent = belt_router.plan_tiles(route)
    adopted = {(s["x"], s["y"]) for s in route if s.get("adopt")}
    conflicts = belt_router.plan_conflicts(route, prot)
    if conflicts["count"]:
        return _result(False, PROTECTED_TILES, conflicts=conflicts,
                       reason=("%d of %d routed tiles are operator-protected %s - not laying "
                               "it (BUILD LAW 3)."
                               % (conflicts["count"], len(ent), conflicts["tiles"][:6])))
    hit_machine = [t for t in ent if t in obstacles.hard]
    if hit_machine:
        return _result(False, THROUGH_MACHINE,
                       reason=("route would place on %d occupied tile(s) %s - a lane never "
                               "runs through a machine." % (len(hit_machine), hit_machine[:6])))
    hit_lane = [t for t in ent
                if t in foreign or (t in obstacles.belts and t not in adopted)]
    if hit_lane:
        return _result(False, THROUGH_LANE,
                       reason=("route would place on %d existing lane tile(s) %s - lanes cross "
                               "by UNDERGROUND, never by merge (spec L10)."
                               % (len(hit_lane), hit_lane[:6])))

    tiles = [(s["x"], s["y"], s["dir"]) for s in route if not s.get("adopt")]
    if not tiles:
        return _result(False, EMPTY_ROUTE, route=route,
                       reason=("every tile of this route is already an adoptable belt - there "
                               "is nothing to build; trace the existing line instead."))

    names = sorted({s["entity"] for s in route if not s.get("adopt")})
    crossings = sum(1 for s in route if s.get("type") == "input")
    plan = buildplan.new_plan(
        KIND,
        {"item": item, "from": list(from_xy), "to": list(to_xy), "kind": kind,
         "route": route, "crossings": crossings},
        tiles, scan_tick=scan_tick, names=names)
    rec = {
        "id": plan["id"], "plan_id": plan["id"], "item": item,
        "from": list(from_xy), "to": list(to_xy), "kind": kind, "names": names,
        "tiles": [[x, y] for (x, y) in ent],     # ALL entity tiles, adopted ones included
        "crossings": crossings, "status": "planned", "reason": "",
        "created_ts": time.time(), "retired_ts": None, "consumers": None,
    }
    _put(rec)
    return _result(True, lane=rec, plan=plan, route=route, conflicts=conflicts,
                   crossings=crossings,
                   reason="%d tiles, %d underground crossing(s)" % (len(tiles), crossings))


# --------------------------------------------------------------------------- build
def place_lane(plan, tiles):
    """buildplan place_fn. Emits belt_router.plan_to_lua for exactly the missing tiles, then
    RE-PROBES to find out what actually landed - `built/total` is an aggregate and cannot
    attribute a failure to a tile, and a build that reports success per-tile it never checked
    is the failure mode BUILD LAW 1 exists to stop. RCON WRITE (the only one here)."""
    want = {_xy(t) for t in tiles}
    if not want:
        return {"placed": [], "already": [], "failed": []}
    route = (plan.get("args") or {}).get("route") or []
    steps = [s for s in route if (s["x"], s["y"]) in want and not s.get("adopt")]
    out = []
    for cmd in belt_router.plan_to_lua(steps):
        out.append(rcon.run(cmd).strip())
    got = {_xy(t) for t in buildplan.probe(plan, sorted(want))}
    placed = sorted(want & got)
    failed = [{"tile": list(t),
               "reason": "not placed (tile occupied, or no belt in inventory) [%s]"
                         % ("; ".join(out) or "no command")}
              for t in sorted(want - got)]
    return {"placed": placed, "already": [], "failed": failed}


def verify_lane(plan, settle=3.0, tol=1):
    """buildplan verify_fn: lane_lint.verify_supply - "connected AND items arriving".

    NOT "did create_entity return ok" (BUILD LAW 1), and not "a BFS got within 6 tiles"
    either: `connected` means the destination is genuinely ON the traced run, and `moving`
    means a second sample of the tail after `settle` shows the items advanced - so a
    backed-up but ARRIVING lane passes while a full-but-frozen or empty tail fails.
    """
    a = plan.get("args") or {}
    if a.get("kind", "belt") != "belt":
        # verify_supply floods the BELT graph (lane_lint.BELT_TYPES). A pipe run is invisible
        # to it and would read "no belt at (x,y)" -> not connected -> every attempt fails.
        # Say so, instead of reporting a routing failure that never happened.
        return {"ok": False,
                "detail": "kind=%r has no functional verifier here: lane_lint.verify_supply "
                          "traces belts only" % a.get("kind")}
    r = lane_lint.verify_supply(a["item"], _xy(a["from"]), _xy(a["to"]), settle=settle, tol=tol)
    ok = bool(r.get("connected") and r.get("moving"))
    sev1 = [f["code"] for f in (r.get("findings") or ()) if f.get("sev") == 1]
    detail = ("%s: connected=%s moving=%s arrived=%d path=%d%s"
              % (a["item"], r.get("connected"), r.get("moving"), int(r.get("arrived") or 0),
                 int(r.get("path_len") or 0),
                 (" findings=" + ",".join(sorted(set(sev1)))) if sev1 else ""))
    return {"ok": ok, "detail": detail}


def build(plan, *, tries=6, delay=5, place_fn=None, verify_fn=None, probe_fn=None,
          force=False):
    """Apply a planned lane through buildplan (truce -> staleness -> protected -> place ->
    verify -> rollback). Accepts a plan_supply() result, a buildplan record, or a plan id.

    On a verified build the registry entry goes `active`. On a failed one, apply() has
    already rolled the lane back out of the ground in the same pass (BUILD LAW 2), so the
    entry goes `retired` with the verifier's reason - which FREES the (item, destination)
    pair, letting the caller plan a different route instead of re-laying the dead one.
    """
    bp = _as_plan(plan)
    # BUILD LAW 1 is "never build what cannot be verified", not "build, then find out".
    # buildplan gate 0b refuses UP FRONT when verify_fn is None; this is the same refusal one
    # level up, for a kind whose only available verifier structurally cannot pass. Without it
    # a kind="pipe" lane is laid, fails `tries` verification rounds, and is rolled straight
    # back out of the ground - a build/teardown cycle whose outcome was knowable beforehand.
    _kind = (bp.get("args") or {}).get("kind", "belt")
    if verify_fn is None and _kind != "belt":
        raise ValueError(
            "build(): kind %r has no functional verifier - lane_lint.verify_supply traces "
            "BELTS, so every attempt would fail and apply() would lay this route and tear it "
            "straight back out. Pass verify_fn= with a real check for %r (a fluid run needs a "
            "fluidbox read, not a belt trace)." % (_kind, _kind))
    out = buildplan.apply(bp, place_fn=place_fn or place_lane,
                          verify_fn=verify_fn or verify_lane, probe_fn=probe_fn,
                          tries=tries, delay=delay, rollback_on_fail=True, force=force)
    rec = get_lane(out["id"])
    if rec is None:
        return out
    v = out.get("verify") or {}
    if out.get("status") == "verified":
        rec["status"] = "active"
        rec["reason"] = (v.get("check") or {}).get("detail", "")
        rec["retired_ts"] = None
    elif out.get("status") in ("failed", "superseded"):
        rec["status"] = "retired"
        rec["retired_ts"] = time.time()
        rec["reason"] = (v.get("refused")
                         or (v.get("check") or {}).get("detail")
                         or "build did not verify")
    _put(rec)
    return out


def _as_plan(plan):
    """A buildplan record from any of the four things a caller may hold. ORDER MATTERS: a
    REGISTRY record also carries "status" and "tiles", so it must be matched on its own
    "plan_id" key before the buildplan shape is tried, or build() would hand buildplan.apply
    a registry record whose `tiles` carry no direction and whose `args` do not exist."""
    if isinstance(plan, str):
        return buildplan.load(plan)                       # a plan id
    if not isinstance(plan, dict):
        raise TypeError("build(): expected a plan_supply result, a buildplan record or an id")
    if "ok" in plan and "code" in plan:                   # a plan_supply() result
        if not plan.get("ok") or plan.get("plan") is None:
            raise ValueError("build(): this plan_supply result was REFUSED (%s): %s"
                             % (plan.get("code"), plan.get("reason")))
        return plan["plan"]
    if plan.get("plan_id"):                               # a registry record
        return buildplan.load(plan["plan_id"])
    if "scan_tick" in plan and "verify" in plan:          # a buildplan record
        return plan
    raise TypeError("build(): expected a plan_supply result, a buildplan record or an id")


# --------------------------------------------------------------------------- consumers
def _chunked(build_lua):
    """rcon.read_chunked on a PRIVATE buffer key. `build_lua(store)` returns the /sc.

    RAISES on a read that did not happen - and read_chunked keeps that contract: a Lua runtime
    error comes back as prose, and int(prose) swallowed would read as "this lane has no
    consumers" and become a delete order for a working lane. A failed read must be
    indistinguishable from no read at all, never from an answer. The reassembled length is
    checked too, so a short or spliced payload cannot pass as an empty one either.
    """
    try:
        return rcon.read_chunked(build_lua, chunk=CHUNK)
    except rcon.ChunkedReadError as e:
        raise RuntimeError("consumer probe FAILED: %s" % str(e)[:220])


def consumer_lua(x1, y1, x2, y2, store=STORE):
    """READ-ONLY: every tile in the box from which something DRAWS off a belt.

    An inserter's pickup_position is the authority, never its facing - the operator's own
    inserters fill the FAR lane and drills the NEAR one, and inferring the tile from which
    side the entity sits on is exactly the mistake that put six copper drills' output on bare
    ground (spec L8: "Never infer this from which side the entity sits - read
    drop_position"). Loaders draw from the tile they stand on.

    The empty case is written out literally rather than left to helpers.table_to_json: Lua
    cannot tell an empty ARRAY from an empty OBJECT, so an empty `o` serialises to `{"c":{}}`
    and the caller's list parse silently sees nothing. world.scan_area uses the same guard.
    """
    box = (int(x1), int(y1), int(x2), int(y2))
    return ("/sc local s=game.surfaces[1]; local o={};"
            "for _,e in pairs(s.find_entities_filtered{area={{%d,%d},{%d,%d}},"
            "type='inserter'}) do local p=e.pickup_position;"
            " o[#o+1]=math.floor(p.x)..','..math.floor(p.y) end;" % box +
            "for _,e in pairs(s.find_entities_filtered{area={{%d,%d},{%d,%d}},"
            "type={'loader','loader-1x1'}}) do"
            " o[#o+1]=math.floor(e.position.x)..','..math.floor(e.position.y) end;" % box +
            "if #o==0 then " + store + "='{\"c\":[]}' else "
            + store + "=helpers.table_to_json{c=o} end;"
            "rcon.print(#" + store + ")")


def probe_consumers(rec, pad=CONSUMER_PAD):
    """How many of this lane's tiles a consumer actually draws from. RCON READ ONLY.

    This is the mechanical test behind spec L1 (no dead run) and the one that identifies a
    coal-to-drill fuel lane after electrify_mines: the burner drills it fed are gone, so no
    inserter's pickup_position lands on it and the count is 0 - while the belts themselves
    still read perfectly healthy.

    A 0 RETURNED FROM HERE IS A DELETE ORDER (retire_obsolete tears the lane out), so it is
    only ever returned for a read that SUCCEEDED and found nothing. Every way the read can
    fail - a Lua error, a truncated or non-JSON payload, a payload without the expected shape
    - RAISES instead, and retire_obsolete's handler then counts the lane as supplied. Getting
    this backwards is how a flaky RCON read deletes a working lane.
    """
    tiles = _tset(rec)
    if not tiles:
        return 0
    xs = [t[0] for t in tiles]
    ys = [t[1] for t in tiles]
    body = _chunked(lambda store: consumer_lua(min(xs) - pad, min(ys) - pad,
                                               max(xs) + pad, max(ys) + pad, store=store))
    try:
        raw = json.loads(body)
    except ValueError:
        raise RuntimeError("consumer probe FAILED: unparseable payload (%d chars) %r - a "
                           "truncated read is not an empty answer" % (len(body), body[:160]))
    if not isinstance(raw, dict) or not isinstance(raw.get("c"), list):
        raise RuntimeError("consumer probe FAILED: payload has no consumer list (%r)"
                           % (body[:160],))
    claimed = set()
    for s in raw.get("c") or ():
        try:
            a, b = str(s).split(",")
            claimed.add((int(a), int(b)))
        except ValueError:
            continue
    return len(tiles & claimed)


def _downstream_feeds(live):
    """lane -> 1 when its tail touches another live lane. A trunk whose only "consumer" is
    the feed row it hands off to has no inserter of its own and would otherwise read dead:
    the operator's L1_copper_trunk (111 belts) ends by feeding feed_row_y17, not a machine."""
    out = {}
    sets = {r["id"]: _tset(r) for r in live}
    for r in live:
        tiles = [_xy(t) for t in r.get("tiles") or ()]
        out[r["id"]] = 0
        if not tiles:
            continue
        tail = tiles[-1]
        for other in live:
            if other["id"] == r["id"]:
                continue
            if any(_cheb(tail, t) <= 1 for t in sets[other["id"]]):
                out[r["id"]] = 1
                break
    return out


def _axis_index(tiles):
    rows, cols = {}, {}
    for (x, y) in tiles:
        rows.setdefault(y, set()).add(x)
        cols.setdefault(x, set()).add(y)
    return rows, cols


def _longest_run(vals):
    """Longest CONSECUTIVE stretch in a set of coordinates. The spec says "over >= 8
    consecutive tiles", not "8 tiles somewhere in common": two lanes that clip the same row
    at eight scattered points are two lanes crossing a corridor, not one lane twice."""
    best = run = 0
    prev = None
    for v in sorted(vals):
        run = run + 1 if prev is not None and v == prev + 1 else 1
        prev = v
        best = max(best, run)
    return best


def parallel_duplicates(live, sep_max=DUP_SEP_MAX, overlap_min=DUP_OVERLAP_MIN,
                        dest_tol=DEST_TOL):
    """Pairs of live lanes for the SAME item, into the SAME destination, that run
    axis-parallel within `sep_max` tiles over at least `overlap_min` CONSECUTIVE collinear
    tiles -> [(a_id, b_id, offset, overlap)].

    This is spec L2, the operator's single biggest deletion class (92/127 belts, 72.4%):
    `y=-39.5` and `y=-41.5` alongside the kept `y=-40.5`, both merging at the same tile.
    principles.one_lane_per_item_per_destination uses `0 < off <= 3` on live belt runs;
    here offset 0 is included too, because two REGISTERED lanes sharing 8+ tiles of one row
    is the same lane recorded twice and is just as duplicate.

    THE DESTINATION CLAUSE IS LOAD-BEARING, and it is the difference between this detector
    and principles.py's. The measured law is "two runs carrying the same item ... TERMINATING
    AT THE SAME CONSUMER are duplicates" - principles.py drops that clause safely because it
    only ever emits a `warn` for a human to read, while this one is wired to
    buildplan.supersede and actually tears belts out of the ground. Without it, two perfectly
    healthy same-item lanes to consumers 40 tiles apart, sharing nothing but a corridor,
    read as duplicates and one gets deleted. `dest_tol` is DEST_TOL on purpose: the same
    tolerance plan_supply refuses a duplicate at, so the creation gate and the deletion gate
    agree on what "the same destination" means. A pair plan_supply was willing to CREATE is
    never a pair retire_obsolete will destroy.
    """
    out = []
    by_item = {}
    for r in live:
        by_item.setdefault(r.get("item"), []).append(r)
    for group in by_item.values():
        idx = {r["id"]: _axis_index(_tset(r)) for r in group}
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                ta, tb = a.get("to"), b.get("to")
                if ta is None or tb is None or _cheb(_xy(ta), _xy(tb)) > dest_tol:
                    continue     # different consumers, or unknown: never a delete order
                best = None
                for ai, bi in ((0, 0), (1, 1)):          # 0 = rows (horizontal), 1 = cols
                    A, B = idx[a["id"]][ai], idx[b["id"]][bi]
                    for ka, va in A.items():
                        for kb, vb in B.items():
                            off = abs(ka - kb)
                            if off > sep_max:
                                continue
                            ov = _longest_run(va & vb)
                            if ov >= overlap_min and (best is None or ov > best[1]):
                                best = (off, ov)
                if best:
                    out.append((a["id"], b["id"], best[0], best[1]))
    return out


# --------------------------------------------------------------------------- retirement
def _rank(rec, counts):
    """Which of two duplicates survives. The operator's rule (spec L2 keep_which) is "the run
    whose tiles receive a producer's drop_position"; the registry's proxy for that is the run
    something actually draws from, then the one that verified, then the newer record."""
    return (counts.get(rec["id"], 0), 1 if rec.get("status") == "active" else 0,
            rec.get("created_ts") or 0)


def retire_obsolete(*, consumers_fn=None, dry_run=False, sep_max=DUP_SEP_MAX,
                    overlap_min=DUP_OVERLAP_MIN, min_consumers=CONSUMERS_MIN,
                    dest_tol=DEST_TOL):
    """Find and remove lanes that no longer do anything. Returns a list of
    {"id","item","reason","removed","not_found","consumers","dry_run"}.

    Two rules, both measured:
      REDUNDANT   a parallel duplicate of another live lane for the same item INTO THE SAME
                  destination (spec L2 - see parallel_duplicates on why the destination
                  clause cannot be dropped on the deletion side).
                  The loser is whichever of the pair has fewer consumers, then whichever
                  never verified, then whichever is older.
      NO CONSUMER nothing draws from any of its tiles and its tail feeds no other live lane
                  (spec L1). This is the coal-to-drill fuel lane after electrification: the
                  burner drills are gone, the belts are intact, and the lane is dead weight.
                  Applied to ACTIVE lanes only - a lane still in "planned" has not been built
                  yet, so of course nothing draws from it, and retiring it here would
                  supersede a plan the caller is about to apply (a superseded plan is refused
                  forever). The redundancy rule DOES cover planned lanes: a duplicate should
                  die before it is laid, not after.

    Teardown is buildplan.supersede -> rollback -> the registry-scoped refunding remover:
    ONLY entities matching this plan's own names at this plan's own tiles are destroyed and
    their contents go back into the character's inventory. A tile we can no longer find stays
    in the built ledger so reconcile_removals can still protect it.

    RCON WRITE - so it gates on the truce first (BUILD LAW 6: zero construction, and zero
    teardown, while a human is connected). dry_run=True never writes and never checks.
    """
    consumers_fn = consumers_fn or probe_consumers
    db = _load()
    live = lanes(status=ACTIVE, db=db)
    if not live:
        return []
    if not dry_run and _operator_present():
        return [{"id": None, "item": None, "reason": "OPERATOR PRESENT: a human is connected; "
                                                     "no teardown until he logs off (truce).",
                 "removed": 0, "not_found": 0, "consumers": None, "dry_run": False}]

    counts = {}
    for r in live:
        try:
            counts[r["id"]] = int(consumers_fn(r) or 0)
        except Exception:
            # A probe failure is NEVER a delete order: an unreadable lane counts as supplied,
            # so a flaky RCON read can never tear out a working lane.
            counts[r["id"]] = min_consumers
    feeds = _downstream_feeds(live)

    obsolete = {}
    for a_id, b_id, off, ov in parallel_duplicates(live, sep_max, overlap_min, dest_tol):
        a = get_lane(a_id, db=db)
        b = get_lane(b_id, db=db)
        keep, drop = (a, b) if _rank(a, counts) >= _rank(b, counts) else (b, a)
        obsolete.setdefault(drop["id"], (
            "redundant: parallel duplicate of lane %s (%s, offset %d tiles, %d tiles of "
            "shared span) - one lane per item per destination"
            % (keep["id"], keep["item"], off, ov)))
    for r in live:
        if r["id"] in obsolete or r.get("status") != "active":
            continue
        if counts.get(r["id"], 0) < min_consumers and not feeds.get(r["id"]):
            obsolete[r["id"]] = (
                "no consumer: nothing draws from any of its %d tiles and its tail feeds no "
                "other lane - every belt tile must lie on a producer->consumer path"
                % len(_tset(r)))

    out = []
    for rid, reason in sorted(obsolete.items()):
        rec = get_lane(rid, db=db)
        row = {"id": rid, "item": rec.get("item"), "reason": reason, "removed": 0,
               "not_found": 0, "consumers": counts.get(rid), "dry_run": bool(dry_run)}
        if dry_run:
            out.append(row)
            continue
        try:
            bp = buildplan.load(rec.get("plan_id") or rid)
        except (KeyError, OSError, ValueError):
            # KeyError = no such plan; OSError/ValueError = the file is there but unreadable
            # or corrupt (json.JSONDecodeError IS a ValueError). Either way this lane has no
            # recoverable teardown scope - retire the RECORD and leave the ground alone,
            # rather than letting one bad file abort a pass that has already torn out others.
            bp = None
        if bp is not None:
            bp = buildplan.supersede(bp, keep=(), reason=reason)
            sup = (bp.get("verify") or {}).get("superseded") or {}
            row["removed"] = int(sup.get("removed", 0))
            row["not_found"] = int(sup.get("not_found", 0))
        rec["status"] = "retired"
        rec["reason"] = reason
        rec["retired_ts"] = time.time()
        rec["consumers"] = counts.get(rid)
        _put(rec, db=_load())
        out.append(row)
    return out


# --------------------------------------------------------------------------- wiring
def _register_kind():
    """Register the kind so buildplan.resume() can re-verify a crashed lane build with no
    caller context. remove=None deliberately: the default refunding, registry-scoped remover
    is exactly right for belts."""
    return buildplan.register(KIND, place=place_lane, verify=verify_lane, remove=None)


_register_kind()


# --------------------------------------------------------------------------- cli
def _main(argv):
    if len(argv) < 2:
        print(__doc__.rsplit("CLI", 1)[-1].strip())
        return 2
    cmd = argv[1]
    if cmd == "list":
        item = argv[2] if len(argv) > 2 else None
        for r in lanes(item=item):
            print("%-14s %-16s %s -> %s  %3d tiles  %d ug  %s"
                  % (r["status"], r["item"], tuple(r["from"]), tuple(r["to"]),
                     len(r.get("tiles") or ()), r.get("crossings") or 0, r["id"]))
        return 0
    if cmd == "plan" and len(argv) >= 7:
        res = plan_supply(argv[2], (int(argv[3]), int(argv[4])),
                          (int(argv[5]), int(argv[6])))
        print(json.dumps({k: v for k, v in res.items() if k not in ("plan", "route")}, indent=1))
        if res["ok"]:
            print("%d step(s) planned, NOTHING built" % len(res["route"]))
        return 0 if res["ok"] else 1
    if cmd == "obsolete":
        for row in retire_obsolete(dry_run=True):
            print("%s  %-14s %s" % (row["id"], row["item"], row["reason"]))
        return 0
    print(__doc__.rsplit("CLI", 1)[-1].strip())
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv))
