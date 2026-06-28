"""Continuous maintenance patrol: smooth-walk the open base perimeter, and at each
stop run the full maintenance pass (pickup, fill ore chests, run the green science
factory, keep everything fueled from the coal chest) + feed all labs. Research
advances meanwhile. Perimeter points are open ground (no weaving through the build)."""
import autopilot as a
import time

PERIMETER = [(26, -10), (26, -32), (-14, -32), (-14, -12)]


def restock_and_craft():
    # pull copper from the copper furnace outputs, craft a science buffer to keep labs fed
    a._print(
        "/sc local s=game.surfaces['nauvis']; local p=game.players[1]; local inv=p.get_main_inventory();"
        "local cu=0; for _,f in pairs(s.find_entities_filtered{area={{-3,-46},{24,-40}},type='furnace'}) do"
        " local o=f.get_output_inventory(); local c=o.get_item_count('copper-plate'); if c>0 then"
        " local n=math.min(c,250-cu); o.remove{name='copper-plate',count=n}; inv.insert{name='copper-plate',count=n}; cu=cu+n end"
        " if cu>=250 then break end end;"
        "p.begin_crafting{recipe='logistic-science-pack',count=20};"
        "p.begin_crafting{recipe='automation-science-pack',count=15}"
    )


CYCLES = 16
for cyc in range(CYCLES):
    wx, wy = PERIMETER[cyc % len(PERIMETER)]
    a.walk(wx, wy, tol=3, timeout=70)
    a.maintain()
    if cyc % 3 == 0:
        restock_and_craft()
    out = a.feed_labs().strip()
    print(f"[patrol {cyc+1}/{CYCLES}] at ({wx},{wy}) | {out}", flush=True)

print("patrol stint complete", flush=True)
