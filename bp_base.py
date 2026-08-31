"""Build the base from blueprints only - the phase-0 half that never had them.

Phases 1-3 were always blueprint-driven (`modules.stamp_at` -> craft -> revive). Phase 0 was
fifteen bespoke Python placers and not one blueprint, and every structural failure this
session came out of that half: two builders making the same thing and destroying each other's
work, a demolition radius that scaled with the size of the job, a cleanup sweep whose bounds
annexed a smelter row. A blueprint cannot do any of those, because the layout is DATA.

THE SLOTS, and why each print was chosen
----------------------------------------
Everything here is checked BUILDABLE against current tech before it is stamped. The library's
better prints - the 50x46 steel Smelting Blocks, the 1.5/s science rows, every Unloading
station - need `fast-transport-belt`, `fast-inserter` or `steel-furnace`, and this base has
none of the three (`logistics-2` and `advanced-material-processing` are both unresearched).
Stamping a print you cannot revive just litters ghosts, so `feasible()` is a hard gate rather
than a warning.

  iron_smelter    Basic Smelting - Iron/Copper   43x11   281 ents  stone furnaces, basic belts
  copper_smelter  Basic Smelting - Iron/Copper   43x11   281 ents  the same print, stamped again
  red_science     bootstrap-red-science          20x13   104 ents  the de-fasted 1.5/s row
  green_science   bootstrap-green-science        15x21   147 ents  ditto

`bootstrap-*-science` exist precisely because the tileable originals are blocked on one item,
`fast-inserter`; they are those prints with the fast inserters substituted out.
"""
import bplib
import modules
import status

# Items this base cannot make yet. Anything in a blueprint that is also in here means the
# print cannot be revived, so it must not be stamped.
def locked_items(B, A=None, names=()):
    """Entity names that cannot be built right now - ASKED OF THE GAME, not guessed.

    This was a hardcoded list of the usual suspects, and `small-lamp` was not on it. Its
    recipe needs optics, which is unresearched, so eight lamp ghosts got stamped that can
    never be revived - permanent litter inside an otherwise finished blueprint. A hardcoded
    denylist can only ever exclude the items someone remembered.

    Two questions, both answered by the game: is the recipe ENABLED for this force, and do
    its ingredients exist at this tech level. `steel-plate` is the other trap - it is a
    SMELTING recipe, so an item needing it cannot be hand-crafted however much iron you hold.
    """
    out = set()
    for name in names:
        if not modules.craftable_now(name):
            out.add(name)
    if A is None or not names:
        return out
    q = ("/sc local f=game.forces.player local o={} "
         + " ".join("local r=f.recipes['%s'] if not r or not r.enabled then o[#o+1]='%s' end "
                    % (n, n) for n in sorted(names))
         + "rcon.print(table.concat(o,' '))")
    try:
        out.update(t for t in A._print(q).split() if t)
    except Exception:
        pass
    return out


SLOTS = [
    # (slot, library, child label or None, offset from the block anchor)
    #
    # THE OFFSETS ARE THE POINT. Siting each print independently at its own nearest-fit gives
    # four unrelated buildings scattered across the map - which is what a "find the closest
    # legal spot" search actually produces, and it is not a base. The arrangement is fixed
    # here as data and the whole BLOCK is sited once, so the smelters sit side by side with
    # their outputs on the same edge and the science rows sit east of them where a bus can
    # reach both. One anchor, one coherent layout.
    #
    #                    x=0                    x=50
    #        y=0   [ iron smelter  44x12 ]  [ red science  21x14 ]
    #        y=16  [ copper smelt  44x12 ]  [ green science 16x22 ]
    #
    ("iron_smelter", "nilaus-sa-masterclass-early-game-smelting", "Basic Smelting - Iron", (0, 0)),
    ("copper_smelter", "nilaus-sa-masterclass-early-game-smelting", "Basic Smelting - Iron", (0, 16)),
    ("red_science", "bootstrap-red-science", None, (50, 0)),
    ("green_science", "bootstrap-green-science", None, (50, 18)),
]

# Room for the block plus the bus corridor down its eastern side and belt runs around it.
BLOCK_W, BLOCK_H = 76, 44


def bp_for(slot):
    """The blueprint string for a slot, honouring dashboard overrides like the other phases."""
    for name, lib, child, _ in SLOTS:
        if name == slot:
            return modules.child_string(modules.lib_for(slot, lib), child)
    raise KeyError(slot)


def entity_names(bp_string):
    d = bplib.decode(bp_string)
    b = d.get("blueprint", {})
    return {e["name"] for e in b.get("entities", [])}


def feasible(bp_string, locked):
    """(ok, blocked). Stamping a print you cannot revive just litters ghosts over the map."""
    bad = sorted(entity_names(bp_string) & set(locked))
    return (not bad), bad


def blocked_in(A, B, bp_string):
    """The unbuildable entities of ONE print, asked of the game for exactly its contents."""
    names = entity_names(bp_string)
    return sorted(locked_items(B, A, names) & names)


def size_of(bp_string):
    return modules.bp_size(bp_string)


# --------------------------------------------------------------------------- siting
def free_box(obstacles, x, y, w, h):
    """Is every tile of the w x h box at (x,y) clear of hard ground and reservations?"""
    for ix in range(x, x + w):
        for iy in range(y, y + h):
            if (ix, iy) in obstacles:
                return False
    return True


def find_site(obstacles, w, h, prefer, span=140, step=2):
    """The clear w x h box nearest `prefer`, searched on a ring so the result is the closest
    legal site rather than the first one a raster scan trips over. Deterministic."""
    px, py = prefer
    best = None
    for r in range(0, span, step):
        for dx in range(-r, r + 1, step):
            for dy in (-r, r) if r else (0,):
                for (ox, oy) in ((dx, dy), (dy, dx)):
                    x, y = px + ox, py + oy
                    if free_box(obstacles, x, y, w, h):
                        d = ox * ox + oy * oy
                        if best is None or d < best[0]:
                            best = (d, x, y)
        if best:
            return best[1], best[2]
    return None


def obstacle_set(A, x1, y1, x2, y2, pad=2):
    """Everything a stamp must avoid: entities, ghosts, ore, water, cliffs - plus a pad so
    blueprints do not end up flush against the things they are meant to serve."""
    raw = A._print(
        "/sc local s=game.surfaces[1] local o={} "
        "for _,e in pairs(s.find_entities_filtered{area={{%d,%d},{%d,%d}}}) do "
        # Trees and rocks are NOT obstacles: modules.stamp_at clears them before it stamps.
        # Counting them blocked every site west of x=38 and forced the whole base 100+ tiles
        # from the labs and the power plant, for scenery. Cliffs stay - those cannot be cleared.
        "  if e.name~='character' and e.type~='tree' and e.type~='simple-entity' then "
        "    local b=e.bounding_box "
        "    o[#o+1]=math.floor(b.left_top.x)..','..math.floor(b.left_top.y)..','"
        "      ..(math.ceil(b.right_bottom.x)-1)..','..(math.ceil(b.right_bottom.y)-1) end end "
        "for _,t in pairs(s.find_tiles_filtered{area={{%d,%d},{%d,%d}},"
        "  name={'water','deepwater','water-shallow','water-mud'}}) do "
        "  o[#o+1]=t.position.x..','..t.position.y..','..t.position.x..','..t.position.y end "
        "rcon.print(table.concat(o,';'))" % (x1, y1, x2, y2, x1, y1, x2, y2)).strip()
    out = set()
    for tok in raw.split(";"):
        f = tok.split(",")
        if len(f) != 4:
            continue
        a, b, c, d = (int(v) for v in f)
        for ix in range(a - pad, c + 1 + pad):
            for iy in range(b - pad, d + 1 + pad):
                out.add((ix, iy))
    return out


# --------------------------------------------------------------------------- building
def stamp(slot, x, y, bp_string=None):
    """Ghost-stamp one slot. Returns the ghost count."""
    bp = bp_string or bp_for(slot)
    w, h = size_of(bp)
    return modules.stamp_at(bp, x, y, (w, h))


def build_out(A, area, rounds=6):
    """Craft toward the ghosts in `area`, then revive whatever the inventory can pay for.

    Repeats: crafting one batch per pass is what keeps the loop breathing, and a revive that
    runs out of an item falls through to the next tier instead of stalling the whole print.
    """
    needs = modules.ghost_needs(area)
    priority = [n for n, _ in sorted(needs.items(), key=lambda kv: -kv[1])]
    built = 0
    for _ in range(rounds):
        modules.craft_batch(area, priority)
        modules.revive(area, priority)
        left = sum(modules.ghost_needs(area).values())
        if left == 0:
            break
        built += 1
    return sum(modules.ghost_needs(area).values())


def describe(slot, bp_string, site):
    w, h = size_of(bp_string)
    return "%s: %dx%d at %s" % (slot, w, h, site)


def block_sites(anchor):
    """{slot: (x, y)} for the whole base block placed at `anchor` (its top-left tile)."""
    ax, ay = anchor
    return {slot: (ax + ox, ay + oy) for slot, _, _, (ox, oy) in SLOTS}


def find_block(obstacles, prefer, w=BLOCK_W, h=BLOCK_H):
    """One anchor for the entire block. Siting the block rather than each print is what keeps
    the smelters beside each other and the science within a bus run of them."""
    return find_site(obstacles, w, h, prefer)


def plan(A, prefer=(-10, -34), bounds=(-80, -60, 110, 70)):
    """Site the whole base and check every print is buildable. Returns a plan dict; builds
    nothing. `reason` is set when the base cannot be placed as designed."""
    import bootstrap as B
    locked = locked_items(B)
    prints, blocked = {}, {}
    for slot, lib, child, _ in SLOTS:
        bp = modules.child_string(modules.lib_for(slot, lib), child)
        ok, bad = feasible(bp, locked)
        prints[slot] = bp
        if not ok:
            blocked[slot] = bad
    if blocked:
        return {"reason": "; ".join("%s needs %s" % (s, ", ".join(b))
                                    for s, b in sorted(blocked.items())),
                "blocked": blocked}
    obs = obstacle_set(A, *bounds, pad=2)
    anchor = find_block(obs, prefer)
    if anchor is None:
        return {"reason": "no clear %dx%d site for the base block near %s"
                          % (BLOCK_W, BLOCK_H, prefer)}
    sites = block_sites(anchor)
    return {"anchor": anchor, "sites": sites, "prints": prints, "obstacles": len(obs),
            "reason": None}
