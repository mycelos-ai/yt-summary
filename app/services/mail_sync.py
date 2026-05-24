"""Sync a profile's IMAP mailbox into the library.

Mirrors playlist_sync.py: read the profile's mailbox config, pull the
messages newer than the stored UID cursor, and for each new one create
an email-kind video row, store the cleaned body as its transcript, tag
it with the sender, and enqueue a summary job. The existing worker /
summarizer / embedder pick it up from there unchanged.
"""

import logging
import re
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
    strip_reply_prefix,
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


_OWN_ADDR_RE = re.compile(r"[^\s,;<>]+@[^\s,;<>]+")


def _own_addresses_from_settings(s: dict[str, str]) -> frozenset[str]:
    """The profile's own sending addresses (for the forward-to-summarize
    flow), parsed from the free-text profile field into lowercased
    addresses."""
    raw = s.get("mail_own_addresses") or ""
    return frozenset(a.lower() for a in _OWN_ADDR_RE.findall(raw))


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

    # Two ways a message gets summarized: it's from a subscribed
    # newsletter (strict opt-in), or it was forwarded from one of the
    # profile's own addresses (always summarized, unwrapped to the
    # original sender). With neither configured there's nothing to do —
    # skip the fetch entirely so the cursor stays put.
    subscribed = await mail_senders_repo.subscribed_addrs(db, user_id)
    own = _own_addresses_from_settings(s)
    if not subscribed and not own:
        return MailSyncResult(
            fetched=0, newly_ingested=0, skipped_existing=0, max_uid=0
        )

    try:
        since_uid = int(s.get("imap_last_uid") or 0)
    except ValueError:
        since_uid = 0

    messages = await fetch_new_messages(cfg, since_uid, own_addresses=own)

    # Keep the sender list fresh: surface senders in this batch on the
    # management page (as unsubscribed candidates) without a manual
    # rescan. For a forward we surface the *original* sender, not the
    # user's own address.
    candidates: list[tuple[str, str, str | None, str | None]] = []
    for m in messages:
        when = m.date.isoformat() if m.date else None
        if m.sender_addr.strip().lower() in own:
            if m.forwarded_addr:
                candidates.append(
                    (m.forwarded_addr, m.forwarded_name or m.forwarded_addr,
                     when, m.forwarded_subject or m.subject)
                )
        elif m.sender_addr:
            candidates.append((m.sender_addr, m.sender_name, when, m.subject))
    if candidates:
        await mail_senders_repo.upsert_discovered(db, user_id, candidates)

    newly = 0
    skipped = 0
    max_uid = since_uid
    for m in messages:
        max_uid = max(max_uid, m.uid)
        try:
            sender = m.sender_addr.strip().lower()
            is_forward = sender in own
            # Skip mail that's neither a forward nor from a subscribed
            # sender — the cursor still advances past it below.
            if not is_forward and sender not in subscribed:
                continue

            if is_forward:
                # Attribute to the original newsletter; fall back to a
                # generic "Forwarded" when the block couldn't be parsed.
                attr_addr = m.forwarded_addr or m.sender_addr
                attr_name = m.forwarded_name or "Forwarded"
                title = strip_reply_prefix(m.forwarded_subject or m.subject) \
                    or m.subject
            else:
                attr_addr, attr_name = m.sender_addr, m.sender_name
                title = m.subject

            item_id = f"{user_id}:{mail_id_from_message_id(m.message_id)}"
            existing = await videos_repo.get(db, item_id)
            if existing is not None:
                skipped += 1
                continue
            if not m.body.strip():
                # Nothing extractable (e.g. an image-only mail). Skip it
                # but let the cursor advance past it.
                continue
            description = f"From {attr_name} <{attr_addr}>".strip()
            await videos_repo.upsert_metadata(
                db,
                video_id=item_id,
                url=m.web_url or "",
                title=title,
                description=description,
                thumbnail_path=None,
                duration_seconds=None,
                user_id=user_id,
                kind=VideoKind.EMAIL,
            )
            await videos_repo.set_transcript(
                db, item_id, m.body, TranscriptSource.EMAIL
            )
            await tags_repo.set_tags_for_video(db, item_id, [attr_name])
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
