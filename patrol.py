"""Continuous maintenance patrol: smooth-walk the open base perimeter, and at each
stop run the full maintenance pass (pickup, fill ore chests, run the green science
factory, keep everything fueled from the coal chest) + feed all labs. Research
advances meanwhile. Perimeter points are open ground (no weaving through the build)."""
import autopilot as a
import time

PERIMETER = [(26, -10), (26, -32), (-14, -32), (-14, -12)]


def restock_and_craft():
    # Pull copper/iron ONLY when low (never over-pull - that clogged the inventory with
    # thousands of plates). Craft a small science buffer to keep labs fed.
    a._print(
        "/sc local s=game.surfaces['nauvis']; local p=game.players[1]; local inv=p.get_main_inventory();"
        # copper: top up to ~250 only if below 150
        "if inv.get_item_count('copper-plate')<150 then local need=250-inv.get_item_count('copper-plate');"
        " for _,f in pairs(s.find_entities_filtered{area={{-3,-46},{24,-40}},type='furnace'}) do local o=f.get_output_inventory();"
        "  local c=math.min(o.get_item_count('copper-plate'),need); if c>0 then o.remove{name='copper-plate',count=c}; inv.insert{name='copper-plate',count=c}; need=need-c end if need<=0 then break end end end;"
        # iron: top up to ~250 only if below 150
        "if inv.get_item_count('iron-plate')<150 then local need=250-inv.get_item_count('iron-plate');"
        " for _,f in pairs(s.find_entities_filtered{area={{-3,-33},{24,-28}},type='furnace'}) do local o=f.get_output_inventory();"
        "  local c=math.min(o.get_item_count('iron-plate'),need); if c>0 then o.remove{name='iron-plate',count=c}; inv.insert{name='iron-plate',count=c}; need=need-c end if need<=0 then break end end end;"
        "if inv.get_item_count('transport-belt')<40 then p.begin_crafting{recipe='transport-belt',count=40} end;"
        "if inv.get_item_count('logistic-science-pack')<20 then p.begin_crafting{recipe='logistic-science-pack',count=30} end;"
        "if inv.get_item_count('automation-science-pack')<20 then p.begin_crafting{recipe='automation-science-pack',count=30} end"
    )


import subprocess
import tasks

CYCLES = 80
for cyc in range(CYCLES):
    # STAND STILL: maintenance is all server-side, so the character stays put and only
    # moves when a task explicitly needs it on-site (a build/repair calls goto()). Seth's
    # rule: no aimless perimeter walking.
    restock_and_craft()      # keep a science buffer crafting
    a.maintain()             # pickup, ore chests, green factory, components, fuel ALL burners, feed labs, cleanup orphans
    if cyc % 10 == 9:        # periodic deep prune of redundant (connected) power poles
        subprocess.run(['python3', 'remove_redundant.py'], cwd='/Users/sutonimh/code/factorio')
    out = a.feed_labs().strip()
    tasks.render()   # keep the GUI note fresh (never stale)
    print(f"[patrol {cyc+1}/{CYCLES}] (stationary) | {out}", flush=True)
    time.sleep(15)   # wait between maintenance passes; character does NOT wander

print("patrol stint complete", flush=True)
