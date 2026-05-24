from unittest.mock import AsyncMock, patch

from app.config import Config
from app.models import VideoKind
from app.repos import jobs as jobs_repo
from app.repos import mail_senders as mail_senders_repo
from app.repos import settings as settings_repo
from app.repos import tags as tags_repo
from app.repos import videos as videos_repo
from app.services.mail_sync import sync_mailbox
from app.services.mailbox import MailMessage, mail_id_from_message_id


def _msg(
    uid, mid, *, subject="Subj", sender="Acme News",
    addr="news@acme.com", body="Body content.",
    fwd_addr=None, fwd_name=None, fwd_subject=None,
):
    return MailMessage(
        uid=uid,
        message_id=mid,
        sender_name=sender,
        sender_addr=addr,
        subject=subject,
        date=None,
        body=body,
        web_url="https://acme.com/web",
        forwarded_addr=fwd_addr,
        forwarded_name=fwd_name,
        forwarded_subject=fwd_subject,
    )


async def _enable_imap(db, user_id=1):
    for k, v in {
        "imap_enabled": "1",
        "imap_host": "imap.acme.com",
        "imap_username": "news@acme.com",
        "imap_password": "pw",
    }.items():
        await settings_repo.set_for_user(db, user_id, k, v)


async def _subscribe(db, addr="news@acme.com", user_id=1):
    await mail_senders_repo.upsert_discovered(
        db, user_id, [(addr, "Acme News", None, None)]
    )
    await mail_senders_repo.set_subscriptions(db, user_id, [addr])


async def test_sync_disabled_returns_none(db, config: Config):
    assert await sync_mailbox(db, config, 1) is None


async def test_sync_ingests_messages(db, config: Config):
    await _enable_imap(db)
    await _subscribe(db)
    msgs = [_msg(10, "<a@acme.com>"), _msg(11, "<b@acme.com>", subject="Other")]
    with patch(
        "app.services.mail_sync.fetch_new_messages", AsyncMock(return_value=msgs)
    ):
        result = await sync_mailbox(db, config, 1)

    assert result is not None
    assert result.newly_ingested == 2
    item_id = f"1:{mail_id_from_message_id('<a@acme.com>')}"
    v = await videos_repo.get(db, item_id)
    assert v is not None
    assert v.kind is VideoKind.EMAIL
    assert v.title == "Subj"
    assert v.transcript == "Body content."
    assert v.url == "https://acme.com/web"
    # Sender becomes a tag.
    assert "Acme News" in await tags_repo.tags_for_video(db, item_id)
    # A summary job was enqueued.
    job = await jobs_repo.latest_for_video(db, item_id)
    assert job is not None
    # Cursor advanced to the highest UID seen.
    assert await settings_repo.get_for_user(db, 1, "imap_last_uid") == "11"


async def test_sync_is_idempotent(db, config: Config):
    await _enable_imap(db)
    await _subscribe(db)
    msgs = [_msg(10, "<a@acme.com>")]
    with patch(
        "app.services.mail_sync.fetch_new_messages", AsyncMock(return_value=msgs)
    ):
        first = await sync_mailbox(db, config, 1)
        # Second run returns the same message (e.g. cursor not yet past it);
        # it must be skipped, not re-ingested.
        second = await sync_mailbox(db, config, 1)

    assert first is not None and first.newly_ingested == 1
    assert second is not None and second.newly_ingested == 0
    assert second.skipped_existing == 1


async def test_sync_skips_empty_body_but_advances_cursor(db, config: Config):
    await _enable_imap(db)
    await _subscribe(db)
    msgs = [_msg(20, "<empty@acme.com>", body="   ")]
    with patch(
        "app.services.mail_sync.fetch_new_messages", AsyncMock(return_value=msgs)
    ):
        result = await sync_mailbox(db, config, 1)

    assert result is not None
    assert result.newly_ingested == 0
    item_id = f"1:{mail_id_from_message_id('<empty@acme.com>')}"
    assert await videos_repo.get(db, item_id) is None
    # Cursor still advances past the skipped message.
    assert await settings_repo.get_for_user(db, 1, "imap_last_uid") == "20"


async def test_sync_one_bad_message_does_not_abort_batch(db, config: Config):
    await _enable_imap(db)
    await _subscribe(db)
    msgs = [_msg(30, "<good@acme.com>"), _msg(31, "<bad@acme.com>")]

    real_enqueue = jobs_repo.enqueue
    calls = {"n": 0}

    async def flaky_enqueue(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db hiccup")
        return await real_enqueue(*args, **kwargs)

    with (
        patch(
            "app.services.mail_sync.fetch_new_messages",
            AsyncMock(return_value=msgs),
        ),
        patch("app.services.mail_sync.jobs_repo.enqueue", flaky_enqueue),
    ):
        result = await sync_mailbox(db, config, 1)

    # First message blew up in enqueue; second still ingested.
    assert result is not None
    assert result.newly_ingested == 1


async def test_sync_strict_opt_in_ingests_nothing_without_subscription(db, config: Config):
    """With no subscribed senders the sync ingests nothing and never
    even fetches — strict opt-in."""
    await _enable_imap(db)  # connected, but no sender subscribed
    fetch_mock = AsyncMock(return_value=[_msg(10, "<a@acme.com>")])
    with patch("app.services.mail_sync.fetch_new_messages", fetch_mock):
        result = await sync_mailbox(db, config, 1)

    assert result is not None
    assert result.newly_ingested == 0
    fetch_mock.assert_not_awaited()  # no fetch when nobody is subscribed


async def test_sync_only_ingests_subscribed_senders(db, config: Config):
    """Mail from unsubscribed senders (e.g. spam) is skipped; subscribed
    senders are ingested; the cursor advances past both."""
    await _enable_imap(db)
    await _subscribe(db, addr="news@acme.com")
    msgs = [
        _msg(40, "<wanted@acme.com>", addr="news@acme.com"),
        _msg(41, "<spam@evil.com>", addr="spam@evil.com", sender="Spammer"),
    ]
    with patch(
        "app.services.mail_sync.fetch_new_messages", AsyncMock(return_value=msgs)
    ):
        result = await sync_mailbox(db, config, 1)

    assert result is not None
    assert result.newly_ingested == 1  # only the subscribed sender
    wanted_id = f"1:{mail_id_from_message_id('<wanted@acme.com>')}"
    spam_id = f"1:{mail_id_from_message_id('<spam@evil.com>')}"
    assert await videos_repo.get(db, wanted_id) is not None
    assert await videos_repo.get(db, spam_id) is None
    # Cursor advances past both so we don't re-scan the spam next tick.
    assert await settings_repo.get_for_user(db, 1, "imap_last_uid") == "41"
    # The unsubscribed sender still surfaces in the management list
    # (as not-subscribed) so the user can subscribe later if they want.
    addrs = {s.sender_addr for s in await mail_senders_repo.list_for_user(db, 1)}
    assert "spam@evil.com" in addrs


async def test_sync_forwarded_mail_attributed_to_original_sender(db, config: Config):
    """A mail forwarded from one of the profile's own addresses is always
    summarized, unwrapped to the original newsletter, and the original
    sender becomes a (non-subscribed) candidate."""
    await _enable_imap(db)
    await settings_repo.set_for_user(db, 1, "mail_own_addresses", "stefan@gmail.com")
    # No subscriptions at all — the forward path must still ingest.
    msgs = [
        _msg(60, "<fwd1@acme.com>", addr="stefan@gmail.com", sender="Stefan",
             subject="Fwd: TLDR 2026-05-24", body="Forwarded newsletter body.",
             fwd_addr="dan@tldr.tech", fwd_name="TLDR",
             fwd_subject="TLDR 2026-05-24"),
    ]
    with patch(
        "app.services.mail_sync.fetch_new_messages", AsyncMock(return_value=msgs)
    ):
        result = await sync_mailbox(db, config, 1)

    assert result is not None
    assert result.newly_ingested == 1
    item_id = f"1:{mail_id_from_message_id('<fwd1@acme.com>')}"
    v = await videos_repo.get(db, item_id)
    assert v is not None
    assert v.title == "TLDR 2026-05-24"  # attributed to the original
    assert "TLDR" in await tags_repo.tags_for_video(db, item_id)
    # Original sender surfaces as candidate; own address does not.
    addrs = {s.sender_addr for s in await mail_senders_repo.list_for_user(db, 1)}
    assert "dan@tldr.tech" in addrs
    assert "stefan@gmail.com" not in addrs
    # Forwarding does NOT auto-subscribe — strict opt-in stays intact.
    assert await mail_senders_repo.subscribed_addrs(db, 1) == set()


async def test_sync_unparsed_forward_falls_back_to_generic(db, config: Config):
    """When the forward block can't be parsed, still summarize (generic
    'Forwarded' attribution) and add no candidate."""
    await _enable_imap(db)
    await settings_repo.set_for_user(db, 1, "mail_own_addresses", "stefan@gmail.com")
    msgs = [
        _msg(61, "<fwd2@acme.com>", addr="stefan@gmail.com", sender="Stefan",
             subject="Fwd: Some article", body="A forwarded one-off article."),
    ]
    with patch(
        "app.services.mail_sync.fetch_new_messages", AsyncMock(return_value=msgs)
    ):
        result = await sync_mailbox(db, config, 1)

    assert result is not None
    assert result.newly_ingested == 1
    item_id = f"1:{mail_id_from_message_id('<fwd2@acme.com>')}"
    v = await videos_repo.get(db, item_id)
    assert v is not None
    assert v.title == "Some article"  # "Fwd:" stripped
    assert "Forwarded" in await tags_repo.tags_for_video(db, item_id)
    # Nothing to subscribe to → no candidate created.
    assert await mail_senders_repo.list_for_user(db, 1) == []
