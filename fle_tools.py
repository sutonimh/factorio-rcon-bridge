#!/usr/bin/env python3
"""FLE-derived building tools: connect (belt/pipe/pole) + nearest_buildable over RCON.

The Lua side lives in lua/fle_lib.lua, vendored from the Factorio Learning
Environment (https://github.com/JackHopkins/factorio-learning-environment, MIT,
commit f748ec452dfa79f6a57a12ddcff1ff9102cdb11f) — see LUA-VENDORING.md for what
was taken and every adaptation.

Load model (mind the ~4KB RCON command limit, per GOTCHAS "RCON client protocol"):
  * fle_lib.lua is split on `-- @chunk <name>` markers; init() sends each chunk
    as ONE /sc command. Functions land in the plain Lua global `fle` (NOT storage:
    Factorio can't serialize functions in storage — it would break autosaves).
  * The global survives across /sc calls for the server-process lifetime; every
    wrapper call probes `fle.VERSION` and re-pushes automatically after a server
    restart or save reload.
  * Results come back as JSON via storage.fle_out (a string) read in slices —
    the architect.py chunked-read pattern (each slice .rstrip'd: rcon.print
    appends a newline per response).

Belt conventions (GOTCHAS): routes are DIRECT L-paths; existing belts are crossed
with underground belts (never overwritten, never detoured around); buildings are
hard obstacles that get bridged underground on straight runs or reported as gaps.

Usage:
    python3 fle_tools.py selftest                     # offline: chunk-split dry run
    python3 fle_tools.py init [--force]               # push the lua into the game
    python3 fle_tools.py connect x1 y1 x2 y2 kind     # kind: belt | pipe | pole
    python3 fle_tools.py nearest name x y             # nearest buildable origin
"""
import json
import pathlib
import re
import sys

import rcon  # reuse the client in this dir

HERE = pathlib.Path(__file__).resolve().parent
LIB = HERE / "lua" / "fle_lib.lua"
CHUNK_LIMIT = 3500   # bytes per /sc command; Factorio truncates/loses ~4KB commands
READ_CHUNK = 3000    # chars per chunked storage read (architect.py precedent)

KINDS = {"belt": "transport-belt", "pipe": "pipe", "pole": "small-electric-pole"}


# ----------------------------------------------------------------------------- split
def split_lua(text, limit=CHUNK_LIMIT):
    """Split fle_lib.lua into (name, code) chunks on `-- @chunk <name>` markers.

    Everything before the first marker (the license header) is NOT sent to the
    game. Each chunk must be a self-contained Lua statement sequence (they run as
    separate /sc commands, so locals don't carry over — every chunk re-derives
    `local F = fle`). Raises if any chunk would exceed the RCON command limit.
    Pure function: unit-testable without RCON (see selftest())."""
    chunks = []
    name, buf = None, []
    for line in text.splitlines():
        m = re.match(r"--\s*@chunk\s+(\S+)", line)
        if m:
            if name:
                chunks.append((name, "\n".join(buf).strip()))
            name, buf = m.group(1), []
        elif name is not None:
            buf.append(line)
    if name:
        chunks.append((name, "\n".join(buf).strip()))
    if not chunks:
        raise ValueError(f"no -- @chunk markers found in {LIB}")
    for cname, code in chunks:
        size = len(("/sc " + code).encode("utf-8"))
        if size > limit:
            raise ValueError(f"chunk '{cname}' is {size}B > {limit}B — split it in fle_lib.lua")
        if not code:
            raise ValueError(f"chunk '{cname}' is empty")
    return chunks


def lib_version(text=None):
    """The F.VERSION declared in fle_lib.lua (bump it there on any change)."""
    text = text if text is not None else LIB.read_text()
    m = re.search(r"F\.VERSION\s*=\s*(\d+)", text)
    if not m:
        raise ValueError("no F.VERSION in fle_lib.lua")
    return int(m.group(1))


# ----------------------------------------------------------------------------- load
def _probe():
    """The in-game fle version, or None if not loaded (server restarted, etc.)."""
    out = rcon.run("/sc rcon.print(fle and fle.VERSION or 'nil')").strip()
    return int(out) if out.isdigit() else None


def init(force=False):
    """Push lua/fle_lib.lua into the game as /sc chunks. Idempotent: skips when the
    in-game version already matches the file (force=True re-pushes regardless).
    Returns True if it pushed, False if already current."""
    text = LIB.read_text()
    want = lib_version(text)
    if not force and _probe() == want:
        return False
    for cname, code in split_lua(text):
        out = rcon.run("/sc " + code).strip()
        if out:  # silent commands answer empty; anything else is a Lua error
            raise RuntimeError(f"fle_lib chunk '{cname}' errored: {out}")
    got = _probe()
    if got != want:
        raise RuntimeError(f"fle_lib load verify failed: in-game version {got}, want {want}")
    return True


def _ensure():
    if _probe() != lib_version():
        init(force=True)


def _call(expr):
    """Evaluate a Lua expression returning a table: fle.out() stores its JSON in
    storage.fle_out and prints the length; read it back in READ_CHUNK slices
    (GOTCHAS: a single large RCON response gets truncated; rcon.print appends a
    trailing newline per response, so rstrip each slice)."""
    _ensure()
    out = rcon.run(f"/sc fle.out({expr})").strip()
    if not out.isdigit():
        raise RuntimeError(f"fle call errored: {out or '(empty)'}  [{expr}]")
    n = int(out)
    parts, i = [], 1
    while i <= n:
        parts.append(rcon.run(f"/sc rcon.print(storage.fle_out:sub({i},{i + READ_CHUNK - 1}))").rstrip("\r\n"))
        i += READ_CHUNK
    return json.loads("".join(parts))


# ----------------------------------------------------------------------------- api
def connect(a_pos, b_pos, kind, dry_run=False, name=None):
    """Auto-route from tile a_pos=(x,y) to tile b_pos=(x,y). kind in {belt, pipe,
    pole}. Coordinates are integer TILE coords (entity centers land at +0.5).
    Returns the parsed result: {placed, gaps, connected, entities, ...}.
      belt: direct L-path of transport-belt; existing collinear same-direction
            belts are adopted; crossing belts and buildings are bridged with
            underground-belt pairs on straight runs (else counted in gaps);
            connected verified by belt_neighbours BFS.
      pipe: same layer with pipe / pipe-to-ground (range 8); 2.1 has no fluidbox
            API, so connected just means gaps==0 — verify flow with fluid_probe().
      pole: poles stepped at wire reach along the line, skipping saturated spots,
            stopping early once both ends share an electric network.
    dry_run counts placements without touching the world (pole dry runs report
    connected=False)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}, got {kind!r}")
    ax, ay = int(a_pos[0]), int(a_pos[1])
    bx, by = int(b_pos[0]), int(b_pos[1])
    ename = name or KINDS[kind]
    dry = "true" if dry_run else "false"
    return _call(f"fle.connect({ax},{ay},{bx},{by},'{kind}','{ename}',{dry})")


def nearest_buildable(name, near_x, near_y, max_radius=30):
    """Spiral out from (near_x, near_y) to the nearest origin where `name` fits:
    no water/buildings in its collision box, full resource coverage for drills,
    crude-oil for pumpjacks, and a final can_place_entity guard. Returns
    {found, x, y, left_top, right_bottom} (or {found: False, error})."""
    return _call(f"fle.nearest_buildable('{name}',{int(near_x)},{int(near_y)},{int(max_radius)})")


def fluid_probe(x, y, fluid="water"):
    """Live fluid check at a tile: get_fluid_count on the pipe/tank/boiler there
    (the 2.1-safe way to verify a pipe run actually flows — settle ~2-3s first,
    per GOTCHAS 'Fluid verification')."""
    return _call(f"fle.fluid_probe({int(x)},{int(y)},'{fluid}')")


# ----------------------------------------------------------------------------- cli
def selftest():
    """Offline validation: split the real file, check sizes/names, and exercise the
    splitter's error paths on synthetic input. No RCON traffic."""
    text = LIB.read_text()
    chunks = split_lua(text)
    print(f"fle_lib.lua v{lib_version(text)}: {len(chunks)} chunks")
    for cname, code in chunks:
        print(f"  {cname:<10} {len(('/sc ' + code).encode()):>5}B")
    assert [c[0] for c in chunks] == ["core", "placeable", "path", "lay",
                                      "beltcheck", "poles", "nearest", "api",
                                      "travelreq", "travelq", "travelstep",
                                      "travelinit"], "chunk order changed"
    assert all("storage.fle " not in code for _, code in chunks), \
        "functions must live in the `fle` global, never storage (saves break)"
    try:
        split_lua("-- @chunk big\n" + "x = 1\n" * 2000)
        raise AssertionError("oversize chunk not rejected")
    except ValueError:
        pass
    try:
        split_lua("print(1)")
        raise AssertionError("markerless file not rejected")
    except ValueError:
        pass
    print("selftest OK")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "selftest":
        selftest()
    elif cmd == "init":
        print("pushed" if init(force="--force" in sys.argv) else "already current")
    elif cmd == "connect":
        x1, y1, x2, y2 = (int(v) for v in sys.argv[2:6])
        print(json.dumps(connect((x1, y1), (x2, y2), sys.argv[6],
                                 dry_run="--dry" in sys.argv), indent=2))
    elif cmd == "nearest":
        print(json.dumps(nearest_buildable(sys.argv[2], int(sys.argv[3]), int(sys.argv[4])), indent=2))
    else:
        print(__doc__)
        sys.exit(2)
