#!/usr/bin/env python3
"""Local-LLM client for the self-learning loop (halo's Lemonade server, OpenAI-compatible).

All runtime inference is LOCAL (MEGABASE-V2-DESIGN §6): the 4B triages every maintain lap,
the 35B is the architect, Coder-30B drafts lesson->code patches. No cloud in the loop.

Endpoint resolution: $LEMONADE_URL, else charon-reachable LAN default. Key: $LEMONADE_KEY,
else lemonade.key next to this file (0600, gitignored — same pattern as rcon.pass).
"""
import json
import os
import pathlib
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
URL = os.environ.get("LEMONADE_URL", "http://192.168.2.42:13305/v1")

TRIAGE = "Qwen3-4B-Instruct-2507-GGUF"
ARCHITECT = "Qwen3.6-35B-A3B-MTP-GGUF"
CODER = "Qwen3-Coder-30B-A3B-Instruct-GGUF"


def _key():
    k = os.environ.get("LEMONADE_KEY")
    if k:
        return k.strip()
    f = HERE / "lemonade.key"
    if f.exists():
        return f.read_text().strip()
    raise RuntimeError("no LEMONADE_KEY env and no lemonade.key file")


MAX_PROMPT_CHARS = 40000  # ~12k tokens. halo's KV pool is SHARED (131k / 4 slots); an oversized
# prompt crashed the 35B worker for every consumer on 2026-08-29 (GGML_ASSERT logits != nullptr
# after "failed to find free space in the KV cache"). Compact payloads (architect.compact) —
# never raise this without checking halo's slot budget.


def chat(messages, model=ARCHITECT, max_tokens=2048, temperature=0.2, timeout=300, think=False):
    """One chat completion; returns the assistant text. Raises on transport errors.

    think=False disables Qwen's thinking phase (chat_template_kwargs.enable_thinking) — with it
    on, reasoning eats the max_tokens budget and content can come back EMPTY (a 'reply {ok}'
    probe spent 239 completion tokens thinking). Enable per-call only with a generous budget."""
    total = sum(len(m.get("content", "")) for m in messages)
    if total > MAX_PROMPT_CHARS:
        raise ValueError("prompt too large for halo's shared KV pool: %d chars > %d"
                         % (total, MAX_PROMPT_CHARS))
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": bool(think)},
    }).encode()
    req = urllib.request.Request(
        URL + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + _key()},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("lemonade HTTP %s: %s" % (e.code, e.read()[:500])) from e
    if "choices" not in data:
        raise RuntimeError("lemonade error response: %s" % json.dumps(data)[:500])
    return data["choices"][0]["message"]["content"]


def extract_json(text):
    """Tolerant JSON extraction: first '{' or '[' to its matching end. None if unparseable."""
    for opener, closer in (("{", "}"), ("[", "]")):
        i = text.find(opener)
        if i < 0:
            continue
        j = text.rfind(closer)
        while j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                j = text.rfind(closer, i, j)
    return None


def chat_json(messages, model=ARCHITECT, retries=1, **kw):
    """chat() that must yield JSON; one corrective retry, then None (caller logs a lesson)."""
    for attempt in range(retries + 1):
        out = chat(messages, model=model, **kw)
        parsed = extract_json(out)
        if parsed is not None:
            return parsed
        messages = messages + [
            {"role": "assistant", "content": out[:2000]},
            {"role": "user", "content": "That was not valid JSON. Reply with ONLY the JSON object, no prose."},
        ]
    return None


if __name__ == "__main__":
    print(chat([{"role": "user", "content": "Reply with exactly: ok"}],
               model=TRIAGE, max_tokens=10))
