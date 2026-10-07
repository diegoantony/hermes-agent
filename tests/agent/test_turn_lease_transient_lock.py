"""A refresh that RAISES (state.db write lock busy) must not kill the turn while the TTL
still has margin; a fenced miss (False) or exhausted margin still interrupts."""
import sqlite3
import time

from agent.turn_facade_lease import LEASE_TTL_SECONDS, DurableTurnLease
from tests.agent.test_turn_facade_lease import _Db, _agent


class _LockedDb(_Db):
    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = list(outcomes)

    def refresh_session_turn_lease(self, session_id, holder, **kwargs):
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


def _lease(db):
    agent = _agent(db)
    calls = []
    agent.interrupt = lambda msg, **kw: calls.append(msg)
    lease = DurableTurnLease(agent, db, "s1", "h")
    lease.turn_active = True
    return lease, calls


def test_transient_lock_does_not_interrupt_and_renewal_resumes():
    db = _LockedDb([sqlite3.OperationalError("database is locked")] * 2 + [True])
    lease, calls = _lease(db)
    assert lease.refresh_tick() is None
    assert lease.refresh_tick() is None
    assert lease.refresh_tick() is None
    assert calls == [] and lease.interrupt_message is None


def test_lock_past_ttl_margin_interrupts():
    db = _LockedDb([sqlite3.OperationalError("database is locked")])
    lease, calls = _lease(db)
    lease._last_refresh_ok = time.monotonic() - LEASE_TTL_SECONDS
    assert lease.refresh_tick() is False
    assert len(calls) == 1 and "could not be refreshed" in calls[0]


def test_fenced_miss_still_interrupts_immediately():
    db = _LockedDb([False])
    lease, calls = _lease(db)
    assert lease.refresh_tick() is False
    assert len(calls) == 1 and "lease lost" in calls[0]
