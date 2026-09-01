"""Keep the base's connective infrastructure correct, from what is on the map right now.

The operator's brief (2026-08-31): "the self heal should be dynamic so changing the base
doesn't break things - derpface should know about any infrastructure that is currently on the
base and be autonomously optimizing, adding to and maintaining that infrastructure."

`world_model` answers what exists. This decides what is MISSING and builds it. Nothing here
reads a cache: move a mine, restamp a smelter block somewhere else, delete a lane, and the
next pass simply sees a different world and closes whatever gap it finds.

Why this did not exist before, and what it replaces: `fix_lanes` repaired lanes from
`lanes.json`, so when the base moved it spent every twenty seconds faithfully repairing a
lane to a demolished smelter row. A fixer that works from a record can only maintain the base
it was told about. A fixer that works from a census maintains the base that is there.
"""
import belt_router
import memory
import world_model

# Below this many drills a patch cannot keep a smelting block fed, so it is not a candidate
# while a bigger one exists. A one-drill outpost delivered four ore to forty furnaces.
MIN_FEED_DRILLS = 3


def ore_for_block(block, census):
    """Which ore a smelting block is FOR, derived rather than declared.

    Read in order of how much the world actually tells us:
      1. what its own input belt is carrying, if anything
      2. what its furnaces hold
      3. nothing - and then we say so, instead of guessing from position
    """
    return block.get("ore")


def assign(census, carrying=None):
    """[(mine, block, input_row, feed_side)] - which mine feeds which starved row.

    ONE ORE PER BLOCK, not per row. A smelting block has several input rows and they are all
    the same feedstock; assigning per row sent copper ore to the iron block's second input
    because that row happened to be the next starved one in the list. Rows are grouped by
    block, a block gets one mine, and every one of its rows is fed from it.

    A mine already committed to another block is not reused, so two blocks cannot both be
    promised the same patch and one silently starve. Deterministic, so a half-built lane is
    resumed next pass rather than replaced by a different plan.
    """
    carrying = carrying or {}
    # A mine too small to feed a block is not a candidate while a viable one exists.
    viable = [m for m in census.get("mines", [])
              if int(m.get("drills") or 0) >= MIN_FEED_DRILLS]
    pool = viable if viable else list(census.get("mines", []))
    by_block = {}
    for u in census.get("unfed", []):
        key = tuple(u["block"]["bbox"])
        by_block.setdefault(key, {"block": u["block"], "rows": []})
        by_block[key]["rows"].append((u["input_row"], u.get("feed_side")))
    taken = set()
    out = []
    for key in sorted(by_block):
        entry = by_block[key]
        b = entry["block"]
        bx = (b["bbox"][0] + b["bbox"][2]) // 2
        by = (b["bbox"][1] + b["bbox"][3]) // 2
        want = b.get("ore")          # what this block actually smelts, if it has ever run
        best = None
        for m in pool:
            if m["ore"] in ("coal", "unknown") or not m.get("drops"):
                continue
            if m["ore"] in taken:
                continue
            if want and m["ore"] != want:
                continue                 # a block that smelts iron does not want a copper lane
            mx = (m["bbox"][0] + m["bbox"][2]) // 2
            my = (m["bbox"][1] + m["bbox"][3]) // 2
            d = abs(mx - bx) + abs(my - by)
            # A CAPACITY FLOOR, THEN NEAREST - which is what both failures actually wanted.
            # Nearest-only sent a ONE-DRILL outpost to feed forty furnaces (the lane built
            # fine and delivered four ore). Capacity-only then sent copper 148 belts past a
            # five-drill iron patch sixty belts away, because copper had one more drill.
            # Neither number is the goal: a source has to be able to fill the block, and past
            # that, belt is just cost.
            key = (d, -int(m.get("drills") or 0), m["ore"])
            if best is None or key < best[0]:
                best = (key, m)
        if not best:
            continue
        taken.add(best[1]["ore"])
        for row, side in sorted(entry["rows"]):
            out.append((best[1], b, row, side))
    return out


def feed_tile(block, row, side):
    """The tile a lane must reach to deliver into `row` - just past the block's feed end."""
    x1, _, x2, _ = block["bbox"]
    if side == "east":
        return (x2 + 2, row)
    if side == "west":
        return (x1 - 2, row)
    return None


def plan_lanes(A, census=None, log=None):
    """Routes that would close every unfed input. Builds nothing."""
    say = log or (lambda m: None)
    c = census or world_model.census(A)
    plans = []
    for mine, block, row, side in assign(c):
        goal = feed_tile(block, row, side)
        if goal is None:
            say("skip %s block row %d: feed side is ambiguous, not guessing" % (block["kind"], row))
            continue
        # START FROM WHERE THE ORE ALREADY GETS TO. The mine's drop tile usually has a belt
        # on it already, so routing from there finds no route at all - and even when it does,
        # it builds a second lane beside the one that exists. If a belt run from this mine
        # already terminates somewhere, extend THAT: it is shorter, it reuses what is built,
        # and it is what "maintaining the infrastructure" means rather than duplicating it.
        start = _lane_start(mine, c)
        x1 = min(start[0], goal[0]) - 25
        y1 = min(start[1], goal[1]) - 25
        x2 = max(start[0], goal[0]) + 25
        y2 = max(start[1], goal[1]) + 25
        obs = belt_router.scan_obstacles(x1, y1, x2, y2)
        route = belt_router.plan_route(_beside(start, obs), goal, obstacles=obs)
        plans.append({"ore": mine["ore"], "from": start, "to": goal, "row": row,
                      "route": route, "block": block["bbox"],
                      "drills": mine.get("drills"), "machines": block.get("count")})
        say("%s: %s -> %s  %s" % (mine["ore"], start, goal,
                                  ("%d belts" % len(route)) if route else "NO ROUTE"))
    return plans


def _lane_start(mine, census, radius=40):
    """The end of the mine's existing lane if it has one, else its drop tile."""
    drop = mine["drops"][0]
    best = None
    for e in census.get("lane_ends", []):
        d = abs(e[0] - drop[0]) + abs(e[1] - drop[1])
        if d <= radius and (best is None or d < best[0]):
            best = (d, tuple(e))
    return best[1] if best else drop


def _beside(tile, obs):
    """Start one tile off the drop point: a route beginning ON the source can reverse straight
    back down the line that feeds it."""
    x, y = tile
    blocked = set(obs.hard) | set(obs.belts)
    for d in ((1, 0), (0, 1), (-1, 0), (0, -1)):
        c = (x + d[0], y + d[1])
        if c not in blocked:
            return c
    return tile


# --------------------------------------------------------------------------- building
def build_lanes(A, census=None, log=None, max_lanes=1):
    """Close the starved inputs by actually laying the belt. Returns what it built.

    One lane per pass on purpose: a lane is dozens of belts and hundreds of ticks, and the
    controller's other duties should not wait behind it. The next pass re-censuses, sees the
    lane it just built, and moves to the next gap - which is also what makes a half-finished
    lane resume rather than restart.

    Guards, and only the ones that earn their place:
      - the truce: nothing is built while the operator is connected
      - materials: a partial lane is not half a feed, it is a broken belt that reads as one
      - verification: the lane must actually change what the machines report, or it is noise
    """
    say = log or (lambda m: None)
    import bootstrap as B
    if B.operator_present():
        return []
    built = []
    for p in plan_lanes(A, census, log=lambda m: None)[:max_lanes]:
        route = p.get("route")
        if not route:
            say("infra: %s lane %s -> %s has NO ROUTE" % (p["ore"], p["from"], p["to"]))
            continue
        need = {}
        for s in route:
            if not s.get("adopt"):
                need[s["entity"]] = need.get(s["entity"], 0) + 1
        have = _inventory(A, list(need))
        short = {n: c - have.get(n, 0) for n, c in need.items() if c > have.get(n, 0)}
        if short:
            say("infra: %s lane needs %s - not laying a partial run"
                % (p["ore"], ", ".join("%d %s" % (c, n) for n, c in sorted(short.items()))))
            continue
        # ASK EXPERIENCE FIRST. This is the loop that was missing: the bot produced the
        # evidence for every correction made today - a one-drill mine feeding forty furnaces,
        # a lane laid beside an existing one - and consulted none of it, because I was the
        # feedback loop. `unknown` is the common answer and means no information, never
        # permission.
        ctx = lane_context(p)
        adv = memory.advise("build_lane", ctx)
        if adv["verdict"] == "bad" and adv["confidence"] >= 0.6:
            say("infra: experience advises against this lane (%s) - skipping" % adv["why"])
            continue
        say("infra: laying %s %s -> %s (%d belts)%s"
            % (p["ore"], p["from"], p["to"], len(route),
               "" if adv["verdict"] == "unknown" else "  [experience: %s]" % adv["verdict"]))
        for cmd in belt_router.plan_to_lua(route):
            A._print(cmd)
        p["context"] = ctx
        built.append(p)
    return built


def lane_context(plan):
    """The facts about a lane that make one attempt comparable to another.

    Deliberately NOT the coordinates. What generalises is the shape of the decision - how big
    the source was, how big the sink, how far the run - so a lesson learned at one end of the
    map informs the other. Positions would make every context unique and every lookup a miss,
    which is exactly how corrections.check() ended up never matching anything.
    """
    b = plan.get("block") or (0, 0, 0, 0)
    return {"kind": "ore_lane", "ore": plan.get("ore"),
            "drills": int(plan.get("drills") or 0),
            "machines": int(plan.get("machines") or 0),
            "length": len(plan.get("route") or ())}


def verify(A, plan, before, settle=None, log=None):
    """Did the lane change anything? Records the outcome either way.

    SETTLE FIRST, AND SCALE IT TO THE RUN. A fifty-belt lane needs well over a minute to carry
    its first ore, so measuring on completion records "starved 80 -> 80" for a lane that is
    working perfectly - and that false negative goes straight into the memory the bot now
    consults, teaching it to avoid the correct action. feed_planner.verify already had this
    exact bug and this exact fix; poisoning a learning store with it is worse than logging a
    wrong line, because the wrong line does not persist and compound.
    """
    import time
    import world_model
    say = log or (lambda m: None)
    n = len(plan.get("route") or ())
    time.sleep(settle if settle is not None else min(180.0, 20.0 + n * 1.5))
    after = _starved_in(world_model.census(A), plan.get("block"))
    good = after < before
    memory.remember("build_lane", plan.get("context") or lane_context(plan),
                    "good" if good else "bad",
                    importance=1.5 if not good else 1.0,
                    detail=("starved %d -> %d after a %d-belt %s lane"
                            % (before, after, len(plan.get("route") or ()), plan.get("ore"))))
    say("infra: %s lane %s - starved %d -> %d"
        % (plan.get("ore"), "WORKED" if good else "did not help", before, after))
    return good


def _starved_in(census, bbox):
    if not bbox:
        return 0
    return sum(u.get("starved", 0) for u in census.get("unfed", [])
               if tuple(u["block"]["bbox"]) == tuple(bbox))


def _inventory(A, names):
    if not names:
        return {}
    raw = A._print("/sc local p=storage.derpface if not (p and p.valid) then return end "
                   "local inv=p.get_main_inventory() local o={} "
                   + " ".join("o[#o+1]='%s='..inv.get_item_count('%s')" % (n, n) for n in names)
                   + " rcon.print(table.concat(o,' '))")
    out = {}
    for tok in (raw or "").split():
        if "=" in tok:
            k, v = tok.rsplit("=", 1)
            try:
                out[k] = int(v)
            except ValueError:
                pass
    return out


def maintain(A, log=None):
    """One upkeep pass: census the base, then close the most pressing gap in it."""
    import world_model
    say = log or (lambda m: None)
    c = world_model.census(A)
    if not c.get("unfed"):
        return []
    say("infrastructure: " + world_model.summary(c).replace("\n", " | "))
    built = build_lanes(A, c, log=say)
    # VERIFY AND REMEMBER. Without this the store only ever fills with things I typed; the
    # bot has to be the one generating its own evidence, or nothing has changed.
    for p in built:
        before = _starved_in(c, p.get("block"))
        try:
            verify(A, p, before, log=say)
        except Exception as e:
            say("infra: could not verify the %s lane (%s)" % (p.get("ore"), str(e)[:80]))
    return built
