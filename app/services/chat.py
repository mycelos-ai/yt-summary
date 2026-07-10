from collections.abc import AsyncIterator
from typing import Any

import litellm

from app.models import ChatMessage
from app.services.chat_core import build_messages

SYSTEM_TEMPLATE = (
    "You are answering follow-up questions about a YouTube video. "
    "The full transcript is below. Answer accurately based on the "
    "transcript. If something isn't in the transcript, say so "
    "explicitly — don't make up facts.\n\n"
    "FORMAT YOUR RESPONSE AS MARKDOWN:\n"
    "- Use **bold** for key terms.\n"
    "- Use bullet lists for enumerations.\n"
    "- Use tables (`| col | col |` with `|---|---|` separator row) "
    "for two-column mappings — concept → explanation, option → "
    "tradeoff, name → role. They render as proper HTML tables, not "
    "raw pipes. Don't force a table for plain lists; bullets read "
    "better there.\n"
    "- Use `inline code` for technical terms, commands, or short "
    "snippets, and fenced code blocks (```lang ... ```) for any "
    "multi-line code.\n"
    "- Keep responses scannable — short paragraphs, no walls of text.\n\n"
    "TRANSCRIPT:\n{transcript}"
)


async def stream_reply(
    *,
    transcript: str,
    history: list[ChatMessage],
    user_message: str,
    model: str,
    api_key: str,
    base_url: str | None,
) -> AsyncIterator[str]:
    messages = build_messages(
        system_prompt=SYSTEM_TEMPLATE.format(transcript=transcript),
        history=[(m.role, m.content) for m in history],
        user_message=user_message,
    )

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "api_key": api_key,
        "stream": True,
    }
    if base_url:
        kwargs["api_base"] = base_url

    response: Any = await litellm.acompletion(**kwargs)
    async for chunk in response:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
