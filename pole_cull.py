"""Remove every power pole the base does not need, continuously and by proof.

WHY THIS EXISTS
---------------
Three pole-cleanup mechanisms already lived in this repo and NONE of them ran:

  * `optimize_poles.py`   a manual script, never called by anything
  * `remove_redundant.py` called only from patrol.py, which nothing imports
  * `bootstrap.dedupe_poles()`  commented out of the maintenance loop

So poles accumulated. The operator has now had to point at the same mess twice, and the
second time the fix was 27 poles culled BY HAND at the smelter stack. A hand-cull is not a
fix; it is the same bug happening again with extra steps.

The old `dedupe_poles` also could not have found those 27. Its candidate rule was "another
pole within 2.0 tiles" - a proximity guess. A small pole supplies a 5x5 window and wires 7.5
tiles, so two poles FOUR tiles apart can be entirely redundant and that rule never sees them.

WHAT THIS DOES INSTEAD
----------------------
A pole is removable when removing it provably costs nothing:

  1. every consumer it supplies is also supplied by a pole that stays, and
  2. the poles that stay are still ONE connected electric network, and
  3. every generator is still supplied, so power still enters that network.

That is a decision the bot can make on its own, from one map read, with no human in the
loop and no per-pole probing. Poles that supply nothing are candidates too - the old code
refused to touch them because it had no connectivity test and kept deleting the connector
that bridged the steam engine to the base. With rule 2 that fear is answerable: an orphan
that is not an articulation point is pure waste and goes; one that is load-bearing stays.

Removal is applied as ONE batch and verified against the live game. If the batch raises the
unpowered count or splits the grid, the whole batch is put back. See GOTCHAS 'power grid'.
"""
import math

import power_planner as PP

# The operator's floor: below this many poles we do not bother, and we never cull the last
# few poles of a tiny grid where every one of them is structural.
MIN_POLES_TO_BOTHER = 4


# --------------------------------------------------------------------------- pure core
def _centre(p):
    return PP.centre(p["name"], p["x"], p["y"])


def _wires(a, b):
    """Do two poles wire to each other? Factorio measures wire reach centre-to-centre, and a
    mixed-tier pair wires at the SHORTER of the two reaches."""
    ax, ay = _centre(a)
    bx, by = _centre(b)
    return math.hypot(ax - bx, ay - by) <= PP.wire_reach(a["name"], b["name"])


def coverage(poles, consumers):
    """{consumer index -> set of pole indices supplying it}.

    `consumers` are inclusive tile boxes (l, t, r, b); `poles` carry a top-left tile (x, y).
    Supply is a box OVERLAP test, not a centre-distance test - that is what the engine does,
    and getting it wrong is how you conclude a pole is redundant when it is the only thing
    powering the edge of a furnace."""
    out = {}
    for ci, box in enumerate(consumers):
        out[ci] = {pi for pi, p in enumerate(poles)
                   if PP.covers(p["name"], p["x"], p["y"], box)}
    return out


def connected(poles, keep):
    """Could the kept poles form one network, given wire reach?

    NOTE THE MOOD. This is what is ACHIEVABLE, not what currently exists. Factorio does not
    re-wire survivors when you delete a pole out of the middle of a chain: the two halves stay
    within reach of each other and stay on separate networks, exactly as a script-placed pole
    sits unwired until you call connect_to. The first live run of this culler removed 60 poles
    and split the grid 86/19, darkening the whole lab array, because this function said "still
    reachable" and the game meant "still unwired".

    So a cull is only safe when paired with `rewire()`. `apply()` does both.
    """
    keep = sorted(keep)
    if len(keep) <= 1:
        return True
    idx = {k: i for i, k in enumerate(keep)}
    adj = {i: [] for i in range(len(keep))}
    for i, a in enumerate(keep):
        for b in keep[i + 1:]:
            if _wires(poles[a], poles[b]):
                adj[i].append(idx[b])
                adj[idx[b]].append(i)
    seen = {0}
    stack = [0]
    while stack:
        n = stack.pop()
        for m in adj[n]:
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return len(seen) == len(keep)


def cull(poles, consumers, protect=()):
    """Indices of poles that can be removed without unpowering anything or splitting the grid.

    Greedy to a fixpoint, taking the LEAST useful pole first (fewest consumers supplied) and
    breaking ties on position so the result is reproducible - the same map always yields the
    same cull, which matters because this runs unattended on a loop.

    `consumers` must already include the generators: a network nothing feeds is not a win.
    """
    if len(poles) < MIN_POLES_TO_BOTHER:
        return []
    protect = set(protect)
    cov = coverage(poles, consumers)

    # A consumer no pole supplies is already unpowered; culling cannot make it worse, and
    # holding the whole pass hostage to it would mean one dark machine freezes cleanup
    # forever. Ignore it here - fix_unpowered() is what answers for those.
    live = {ci: pis for ci, pis in cov.items() if pis}

    keep = set(range(len(poles)))
    removed = []

    def useful(pi):
        return sum(1 for pis in live.values() if pi in pis)

    order = sorted(range(len(poles)),
                   key=lambda pi: (useful(pi), poles[pi]["x"], poles[pi]["y"], poles[pi]["name"]))

    progress = True
    while progress:
        progress = False
        for pi in order:
            if pi not in keep or pi in protect:
                continue
            trial = keep - {pi}
            # rule 1: nothing this pole supplies goes dark
            if any(not (pis & trial) for pis in live.values() if pi in pis):
                continue
            # rules 2+3: what is left is still one network (generators are in `consumers`,
            # so rule 1 already kept them supplied)
            if not connected(poles, trial):
                continue
            keep = trial
            removed.append(pi)
            progress = True
    return sorted(removed)


def explain(poles, consumers, protect=()):
    """Human-readable summary of what a cull would do and why - for the status log, so the
    loop says what it changed instead of silently deleting the operator's poles."""
    gone = cull(poles, consumers, protect)
    if not gone:
        return "poles: %d, none redundant" % len(poles)
    orphans = sum(1 for pi in gone
                  if not any(PP.covers(poles[pi]["name"], poles[pi]["x"], poles[pi]["y"], b)
                             for b in consumers))
    return ("poles: %d -> %d (cull %d: %d supplying nothing, %d duplicated cover)"
            % (len(poles), len(poles) - len(gone), len(gone), orphans, len(gone) - orphans))


# --------------------------------------------------------------------------- live map
_READ = (
    "/sc local s=game.surfaces[1]; local o={};"
    "for _,p in pairs(s.find_entities_filtered{type='electric-pole'}) do"
    "  local b=p.bounding_box;"
    # carry the EXACT position too. Deriving it back from the tile drops any pole the
    # operator placed off our half-tile grid, and a destroy that misses turns the revert
    # into a duplicate-or-lose. The first live run finished one pole short because of it.
    "  o[#o+1]='P|'..p.name..'|'..math.floor(b.left_top.x)..'|'..math.floor(b.left_top.y)"
    "    ..'|'..p.position.x..'|'..p.position.y end;"
    "local T={'assembling-machine','lab','inserter','mining-drill','furnace','pumpjack',"
    "'beacon','radar','lamp','electric-turret','boiler','offshore-pump'};"
    "for _,e in pairs(s.find_entities_filtered{type=T}) do"
    "  if e.prototype.electric_energy_source_prototype then local b=e.bounding_box;"
    "    o[#o+1]='C|'..math.floor(b.left_top.x)..'|'..math.floor(b.left_top.y)..'|'"
    "      ..(math.ceil(b.right_bottom.x)-1)..'|'..(math.ceil(b.right_bottom.y)-1) end end;"
    "for _,e in pairs(s.find_entities_filtered{name='steam-engine'}) do local b=e.bounding_box;"
    "  o[#o+1]='G|'..math.floor(b.left_top.x)..'|'..math.floor(b.left_top.y)..'|'"
    "    ..(math.ceil(b.right_bottom.x)-1)..'|'..(math.ceil(b.right_bottom.y)-1) end;"
    "rcon.print(table.concat(o,';'))"
)


def read_world(A):
    """(poles, consumers) off the live map. Generators are appended to `consumers` because
    they must stay supplied too - a pole layout that powers every machine but strands the
    steam engine is not a saving."""
    poles, consumers = [], []
    for tok in (A._print(_READ).strip() or "").split(";"):
        f = tok.split("|")
        if f[0] == "P" and len(f) == 6:
            poles.append({"name": f[1], "x": int(f[2]), "y": int(f[3]),
                          "px": float(f[4]), "py": float(f[5])})
        elif f[0] in ("C", "G") and len(f) == 5:
            consumers.append(tuple(int(v) for v in f[1:5]))
    return poles, consumers


def _pos(p):
    """The pole's real position if we read it off the map, else the tile centre."""
    if "px" in p:
        return (p["px"], p["py"])
    return PP.centre(p["name"], p["x"], p["y"])


_REWIRE = (
    "/sc local s=game.surfaces[1]; local P=s.find_entities_filtered{type='electric-pole'};"
    "local n=0;"
    "for i=1,#P do for j=i+1,#P do local a,b=P[i],P[j];"
    "  local r=math.min(a.prototype.get_max_wire_distance(a.quality),"
    "                   b.prototype.get_max_wire_distance(b.quality));"
    "  local dx,dy=a.position.x-b.position.x,a.position.y-b.position.y;"
    "  if (dx*dx+dy*dy) <= r*r and a.electric_network_id ~= b.electric_network_id then"
    "    local ca=a.get_wire_connector(defines.wire_connector_id.pole_copper,true);"
    "    local cb=b.get_wire_connector(defines.wire_connector_id.pole_copper,true);"
    "    if ca.connect_to(cb,false) then n=n+1 end end end end;"
    "rcon.print(n)"
)


def rewire(A):
    """Wire any two poles that are within reach but sat on DIFFERENT networks, until the grid
    is whole again. This is the other half of a cull: removing a pole from the middle of a
    chain leaves the halves reachable but unconnected, and nothing in the engine heals that.

    Only ever ADDS wires, and only between poles already on separate networks, so it cannot
    disturb a layout the operator wired deliberately. Returns the number of links made."""
    return int(A._print(_REWIRE).strip() or 0)


def apply(A, protect=(), dry_run=False, log=None):
    """Cull redundant poles on the live map, as one verified batch.

    Returns the number removed. Batch-and-verify rather than the old probe-each-pole loop:
    that one paid a 0.6s settle per CANDIDATE, so a real cleanup would have taken minutes of
    wall clock every lap. Here the decision is computed offline from a single read and the
    game is touched twice.
    """
    say = log or (lambda m: None)
    # The truce covers WRITES, and this is a write. Culling poles out from under the operator
    # while they are stood in the base fixing something by hand is exactly the class of thing
    # that got the bot told off before.
    if not dry_run:
        try:
            import bootstrap
            if bootstrap.operator_present():
                say("pole cull: operator present, standing down")
                return 0
        except Exception:
            pass                                 # no truce available -> proceed
    poles, consumers = read_world(A)
    if len(poles) < MIN_POLES_TO_BOTHER:
        return 0
    gone = cull(poles, consumers, protect)
    if not gone:
        return 0
    say(explain(poles, consumers, protect))
    if dry_run:
        return len(gone)

    import time
    base_unpow, base_nets = _unpowered(A), _networks(A)
    spec = [(poles[i]["name"], _pos(poles[i])) for i in gone]

    # Destroy in chunks: one /sc is a single line, and 60-odd removals ran to 7k characters.
    for chunk in _chunks(spec, 25):
        A._print("/sc local s=game.surfaces[1];" + ";".join(
            "for _,p in pairs(s.find_entities_filtered{type='electric-pole',position={%s,%s},"
            "radius=0.4}) do p.destroy() end" % (c[0], c[1]) for _, c in chunk))
    links = rewire(A)                            # heal the chains the removals broke
    time.sleep(0.6)

    if _unpowered(A) > base_unpow or _networks(A) > base_nets:
        # Put every one of them back. A partial revert would leave the grid in a state
        # neither the plan nor the operator chose, which is worse than the poles.
        for chunk in _chunks(spec, 25):
            A._print("/sc local s=game.surfaces[1]; local f=game.forces.player;" + ";".join(
                "s.create_entity{name='%s',position={%s,%s},force=f}" % (n, c[0], c[1])
                for n, c in chunk))
        rewire(A)                                # and re-wire what we just put back
        say("pole cull REVERTED: removal unpowered a consumer or split the grid")
        return 0
    say("pole cull: removed %d, wired %d link%s to keep the grid whole"
        % (len(gone), links, "" if links == 1 else "s"))
    return len(gone)


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _unpowered(A):
    return int(A._print(
        "/sc local s=game.surfaces[1]; local n=0;"
        "for _,e in pairs(s.find_entities_filtered{type={'assembling-machine','lab','inserter',"
        "'mining-drill','furnace'}}) do"
        "  if e.prototype.electric_energy_source_prototype and e.status==58 then n=n+1 end end;"
        "rcon.print(n)").strip() or 0)


def _networks(A):
    return int(A._print(
        "/sc local s=game.surfaces[1]; local seen={}; local n=0;"
        "for _,p in pairs(s.find_entities_filtered{type='electric-pole'}) do"
        "  local id=p.electric_network_id; if id and not seen[id] then seen[id]=true; n=n+1 end end;"
        "rcon.print(n)").strip() or 0)


if __name__ == "__main__":
    import sys
    import autopilot as A
    dry = "--apply" not in sys.argv
    poles, consumers = read_world(A)
    print(explain(poles, consumers))
    n = apply(A, dry_run=dry, log=print)
    print(("would remove %d (pass --apply to do it)" if dry else "removed %d") % n)
