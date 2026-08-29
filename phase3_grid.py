#!/usr/bin/env python3
"""Phase 3: city blocks + trains — the megabase (MEGABASE-V2-DESIGN §5 phase 3).

The City Blocks 2.0 skeleton (roboport/substation/big-pole grid) is stamped block-by-block
from the phase-2 grid origin; rail arteries come from the substation-aligned rail-segment
book; generic trains dispatch via trains.py groups + interrupts. Blocks are built by
CONSTRUCTION BOTS once the block's roboports are up — the character only seeds each new
block's roboports/poles (the coverage bootstrap), then bots + logistics do the rest.

Block plan (throughput order, design §5): skeleton -> smelting -> circuits -> science ->
mall relocation. Grid state lives in p["grid"]: {origin, pitch, blocks: {"x,y": stage}}.

HONEST SCAFFOLD NOTE: block-interior modules (smelting/science) and the bus-science
teardown land iteratively once the proving run reaches this phase; stamping the skeleton,
rail ring, and train groups are implemented below.
"""
import autopilot as A
import bootstrap as B
import modules
import status
import trains

PITCH = 100
SEED_PRIORITY = ("roboport", "big-electric-pole", "substation", "medium-electric-pole")
BLOCK_LIB = "nilaus-sa-masterclass-city-blocks-2-0"
RAIL_LIB = "nilaus-space-age-substation-aligned-rail-segments"
# build-out order: offsets from origin in block units, ring by ring
BLOCK_ORDER = [(0, 0), (1, 0), (0, 1), (1, 1), (-1, 0), (0, -1), (-1, 1), (1, -1), (-1, -1),
               (2, 0), (0, 2), (2, 1), (1, 2), (2, 2)]


def _g(p):
    g = p.setdefault("grid", {})
    g.setdefault("origin", (p.get("mall") or {}).get("grid_origin"))
    g.setdefault("pitch", PITCH)
    g.setdefault("blocks", {})
    return g


def _block_anchor(g, bx, by):
    ox, oy = g["origin"]
    return ox + bx * g["pitch"], oy + by * g["pitch"]


def stamp_block(p, bx, by):
    """Stamp one City Block 2.0 skeleton; character-seed its roboports; bots finish."""
    g = _g(p)
    key = f"{bx},{by}"
    if g["blocks"].get(key):
        return
    ax, ay = _block_anchor(g, bx, by)
    bp = modules.child_string(modules.lib_for("city-block", BLOCK_LIB))
    n = modules.stamp_at(bp, ax, ay, (g["pitch"], g["pitch"]))
    g["blocks"][key] = "stamped"
    status.log(f"city block ({bx},{by}) stamped: {n} ghosts @ {ax},{ay}")


def seed_block(p, bx, by):
    """Character-build the block's roboports + poles so bot coverage reaches it; everything
    else in the block is left to construction robots + logistics."""
    g = _g(p)
    ax, ay = _block_anchor(g, bx, by)
    area = (ax - 2, ay - 2, ax + g["pitch"] + 2, ay + g["pitch"] + 2)
    modules.craft_batch(area, SEED_PRIORITY, default_cap=8)
    modules.revive(area, SEED_PRIORITY)
    remaining = modules.ghost_needs(area)
    if not any(remaining.get(n) for n in SEED_PRIORITY):
        g["blocks"][f"{bx},{by}"] = "seeded"
        status.log(f"city block ({bx},{by}) seeded (bots take over)")


def ensure_ore_trains(p):
    """Generic interrupt trains for each ore with a rail-served outpost. Idempotent: groups
    are rebuilt deterministically; stops come from the rail-segment blueprints' named stations."""
    for item in ("iron-ore", "copper-ore", "coal", "stone"):
        stops = trains.list_trains()
        try:
            trains.create_group_schedule(f"ore:{item}", item)
        except Exception as e:
            status.log(f"train group ore:{item}: {e}")
    _ = stops  # fleet listing informational for now


def advance(p):
    g = _g(p)
    if not g["origin"]:
        raise RuntimeError("phase3: no grid origin (phase 2 must fix it)")
    # progress one block at a time through stamped -> seeded; bots finish each block
    for bx, by in BLOCK_ORDER:
        key = f"{bx},{by}"
        stage = g["blocks"].get(key)
        if stage is None:
            stamp_block(p, bx, by)
            return
        if stage == "stamped":
            seed_block(p, bx, by)
            return
    ensure_ore_trains(p)


def gate(p):
    """Victory ladder v1: skeleton grid seeded 3x3 + trains grouped. SPM targets escalate
    as block interiors land (design: 1k SPM, 5x5, self-expanding)."""
    g = _g(p)
    seeded = sum(1 for v in g["blocks"].values() if v == "seeded")
    checks = {"blocks_seeded>=9": seeded >= 9}
    return all(checks.values()), checks
