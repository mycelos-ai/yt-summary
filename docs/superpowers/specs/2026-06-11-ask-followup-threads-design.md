# Ask Follow-up Threads + Shared Chat Core

**Status:** Draft — design phase
**Date:** 2026-06-11

## Goal

"Ask my library" today answers one question and stops — a one-shot
artifact. Users want to keep going: ask a follow-up that builds on the
previous answer ("explain that part deeper"), as a real conversation
over the library.

Turn a synthesis into a **thread**: the first question plus any number
of follow-ups, each answered with the running thread as context, all
grounded in the **same fixed source set** chosen at the start.

While building this, extract the **genuinely shared chat logic** that is
currently duplicated between the per-video chat and ask — without
forcing a full unification (their delivery and grounding differ).

## Terminology

As in earlier specs: every "user" is a Netflix-style **Profile**.

## Scope of unification (decided)

The per-video chat (`services/chat.py`) and ask both build a
`[system] + history + user` message list and call an LLM. That message
construction is the real shared core and is extracted. Their **edges
stay separate**: the video chat streams (SSE, ephemeral); ask runs a
background job and persists an archived, cited artifact. We do NOT unify
delivery — that would be a forced abstraction.

## Data model

### New table `synthesis_messages`

Added to `db.SCHEMA` (idempotent `CREATE TABLE IF NOT EXISTS`). Mirrors
`chat_messages` plus a per-message status (the spinner lives per
assistant turn, not on the thread):

```sql
CREATE TABLE IF NOT EXISTS synthesis_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    synthesis_id INTEGER NOT NULL REFERENCES syntheses(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT,                 -- NULL while an assistant turn is pending
    status TEXT NOT NULL CHECK(status IN ('pending','ready','failed')),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_synthesis_messages_synthesis
    ON synthesis_messages(synthesis_id, created_at, id);
```

- `user` messages are inserted directly `ready` (no job).
- `assistant` messages start `pending` (content NULL) → job → `ready`
  (content set) or `failed` (error set).

### `syntheses` becomes the thread container

The table is unchanged structurally. `query` is the thread title;
`source_ids_json` is the fixed source set for the whole thread. The
existing `result_md` / `status` / `error` columns on the `syntheses`
row become meaningless (status now lives per message) but are left in
place — no risky `DROP COLUMN`.

### Old data: cleared, not migrated

The user chose "fresh tables, discard old data." On first boot after
this update, the existing `syntheses` rows are deleted once (so no
container rows exist without messages). This is gated by a settings
marker (`syntheses_threads_migrated=1`) so it runs exactly once and is
idempotent. No per-row backfill.

## Shared chat core

### New `app/services/chat_core.py`

A single pure function capturing the duplicated message-list build:

```python
def build_messages(
    *, system_prompt: str,
    history: list[tuple[str, str]],   # [(role, content), ...]
    user_message: str,
) -> list[dict[str, str]]:
    """[system] + history turns + the new user message. Pure; no LLM
    call. The two chat paths differ only in HOW they invoke the model
    (stream vs. job), not in how the message list is shaped."""
    messages = [{"role": "system", "content": system_prompt}]
    for role, content in history:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    return messages
```

Deliberately minimal — only the message shaping. No litellm call in the
core; that is where the paths genuinely differ (`stream=True` vs not).

### Video chat refactor (`services/chat.py`)

`stream_reply` currently builds the messages list inline. Replace that
block with a `build_messages(...)` call (converting its
`list[ChatMessage]` history to `[(m.role, m.content), ...]`). The
streaming loop and `stream=True` litellm call are untouched.

**Safety net:** the existing `tests/test_services_chat.py` and
`tests/test_routes_chat.py` MUST stay green unchanged — that proves the
extraction is behaviour-preserving. If a chat test breaks, the
extraction is wrong.

## Ask thread service & routes

### Service (`services/ask.py`)

- Keep `gather_sources` and `build_prompt` (the `_SYSTEM` + packed
  summaries). The system content for a thread is the `_SYSTEM` prompt
  plus the sources block, built once from the thread's fixed source set.
- New `run_message(db, *, message_id)`: answers one pending `assistant`
  message. Loads its thread, gathers the **already-recorded** source
  videos (from `syntheses.source_ids_json` — NOT a fresh search),
  builds `build_messages(system=_SYSTEM+sources, history=all prior ready
  turns, user=the latest user question)`, calls `_completion` (no
  stream), writes `content` + `status=ready` (or `failed`). Same
  crash-safety net as the current `run`.
- The first question creates: the synthesis container (records the
  source set via the existing retrieval), a `user` message, and a
  pending `assistant` message → job.

### Routes (`routes/ask.py`)

- `POST /ask` (first question) — creates container + first user/assistant
  messages, redirects to `/ask/{id}`.
- **New** `POST /ask/{synthesis_id}/followup` (form field `query`) —
  appends a `user` message + a pending `assistant` message to the same
  thread, spawns the job, redirects back to `/ask/{id}`. 404 for a
  foreign profile.
- `GET /ask/{synthesis_id}` and `GET /ask/{synthesis_id}/fragment` —
  render **all** thread messages. The fragment keeps polling while ANY
  assistant message is `pending`; sends `HX-Refresh` when all are done
  (mirrors the existing fixed fragment-polling).

## UI (`ask/_body.html`, `ask/show.html`)

- The thread renders as a sequence: each `user` question as a block,
  each `assistant` answer as rendered Markdown carrying the **export
  menu** (the macro from the unified-export-menu feature) so an answer
  can be copied/downloaded. Pending answers show the spinner.
- Below the thread, a **follow-up input** (`POST /ask/{id}/followup`),
  shown once no turn is pending.
- The **Sources list** (fixed source set) renders once at the thread
  level, not per turn.

## Testing strategy

House style: no live LLM/network (completions mocked); render via
TestClient; no browser in the suite.

- **chat_core.build_messages:** pure unit tests — order
  `[system]+history+user`; empty history; role pass-through.
- **Video-chat regression (critical):** existing
  `tests/test_services_chat.py` + `tests/test_routes_chat.py` stay green
  unchanged.
- **Schema/migration:** `synthesis_messages` table exists; `init_schema`
  idempotent; the one-time `syntheses` clear runs exactly once (marker).
- **synthesis_messages repo:** `append` (user→ready, assistant→pending);
  `history(synthesis_id)` ordered; `mark_ready`/`mark_failed` per
  message; a `pending` lookup.
- **Ask thread service:** `run_message` with stubbed `_completion` →
  the user question + prior thread turns appear in the messages; answer
  becomes `ready`; the fixed source set is reused across turns (NOT
  re-searched); crash → `failed`.
- **Routes:** `POST /ask/{id}/followup` appends user+pending-assistant
  and redirects; `/ask/{id}` renders all turns + the follow-up field;
  the fragment polls while any turn is pending and `HX-Refresh`es when
  all ready; foreign profile → 404.

## Out of scope

- Live token streaming for ask (stays a background job).
- Re-searching the library on follow-ups (fixed source set per thread).
- Full unification of the two chat subsystems' delivery paths.
- Editing/deleting individual turns.

## Rollout

Single PR. One new table (via the existing SCHEMA mechanism) + one
settings-gated one-time clear. The chat_core extraction is a pure
refactor guarded by the existing chat tests. No changes to the
per-video chat's delivery.
