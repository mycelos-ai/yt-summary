from types import SimpleNamespace
from unittest.mock import patch

import imap_tools

from app.services.mailbox import (
    ImapConfig,
    clean_body,
    fetch_new_messages,
    mail_id_from_message_id,
)

NEWSLETTER_HTML = """
<html><head><style>.x{display:none}</style></head><body>
<span style="display:none;font-size:0">Invisible pre-header padding spam</span>
<img src="https://track.example.com/o.gif" width="1" height="1">
<table><tr><td><a href="https://news.example.com/web">View in browser</a></td></tr></table>
<h1>The Big Story This Week</h1>
<p>OpenAI shipped a new model called GPT-X with a 2 million token context
window. This matters because long-document workflows get cheaper.</p>
<p>Second item: Stripe migrated its billing system to Rust over 6 months.</p>
<hr>
<p>You received this email because you subscribed at example.com.</p>
<p><a href="https://news.example.com/unsub">Unsubscribe</a> | Manage preferences</p>
<p>&copy; 2026 Example Media. All rights reserved.</p>
</body></html>
"""


def test_mail_id_is_stable_and_prefixed():
    a = mail_id_from_message_id("<abc@example.com>")
    b = mail_id_from_message_id("<abc@example.com>")
    c = mail_id_from_message_id("<other@example.com>")
    assert a == b
    assert a != c
    assert a.startswith("mail-")
    assert len(a) == len("mail-") + 11


def test_clean_body_keeps_content_drops_cruft():
    body = clean_body(html=NEWSLETTER_HTML, plain=None)
    # Substance survives.
    assert "GPT-X" in body
    assert "Stripe" in body
    # Invisible pre-header spam removed by the lxml pre-clean.
    assert "pre-header padding" not in body
    # Footer boilerplate cut off.
    assert "unsubscribe" not in body.lower()
    assert "rights reserved" not in body
    assert "received this email" not in body


def test_clean_body_falls_back_to_plain_text():
    body = clean_body(html=None, plain="Plain text body.\nSecond line.")
    assert "Plain text body." in body
    assert "Second line." in body


def test_clean_body_empty_when_nothing_usable():
    assert clean_body(html="", plain="") == ""


def test_clean_body_strips_zero_width_padding():
    html = "<html><body><p>Real​‌ content­ here that is long "
    html += "enough to be extracted by the reader engine.</p></body></html>"
    body = clean_body(html=html, plain=None)
    assert "​" not in body and "‌" not in body and "­" not in body
    assert "Real content here" in body


def _fake_msg(
    uid, message_id, *, name="Acme Newsletter", from_="news@acme.com",
    subject="Weekly digest", html=None, text=None,
):
    return SimpleNamespace(
        uid=str(uid),
        headers={"message-id": (message_id,)} if message_id else {},
        from_=from_,
        from_values=SimpleNamespace(name=name),
        subject=subject,
        html=html,
        text=text,
        date=None,
        date_str="Mon, 01 Jan 2026 00:00:00 +0000",
    )


class _FakeBox:
    def __init__(self, messages):
        self._messages = messages
        self.fetched_kwargs = None

    def login(self, username, password, initial_folder=None):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetch(self, *args, mark_seen=False, bulk=False, limit=None, **kw):
        self.fetched_kwargs = {"mark_seen": mark_seen, "limit": limit}
        msgs = self._messages[:limit] if limit else self._messages
        return iter(msgs)


def _patch_box(messages):
    box = _FakeBox(messages)
    return patch.object(imap_tools, "MailBox", lambda host, port=None: box), box


def _cfg():
    return ImapConfig(
        host="imap.acme.com", port=993, ssl=True,
        username="news@acme.com", password="pw", folder="INBOX",
    )


async def test_fetch_new_messages_parses_and_cleans():
    msgs = [_fake_msg(101, "<a@acme.com>", html=NEWSLETTER_HTML)]
    cm, _box = _patch_box(msgs)
    with cm:
        out = await fetch_new_messages(_cfg(), since_uid=100)
    assert len(out) == 1
    m = out[0]
    assert m.uid == 101
    assert m.message_id == "<a@acme.com>"
    assert m.sender_name == "Acme Newsletter"
    assert m.subject == "Weekly digest"
    assert "GPT-X" in m.body
    assert m.web_url == "https://news.example.com/web"


async def test_fetch_new_messages_synthesises_id_when_missing():
    msgs = [_fake_msg(5, "", html="<html><body><p>Some body text here.</p></body></html>")]
    cm, _box = _patch_box(msgs)
    with cm:
        out = await fetch_new_messages(_cfg(), since_uid=0)
    assert out[0].message_id  # non-empty synthesised id


async def test_fetch_new_messages_marks_seen_and_limits():
    msgs = [_fake_msg(i, f"<{i}@acme.com>", html="<p>body</p>") for i in range(1, 5)]
    cm, box = _patch_box(msgs)
    with cm:
        await fetch_new_messages(_cfg(), since_uid=0, batch_limit=2)
    assert box.fetched_kwargs["mark_seen"] is True
    assert box.fetched_kwargs["limit"] == 2


async def test_missing_imap_tools_raises_friendly_error():
    import sys

    from app.services.mailbox import check_connection

    # Simulate imap-tools not being installed: a None entry in
    # sys.modules makes `import imap_tools` raise ImportError.
    with patch.dict(sys.modules, {"imap_tools": None}):
        try:
            await check_connection(_cfg())
        except ValueError as e:
            assert "isn't installed" in str(e)
        else:
            raise AssertionError("expected ValueError")


async def test_discover_senders_aggregates_by_address():
    from app.services.mailbox import discover_senders

    # reverse=True in the real fetch yields newest first; the fake just
    # returns this list in order, so first-seen = "latest".
    msgs = [
        _fake_msg(50, "<x@a.com>", name="Acme", from_="news@acme.com",
                  subject="Latest issue"),
        _fake_msg(49, "<y@a.com>", name="Acme", from_="news@acme.com",
                  subject="Older issue"),
        _fake_msg(48, "<z@b.com>", name="Other", from_="hi@other.com",
                  subject="Hello"),
    ]
    cm, _box = _patch_box(msgs)
    with cm:
        result = await discover_senders(_cfg(), limit=10)

    by_addr = {s.addr: s for s in result.senders}
    assert set(by_addr) == {"news@acme.com", "hi@other.com"}
    assert by_addr["news@acme.com"].count == 2
    assert by_addr["news@acme.com"].last_subject == "Latest issue"
    assert result.max_uid == 50


async def test_fetch_new_messages_wraps_errors_as_valueerror():
    def boom(host, port=None):
        raise OSError("connection refused")

    with patch.object(imap_tools, "MailBox", boom):
        try:
            await fetch_new_messages(_cfg(), since_uid=0)
        except ValueError as e:
            assert "IMAP connection failed" in str(e)
        else:
            raise AssertionError("expected ValueError")
