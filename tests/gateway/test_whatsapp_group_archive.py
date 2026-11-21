"""Opt-in group feed is persisted before intake policy, without dispatch or sends."""
import json
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

SOURCE = "120363000000000001@g.us"
OTHER = "120363000000000002@g.us"


def adapter(home, monkeypatch, listed=True):
    monkeypatch.setenv("HERMES_HOME", str(home))
    a = object.__new__(WhatsAppAdapter)
    a.platform = Platform.WHATSAPP
    a._profile_home = home
    a.config = PlatformConfig(enabled=True, extra={
        "archive_group_jids": [SOURCE] if listed else [],
        "group_policy": "allowlist", "group_allow_from": [OTHER],
    })
    a._group_policy = "allowlist"
    a._group_allow_from = [OTHER]
    a._message_handler = AsyncMock()
    a._enqueue_text_event = AsyncMock()
    a._http_session = AsyncMock()
    return a


def message(**overrides):
    return dict(isGroup=True, chatId=SOURCE, senderId="sender@lid", body="hello https://example.org/a",
                messageId="one", timestamp=1790000000, quotedMessageId="quoted", **overrides)


@pytest.mark.asyncio
async def test_archive_before_policy_no_turn_or_send(tmp_path, monkeypatch):
    a = adapter(tmp_path, monkeypatch)
    await a._process_inbound(message())
    files = list(tmp_path.glob("whatsapp/group-archive/*/*.jsonl"))
    assert len(files) == 1
    row = json.loads(files[0].read_text())
    assert row == dict(timestamp=1790000000, chat_jid=SOURCE, sender="sender@lid",
                       text="hello https://example.org/a", id="one", quoted_id="quoted",
                       urls=["https://example.org/a"], media={})
    assert files[0].stat().st_mode & 0o777 == 0o600
    a._enqueue_text_event.assert_not_called()
    a._message_handler.assert_not_called()
    a._http_session.post.assert_not_called()


@pytest.mark.asyncio
async def test_unlisted_and_default_do_not_archive(tmp_path, monkeypatch):
    a = adapter(tmp_path, monkeypatch, listed=False)
    await a._process_inbound(message())
    assert not list(tmp_path.rglob("*.jsonl"))


@pytest.mark.asyncio
async def test_duplicate_survives_adapter_restart_and_day_change(tmp_path, monkeypatch):
    a = adapter(tmp_path, monkeypatch)
    await a._process_inbound(message())
    a = adapter(tmp_path, monkeypatch)
    data = message(); data["timestamp"] += 86400
    await a._process_inbound(data)
    files = list(tmp_path.rglob("*.jsonl"))
    assert sum(len(f.read_text().splitlines()) for f in files) == 1


@pytest.mark.asyncio
async def test_media_metadata_only(tmp_path, monkeypatch):
    a = adapter(tmp_path, monkeypatch)
    await a._process_inbound(message(hasMedia=True, mediaType="image", mime="image/jpeg",
                                     mediaUrls=["/private/payload"], fileName="photo.jpg"))
    row = json.loads(next(tmp_path.rglob("*.jsonl")).read_text())
    assert row["media"] == {"type": "image", "mime": "image/jpeg", "file_name": "photo.jpg"}
    assert "/private/payload" not in json.dumps(row)


@pytest.mark.asyncio
async def test_archive_only_group_outbound_denied(tmp_path, monkeypatch):
    a = adapter(tmp_path, monkeypatch)
    for endpoint in ("send", "send-media", "send-poll", "send-location", "edit", "react"):
        result = await a._post_bridge_message(endpoint, {"chatId": SOURCE}, timeout=1)
        assert result.success is False
        assert "archive-only" in result.error
    a._http_session.post.assert_not_called()
    await a.send_typing(SOURCE)
    from plugins.platforms.whatsapp.adapter import _standalone_send
    result = await _standalone_send(a.config, SOURCE, "must not send")
    assert "archive-only" in result["error"]
    a._http_session.post.assert_not_called()


@pytest.mark.asyncio
async def test_listed_reply_group_archives_before_turn(tmp_path, monkeypatch):
    a = adapter(tmp_path, monkeypatch)
    a.config.extra["group_allow_from"] = [SOURCE]
    async def gated(data):
        assert len(list(tmp_path.rglob("*.jsonl"))) == 1
    a._build_message_event = gated
    await a._process_inbound(message())


@pytest.mark.asyncio
async def test_missing_id_fails_closed(tmp_path, monkeypatch):
    a = adapter(tmp_path, monkeypatch)
    data = message(); data["messageId"] = ""
    with pytest.raises(ValueError, match="messageId"):
        await a._process_inbound(data)
    a._enqueue_text_event.assert_not_called()
