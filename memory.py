"""Derpface's own experience: what he tried, in what situation, and how it turned out.

WHY THIS EXISTS
---------------
The operator, watching a day of me hand-correcting the bot (2026-08-31): "i feel like the
self learning part of this isnt working as intended and instead you are the one correcting
mistakes."

He is right, and the audit is unambiguous. `lessons.py` has fifteen write sites and exactly
ONE read site - the architect's LLM prompt. `corrections.check()` has none at all. So the bot
generates evidence about its own failures and no decision it makes ever consults it. Today it
produced the evidence for four separate corrections - nearest-mine-not-biggest, ore-blind
block assignment, lane duplication, three wrong "is it fed?" heuristics - and learned nothing
from any of them, because I was the feedback loop.

PRIOR ART, AND WHAT IS TAKEN FROM IT
------------------------------------
  Generative Agents (Park et al.)  the retrieval score, recency * importance * relevance,
                                   with recency as exponential decay and reflection that
                                   distils repeated observations into higher-level rules.
  Voyager (Wang et al.)            a skill library keyed by a description, grown as things
                                   succeed, so a working solution is reused rather than
                                   rediscovered.
  RATs / EvoTrainer                two stores, not one: successes become skills, failures
                                   become compact lessons. Failure memory is the half this
                                   repo kept writing and never reading.

RELEVANCE IS FEATURE OVERLAP, NOT EMBEDDINGS. Voyager and Generative Agents both embed text
and compare vectors. This runs inside a three-second control loop against a live game, where
a network call per decision is not affordable and non-determinism is actively harmful - a
fixer whose target shuffles between passes is worse than none. Contexts here are small dicts
of measured facts, so a weighted overlap is both cheap and exactly reproducible.

THE POINT IS `advise()`. Everything else is bookkeeping. `advise` is what a builder calls
BEFORE acting, and it answers from experience rather than from a rule someone hardcoded.
"""
import json
import math
import os
import pathlib
import time

HERE = pathlib.Path(__file__).resolve().parent
# JSONL, not SQLite: the autopilot dir is on Unraid's /mnt/user, whose shfs FUSE layer breaks
# SQLite with "disk I/O error 522". lessons.py learned this the hard way; same choice here.
PATH = pathlib.Path(os.environ.get("MEMORY_PATH", HERE / "memory.jsonl"))

HALF_LIFE_S = 6 * 3600.0     # an observation is worth half as much six hours later
W_RECENCY = 1.0
W_IMPORTANCE = 1.0
W_RELEVANCE = 2.0            # what happened in a SIMILAR situation matters most
MIN_EVIDENCE = 2             # below this, advise() reports low confidence rather than a verdict


# --------------------------------------------------------------------------- scoring
def recency(ts, now=None, half_life=HALF_LIFE_S):
    """Exponential decay, 1.0 at now. Old evidence still counts, it just counts less - a base
    changes, and a lane that failed yesterday for reasons since fixed should not veto forever."""
    now = time.time() if now is None else now
    age = max(0.0, now - (ts or 0))
    return 0.5 ** (age / max(1.0, half_life))


def relevance(a, b):
    """Fuzzy match between two context dicts: the share of `a`'s facts that `b` agrees with.

    Exact-key matching is what made `corrections.check()` useless in practice - a signature had
    to line up perfectly to retrieve anything, so almost nothing ever did. Partial agreement is
    the normal case: "a lane, from a small mine, to a big block" should partly match "a lane,
    from a small mine, to a small block" and inform the decision proportionally.
    """
    if not a:
        return 0.0
    hits = 0.0
    for k, v in a.items():
        if k not in b:
            continue
        ov = b[k]
        if v == ov:
            hits += 1.0
        elif isinstance(v, (int, float)) and isinstance(ov, (int, float)):
            lo, hi = sorted((abs(float(v)), abs(float(ov))))
            hits += (lo / hi) if hi else 1.0        # numerically close is partly the same
    return hits / len(a)


def score(row, context, now=None):
    return (W_RECENCY * recency(row.get("ts"), now)
            + W_IMPORTANCE * float(row.get("importance", 1.0))
            + W_RELEVANCE * relevance(context, row.get("context") or {}))


# --------------------------------------------------------------------------- store
def _all(path=None):
    p = pathlib.Path(path or PATH)
    out = []
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue          # a torn line must not poison the whole memory
    except OSError:
        pass
    return out


def remember(action, context, outcome, importance=1.0, detail="", path=None, now=None):
    """Record one experience. `outcome` is 'good' or 'bad' - the thing worth knowing later."""
    row = {"ts": now if now is not None else time.time(), "action": action,
           "context": dict(context or {}), "outcome": outcome,
           "importance": float(importance), "detail": detail[:200]}
    p = pathlib.Path(path or PATH)
    try:
        with p.open("a") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass
    return row


def recall(action=None, context=None, k=8, path=None, now=None):
    """The k most pertinent past experiences, best first."""
    rows = [r for r in _all(path) if action is None or r.get("action") == action]
    return sorted(rows, key=lambda r: score(r, context or {}, now), reverse=True)[:k]


# --------------------------------------------------------------------------- the point
def advise(action, context, path=None, now=None, k=12):
    """{'verdict','confidence','good','bad','why'} - what experience says about doing this.

    Returns a JUDGEMENT, not a lookup. Every past attempt is weighted by how similar its
    situation was and how recently it happened, so evidence from a near-identical failure
    yesterday outweighs a vaguely-related success last week.

    'unknown' with low confidence is a real and common answer, and callers must treat it as
    "no information" rather than "permission" - the bot has never done most things.
    """
    rows = recall(action, context, k=k, path=path, now=now)
    good = bad = 0.0
    why = []
    for r in rows:
        w = relevance(context or {}, r.get("context") or {}) * recency(r.get("ts"), now)
        if w <= 0:
            continue
        if r.get("outcome") == "good":
            good += w
        elif r.get("outcome") == "bad":
            bad += w
            if len(why) < 3 and r.get("detail"):
                why.append(r["detail"])
    total = good + bad
    if total <= 0 or len(rows) < MIN_EVIDENCE:
        return {"verdict": "unknown", "confidence": 0.0, "good": good, "bad": bad,
                "why": "no comparable experience"}
    ratio = good / total
    verdict = "good" if ratio >= 0.6 else "bad" if ratio <= 0.4 else "mixed"
    return {"verdict": verdict, "confidence": round(min(1.0, total / 3.0), 2),
            "good": round(good, 3), "bad": round(bad, 3),
            "why": "; ".join(why) or "no failure detail recorded"}


def reflect(path=None, now=None, min_count=3, min_ratio=0.75):
    """Distil repeated failures into rules - the Generative Agents reflection step.

    An action that has gone badly `min_count` times, in `min_ratio` of its attempts, is no
    longer an incident; it is a pattern, and belongs in lessons.py where the architect's
    prompt will actually see it.
    """
    tally = {}
    for r in _all(path):
        t = tally.setdefault(r.get("action"), {"good": 0, "bad": 0, "detail": ""})
        if r.get("outcome") == "good":
            t["good"] += 1
        elif r.get("outcome") == "bad":
            t["bad"] += 1
            t["detail"] = r.get("detail") or t["detail"]
    out = []
    for action, t in sorted(tally.items()):
        n = t["good"] + t["bad"]
        if t["bad"] >= min_count and n and (t["bad"] / n) >= min_ratio:
            out.append({"action": action, "bad": t["bad"], "of": n, "detail": t["detail"]})
    return out


def summary(path=None):
    rows = _all(path)
    if not rows:
        return "no experience recorded yet"
    good = sum(1 for r in rows if r.get("outcome") == "good")
    bad = sum(1 for r in rows if r.get("outcome") == "bad")
    acts = sorted({r.get("action") for r in rows})
    return ("%d experiences (%d good, %d bad) across %d actions: %s"
            % (len(rows), good, bad, len(acts), ", ".join(acts[:6])))
