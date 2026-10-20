"""Fallback must not skip a Codex entry whose persisted 429 cooldown is stale (quota reopened early).

Regression: the primary (anthropic) hit a 429; the chain's openai-codex entry carried a persisted
weekly reset days out, ``has_available`` said False, ``_candidate_pool_exhausted`` skipped it, and the
turn ended with "request was not processed" while other sessions used the same Codex account.
"""
from types import SimpleNamespace

import agent.credential_pool as credential_pool
from agent.chat_completion_helpers import _candidate_pool_exhausted


class _Entry(SimpleNamespace):
    pass


class _FakePool:
    """Persisted view says exhausted; ``reclaim`` answers like the live Codex probe would."""

    provider = "openai-codex"

    def __init__(self, live_restored: bool):
        self._live = live_restored
        self._e = _Entry(id="cred-1")
        self.reclaim_calls = 0

    def has_credentials(self):
        return True

    def has_available(self, *, model=None):
        return False

    def entries(self):
        return [self._e]

    def reclaim(self, credential_id, *, model=None):
        self.reclaim_calls += 1
        return self._e if self._live else None

    def next_available_at(self, *, model=None):
        import time
        return time.time() + 4 * 86400  # weekly window persisted by the 429


def _agent():
    return SimpleNamespace(_credential_pool=None)


def test_stale_codex_cooldown_is_not_treated_as_exhausted(monkeypatch):
    pool = _FakePool(live_restored=True)
    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: pool)
    assert _candidate_pool_exhausted(_agent(), "openai-codex", "gpt-5.6-sol") is False
    assert pool.reclaim_calls == 1


def test_really_exhausted_codex_is_still_skipped(monkeypatch):
    pool = _FakePool(live_restored=False)
    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: pool)
    assert _candidate_pool_exhausted(_agent(), "openai-codex", "gpt-5.6-sol") is True


def test_probe_error_falls_back_to_persisted_view(monkeypatch):
    pool = _FakePool(live_restored=True)

    def boom(*a, **k):
        raise RuntimeError("probe down")

    pool.reclaim = boom
    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: pool)
    assert _candidate_pool_exhausted(_agent(), "openai-codex", "gpt-5.6-sol") is True
