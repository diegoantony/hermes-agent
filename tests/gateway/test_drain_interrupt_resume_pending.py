"""t_e3ed4466: a turn this gateway interrupted for its OWN restart/shutdown drain must be
queued for auto-resume (``resume_pending``, the same contract crash recovery already uses),
never closed with the generic "Send it again" notice nothing ever resends. Mirrors
tests/gateway/test_incomplete_gateway_turns.py's harness (real ``_handle_message`` path, fake
adapter) so the assertion exercises production persistence code, not a mock of it.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.turn_failure_copy import FAILED_TURN_NOTICE, PARTIAL_FAILED_TURN_NOTICE
import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, ProcessingOutcome
from gateway.session import SessionEntry, SessionSource, build_session_key


class CaptureSlackAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="fake-token"), Platform.SLACK)
        self.sent = []
        self.processing_hooks = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append({"chat_id": chat_id, "content": content, "reply_to": reply_to, "metadata": metadata})
        return SendResult(success=True, message_id="slack-1")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}

    async def on_processing_start(self, event: MessageEvent) -> None:
        self.processing_hooks.append(("start", event.message_id))

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        self.processing_hooks.append(("complete", event.message_id, outcome))


def _make_restart_interrupted_result(*, tool_ran: bool) -> dict:
    """The exact shape ``_hmwa_persist_turn_transcript`` sees when THIS gateway's own restart
    drain interrupted an in-flight turn before it could reply (gateway/run.py::stop(), the
    ``_INTERRUPT_REASON_GATEWAY_RESTART`` cause) — never a user ``/stop`` or a provider error."""
    messages = [{"role": "user", "content": "hello"}]
    if tool_ran:
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": "t0", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]})
        messages.append({"role": "tool", "content": "out", "tool_call_id": "t0"})
    return {
        "final_response": "", "messages": messages, "tools": [], "history_offset": 0,
        "api_calls": 1, "completed": False, "failed": True, "interrupted": True,
        "interrupt_message": "Gateway restarting", "last_prompt_tokens": 0,
    }


def _make_runner(adapter: CaptureSlackAdapter, agent_result: dict) -> gateway_run.GatewayRunner:
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True, token="fake-token")})
    runner.adapters = {Platform.SLACK: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:slack:channel:C123:171717", session_id="sess-1",
        created_at=datetime.now(), updated_at=datetime.now(), platform=Platform.SLACK, chat_type="channel",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner.session_store.has_platform_message_id = MagicMock(return_value=False)
    runner.session_store.transcript_tail_role = MagicMock(return_value="user")
    runner.session_store.mark_resume_pending = MagicMock(return_value=True)
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(return_value=agent_result)
    return runner


def _make_event() -> MessageEvent:
    return MessageEvent(
        text="hello",
        source=SessionSource(platform=Platform.SLACK, chat_id="C123", chat_type="channel",
                             thread_id="171717", user_id="U123"),
        message_id="m-1",
    )


async def _run(monkeypatch, tmp_path, agent_result):
    adapter = CaptureSlackAdapter()
    runner = _make_runner(adapter, agent_result)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *_a, **_k: 100)
    monkeypatch.setenv("SLACK_HOME_CHANNEL", "C123")
    adapter.set_message_handler(runner._handle_message)
    adapter._keep_typing = lambda *_args, **_kwargs: asyncio.Event().wait()
    event = _make_event()
    await adapter._process_message_background(event, build_session_key(event.source))
    return adapter, runner


@pytest.mark.asyncio
async def test_gateway_restart_interrupt_marks_resume_pending_instead_of_send_it_again(monkeypatch, tmp_path):
    """No tool ran yet: safe to replay. The turn must be queued for auto-resume, not closed with
    a notice nobody ever resends (the exact 16-lost-request pattern in t_e3ed4466)."""
    adapter, runner = await _run(monkeypatch, tmp_path, _make_restart_interrupted_result(tool_ran=False))

    transcript_roles = [c.args[1]["role"] for c in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_roles == ["session_meta", "user"]  # no closing "assistant" boundary: tail stays open
    assert runner.session_store.mark_resume_pending.call_args == (
        ("agent:main:slack:channel:C123:171717", "restart_interrupted"), {},
    )
    assert FAILED_TURN_NOTICE not in str(runner.session_store.append_to_transcript.call_args_list)
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_gateway_restart_interrupt_with_a_tool_already_run_keeps_the_notice(monkeypatch, tmp_path):
    """A tool ran before the interrupt: its effects may be real, so auto-replay would risk running
    it twice. This case keeps today's behaviour — closed with the partial notice, never resumed."""
    adapter, runner = await _run(monkeypatch, tmp_path, _make_restart_interrupted_result(tool_ran=True))

    transcript_roles = [c.args[1]["role"] for c in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_roles == ["session_meta", "user", "assistant"]
    boundary = runner.session_store.append_to_transcript.call_args_list[-1].args[1]["content"]
    assert boundary == PARTIAL_FAILED_TURN_NOTICE
    assert runner.session_store.mark_resume_pending.called is False
