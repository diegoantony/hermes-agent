"""A rate-limited Kanban worker must not respawn before the PROVIDER'S OWN reset instant.

t_b88db5f0: 2,148 ``rate_limited`` runs on 2026-10-23/24 (one task respawned 117 times)
because ``check_respawn_guard`` only ever applied the flat
``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS`` (default 300s) cooldown — fine for a short
throttle, but a multi-hour provider quota window still respawns dozens of times inside it.
The worker now carries the 429's own ``resets_at`` / ``resets_in_seconds`` (parsed from the
body/headers by ``agent.error_classifier``, stamped onto the turn result as
``failure_resets_at`` by ``agent.turn_recovery``) through the exit trailer
(``hermes_cli.quiet_single_query.exit_single_query``) into the run's metadata, and
``check_respawn_guard``/``_rate_limit_not_before`` wait for THAT instant instead of the flat
window — falling back to the unchanged flat cooldown when the worker reported no reset.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER, exit_single_query


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    return home


def _dead_worker_with_log(conn, tid: str, pid: int, trailer_suffix: str = "") -> None:
    """Claim ``tid`` for a worker that already exited 75 and wrote its log — never reaped here."""
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    conn.execute(
        "UPDATE tasks SET worker_pid=?, worker_started_at=NULL, started_at=? WHERE id=?",
        (pid, int(time.time()) - 120, tid),
    )
    conn.commit()
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(
            f"model hit the quota wall\n\n"
            f"{KANBAN_WORKER_EXIT_TRAILER}{kb.KANBAN_RATE_LIMIT_EXIT_CODE}{trailer_suffix}\n"
        )


# ── Trailer parsing ──────────────────────────────────────────────────────


def test_worker_log_rate_limit_hint_parses_resets_at(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _dead_worker_with_log(conn, tid, 70001, " resets_at=1700003600.0")
        assert kbd._worker_log_rate_limit_hint(tid) == {"resets_at": 1700003600.0}


def test_worker_log_rate_limit_hint_parses_resets_in(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _dead_worker_with_log(conn, tid, 70002, " resets_in=14400.0")
        assert kbd._worker_log_rate_limit_hint(tid) == {"resets_in_seconds": 14400.0}


def test_worker_log_rate_limit_hint_empty_without_reset_info(kanban_home):
    """An older worker build (or a 429 with no reset data) leaves the plain trailer — no hint,
    exactly the pre-existing wire format, so an old log never falsely parses a hint."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _dead_worker_with_log(conn, tid, 70003)
        assert kbd._worker_log_rate_limit_hint(tid) == {}
        # The bare exit code must still parse unaffected by the trailer extension.
        assert kbd._worker_log_exit_code(tid) == kb.KANBAN_RATE_LIMIT_EXIT_CODE


def test_exit_single_query_writes_resets_at_trailer(monkeypatch, capsys):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    with pytest.raises(SystemExit):
        exit_single_query(kb.KANBAN_RATE_LIMIT_EXIT_CODE, resets_at=1700003600.0)
    err = capsys.readouterr().err
    assert f"{KANBAN_WORKER_EXIT_TRAILER}{kb.KANBAN_RATE_LIMIT_EXIT_CODE} resets_at=1700003600.0" in err


def test_exit_single_query_writes_resets_in_trailer(monkeypatch, capsys):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    with pytest.raises(SystemExit):
        exit_single_query(kb.KANBAN_RATE_LIMIT_EXIT_CODE, resets_in_seconds=120.0)
    err = capsys.readouterr().err
    assert f"{KANBAN_WORKER_EXIT_TRAILER}{kb.KANBAN_RATE_LIMIT_EXIT_CODE} resets_in=120.0" in err


# ── _rate_limit_not_before ───────────────────────────────────────────────


def test_rate_limit_not_before_prefers_resets_at_over_flat_cooldown():
    """A multi-hour reset must win over the 5-minute flat cooldown — the whole point of #b88db5f0."""
    ended = 1_000_000
    meta = {"resets_at": ended + 7200}  # 2h away, way past any flat cooldown
    not_before = kbd._rate_limit_not_before(meta, ended, rl_cooldown=300)
    assert not_before == ended + 7200


def test_rate_limit_not_before_prefers_resets_in_seconds():
    ended = 1_000_000
    meta = {"resets_in_seconds": 9000}
    not_before = kbd._rate_limit_not_before(meta, ended, rl_cooldown=300)
    assert not_before == ended + 9000


def test_rate_limit_not_before_caps_a_bogus_far_future_reset():
    ended = 1_000_000
    meta = {"resets_at": ended + 999_999}  # far beyond the 6h default cap
    not_before = kbd._rate_limit_not_before(meta, ended, rl_cooldown=300)
    assert not_before == ended + kbd.DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS


def test_rate_limit_not_before_falls_back_to_flat_cooldown_without_a_reset():
    """No reset reported at all: behavior is UNCHANGED from before this fix."""
    ended = 1_000_000
    assert kbd._rate_limit_not_before({}, ended, rl_cooldown=300) == ended + 300
    assert kbd._rate_limit_not_before({}, ended, rl_cooldown=0) is None


# ── check_respawn_guard end-to-end ───────────────────────────────────────


def _seed_rate_limited_run(conn, tid: str, ended_at: int, metadata: dict | None = None) -> None:
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    import json as _json
    conn.execute(
        "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
        "ended_at=?, metadata=? WHERE id=?",
        (ended_at, _json.dumps(metadata or {}), run_id),
    )
    conn.execute(
        "UPDATE tasks SET status='ready', current_run_id=NULL, "
        "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
        "last_failure_error=? WHERE id=?",
        ("pid 1 exited rate-limited (quota wall) — requeued", tid),
    )
    conn.commit()


def test_guard_waits_for_reported_reset_past_the_flat_cooldown_window(kanban_home, monkeypatch):
    """The exact bug: a flat 300s cooldown would allow a respawn after 5 minutes even though
    the provider's own ``resets_at`` is 2 hours out — the guard must still block."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl-reset", assignee="a")
        _seed_rate_limited_run(conn, tid, now, metadata={"resets_at": now + 7200})

        # Well past the flat 300s cooldown, but far short of the reported reset.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 600)
        assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

        # Just before the reported reset: still blocked.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 7199)
        assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

        # At/after the reported reset: allowed.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 7200)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_guard_falls_back_to_flat_cooldown_when_worker_reported_no_reset(kanban_home, monkeypatch):
    """No regression: a run with no reset metadata behaves exactly as before this change."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl-no-hint", assignee="a")
        _seed_rate_limited_run(conn, tid, now, metadata={})

        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"
        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_synthetic_429_exactly_one_requeue_no_spawn_before_reset_no_failure_counted(
    kanban_home, monkeypatch,
):
    """Full acceptance scenario: a synthetic 429 reporting a reset 2h out requeues the task
    EXACTLY ONCE, the guard refuses every claim attempt before the reset (simulating many
    dispatcher ticks), ``consecutive_failures`` never increments, and the guard opens at the
    reported instant — not one tick sooner."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000
    reset_at = now + 7200  # 2h — the flat cooldown alone would have allowed ~24 respawns here

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="synthetic-429", assignee="a")
        monkeypatch.setattr(_kb.time, "time", lambda: now)
        _dead_worker_with_log(conn, tid, 70010, f" resets_at={reset_at}")

        kbd.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0  # the breaker must never trip on a quota wall

        run = conn.execute(
            "SELECT outcome, metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert run["outcome"] == "rate_limited"
        assert _kb._json_dict(run["metadata"]).get("resets_at") == float(reset_at)

        # Exactly one rate_limited event recorded for this run.
        n_events = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id=? AND kind='rate_limited'",
            (tid,),
        ).fetchone()["n"]
        assert n_events == 1

        # Simulate many dispatcher ticks across the 2h window: never once allowed to respawn
        # before the reported reset, and the failure counter stays untouched throughout.
        for probe_offset in (60, 300, 600, 1800, 3600, 7199):
            monkeypatch.setattr(_kb.time, "time", lambda o=probe_offset: now + o)
            assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"
            assert kb.get_task(conn, tid).consecutive_failures == 0

        # The reset itself: respawn is finally allowed — the "exactly 1 requeue, wait for the
        # reset" contract.
        monkeypatch.setattr(_kb.time, "time", lambda: reset_at)
        assert kbd.check_respawn_guard(conn, tid) is None
        assert kb.get_task(conn, tid).consecutive_failures == 0
