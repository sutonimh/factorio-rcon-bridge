#!/usr/bin/env python3
"""Status reporting (CHARON-MIGRATION Phase 4): the autopilot writes a compact heartbeat so a
Claude session can check on derpface between logins, and a rolling log so stalls are visible.

A Claude session resuming the project:
    ssh charon cat /mnt/user/appdata/factorio-autopilot/status.json   # current state
    ssh charon tail -50 /mnt/user/appdata/factorio-autopilot/autopilot.log
then steers via RCON (charon:27015) exactly like a local session. See CHARON-MIGRATION.md.
"""
import json
import time
import threading
import pathlib
import autopilot as A

HERE = pathlib.Path(__file__).resolve().parent
STATUS_PATH = HERE / "status.json"
LOG_PATH = HERE / "autopilot.log"
_lock = threading.Lock()
_recent = []   # last N log lines, surfaced in status.json


def log(msg):
    """Append a timestamped line to autopilot.log + the recent-log ring (replaces silent
    except:pass so stalls/errors are visible)."""
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    with _lock:
        _recent.append(line)
        del _recent[:-40]
        try:
            with open(LOG_PATH, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


def write_status(build_queue=None):
    """Gather a compact game-state snapshot in ONE RCON call and write status.json (the heartbeat
    a Claude session reads). Cheap enough to call every maintenance lap."""
    raw = A._print(
        "/sc local f=game.forces.player; local s=game.surfaces[1]; local d=storage.derpface;"
        "local eng=s.find_entities_filtered{name='steam-engine'}[1]; local b=s.find_entities_filtered{name='boiler'}[1];"
        "local labs=s.find_entities_filtered{name='lab'}; local lr=0; for _,l in pairs(labs) do if l.status==1 then lr=lr+1 end end;"
        "local asm=s.find_entities_filtered{type='assembling-machine'}; local aw=0; for _,a in pairs(asm) do if a.status==1 then aw=aw+1 end end;"
        "local function mf(name) local m=999; for _,dr in pairs(s.find_entities_filtered{name='burner-mining-drill'}) do local fb=dr.get_fuel_inventory(); local c=fb and fb.get_item_count('coal') or 0; if c<m then m=c end end; return m==999 and -1 or m end;"
        "local parts={};"
        "parts['ticks']=game.tick;"
        "parts['research']=f.current_research and f.current_research.name or 'BLOCKED/none';"
        "parts['research_pct']=math.floor((f.research_progress or 0)*100);"
        "parts['engine_energy']=eng and math.floor(eng.energy) or 0;"
        "parts['boiler_fuel']=b and b.get_fuel_inventory().get_item_count('coal') or 0;"
        "parts['labs_running']=lr; parts['labs_total']=#labs;"
        "parts['assemblers_working']=aw; parts['assemblers_total']=#asm;"
        "parts['drills']=#s.find_entities_filtered{name='burner-mining-drill'};"
        "parts['min_drill_fuel']=mf();"
        "parts['derp_x']=d and math.floor(d.position.x) or 0; parts['derp_y']=d and math.floor(d.position.y) or 0;"
        "parts['derp_walking']=tostring(d and d.walking_state.walking);"
        "parts['derp_coal']=d and d.get_main_inventory().get_item_count('coal') or 0;"
        "local out={}; for k,v in pairs(parts) do out[#out+1]=k..'\\t'..tostring(v) end;"
        "rcon.print(table.concat(out,'\\n'))").strip()
    st = {}
    for ln in raw.split("\n"):
        if "\t" in ln:
            k, v = ln.split("\t", 1)
            try:
                st[k] = int(v)
            except ValueError:
                st[k] = v
    power_ok = st.get("boiler_fuel", 0) > 0
    data = {
        "ts": int(time.time()),
        "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "game_hours": round(st.get("ticks", 0) / 60 / 3600, 2),
        "research": {"current": st.get("research"), "pct": st.get("research_pct"),
                     "blocked": st.get("research") in (None, "BLOCKED/none")},
        "power_ok": power_ok,
        "engine_energy": st.get("engine_energy"),
        "boiler_fuel": st.get("boiler_fuel"),
        "labs": f"{st.get('labs_running')}/{st.get('labs_total')}",
        "assemblers": f"{st.get('assemblers_working')}/{st.get('assemblers_total')}",
        "drills": st.get("drills"),
        "min_drill_fuel": st.get("min_drill_fuel"),
        "derpface": {"pos": [st.get("derp_x"), st.get("derp_y")],
                     "walking": st.get("derp_walking"), "coal": st.get("derp_coal")},
        "build_queue": [getattr(t, "__name__", str(t)) for t in (build_queue or [])],
        "recent_log": _recent[-12:],
    }
    with _lock:
        try:
            STATUS_PATH.write_text(json.dumps(data, indent=2))
        except Exception:
            pass
    return data


if __name__ == "__main__":
    print(json.dumps(write_status(), indent=2))
