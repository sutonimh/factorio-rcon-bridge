#!/usr/bin/env python3
"""Offline tests for bplib.py — no network, no RCON, no library files touched.

Run: python3 test_bplib.py   (or: python3 -m pytest test_bplib.py)
"""
import bplib

V2_0_15 = (2 << 48) | (0 << 32) | (15 << 16)   # major.minor.patch in the u64
V1_1_110 = (1 << 48) | (1 << 32) | (110 << 16)


def _bp(version=V2_0_15, label="test", entities=None, extra=None):
    inner = {
        "item": "blueprint",
        "label": label,
        "version": version,
        "entities": entities if entities is not None else [
            {"entity_number": 1, "name": "transport-belt", "position": {"x": 0.5, "y": 0.5}},
            {"entity_number": 2, "name": "inserter", "position": {"x": 3.5, "y": 2.5}},
        ],
    }
    inner.update(extra or {})
    return {"blueprint": inner}


def test_roundtrip():
    bp = _bp()
    s = bplib.encode(bp)
    assert s[0] == "0"
    assert bplib.decode(s) == bp


def test_game_version():
    assert bplib.game_version(_bp()) == (2, 0)
    assert bplib.game_version(_bp(version=V1_1_110)) == (1, 1)
    # bare inner dict (no wrapper)
    assert bplib.game_version(_bp()["blueprint"]) == (2, 0)
    # book with its own version
    book = {"blueprint_book": {"label": "b", "version": V2_0_15, "blueprints": [_bp()]}}
    assert bplib.game_version(book) == (2, 0)
    # book without a version recurses to first child
    book = {"blueprint_book": {"label": "b", "blueprints": [_bp(version=V2_0_15)]}}
    assert bplib.game_version(book) == (2, 0)


def test_verify_2x():
    bplib.verify_2x(bplib.encode(_bp()))
    try:
        bplib.verify_2x(bplib.encode(_bp(version=V1_1_110)))
    except ValueError as e:
        assert "not 2.x" in str(e)
    else:
        raise AssertionError("1.1 blueprint was admitted")


def test_strip_snap():
    bp = _bp(extra={"snap-to-grid": {"x": 100, "y": 100}, "absolute-snapping": True,
                    "position-relative-to-grid": {"x": 4, "y": 4}})
    book = {"blueprint_book": {"version": V2_0_15, "blueprints": [bp]}}
    bplib.strip_snap(book)
    inner = book["blueprint_book"]["blueprints"][0]["blueprint"]
    for k in bplib.SNAP_KEYS:
        assert k not in inner, k
    assert inner["label"] == "test"          # everything else untouched
    assert len(inner["entities"]) == 2


def test_book_children():
    book = {"blueprint_book": {"version": V2_0_15, "blueprints": [
        dict(_bp(label="alpha"), index=0),
        dict(_bp(label="beta"), index=1),
    ]}}
    kids = bplib.book_children(book)
    assert [k[0] for k in kids] == ["alpha", "beta"]
    assert all("index" not in k[1] for k in kids)
    bplib.decode(bplib.encode(kids[0][1]))   # children are directly encodable


def test_stats():
    count, wh = bplib._stats(_bp())
    assert count == 2 and wh == [3.0, 2.0]
    book = {"blueprint_book": {"blueprints": [_bp(), _bp()]}}
    assert bplib._stats(book) == (4, None)


def test_stamp_lua_single():
    cmds = bplib.stamp_lua(bplib.encode(_bp()), 12, -34)
    assert len(cmds) == 1
    c = cmds[0]
    assert c.startswith("/sc ") and len(c) <= bplib.MAX_CMD
    assert "create_entities_from_blueprint_string" in c
    assert "force='player'" in c
    assert "position={x=12, y=-34}" in c
    assert "direction" not in c
    assert "direction=4" in bplib.stamp_lua(bplib.encode(_bp()), 0, 0, direction=4)[0]


def test_stamp_lua_chunked():
    # a huge blueprint must go through the storage._bp chunk-append pattern
    # (GOTCHAS "RCON client protocol": >~4KB in one packet is truncated/lost)
    ents = [{"entity_number": i + 1, "name": "stone-wall-%d" % i,
             "position": {"x": float(i), "y": 0.5}} for i in range(2000)]
    s = bplib.encode(_bp(entities=ents))
    assert len(s) > bplib.MAX_CMD
    cmds = bplib.stamp_lua(s, 5, 6)
    assert len(cmds) >= 3
    assert cmds[0] == "/sc storage._bp=''"
    for c in cmds:
        assert len(c) <= bplib.MAX_CMD, "command over the 4KB RCON safety limit"
    for c in cmds[1:-1]:
        assert c.startswith("/sc storage._bp=storage._bp..'")
    assert "".join(  # reassembled payload is byte-identical to the original string
        c[len("/sc storage._bp=storage._bp..'"):-1] for c in cmds[1:-1]) == s
    last = cmds[-1]
    assert "create_entities_from_blueprint_string" in last
    assert "string=storage._bp" in last and "storage._bp=nil" in last
    assert "position={x=5, y=6}" in last and "force='player'" in last


def test_slug():
    assert bplib._slug("Nauvis Starter Base (v2.0)") == "nauvis-starter-base-v2-0"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except Exception as e:
            failed += 1
            print("FAIL %s: %r" % (fn.__name__, e))
    sys.exit(1 if failed else 0)
