"""Keep every test out of the live learning stores.

`infra.verify` writes to BOTH memory and the skill library, and a test that exercised it
without redirecting both put real rows into the repo's skills.jsonl - "2 win / 1 loss" for a
skill no bot had ever used. A learning store polluted by test fixtures is worse than a
polluted log: the bot consults it, so invented evidence changes real decisions.

Redirecting at the session level rather than per-test means a NEW test cannot forget.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_learning_stores(tmp_path, monkeypatch):
    import memory
    import skills
    monkeypatch.setattr(memory, "PATH", tmp_path / "memory.jsonl")
    monkeypatch.setattr(skills, "PATH", tmp_path / "skills.jsonl")
    try:
        import corrections
        monkeypatch.setattr(corrections, "PATH", tmp_path / "corrections.json")
    except ImportError:
        pass
    yield
