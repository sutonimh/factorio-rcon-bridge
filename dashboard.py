#!/usr/bin/env python3
"""Web dashboard for the autopilot (MEGABASE-V2-DESIGN §7). Stdlib only — no pip deps.

Serves dashboard.html plus a small JSON API over the autopilot's runtime files (status.json,
phase.json, lessons.jsonl, architect-report.json, orders.json, world-db.json) and a few live
RCON reads (production counts, research, entity map). Read-only by design: no override lane
yet — steering stays with RCON sessions.

Run on charon next to the autopilot (its own container, /app mounted read-only):
    docker run -d --name factorio-dash --restart always \
      -v /mnt/user/appdata/factorio-autopilot:/app:ro -w /app \
      -e FACTORIO_RCON_HOST=<charon-lan-ip> -p 8619:8619 \
      python:3.12-slim python3 -u dashboard.py
"""
import json
import pathlib
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rcon

HERE = pathlib.Path(__file__).resolve().parent
PORT = 8619
_cache = {}


def _read_json(name, default):
    try:
        return json.loads((HERE / name).read_text())
    except (OSError, ValueError):
        return default


def _tail(name, n):
    try:
        return (HERE / name).read_text().splitlines()[-n:]
    except OSError:
        return []


def _rcon_cached(key, cmd, ttl=5):
    """Live RCON read with a small TTL cache so page polls don't hammer the game."""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        out = rcon.run(cmd).strip()
    except Exception as e:
        out = json.dumps({"error": str(e)[:100]})
    _cache[key] = (now, out)
    return out


def live_metrics():
    out = _rcon_cached("metrics", (
        "/sc local s=game.surfaces[1]; local f=game.forces.player;"
        "local ps=f.get_item_production_statistics(s);"
        "local function pm(n) return ps.get_flow_count{name=n,category='input',precision_index=defines.flow_precision_index.one_minute} end;"
        "local r=f.current_research;"
        "rcon.print(helpers.table_to_json({tick=game.tick,"
        "research=r and r.name or '',research_pct=r and math.floor(f.research_progress*100) or 0,"
        "iron_pm=pm('iron-plate'),copper_pm=pm('copper-plate'),"
        "red_pm=pm('automation-science-pack'),green_pm=pm('logistic-science-pack')}))"
    ), ttl=5)
    try:
        return json.loads(out)
    except ValueError:
        return {"error": out[:200]}


def live_map():
    """Entity scatter for the canvas map: name-class + tile pos, capped."""
    out = _rcon_cached("map", (
        "/sc local s=game.surfaces[1]; local o={};"
        "for _,e in pairs(s.find_entities_filtered{force='player'}) do"
        "  if #o<2500 and e.name~='character' then o[#o+1]={n=e.name,x=math.floor(e.position.x),y=math.floor(e.position.y)} end end;"
        "rcon.print(helpers.table_to_json(o))"
    ), ttl=10)
    try:
        return json.loads(out)
    except ValueError:
        return []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                self._send((HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(b"dashboard.html missing", "text/plain")
        elif self.path == "/api/state":
            rep = _read_json("architect-report.json", {})
            self._send({
                "status": _read_json("status.json", {}),
                "phase": _read_json("phase.json", {}),
                "lessons": _read_json("lessons.jsonl", None) or [json.loads(x) for x in _tail("lessons.jsonl", 15) if x.strip()],
                "architect_summary": rep.get("summary", ""),
                "architect_actions": rep.get("prioritized_actions", [])[:5],
                "orders": _read_json("orders.json", [])[-10:] if isinstance(_read_json("orders.json", []), list) else [],
                "metrics": live_metrics(),
            })
        elif self.path == "/api/map":
            self._send(live_map())
        elif self.path.startswith("/api/log"):
            self._send({"lines": _tail("autopilot.log", 60)})
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    print(f"dashboard on :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
