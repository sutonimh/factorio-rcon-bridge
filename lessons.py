#!/usr/bin/env python3
"""Lesson store: the automated GOTCHAS.md (MEGABASE-V2-DESIGN §6).

Every executor post-condition failure, triage anomaly, and architect finding lands here as a
structured lesson; the top-K relevant lessons are injected into every LLM prompt so mistakes
compound into rules. Repeat-firing lessons get promoted to codegen candidates.

Storage is JSONL (lessons.jsonl), NOT SQLite: the autopilot dir lives on /mnt/user (Unraid
shfs), where SQLite throws disk I/O error 522. Append-only + tiny, so JSONL is the right
shape anyway. Runtime file, gitignored.
"""
import json
import os
import pathlib
import time

HERE = pathlib.Path(__file__).resolve().parent
PATH = pathlib.Path(os.environ.get("LESSONS_PATH", HERE / "lessons.jsonl"))


def add(condition, mistake, rule, evidence="", phase=None, tags=(), key=None, world=None):
    """Record (or count-bump) a lesson.

    Dedup: `key` (a STABLE structured id like "issue:grid_split" or "triage:coal") when
    given, else exact (condition, mistake) - LLM prose never repeats exactly, which kept
    every lesson at count=1 and made promotion unreachable (audit 2026-08-29). `world`
    scopes coordinate-bearing lessons to one map so stale coords don't outlive it."""
    rows = _all()
    for r in rows:
        if (key and r.get("key") == key) or (not key and r["condition"] == condition and r["mistake"] == mistake):
            r["count"] += 1
            r["last_ts"] = int(time.time())
            if evidence:
                r["evidence"] = evidence[-2000:]
            _write(rows)
            return r
    row = {"id": max((r["id"] for r in rows), default=0) + 1, "key": key, "world": world,
           "condition": condition, "mistake": mistake, "rule": rule,
           "evidence": evidence[-2000:], "phase": phase, "tags": sorted(tags),
           "count": 1, "ts": int(time.time()), "last_ts": int(time.time()),
           "promoted": False}
    rows.append(row)
    _write(rows)
    return row


def relevant(phase=None, tags=(), k=8):
    """Top-K lessons for a prompt: tag/phase matches first, then most-fired, then newest."""
    tags = set(tags)

    def score(r):
        return (len(tags & set(r["tags"])), 1 if r["phase"] == phase else 0,
                r["count"], r["last_ts"])

    return sorted(_all(), key=score, reverse=True)[:k]


def promotable(min_count=3):
    """Lessons that keep firing and have no codified fix yet -> Coder-30B candidates."""
    return [r for r in _all() if r["count"] >= min_count and not r["promoted"]]


def mark_promoted(lesson_id):
    rows = _all()
    for r in rows:
        if r["id"] == lesson_id:
            r["promoted"] = True
    _write(rows)


def prompt_block(phase=None, tags=(), k=8):
    """Render lessons as a compact prompt section ('' if none)."""
    rows = relevant(phase, tags, k)
    if not rows:
        return ""
    lines = ["Hard-won lessons from this base (follow them):"]
    for r in rows:
        lines.append(f"- WHEN {r['condition']}: {r['rule']} (seen {r['count']}x)")
    return "\n".join(lines)


def _all():
    if not PATH.exists():
        return []
    return [json.loads(line) for line in PATH.read_text().splitlines() if line.strip()]


def _write(rows):
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(PATH)


if __name__ == "__main__":
    for r in _all():
        print(f"[{r['count']}x]{' P' if r['promoted'] else ''} {r['condition']} -> {r['rule']}")
