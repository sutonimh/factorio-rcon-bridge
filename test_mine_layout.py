#!/usr/bin/env python3
"""Offline unit tests for mine_layout.py - NO live server.

Run with:
    python3 test_mine_layout.py

The drop-position expectations are the engine-probed vectors, cross-checked live against
16 real drills on the 2.1.17 server (read-only) before these tests were written. scan_patch
is driven by a scripted FakeRcon that serves the chunked storage._world protocol, in the
test_world_executor.py style; nothing here opens a socket.
"""
import itertools
import re
import traceback

import executor
import mine_layout as M
import rcon


# --------------------------------------------------------------------------- harness
class FakeRcon:
    """Scripted rcon.run plus native handling of the chunked storage._world read."""

    def __init__(self, script=(), payload=None):
        self.script = list(script)
        self.calls = []
        self.payload = payload

    def __call__(self, cmd, timeout=10.0):
        self.calls.append(cmd)
        # the buffer key is minted per read (rcon.read_chunked), so match ANY scratch
        m = re.search(r"storage\._\w+:sub\((\d+),(\d+)\)", cmd)
        if m:
            i, j = int(m.group(1)), int(m.group(2))
            return self.payload[i - 1:j] + "\n"
        if re.search(r"storage\._\w+=nil", cmd):
            return ""                      # read_chunked clears its scratch key in a finally
        if not self.script:
            raise AssertionError("unexpected RCON call (script exhausted): %s" % cmd[:160])
        sub, resp = self.script.pop(0)
        assert sub in cmd, "expected %r in RCON cmd, got: %s" % (sub, cmd[:200])
        return resp(cmd) if callable(resp) else resp


def rect_patch(ore, x1, y1, x2, y2, foreign=None):
    tiles = {(x, y): 1000 for x in range(x1, x2 + 1) for y in range(y1, y2 + 1)}
    return {"ore": ore, "tiles": tiles, "bbox": (x1, y1, x2, y2), "foreign": foreign or {}}


DENSE = rect_patch("iron-ore", 0, 0, 19, 11)      # 20 x 12


def roles(plan, role):
    return [e for e in plan["entities"] if e["role"] == role]


# --------------------------------------------------------------------------- geometry
def test_drop_tile_matches_probed_vectors():
    """burner vec (-0.35,-1.30), electric (0,-1.85), big (0,-2.85); centre = tile + tw/2."""
    b = "burner-mining-drill"
    assert M.drop_tile(b, 0, 0, M.N) == (0, -1)      # centre (1,1) + (-0.35,-1.30)
    assert M.drop_tile(b, 0, 0, M.E) == (2, 0)       # + ( 1.30,-0.35)
    assert M.drop_tile(b, 0, 0, M.S) == (1, 2)       # + ( 0.35, 1.30)
    assert M.drop_tile(b, 0, 0, M.W) == (-1, 1)      # + (-1.30, 0.35)
    e = "electric-mining-drill"
    assert M.drop_tile(e, 0, 0, M.N) == (1, -1)      # centre (1.5,1.5): odd tw -> .5
    assert M.drop_tile(e, 0, 0, M.S) == (1, 3)
    assert M.drop_tile("big-mining-drill", 0, 0, M.N) == (2, -1)
    assert M.drop_tile("big-mining-drill", 0, 0, M.S) == (2, 5)
    # verified against the live server before this test was written
    assert M.drop_tile(b, -44, 13, M.S) == (-43, 15)
    assert M.drop_tile(e, -44, 12, M.S) == (-43, 15)
    assert M.drop_tile(e, -44, 16, M.N) == (-43, 15)
    # the COLUMN rule is tier-dependent: a burner drops left facing north, right facing
    # south; electric/big drop on their centre column. This is half of the copper failure.
    assert M.drop_tile(b, 0, 0, M.N)[0] == 0 and M.drop_tile(b, 0, 0, M.S)[0] == 1
    assert M.drop_tile(e, 0, 0, M.N)[0] == M.drop_tile(e, 0, 0, M.S)[0]
    for bad in (1, 2, 6, 15):
        try:
            M.drop_tile(e, 0, 0, bad)
            raise AssertionError("direction %d should be rejected" % bad)
        except ValueError:
            pass


def test_mining_area_and_centers():
    assert M.mining_area("burner-mining-drill", 0, 0) == (0, 0, 1, 1)          # 2x2
    assert M.mining_area("electric-mining-drill", 0, 0) == (-1, -1, 3, 3)      # 5x5
    assert M.mining_area("big-mining-drill", 0, 0) == (-4, -4, 8, 8)           # 13x13
    assert M.center("transport-belt", 5, 7) == (5.5, 7.5)
    assert M.center("burner-mining-drill", 5, 7) == (6.0, 8.0)
    assert M.center("electric-mining-drill", 5, 7) == (6.5, 8.5)


# --------------------------------------------------------------------------- the plan
def test_every_drop_lands_on_the_lane():
    p = M.plan_outpost(DENSE)
    ds = roles(p, "drill")
    assert len(ds) >= 8, len(ds)
    for e in ds:
        d = M.drop_tile(p["drill"], e["x"], e["y"], e["direction"])
        assert d in p["lane_tiles"], "%s at (%d,%d) dir %d drops on %s" % (
            e["entity"], e["x"], e["y"], e["direction"], d)
        assert d[1] == p["lane_y"]
    # the paired-row rule from mineore's calculator: top faces south, bottom faces north
    assert {e["direction"] for e in ds if e["side"] == "top"} == {M.S}
    assert {e["direction"] for e in ds if e["side"] == "bottom"} == {M.N}
    th = M.DRILLS[p["drill"]]["th"]
    assert {e["y"] for e in ds if e["side"] == "top"} == {p["lane_y"] - th}
    assert {e["y"] for e in ds if e["side"] == "bottom"} == {p["lane_y"] + 1}
    assert M.validate(p)["ok"], M.validate(p)["errors"]


def test_poles_never_on_the_lane_and_nothing_overlaps():
    p = M.plan_outpost(DENSE)
    ps = roles(p, "pole")
    assert ps, "expected poles for an electric mine"
    dfp = set()
    for e in roles(p, "drill"):
        dfp |= M.footprint(e["entity"], e["x"], e["y"])
    for e in ps:
        fp = M.footprint(e["entity"], e["x"], e["y"])
        assert not (fp & p["lane_tiles"]), "pole on the lane at (%d,%d)" % (e["x"], e["y"])
        assert not (fp & dfp), "pole over a drill at (%d,%d)" % (e["x"], e["y"])
    used = set()
    for e in p["entities"]:
        fp = M.footprint(e["entity"], e["x"], e["y"])
        assert not (fp & used), "footprint collision: %s at (%d,%d)" % (e["entity"], e["x"], e["y"])
        used |= fp
    # one electric network, and every drill inside some pole's supply area
    assert len(M._components(ps, M.POLES[p["pole"]]["wire"])) == 1
    assert not p["warnings"], p["warnings"]


def test_lane_contiguous_and_output_hooked_up():
    p = M.plan_outpost(DENSE)
    s, e = p["lane_span"]
    xs = sorted(q["x"] for q in roles(p, "lane"))
    assert xs == list(range(s, e + 1)), xs
    assert all(q["y"] == p["lane_y"] and q["direction"] == M.E for q in roles(p, "lane"))
    drops = sorted(M.drop_tile(p["drill"], q["x"], q["y"], q["direction"])[0]
                   for q in roles(p, "drill"))
    assert s == drops[0] - 1 and e == drops[-1]
    out = sorted(roles(p, "output"), key=lambda q: q["x"])
    assert [q["entity"] for q in out] == ["inserter", "wooden-chest"]
    # inserter direction is its PICKUP side: 12/west picks off the belt, drops east
    assert out[0] == {"entity": "inserter", "x": e + 1, "y": p["lane_y"],
                      "direction": M.W, "role": "output"}
    assert (out[1]["x"], out[1]["y"]) == (e + 2, p["lane_y"])
    assert roles(M.plan_outpost(DENSE, output=None), "output") == []


def test_bom_matches_entity_count():
    p = M.plan_outpost(DENSE)
    counts = {}
    for e in p["entities"]:
        counts[e["entity"]] = counts.get(e["entity"], 0) + 1
    assert p["bom"] == counts, (p["bom"], counts)
    assert sum(p["bom"].values()) == len(p["entities"])
    assert p["bom"]["transport-belt"] == p["lane_span"][1] - p["lane_span"][0] + 1
    assert M.validate(p)["ok"]


def test_tier_swap_replans_drop_tiles():
    """The 2026-08-30 copper failure, encoded: a 3x3 drill dropped into a 2x2 drill's
    position moves the drop tile off the lane. replan() re-derives it; naively reusing the
    old CENTRE does not."""
    elec = M.plan_outpost(DENSE)
    burn = M.replan(elec, drill="burner-mining-drill")
    back = M.replan(burn, drill="electric-mining-drill")
    for p in (elec, burn, back):
        v = M.validate(p)
        assert v["ok"], (p["drill"], v["errors"])
        assert sum(p["bom"].values()) == len(p["entities"])
        for e in roles(p, "drill"):
            assert M.drop_tile(p["drill"], e["x"], e["y"], e["direction"]) in p["lane_tiles"]
    assert elec["lane_y"] == burn["lane_y"] == back["lane_y"]
    assert burn["drill"] == "burner-mining-drill" and back["drill"] == "electric-mining-drill"
    # the drill ROWS shift by the tier's height; the drops still land on the same lane row
    assert min(e["y"] for e in roles(elec, "drill")) == elec["lane_y"] - 3
    assert min(e["y"] for e in roles(burn, "drill")) == burn["lane_y"] - 2
    assert {M.drop_tile(elec["drill"], e["x"], e["y"], e["direction"])[1]
            for e in roles(elec, "drill")} == {elec["lane_y"]}
    assert {M.drop_tile(burn["drill"], e["x"], e["y"], e["direction"])[1]
            for e in roles(burn, "drill")} == {burn["lane_y"]}
    assert back["bom"] == elec["bom"]
    # pole=None means NO poles (a burner mine takes coal, not power); omitting the argument
    # keeps the plan's pole - the sentinel matters or a burner mine grows a dead pole line
    assert "small-electric-pole" in burn["bom"]
    nopole = M.replan(elec, drill="burner-mining-drill", pole=None)
    assert not roles(nopole, "pole") and M.validate(nopole)["ok"]
    assert set(nopole["bom"]) == {"burner-mining-drill", "transport-belt", "inserter",
                                  "wooden-chest"}, nopole["bom"]

    # and now the failure itself: keep the burner's CENTRE, swap the prototype under it.
    b = roles(burn, "drill")[0]
    bcx, bcy = M.center("burner-mining-drill", b["x"], b["y"])
    naive_tile = (int(bcx - 3 / 2.0), int(bcy - 3 / 2.0))     # 3x3 re-centred on (bcx,bcy)
    naive_drop = M.drop_tile("electric-mining-drill", naive_tile[0], naive_tile[1],
                             b["direction"])
    assert naive_drop[1] != burn["lane_y"], (
        "the tier-swap test is not exercising the bug: naive drop %s" % (naive_drop,))
    assert naive_drop not in burn["lane_tiles"]


def test_ragged_patch_drops_starved_drills_and_rebuilds_the_lane():
    """mineore placer._filter_belt_lines: after unviable drills are filtered the belt is
    rebuilt inside the SURVIVING span, so no orphan lane tile is planned."""
    tiles = {}
    for x in range(0, 30):
        for y in range(0, 12 if x < 18 else 3):
            tiles[(x, y)] = 500
    patch = {"ore": "iron-ore", "tiles": tiles, "bbox": (0, 0, 29, 11), "foreign": {}}
    full = M.plan_outpost(rect_patch("iron-ore", 0, 0, 29, 11), min_ore_tiles=20)
    p = M.plan_outpost(patch, min_ore_tiles=20)
    assert len(roles(p, "drill")) < len(roles(full, "drill")), "starved drills not dropped"
    for e in roles(p, "drill"):
        a, b, c, d = M.mining_area(p["drill"], e["x"], e["y"])
        n = sum(1 for x in range(a, c + 1) for y in range(b, d + 1) if (x, y) in tiles)
        assert n >= 20, (e, n)
    drops = sorted(M.drop_tile(p["drill"], e["x"], e["y"], e["direction"])[0]
                   for e in roles(p, "drill"))
    xs = sorted(q["x"] for q in roles(p, "lane"))
    assert xs == list(range(drops[0] - 1, drops[-1] + 1)), (xs[:3], xs[-3:], drops[-1])
    assert max(xs) == drops[-1], "orphan lane tile past the last drill"
    assert M.validate(p)["ok"], M.validate(p)["errors"]


def test_foreign_ore_veto():
    foreign = {(x, y): "coal" for x in range(20, 30) for y in range(0, 12)}
    patch = rect_patch("stone", 0, 0, 29, 11, foreign=foreign)
    p = M.plan_outpost(patch)
    for e in roles(p, "drill"):
        a, b, c, d = M.mining_area(p["drill"], e["x"], e["y"])
        touching = [(x, y) for x in range(a, c + 1) for y in range(b, d + 1) if (x, y) in foreign]
        assert not touching, "drill at (%d,%d) mines foreign %s" % (e["x"], e["y"], touching[:2])
    assert max(e["x"] for e in roles(p, "drill")) < 20
    assert M.validate(p)["ok"], M.validate(p)["errors"]
    # a patch that is entirely foreign-adjacent has no viable row at all
    try:
        M.plan_outpost(rect_patch("stone", 0, 0, 3, 3,
                                  foreign={(x, y): "coal" for x in range(-4, 8)
                                           for y in range(-4, 8)}))
        raise AssertionError("expected LayoutError on a fully vetoed patch")
    except M.LayoutError:
        pass


def test_pole_count_is_the_interval_cover_optimum():
    """_stab must be a MINIMUM stabbing set - brute-forced here for small inputs - and no
    pole gap may exceed max_wire_distance."""
    def brute(ivs):
        pts = sorted({v for iv in ivs for v in iv})
        for k in range(0, len(pts) + 1):
            for combo in itertools.combinations(pts, k):
                if all(any(lo <= c <= hi for c in combo) for lo, hi in ivs):
                    return k
        return len(ivs)

    cases = [[(0, 3), (2, 5), (4, 9)], [(0, 0), (5, 5), (10, 10)],
             [(0, 10), (1, 2), (3, 4), (8, 9)], [(-4, -1), (-2, 2), (1, 6), (6, 6)],
             [(0, 2)], []]
    for ivs in cases:
        got = M._stab(ivs)
        assert len(got) == brute(ivs), (ivs, got, brute(ivs))
        assert all(any(lo <= c <= hi for c in got) for lo, hi in ivs), (ivs, got)

    # end to end: the per-row cover equals the optimum for that row's intervals
    p = M.plan_outpost(DENSE)
    th = M.DRILLS[p["drill"]]["th"]
    ph = M.POLES[p["pole"]]["th"]
    tw = M.DRILLS[p["drill"]]["tw"]
    for side, py in (("top", p["lane_y"] - th - ph), ("bottom", p["lane_y"] + th + 1)):
        ds = [e for e in roles(p, "drill") if e["side"] == side]
        ivs = [i for i in (M._supply_x_range(p["pole"], py, (e["x"], e["y"], tw, th))
                           for e in ds) if i]
        row = [q for q in roles(p, "pole") if q["y"] == py]
        # the cover itself is minimal; the row may hold extra poles from the wire/bridge
        # passes, which exist to make the network reachable, not to cover a drill
        assert len(M._stab(ivs)) == brute(ivs), (side, ivs)
        assert len(row) >= len(M._stab(ivs)), (side, len(row))
        for e in ds:                       # and every drill in the row really is covered
            assert any(M._supply_x_range(p["pole"], py, (e["x"], e["y"], tw, th))[0]
                       <= q["x"] <=
                       M._supply_x_range(p["pole"], py, (e["x"], e["y"], tw, th))[1]
                       for q in row), (side, e)
        xs = sorted(q["x"] for q in row)
        for a, b in zip(xs, xs[1:]):
            assert b - a <= M.POLES[p["pole"]]["wire"], (a, b)


def test_validate_catches_an_injected_bad_plan():
    p = M.plan_outpost(DENSE)
    assert M.validate(p)["ok"]

    bad = dict(p, entities=[dict(e) for e in p["entities"]])
    pole = next(e for e in bad["entities"] if e["role"] == "pole")
    pole["x"], pole["y"] = p["lane_span"][0], p["lane_y"]      # move it onto the lane
    v = M.validate(bad)
    assert not v["ok"] and any("sits on the lane" in m for m in v["errors"]), v["errors"]
    assert any("collision" in m for m in v["errors"]), v["errors"]

    bad2 = dict(p, entities=[dict(e) for e in p["entities"]])
    d = next(e for e in bad2["entities"] if e["role"] == "drill")
    d["direction"] = M.E                                        # rotate the drop off the lane
    v2 = M.validate(bad2)
    assert not v2["ok"] and any("not the lane row" in m for m in v2["errors"]), v2["errors"]

    bad3 = dict(p, entities=[e for e in p["entities"] if not
                             (e["role"] == "lane" and e["x"] == p["lane_span"][0] + 2)])
    bad3["bom"] = M.bom(bad3)
    v3 = M.validate(bad3)
    assert not v3["ok"] and any("gap tile" in m for m in v3["errors"]), v3["errors"]

    bad4 = dict(p, bom=dict(p["bom"], **{"transport-belt": 1}))
    assert any("bom totals" in m for m in M.validate(bad4)["errors"])


# --------------------------------------------------------------------------- scan (RCON)
def test_scan_patch_is_read_only_and_parses():
    payload = ("iron-ore|3,4,1500 4,4,1400 3,5,900\n"
               "copper-ore|9,9,700")
    fake = FakeRcon([("find_entities_filtered", str(len(payload)) + "\n")], payload=payload)
    orig, rcon.run = rcon.run, fake
    try:
        patch = M.scan_patch("iron-ore", 5, 5, radius=10)
    finally:
        rcon.run = orig
    assert patch["ore"] == "iron-ore"
    assert patch["tiles"] == {(3, 4): 1500, (4, 4): 1400, (3, 5): 900}
    assert patch["bbox"] == (3, 4, 4, 5)
    assert patch["foreign"] == {(9, 9): "copper-ore"}
    build = fake.calls[0]
    assert "type='resource'" in build and "area={{-5,-5},{15,15}}" in build, build
    for bad in ("create_entity", "destroy", "revive", "entity-ghost", ".insert",
                ".remove", "set_", "clear_", "die("):
        assert bad not in build, "mutating call %r in the scan: %s" % (bad, build[:200])
    assert M.parse_patch("iron-ore", "")["tiles"] == {}
    assert M.parse_patch("iron-ore", "")["bbox"] is None


def test_to_orders_and_to_ghosts():
    p = M.plan_outpost(DENSE)
    orders = M.to_orders(p)
    assert len(orders) == len(p["entities"])
    assert all(o["kind"] == "place" for o in orders)
    assert "place" in executor.KINDS
    for o in orders:
        assert set(o["args"]) == {"name", "tile_x", "tile_y", "direction"}, o
        assert isinstance(o["args"]["tile_x"], int) and isinstance(o["args"]["tile_y"], int)
        assert o["args"]["direction"] in (M.N, M.E, M.S, M.W)
    assert orders[0]["args"]["name"] == p["drill"], "drills must be ordered first"
    ghosts = M.to_ghosts(p)
    assert len(ghosts) == len(p["entities"])
    for g, e in zip(ghosts, p["entities"]):
        tw, th = M.size_of(e["entity"])
        assert set(g) == {"name", "x", "y", "dir"}
        assert g["x"] == e["x"] + tw / 2.0 and g["y"] == e["y"] + th / 2.0
    belt = next(g for g in ghosts if g["name"] == "transport-belt")
    assert belt["x"] % 1 == 0.5 and belt["y"] % 1 == 0.5          # 1x1 -> .5 centre
    drill = next(g for g in ghosts if g["name"] == "electric-mining-drill")
    assert drill["x"] % 1 == 0.5                                  # 3x3 -> .5 centre too
    burn = M.to_ghosts(M.replan(p, drill="burner-mining-drill"))
    bd = next(g for g in burn if g["name"] == "burner-mining-drill")
    assert bd["x"] % 1 == 0.0 and bd["y"] % 1 == 0.0              # 2x2 -> integer centre


# --------------------------------------------------------------------------- runner
TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    passed, failed = 0, []
    for t in TESTS:
        try:
            t()
            passed += 1
            print("  pass  %s" % t.__name__)
        except Exception:
            failed.append(t.__name__)
            print("  FAIL  %s" % t.__name__)
            traceback.print_exc()
    print("\n%d passed, %d failed (%d total)" % (passed, len(failed), len(TESTS)))
    raise SystemExit(1 if failed else 0)
