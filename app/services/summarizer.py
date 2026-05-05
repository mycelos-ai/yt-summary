from typing import Any

import litellm

SYSTEM_PROMPT = (
    "You are a careful summarizer. Produce a clear, structured summary of the "
    "following YouTube transcript. Use Markdown. Start with a one-paragraph "
    "TL;DR, then key points as bullets, then notable quotes if any."
)

REDUCE_SYSTEM_PROMPT = (
    "You are merging several partial summaries of a single YouTube transcript "
    "into one cohesive Markdown summary. Preserve the structure: TL;DR, key "
    "points, notable quotes."
)


def _split_into_chunks(transcript: str, model: str, target_tokens: int) -> list[str]:
    words = transcript.split()
    if not words:
        return []
    approx_words_per_chunk = max(int(target_tokens * 0.6), 100)
    chunks: list[str] = []
    i = 0
    while i < len(words):
        end = min(i + approx_words_per_chunk, len(words))
        chunk = " ".join(words[i:end])
        while litellm.token_counter(model=model, text=chunk) > target_tokens and end - i > 1:
            end = i + max((end - i) // 2, 1)
            chunk = " ".join(words[i:end])
        chunks.append(chunk)
        i = end
    return chunks


async def _completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    api_key: str,
    base_url: str | None,
) -> str:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "api_key": api_key,
    }
    if base_url:
        kwargs["api_base"] = base_url
    response = await litellm.acompletion(**kwargs)
    return response.choices[0].message.content or ""


async def summarize(
    *,
    transcript: str,
    model: str,
    api_key: str,
    base_url: str | None,
) -> str:
    max_tokens = litellm.get_max_tokens(model) or 8000
    budget = int(max_tokens * 0.7)
    transcript_tokens = litellm.token_counter(model=model, text=transcript)

    if transcript_tokens <= budget:
        return await _completion(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            api_key=api_key,
            base_url=base_url,
        )

    chunks = _split_into_chunks(transcript, model, target_tokens=budget // 2)
    partials: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        part = await _completion(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Part {idx} of {len(chunks)}:\n\n{chunk}",
                },
            ],
            api_key=api_key,
            base_url=base_url,
        )
        partials.append(part)

    return await _completion(
        model=model,
        messages=[
            {"role": "system", "content": REDUCE_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n---\n\n".join(partials)},
        ],
        api_key=api_key,
        base_url=base_url,
    )
