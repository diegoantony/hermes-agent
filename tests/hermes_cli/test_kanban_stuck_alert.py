"""The ``stranded_in_ready`` diagnostic had no delivery path: visible only on
pull (``hermes kanban diagnostics`` / the dashboard) and mirrored as a bare
log line by the embedded dispatcher's health check. Field incident
(t_b16460c8): a card sat ``ready`` for 40h while nothing routed the signal
anywhere actionable. ``scan_and_alert_stuck_ready_tasks`` turns it into a
durable ``dispatcher_stuck`` event, which ``kanban_watchers_notifier.
TERMINAL_KINDS`` already knows how to push to a subscribed card/chat.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _backdate_created(conn, task_id, ts):
    conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (ts, task_id))
    conn.execute(
        "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'created'",
        (ts, task_id),
    )
    conn.commit()


def test_stranded_ready_task_gets_a_durable_alert_event(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        now = int(time.time())
        _backdate_created(conn, tid, now - 3600 * 48)  # 48h old, far past the 30m threshold

        alerted = kbd.scan_and_alert_stuck_ready_tasks(conn, now=now)
        assert alerted == [tid]

        events = kb.list_events(conn, tid)
        stuck_events = [e for e in events if e.kind == "dispatcher_stuck"]
        assert len(stuck_events) == 1
        assert stuck_events[0].payload["severity"] in ("error", "critical")


def test_stuck_alert_is_rate_limited_per_task(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        now = int(time.time())
        _backdate_created(conn, tid, now - 3600 * 48)

        first = kbd.scan_and_alert_stuck_ready_tasks(conn, now=now)
        assert first == [tid]

        # Same tick cycle (cooldown not elapsed): no duplicate event.
        second = kbd.scan_and_alert_stuck_ready_tasks(conn, now=now + 60)
        assert second == []
        assert len([e for e in kb.list_events(conn, tid) if e.kind == "dispatcher_stuck"]) == 1

        # Cooldown elapsed: a fresh alert is allowed.
        third = kbd.scan_and_alert_stuck_ready_tasks(
            conn, now=now + kbd._STUCK_ALERT_COOLDOWN_SECONDS + 60,
        )
        assert third == [tid]
        assert len([e for e in kb.list_events(conn, tid) if e.kind == "dispatcher_stuck"]) == 2


def test_healthy_ready_task_is_not_alerted(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        assert kbd.scan_and_alert_stuck_ready_tasks(conn) == []
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "dispatcher_stuck"]
