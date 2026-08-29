#!/usr/bin/env python3
"""Lesson -> patch pipeline: Coder-30B drafts fixes for promoted lessons (MEGABASE-V2-DESIGN §6).

A lesson that keeps firing (lessons.promotable()) has earned a codified fix -- a guard, an
adjusted threshold, a new post-condition. This module gathers the relevant function source,
asks halo's Coder-30B for a MINIMAL unified diff, validates the diff offline (applies to a
temp copy + py_compile), and writes the result to proposals/<id>-<slug>.patch with a .md
summary alongside. Proposals are working artifacts: a human (or a Mac-side Claude session)
reviews and turns a good one into a branch + PR per WORKFLOW.md. This module NEVER touches
the real tree, never commits, never auto-applies -- "never auto-merged" is the design's rule
and it starts here.

CLI: python3 codegen.py [lesson_id]     (no id -> every promotable lesson)
"""
import json
import pathlib
import py_compile
import re
import shutil
import sys
import tempfile

import lessons
import llm

HERE = pathlib.Path(__file__).resolve().parent
PROPOSALS = HERE / "proposals"

MAX_CONTEXT_CHARS = 20000  # function-source budget; llm.py hard-caps the whole prompt at 40k

SYSTEM = (
    "You harden a Python Factorio-autopilot codebase by codifying one hard-won lesson as a "
    "MINIMAL code change: a guard clause, an adjusted threshold, or a post-condition check. "
    "Change as few lines as possible; never refactor, rename, or reformat. Match the "
    "surrounding style (stdlib only, terse comments). Reply with ONLY a unified diff -- "
    "'--- a/<file>' / '+++ b/<file>' headers and @@ hunks with 3 context lines. No prose, "
    "no code fences, no explanation before or after the diff."
)


# --------------------------------------------------------------- context gathering
def _candidate_names(lesson):
    """Function names hinted by the lesson: traceback frames first, then call-ish tokens."""
    text = " ".join(str(lesson.get(k, "")) for k in ("evidence", "mistake", "condition", "rule"))
    names = re.findall(r'File "[^"]+", line \d+, in (\w+)', text)   # traceback frames
    names += re.findall(r"\b([a-z_][a-z0-9_]{2,})\(", text)          # foo(... mentions
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _find_function(name):
    """(relpath, source) for the first `def name` across the repo's *.py, else None."""
    pat = re.compile(r"^(\s*)def %s\(" % re.escape(name))
    for path in sorted(HERE.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            m = pat.match(line)
            if m is None:
                continue
            indent = len(m.group(1))
            j = i + 1
            while j < len(lines):
                ln = lines[j]
                if ln.strip() and not ln.startswith(" " * (indent + 1)) \
                        and not ln.lstrip().startswith(("#", ")", "]", "}")):
                    break
                j += 1
            return path.name, "\n".join(lines[i:j])
    return None


def gather_context(lesson):
    """[(relpath, function source), ...] for the functions the lesson's evidence names."""
    out, total = [], 0
    for name in _candidate_names(lesson):
        hit = _find_function(name)
        if hit is None or any(h[0] == hit[0] and hit[1] in h[1] for h in out):
            continue
        if total + len(hit[1]) > MAX_CONTEXT_CHARS:
            break
        out.append(hit)
        total += len(hit[1])
    return out


# --------------------------------------------------------------- drafting
def _extract_diff(text):
    """Strip fences/prose; return the unified diff, or None if there isn't one."""
    text = re.sub(r"^```[a-z]*\s*$", "", text, flags=re.MULTILINE)
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("--- "):
            return "\n".join(lines[i:]).strip() + "\n"
    return None


def draft_fix(lesson):
    """Ask Coder-30B for a minimal unified diff codifying the lesson. None if no usable draft."""
    ctx = gather_context(lesson)
    if not ctx:
        return None
    parts = ["LESSON (fired %dx):" % lesson.get("count", 0),
             json.dumps({k: lesson.get(k) for k in
                         ("condition", "mistake", "rule", "phase", "evidence")}, indent=2),
             "", "CURRENT SOURCE:"]
    for relpath, src in ctx:
        parts += ["", "# file: %s" % relpath, src]
    parts += ["", "Produce the minimal unified diff (paths a/<file> -> b/<file>) that "
              "codifies the lesson's rule in the source above."]
    out = llm.chat(
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": "\n".join(parts)}],
        model=llm.CODER, max_tokens=4000, think=False)
    return _extract_diff(out)


# --------------------------------------------------------------- unified-diff applier
def parse_diff(text):
    """Parse a unified diff -> [{'path', 'hunks': [{'old_start', 'lines'}]}]. Raises ValueError."""
    files, cur, i = [], None, 0
    lines = text.splitlines()
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("--- "):
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise ValueError("malformed diff: '---' without '+++'")
            new = lines[i + 1][4:].split("\t")[0].strip()
            old = ln[4:].split("\t")[0].strip()
            path = old if new == "/dev/null" else new
            if path.startswith(("a/", "b/")):
                path = path[2:]
            cur = {"path": path, "hunks": []}
            files.append(cur)
            i += 2
            continue
        if ln.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", ln)
            if m is None or cur is None:
                raise ValueError("malformed hunk header: %r" % ln)
            old_n = int(m.group(2) or "1")
            new_n = int(m.group(4) or "1")
            body, got_old, got_new = [], 0, 0
            i += 1
            while i < len(lines) and (got_old < old_n or got_new < new_n):
                bl = lines[i]
                if bl.startswith("\\"):        # "\ No newline at end of file"
                    i += 1
                    continue
                if not bl:
                    bl = " "                   # trailing-whitespace-stripped context line
                if bl[0] not in " +-":
                    raise ValueError("unexpected line in hunk: %r" % bl)
                body.append(bl)
                if bl[0] in " -":
                    got_old += 1
                if bl[0] in " +":
                    got_new += 1
                i += 1
            if got_old != old_n or got_new != new_n:
                raise ValueError("hunk body shorter than its header claims")
            cur["hunks"].append({"old_start": int(m.group(1)), "lines": body})
            continue
        i += 1
    if not files or not any(f["hunks"] for f in files):
        raise ValueError("no hunks found in diff")
    return files


def apply_hunks(original, hunks, fuzz=50):
    """Apply parsed hunks to a file's text. Exact context match required, position may drift
    up to `fuzz` lines from the header's claim. Raises ValueError on any mismatch."""
    src = original.splitlines()
    out, pos = [], 0   # pos = next unconsumed src index
    for h in hunks:
        old = [l[1:] for l in h["lines"] if l[0] in " -"]
        want = max(h["old_start"] - 1, 0)

        def matches(at):
            return at >= pos and src[at:at + len(old)] == old
        cand = None
        for d in range(fuzz + 1):
            if matches(want + d):
                cand = want + d
                break
            if d and matches(want - d):
                cand = want - d
                break
        if cand is None and not old:           # pure-insert hunk into an empty region
            cand = max(pos, min(want, len(src)))
        if cand is None:
            raise ValueError("hunk context not found near line %d" % h["old_start"])
        out += src[pos:cand]
        for l in h["lines"]:
            if l[0] in " +":
                out.append(l[1:])
        pos = cand + len(old)
    out += src[pos:]
    text = "\n".join(out)
    if original.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text


def validate_patch(diff_text, root=HERE):
    """Apply the diff to a temp copy of the touched files + py_compile the results.

    Returns (ok, reason). Never touches the real tree. Rejects diffs that touch files
    outside root, non-.py files, or files that don't exist (no file creation -- a minimal
    lesson fix edits existing code)."""
    if not diff_text:
        return False, "empty diff"
    try:
        files = parse_diff(diff_text)
    except ValueError as e:
        return False, "parse: %s" % e
    root = pathlib.Path(root)
    with tempfile.TemporaryDirectory(prefix="codegen-") as td:
        for f in files:
            rel = pathlib.PurePosixPath(f["path"])
            if rel.is_absolute() or ".." in rel.parts:
                return False, "path escapes repo: %s" % rel
            if rel.suffix != ".py":
                return False, "non-python file: %s" % rel
            real = root / rel
            if not real.is_file():
                return False, "no such file: %s" % rel
            tmp = pathlib.Path(td) / rel
            tmp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(real, tmp)
            try:
                tmp.write_text(apply_hunks(tmp.read_text(), f["hunks"]))
            except ValueError as e:
                return False, "apply %s: %s" % (rel, e)
            try:
                py_compile.compile(str(tmp), doraise=True)
            except py_compile.PyCompileError as e:
                return False, "compile %s: %s" % (rel, str(e).splitlines()[0])
    return True, ""


# --------------------------------------------------------------- proposals
def _slug(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")[:40] or "lesson"


def propose(lesson_id=None):
    """Draft + validate a patch per promotable lesson; write valid ones to proposals/.

    A written proposal marks its lesson promoted (it has a pending codified fix; review may
    still reject it -- rejecting means reverting the flag by hand or refining the lesson).
    Returns [(lesson_id, patch_path | None, note), ...]."""
    rows = lessons.promotable()
    if lesson_id is not None:
        rows = [r for r in rows if r["id"] == int(lesson_id)]
        if not rows:
            raise KeyError("lesson %s is not promotable (unknown, promoted, or count<3)" % lesson_id)
    results = []
    for r in rows:
        diff = draft_fix(r)
        if diff is None:
            results.append((r["id"], None, "no diff drafted (no context match or no diff in reply)"))
            continue
        ok, why = validate_patch(diff)
        if not ok:
            results.append((r["id"], None, "rejected: " + why))
            continue
        PROPOSALS.mkdir(exist_ok=True)
        stem = "%d-%s" % (r["id"], _slug(r["condition"]))
        patch = PROPOSALS / (stem + ".patch")
        patch.write_text(diff)
        (PROPOSALS / (stem + ".md")).write_text(
            "# Lesson %d -> proposed patch\n\n"
            "- **condition**: %s\n- **mistake**: %s\n- **rule**: %s\n"
            "- **fired**: %dx (phase %s)\n\n"
            "Drafted by %s, validated offline (applies clean + compiles). Review per\n"
            "WORKFLOW.md: apply with `git apply proposals/%s`, test, branch, PR. Never\n"
            "auto-merged.\n\n```diff\n%s```\n"
            % (r["id"], r["condition"], r["mistake"], r["rule"], r["count"], r["phase"],
               llm.CODER, patch.name, diff))
        lessons.mark_promoted(r["id"])
        results.append((r["id"], str(patch), "ok"))
    return results


if __name__ == "__main__":
    got = propose(sys.argv[1] if len(sys.argv) > 1 else None)
    if not got:
        print("no promotable lessons")
    for lid, path, note in got:
        print("lesson %d: %s%s" % (lid, note, " -> " + path if path else ""))
    sys.exit(0 if all(p for _, p, _ in got) or not got else 1)
