"""What the operator's teardowns MEAN, kept as lessons rather than as forbidden ground.

THE OLD MODEL AND WHY IT WAS WRONG
----------------------------------
`record_operator_deletions` protected every tile the operator cleared, forever. Two failures,
and the operator named both:

  "the bot now treats the demolished ground as sacred and refuses to rebuild there"
      -> a coordinate blacklist sterilises perfectly good land. The builder printed
         "OPERATOR-OWNED ROUTE: 19/31 tiles are operator-protected" and declined to build a
         copper outpost on ground whose only crime was having held a bad one.

  "the bot should learn from the changes I make and not just keep repeating mistakes"
      -> and a coordinate blacklist teaches NOTHING. Move ten tiles left and the very same
         mistake is legal again, so the bot repeats it somewhere else and the operator tears
         it down again.

THE MODEL HERE
--------------
A removal is a CORRECTION, and a correction is about a KIND of thing built in a KIND of
situation - never about a place. "You put a smelter row where nothing consumed its output" is
portable; "do not build at (31,-40)" is not.

So each removal is reduced to a SIGNATURE - what was removed, in what role, with what was
wrong about it - and the builder asks "have I been corrected for this?" before building,
rather than "is this ground allowed?". The land stays free. The lesson travels.

Where a removal cannot be reduced to anything more specific than "the operator removed some
belt", that is recorded honestly as a weak correction rather than dressed up: a signature
nobody can act on should look useless, not authoritative.
"""
import collections
import json
import pathlib
import time

PATH = pathlib.Path(__file__).resolve().parent / "corrections.json"

# How many times the same signature must be corrected before the builder treats it as a hard
# rule rather than a caution. One removal can be the operator tidying; three is a policy.
HARD_AFTER = 2


def signature(kind, role=None, fault=None):
    """The portable identity of a mistake: what, doing what job, wrong how. Never a position."""
    return "|".join((kind or "?", role or "?", fault or "?"))


def diagnose(removed, world):
    """Turn a removal into a FAULT if the world explains it, else None.

    These are the faults this base has actually produced, and each is checkable from a census
    rather than guessed:

      orphan_output   a producer whose output row had nothing downstream consuming it
      unfed_input     a consumer whose input row nothing delivered to
      duplicate       a second structure of a kind already present and working
      disconnected    belt or inserter that touched nothing at either end

    Anything else is left undiagnosed on purpose - a fault name invented to fill the field
    would make a weak correction look strong.
    """
    kind = removed.get("kind")
    if kind in ("transport-belt", "underground-belt") and not removed.get("connected", True):
        return "disconnected"
    if kind in ("furnace", "assembling-machine"):
        if removed.get("output_consumed") is False:
            return "orphan_output"
        if removed.get("input_fed") is False:
            return "unfed_input"
    if removed.get("duplicate_of"):
        return "duplicate"
    return None


def load(path=None):
    try:
        return json.loads(pathlib.Path(path or PATH).read_text())
    except (OSError, ValueError):
        return {}


def save(db, path=None):
    try:
        pathlib.Path(path or PATH).write_text(json.dumps(db, indent=1, sort_keys=True))
    except OSError:
        pass


def record(removals, world=None, path=None, now=None):
    """Fold a batch of operator removals into the correction set. Returns the signatures hit."""
    db = load(path)
    hit = []
    for r in removals:
        sig = signature(r.get("kind"), r.get("role"), diagnose(r, world or {}))
        row = db.setdefault(sig, {"count": 0, "first": None, "last": None, "examples": []})
        row["count"] += 1
        stamp = now or time.strftime("%Y-%m-%d %H:%M:%S")
        row["first"] = row["first"] or stamp
        row["last"] = stamp
        if len(row["examples"]) < 5 and r.get("where"):
            row["examples"].append(list(r["where"]))
        hit.append(sig)
    save(db, path)
    return hit


def check(kind, role=None, fault=None, path=None):
    """Has the operator corrected this kind of build before? Returns None or the record.

    Deliberately keyed on the SIGNATURE, so a correction earned at one end of the map applies
    at the other. That is the whole difference from protecting tiles.
    """
    db = load(path)
    row = db.get(signature(kind, role, fault))
    if not row:
        return None
    row = dict(row)
    row["hard"] = row["count"] > HARD_AFTER
    return row


def explain(path=None):
    db = load(path)
    if not db:
        return "no corrections recorded"
    out = []
    for sig, row in sorted(db.items(), key=lambda kv: -kv[1]["count"]):
        kind, role, fault = sig.split("|")
        out.append("%-2dx %s as %s%s%s"
                   % (row["count"], kind, role,
                      "" if fault == "?" else " (fault: %s)" % fault,
                      "  [HARD RULE]" if row["count"] > HARD_AFTER else ""))
    return "\n".join(out)


def undiagnosed(path=None):
    """Signatures with no fault - the ones that teach nothing yet. Worth surfacing rather than
    hiding: they are where the model is still guessing."""
    return sorted(s for s in load(path) if s.endswith("|?"))
