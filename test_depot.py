#!/usr/bin/env python3
"""Tests for THE DEPOT - bootstrap.ensure_inventory_room / depot_manifest / depot_take.

    python3 -m pytest test_depot.py

Offline: `autopilot._print` is replaced by a scripted fake, so nothing here touches the live
server. The point of these tests is the failure that actually happened: derpface reached 80/80
stacks, `can_insert` went false for every item, and every build failed at placement while the
planner log read like a gating problem. The guard against that must (a) actually run, (b) not
strip the working set, and (c) leave a manifest so the material can be taken back.
"""
import json
import pathlib
import sys

import rcon

_REAL = rcon.run


def _no_rcon(cmd, timeout=10.0):
    raise AssertionError("offline test issued RCON: %s" % str(cmd)[:160])


rcon.run = _no_rcon

import autopilot as A                                                     # noqa: E402
import bootstrap as B                                                     # noqa: E402


class Fake:
    """Scripted A._print: each call returns the next queued reply and records the command."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []

    def __call__(self, cmd, *a, **k):
        self.sent.append(cmd)
        return self.replies.pop(0) if self.replies else ""

    def sent_matching(self, needle):
        return [c for c in self.sent if needle in c]


def _with_fake(fake, fn):
    old_print, old_log = A._print, B.status.log
    B.status.log = lambda *a, **k: None
    A._print = fake
    try:
        return fn()
    finally:
        A._print, B.status.log = old_print, old_log


def test_a_roomy_inventory_is_left_alone():
    """The guard runs at the top of EVERY planner pass, so the common case must be one cheap
    read and no writes - otherwise it is a tax on every pass and someone will move it behind a
    condition, which is how the last one stopped running."""
    fake = Fake("40")
    out = _with_fake(fake, lambda: B.ensure_inventory_room())
    assert out is None
    assert len(fake.sent) == 1, fake.sent
    assert "count_empty_stacks" in fake.sent[0]


def test_a_full_inventory_is_offloaded():
    fake = Fake("0", "6 62 0 transport-belt=1174 iron-gear-wheel=733", "")
    out = _with_fake(fake, lambda: B.ensure_inventory_room())
    assert out is not None and out.startswith("6 62 0")
    dumped = fake.sent_matching("count_empty_stacks()")
    assert len(dumped) >= 2, "the offload must report the free space it achieved"


def test_the_offload_lua_carries_no_dash_comments():
    """`/sc` is sent as ONE LINE, so a Lua `--` comment swallows the rest of the command and the
    whole thing silently does nothing. That is not a style rule - it is the bug that made the
    first hand-run dump a no-op while reporting nothing at all."""
    fake = Fake("0", "6 62 0", "")
    _with_fake(fake, lambda: B.ensure_inventory_room())
    for cmd in fake.sent:
        assert "--" not in cmd, "a Lua comment would comment out the rest of the command: %s" % cmd[:200]


def test_the_working_set_is_never_offloaded():
    """A build that has to re-craft its own belts has traded one stall for another. The keep
    list must reach the Lua, and must cover what a placement actually consumes."""
    fake = Fake("0", "6 62 0", "")
    _with_fake(fake, lambda: B.ensure_inventory_room())
    spec = fake.sent_matching("keep[")[0]
    for essential in ("transport-belt", "small-electric-pole", "inserter", "iron-plate",
                      "assembling-machine-1", "boiler", "steam-engine"):
        assert essential in spec, "%s must survive an offload" % essential
    assert B.DEPOT_KEEP["boiler"] >= 1 and B.DEPOT_KEEP["steam-engine"] >= 2, \
        "a plant column needs its own parts in hand or the build fails at placement"


def test_the_depot_tiles_are_fixed_and_central():
    """Seth: 'keep these chests in a central location.' Fixed tiles, not wherever he stands -
    a scattered chest is one nobody finds again, and the manifest would name a moving target."""
    assert len(B.DEPOT_TILES) >= 4
    xs = [x for x, _ in B.DEPOT_TILES]
    ys = [y for _, y in B.DEPOT_TILES]
    # ONE BLOCK, but one that can GROW. Six chests filled within hours and the offload then
    # reported "the depot needs another chest" every pass while free_slots stayed at 0 - and at
    # 0 free slots nothing can be crafted or placed. It extends southward from the same corner
    # rather than scattering, so it is still the one findable place.
    assert max(xs) - min(xs) <= 4, "the depot must stay one narrow column block"
    assert max(ys) - min(ys) <= 16, "it grows southward, it does not sprawl"
    assert len(B.DEPOT_TILES) >= 12, "it must have room to grow past the first fill"
    # between the plate chests (y 3..12) and the lab array (y 36..44), near the x=0 lab column
    assert 0 <= min(xs) and max(xs) <= 12, B.DEPOT_TILES
    assert 12 <= min(ys) and max(ys) <= 34, B.DEPOT_TILES   # room to grow southward


def test_the_manifest_records_what_is_in_each_chest(tmp_path):
    """'make sure to keep track of whats in the chest so he can use it later' - a blind dump
    loses material the bot then re-crafts from raws."""
    fake = Fake("2,20|transport-belt|1174;2,20|iron-plate|1000;3,20|iron-gear-wheel|733")
    target = tmp_path / "depot-manifest.json"
    old = B._depot_manifest_path
    B._depot_manifest_path = lambda: target
    try:
        out = _with_fake(fake, lambda: B.depot_manifest())
    finally:
        B._depot_manifest_path = old
    assert out["totals"]["transport-belt"] == 1174
    assert out["totals"]["iron-gear-wheel"] == 733
    assert out["by_chest"]["2,20"]["iron-plate"] == 1000
    written = json.loads(target.read_text())
    assert written["totals"] == out["totals"], "the manifest must be persisted, not just returned"
    assert written["tiles"], "the manifest must say WHERE the depot is"


def test_material_can_be_taken_back_out():
    """The other half of the rule: if it cannot come back, the dump is a slow way of throwing
    it away."""
    fake = Fake("200", "")
    got = _with_fake(fake, lambda: B.depot_take("transport-belt", 200))
    assert got == 200
    assert "transport-belt" in fake.sent[0]


def test_an_unreadable_free_space_reading_does_not_offload_blind():
    """A failed read must never be mistaken for 'the bag is full' - offloading on garbage would
    strip the working set for no reason."""
    fake = Fake("<error: whatever>")
    out = _with_fake(fake, lambda: B.ensure_inventory_room())
    assert out is None
    assert len(fake.sent) == 1, "it must stop after the failed read, not go on to dump"


def test_the_planner_actually_calls_it():
    """THE WHOLE POINT. `autopilot.manage_inventory` was correct-looking code that never ran:
    it hangs off `maintain()`, which hangs off `patrol.py`, which nothing imports. A guard that
    is not wired into the live path is not a guard."""
    src = (pathlib.Path(__file__).resolve().parent / "planner.py").read_text()
    assert "ensure_inventory_room()" in src, "phase0 must offload before it gates anything"
    head = src[src.index("def phase0("):]
    head = head[:head.index("for name, fn in PHASE0_STAGES")]
    assert "ensure_inventory_room()" in head, \
        "the offload must run BEFORE the stages, not after they have already failed on NO_ITEM"


def test_the_superseded_helper_says_so():
    """The dead one stays only for patrol.maintain(); its docstring must send the next reader to
    the live one, or this gets 'fixed' by extending the version that never runs."""
    doc = A.manage_inventory.__doc__ or ""
    assert "SUPERSEDED" in doc and "ensure_inventory_room" in doc


if __name__ == "__main__":
    import traceback
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn() if fn.__code__.co_argcount == 0 else fn(pathlib.Path("/tmp"))
                print("ok   %s" % name)
            except Exception:
                fails += 1
                print("FAIL %s" % name)
                traceback.print_exc()
    sys.exit(1 if fails else 0)


def test_depot_grows_with_any_container_it_has():
    """The depot could only extend itself by placing iron-chest. A character carrying 38
    WOODEN chests and no iron ones therefore could not grow a full depot: every pass logged
    "the depot needs another chest" while holding the chests to build it, inventory sat at 0
    free stacks, and a full inventory makes can_insert false for EVERY item - which silently
    blocks every build in the base. Preference order, not a single hardcoded name."""
    import bootstrap
    import inspect
    src = inspect.getsource(bootstrap.ensure_inventory_room)
    assert "'steel-chest','iron-chest','wooden-chest'" in src, \
        "the depot can only grow with one hardcoded container again"
    assert src.index("steel-chest") < src.index("wooden-chest"), "best container should win"


def test_the_science_chain_builds_red_as_well_as_green():
    """The chain was green-only, and every green link already existed, so the stage returned
    immediately while nothing on the map made automation-science-pack. 28 furnaces sat at
    full_output with no plate consumer and all 10 labs read missing_science_packs."""
    import bootstrap
    import inspect
    src = inspect.getsource(bootstrap.automate_green_science)
    chain = src.split("chain = [", 1)[1].split("]", 1)[0]
    assert chain.count("automation-science-pack") >= 1, "red science is missing from the chain"
    assert chain.count("logistic-science-pack") >= 1, "green science fell out of the chain"


def test_science_chain_never_blanket_clears():
    """It used to call clear_area with a radius that SCALED WITH HOW MUCH WAS MISSING. That
    never fired while the chain was complete; the moment red science was added it fired and
    destroyed the two existing green assemblers it was walking over to reach. More missing ->
    bigger blast radius is exactly backwards."""
    import bootstrap
    import inspect
    src = inspect.getsource(bootstrap.automate_green_science)
    assert "clear_area" not in src, "the science chain regained a blanket area clear"
    assert "clear=1" in src, "each machine should clear only its own footprint"
