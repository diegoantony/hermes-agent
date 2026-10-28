"""A stale ``last_failure_error`` must not park a ready card in ``blocker_auth``.

Field repro: a run ended ``rate_limited`` (stamping "rate-limited (quota wall)"),
the next run proved the quota was back and ended with a dependency block, the
parent closed and ``recompute_ready`` promoted the card — which then sat in
``ready`` for 40h because ``check_respawn_guard`` still matched the old text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

_RATE_LIMIT_TEXT = "pid 1 exited rate-limited (quota wall) — requeued without counting a failure"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    kb.init_db()
    return home


def _rate_limited_run(conn, tid):
    """The dispatcher's rate-limit requeue: run ends ``rate_limited`` and the
    quota text is stamped without counting a failure."""
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    conn.execute(
        "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
        "ended_at=? WHERE id=?", (1_000, run_id),
    )
    conn.execute(
        "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
        "claim_expires=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?",
        (_RATE_LIMIT_TEXT, tid),
    )
    conn.commit()


def test_dependency_promotion_after_rate_limit_is_dispatchable(kanban_home):
    with kbc.connect() as conn:
        child = kb.create_task(conn, title="child", assignee="a")
        _rate_limited_run(conn, child)

        # Next run starts (quota is back), creates a parent and parks on it.
        kb.claim_task(conn, child)
        run_id = kb.get_task(conn, child).current_run_id
        parent = kb.create_task(conn, title="parent", assignee="b")
        kb.link_tasks(conn, parent, child, expected_child_run_id=run_id)
        assert kb.block_task(
            conn, child, reason="waiting on parent", kind="dependency",
            expected_run_id=run_id,
        )
        assert kb.get_task(conn, child).status == "todo"

        # Parent closes -> dependency promotion.
        kb.claim_task(conn, parent)
        assert kb.complete_task(
            conn, parent, summary="done",
            expected_run_id=kb.get_task(conn, parent).current_run_id,
        )
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"

        assert kbd.check_respawn_guard(conn, child) is None
        # The promotion is a deliberate fresh start: the stale diagnosis is gone.
        assert kb.get_task(conn, child).last_failure_error is None


def test_newer_handoff_run_supersedes_stamped_quota_text(kanban_home):
    """Guard-level invariant, independent of which path re-queued the card:
    once a later run ENDED as a deliberate hand-off (here a dependency block),
    the quota/auth text stamped by an older run is no longer the diagnosis."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _rate_limited_run(conn, tid)
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='blocked', status='blocked', ended_at=? "
            "WHERE id=?", (2_000, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL WHERE id=?", (tid,),
        )
        conn.commit()
        assert kb.get_task(conn, tid).last_failure_error == _RATE_LIMIT_TEXT
        assert kbd.check_respawn_guard(conn, tid) is None


def test_auth_failure_still_guards_when_it_is_the_latest_run(kanban_home):
    """The guard must still fire for a genuine, current auth failure."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='spawn_failed', status='failed', ended_at=? "
            "WHERE id=?", (2_000, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?",
            ("provider authentication failed (401)", tid),
        )
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) == "blocker_auth"
