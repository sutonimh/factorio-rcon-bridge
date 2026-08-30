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


def _rcon_chunked(key, build_lua, ttl=30):
    """Chunked storage read (architect.py pattern) with TTL cache — for payloads >4KB that a
    single RCON response would truncate. build_lua must end with storage._dash=<json string>
    and rcon.print(#storage._dash)."""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        n = int(rcon.run(build_lua).strip() or "0")
        parts, i = [], 1
        while i <= n:
            parts.append(rcon.run("/sc rcon.print(storage._dash:sub(%d,%d))" % (i, i + 2999)).rstrip("\r\n"))
            i += 3000
        rcon.run("/sc storage._dash=nil")
        out = "".join(parts)
    except Exception as e:
        out = json.dumps({"error": str(e)[:100]})
    _cache[key] = (now, out)
    return out


def terrain():
    """Low-res terrain grid over the built-base bbox (step-2 tiles): water/ore/tree/land as
    char rows. Rendered under the live map so the base sits in real geography."""
    out = _rcon_chunked("terrain", (
        "/sc local s=game.surfaces[1];"
        "local x1,y1,x2,y2=1e9,1e9,-1e9,-1e9;"
        "for _,e in pairs(s.find_entities_filtered{force='player'}) do"
        "  local p=e.position; if p.x<x1 then x1=p.x end; if p.x>x2 then x2=p.x end;"
        "  if p.y<y1 then y1=p.y end; if p.y>y2 then y2=p.y end end;"
        "if x1>x2 then storage._dash='{}' rcon.print(2) return end;"
        "x1,y1,x2,y2=math.floor(x1)-14,math.floor(y1)-14,math.floor(x2)+14,math.floor(y2)+14;"
        "local STEP=2; local W=math.floor((x2-x1)/STEP)+1; local H=math.floor((y2-y1)/STEP)+1;"
        "local grid={}; for r=1,H do grid[r]={} for c=1,W do grid[r][c]='.' end end;"
        "local function mark(px,py,ch) local c=math.floor((px-x1)/STEP)+1; local r=math.floor((py-y1)/STEP)+1;"
        "  if r>=1 and r<=H and c>=1 and c<=W then grid[r][c]=ch end end;"
        "for _,t in pairs(s.find_tiles_filtered{area={{x1,y1},{x2,y2}},name={'water','deepwater'}}) do mark(t.position.x,t.position.y,'w') end;"
        "local OC={['iron-ore']='i',['copper-ore']='c',['coal']='k',['stone']='s',['crude-oil']='o'};"
        "for _,r in pairs(s.find_entities_filtered{area={{x1,y1},{x2,y2}},type='resource'}) do mark(r.position.x,r.position.y,OC[r.name] or 's') end;"
        "for _,t in pairs(s.find_entities_filtered{area={{x1,y1},{x2,y2}},type='tree'}) do mark(t.position.x,t.position.y,'t') end;"
        "local rows={}; for r=1,H do rows[r]=table.concat(grid[r]) end;"
        "storage._dash=helpers.table_to_json({x1=x1,y1=y1,step=STEP,rows=rows});"
        "rcon.print(#storage._dash)"
    ), ttl=30)
    try:
        return json.loads(out)
    except ValueError:
        return {}


def live_map():
    """Entity scatter for the canvas map: name-class + tile pos (+ ghosts flagged g=1) and
    derpface position, capped."""
    out = _rcon_cached("map", (
        "/sc local s=game.surfaces[1]; local o={};"
        "for _,e in pairs(s.find_entities_filtered{force='player'}) do"
        "  if #o<3000 and e.name~='character' then"
        "    if e.name=='entity-ghost' then o[#o+1]={n=e.ghost_name,x=math.floor(e.position.x),y=math.floor(e.position.y),g=1}"
        "    else o[#o+1]={n=e.name,x=math.floor(e.position.x),y=math.floor(e.position.y)} end end end;"
        "local p=storage.derpface; local dp=p and p.valid and {x=p.position.x,y=p.position.y} or nil;"
        "rcon.print(helpers.table_to_json({ents=o,derp=dp}))"
    ), ttl=4)
    try:
        d = json.loads(out)
        return d if isinstance(d, dict) else {"ents": d, "derp": None}
    except ValueError:
        return {"ents": [], "derp": None}


def derpface_window(half=6):
    """Live close-up around derpface: entities + water/resource tiles in a ~(2*half)^2 area,
    plus position/walking/crafting state. Small + fast (2s TTL) so the panel feels live."""
    out = _rcon_cached("derp", (
        "/sc local p=storage.derpface; if not (p and p.valid) then rcon.print('{}') return end;"
        "local s=p.surface; local px,py=p.position.x,p.position.y;"
        f"local x1,y1,x2,y2=math.floor(px)-{half},math.floor(py)-{half},math.floor(px)+{half},math.floor(py)+{half};"
        "local ents={};"
        "for _,e in pairs(s.find_entities_filtered{area={{x1,y1},{x2,y2}}}) do"
        "  if e.name~='character' and #ents<160 then"
        "    local n=(e.name=='entity-ghost') and e.ghost_name or e.name;"
        "    local r={n=n,x=e.position.x,y=e.position.y,t=e.type,g=(e.name=='entity-ghost') and 1 or nil};"
        "    local okd,dd=pcall(function() return e.direction end); if okd and dd then r.d=dd end;"
        "    local oks,st=pcall(function() return e.status end);"
        "    if oks and st==defines.entity_status.working then r.w=1 end;"
        "    if e.type=='transport-belt' then local it=0;"
        "      for li=1,e.get_max_transport_line_index() do it=it+#e.get_transport_line(li) end;"
        "      if it>0 then r.it=it end end;"
        "    ents[#ents+1]=r end end;"
        "local tiles={};"
        "for _,t in pairs(s.find_tiles_filtered{area={{x1,y1},{x2,y2}},name={'water','deepwater'}}) do"
        "  tiles[#tiles+1]={x=t.position.x,y=t.position.y,w=1} end;"
        "for _,r in pairs(s.find_entities_filtered{area={{x1,y1},{x2,y2}},type='resource'}) do"
        "  tiles[#tiles+1]={x=math.floor(r.position.x),y=math.floor(r.position.y),o=r.name} end;"
        "rcon.print(helpers.table_to_json({x=px,y=py,walking=p.walking_state.walking,"
        "craftq=p.crafting_queue_size,ents=ents,tiles=tiles,x1=x1,y1=y1,x2=x2,y2=y2}))"
    ), ttl=2)
    try:
        return json.loads(out)
    except ValueError:
        return {}


def _researched():
    """Set of researched tech names (cached 30s)."""
    out = _rcon_chunked("techs", (
        "/sc local t={}; for n,tech in pairs(game.forces.player.technologies) do"
        "  if tech.researched then t[#t+1]=n end end;"
        "storage._dash=helpers.table_to_json(t); rcon.print(#storage._dash)"), ttl=30)
    try:
        return set(json.loads(out))
    except (ValueError, TypeError):
        return set()


def _producing():
    """Item names with nonzero cumulative production (cached 30s)."""
    out = _rcon_chunked("prod", (
        "/sc local s=game.surfaces[1]; local ps=game.forces.player.get_item_production_statistics(s);"
        "local t={}; for n,c in pairs(ps.input_counts) do if c>0 then t[#t+1]=n end end;"
        "storage._dash=helpers.table_to_json(t); rcon.print(#storage._dash)"), ttl=30)
    try:
        return set(json.loads(out))
    except (ValueError, TypeError):
        return set()


def _bom(node, acc):
    if "blueprint_book" in node:
        for ch in node["blueprint_book"].get("blueprints", []):
            _bom(ch, acc)
    elif "blueprint" in node:
        for e in node["blueprint"].get("entities", []):
            acc[e["name"]] = acc.get(e["name"], 0) + 1


def analyze_bp(bp_string, techdb, researched, producing):
    """Readiness review of one blueprint/book against live research + production."""
    import bplib
    d = bplib.decode(bp_string)
    maj, minor = bplib.game_version(d)
    bom = {}
    _bom(d, bom)
    rows, ready_count, total = [], 0, 0
    missing = {}
    for item, cnt in sorted(bom.items(), key=lambda kv: -kv[1]):
        tech = techdb.unlocking_tech(item)
        ok = (tech is None) or (tech in researched)
        total += cnt
        if ok:
            ready_count += cnt
        else:
            missing[tech] = missing.get(tech, 0) + cnt
        rows.append({"item": item, "count": cnt, "tech": tech,
                     "researched": ok, "producing": item in producing})
    pct = int(100 * ready_count / total) if total else 0
    role, role_why = classify_bp(bom)
    return {"game_version": f"{maj}.{minor}", "v2": maj == 2, "entity_count": total,
            "distinct_items": len(bom), "research_ready_pct": pct,
            "missing_techs": sorted(missing, key=lambda t: -missing[t]),
            "producing_pct": int(100 * sum(1 for r in rows if r["producing"]) / max(1, len(rows))),
            "role": role, "role_why": role_why,
            "bom": rows[:40]}


def bp_preview(name, child=0):
    """Entity layout of one blueprint (or one child of a book) for the client-side preview
    renderer. Books list their children so the modal can page through them."""
    import bplib
    try:
        s = bplib.load(name)[0]
        d = bplib.decode(s)
    except Exception as e:
        return {"error": str(e)[:200]}
    labels = []
    node = d
    if "blueprint_book" in d:
        kids = d["blueprint_book"].get("blueprints", [])
        if not kids:
            return {"error": "empty book"}
        labels = [(k.get("blueprint") or k.get("blueprint_book") or {}).get("label", f"#{i}")
                  for i, k in enumerate(kids)]
        node = kids[max(0, min(child, len(kids) - 1))]
        while "blueprint_book" in node:      # nested book: dive to its first blueprint
            node = node["blueprint_book"]["blueprints"][0]
    bp = node.get("blueprint", {})
    ents = [{"n": e["name"], "x": e["position"]["x"], "y": e["position"]["y"],
             "d": e.get("direction", 0)} for e in bp.get("entities", [])][:5000]
    return {"label": bp.get("label", name), "children": labels, "child": child, "ents": ents}


OVERRIDES = HERE / "bp-overrides.json"
SLOTS = ("oil-block", "robot-factory", "city-block", "rail-segments", "science", "smelting", "mall")


def classify_bp(bom):
    """Guess which build slot a print serves from its contents. Returns (slot, reason)."""
    n = lambda *names: sum(bom.get(x, 0) for x in names)
    rails = n("rail", "straight-rail", "curved-rail-a", "curved-rail-b", "rail-signal",
              "rail-chain-signal", "train-stop", "rail-ramp", "rail-support")
    total = max(1, sum(bom.values()))
    if rails / total > 0.3:
        return "rail-segments", f"{rails} rail pieces"
    if n("oil-refinery") >= 2 or (n("chemical-plant") >= 2 and n("pumpjack", "storage-tank")):
        return "oil-block", f"{n('oil-refinery')} refineries, {n('chemical-plant')} chem plants"
    if n("roboport") >= 2 and n("big-electric-pole", "substation") >= 4 and total < 200:
        return "city-block", "roboport/pole grid skeleton"
    if n("lab") >= 4:
        return "science", f"{n('lab')} labs"
    if n("stone-furnace", "steel-furnace", "electric-furnace") >= 8:
        return "smelting", f"{n('stone-furnace','steel-furnace','electric-furnace')} furnaces"
    if n("roboport") >= 1 and n("logistic-chest-passive-provider", "passive-provider-chest",
                                "logistic-chest-storage", "storage-chest") >= 2:
        return "robot-factory", "roboport + logistic chests"
    if n("assembling-machine-1", "assembling-machine-2", "assembling-machine-3") >= 10:
        return "mall", "many assemblers"
    return None, "no clear role"


def blueprint_catalog():
    import bplib
    import techdb
    researched, producing = _researched(), _producing()
    key = ("bpcat", len(researched), len(producing))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    out = []
    for meta in bplib.catalog():
        name = meta["name"]
        try:
            s = bplib.load(name)[0]
            a = analyze_bp(s, techdb, researched, producing)
            a.pop("bom", None)
        except Exception as e:
            a = {"error": str(e)[:120]}
        out.append({"name": name, "label": meta.get("label", ""), **a})
    result = {"prints": out, "overrides": _read_json("bp-overrides.json", {}), "slots": SLOTS}
    _cache[key] = (time.time(), result)
    return result


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
                # no-store: a cached page kept serving stale UI after fixes shipped
                data = (HERE / "dashboard.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
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
                "action": _read_json("action.json", {}),
                "prompts": [json.loads(x) for x in _tail("operator-inbox.jsonl", 6) if x.strip()],
                "ai": [json.loads(x) for x in _tail("llm-activity.jsonl", 12) if x.strip()],
            })
        elif self.path == "/api/terrain":
            self._send(terrain())
        elif self.path == "/api/blueprints":
            self._send(blueprint_catalog())
        elif self.path.startswith("/api/blueprints/preview"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            self._send(bp_preview(q.get("name", [""])[0], int(q.get("child", ["0"])[0])))
        elif self.path == "/api/map":
            self._send(live_map())
        elif self.path == "/api/derpface":
            self._send(derpface_window())
        elif self.path.startswith("/api/log"):
            self._send({"lines": _tail("autopilot.log", 60)})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        except ValueError:
            self._send({"error": "bad json"})
            return
        if self.path == "/api/blueprints/submit":
            import bplib
            import techdb
            name = "".join(ch if ch.isalnum() or ch in "-_" else "-"
                           for ch in (body.get("name") or "user-print").lower())[:48]
            s = (body.get("string") or "").strip()
            try:
                bplib.verify_2x(s)
                a = analyze_bp(s, techdb, _researched(), _producing())
                bplib.save("user-" + name, s, {"source_url": "dashboard-submit", "label": body.get("name", name)})
                _cache.pop(("bpcat",), None)
                self._send({"saved": "user-" + name, **a})
            except Exception as e:
                self._send({"error": str(e)[:300]})
        elif self.path == "/api/prompt":
            text = (body.get("text") or "").strip()
            if not text:
                self._send({"error": "empty prompt"})
                return
            row = {"id": int(time.time() * 1000), "ts": int(time.time()),
                   "text": text[:2000], "status": "pending", "result": ""}
            try:
                with open(HERE / "operator-inbox.jsonl", "a") as f:
                    f.write(json.dumps(row) + "\n")
                self._send({"ok": True, "id": row["id"]})
            except OSError as e:
                self._send({"error": str(e)[:200]})
        elif self.path == "/api/blueprints/select":
            slot, name = body.get("slot"), body.get("name")
            if slot not in SLOTS or not name:
                self._send({"error": f"slot must be one of {SLOTS}"})
                return
            ov = _read_json("bp-overrides.json", {})
            ov[slot] = name
            try:
                OVERRIDES.write_text(json.dumps(ov, indent=1))
                self._send({"ok": True, "overrides": ov})
            except OSError as e:
                self._send({"error": f"write failed ({e}) - is /app mounted read-only?"})
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    print(f"dashboard on :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
