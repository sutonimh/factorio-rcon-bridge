#!/usr/bin/env python3
"""Curate the Phase 0-2 blueprint library (MEGABASE-V2-DESIGN.md §4 shortlist).

Fetches each entry (factorio.school primary, factorioprints-Firebase fallback for
school keys; FactorioBin CDN for factoriobin posts), verifies 2.x, and saves it under
blueprints/library/ with a meta sidecar. Books are saved whole AND split into the
relevant children (matched by label substring, case-insensitive).

Rerunnable: overwrites existing entries with fresh fetches. Network only — no RCON.

Run: python3 curate_library.py
"""
import sys
import bplib

# (name, source, key, child-label substrings to also save individually)
SHORTLIST = [
    ("jumpstart-science3",            "school", "-OEAvLn7GVfCLngIvBSj", []),
    ("nilaus-sa-masterclass",         "school", "-OXRxN4v1U8dwIjSO4l4",
        ["starter base", "hub", "smelting", "science", "oil", "robots", "city blocks"]),
    ("nilaus-space-age",              "school", "-OBAyDy9PnXey5SMeUra",
        ["rail segment"]),
    ("elevated-train-city",           "school", "-OBMVXEGc7VfjNXQtso4", []),
    ("city-block-elevated",           "school", "-OE19hywPpVuQWsFJexu", []),
    ("tileable-labs",                 "school", "-OAA94aHsDaXcxqAAjKo", []),
    ("raynquist-balancers-fall2025",  "factoriobin", "cgn0od", []),
    # MAIN BUS + SCIENCE ARRAY (MAIN-BUS-PLAN.md). The science book is 0.17 vintage and is
    # migrated on the way in; its red/green children are the array's unit modules. Both use
    # fast-inserter, which this base has not researched - see bootstrap-*-science in the
    # library for the tier-downgraded variants that actually stamp.
    ("tileable-science-early-mid",    "school", "-KnQ865j-qQ21WoUPbd3",
        ["automation science", "logistic science"]),
    ("mainbus-splitters",             "school", "-Kzd-fbMeZaBtIuz7D7R", []),
    ("mainbus-4lane-t-junction",      "school", "-OC4gl7J2NQqgr0h1JDv", []),
]


def _fetch(source, key):
    """Returns (bp_string, meta). School keys fall back to the Firebase mirror."""
    if source == "factoriobin":
        return bplib.fetch_factoriobin(key)
    try:
        return bplib.fetch_school(key)
    except Exception as e:
        print("    factorio.school failed (%r), trying firebase mirror" % e)
        return bplib.fetch_firebase(key)


def _save_children(name, bp, meta, wanted):
    """Save book children whose label contains any wanted substring."""
    saved, labels = [], []
    for label, child in bplib.book_children(bp):
        labels.append(label)
        if not any(w in label.lower() for w in wanted):
            continue
        # short child name: label up to Nilaus's " - <book title>" boilerplate
        short = label.split(" - ")[0]
        cname = "%s--%s" % (name, bplib._slug(short) or "unlabeled")
        cmeta = dict(meta, label=label, parent_book=name)
        try:
            bplib.save(cname, bplib.encode(child), cmeta)
            saved.append(cname)
        except ValueError as e:
            print("    SKIP child %r: %s" % (label, e))
    return saved, labels


def main():
    failures = []
    for name, source, key, wanted in SHORTLIST:
        print("== %s (%s %s)" % (name, source, key))
        try:
            bp_string, meta = _fetch(source, key)
            bp = bplib.verify_2x(bp_string)
        except Exception as e:
            print("    FAILED: %r" % e)
            failures.append((name, repr(e)))
            continue
        m = bplib.save(name, bp_string, meta)
        print("    saved v%s  %s ents  footprint %s" % (
            m["game_version"], m["entity_count"], m["footprint_wh"]))
        if wanted and "blueprint_book" in bp:
            saved, labels = _save_children(name, bp, meta, wanted)
            print("    book children: %s" % ", ".join(repr(l) for l in labels))
            for c in saved:
                print("    + child saved: %s" % c)

    print("\n== catalog")
    for e in bplib.catalog():
        print("%-55s v%-6s %6s ents  %-18s %s" % (
            e["name"], e.get("game_version", "?"), e.get("entity_count", "?"),
            str(e.get("footprint_wh")), e.get("label", "")))
    if failures:
        print("\n== FAILURES")
        for name, err in failures:
            print("%-30s %s" % (name, err))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
