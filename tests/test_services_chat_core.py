from app.services.chat_core import build_messages


def test_build_messages_orders_system_history_user():
    msgs = build_messages(
        system_prompt="SYS",
        history=[("user", "q1"), ("assistant", "a1")],
        user_message="q2",
    )
    assert msgs == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]


def test_build_messages_empty_history():
    msgs = build_messages(system_prompt="S", history=[], user_message="hi")
    assert msgs == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "hi"},
    ]
