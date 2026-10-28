"""State reads behind a ws request never run on the owner loop. state.db is the owner's largest
file; under host IO pressure one indexed read of a cold page waits on the disk for seconds, and the
gateway's liveness watchdog restarts a loop that stops ticking. Each probe here slows the exact read
a handler makes (the disk wait) and measures the loop's heartbeat through ``_dispatch_request``."""
import asyncio
from contextlib import closing
from itertools import pairwise
import time
from types import SimpleNamespace

import pytest

from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch

SLOW_S = 1.0


def _slowed(real):
    def read(*args, **kwargs):
        time.sleep(SLOW_S)  # health: allow HX013 -- the simulated disk wait IS the behaviour under test
        return real(*args, **kwargs)
    return read


async def _max_loop_gap(work):
    """The longest gap between loop heartbeats while *work* runs, and its result."""
    ticks = []

    async def ticker():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(.02)  # health: allow HX013 -- the loop heartbeat probe; its gaps are the measurement
    beat = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        async with asyncio.timeout(30):
            result = await work
    finally:
        beat.cancel()
    ticks.append(time.monotonic())  # a loop blocked from the first tick still yields one gap
    return max(b - a for a, b in pairwise(ticks)), result


@pytest.mark.asyncio
@pytest.mark.parametrize('include_result', [False, True], ids=['receipt', 'receipt-with-result'])
async def test_prompt_receipt_reads_the_admission_off_the_loop(tmp_path, monkeypatch, include_result):
    from gateway import session_authority, session_results
    from tui_gateway.ws import _dispatch_request

    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        epoch = begin_runtime_epoch(db, instance_id='current')
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id='owned',
                                     instance_id='current', db=db, epoch=epoch)
        authority.sessions['s'] = LiveSession(None, 'route')
        connection = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None),
                                         {'user_id': 'human', 'profile_id': 'owned', 'instance_id': 'current',
                                          'capabilities': ['session:read', 'session:submit']})
        # The admission's principal must match the actor's authenticated subject (the receipt
        # handler's ownership check), not the raw identity's user_id.
        row = admit_session_input(db, epoch=epoch, principal_id=connection.actor.subject, session_id='s',
                                  request_id='input', payload={'text': 'hello'})
        monkeypatch.setattr(session_authority, 'get_session_admission',
                            _slowed(session_authority.get_session_admission))
        monkeypatch.setattr(session_results, 'admission_result', _slowed(session_results.admission_result))
        params = {'session_id': 's', 'admission_id': row['admission_id']}
        if include_result:
            params['include_result'] = True
        request = {'id': 1, 'method': 'prompt.receipt', 'params': params}
        try:
            gap, response = await _max_loop_gap(
                _dispatch_request(connection, request, 'prompt.receipt', connection.transport))
        finally:
            await connection.close()
    assert response['result']['admission_id'] == row['admission_id'], response
    assert response['result']['status'] == 'queued'
    assert gap < SLOW_S / 2, f'the owner loop stalled {gap:.2f}s behind one state.db read'


def test_kanban_run_reads_the_claim_off_the_loop(tmp_path, monkeypatch):
    """The dispatcher's ``kanban.run``: the claim check reads the card on the shared Kanban DB and
    the owner's saved session; neither read may stall the loop that serves every other session."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from gateway.session_contract import Principal
    from gateway.session_kanban import run_task
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env',
                        lambda name: str(tmp_path) if name == 'default' else str(tmp_path / name))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    kb.create_board('owned')
    with closing(connect(board='owned')) as conn:
        task_id = kb.create_task(conn, title='Owned', assignee='default', workspace_kind='dir',
                                 workspace_path=str(workspace))
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, task_id)
    config = {'model': {'default': 'loop-model', 'provider': 'custom'}, 'platform_toolsets': {'cli': ['terminal']}}
    monkeypatch.setattr('gateway.run._load_gateway_config', lambda: config)
    monkeypatch.setattr('gateway.session_local.create_local_session', lambda *a, **k: SimpleNamespace(
        session_id='owner-session', profile_id=str(tmp_path)))
    monkeypatch.setattr(kb, 'get_task', _slowed(kb.get_task))
    monkeypatch.setattr('gateway.session_kanban.asdict', lambda receipt: {})
    calls = []

    async def submit(actor, submission, **kwargs):
        calls.append('submit')
        return SimpleNamespace(admission_id='a')

    async def resume(ref, params):
        calls.append('resume')
    db = SimpleNamespace(get_session=_slowed(lambda sid: None), db_path=str(tmp_path / 'state.db'))
    authority = SimpleNamespace(profile_id=str(tmp_path), db=db, submit=submit)
    actor = Principal('owner', str(tmp_path), frozenset({'session:create'}), 'transport')
    connection = SimpleNamespace(authority=authority, actor=actor, native_owner=True, resume=resume)
    params = dict(board='owned', task_id=task_id, run_id=task.current_run_id, claim_lock=task.claim_lock)
    gap, _ = asyncio.run(_max_loop_gap(run_task(connection, params)))
    assert calls == ['resume', 'submit']
    with closing(connect(board='owned')) as conn:
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='owner_admitted'",
                            (task_id,)).fetchone()[0] == 1
    assert gap < SLOW_S / 2, f'the owner loop stalled {gap:.2f}s behind the Kanban claim reads'
