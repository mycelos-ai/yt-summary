"""Unit tests for the streamed chat-message fragment builder.

Import `app.main` first so it finishes loading before we reach into
`app.routes.chat` — that route module imports from `app.main`, so a
top-level `from app.routes.chat import _msg_html` as the very first
import triggers a circular import. Every other route test follows the
same "create_app first, route bits locally" pattern.
"""

import app.main  # noqa: F401  (load app.main fully before routes.chat)


def test_msg_html_error_uses_new_classes():
    from app.routes.chat import _msg_html
    html = _msg_html("assistant", "boom", is_error=True)
    assert "chat-answer-content" in html
    assert "chat-msg-error" in html
    assert 'class="chat-msg ' not in html


def test_msg_html_error_does_not_use_old_chat_content():
    from app.routes.chat import _msg_html
    html = _msg_html("assistant", "oops", is_error=True)
    assert "chat-content" not in html


def test_msg_html_user_is_bubble():
    from app.routes.chat import _msg_html
    html = _msg_html("user", "hi")
    assert "chat-bubble-user" in html


def test_msg_html_assistant_normal():
    from app.routes.chat import _msg_html
    html = _msg_html("assistant", "hello")
    assert "chat-answer" in html
    assert "chat-answer-content" in html
    assert "chat-msg-error" not in html
