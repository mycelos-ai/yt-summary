from collections.abc import AsyncIterator
from typing import Any

import litellm

from app.models import ChatMessage

SYSTEM_TEMPLATE = (
    "You are answering follow-up questions about a YouTube video. "
    "The full transcript is below. Answer accurately based on the transcript. "
    "If something is not in the transcript, say so.\n\n"
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
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_TEMPLATE.format(transcript=transcript)},
    ]
    for m in history:
        messages.append({"role": m.role, "content": m.content})
    messages.append({"role": "user", "content": user_message})

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "api_key": api_key,
        "stream": True,
    }
    if base_url:
        kwargs["api_base"] = base_url

    response = await litellm.acompletion(**kwargs)
    async for chunk in response:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
