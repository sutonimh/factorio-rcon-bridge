#!/usr/bin/env python3
"""Generalized blueprint-module machinery: stamp a library blueprint as ghosts, then craft
toward + revive them in priority order. Extracted from the phase1_oil pattern so phases 2-3
(robot factory, mall, city blocks, rail segments) reuse one proven path.

Pre-bots: build_ghosts_priority revives with real inventory items. Post-bots: construction
robots build ghosts inside roboport coverage on their own; revive() still catches the rest
(e.g. the first roboports of a new block, outside existing coverage).
"""
import json

import autopilot as A
import bootstrap as B
import bplib
import status
import techdb


def child_string(lib_name, child_label=None, index=0):
    """Extract one blueprint from a library book (by label substring, else index),
    snap-stripped and re-encoded. A bare (non-book) entry returns itself."""
    s = bplib.load(lib_name)[0]
    d = bplib.decode(s)
    if "blueprint_book" not in d:
        bplib.strip_snap(d)
        return bplib.encode(d)
    kids = d["blueprint_book"]["blueprints"]
    node = None
    if child_label:
        for k in kids:
            lbl = (k.get("blueprint") or k.get("blueprint_book") or {}).get("label", "")
            if child_label.lower() in lbl.lower():
                node = k
                break
        if node is None:
            raise KeyError(f"{lib_name}: no child matching {child_label!r}")
    else:
        node = kids[index]
    bplib.strip_snap(node)
    return bplib.encode(node)


def bp_size(bp_string):
    """(w, h) of a blueprint's entity bbox."""
    d = bplib.decode(bp_string)
    ents = d["blueprint"]["entities"]
    xs = [e["position"]["x"] for e in ents]
    ys = [e["position"]["y"] for e in ents]
    return int(max(xs) - min(xs)) + 2, int(max(ys) - min(ys)) + 2


def stamp_at(bp_string, ax, ay, size_hint=None):
    """Chunk-gen + terrain-clear + ghost-stamp centered on the anchor area. Returns ghost
    count (raises on cliffs or a failed stamp). Anchor (ax, ay) = top-left tile."""
    w, h = size_hint or bp_size(bp_string)
    cx, cy = ax + w // 2, ay + h // 2
    A._print(f"/sc local s=game.surfaces[1]; s.request_to_generate_chunks({{{cx},{cy}}},{max(w, h) // 32 + 2}); s.force_generate_chunk_requests()")
    cliffs = A.clear_area(cx, cy, max(w, h) // 2 + 4)
    if isinstance(cliffs, int) and cliffs > 0:
        raise RuntimeError(f"stamp site ({ax},{ay}) has {cliffs} cliffs - move it")
    out = "-1"
    for cmd in bplib.stamp_lua(bp_string, cx, cy):
        out = A._print(cmd).strip()
    n = int(out or "-1")
    if n <= 0:
        raise RuntimeError(f"stamp at ({ax},{ay}) placed {n} ghosts")
    return n


def ghost_needs(area):
    """Remaining ghosts by item name within area=(x1,y1,x2,y2)."""
    out = A._print(
        f"/sc local s=game.surfaces[1]; local t={{}};"
        f"for _,g in pairs(s.find_entities_filtered{{name='entity-ghost',area={{{{{area[0]},{area[1]}}},{{{area[2]},{area[3]}}}}}}}) do"
        "  t[g.ghost_name]=(t[g.ghost_name] or 0)+1 end;"
        "rcon.print(helpers.table_to_json(t))").strip()
    try:
        return json.loads(out) if out.startswith("{") else {}
    except ValueError:
        return {}


def craftable_now(item):
    t = techdb.unlocking_tech(item)
    return (t is None) or B._tech_done(t)


def craft_batch(area, priority, batch_caps=None, default_cap=10):
    """Craft ONE batch toward the neediest buildable ghost item (one per pass so the maintain
    loop keeps breathing). Returns the item crafted or None."""
    needs = ghost_needs(area)
    caps = batch_caps or {}
    for item in priority:
        n = needs.get(item, 0)
        if n <= 0 or not craftable_now(item):
            continue
        cap = caps.get(item, default_cap)
        have = B._count(item)
        if have >= min(n, cap):
            continue
        try:
            B.make(item, min(n - have, cap))
            status.log(f"module craft: {item} x{min(n - have, cap)} ({n} ghosts left)")
        except Exception as e:
            status.log(f"module craft {item} failed: {e}")
        return item
    return None


def revive(area, priority):
    r = A.build_ghosts_priority(list(priority), area=area)
    if r and "revived 0" not in r:
        status.log(f"module revive: {r}")


def bots_idle():
    """Idle construction robots across the force's logistic networks."""
    out = A._print(
        "/sc local n=0; for _,net in pairs(game.forces.player.logistic_networks['nauvis'] or {}) do"
        "  n=n+net.available_construction_robots end; rcon.print(n)").strip()
    try:
        return int(out)
    except ValueError:
        return 0
