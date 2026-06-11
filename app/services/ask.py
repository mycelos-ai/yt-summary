"""Library synthesis — "ask my library" (Part C.2).

A question answered across the Profile's stored items, with citations
back into the library. Generalizes the digest machinery from "last 24 h"
to "this question": hybrid search (FTS + vector RRF) selects the top N
items, their summaries are packed into one LLM call, and the answer is
Markdown with [title](/v/{id}) source links.

The LLM call runs in the background (the route enqueues, like digest),
so this module never blocks a request handler.
"""

from __future__ import annotations

import json
import logging

import aiosqlite
import litellm

from app.models import Video
from app.repos import llm_models as llm_models_repo
from app.repos import settings as settings_repo
from app.repos import syntheses as syntheses_repo
from app.repos import videos as videos_repo

log = logging.getLogger(__name__)

# Default number of source items packed into the synthesis call. Summaries
# (not transcripts) keep the token budget sane.
DEFAULT_SOURCE_LIMIT = 8

_SYSTEM = (
    "You answer questions using ONLY the provided summaries from the "
    "user's personal library. Cite every claim with the item's Markdown "
    "link, exactly as given in the sources (e.g. [Title](/v/<id>)). If the "
    "provided summaries don't cover the question, say so plainly instead "
    "of guessing. Answer in Markdown. Do not invent items or links."
)


async def _vector_ids(db: aiosqlite.Connection, query: str) -> list[str]:
    """Embed the query and return ids ranked by similarity; [] on any
    failure so retrieval degrades to FTS-only."""
    try:
        from app.repos import embeddings as embeddings_repo
        from app.services.embeddings import embed_text
        settings = await settings_repo.get_all(db)
        model = settings.get("embedding_model", "").strip() or None
        base_url = settings.get("embedding_base_url", "").strip() or None
        vector = await embed_text(query, model=model, api_key="", base_url=base_url)
        hits = await embeddings_repo.search_by_summary_vector(db, vector, limit=50)
        return [vid for vid, _ in hits]
    except Exception as e:  # pragma: no cover - defensive
        log.info("ask: vector retrieval degraded to FTS: %s", e)
        return []


async def gather_sources(
    db: aiosqlite.Connection, query: str, *, user_id: int,
    limit: int = DEFAULT_SOURCE_LIMIT,
) -> list[Video]:
    """Top items for the question, via the same hybrid search as home."""
    vector_ids = await _vector_ids(db, query)
    videos = await videos_repo.search(
        db, query, limit=limit, vector_ids=vector_ids, user_id=user_id,
    )
    # Only items that actually have a summary are useful as sources.
    return [v for v in videos if v.summary][:limit]


def build_prompt(query: str, sources: list[Video]) -> tuple[str, str]:
    """Pure builder: (system, user) messages. The user message lists each
    source with its citation link target and summary."""
    blocks = []
    for v in sources:
        blocks.append(
            f"### [{v.title}](/v/{v.id})\n{v.summary}"
        )
    sources_md = "\n\n".join(blocks) if blocks else "(no matching items)"
    user = (
        f"QUESTION:\n{query}\n\n"
        f"SOURCES (cite these by their Markdown links):\n\n{sources_md}\n\n"
        "Answer now, in Markdown, citing every claim."
    )
    return _SYSTEM, user



async def _completion_messages(
    *, messages: list[dict], model: str, api_key: str,
    base_url: str | None,
) -> str:
    """litellm call from a prebuilt messages list (system already at [0]
    via build_messages). Monkeypatched in tests."""
    kwargs: dict = {"model": model, "messages": messages, "api_key": api_key}
    if base_url:
        kwargs["api_base"] = base_url
    response = await litellm.acompletion(**kwargs)
    return response.choices[0].message.content or ""


def _sources_block(sources: list[Video]) -> str:
    blocks = [f"### [{v.title}](/v/{v.id})\n{v.summary}" for v in sources]
    return "\n\n".join(blocks) if blocks else "(no matching items)"


async def start_thread(
    db: aiosqlite.Connection, *, user_id: int, query: str,
) -> tuple[int, int]:
    """Create a thread: synthesis container (recording the fixed source
    set via retrieval), the first user message, and a pending assistant
    message. Returns (synthesis_id, assistant_message_id)."""
    from app.models import SynthesisStatus
    from app.repos import synthesis_messages as sm_repo
    sources = await gather_sources(db, query, user_id=user_id)
    s = await syntheses_repo.create_pending(
        db, user_id=user_id, query=query, source_ids=[v.id for v in sources],
    )
    await sm_repo.append(db, synthesis_id=s.id, role="user", content=query,
                         status=SynthesisStatus.READY)
    assistant = await sm_repo.append(db, synthesis_id=s.id, role="assistant",
                                     content=None, status=SynthesisStatus.PENDING)
    return s.id, assistant.id


async def add_followup(
    db: aiosqlite.Connection, *, synthesis_id: int, query: str,
) -> int:
    """Append a user turn + pending assistant turn to an existing thread.
    Returns the assistant message id. Does NOT re-search — the thread's
    fixed source set is reused."""
    from app.models import SynthesisStatus
    from app.repos import synthesis_messages as sm_repo
    await sm_repo.append(db, synthesis_id=synthesis_id, role="user",
                         content=query, status=SynthesisStatus.READY)
    assistant = await sm_repo.append(db, synthesis_id=synthesis_id,
                                     role="assistant", content=None,
                                     status=SynthesisStatus.PENDING)
    return assistant.id


async def run_message(db: aiosqlite.Connection, *, message_id: int) -> None:
    """Answer one pending assistant message using the thread's fixed
    source set and prior turns as context. Marks the message ready/failed."""
    from app.repos import synthesis_messages as sm_repo
    from app.services.chat_core import build_messages
    msg = await sm_repo.get(db, message_id)
    if msg is None:
        return
    model_row = await llm_models_repo.get_default(db)
    if model_row is None:
        await sm_repo.mark_failed(db, message_id=message_id,
                                  error="No default LLM configured")
        return
    try:
        s = await syntheses_repo.get(db, msg.synthesis_id)
        ids = json.loads(s.source_ids_json) if s and s.source_ids_json else []
        by_id = await videos_repo.get_many(db, ids)
        sources = [by_id[i] for i in ids if i in by_id]
        system = f"{_SYSTEM}\n\nSOURCES:\n\n{_sources_block(sources)}"
        turns = await sm_repo.history(db, synthesis_id=msg.synthesis_id)
        # Every message before this pending assistant. start_thread /
        # add_followup always insert (user, assistant) pairs, so `prior`
        # ends with the user question we're answering: prior[-1] is that
        # question, prior[:-1] is the conversation context.
        prior = [t for t in turns if t.id < message_id]
        if not prior:
            await sm_repo.mark_failed(
                db, message_id=message_id,
                error="Thread invariant violated: no user turn precedes message",
            )
            return
        user_turn = prior[-1].content or ""
        # Drop turns with no content (a prior assistant turn that failed or
        # is still pending) — passing ("assistant", None) into the model
        # breaks the messages contract.
        hist = [
            (t.role, t.content) for t in prior[:-1] if t.content is not None
        ]
        messages = build_messages(system_prompt=system, history=hist,
                                  user_message=user_turn)
        answer = await _completion_messages(
            messages=messages, model=model_row.model,
            api_key=model_row.api_key or "", base_url=model_row.base_url or None,
        )
        await sm_repo.mark_ready(db, message_id=message_id, content=answer)
    except Exception as e:
        log.exception("ask: message %s failed", message_id)
        await sm_repo.mark_failed(db, message_id=message_id,
                                  error=f"{type(e).__name__}: {e}")


async def ask_now(
    db: aiosqlite.Connection, *, user_id: int, query: str,
):
    """Create a one-turn thread, answer it synchronously, return
    (synthesis_row, assistant_message). Used by REST API + MCP tool."""
    from app.repos import synthesis_messages as sm_repo
    s_id, assistant_id = await start_thread(db, user_id=user_id, query=query)
    await run_message(db, message_id=assistant_id)
    s = await syntheses_repo.get(db, s_id)
    answer = await sm_repo.get(db, assistant_id)
    return s, answer


