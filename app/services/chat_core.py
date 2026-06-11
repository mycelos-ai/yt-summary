"""Shared chat message construction.

Both the per-video chat (streaming) and ask-my-library (background job)
build the same [system] + history + user message list before invoking
the model. That construction lives here; the two paths differ only in
HOW they call the model, not in how the message list is shaped.
"""

from __future__ import annotations


def build_messages(
    *,
    system_prompt: str,
    history: list[tuple[str, str]],
    user_message: str,
) -> list[dict[str, str]]:
    """[system] + history turns (each a (role, content) pair) + the new
    user message. Pure — no LLM call."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt}
    ]
    for role, content in history:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    return messages
