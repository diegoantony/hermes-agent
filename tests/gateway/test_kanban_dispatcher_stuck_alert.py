"""``dispatcher_stuck`` (hermes_cli.kanban_db_dispatch.scan_and_alert_stuck_ready_tasks)
must be wired into the notifier's delivery filters, or emitting the event is a
no-op nobody ever sees (field incident t_b16460c8: a stranded-ready card had
no delivery path at all, only a pull-based diagnostic and a log line)."""

from __future__ import annotations

from gateway.kanban_watchers_notifier import TERMINAL_KINDS, _WAKE_KINDS, diagnostic_event


class _Ev:
    kind = "dispatcher_stuck"
    payload = None


def test_dispatcher_stuck_is_claimable_by_a_subscription():
    assert "dispatcher_stuck" in TERMINAL_KINDS


def test_dispatcher_stuck_wakes_the_origin():
    assert "dispatcher_stuck" in _WAKE_KINDS


def test_dispatcher_stuck_counts_as_a_diagnostic_event():
    assert diagnostic_event(_Ev()) is True
