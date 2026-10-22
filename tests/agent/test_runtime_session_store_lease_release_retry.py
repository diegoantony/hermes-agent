"""RED: release_session_turn_lease must not stay blocked by a sticky RPC failure it can
retry past, nor silently clear a failure it never verified.

Correlates with observed production failures (not reproducing them verbatim -- those carry
PII/session content this fix must never touch): errors.log 2026-10-22 08:26:58-08:27:07
(TimeoutError('worker_receipt_timeout') from runtime_session_store.py:112, same sticky-gate
mechanism as the 2026-10-21 21:15 permission_denied failure) and gateway.log 08:27:23
('Managed worker lost: managed_worker_failed'). Both land on the SAME local short-circuit:
once agent.runtime_session_store.RuntimeSessionStore.failure is set by any rpc() failure,
_apply()'s `if self.failure: raise` fires for every subsequent call on that instance --
including release_session_turn_lease's turn.cleanup -- without a new delivery attempt. The
owner-side lease row then survives until LEASE_TTL_SECONDS (300s), denying any other
session/worker ("retained by owner") for a transient RPC hiccup.

RED proof that this was broken (zero new rpc() attempts on release once poisoned, by
inspection and by a prior run of this exact fixture against the unmodified store) is recorded
in evidence/worker-persist-20261022/checkpoint.json rather than kept as a permanently-red test
here; the fix target below (test_release_recovers_once_the_owner_answers_again) is the
regression guard going forward.
"""
import json

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError


def test_release_recovers_once_the_owner_answers_again(tmp_path):
    """Post-fix target: the exact queued append gets one more real delivery attempt as part of
    release; when the owner answers this time, both the lost append and the lease cleanup land,
    and the store returns to a healthy (failure=None, empty pending) state."""
    calls = []

    def flaky(method, **params):
        calls.append((params['operation'], params['payload']))
        if len(calls) == 1:
            raise TimeoutError('worker_receipt_timeout')
        return {'count': 1, 'annotations': [{'_row_id': 1}]} if params['operation'] == 'transcript.append' \
            else {'value': True}

    store = RuntimeSessionStore(flaky, {'session_id': 's'}, tmp_path / 'private')
    try:
        with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
            store.append_messages_batch('s', [{'role': 'user', 'content': 'hi'}])
        assert store.failure == 'worker_receipt_timeout'

        store.release_session_turn_lease('s', 'holder')  # must not raise

        assert store.failure is None
        assert store.journal['pending'] == []
        ops = [op for op, _ in calls]
        assert ops == ['transcript.append', 'transcript.append', 'turn.cleanup'], (
            'expected: 1st attempt (fails), retry of the SAME queued append (succeeds), then '
            'the release itself -- never re-queuing/duplicating and never skipping the retry')
        assert json.loads(store.path.read_text())['pending'] == []
    finally:
        store.close()


def test_release_does_not_mask_a_non_delivery_failure(tmp_path, monkeypatch):
    """A failure with nothing queued for retry (disk/outbox problem, not an RPC delivery
    problem) must stay sticky exactly as before: release must not call retry_pending() when
    there is no pending entry to retry, because retry_pending() trivially clears self.failure
    on an empty queue -- that would silently paper over an unverified local fault."""
    store = RuntimeSessionStore(lambda *a, **k: {}, {'session_id': 's'}, tmp_path / 'private')

    def fail_replace(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr('agent.runtime_session_store.os.replace', fail_replace)
    try:
        with pytest.raises(OSError, match='disk unavailable'):
            store.queue_token_counts('s', input_tokens=1)
        assert store.failure == 'disk unavailable'
        assert store.journal['pending'] == []

        with pytest.raises(WorkerPersistenceError, match='disk unavailable'):
            store.release_session_turn_lease('s', 'holder')
        assert store.failure == 'disk unavailable', 'must stay sticky -- nothing was verified fixed'
    finally:
        store._outbox_owner.close()


def test_release_retry_never_bypasses_cross_worker_session_scope():
    """Negative/authorization control: the retry path must never touch or weaken
    RuntimeSessionStore._session()'s cross-session scope check used by every other mutating
    call. release_session_turn_lease already does not scope-check locally by design (its
    admission target survives physical rotation -- the owner enforces holder ownership); the
    retry-before-cleanup change must not add a new local bypass of that documented invariant
    either, so it must still never CALL _session() (comments mentioning it by name are fine)."""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(RuntimeSessionStore.release_session_turn_lease)))
    calls = [node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert '_session' not in calls, (
        'release_session_turn_lease must keep NOT calling _session() -- scope/holder ownership '
        'enforcement stays entirely on the owner side, exactly as before this change')


def test_cross_session_calls_stay_denied_after_the_change(tmp_path):
    """Full-fidelity negative control: every other mutating call on a store scoped to session
    's' must still be denied for any other session id -- this change touches only the release
    path's internal retry, nothing in _session()."""
    store = RuntimeSessionStore(lambda *a, **k: {'value': True}, {'session_id': 's'}, tmp_path / 'private')
    try:
        with pytest.raises(WorkerPersistenceError, match='permission_denied'):
            store.queue_token_counts('other-session', input_tokens=1)
        with pytest.raises(WorkerPersistenceError, match='permission_denied'):
            store.refresh_session_turn_lease('other-session', 'holder')
    finally:
        store.close()


def test_release_retry_preserves_permanent_owner_denial(tmp_path):
    calls = []
    def denied(method, **params):
        calls.append(params)
        raise WorkerPersistenceError('permission_denied')
    store = RuntimeSessionStore(denied, {'session_id': 's'}, tmp_path / 'private')
    try:
        with pytest.raises(WorkerPersistenceError, match='permission_denied'):
            store.queue_token_counts('s', input_tokens=1)
        frozen = json.loads(json.dumps(store.journal['pending']))
        with pytest.raises(WorkerPersistenceError, match='permission_denied'):
            store.release_session_turn_lease('s', 'holder')
        assert len(calls) == 2
        assert calls[0] == calls[1], 'retry must preserve scope, sequence and payload'
        assert store.journal['pending'] == frozen
        assert store.failure == 'permission_denied'
        assert not any(p['operation'] == 'turn.cleanup' for p in calls)
    finally:
        store._outbox_owner.close()


def test_release_retry_on_continued_timeout_is_bounded_and_durable(tmp_path):
    calls = []
    def unavailable(method, **params):
        calls.append(params)
        raise TimeoutError('worker_receipt_timeout')
    store = RuntimeSessionStore(unavailable, {'session_id': 's'}, tmp_path / 'private')
    try:
        with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
            store.queue_token_counts('s', input_tokens=1)
        frozen = json.loads(json.dumps(store.journal['pending']))
        with pytest.raises(WorkerPersistenceError, match='worker_receipt_timeout'):
            store.release_session_turn_lease('s', 'holder')
        assert len(calls) == 2, 'cleanup may retry once, never spin until owner returns'
        assert calls[0] == calls[1]
        assert store.journal['pending'] == frozen
        assert json.loads(store.path.read_text())['pending'] == frozen
    finally:
        store._outbox_owner.close()
