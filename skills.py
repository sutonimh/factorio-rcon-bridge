"""What has WORKED, kept so it is reused rather than rediscovered.

memory.py is the failure half - it can tell you an approach went badly. This is the other
half: the approach that went well, with the parameter choices that made it go well, so the
next similar problem starts from the winning shape instead of from my hardcoded guess.

VOYAGER, AND HOW THIS DIFFERS
-----------------------------
Voyager (Wang et al.) grows a library of LLM-written JavaScript, each skill indexed by an
embedding of its docstring and retrieved by querying with the current plan. Two deliberate
departures:

  NO GENERATED CODE. Skills here name a procedure this repo already has - lay an ore lane,
  stamp a print, bridge to the grid - and carry the PARAMETERS that worked. Writing new
  executable code into a loop that drives a live base is a different risk class, and nothing
  about the learning needs it.

  NO EMBEDDINGS. Same reason as memory.py: a network call per decision inside a three-second
  control loop is unaffordable, and a skill that ranks differently between passes makes the
  builder oscillate. Retrieval reuses memory.relevance - weighted feature overlap, cheap and
  exactly reproducible.

WHY THIS IS NOT JUST advise() AGAIN
-----------------------------------
`advise` answers "should I do this?" about ONE plan. This answers "which of these plans should
I pick?" by ranking candidates against what has worked in comparable situations. That is the
difference between a veto and a preference, and the preference is what removes my hardcoded
constants: MIN_FEED_DRILLS exists because I watched a one-drill mine fail to feed forty
furnaces. The bot saw the same evidence. It should be able to reach the same conclusion.
"""
import json
import os
import pathlib
import time

import memory

HERE = pathlib.Path(__file__).resolve().parent
PATH = pathlib.Path(os.environ.get("SKILLS_PATH", HERE / "skills.jsonl"))

MIN_USES = 2          # below this a skill is an anecdote, not a preference
PRIOR = 1.0           # Laplace prior: one imagined win and loss, so a single fluke is not 100%


def _all(path=None):
    out = []
    try:
        for line in pathlib.Path(path or PATH).read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def _write(rows, path=None):
    try:
        p = pathlib.Path(path or PATH)
        p.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    except OSError:
        pass


def key(action, params):
    """A skill's identity: the action plus the parameter choices, not the situation it ran in."""
    return action + "|" + json.dumps(params or {}, sort_keys=True)


def record(action, params, context, outcome, path=None, now=None):
    """Fold one outcome into the library. Creates the skill on first use."""
    rows = _all(path)
    k = key(action, params)
    for r in rows:
        if r["key"] == k:
            break
    else:
        r = {"key": k, "action": action, "params": dict(params or {}),
             "wins": 0, "losses": 0, "contexts": [], "last": 0}
        rows.append(r)
    if outcome == "good":
        r["wins"] += 1
    else:
        r["losses"] += 1
    r["last"] = now if now is not None else time.time()
    # Keep a bounded sample of the situations it ran in - that is what retrieval matches on.
    if context and len(r["contexts"]) < 12:
        r["contexts"].append(dict(context))
    _write(rows, path)
    return r


def success(skill):
    """Laplace-smoothed win rate, so one lucky first attempt is not a 100% skill."""
    w, l = skill.get("wins", 0), skill.get("losses", 0)
    return (w + PRIOR) / (w + l + 2 * PRIOR)


def fit(skill, context, now=None):
    """How well this skill's past situations match the one in front of us."""
    cs = skill.get("contexts") or []
    if not cs:
        return 0.0
    return max(memory.relevance(context or {}, c) for c in cs)


def rank(action, context, path=None, now=None):
    """Skills for `action`, best first: proven, in situations like this one, recently."""
    out = []
    for s in _all(path):
        if s.get("action") != action:
            continue
        score = success(s) * (0.25 + 0.75 * fit(s, context, now)) * memory.recency(s.get("last"), now)
        out.append((score, s))
    out.sort(key=lambda t: (-t[0], t[1]["key"]))
    return [{"score": round(sc, 4), **s} for sc, s in out]


def prefer(action, context, candidates, param_of, path=None, now=None):
    """Reorder `candidates` by what has worked in situations like this one.

    THE POINT. `candidates` are the options a planner already generated; `param_of` maps each
    to the parameter dict that identifies it. Options the library has never seen keep their
    original relative order and sit after anything proven - unknown is not a demotion below
    something known-bad, and it is not a promotion either.

    Nothing is discarded. A preference that silently drops an option would make the base
    unrecoverable the moment the library learned something wrong, which - given a store fed by
    an automated verifier - is a when, not an if.
    """
    ranked = rank(action, context, path, now)
    by_key = {r["key"]: r for r in ranked}
    good, unseen, bad = [], [], []
    for i, c in enumerate(candidates):
        r = by_key.get(key(action, param_of(c)))
        if r is None or r["wins"] + r["losses"] < MIN_USES:
            unseen.append((i, c))                    # an anecdote ranks with the unknown
        elif success(r) > 0.5:
            good.append((r["score"], i, c))
        else:
            bad.append((r["score"], i, c))
    # PROVEN, THEN UNKNOWN, THEN KNOWN-BAD. Ordering purely by score would put a thoroughly
    # failed option above one never tried, which is backwards: an untried option might work,
    # and a repeatedly failed one is the thing the library exists to steer away from.
    good.sort(key=lambda t: (-t[0], t[1]))
    bad.sort(key=lambda t: (-t[0], t[1]))
    return ([c for _, _, c in good] + [c for _, c in unseen]
            + [c for _, _, c in bad])


def explain(action=None, path=None):
    rows = [s for s in _all(path) if action is None or s.get("action") == action]
    if not rows:
        return "no skills learned yet"
    rows.sort(key=lambda s: -success(s))
    return "\n".join(
        "%-14s %-42s %d win / %d loss  (%.0f%%)"
        % (s["action"], json.dumps(s["params"], sort_keys=True)[:42],
           s["wins"], s["losses"], 100 * success(s))
        for s in rows)
