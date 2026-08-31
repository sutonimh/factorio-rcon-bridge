"""Clear the hand-built base back to its foundations, so it can be rebuilt from blueprints.

WHY
---
Phase 0 was written as bespoke Python placers - 15 stages, not one of them touching a
blueprint, while phases 1-3 are all stamp-and-revive. Everything that has gone wrong in the
hand-built half traces to that: two builders producing the same thing and destroying each
other's work, a demolition radius that scaled with the size of the job, a cleanup sweep whose
bounds annexed a smelter row. A blueprint has none of those failure modes because the layout
is DATA, not procedure.

The operator's instruction (2026-08-30): "tear down everything in the existing base except the
mines, lab array and the power plant and rebuild the base using exclusively blueprints."

WHAT SURVIVES
-------------
Three things, defined by REGION rather than by entity type, because a region is checkable and
a type list is a thing you can forget an entry in - which is exactly how the iron smelter row
lost its inserters twice.

  MINES        each drill, plus a margin for the poles and belts serving it
  LAB ARRAY    the labs, their inserter chain, and the operator's 105-ghost reservation
  POWER PLANT  boilers, steam engines, offshore pump, the water pipes, the fuel inserters

Everything outside those regions that is base furniture - furnaces, assemblers, belts,
splitters, chests, inserters - is refunded to the character and removed. Power poles are left
alone here and handed to `pole_cull`, which already knows how to tell a redundant pole from a
load-bearing one; deciding that twice, in two places, is how you get a fight.

NOTHING IS DESTROYED THAT THE OPERATOR PLACED BY HAND AND THE BASELINE KNOWS ABOUT: the
protected set from `bootstrap.diff_since_baseline` is honoured, so his repairs survive a
teardown he asked for.
"""
import status

# Regions are (x1, y1, x2, y2) inclusive tile boxes.
LAB_ARRAY = (-4, 28, 27, 53)          # labs at x=0..8 y=36..44 + the ghost reservation to x=24
POWER_PLANT = (-37, 33, -23, 54)      # boilers/engines/pump/pipes, and the outlier lab at -25,41
MINE_MARGIN = 7                       # tiles around each drill kept as its supporting furniture

# What teardown is allowed to remove. Anything not named here is left alone by construction:
# an unknown entity is not "junk I have not thought about", it is something to leave.
DEMOLISH_TYPES = ("furnace", "assembling-machine", "transport-belt", "underground-belt",
                  "splitter", "container", "inserter", "logistic-container", "lamp",
                  "arithmetic-combinator", "decider-combinator", "constant-combinator")

# Poles are deliberately NOT in that list - see the module docstring.


def regions(drills, extra=()):
    """Every keep-region for the live map: the two fixed areas plus a box around each drill."""
    out = [LAB_ARRAY, POWER_PLANT]
    for (x, y) in drills:
        out.append((x - MINE_MARGIN, y - MINE_MARGIN, x + MINE_MARGIN, y + MINE_MARGIN))
    out.extend(extra)
    return out


def inside(regions_, x, y):
    return any(x1 <= x <= x2 and y1 <= y <= y2 for (x1, y1, x2, y2) in regions_)


def _lua_regions(regions_):
    return "{" + ",".join("{%d,%d,%d,%d}" % r for r in regions_) + "}"


def read_drills(A):
    raw = A._print("/sc local s=game.surfaces[1] local o={} "
                   "for _,e in pairs(s.find_entities_filtered{type='mining-drill'}) do "
                   "o[#o+1]=math.floor(e.position.x)..','..math.floor(e.position.y) end "
                   "rcon.print(table.concat(o,';'))").strip()
    return [tuple(int(v) for v in t.split(",")) for t in raw.split(";") if "," in t]


def survey(A, protect=(), log=None):
    """What a teardown WOULD remove, counted by name. Read-only."""
    return _run(A, protect, dry=True, log=log)


def apply(A, protect=(), log=None):
    """Remove it. Returns {name: count} of what went."""
    return _run(A, protect, dry=False, log=log)


def _run(A, protect, dry, log=None):
    say = log or (lambda m: None)
    drills = read_drills(A)
    if not drills:
        say("teardown REFUSED: no mining drills found - that is not a base, that is a bad read")
        return {}
    keep = regions(drills)
    prot = "{" + ",".join("['%d,%d']=true" % (int(x), int(y)) for (x, y) in protect) + "}"
    lua = (
        "/sc local s=game.surfaces[1] local p=storage.derpface "
        "local inv=p and p.valid and p.get_main_inventory() "
        "local R=" + _lua_regions(keep) + " local PROT=" + prot + " "
        "local T={" + ",".join("'%s'" % t for t in DEMOLISH_TYPES) + "} "
        "local function keepit(x,y) for _,r in pairs(R) do "
        "  if x>=r[1] and x<=r[3] and y>=r[2] and y<=r[4] then return true end end return false end "
        "local c={} local n=0 "
        "for _,e in pairs(s.find_entities_filtered{type=T,force='player'}) do "
        "  local x,y=math.floor(e.position.x),math.floor(e.position.y) "
        "  if not keepit(x,y) and not PROT[x..','..y] then "
        "    c[e.name]=(c[e.name] or 0)+1 n=n+1 "
        + ("" if dry else
           # get_output_inventory() covers furnaces, assemblers AND containers in one call.
           # defines.inventory.furnace_result is nil in 2.x and indexing it aborts the whole
           # /sc - which at least failed safe, since the command is atomic and nothing was
           # destroyed, but a refund that silently skipped would have quietly binned the base.
           "    if inv then "
           "      local ok,ci=pcall(function() return e.get_output_inventory() end) "
           "      if ok and ci then for _,it in pairs(ci.get_contents()) do "
           "        inv.insert{name=it.name,count=it.count} end end "
           "      local gp=e.prototype.items_to_place_this "
           "      if gp and gp[1] then inv.insert{name=gp[1].name,count=1} end end "
           "    e.destroy() ")
        + "  end end "
        "local o={} for k,v in pairs(c) do o[#o+1]=k..'='..v end "
        "rcon.print(n..'|'..table.concat(o,' '))")
    out = A._print(lua).strip()
    total, _, rest = out.partition("|")
    counts = {}
    for tok in rest.split():
        if "=" in tok:
            k, v = tok.rsplit("=", 1)
            try:
                counts[k] = int(v)
            except ValueError:
                pass
    say("teardown %s: %s entities - %s"
        % ("WOULD remove" if dry else "removed", total or "0",
           ", ".join("%dx %s" % (v, k) for k, v in sorted(counts.items(), key=lambda i: -i[1]))
           or "nothing"))
    return counts


if __name__ == "__main__":
    import sys
    import autopilot as A
    import bootstrap as B
    prot = ()
    try:
        prot = tuple(tuple(t) for t in (B.diff_since_baseline().get("removed") or ()))
    except Exception:
        pass
    if "--apply" in sys.argv:
        if B.operator_present():
            print("operator is online - not tearing down the base under them")
            sys.exit(1)
        apply(A, protect=prot, log=print)
        report_power(A, log=print)     # a demolition ends by asking what it left dark
    else:
        survey(A, protect=prot, log=print)
        print("(dry run; pass --apply to do it)")


# --------------------------------------------------------------------------- aftercare
def power_check(A):
    """Which consumers read no_power after a teardown, grouped by type.

    THIS EXISTS BECAUSE I SKIPPED IT. The blueprint rebuild went fine and the base still sat
    idle, because all three mines ended up with ZERO poles - 65 of 103 were in the new block,
    3 at the power plant, none at any drill - and every drill had been reading no_power while
    I was busy stamping prints.

    The mechanism is the interaction, not either half: teardown removes the belts, inserters
    and furnaces a pole was covering, and the pole becomes a genuine orphan; pole_cull then
    correctly removes it, because by then it really is supplying nothing. Each step is right
    and the pair of them puts the mines in the dark.

    So a demolition has to end by asking what it left unpowered. Reading the world back is
    the only thing that catches an emergent failure neither component can see on its own.
    """
    raw = A._print(
        "/sc local s=game.surfaces[1] local c={} "
        "for _,e in pairs(s.find_entities_filtered{type={'mining-drill','assembling-machine',"
        "'lab','inserter','furnace'}}) do "
        "  if e.prototype.electric_energy_source_prototype "
        "     and e.status==defines.entity_status.no_power then "
        "    c[e.type]=(c[e.type] or 0)+1 end end "
        "local o={} for k,v in pairs(c) do o[#o+1]=k..'='..v end "
        "rcon.print(table.concat(o,' '))").strip()
    out = {}
    for tok in raw.split():
        if "=" in tok:
            k, v = tok.rsplit("=", 1)
            try:
                out[k] = int(v)
            except ValueError:
                pass
    return out


def report_power(A, log=None):
    """Log what a teardown left dark. A mine with no poles is not a smaller base, it is a
    stopped one: no ore moves, so nothing downstream can be diagnosed either."""
    say = log or (lambda m: None)
    dark = power_check(A)
    if not dark:
        say("teardown aftercare: nothing left unpowered")
        return dark
    say("teardown aftercare: UNPOWERED after demolition - "
        + ", ".join("%d %s" % (v, k) for k, v in sorted(dark.items(), key=lambda i: -i[1]))
        + "  (poles that served the demolished half became orphans and were culled; the "
          "mines need their own lines back)")
    return dark
