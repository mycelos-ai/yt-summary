from unittest.mock import AsyncMock, patch

from app.config import Config
from app.models import VideoKind
from app.repos import jobs as jobs_repo
from app.repos import settings as settings_repo
from app.repos import tags as tags_repo
from app.repos import videos as videos_repo
from app.services.mail_sync import sync_mailbox
from app.services.mailbox import MailMessage, mail_id_from_message_id


def _msg(uid, mid, *, subject="Subj", sender="Acme News", body="Body content."):
    return MailMessage(
        uid=uid,
        message_id=mid,
        sender_name=sender,
        sender_addr="news@acme.com",
        subject=subject,
        date=None,
        body=body,
        web_url="https://acme.com/web",
    )


async def _enable_imap(db, user_id=1):
    for k, v in {
        "imap_enabled": "1",
        "imap_host": "imap.acme.com",
        "imap_username": "news@acme.com",
        "imap_password": "pw",
    }.items():
        await settings_repo.set_for_user(db, user_id, k, v)


async def test_sync_disabled_returns_none(db, config: Config):
    assert await sync_mailbox(db, config, 1) is None


async def test_sync_ingests_messages(db, config: Config):
    await _enable_imap(db)
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
