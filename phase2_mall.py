#!/usr/bin/env python3
"""Phase 2: robot mall era (MEGABASE-V2-DESIGN §5 phase 2).

Entry: construction-robotics researched (gate1). The bus base matures into the megabase's
factory-factory: build the All-In-One Early-Game Robot Factory (374 ents, hand-buildable by
design - GOTCHAS "robot-rush factory"), get real construction bots flying, upgrade the base
in place (steel furnaces), scale power, fix the city-grid origin, and stock rail materials.

State lives in p["mall"]: {anchor, stamped, grid_origin}.
"""
import pathlib

import autopilot as A
import bootstrap as B
import modules
import status

HERE = pathlib.Path(__file__).resolve().parent
ROBOT_BP = HERE / "blueprints" / "early-game-robot-factory.txt"
SIZE = 40                       # robot factory footprint is ~34x30; margin included
GRID_PITCH = 100                # City Blocks 2.0 snap period

PRIORITY = ("small-electric-pole", "medium-electric-pole", "pipe", "pipe-to-ground",
            "assembling-machine-2", "assembling-machine-1", "chemical-plant", "roboport",
            "logistic-chest-passive-provider", "logistic-chest-storage", "steel-chest",
            "iron-chest", "transport-belt", "fast-transport-belt", "underground-belt",
            "fast-underground-belt", "splitter", "fast-splitter", "inserter",
            "fast-inserter", "long-handed-inserter", "small-lamp")
# rail-era mall stock targets (gate2): enough to lay the first city blocks + rails
STOCK_TARGETS = {"rail": 400, "big-electric-pole": 60, "substation": 20, "roboport": 12,
                 "rail-signal": 40, "rail-chain-signal": 40, "train-stop": 8,
                 "locomotive": 3, "cargo-wagon": 6, "construction-robot": 100}


def _m(p):
    return p.setdefault("mall", {"anchor": None, "stamped": False, "grid_origin": None})


def stamp_robot_factory(p):
    m = _m(p)
    if m["stamped"]:
        return
    if not m["anchor"]:
        # site it east of spawn, clear of the bus base
        for ax in range(60, 220, 40):
            for ay in (-20, 20, -60):
                try:
                    n = modules.stamp_at(ROBOT_BP.read_text().strip(), ax, ay, (SIZE, SIZE))
                    m["anchor"] = [ax, ay]
                    m["stamped"] = True
                    status.log(f"robot factory stamped: {n} ghosts @ {ax},{ay}")
                    return
                except RuntimeError as e:
                    status.log(f"robot factory site ({ax},{ay}) rejected: {e}")
        raise RuntimeError("no site accepted the robot factory stamp")


def build_robot_factory(p):
    m = _m(p)
    area = (m["anchor"][0] - 2, m["anchor"][1] - 2,
            m["anchor"][0] + SIZE + 2, m["anchor"][1] + SIZE + 2)
    modules.craft_batch(area, PRIORITY)
    modules.revive(area, PRIORITY)


def upgrade_in_place():
    """Justified upgrades only (Seth's rule): steel furnaces are strictly better."""
    try:
        B.upgrade_furnaces_to_steel()
    except Exception as e:
        status.log(f"steel-furnace upgrade pass: {e}")


def fix_grid_origin(p):
    """Pin the global city-grid origin on fresh land, snapped to the block pitch."""
    m = _m(p)
    if m["grid_origin"]:
        return tuple(m["grid_origin"])
    base_x, base_y = 0, 0
    gx = ((base_x + 250) // GRID_PITCH) * GRID_PITCH
    gy = (base_y // GRID_PITCH) * GRID_PITCH
    m["grid_origin"] = [gx, gy]
    status.log(f"city-grid origin fixed at {gx},{gy} (pitch {GRID_PITCH})")
    return (gx, gy)


def stock_mall(p):
    """Craft toward the rail-era stock targets (one batch per pass)."""
    for item, target in STOCK_TARGETS.items():
        if not modules.craftable_now(item):
            continue
        have = B._count(item)
        if have >= target:
            continue
        try:
            B.make(item, min(target - have, 25))
            status.log(f"mall stock: {item} {have}->{B._count(item)} (target {target})")
        except Exception as e:
            status.log(f"mall stock {item} failed: {e}")
        return


def advance(p):
    stamp_robot_factory(p)
    build_robot_factory(p)
    upgrade_in_place()
    fix_grid_origin(p)
    stock_mall(p)


def gate(p):
    bots = modules.bots_idle()
    stock_ok = all(B._count(i) >= t for i, t in STOCK_TARGETS.items()
                   if modules.craftable_now(i))
    checks = {
        "bots>=50": bots >= 50,
        "railway": B._tech_done("railway"),
        "automated-rail-transportation": B._tech_done("automated-rail-transportation"),
        "mall_stock": stock_ok,
        "grid_origin": _m(p)["grid_origin"] is not None,
    }
    return all(checks.values()), checks
