"""Sync a profile's IMAP mailbox into the library.

Mirrors playlist_sync.py: read the profile's mailbox config, pull the
messages newer than the stored UID cursor, and for each new one create
an email-kind video row, store the cleaned body as its transcript, tag
it with the sender, and enqueue a summary job. The existing worker /
summarizer / embedder pick it up from there unchanged.
"""

import logging
from dataclasses import dataclass

import aiosqlite

from app.config import Config
from app.models import TranscriptSource, VideoKind
from app.repos import jobs as jobs_repo
from app.repos import mail_senders as mail_senders_repo
from app.repos import settings as settings_repo
from app.repos import tags as tags_repo
from app.repos import videos as videos_repo
from app.services.mailbox import (
    ImapConfig,
    fetch_new_messages,
    mail_id_from_message_id,
)

log = logging.getLogger(__name__)


@dataclass
class MailSyncResult:
    fetched: int
    newly_ingested: int
    skipped_existing: int
    max_uid: int


def _imap_config_from_settings(
    s: dict[str, str], *, require_enabled: bool = True
) -> ImapConfig | None:
    """Build an ImapConfig from a profile's settings, or None if the
    mailbox isn't fully configured.

    `require_enabled` separates two distinct states: the scheduler only
    auto-polls when "Enable newsletter polling" is on, but the mailbox is
    still "connected" (and scannable for senders) as long as valid
    credentials are saved — so the sender-management UI passes
    require_enabled=False.
    """
    if require_enabled and not s.get("imap_enabled"):
        return None
    host = (s.get("imap_host") or "").strip()
    username = (s.get("imap_username") or "").strip()
    password = s.get("imap_password") or ""
    if not host or not username or not password:
        return None
    ssl = s.get("imap_ssl", "1") != "0"
    try:
        port = int(s.get("imap_port") or (993 if ssl else 143))
    except ValueError:
        port = 993 if ssl else 143
    folder = (s.get("imap_folder") or "INBOX").strip() or "INBOX"
    return ImapConfig(
        host=host,
        port=port,
        ssl=ssl,
        username=username,
        password=password,
        folder=folder,
    )


async def sync_mailbox(
    db: aiosqlite.Connection,
    config: Config,  # noqa: ARG001 — kept for signature parity with sync_playlist
    user_id: int,
) -> MailSyncResult | None:
    """Pull new newsletters for one profile. Returns None when the
    profile has no enabled mailbox (the common case for most profiles).
    """
    s = await settings_repo.get_all_for_user(db, user_id)
    cfg = _imap_config_from_settings(s)
    if cfg is None:
        return None

    # Newsletters are strictly opt-in: nothing is ingested until at
    # least one sender is subscribed. With an empty subscription set we
    # skip the fetch entirely — cheap, and the cursor (initialised to
    # "now" at scan time) stays put so subscribing later crawls forward.
    subscribed = await mail_senders_repo.subscribed_addrs(db, user_id)
    if not subscribed:
        return MailSyncResult(
            fetched=0, newly_ingested=0, skipped_existing=0, max_uid=0
        )

    try:
        since_uid = int(s.get("imap_last_uid") or 0)
    except ValueError:
        since_uid = 0

    messages = await fetch_new_messages(cfg, since_uid)

    # Keep the sender list fresh: surface any new senders in this batch
    # on the management page (as unsubscribed) without a manual rescan.
    seen_senders = [
        (
            m.sender_addr,
            m.sender_name,
            m.date.isoformat() if m.date else None,
            m.subject,
        )
        for m in messages
        if m.sender_addr
    ]
    if seen_senders:
        await mail_senders_repo.upsert_discovered(db, user_id, seen_senders)

    newly = 0
    skipped = 0
    max_uid = since_uid
    for m in messages:
        max_uid = max(max_uid, m.uid)
        try:
            # Strict opt-in: only ingest mail from subscribed senders.
            # Unsubscribed mail (incl. spam) is skipped, but the cursor
            # still advances past it below.
            if m.sender_addr.strip().lower() not in subscribed:
                continue
            item_id = f"{user_id}:{mail_id_from_message_id(m.message_id)}"
            existing = await videos_repo.get(db, item_id)
            if existing is not None:
                skipped += 1
                continue
            if not m.body.strip():
                # Nothing extractable (e.g. an image-only mail). Skip it
                # but let the cursor advance past it.
                continue
            description = f"From {m.sender_name} <{m.sender_addr}>".strip()
            await videos_repo.upsert_metadata(
                db,
                video_id=item_id,
                url=m.web_url or "",
                title=m.subject,
                description=description,
                thumbnail_path=None,
                duration_seconds=None,
                user_id=user_id,
                kind=VideoKind.EMAIL,
            )
            await videos_repo.set_transcript(
                db, item_id, m.body, TranscriptSource.EMAIL
            )
            await tags_repo.set_tags_for_video(db, item_id, [m.sender_name])
            await jobs_repo.enqueue(db, item_id)
            newly += 1
        except Exception:
            # One bad message must not stop the batch — log and move on,
            # same resilience contract as playlist_sync._process_entries.
            log.exception(
                "mail_sync: failed to ingest message uid=%s for user %s",
                m.uid,
                user_id,
            )

    if max_uid > since_uid:
        await settings_repo.set_for_user(
            db, user_id, "imap_last_uid", str(max_uid)
        )

    return MailSyncResult(
        fetched=len(messages),
        newly_ingested=newly,
        skipped_existing=skipped,
        max_uid=max_uid,
    )
