"""Opt-in, append-only group feed; never dispatches turns or downloads media."""
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

_LOCK = Lock()
_JID = re.compile(r"[0-9]+@g\.us\Z")
_URL = re.compile(r"https?://[^\s<>]+")


def archive_only(extra: dict, jid: str) -> bool:
    """Archived groups outside the reply allowlist are read-only, even under open policy."""
    return jid in extra.get("archive_group_jids", []) and jid not in extra.get("group_allow_from", [])


def append_group_message(home: Path, extra: dict, data: dict) -> bool:
    """Persist before policy. JSONL is the sole durable feed and dedup authority.

    The adapter calls synchronously on its single poll loop. The lock additionally
    serializes callers in other threads; each restart rebuilds dedup from the feed.
    Failures propagate so an unarchived message cannot open an agent turn.
    """
    jid = str(data.get("chatId") or "")
    if not data.get("isGroup") or jid not in extra.get("archive_group_jids", []):
        return False
    if not _JID.fullmatch(jid):
        raise ValueError("Invalid archive group JID")
    message_id = str(data.get("messageId") or "")
    if not message_id:
        raise ValueError("Archived message requires messageId")
    timestamp = data.get("timestamp")
    instant = datetime.fromtimestamp(float(timestamp), UTC) if timestamp is not None else datetime.now(UTC)
    text = str(data.get("body") or "")
    media = {key: data[value] for key, value in (
        ("type", "mediaType"), ("mime", "mime"), ("file_name", "fileName"),
    ) if data.get(value)} if data.get("hasMedia") else {}
    row = dict(timestamp=timestamp if timestamp is not None else instant.isoformat(),
               chat_jid=jid, sender=data.get("senderId"), text=text, id=message_id,
               quoted_id=data.get("quotedMessageId"), urls=list(dict.fromkeys(_URL.findall(text))), media=media)
    with _LOCK:
        archive_root = home / "whatsapp" / "group-archive"
        archive_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = archive_root / jid
        directory.mkdir(exist_ok=True, mode=0o700)
        for existing in directory.glob("*.jsonl"):
            with existing.open(encoding="utf-8") as stream:
                for line in stream:
                    if json.loads(line)["id"] == message_id:
                        return False
        path = directory / f"{instant:%Y-%m-%d}.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return True
