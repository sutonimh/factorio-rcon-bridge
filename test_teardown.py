"""Tests for teardown: what survives is defined by region, and regions are checkable."""
import teardown as T


def test_keep_regions_cover_the_three_survivors():
    drills = [(20, -42), (-25, -63), (-39, 15)]
    r = T.regions(drills)
    assert T.inside(r, 0, 36), "a lab is not protected"
    assert T.inside(r, 8, 44), "the far corner of the lab array is not protected"
    assert T.inside(r, 24, 50), "the ghost reservation is not protected"
    assert T.inside(r, -29, 46), "a boiler is not protected"
    assert T.inside(r, -33, 37), "a steam engine is not protected"
    assert T.inside(r, -31, 51), "the offshore pump is not protected"
    assert T.inside(r, -25, 41), "the outlier lab is not protected"
    for d in drills:
        assert T.inside(r, *d), "drill %s is not protected" % (d,)


def test_open_ground_is_not_protected():
    r = T.regions([(20, -42)])
    assert not T.inside(r, 40, 0), "open ground reads as protected; nothing would be removed"
    assert not T.inside(r, 6, 15), "the copper smelter row reads as protected"


def test_drill_margin_covers_its_furniture():
    """A drill's poles and output belt sit beside it and must not be torn out from under it."""
    r = T.regions([(20, -42)])
    assert T.inside(r, 20 + T.MINE_MARGIN, -42)
    assert not T.inside(r, 20 + T.MINE_MARGIN + 1, -42)


def test_poles_are_not_demolished_here():
    """pole_cull already knows a redundant pole from a load-bearing one. Deciding that in two
    places is how two builders end up fighting."""
    assert "electric-pole" not in T.DEMOLISH_TYPES
    assert not any("pole" in t for t in T.DEMOLISH_TYPES)


def test_demolish_list_is_a_whitelist_not_a_blacklist():
    """An unknown entity is not "junk I have not thought about", it is something to leave.
    plan_mine_geometry cleared by BLACKLIST and destroyed the iron output inserters."""
    for t in T.DEMOLISH_TYPES:
        assert "-" in t or t.isalpha(), t
    for must_not in ("mining-drill", "lab", "boiler", "generator", "offshore-pump",
                     "pipe", "pipe-to-ground", "electric-pole", "character", "entity-ghost"):
        assert must_not not in T.DEMOLISH_TYPES, "%s would be demolished" % must_not


def test_regions_accepts_extra_keeps():
    r = T.regions([(0, 0)], extra=[(100, 100, 110, 110)])
    assert T.inside(r, 105, 105)


def test_lua_regions_is_wellformed():
    s = T._lua_regions([(1, 2, 3, 4), (5, 6, 7, 8)])
    assert s == "{{1,2,3,4},{5,6,7,8}}"


def test_no_drills_means_no_teardown():
    """A bad read must not be interpreted as an empty base and license to delete everything."""
    class FakeA:
        def _print(self, lua):
            return ""
    said = []
    out = T._run(FakeA(), (), dry=True, log=said.append)
    assert out == {}
    assert any("REFUSED" in m for m in said)


# --------------------------------------------------------------------------- aftercare
class _FakeA:
    def __init__(self, reply):
        self.reply = reply
    def _print(self, lua):
        return self.reply


def test_power_check_parses_what_is_dark():
    out = T.power_check(_FakeA("mining-drill=13 inserter=4"))
    assert out == {"mining-drill": 13, "inserter": 4}


def test_power_check_empty_when_all_lit():
    assert T.power_check(_FakeA("")) == {}


def test_report_power_names_the_mines_when_drills_are_dark():
    """The blueprint rebuild went fine and the base still sat idle: all three mines ended up
    with zero poles and every drill read no_power. Teardown removes what a pole was covering,
    the pole becomes a genuine orphan, pole_cull correctly removes it - each step right, the
    pair of them puts the mines in the dark. Only reading the world back catches that."""
    said = []
    T.report_power(_FakeA("mining-drill=13"), log=said.append)
    joined = " ".join(said)
    assert "UNPOWERED" in joined and "13 mining-drill" in joined


def test_report_power_is_quiet_when_clean():
    said = []
    T.report_power(_FakeA(""), log=said.append)
    assert "nothing left unpowered" in " ".join(said)


# --------------------------------------------------------------------------- world model
def test_invalidate_removes_the_stale_caches(tmp_path):
    """lanes.json still recorded iron-ore running west to (-4,-40) - the demolished smelter
    row - while the new base sat at x=38..108. So lane_stalled fired every 20s, triage routed
    to fix_lanes, and fix_lanes repaired a lane pointing at bare dirt, forever. The self-heals
    were not failing; they were succeeding at the wrong target."""
    for name in T.STALE_STATE:
        (tmp_path / name).write_text("{}")
    said = []
    gone = T.invalidate_world_model(root=tmp_path, log=said.append)
    assert sorted(gone) == sorted(T.STALE_STATE)
    for name in T.STALE_STATE:
        assert not (tmp_path / name).exists()
    assert "world model invalidated" in " ".join(said)


def test_invalidate_is_safe_when_nothing_is_there(tmp_path):
    said = []
    assert T.invalidate_world_model(root=tmp_path, log=said.append) == []
    assert "nothing stale to clear" in " ".join(said)


def test_lane_caches_are_in_the_stale_list():
    """These are the files that made the bot aim at the old base."""
    assert "lanes.json" in T.STALE_STATE
    assert "supply-lanes.json" in T.STALE_STATE


def test_reconcile_releases_our_own_removals_but_keeps_the_operators(monkeypatch):
    """diff_since_baseline protects anything that vanished while the builder was down, as the
    operator's INTENT. It cannot tell an authorised teardown from a hand-deletion, so after
    this one the builder refused to rebuild: "OPERATOR-OWNED ROUTE: 19/31 tiles are
    operator-protected". Ours must be released; his must be kept."""
    import bootstrap as B
    his = {(100, 100), (101, 100)}
    ours = {(5, 5), (6, 5)}
    store = {"p": his | ours}
    monkeypatch.setattr(B, "_protected_load", lambda: set(store["p"]))
    monkeypatch.setattr(B, "_protected_save", lambda t: store.__setitem__("p", set(t)))
    monkeypatch.setattr(B, "save_baseline", lambda *a, **k: 42)
    said = []
    freed = T.reconcile_ownership(None, removed_tiles=ours, log=said.append)
    assert freed == 2
    assert store["p"] == his, "the operator's own protected tiles were dropped"
    assert "re-baselined 42" in " ".join(said)


def test_reconcile_with_no_removals_still_rebaselines(monkeypatch):
    import bootstrap as B
    monkeypatch.setattr(B, "_protected_load", lambda: {(1, 1)})
    monkeypatch.setattr(B, "_protected_save", lambda t: None)
    monkeypatch.setattr(B, "save_baseline", lambda *a, **k: 7)
    said = []
    assert T.reconcile_ownership(None, log=said.append) == 0
    assert "re-baselined 7" in " ".join(said)
