"""Task tracker that drives the in-game GUI note so it's ALWAYS current (Seth's rule:
no stale note). Tasks live in tasks.json with a status (active/queued/done). render()
rebuilds the notepad from the task list PLUS live game status (research %, labs working,
poles, fuel) pulled fresh each call, so even between task edits the note isn't stale.
The patrol calls render() every cycle.

CLI:  python3 tasks.py add "desc" [active|queued]
      python3 tasks.py status "desc-substr" active|queued|done
      python3 tasks.py render
"""
import json, os, sys

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tasks.json')


def load():
    try:
        return json.load(open(FILE))
    except Exception:
        return []


def save(tasks):
    json.dump(tasks, open(FILE, 'w'), indent=2)


def add(desc, status='queued'):
    tasks = load()
    if not any(t['desc'] == desc for t in tasks):
        tasks.append({'desc': desc, 'status': status})
        save(tasks)


def set_status(substr, status):
    tasks = load()
    for t in tasks:
        if substr.lower() in t['desc'].lower():
            t['status'] = status
    save(tasks)


def _live():
    """Pull fresh game status for the note header (so it's never stale)."""
    import autopilot as a
    out = a._print(
        "/sc local f=game.forces.player; local s=game.surfaces['nauvis'];"
        "local lw=0; for _,l in pairs(s.find_entities_filtered{name='lab'}) do if l.status==1 then lw=lw+1 end end;"
        "local fw=0; for _,e in pairs(s.find_entities_filtered{type='furnace'}) do if e.status==1 then fw=fw+1 end end;"
        "local stock=s.find_entities_filtered{position={20.5,-1.5},radius=1.5,type='container'}[1];"
        "local coal=stock and stock.get_inventory(1).get_item_count('coal') or 0;"
        "rcon.print(f.current_research.name..'|'..string.format('%.0f',f.research_progress*100)..'|'..lw..'|'..fw..'|'..coal)"
    ).strip()
    parts = (out.split('|') + ['?'] * 5)[:5]
    return dict(zip(['research', 'prog', 'labs', 'furn', 'coal'], parts))


def render():
    """Rebuild the GUI note from the task list + live status. Never stale."""
    import autopilot as a
    tasks = load()
    s = _live()
    lines = [f"RESEARCH: {s['research']} {s['prog']}% | labs {s['labs']}/4 | furnaces {s['furn']} | coal {s['coal']}"]
    active = [t['desc'] for t in tasks if t['status'] == 'active']
    queued = [t['desc'] for t in tasks if t['status'] == 'queued']
    if active:
        lines.append("--- ACTIVE ---")
        lines += ["* " + d for d in active]
    if queued:
        lines.append("--- QUEUED ---")
        lines += ["- " + d for d in queued]
    a.notepad(lines)
    return f"note rendered: {len(active)} active, {len(queued)} queued"


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'render'
    if cmd == 'add':
        add(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else 'queued')
        print('added')
    elif cmd == 'status':
        set_status(sys.argv[2], sys.argv[3])
        print('updated')
    print(render())
