import json
from unittest.mock import AsyncMock

from gateway.config import Platform, PlatformConfig, load_gateway_config


def _make_adapter(require_mention=None, mention_patterns=None, free_response_chats=None,
                  dm_policy=None, allow_from=None, group_policy=None, group_allow_from=None,
                  allow_reply_to_bot=None):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    extra = {}
    if require_mention is not None:
        extra["require_mention"] = require_mention
    if mention_patterns is not None:
        extra["mention_patterns"] = mention_patterns
    if free_response_chats is not None:
        extra["free_response_chats"] = free_response_chats
    if dm_policy is not None:
        extra["dm_policy"] = dm_policy
    if allow_from is not None:
        extra["allow_from"] = allow_from
    if group_policy is not None:
        extra["group_policy"] = group_policy
    if group_allow_from is not None:
        extra["group_allow_from"] = group_allow_from
    if allow_reply_to_bot is not None:
        extra["allow_reply_to_bot"] = allow_reply_to_bot

    adapter = object.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = PlatformConfig(enabled=True, extra=extra)
    adapter._message_handler = AsyncMock()
    adapter._dm_policy = str(extra.get("dm_policy", "pairing")).strip().lower()
    adapter._allow_from = WhatsAppAdapter._coerce_allow_list(extra.get("allow_from"))
    adapter._group_policy = str(extra.get("group_policy", "pairing")).strip().lower()
    adapter._group_allow_from = WhatsAppAdapter._coerce_allow_list(extra.get("group_allow_from"))
    adapter._mention_patterns = adapter._compile_mention_patterns()
    adapter._free_response_chats = adapter._whatsapp_free_response_chats()
    return adapter


def _group_message(body="hello", **overrides):
    data = {
        "isGroup": True,
        "body": body,
        "chatId": "120363001234567890@g.us",
        "mentionedIds": [],
        "botIds": ["15551230000@s.whatsapp.net", "15551230000@lid"],
        "quotedParticipant": "",
    }
    data.update(overrides)
    return data


def _dm_message(body="hello", **overrides):
    data = {
        "isGroup": False,
        "body": body,
        "senderId": "6281234567890@s.whatsapp.net",
        "from": "6281234567890@s.whatsapp.net",
        "botIds": [],
        "mentionedIds": [],
    }
    data.update(overrides)
    return data


# --- Existing tests (unchanged logic, updated helper) ---


def test_group_messages_can_require_direct_trigger_via_config():
    adapter = _make_adapter(require_mention=True, group_policy="open")

    assert adapter._should_process_message(_group_message("hello everyone")) is False
    assert adapter._should_process_message(
        _group_message(
            "hi there",
            mentionedIds=["15551230000@s.whatsapp.net"],
        )
    ) is True
    assert adapter._should_process_message(
        _group_message(
            "replying",
            quotedParticipant="15551230000@lid",
        )
    ) is True
    assert adapter._should_process_message(_group_message("/status")) is True


def test_regex_mention_patterns_allow_custom_wake_words():
    adapter = _make_adapter(
        require_mention=True,
        mention_patterns=[r"^\s*chompy\b"],
        group_policy="open",
    )

    assert adapter._should_process_message(_group_message("chompy status")) is True
    assert adapter._should_process_message(_group_message("   chompy help")) is True
    assert adapter._should_process_message(_group_message("hey chompy")) is False


def test_invalid_regex_patterns_are_ignored():
    adapter = _make_adapter(
        require_mention=True,
        mention_patterns=[r"(", r"^\s*chompy\b"],
        group_policy="open",
    )

    assert adapter._should_process_message(_group_message("chompy status")) is True
    assert adapter._should_process_message(_group_message("hello everyone")) is False


def test_free_response_chats_bypass_mention_gating():
    adapter = _make_adapter(
        require_mention=True,
        free_response_chats=["120363001234567890@g.us"],
        group_policy="open",
    )

    assert adapter._should_process_message(_group_message("hello everyone")) is True


def test_blank_free_response_chats_falls_through_to_env(monkeypatch):
    """A present-but-blank ``free_response_chats: ''`` in config.yaml means unset: the env CSV applies."""
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    monkeypatch.setenv("WHATSAPP_FREE_RESPONSE_CHATS", "123@g.us")
    adapter = object.__new__(WhatsAppAdapter)
    adapter.config = PlatformConfig(enabled=True, extra={"free_response_chats": ""})
    assert adapter._whatsapp_free_response_chats() == {"123@g.us"}
    # An explicit empty list is a real "no chats" value once no explicit env is set.
    monkeypatch.setenv("WHATSAPP_FREE_RESPONSE_CHATS", "  ")
    adapter.config = PlatformConfig(enabled=True, extra={"free_response_chats": []})
    assert adapter._whatsapp_free_response_chats() == set()


def test_free_response_chats_does_not_bypass_other_groups():
    adapter = _make_adapter(
        require_mention=True,
        free_response_chats=["999999999999@g.us"],
        group_policy="open",
    )

    assert adapter._should_process_message(_group_message("hello everyone")) is False


def test_mention_stripping_removes_bot_phone_from_body():
    adapter = _make_adapter(require_mention=True)

    data = _group_message("@15551230000 what is the weather?")
    cleaned = adapter._clean_bot_mention_text(data["body"], data)
    assert "15551230000" not in cleaned
    assert "weather" in cleaned


# --- New dm_policy tests ---


def test_dm_policy_disabled_still_allows_groups():
    adapter = _make_adapter(
        dm_policy="disabled",
        require_mention=False,
        group_policy="open",
    )

    assert adapter._should_process_message(_group_message("hello")) is True


# --- New group_policy tests ---


# --- Config bridging tests ---

def test_config_bridges_whatsapp_dm_and_group_policy(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "whatsapp:\n"
        "  dm_policy: disabled\n"
        "  group_policy: allowlist\n"
        "  group_allow_from:\n"
        "    - \"120363001234567890@g.us\"\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("WHATSAPP_DM_POLICY", raising=False)
    monkeypatch.delenv("WHATSAPP_GROUP_POLICY", raising=False)
    monkeypatch.delenv("WHATSAPP_GROUP_ALLOWED_USERS", raising=False)

    config = load_gateway_config()

    assert config is not None
    assert config.platforms[Platform.WHATSAPP].extra["dm_policy"] == "disabled"
    assert config.platforms[Platform.WHATSAPP].extra["group_policy"] == "allowlist"
    assert config.platforms[Platform.WHATSAPP].extra["group_allow_from"] == ["120363001234567890@g.us"]
    assert __import__("os").environ["WHATSAPP_DM_POLICY"] == "disabled"
    assert __import__("os").environ["WHATSAPP_GROUP_POLICY"] == "allowlist"
    assert __import__("os").environ["WHATSAPP_GROUP_ALLOWED_USERS"] == "120363001234567890@g.us"


# --- Broadcast / status / newsletter pseudo-chats are always dropped ---


def test_status_broadcast_chats_are_always_dropped():
    """Felipe's gateway.log showed the agent replying to status@broadcast
    (a contact's WhatsApp Story update). These pseudo-chats aren't real
    conversations and the adapter must drop them regardless of dm_policy.
    """

    # Even on the most permissive config — open DMs, no allowlist — Stories
    # and Channel posts must not reach the agent.
    adapter = _make_adapter(dm_policy="open")

    # Classic Story update — what Felipe was seeing in production.
    status_msg = _dm_message(
        body="[video received]",
        chatId="status@broadcast",
        senderId="34612345678@s.whatsapp.net",
    )
    assert adapter._should_process_message(status_msg) is False

    # Channel / Newsletter broadcast posts.
    newsletter_msg = _dm_message(
        body="check out our latest post",
        chatId="120363999999999999@newsletter",
        senderId="120363999999999999@newsletter",
    )
    assert adapter._should_process_message(newsletter_msg) is False


def test_broadcast_filter_runs_before_allowlist():
    """A status@broadcast message from an allowlisted sender still drops —
    we never want to reply to Stories, even from authorized contacts.
    """
    adapter = _make_adapter(
        dm_policy="allowlist",
        allow_from=["34612345678@s.whatsapp.net"],
    )

    msg = _dm_message(
        body="[image received]",
        chatId="status@broadcast",
        senderId="34612345678@s.whatsapp.net",
    )
    assert adapter._should_process_message(msg) is False




def test_device_qualified_bot_ids_match_bare_mention_and_quote_ids():
    """Baileys reports the bot's own ids as ``<user>:<device>@lid`` while inbound
    mentionedJid / quoted participant ids are bare — both must normalize equal."""
    adapter = _make_adapter(require_mention=True, group_policy="open")
    device_qualified = ["447999674698:14@s.whatsapp.net", "116342762025117:14@lid"]

    assert adapter._should_process_message(
        _group_message("hi there", botIds=device_qualified, mentionedIds=["116342762025117@lid"])
    ) is True
    assert adapter._should_process_message(
        _group_message("and this?", botIds=device_qualified, quotedParticipant="447999674698@s.whatsapp.net")
    ) is True
    assert adapter._should_process_message(_group_message("hello everyone", botIds=device_qualified)) is False


# --- Q8: allow_reply_to_bot (anti-loop: disable reply-to-bot as a group trigger) ---


def test_reply_to_bot_triggers_by_default_unchanged_behavior():
    """No ``allow_reply_to_bot`` key set: reply-to-bot keeps triggering (current behavior)."""
    adapter = _make_adapter(require_mention=True, group_policy="open")

    assert adapter._should_process_message(
        _group_message("replying", quotedParticipant="15551230000@lid")
    ) is True


def test_allow_reply_to_bot_false_disables_reply_trigger_but_keeps_mention():
    """With the new key off: a plain reply to the bot no longer triggers a turn, but an
    explicit @mention and slash commands still do — this is the fix for Q8 (anti-loop)."""
    adapter = _make_adapter(require_mention=True, group_policy="open", allow_reply_to_bot=False)

    assert adapter._should_process_message(
        _group_message("replying", quotedParticipant="15551230000@lid")
    ) is False
    assert adapter._should_process_message(
        _group_message("hi there", mentionedIds=["15551230000@s.whatsapp.net"])
    ) is True
    assert adapter._should_process_message(_group_message("/status")) is True
    assert adapter._should_process_message(_group_message("hello everyone")) is False


def test_allow_reply_to_bot_env_fallback(monkeypatch):
    """``extra.allow_reply_to_bot`` unset falls through to ``WHATSAPP_ALLOW_REPLY_TO_BOT``."""
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    monkeypatch.setenv("WHATSAPP_ALLOW_REPLY_TO_BOT", "false")
    adapter = object.__new__(WhatsAppAdapter)
    adapter.config = PlatformConfig(enabled=True, extra={})
    assert adapter._whatsapp_allow_reply_to_bot() is False

    monkeypatch.setenv("WHATSAPP_ALLOW_REPLY_TO_BOT", "true")
    assert adapter._whatsapp_allow_reply_to_bot() is True

    monkeypatch.delenv("WHATSAPP_ALLOW_REPLY_TO_BOT", raising=False)
    assert adapter._whatsapp_allow_reply_to_bot() is True  # default: unchanged behavior


# --- Anti-loop: configured peer bots must not trigger an unbounded exchange.
# WhatsApp inherits ``BasePlatformAdapter.build_source``'s generic ``bot_users`` -> ``is_bot``
# marking (same mechanism already proven for Mattermost/Signal, #137027); no WhatsApp-specific
# code is needed, only the ``whatsapp.extra.bot_users`` config entry at activation (Fase B). ---


# --- Caso real t_d07cafdf (21/11/2026): "AI Database -- Agentes" sem @mencao nativa ---


def test_other_agent_naming_alfred_is_processed_with_free_response_chats():
    """Reproduz 21/11 11:05: a agente do Jose (559292484004@s.whatsapp.net) escreveu
    'Alfred, concordo...' no grupo de agentes e o Alfred NAO processou, porque
    require_mention=true e o grupo nao estava em free_response_chats; o Diego teve
    que cobrar e o Alfred respondeu manualmente via `hermes send`. Com
    free_response_chats incluindo o grupo (a correcao aplicada em config.yaml), a
    mesma mensagem passa a ser processada -- o SOUL do grupo decide calar ou
    responder (silencio por padrao), sem depender de @mencao nativa."""
    adapter = _make_adapter(
        require_mention=True,
        group_policy="allowlist",
        group_allow_from=["120363431745698109@g.us"],
        free_response_chats=["120363431745698109@g.us"],
    )

    jose_msg = _group_message(
        "Alfred, concordo com o ponto, mas acho que falta medir o efeito real.",
        chatId="120363431745698109@g.us",
        botIds=["238487420813351@lid"],
    )
    assert adapter._should_process_message(jose_msg) is True

    # Outro grupo nao fica free-response so porque o nome do bot aparece no texto.
    other_group_msg = _group_message(
        "Alfred, voce ja viu isso?",
        chatId="120363374829939162@g.us",
        botIds=["238487420813351@lid"],
    )
    assert adapter._should_process_message(other_group_msg) is False


def test_dm_is_unaffected_by_group_free_response_chats():
    """free_response_chats e por chat_id de grupo; nao pode liberar DM nenhuma."""
    adapter = _make_adapter(
        require_mention=True,
        group_policy="allowlist",
        group_allow_from=["120363431745698109@g.us"],
        free_response_chats=["120363431745698109@g.us"],
        dm_policy="allowlist",
        allow_from=["126194729091277@lid"],
    )
    dm = _dm_message("Alfred, bom dia", senderId="126194729091277@lid", chatId="126194729091277@lid")
    dm["from"] = "126194729091277@lid"
    # DM segue o caminho de _is_dm_intake_allowed (pairing/allowlist), nao o de grupo;
    # o ponto deste teste e so confirmar que o chat_id do DM nunca cai no set de
    # free_response_chats (que so contem o JID do grupo de agentes).
    assert dm["chatId"] not in adapter._whatsapp_free_response_chats()


def test_whatsapp_build_source_marks_configured_peer_bots_is_bot():
    """A WhatsApp JID listed in ``extra.bot_users`` is marked ``is_bot`` on its inbound source,
    so ``BotLoopGuard`` (authz_mixin._admit_bot_message/_admit_bot_chain) can cut a reply loop
    even though reply-to-bot stays enabled (peer bots may interact, while the
    exchange must end)."""
    adapter = _make_adapter(group_policy="open")
    adapter.config.extra["bot_users"] = ["15559998888@s.whatsapp.net"]
    adapter.gateway_runner = None

    peer_source = adapter.build_source(
        chat_id="120363001234567890@g.us", chat_type="group",
        user_id="15559998888@s.whatsapp.net", user_name="Other Agent",
    )
    human_source = adapter.build_source(
        chat_id="120363001234567890@g.us", chat_type="group",
        user_id="34612345678@s.whatsapp.net", user_name="Human",
    )
    assert peer_source.is_bot is True
    assert human_source.is_bot is False
