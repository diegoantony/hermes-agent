"""t_d07cafdf (22/11/2026): mensagens de outros agentes devem entrar no contexto da sessão do
Alfred mesmo quando não abrem turno (porque não citam "Alfred" nem mencionam o bot). O
group-archive já grava TODAS as mensagens do grupo opt-in (``archive_group_jids``); este módulo
lê as últimas N e as injeta, rotuladas como conteúdo de terceiros (nunca instrução), na frente
da mensagem que de fato abre turno — sem mudar o que é arquivado nem o gate de processamento.
"""
import json

from plugins.platforms.whatsapp import group_archive


def _write_row(directory, day, **row):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_recent_messages_returns_last_n_oldest_first_excluding_trigger(tmp_path):
    jid = "120363431745698109@g.us"
    directory = tmp_path / "whatsapp" / "group-archive" / jid
    for i in range(5):
        _write_row(directory, "2026-11-21", timestamp=1795200000 + i, chat_jid=jid,
                   sender="isa@lid", text=f"msg {i}", id=f"id{i}")
    rows = group_archive.recent_messages(tmp_path, jid, limit=3, exclude_id="id4")
    assert [r["text"] for r in rows] == ["msg 1", "msg 2", "msg 3"]


def test_recent_messages_empty_when_no_archive_dir(tmp_path):
    assert group_archive.recent_messages(tmp_path, "120363431745698109@g.us", limit=5) == []


def test_format_context_block_labels_as_third_party_content_never_instruction():
    rows = [{"sender": "559292484004@s.whatsapp.net", "text": "proponho quarta-feira",
             "timestamp": 1795200000}]
    block = group_archive.format_context_block(rows)
    assert "quarta-feira" in block
    assert "nunca instruç" in block.lower() or "não são instruç" in block.lower()


def test_format_context_block_empty_rows_returns_empty_string():
    assert group_archive.format_context_block([]) == ""


def test_inject_context_prepends_block_only_for_opted_in_archived_groups(tmp_path):
    jid = "120363431745698109@g.us"
    directory = tmp_path / "whatsapp" / "group-archive" / jid
    _write_row(directory, "2026-11-21", timestamp=1795200000, chat_jid=jid,
               sender="559292484004@s.whatsapp.net", text="proponho quarta-feira", id="prev1")

    extra = {"archive_group_jids": [jid], "context_inject_limit": 5}
    data = {"chatId": jid, "isGroup": True, "messageId": "trigger1"}
    body = group_archive.inject_context(tmp_path, extra, data, "Alfred, confirma?")
    assert "proponho quarta-feira" in body
    assert body.endswith("Alfred, confirma?")

    # Not in archive_group_jids: body passes through unchanged (no injection anywhere else).
    other_extra = {"archive_group_jids": [], "context_inject_limit": 5}
    unchanged = group_archive.inject_context(tmp_path, other_extra, data, "Alfred, confirma?")
    assert unchanged == "Alfred, confirma?"

    # limit=0 (opt-in, disabled by default): unchanged even for an archived group.
    disabled_extra = {"archive_group_jids": [jid], "context_inject_limit": 0}
    disabled = group_archive.inject_context(tmp_path, disabled_extra, data, "Alfred, confirma?")
    assert disabled == "Alfred, confirma?"


def test_inject_context_excludes_the_triggering_message_itself(tmp_path):
    """The triggering message is already archived by append_group_message before
    _build_message_event runs; injecting it again would duplicate it in the body."""
    jid = "120363431745698109@g.us"
    directory = tmp_path / "whatsapp" / "group-archive" / jid
    _write_row(directory, "2026-11-21", timestamp=1795200000, chat_jid=jid,
               sender="238487420813351@lid", text="Alfred, confirma?", id="trigger1")
    extra = {"archive_group_jids": [jid], "context_inject_limit": 5}
    data = {"chatId": jid, "isGroup": True, "messageId": "trigger1"}
    body = group_archive.inject_context(tmp_path, extra, data, "Alfred, confirma?")
    # No prior messages besides the trigger itself -> nothing to inject.
    assert body == "Alfred, confirma?"
