from app.routes.chat import _msg_html


def test_msg_html_error_uses_new_classes():
    html = _msg_html("assistant", "boom", is_error=True)
    assert "chat-answer-content" in html
    assert "chat-msg-error" in html
    assert 'class="chat-msg ' not in html


def test_msg_html_error_does_not_use_old_chat_content():
    html = _msg_html("assistant", "oops", is_error=True)
    assert "chat-content" not in html


def test_msg_html_user_is_bubble():
    html = _msg_html("user", "hi")
    assert "chat-bubble-user" in html


def test_msg_html_assistant_normal():
    html = _msg_html("assistant", "hello")
    assert "chat-answer" in html
    assert "chat-answer-content" in html
    assert "chat-msg-error" not in html
