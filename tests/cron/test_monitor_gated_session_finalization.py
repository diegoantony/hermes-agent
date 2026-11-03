"""Regression test: a monitor-gated cron tick must close its own authority session (t_a4c1624e).

Root cause: the unified runtime (``gateway/session_cron.py``) admits and creates the
authority-owned ``local-*`` session row at FIRE time — before ``run_job`` is ever called. When
the monitor gate in ``_prepare_job_prompt`` suppresses an unchanged tick (or the wake-gate /
empty-prompt gates fire), ``run_job`` returned BEFORE reaching the try/finally that normally
closes that session via ``_finalize_cron_session``. The row stayed open forever (no
``end_reason``), 0 messages, 0 API calls, until the gateway's next restart swept a growing
backlog of them in bulk via ``startup_orphan_reap``. On the production host this accumulated to
1,430 such rows over 3 days, almost all from one ~2-minute monitor job.
"""
from __future__ import annotations

import sys

import pytest

from cron import jobs
from gateway import session_cron
from tests.fakes.cron_owner import admit_fire, cron_owner


@pytest.fixture()
def owner(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    authority = cron_owner(home, monkeypatch)
    # run_job's _open_cron_session_db (and so _finalize_gated_cron_session) acquires the
    # owner's store through hermes_state_registry.acquire() and fails closed unless that
    # call returns the EXACT object the authority already holds ("cron requires the
    # canonical owner store") — a real gateway always builds its authority.db that way.
    # cron_owner's bare SessionDB() bypasses the registry, so re-home the authority on a
    # registry-acquired handle here for the test to see the same object run_job will get.
    import hermes_state_registry as _registry

    authority.db.close()
    authority.db = _registry.acquire(authority.db.db_path)
    yield authority
    _registry.release_or_close(authority.db)


def _write_script(home, name: str, body: str) -> str:
    path = home / "scripts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return name


def _install_agent_stubs(monkeypatch, observed: dict):
    import cron.scheduler as sched
    from cron import scheduler_delivery as sched_delivery

    observed.setdefault("agent_runs", 0)

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs["session_id"]
            self.task_id = self.session_id

        def run_conversation(self, prompt, *_a, **_kw):
            observed["agent_runs"] += 1
            return {"final_response": "agent done", "messages": []}

        def get_activity_summary(self):
            return {"seconds_since_activity": 0.0}

    fake_mod = type(sys)("run_agent")
    fake_mod.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

    from hermes_cli import runtime_provider as _rtp
    monkeypatch.setattr(
        _rtp, "resolve_runtime_provider",
        lambda **_kw: {"provider": "test", "api_key": "k", "base_url": "http://test.local",
                        "api_mode": "chat_completions"})
    monkeypatch.setattr(sched_delivery, "_resolve_origin", lambda job: None)
    monkeypatch.setattr(sched, "_resolve_delivery_target", lambda job: None)
    monkeypatch.setattr(sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: None)
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")

    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_a, **_kw: True)


async def _fire_under_owner(owner_authority, job_id):
    """One unified-runtime fire: admit it (creates the ``local-*`` session), bind its execution
    context exactly as ``gateway/session_cron.py``'s ``execute()`` does, run ``run_job`` under
    that binding, then unbind. Returns ``(session_id, success, final_response)``."""
    from cron.scheduler import run_job

    session_id, execution_id = await admit_fire(owner_authority, job_id)
    job = jobs.get_job(job_id)
    token = session_cron.bind_execution(owner_authority, session_id, job_id, execution_id)
    try:
        success, _doc, final, _error = run_job(job, execution_id=execution_id)
    finally:
        session_cron._execution.reset(token)
    return session_id, success, final


@pytest.mark.asyncio
async def test_monitor_unchanged_tick_closes_its_authority_session(owner, tmp_path, monkeypatch):
    """A monitor job's 'no_change' tick must end its authority session, not leave it dangling
    until the next gateway restart's startup_orphan_reap sweep."""
    from cron.scheduler import SILENT_MARKER

    home = tmp_path / ".hermes"
    _write_script(home, "mon.sh", "echo 'state A'\n")
    job_id = jobs.create_job(
        prompt="Summarize what changed", model="test-model", schedule="every 5m",
        monitor_script="mon.sh", deliver="local")["id"]
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)

    # First tick establishes the monitor baseline and runs the agent for real.
    first_sid, first_ok, _ = await _fire_under_owner(owner, job_id)
    assert first_ok is True
    assert observed["agent_runs"] == 1
    assert owner.db.get_session(first_sid)["ended_at"] is not None

    # Second tick: identical monitor output -> gated, suppressed inside _prepare_job_prompt,
    # BEFORE run_job's try/finally that normally finalizes the session. Must still close ITS
    # OWN session instead of leaving it open for a future restart to sweep.
    second_sid, second_ok, second_final = await _fire_under_owner(owner, job_id)
    assert second_ok is True
    assert second_final == SILENT_MARKER
    assert observed["agent_runs"] == 1  # agent NOT re-invoked — confirms the gate actually fired

    row = owner.db.get_session(second_sid)
    assert row["ended_at"] is not None, (
        "gated tick left its authority session open — this is the t_a4c1624e leak: it would "
        "only close in bulk on the next gateway restart's startup_orphan_reap sweep")
    assert row["api_call_count"] == 0
    assert row["end_reason"] != "startup_orphan_reap"


@pytest.mark.asyncio
async def test_wake_gate_false_tick_closes_its_authority_session(owner, tmp_path, monkeypatch):
    """Same leak, different gate: a ``script`` job whose pre-check prints ``wakeAgent: false``
    hits the same early-return path as the monitor gate and must close its session too."""
    home = tmp_path / ".hermes"
    _write_script(home, "gate.sh", "echo '{\"wakeAgent\": false}'\n")
    job_id = jobs.create_job(
        prompt="React if woken", model="test-model", schedule="every 5m",
        script="gate.sh", deliver="local")["id"]
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)

    session_id, ok, _final = await _fire_under_owner(owner, job_id)
    assert ok is True
    assert observed["agent_runs"] == 0

    row = owner.db.get_session(session_id)
    assert row["ended_at"] is not None, "wake-gate-false tick left its authority session open"
    assert row["end_reason"] != "startup_orphan_reap"
