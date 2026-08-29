#!/usr/bin/env python3
"""Blueprint pipeline: fetch / verify / transform / stamp (MEGABASE-V2-DESIGN.md §4).

Mac-side curation tool + library. The server never fetches; strings are fetched here,
verified 2.x, transformed (snap-to-grid stripped per GOTCHAS "Megabase ghost placement"),
and saved under blueprints/library/ with meta sidecars. stamp_lua() only BUILDS the /sc
command strings for surface.create_entities_from_blueprint_string — it never talks RCON;
the executor runs them.

Blueprint string format: "0" version byte + base64(zlib(json)).

Usage:
    python3 bplib.py catalog              print the library catalog
    python3 bplib.py decode <name|file>   dump decoded JSON of a library entry or .txt file

Fetch sources:
    factorio.school   GET /api/blueprint/<key>            (.blueprintString.blueprintString)
    factoriobin       post page -> cdn perma /fbin-*-0.txt link -> GET
    factorioprints    facorio-blueprints.firebaseio.com/blueprints/<key>.json
                      (yes, the typo'd host is real; strip the huge `favorites` map)
"""
import base64
import json
import pathlib
import re
import time
import urllib.request
import zlib

try:                                    # optional; decode/encode never need it
    import draftsman                    # noqa: F401
    HAVE_DRAFTSMAN = True
except ImportError:
    HAVE_DRAFTSMAN = False

HERE = pathlib.Path(__file__).resolve().parent
LIBRARY = HERE / "blueprints" / "library"

# GOTCHAS "RCON client protocol": >~4KB in one packet gets truncated/lost. Keep every
# /sc command comfortably under that; big strings go through storage._bp chunk-append.
MAX_CMD = 4096
CHUNK = 3000

# GOTCHAS "Megabase ghost placement" rule 3: these keys snap the BP to a fixed world grid
# and collide with already-placed blocks; pop them to place at an exact spot.
SNAP_KEYS = ("snap-to-grid", "absolute-snapping", "position-relative-to-grid")

USER_AGENT = "abyss-factorio-bot/1.0 (blueprint curation; contact: sutonimh)"


# ---------------------------------------------------------------- encode / decode

def decode(bp_string):
    """Blueprint exchange string -> dict. base64 -> zlib -> JSON, skipping the leading
    version byte (always "0" for the 1.x/2.x format)."""
    bp_string = bp_string.strip()
    if not bp_string:
        raise ValueError("empty blueprint string")
    if bp_string[0] != "0":
        raise ValueError("unknown blueprint-string version byte %r" % bp_string[0])
    return json.loads(zlib.decompress(base64.b64decode(bp_string[1:])))


def encode(bp_dict):
    """dict -> blueprint exchange string (compact JSON -> zlib -> base64, "0" prefix)."""
    raw = json.dumps(bp_dict, separators=(",", ":")).encode()
    return "0" + base64.b64encode(zlib.compress(raw, 9)).decode()


def _inner(bp_dict):
    """Unwrap {"blueprint": {...}} / {"blueprint_book": {...}} (or pass a bare inner
    dict through). Returns (kind, inner) where kind is the wrapper key or None."""
    for kind in ("blueprint_book", "blueprint", "upgrade_planner", "deconstruction_planner"):
        if kind in bp_dict:
            return kind, bp_dict[kind]
    return None, bp_dict


def game_version(bp_dict):
    """(major, minor) from the version u64. Works for a bare blueprint and for a
    blueprint_book (recursing to the first child when the book itself has no version)."""
    _, inner = _inner(bp_dict)
    v = inner.get("version")
    if v is None:
        for child in inner.get("blueprints", []):
            try:
                return game_version(child)
            except ValueError:
                continue
        raise ValueError("no version field found in blueprint or its children")
    return (v >> 48) & 0xFFFF, (v >> 32) & 0xFFFF


def verify_2x(bp_string):
    """Raise unless the string decodes to a 2.x blueprint. 1.1 rail BPs are DEAD in 2.x
    (new rail geometry) and 1.1 non-rail stamps degrade silently -- nothing pre-2.0 is
    admitted to the library. Returns the decoded dict on success."""
    bp = decode(bp_string)
    major, minor = game_version(bp)
    if major != 2:
        raise ValueError(
            "blueprint is game version %d.%d, not 2.x -- refusing to admit it to the "
            "library (1.1 rails are dead in 2.x; 1.1 stamps degrade silently)" % (major, minor))
    return bp


# ---------------------------------------------------------------- fetchers (Mac-side)

def _http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_school(key):
    """factorio.school REST. Returns (bp_string, meta_dict)."""
    url = "https://www.factorio.school/api/blueprint/%s" % key
    data = json.loads(_http_get(url))
    bp_string = data["blueprintString"]["blueprintString"]
    meta = {"source_url": "https://factorio.school/view/%s" % key, "key": key}
    if data.get("title"):
        meta["label"] = data["title"]
    return bp_string, meta


def fetch_factoriobin(post_id):
    """FactorioBin: scrape the post page for the cdn.factoriobin.com perma
    /fbin-...-0.txt link, then GET the raw string. Returns (bp_string, meta_dict)."""
    page_url = "https://factoriobin.com/post/%s" % post_id
    page = _http_get(page_url)
    m = re.search(r'https://cdn\.factoriobin\.com/[^\s"\']*fbin-[^\s"\']*-0\.txt', page)
    if not m:
        raise ValueError("no cdn.factoriobin.com fbin-*-0.txt link on %s" % page_url)
    bp_string = _http_get(m.group(0)).strip()
    return bp_string, {"source_url": page_url, "key": post_id}


def fetch_firebase(key):
    """factorioprints Firebase fallback (typo'd host is the real one). Returns
    (bp_string, meta_dict) with the huge `favorites` map stripped from the metadata."""
    url = "https://facorio-blueprints.firebaseio.com/blueprints/%s.json" % key
    data = json.loads(_http_get(url))
    if not data:
        raise ValueError("firebase has no blueprint under key %s" % key)
    bp_string = data["blueprintString"]
    data.pop("favorites", None)
    meta = {"source_url": "https://factorioprints.com/view/%s" % key, "key": key}
    if data.get("title"):
        meta["label"] = data["title"]
    return bp_string, meta


# ---------------------------------------------------------------- transforms

def strip_snap(bp_dict):
    """Remove snap-to-grid / absolute-snapping / position-relative-to-grid (recursing
    into books) so the BP places at exact coords instead of snapping to a world grid
    (GOTCHAS "Megabase ghost placement" rule 3). Mutates and returns bp_dict."""
    kind, inner = _inner(bp_dict)
    for k in SNAP_KEYS:
        inner.pop(k, None)
    if kind == "blueprint_book":
        for child in inner.get("blueprints", []):
            strip_snap(child)
    return bp_dict


def book_children(book_dict):
    """[(label, child_dict), ...] for a blueprint_book. child_dict keeps its wrapper
    ({"blueprint": ...} / nested {"blueprint_book": ...}) minus the book's "index" key,
    so encode(child_dict) is directly usable."""
    kind, inner = _inner(book_dict)
    if kind != "blueprint_book":
        raise ValueError("not a blueprint_book")
    out = []
    for child in inner.get("blueprints", []):
        child = {k: v for k, v in child.items() if k != "index"}
        _, cinner = _inner(child)
        out.append((cinner.get("label", ""), child))
    return out


def _stats(bp_dict):
    """(entity_count, footprint_wh) — entity count from the entities array(s), footprint
    = bbox of entity positions. Books: summed count, no single footprint."""
    kind, inner = _inner(bp_dict)
    if kind == "blueprint_book":
        total = sum(_stats(c)[0] for c in inner.get("blueprints", []))
        return total, None
    ents = inner.get("entities", [])
    if not ents:
        return 0, None
    xs = [e["position"]["x"] for e in ents]
    ys = [e["position"]["y"] for e in ents]
    return len(ents), [round(max(xs) - min(xs), 2), round(max(ys) - min(ys), 2)]


# ---------------------------------------------------------------- library

def _slug(name):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def save(name, bp_string, meta=None):
    """Write blueprints/library/<name>.txt + <name>.meta.json. Verifies 2.x first.
    Returns the merged meta dict."""
    name = _slug(name)
    bp = verify_2x(bp_string)
    major, minor = game_version(bp)
    count, footprint = _stats(bp)
    _, inner = _inner(bp)
    out = dict(meta or {})
    out.setdefault("label", inner.get("label", ""))
    out.update({
        "fetched": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "game_version": "%d.%d" % (major, minor),
        "entity_count": count,
        "footprint_wh": footprint,
    })
    LIBRARY.mkdir(parents=True, exist_ok=True)
    (LIBRARY / (name + ".txt")).write_text(bp_string.strip() + "\n")
    (LIBRARY / (name + ".meta.json")).write_text(json.dumps(out, indent=2) + "\n")
    return out


def load(name):
    """Library entry -> (bp_string, meta_dict)."""
    name = _slug(name)
    bp_string = (LIBRARY / (name + ".txt")).read_text().strip()
    meta_path = LIBRARY / (name + ".meta.json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    return bp_string, meta


def catalog():
    """[{name, **meta}, ...] for every library entry, sorted by name."""
    out = []
    for p in sorted(LIBRARY.glob("*.meta.json")):
        entry = {"name": p.name[:-len(".meta.json")]}
        entry.update(json.loads(p.read_text()))
        out.append(entry)
    return out


# ---------------------------------------------------------------- stamping

def _lua_stamp(bp_expr, x, y, direction=None):
    d = ", direction=%d" % direction if direction is not None else ""
    return ("/sc local s=game.surfaces[1]; "
            "local g=s.create_entities_from_blueprint_string{string=%s, "
            "position={x=%s, y=%s}, force='player'%s}; "
            "storage._bp=nil; rcon.print(#g)" % (bp_expr, x, y, d))


def stamp_lua(bp_string, x, y, direction=None):
    """Build the /sc command(s) that ghost-stamp bp_string at (x, y) headless via
    surface.create_entities_from_blueprint_string (2.1 API, force='player').

    Returns a LIST of /sc command strings for the executor to run IN ORDER over RCON —
    this function never executes anything. A string that fits in one <4KB packet yields
    a single command; a bigger one yields the storage-chunk pattern (GOTCHAS "RCON
    client protocol"): reset storage._bp, append it in slices, then stamp from storage.

    Caller is responsible for the GOTCHAS placement rules FIRST: chunks generated,
    terrain cleared (trees/rocks destroyed, cliffs = move the site), snap keys stripped
    when placing at exact coords.
    """
    bp_string = bp_string.strip()
    single = _lua_stamp("'%s'" % bp_string, x, y, direction)
    if len(single) <= MAX_CMD:
        return [single]
    cmds = ["/sc storage._bp=''"]
    for i in range(0, len(bp_string), CHUNK):
        cmds.append("/sc storage._bp=storage._bp..'%s'" % bp_string[i:i + CHUNK])
    cmds.append(_lua_stamp("storage._bp", x, y, direction))
    return cmds


# ---------------------------------------------------------------- CLI

def _main(argv):
    if argv[:1] == ["catalog"]:
        for e in catalog():
            print("%-45s v%-6s %6s ents  %s  %s" % (
                e["name"], e.get("game_version", "?"), e.get("entity_count", "?"),
                str(e.get("footprint_wh")), e.get("label", "")))
    elif argv[:1] == ["decode"] and len(argv) == 2:
        target = pathlib.Path(argv[1])
        s = target.read_text() if target.exists() else load(argv[1])[0]
        print(json.dumps(decode(s), indent=2))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
