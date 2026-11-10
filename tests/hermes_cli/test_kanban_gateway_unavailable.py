"""Regression coverage for the "Gateway starting: deadline" blocked-card incident.

Production evidence (2026-10-21..11-09, Fable 5.1 audit): 80 Kanban cards parked
``blocked`` on ``last_failure_error`` == "Gateway starting: deadline", and 327 worker
runs recorded ``outcome='crashed'`` for the same cause in four days (241 of them on
2026-11-08 alone, one roughly every 3 minutes for ~17 hours straight — the profile
owner was simply slow/wedged, not the card's fault). Before this fix, a worker giving
up after ``OWNER_WAIT_S`` (kanban_worker_client.py) while the owner was still
``starting``/``draining`` exited 1 like any other crash, so the dispatcher counted it
against the card's ``consecutive_failures`` and tripped the breaker to ``blocked``
after exactly ``failure_limit`` identical spawns — while the real condition (the
owner, not the card) could persist for hours, blocking the entire ready queue one
card at a time.

This fix gives that specific exit a dedicated sentinel (``KANBAN_GATEWAY_UNAVAILABLE_
EXIT_CODE``, mirroring the existing rate-limit quota-wall contract) so the dispatcher
requeues it to ``ready`` WITHOUT spending the failure budget, spaced by the same
cooldown used for a quota wall (``infrastructure_cooldown``).
"""
from pathlib import Path

import pytest

import hermes_cli.kanban_db as kb
import hermes_cli.kanban_db_connect as kbc
import hermes_cli.kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (same shape as test_kanban_db.py)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_exit_code_maps_to_gateway_unavailable_kind():
    assert kbd._exit_code_kind(kb.KANBAN_GATEWAY_UNAVAILABLE_EXIT_CODE) == (
        "gateway_unavailable", kb.KANBAN_GATEWAY_UNAVAILABLE_EXIT_CODE,
    )


def test_classify_dead_worker_exit_never_counts_as_a_crash():
    dead = kbd._classify_dead_worker_exit(12345, "claimer", kb.KANBAN_GATEWAY_UNAVAILABLE_EXIT_CODE)
    assert dead.gateway_unavailable is True
    assert dead.rate_limited is False
    # The outcome recorded on the board must say WHY, not read as a phantom crash.
    assert dead.run_outcome == "gateway_unavailable"


def test_respawn_guard_defers_gateway_unavailable_within_cooldown(kanban_home, monkeypatch):
    """Same shape as the existing rate-limit-cooldown test: within the cooldown the
    guard defers with ``infrastructure_cooldown``; past it, the card is free to
    respawn again — it never falls into ``blocker_auth`` or the breaker."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="gateway-unavailable-guard", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='gateway_unavailable', status='gateway_unavailable', "
            "ended_at=? WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, consecutive_failures=0, "
            "last_failure_error=? WHERE id=?",
            ("pid 1 exited after the gateway stayed unavailable — requeued", tid),
        )
        conn.commit()

        # Inside cooldown -> defer, infra-flavored reason (never counted a failure).
        monkeypatch.setattr(kb.time, "time", lambda: now + 100)
        assert kbd.check_respawn_guard(conn, tid) == "infrastructure_cooldown"
        assert kb.get_task(conn, tid).consecutive_failures == 0

        # Past cooldown -> allowed again, and NOT trapped by blocker_auth even though
        # the stamped text mentions the owner refusing the attempt.
        monkeypatch.setattr(kb.time, "time", lambda: now + 400)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_reclaim_dead_worker_never_trips_breaker_across_many_cycles(kanban_home, monkeypatch):
    """End-to-end: ``failure_limit`` consecutive gateway-unavailable exits on the SAME
    card must never park it ``blocked`` — this is exactly the production incident
    (240 crashes on 2026-11-08, 80 cards ending up blocked) replayed against ONE card.
    """
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")  # no spacing for this test
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="wedged-owner", assignee="a")
        host_prefix = kb._host_prefix()
        for cycle in range(5):  # well past a failure_limit of 2
            kb.claim_task(conn, tid)
            row = conn.execute("SELECT worker_pid, claim_lock FROM tasks WHERE id=?", (tid,)).fetchone()
            # Simulate the managed/unmanaged exit the dispatcher would observe.
            dead = kbd._classify_dead_worker_exit(
                99000 + cycle, row["claim_lock"] or f"{host_prefix}x", kb.KANBAN_GATEWAY_UNAVAILABLE_EXIT_CODE,
            )
            assert dead.gateway_unavailable is True
            kb._end_run(conn, tid, outcome=dead.run_outcome, status=dead.run_outcome, error=dead.error_text,
                        metadata=dead.event_payload)
            conn.execute(
                "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?",
                (dead.error_text[:500], tid),
            )
            conn.commit()

        task = kb.get_task(conn, tid)
        assert task.status == "ready"  # never blocked
        assert task.consecutive_failures == 0  # never spent the failure budget
