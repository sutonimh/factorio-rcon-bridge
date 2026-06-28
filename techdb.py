#!/usr/bin/env python3
"""Tech-tree database + queries (Seth's rule: work from a research DB so gating issues are
known up front, never discovered by a failed craft).

Source of truth: tech-tree.json, dumped from the live game (see scripts in autopilot/session).
Each tech: {prerequisites:[...], count:N, packs:{pack:amount}, unlocks:[recipe...],
trigger:{type,entity?,item?,count?}|null}. Plus recipe_to_tech: {recipe: tech}.

Typical use before building something gated:
    import techdb
    techdb.report('roboport')          # human-readable research plan to that recipe
    techdb.plan_for_recipe('roboport') # ordered tech list (deps first), trigger techs flagged
"""
import json, pathlib

HERE = pathlib.Path(__file__).resolve().parent
_DB = None


def db():
    global _DB
    if _DB is None:
        _DB = json.loads((HERE / "tech-tree.json").read_text())
    return _DB


def tech(name):
    return db()["techs"].get(name)


def unlocking_tech(recipe):
    """Which technology enables this recipe (None if available from start / unknown)."""
    return db()["recipe_to_tech"].get(recipe)


def is_trigger(name):
    t = tech(name)
    return bool(t and t.get("trigger"))


def packs_for(name):
    t = tech(name)
    return t.get("packs") if t else None


def prereq_chain(name):
    """Ordered list of every tech needed to research `name` (dependencies first, `name`
    last), de-duplicated. Use to know the full research path before committing."""
    order, seen = [], set()

    def visit(n):
        if n in seen:
            return
        seen.add(n)
        t = tech(n)
        if not t:
            return
        for p in t.get("prerequisites", []):
            visit(p)
        order.append(n)

    visit(name)
    return order


def plan_for_recipe(recipe):
    """Ordered tech list to unlock a recipe (deps first). None if recipe needs no tech."""
    t = unlocking_tech(recipe)
    return prereq_chain(t) if t else None


def all_packs_for_chain(chain):
    """Union of science packs across a research chain -> the science you must produce."""
    packs = set()
    for n in chain:
        for p in (packs_for(n) or {}):
            packs.add(p)
    return sorted(packs)


def report(recipe):
    """Human-readable research plan to unlock a recipe: the tech path, trigger techs
    flagged, and the science packs required along the way."""
    t = unlocking_tech(recipe)
    if not t:
        return f"{recipe}: no tech gate (craftable once you have the items)"
    chain = prereq_chain(t)
    lines = [f"{recipe} <- tech '{t}'  ({len(chain)} techs in path)"]
    for n in chain:
        tg = tech(n).get("trigger")
        flag = ""
        if tg:
            det = tg.get("entity") or tg.get("item") or ""
            flag = f"  [TRIGGER {tg.get('type')}{(' ' + det) if det else ''}]"
        pk = packs_for(n) or {}
        pkstr = ",".join(sorted(pk)) if pk else "-"
        lines.append(f"  {n}  packs={pkstr}{flag}")
    lines.append("science needed overall: " + ", ".join(all_packs_for_chain(chain)))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    for r in (sys.argv[1:] or ["assembling-machine-1", "roboport", "construction-robot"]):
        print(report(r))
        print()
