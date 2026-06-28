"""Continuous maintenance patrol: smooth-walk the open base perimeter, and at each
stop run the full maintenance pass (pickup, fill ore chests, run the green science
factory, keep everything fueled from the coal chest) + feed all labs. Research
advances meanwhile. Perimeter points are open ground (no weaving through the build)."""
import autopilot as a
import time

PERIMETER = [(26, -10), (26, -32), (-14, -32), (-14, -12)]


def restock_and_craft():
    # pull copper + iron from furnace outputs, craft a science buffer to keep ALL labs fed
    a._print(
        "/sc local s=game.surfaces['nauvis']; local p=game.players[1]; local inv=p.get_main_inventory();"
        "local cu=0; for _,f in pairs(s.find_entities_filtered{area={{-3,-46},{24,-40}},type='furnace'}) do"
        " local o=f.get_output_inventory(); local c=o.get_item_count('copper-plate'); if c>0 then"
        " local n=math.min(c,400-cu); o.remove{name='copper-plate',count=n}; inv.insert{name='copper-plate',count=n}; cu=cu+n end"
        " if cu>=400 then break end end;"
        "local fe=0; for _,f in pairs(s.find_entities_filtered{area={{-3,-33},{24,-28}},type='furnace'}) do"
        " local o=f.get_output_inventory(); local c=o.get_item_count('iron-plate'); if c>0 then"
        " inv.insert{name='iron-plate',count=c}; o.remove{name='iron-plate',count=c}; fe=fe+c end if fe>=400 then break end end;"
        # belts feed the green sub-factory's final stage (green = belt + inserter); keep them stocked
        "if inv.get_item_count('transport-belt')<40 then p.begin_crafting{recipe='transport-belt',count=60} end;"
        "if inv.get_item_count('logistic-science-pack')<20 then p.begin_crafting{recipe='logistic-science-pack',count=30} end;"
        "if inv.get_item_count('automation-science-pack')<20 then p.begin_crafting{recipe='automation-science-pack',count=30} end"
    )


import subprocess

CYCLES = 20
for cyc in range(CYCLES):
    wx, wy = PERIMETER[cyc % len(PERIMETER)]
    a.walk(wx, wy, tol=3, timeout=70)
    restock_and_craft()      # keep a science buffer crafting every lap
    a.maintain()             # pickup, ore chests, green factory, components, fuel ALL burners, feed labs, cleanup orphans
    if cyc % 10 == 9:        # periodic deep prune of redundant (connected) power poles
        subprocess.run(['python3', 'remove_redundant.py'], cwd='/Users/sutonimh/code/factorio')
    out = a.feed_labs().strip()
    print(f"[patrol {cyc+1}/{CYCLES}] at ({wx},{wy}) | {out}", flush=True)

print("patrol stint complete", flush=True)
