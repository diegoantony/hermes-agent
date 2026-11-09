"""Lock-tolerance and orphan-sweep hardening for cron/executions.db (t_91db9281).

Root cause, measured live against a busy gateway (bpftrace fsync probe + a per-PID flock/WAL
writer probe; see t_4e226209 and t_91db9281 for the full evidence): cron/executions.db opened
with sqlite_util's default 5000ms busy_timeout, but the host's fsync was observed stalling the
WAL writer for 2.8-7.9s -- comfortably inside the 30s window cron/jobs.py's own flock tolerates,
but past this ledger's old bound. Any ledger write that lost that race raised 'database is
locked' instead of waiting it out, and because every owner-liveness check in this module treats
"my own process_id" as proof a row is still being handled, a same-process write that raised while
trying to CLOSE a row (finish_execution/adopt_claimed_execution) left that row 'claimed' forever:
recover_interrupted_executions() skipped it unconditionally, with no staleness check at all.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest


def _point_ledger(monkeypatch, tmp_path):
    from cron import executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


def _hold_write_lock(path, seconds: float, ready: threading.Event) -> threading.Thread:
    """Open a second connection and hold an exclusive write transaction for ``seconds``."""

    def _hold():
        conn = sqlite3.connect(path, timeout=seconds + 10)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS _lock_probe(x)")
        ready.set()
        time.sleep(seconds)
        conn.commit()
        conn.close()

    thread = threading.Thread(target=_hold, daemon=True)
    thread.start()
    assert ready.wait(timeout=5), "lock-holder thread never acquired its write lock"
    time.sleep(0.05)  # let BEGIN IMMEDIATE actually land before the probe connection races it
    return thread


def _connect_with_busy_timeout(path, busy_timeout_ms: int) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000)
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    return conn


# --- #1: contention tolerance must match the jobs lock (30s) ------------------------------------


def test_executions_busy_timeout_matches_the_jobs_lock_tolerance():
    """cron/jobs.py tolerates 30s of flock contention; the ledger must match it (t_91db9281)."""
    from cron.executions import EXECUTIONS_BUSY_TIMEOUT_MS
    from cron.jobs import _JOBS_LOCK_TIMEOUT_SECONDS

    assert EXECUTIONS_BUSY_TIMEOUT_MS >= _JOBS_LOCK_TIMEOUT_SECONDS * 1000


def test_old_default_busy_timeout_fails_under_the_measured_fsync_stall(monkeypatch, tmp_path):
    """Reproduces today's incident at the sqlite level: a writer stalled 6s (measured range
    2.8-7.9s) outlives the OLD 5000ms default and a write raises instead of waiting it out."""
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.create_execution("warm-up", source="builtin")  # creates the file + schema

    ready = threading.Event()
    holder = _hold_write_lock(executions.EXECUTIONS_FILE, 6.0, ready)
    try:
        conn = _connect_with_busy_timeout(executions.EXECUTIONS_FILE, 5000)
        try:
            with pytest.raises(sqlite3.OperationalError, match="(?i)locked"):
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO _lock_probe VALUES (1)")
        finally:
            conn.close()
    finally:
        holder.join(timeout=10)


def test_executions_busy_timeout_tolerates_the_same_fsync_stall(monkeypatch, tmp_path):
    """Same stall, but with the ledger's real (>=30s) busy_timeout: the write waits it out
    instead of raising -- the fix for t_91db9281 point 1."""
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.create_execution("warm-up", source="builtin")

    ready = threading.Event()
    holder = _hold_write_lock(executions.EXECUTIONS_FILE, 6.0, ready)
    try:
        conn = _connect_with_busy_timeout(
            executions.EXECUTIONS_FILE, executions.EXECUTIONS_BUSY_TIMEOUT_MS
        )
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO _lock_probe VALUES (1)")
            conn.commit()
        finally:
            conn.close()
    finally:
        holder.join(timeout=10)


# --- #1 (continued): retry-with-backoff as defense in depth beyond busy_timeout -----------------


def test_with_lock_retry_retries_a_transient_lock_then_succeeds(monkeypatch):
    from cron import executions

    monkeypatch.setattr(executions, "_LEDGER_LOCK_RETRY_BASE_SECONDS", 0.001)
    calls = {"n": 0}

    @executions._with_lock_retry
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3


def test_with_lock_retry_gives_up_after_its_attempt_budget(monkeypatch):
    from cron import executions

    monkeypatch.setattr(executions, "_LEDGER_LOCK_RETRY_BASE_SECONDS", 0.001)
    calls = {"n": 0}

    @executions._with_lock_retry
    def always_locked():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        always_locked()
    assert calls["n"] == executions._LEDGER_LOCK_RETRY_ATTEMPTS


def test_with_lock_retry_does_not_retry_a_non_lock_operational_error(monkeypatch):
    """Corruption/disk-full errors are not transient contention and must raise at once."""
    from cron import executions

    calls = {"n": 0}

    @executions._with_lock_retry
    def corrupt():
        calls["n"] += 1
        raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError):
        corrupt()
    assert calls["n"] == 1


# --- #2: a same-process orphan must not stay 'claimed' forever ----------------------------------


def test_same_process_orphan_claim_is_eventually_swept(monkeypatch, tmp_path):
    """Regression for t_91db9281: a row this process still 'owns' (process_id == _PROCESS_ID)
    used to be skipped by recover_interrupted_executions() UNCONDITIONALLY -- even once its claim
    far outlived the derived stale bound -- because every in-process write that could have closed
    it (finish_execution/adopt_claimed_execution) is assumed to still be in flight. When that
    write itself raises under lock contention (see the lock-tolerance tests above) the row was
    abandoned forever: no sweep, same process, same pid, alive until the next gateway restart
    (which can be days). The sweep must apply the SAME staleness bound to same-process rows that
    it already applies to any other live owner."""
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("stuck-by-same-process", source="builtin")
    executions.mark_execution_running(record["id"])

    monkeypatch.setattr(executions, "_live_owner_stale_after_seconds", lambda: 1.0)
    monkeypatch.setattr(executions, "_stale_age_seconds", lambda claimed_at, progress_at: 999.0)

    assert executions.recover_interrupted_executions() == 1
    recovered = executions.get_execution(record["id"])
    assert recovered["status"] == "unknown"
    assert recovered["finished_at"]
    # Truthful cause: this process is alive (it is the one running the sweep), so the row is
    # "wedged", never "scheduler restarted" (no restart happened).
    assert "wedged" in recovered["error"].lower()


def test_same_process_fresh_claim_still_survives_recovery(monkeypatch, tmp_path):
    """The staleness bound must still protect a genuinely in-flight same-process row: the fix
    must not turn every tick into a same-process reap."""
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("fresh-same-process", source="builtin")
    executions.mark_execution_running(record["id"])

    assert executions.recover_interrupted_executions() == 0
    assert executions.get_execution(record["id"])["status"] == "running"
