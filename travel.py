#!/usr/bin/env python3
"""Long-distance travel via Factorio's NATIVE pathfinder (FLE-style).

Replaces fragile long leg-walks: the old walk() computed routes from a Python-side
obstacle scan (pad 6 around the straight line) and drove every leg over RCON, so a
100+ tile trip stuttered through unscanned terrain, sidestepped blindly at water,
and a killed process left the character running forever. This stack is the FLE
model (request_path / get_path / move_to, MIT — see LUA-VENDORING.md):

  1. fle.travel_request(tx, ty, r): corridor chunk pre-generation (ungenerated
     chunks block the pathfinder), then async surface.request_path with the
     character's own collision box + mask.
  2. Poll fle.travel_poll(id) until the on_script_path_request_finished handler
     (registered from /sc — verified working on 2.1.17) delivers the waypoints.
  3. fle.travel_go(id): a server-side script.on_nth_tick(5) walker consumes the
     waypoint queue — 0.35-tile arrival, stuck watchdog that hops to the next
     waypoint (arturh85's legal unstick, never travel-by-teleport).
  4. goto_far polls fle.travel_status() every 2s until done/timeout, honoring
     controller.PREEMPT (a sev-0/1 issue stops the travel and returns).

The walk itself runs entirely server-side: RCON latency or a killed Python
process can no longer produce a runaway walk (the walker stops at the last
waypoint), and autopilot.stop() clears storage.fle_travel (one-controller rule).

Usage:
    python3 travel.py goto <x> <y> [radius]      # long-distance travel
    python3 travel.py status                     # current travel state
    python3 travel.py stop                       # clear queue + halt
"""
import json
import math
import sys
import time

import rcon
import fle_tools

FAR_GOAL_STEP = 8       # displaced-goal retry offset, rotated 90 deg per attempt
POLL_S = 2.0            # travel_status cadence while walking
PATH_POLL_S = 0.5       # travel_poll cadence while the pathfinder computes
PATH_WAIT_S = 45.0      # per-goal pathfinder budget (chunk generation can be slow)
ARRIVE_SLACK = 2.0      # goal snap + pathfinder radius make exact arrival wrong


# ------------------------------------------------------------------------ helpers
def _preempted():
    """controller.PREEMPT gate (same lazy-import pattern as autopilot.walk): a
    severity-0/1 issue outranks any travel."""
    try:
        import controller as _ctl
        return bool(_ctl.PREEMPT.get("want"))
    except ImportError:
        return False


def _pos():
    out = rcon.run("/sc local p=storage.derpface;"
                   " rcon.print(p.position.x..','..p.position.y)").strip()
    x, y = out.split(",")
    return float(x), float(y)


def _stop_lua():
    """Clear the server-side walking queue + halt. Pure storage ops, so it works
    even when the `fle` global isn't loaded (server just restarted)."""
    rcon.run("/sc storage.fle_travel=nil; storage.fle_paths=nil; storage.fle_pathreq=nil;"
             " if storage.derpface and storage.derpface.valid then"
             " storage.derpface.walking_state={walking=false} end")


def _tick_sentinel():
    out = rcon.run("/sc rcon.print(storage.fle_travel_tick or 0)").strip()
    return int(out) if out.lstrip("-").isdigit() else -1


def ensure_handlers():
    """Idempotent handler init. The travelinit chunk registers the path-finished +
    nth-tick handlers; they die with the Lua state (save reload / server restart)
    exactly like the `fle` global, so re-pushing fle_lib re-registers them. After
    any push, verify liveness via the storage sentinel the nth-tick handler
    increments (storage.fle_travel_tick must advance)."""
    pushed = False
    if fle_tools._probe() != fle_tools.lib_version():
        fle_tools.init(force=True)
        pushed = True
    if rcon.run("/sc rcon.print(fle and fle.travel_on and 'on' or 'off')").strip() != "on":
        fle_tools.init(force=True)      # global present but handlers never armed
        pushed = True
    if pushed:
        t1 = _tick_sentinel()
        time.sleep(0.3)
        if _tick_sentinel() == t1:
            raise RuntimeError("travel handlers not ticking (storage.fle_travel_tick frozen)")
    return pushed


def retry_goals(tx, ty, step=FAR_GOAL_STEP):
    """The original goal, then four goals displaced `step` tiles, rotating 90 deg
    per attempt (E, S, W, N) — the arturh85 pattern for goals the pathfinder can't
    terminate at (deep in water / an ore field / dense forest). Pure function
    (unit-tested offline)."""
    return [(tx, ty), (tx + step, ty), (tx, ty + step), (tx - step, ty), (tx, ty - step)]


def _request(gx, gy, radius):
    out = rcon.run(f"/sc rcon.print(fle.travel_request({gx},{gy},{radius}))").strip()
    return int(out) if out.lstrip("-").isdigit() else -1


def _poll(rid):
    out = rcon.run(f"/sc rcon.print(fle.travel_poll({rid}))").strip()
    try:
        return json.loads(out)
    except ValueError:
        return {"status": "error", "raw": out}


def _status():
    out = rcon.run("/sc rcon.print(fle.travel_status())").strip()
    try:
        return json.loads(out)
    except ValueError:
        return {"active": False, "done": False, "partial": True, "raw": out}


def _await_path(rid, deadline):
    """Poll one path request to a terminal state (success/not_found/busy/invalid)
    within the time budget; 'pending' keeps polling."""
    while time.time() < deadline:
        st = _poll(rid)
        s = st.get("status")
        if s == "pending":
            time.sleep(PATH_POLL_S)
            continue
        return st
    return {"status": "timeout"}


# ------------------------------------------------------------------------ goto_far
def goto_far(tx, ty, radius=3, timeout=240):
    """Travel to within `radius` tiles of (tx, ty) using the native pathfinder.

    Returns (x, y, ok) — the same triple as autopilot.walk. ok means the
    character actually ended up near the requested spot (radius + slack, widened
    by the displacement when a displaced retry goal was used). PREEMPT (sev-0/1)
    stops the travel cleanly and returns ok=False so the builder can service it.
    """
    t0 = time.time()
    ensure_handlers()
    _stop_lua()                      # one controller: never two walkers at once
    rid = -1
    used_disp = 0.0
    for gx, gy in retry_goals(tx, ty):
        if time.time() - t0 > timeout or _preempted():
            x, y = _pos()
            return x, y, False
        rid = _request(gx, gy, radius)
        if rid < 0:
            x, y = _pos()
            return x, y, False       # no valid character
        deadline = min(t0 + timeout, time.time() + PATH_WAIT_S)
        st = _await_path(rid, deadline)
        if st.get("status") == "busy":               # pathfinder overloaded: one re-ask
            time.sleep(2.0)
            rid = _request(gx, gy, radius)
            st = _await_path(rid, min(t0 + timeout, time.time() + PATH_WAIT_S))
        if st.get("status") == "success":
            used_disp = math.hypot(gx - tx, gy - ty)
            break
        rid = -1                                     # not_found / timeout: displace goal
    if rid < 0:
        x, y = _pos()
        return x, y, False
    if rcon.run(f"/sc rcon.print(fle.travel_go({rid}))").strip() != "go":
        x, y = _pos()
        return x, y, False
    st = {}
    while True:
        if _preempted():
            _stop_lua()
            x, y = _pos()
            return x, y, False
        st = _status()
        if st.get("done"):
            break
        if time.time() - t0 > timeout:
            _stop_lua()
            x, y = _pos()
            return x, y, False
        time.sleep(POLL_S)
    x = st.get("x") if st.get("x") is not None else _pos()[0]
    y = st.get("y") if st.get("y") is not None else _pos()[1]
    _stop_lua()                      # clear the finished queue + GC paths
    ok = math.hypot(x - tx, y - ty) <= radius + ARRIVE_SLACK + used_disp
    return float(x), float(y), ok


# ------------------------------------------------------------------------ cli
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "goto":
        r = int(sys.argv[4]) if len(sys.argv) > 4 else 3
        print(goto_far(float(sys.argv[2]), float(sys.argv[3]), radius=r))
    elif cmd == "status":
        ensure_handlers()
        print(json.dumps(_status(), indent=2))
    elif cmd == "stop":
        _stop_lua()
        print("stopped")
    else:
        print(__doc__)
        sys.exit(2)
