"""Q5 regression (whatsapp-grupo-agentes-20261119 audit, FALHA #5 'Roteamento e isolamento do
prompt'): ``_hmwa_prepare_turn`` must build the ephemeral ``SessionContext`` (Connected Platforms,
home channels, delivery options) from the ROUTED profile's config, not the launch/default
profile's ``self.config``. Before the fix a multiplexed secondary (e.g. a WhatsApp-group profile
like a restricted secondary profile) would leak the default profile's connected platforms and delivery options
into its own ephemeral system prompt.
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


async def _noop_async(*_a, **_k):
    return None


async def _empty_history():
    return []


def _build_runner(monkeypatch, tmp_path, *, multiplexed: bool, captured: dict):
    runner = object.__new__(GatewayRunner)
    # Launch/default config: has telegram connected. This must NEVER leak into the routed
    # secondary's ephemeral prompt.
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True)})
    runner.config.multiplex_profiles = multiplexed

    routed_config = GatewayConfig(platforms={})  # the routed profile's OWN (empty) platform set

    def _fake_build_session_context(source, config, session_entry, event):
        captured['config'] = config
        return SimpleNamespace(source=source)

    monkeypatch.setattr('gateway.run_turn_prepare.build_session_context', _fake_build_session_context)
    monkeypatch.setattr('gateway.config.load_gateway_config', lambda: routed_config)
    captured['routed_config'] = routed_config

    @asynccontextmanager
    async def _scope(_source):
        yield

    # Multiplexed: the routed profile home is not None, so the fix must reload config under scope.
    # Not multiplexed: key is None, so build_session_context must still get self.config (no change
    # in single-profile behavior).
    runner._async_profile_scope_for_source = _scope
    runner._profile_scope_key_for_source = lambda source: (tmp_path if multiplexed else None)

    runner._hmwa_open_session = lambda *_a: _false_false()
    runner._set_session_env = lambda context: []
    runner._pinned_session_context_prompt = lambda *_a, **_k: ''
    runner._hmwa_acquire_turn_lease = _noop_async
    runner._mark_durable_active_turn = _noop_async
    runner._hmwa_first_contact_notes = _noop_async
    runner._voice_channel_sidecar_note = lambda *_a: None
    runner._delivery_adapter_for = lambda source: None
    runner._bind_adapter_run_generation = lambda *_a: None
    runner._prepare_profile_scoped_inbound_message_text = _return_text
    runner.session_store = object()
    runner._async_session_store = SimpleNamespace(_store=runner.session_store, load_transcript=lambda _sid: _empty_history())
    runner._hmwa_run_session_hygiene = lambda *_a: _empty_history()
    return runner


async def _false_false():
    return False, False


async def _return_text(**_kw):
    return 'hi there'


@pytest.mark.asyncio
async def test_multiplexed_group_route_builds_context_from_the_routed_profile(monkeypatch, tmp_path):
    captured: dict = {}
    runner = _build_runner(monkeypatch, tmp_path, multiplexed=True, captured=captured)

    event = MessageEvent(
        text='hi', source=SessionSource(
            platform=Platform.WHATSAPP, chat_id='120363431745698109@g.us', chat_type='group',
            profile='restricted_profile'),
        message_id='m1')
    entry = SimpleNamespace(yolo=None, session_id='sid', session_key='key')

    prepared, _tokens = await runner._hmwa_prepare_turn(event, event.source, entry, 'key', 'key', 1)

    assert not isinstance(prepared, str)  # a real _PreparedTurn, not a refusal/error reply
    assert captured['config'] is captured['routed_config']
    assert captured['config'] is not runner.config
    assert Platform.TELEGRAM not in captured['config'].platforms  # the default profile's platform never leaks


@pytest.mark.asyncio
async def test_standalone_gateway_keeps_using_its_own_config(monkeypatch, tmp_path):
    """Outside multiplexing (``multiplex_profiles`` off, or the route falls back to the launch
    profile), ``_profile_scope_key_for_source`` returns ``None`` and behavior is unchanged:
    ``build_session_context`` still gets ``self.config``."""
    captured: dict = {}
    runner = _build_runner(monkeypatch, tmp_path, multiplexed=False, captured=captured)

    event = MessageEvent(
        text='hi', source=SessionSource(platform=Platform.TELEGRAM, chat_id='dm-1', chat_type='dm'),
        message_id='m1')
    entry = SimpleNamespace(yolo=None, session_id='sid', session_key='key')

    await runner._hmwa_prepare_turn(event, event.source, entry, 'key', 'key', 1)

    assert captured['config'] is runner.config
    assert captured['config'] is not captured['routed_config']
