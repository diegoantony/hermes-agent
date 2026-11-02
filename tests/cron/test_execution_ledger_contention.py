"""Execution ledger under write-lock contention (slow-fsync hosts).

A WAL commit under ``synchronous=FULL`` holds the write lock across an fsync. On a loaded host that
fsync was measured at 2.8-7.9 s (and 27 s for the checkpoint-on-close), while the ledger waited only
5 s: create/adopt/finish failed with ``database is locked`` and the failed terminal write left the
attempt ``claimed`` forever (the dead-owner sweep skips rows of a live process).
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest


def _point_ledger(monkeypatch, tmp_path):
    from cron import executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    monkeypatch.setattr(executions, "_deferred_terminal_writes", {})
    return executions


class _StalledWriter:
    """Another connection that takes the write lock and sits on it (an fsync stall stand-in)."""

    def __init__(self, path, hold_seconds=None):
        self._path = path
        self._hold = hold_seconds
        self.locked = threading.Event()
        self.release = threading.Event()
        self.error = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            conn = sqlite3.connect(self._path, timeout=1, isolation_level=None)
            try:
                conn.execute("BEGIN IMMEDIATE")
                self.locked.set()
                self.release.wait(self._hold)
                conn.execute("ROLLBACK")
            finally:
                conn.close()
        except BaseException as exc:  # surfaced by join()
            self.error = exc
            self.locked.set()

    def __enter__(self):
        self._thread.start()
        assert self.locked.wait(10)
        assert self.error is None, self.error
        return self

    def __exit__(self, *exc):
        self.release.set()
        self._thread.join(15)
        assert self.error is None, self.error


def test_ledger_busy_tolerance_matches_jobs_lock(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    from cron import incidents, jobs

    expected = int(jobs._JOBS_LOCK_TIMEOUT_SECONDS * 1000)
    assert expected >= 30_000
    for connect in (executions._connect, incidents._connect):
        conn = connect()
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= expected
        finally:
            conn.close()


def test_writer_stalled_past_five_seconds_does_not_fail_dispatch(monkeypatch, tmp_path):
    """Before: the 5 s busy timeout raised ``database is locked`` here."""
    executions = _point_ledger(monkeypatch, tmp_path)
    first = executions.create_execution("warm", source="builtin")  # schema exists
    executions.finish_execution(first["id"], success=True)

    started = time.monotonic()
    with _StalledWriter(executions.EXECUTIONS_FILE, hold_seconds=6.0):
        record = executions.create_execution("stalled", source="builtin")
    assert time.monotonic() - started >= 5.5
    assert record["status"] == "claimed"
    assert executions.finish_execution(record["id"], success=False, error="x")["status"] == "failed"


def test_lost_terminal_write_does_not_leave_attempt_claimed(monkeypatch, tmp_path):
    """A dispatch failure whose ``finish_execution`` also hits a locked ledger.

    Before: the row stayed ``claimed`` for the life of the process — the dead-owner sweep skips
    rows owned by this (live) process and nothing retried the write.
    """
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("dispatch-fails", source="builtin")
    monkeypatch.setattr(executions, "BUSY_TIMEOUT_SECONDS", 0.2, raising=False)

    with _StalledWriter(executions.EXECUTIONS_FILE):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            executions.finish_execution(
                record["id"], success=False, error="Restart-safe cron worker dispatch failed")
    assert executions.get_execution(record["id"])["status"] == "claimed"

    # The periodic sweep (gateway reaper) lands the lost write once the ledger is writable.
    executions.recover_interrupted_executions()

    settled = executions.get_execution(record["id"])
    assert settled["status"] == "failed"
    assert settled["error"] == "Restart-safe cron worker dispatch failed"
    assert executions._deferred_terminal_writes == {}


def test_lost_terminal_write_settles_on_next_successful_create(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("job", source="builtin")
    monkeypatch.setattr(executions, "BUSY_TIMEOUT_SECONDS", 0.2)
    with _StalledWriter(executions.EXECUTIONS_FILE):
        with pytest.raises(sqlite3.OperationalError):
            executions.finish_execution(record["id"], success=True)

    executions.create_execution("next-fire", source="builtin")

    assert executions.get_execution(record["id"])["status"] == "completed"


def test_deferred_write_never_overrides_a_settled_attempt(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("job", source="builtin")
    monkeypatch.setattr(executions, "BUSY_TIMEOUT_SECONDS", 0.2)
    with _StalledWriter(executions.EXECUTIONS_FILE):
        with pytest.raises(sqlite3.OperationalError):
            executions.finish_execution(record["id"], success=False, error="lost")
    # Someone else (e.g. a receipt recovery path) settles the row before the deferred retry runs.
    conn = sqlite3.connect(executions.EXECUTIONS_FILE)
    try:
        conn.execute(
            "UPDATE executions SET status='unknown', finished_at=?, error=? WHERE id=?",
            ("2026-01-01T00:00:00+00:00", "settled elsewhere", record["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    assert executions.settle_deferred_terminal_writes() == 0
    row = executions.get_execution(record["id"])
    assert (row["status"], row["error"]) == ("unknown", "settled elsewhere")
    assert executions._deferred_terminal_writes == {}


@pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "setconfig")
    or not hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"),
    reason="needs Python 3.12+ sqlite3.Connection.setconfig",
)
def test_closing_the_last_ledger_connection_keeps_the_wal(monkeypatch, tmp_path):
    """Before: every per-transaction close checkpointed, fsynced and deleted ``-wal``."""
    executions = _point_ledger(monkeypatch, tmp_path)
    wal = executions.EXECUTIONS_FILE.with_name("executions.db-wal")

    record = executions.create_execution("wal", source="builtin")
    if not executions.get_execution(record["id"]):
        pytest.fail("row not persisted")
    conn = sqlite3.connect(executions.EXECUTIONS_FILE)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    if mode != "wal":
        pytest.skip(f"ledger journal mode is {mode}")
    executions.finish_execution(record["id"], success=True)

    assert wal.exists()
    # And the persisted WAL is still read correctly by a fresh connection.
    assert executions.get_execution(record["id"])["status"] == "completed"
