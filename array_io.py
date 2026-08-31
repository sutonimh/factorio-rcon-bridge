"""Work out which belt of a stamped array is an INPUT and which is an OUTPUT.

A smelting print is a sandwich - belt, inserters, furnaces, inserters, belt - and from the
outside the belts look identical. Guessing wrong is how the main bus came to be fed from two
smelter INPUT belts, which the operator had to fix by hand.

The inserters already know. An inserter that picks from a belt row and drops into a machine
row makes that belt an INPUT; one that picks from the machine row and drops onto a belt makes
it an OUTPUT. That is a fact on the map, not an inference from layout, so it survives the
print being mirrored, rotated, or replaced with a different one.

This is the same technique feed_planner.chain_graph uses to find a lab grid's head: ask the
inserters what they are doing rather than reading the shape.
"""


def classify(inserters, machine_rows):
    """{'input': [rows], 'output': [rows]} from inserter pick/drop rows.

    `inserters` is a list of (pick_row, drop_row); `machine_rows` the rows holding furnaces or
    assemblers. A row is an INPUT if something lifts off it into a machine, an OUTPUT if
    something lands on it from a machine. A row that is both is reported in both lists - that
    is a real configuration (a shared middle belt) and hiding it would be worse than saying so.
    """
    machines = set(machine_rows)
    inputs, outputs = set(), set()
    for pick, drop in inserters:
        if drop in machines and pick not in machines:
            inputs.add(pick)
        elif pick in machines and drop not in machines:
            outputs.add(drop)
    return {"input": sorted(inputs), "output": sorted(outputs)}


def feed_end(belt_dirs):
    """Which END of a west-flowing input row you must feed: items enter where they come FROM.

    Returns 'east' for a row flowing west (direction 12) and 'west' for one flowing east (4).
    Feeding the wrong end puts ore on a belt that immediately carries it away from the
    furnaces, which looks like a connected lane and delivers nothing.
    """
    if not belt_dirs:
        return None
    west = sum(1 for d in belt_dirs if d == 12)
    east = sum(1 for d in belt_dirs if d == 4)
    if west > east:
        return "east"
    if east > west:
        return "west"
    return None


def read(A, x1, y1, x2, y2):
    """Live: (io, machine_rows, belt_rows) for the array in the given box."""
    raw = A._print(
        "/sc local s=game.surfaces[1] local o={} "
        "for _,e in pairs(s.find_entities_filtered{type='inserter',"
        "  area={{%d,%d},{%d,%d}}}) do "
        "  o[#o+1]='I|'..math.floor(e.pickup_position.y)..'|'..math.floor(e.drop_position.y) end "
        "for _,e in pairs(s.find_entities_filtered{type={'furnace','assembling-machine'},"
        "  area={{%d,%d},{%d,%d}}}) do o[#o+1]='M|'..math.floor(e.position.y) end "
        "for _,b in pairs(s.find_entities_filtered{type='transport-belt',"
        "  area={{%d,%d},{%d,%d}}}) do "
        "  o[#o+1]='B|'..math.floor(b.position.y)..'|'..b.direction end "
        "rcon.print(table.concat(o,';'))"
        % (x1, y1, x2, y2, x1, y1, x2, y2, x1, y1, x2, y2)).strip()
    ins, machines, belts = [], set(), {}
    for tok in raw.split(";"):
        f = tok.split("|")
        if f[0] == "I" and len(f) == 3:
            ins.append((int(f[1]), int(f[2])))
        elif f[0] == "M" and len(f) == 2:
            machines.add(int(f[1]))
        elif f[0] == "B" and len(f) == 3:
            belts.setdefault(int(f[1]), []).append(int(f[2]))
    return classify(ins, machines), sorted(machines), belts


def describe(io, belts):
    parts = []
    for row in io["input"]:
        parts.append("input y=%d (feed from the %s)" % (row, feed_end(belts.get(row, [])) or "?"))
    for row in io["output"]:
        parts.append("output y=%d" % row)
    return ", ".join(parts) or "no belt-to-machine inserters found"
