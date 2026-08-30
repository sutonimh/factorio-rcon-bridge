#!/usr/bin/env python3
"""L1 authoritative world registry (MEGABASE-V2-DESIGN section 3).

Every entity the bot builds is registered here: {uid, name, tile_pos, direction, role,
phase, order_id, created_ts}. Servicing/teardown coords derive from this registry, never
from literals — the registry is what makes teardown SURGICAL (GOTCHAS law: never
area-destroy blind). Ore patches are tracked too (record_patch/patches).

Persistence: world-db.json (runtime file, gitignored), written atomically
(tempfile + os.replace) because other sessions share this worktree. All registry ops are
pure file ops so this module unit-tests with NO live server; only scan_area/scan_tiles/
reconcile touch RCON (reads only — chunked-read pattern from architect.py).

tile_pos convention: math.floor of the entity's live CENTER, matching what scan_area
returns. (1x1 on tile (x,y): center (x+.5,y+.5) -> floor (x,y); 2x2 placed at top-left
(x,y): center (x+1,y+1) -> floor (x+1,y+1).) Registering from a scan diff or from a
verified place order both yield this form, so reconcile can match exactly.
"""
import json
import math
import os
import pathlib
import tempfile
import time

import rcon

HERE = pathlib.Path(__file__).resolve().parent
DB_PATH = HERE / "world-db.json"   # tests repoint this at a tmp dir
CHUNK = 3000                       # chars per RCON read slice (large responses truncate)

ROLES = {"mine", "smelter", "power", "science", "bus", "mall", "grid", "rail", "defense"}


# --------------------------------------------------------------------------- persistence
def _empty():
    return {"seq": 0, "entities": {}, "patches": {}}


def _load():
    try:
        return json.loads(pathlib.Path(DB_PATH).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return _empty()


def atomic_write(path, obj):
    """Write JSON atomically: tempfile in the same dir + os.replace (never a partial file,
    even if another session or a crash interleaves)."""
    path = pathlib.Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save(db):
    atomic_write(DB_PATH, db)


# --------------------------------------------------------------------------- registry
def register(entities, role, phase, order_id):
    """Register built entities. `entities`: iterable of {name, tile_pos:(x,y) | x,y,
    direction?}. Returns the list of assigned uids."""
    db = _load()
    uids = []
    now = time.time()
    for e in entities:
        tp = e.get("tile_pos") or (e["x"], e["y"])
        db["seq"] += 1
        uid = "e%d" % db["seq"]
        db["entities"][uid] = {
            "uid": uid, "name": e["name"], "tile_pos": [int(tp[0]), int(tp[1])],
            "direction": int(e.get("direction", e.get("d", 0)) or 0),
            "role": role, "phase": phase, "order_id": order_id,
            "created_ts": now, "missing": False,
        }
        uids.append(uid)
    _save(db)
    return uids


def unregister(uids):
    """Remove records by uid. Returns how many were removed."""
    db = _load()
    n = 0
    for uid in uids:
        if db["entities"].pop(uid, None) is not None:
            n += 1
    _save(db)
    return n


def query(role=None, phase=None, order_id=None, bbox=None, include_missing=True):
    """Filter the registry -> list of record dicts. bbox=(minx,miny,maxx,maxy) on tile_pos."""
    out = []
    for r in _load()["entities"].values():
        if role is not None and r["role"] != role:
            continue
        if phase is not None and r["phase"] != phase:
            continue
        if order_id is not None and r["order_id"] != order_id:
            continue
        if not include_missing and r.get("missing"):
            continue
        if bbox is not None:
            x, y = r["tile_pos"]
            if not (bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]):
                continue
        out.append(dict(r))
    return out


def bounds(role=None, phase=None, order_id=None, include_missing=True):
    """(minx,miny,maxx,maxy) tile bbox of a query result, or None if it's empty."""
    recs = query(role=role, phase=phase, order_id=order_id, include_missing=include_missing)
    if not recs:
        return None
    xs = [r["tile_pos"][0] for r in recs]
    ys = [r["tile_pos"][1] for r in recs]
    return (min(xs), min(ys), max(xs), max(ys))


# --------------------------------------------------------------------------- patches
def record_patch(ore, x, y, per_tile_density):
    """Track an ore patch anchor (richest-spot tile + its 5x5 per-tile density). One record
    per (ore,x,y); re-recording updates the density (patches deplete)."""
    db = _load()
    lst = db["patches"].setdefault(ore, [])
    for p in lst:
        if p["x"] == int(x) and p["y"] == int(y):
            p["per_tile_density"] = per_tile_density
            p["ts"] = time.time()
            break
    else:
        lst.append({"x": int(x), "y": int(y), "per_tile_density": per_tile_density, "ts": time.time()})
    _save(db)


def patches(ore):
    return [dict(p) for p in _load()["patches"].get(ore, [])]


# --------------------------------------------------------------------------- live reads
def _chunked(build_lua):
    """rcon.read_chunked on a PRIVATE buffer key. `build_lua(store)` returns the Lua body.

    storage._world was shared with mine_layout.scan_patch, and the two use INCOMPATIBLE wire
    formats - JSON here, newline-joined text there. A mid-read clobber between them does not
    merely fail to parse; it can parse into a plausible-but-wrong ore patch and put drills on
    the wrong tiles. Hence one minted key per read (see rcon.read_chunked).
    """
    return rcon.read_chunked(lambda store: "/sc " + build_lua(store),
                             chunk=CHUNK, empty="[]")


def _store_lua(store):
    """The tail every scan here shares: park `out` as JSON in the minted buffer, print its
    length. An empty result is the literal '[]' so the length is never 0 for a real read."""
    return ("if #out==0 then %s='[]' else %s=helpers.table_to_json(out) end;"
            "rcon.print(#%s)" % (store, store, store))


def _names_lua(names):
    if not names:
        return "nil"
    return "{" + ",".join("['%s']=true" % n for n in sorted(set(names))) + "}"


def scan_area(x1, y1, x2, y2, names=None):
    """Read live player entities in a tile bbox -> [{n,x,y,d}] with x,y = floor(center).
    RCON READ ONLY. `names` optionally restricts to a set of entity names."""
    lua = (
        "local s=game.surfaces[1]; local NM=" + _names_lua(names) + "; local out={};"
        "for _,e in pairs(s.find_entities_filtered{area={{%d,%d},{%d,%d}},force='player'}) do"
        % (int(x1), int(y1), int(x2), int(y2)) +
        "  local n=e.name;"
        "  if n~='character' and n~='character-corpse' and (NM==nil or NM[n]) then"
        "    local okd,d=pcall(function() return e.direction end);"
        "    out[#out+1]={n=n,x=math.floor(e.position.x),y=math.floor(e.position.y),d=(okd and tonumber(d)) or 0}"
        "  end end;"
    )
    return json.loads(_chunked(lambda store: lua + _store_lua(store)))


def scan_tiles(tiles, names):
    """Probe specific tiles for entities of the given names -> [{n,x,y,d}]. Used to verify +
    collect a belt path's actual tiles. RCON READ ONLY."""
    if not tiles:
        return []
    spec = ";".join("%d,%d" % (int(x), int(y)) for (x, y) in tiles)
    nm = "{" + ",".join("'%s'" % n for n in names) + "}"
    lua = (
        "local s=game.surfaces[1]; local NM=" + nm + "; local out={};"
        "for a,b in ([==[" + spec + "]==]):gmatch('(-?%d+),(-?%d+)') do"
        "  local x,y=tonumber(a),tonumber(b);"
        "  local e=s.find_entities_filtered{position={x+0.5,y+0.5},radius=0.6,name=NM}[1];"
        "  if e then local okd,d=pcall(function() return e.direction end);"
        "    out[#out+1]={n=e.name,x=math.floor(e.position.x),y=math.floor(e.position.y),d=(okd and tonumber(d)) or 0} end end;"
    )
    return json.loads(_chunked(lambda store: lua + _store_lua(store)))


def reconcile(pad=3, tol=1.5):
    """Scan the live surface over the registry's bbox and flag registry entries that no longer
    exist in-game as missing=True (never auto-delete — GOTCHAS: teardown is surgical and
    deliberate). Entries found again get missing=False. Returns
    {checked, missing:[uids], recovered:[uids]}."""
    db = _load()
    recs = list(db["entities"].values())
    if not recs:
        return {"checked": 0, "missing": [], "recovered": []}
    xs = [r["tile_pos"][0] for r in recs]
    ys = [r["tile_pos"][1] for r in recs]
    live = scan_area(min(xs) - pad, min(ys) - pad, max(xs) + pad + 1, max(ys) + pad + 1,
                     names={r["name"] for r in recs})
    by_name = {}
    for e in live:
        by_name.setdefault(e["n"], []).append([e["x"], e["y"], False])   # x, y, used
    went_missing, recovered = [], []
    for r in recs:
        rx, ry = r["tile_pos"]
        hit = None
        for cand in by_name.get(r["name"], []):
            if not cand[2] and abs(cand[0] - rx) <= tol and abs(cand[1] - ry) <= tol:
                hit = cand
                break
        if hit:
            hit[2] = True
            if r.get("missing"):
                r["missing"] = False
                recovered.append(r["uid"])
        elif not r.get("missing"):
            r["missing"] = True
            went_missing.append(r["uid"])
    _save(db)
    return {"checked": len(recs), "missing": went_missing, "recovered": recovered}
