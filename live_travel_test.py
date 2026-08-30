#!/usr/bin/env python3
"""ONE-SHOT live validation for the travel stack (task-sanctioned live test).

Sends derpface to within radius 4 of the coal patch STATE coord using
travel.goto_far, printing a status trace. Skips itself if a player is connected.
Run with the autopilot container STOPPED (one-controller rule).
"""
import json
import sys
import threading
import time

import rcon
import fle_tools
import travel

COAL = (-38, 15)          # phase.json state.coal on charon, read 2026-08-29
RADIUS = 4


def main():
    players = int(rcon.run("/sc rcon.print(#game.connected_players)").strip())
    if players > 0:
        print(f"SKIP: {players} player(s) connected in-game — not running the live travel test")
        return 1
    # Clean slate: clear any leftover walking_state / travel queue (GOTCHAS: a
    # killed walk process leaves walking_state=true).
    travel._stop_lua()
    time.sleep(2)
    p0 = travel._pos()
    time.sleep(3)
    p1 = travel._pos()
    if abs(p0[0] - p1[0]) + abs(p0[1] - p1[1]) > 0.2:
        print(f"ABORT: character still moving ({p0} -> {p1}) — another controller is live")
        return 2
    print(f"start pos: ({p1[0]:.2f}, {p1[1]:.2f}); goal: {COAL} r={RADIUS}")
    pushed = travel.ensure_handlers()
    print(f"ensure_handlers: pushed={pushed}, sentinel={travel._tick_sentinel()}")

    # Trace thread: sample travel_status every 2s while goto_far drives.
    stop_trace = threading.Event()
    trace = []

    def tracer():
        while not stop_trace.is_set():
            try:
                st = travel._status()
                trace.append(st)
                print("  trace:", json.dumps(st))
            except Exception as e:      # noqa: BLE001 — trace must never kill the walk
                print("  trace error:", e)
            stop_trace.wait(2.0)

    t = threading.Thread(target=tracer, daemon=True)
    t.start()
    t0 = time.time()
    x, y, ok = travel.goto_far(COAL[0], COAL[1], radius=RADIUS, timeout=240)
    stop_trace.set()
    t.join(timeout=3)
    dt = time.time() - t0
    import math
    dist = math.hypot(x - COAL[0], y - COAL[1])
    print(f"goto_far -> ({x:.2f}, {y:.2f}) ok={ok} in {dt:.1f}s; {dist:.2f} tiles from goal")
    # travel_stop (goto_far already cleared, but the task asks explicitly; idempotent)
    out = rcon.run("/sc rcon.print(fle.travel_stop())").strip()
    print(f"travel_stop: {out}")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
