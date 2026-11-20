# Opt-in group archive

Set `whatsapp.extra.archive_group_jids` to an explicit list of full numeric
`@g.us` JIDs. The default is `[]`: no archive and no change to intake.
Restart the gateway after changing this list; the adapter transports it to its
owned bridge as a configuration fingerprinted internal environment value.

The bridge forwards these groups without applying its transport group-policy
filter. The adapter appends each inbound message before applying dispatch
policy. Storage is `$HERMES_HOME/whatsapp/group-archive/<jid>/YYYY-MM-DD.jsonl`,
with UTC daily partitions, private directories and mode 0600 files. Records
contain timestamp, chat_jid, sender, text, id, quoted_id, urls and media metadata.
Attachment bytes, quoted attachment paths and bearer credentials are not stored.
The source timestamp is retained; missing timestamps use reception time.

The JSONL archive IS the read-only source-group feed. There is no parallel inbox,
second database, per-message agent trigger or automatic publisher. A later
batch consumer can read this feed without granting the group an agent route.
Append uses fsync, and deduplication reads message IDs across all daily files,
including after restarts. Invalid/missing IDs and damaged JSONL fail closed.
The gateway owns one adapter/poll loop for this archive; simultaneous independent
writers to the same profile archive are not supported.

An archived group absent from `group_allow_from` is archive-only: never opens a
turn, never downloads attachment payloads, and adapter sends (text, media, polls,
locations, edits, reactions and typing) and standalone delivery are denied.
An archived group also present in `group_allow_from` retains normal group-policy
and mention checks after archival. Archival itself never sends a message.

# Checkout backups

`python` callers can import `scripts.checkout_snapshot.backup_checkout`.
It verifies tracked disk objects against Git blobs (respecting clean filters),
writes a PAX filesystem snapshot and verifies the archived bytes against disk.
Do not compare `git archive` bytes to checkout bytes: `.gitattributes` with
`eol=crlf` can produce CRLF exports from an LF checkout despite a clean Git tree.
The caller must ensure no concurrent checkout mutation and a clean tree first.
