# Ask Follow-up Threads + Shared Chat Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn an "ask my library" synthesis into a multi-turn thread (first question + follow-ups answered with the running thread as context, grounded in a fixed source set), and extract the shared message-build logic both chats duplicate.

**Architecture:** New `synthesis_messages` table (one row per turn, per-message status). `syntheses` becomes the thread container (title + fixed `source_ids_json`). A pure `chat_core.build_messages` extracts the `[system]+history+user` list both the per-video chat and ask build today. Ask answers run as background jobs polled by HTMX (mirrors the existing fixed fragment flow). The per-video chat's streaming delivery is untouched.

**Tech Stack:** FastAPI + Jinja2 + Alpine/HTMX, aiosqlite, litellm (mocked in tests), pytest + Starlette TestClient.

---

## File Structure

- **Create** `app/services/chat_core.py` — pure `build_messages(system_prompt, history, user_message)`.
- **Modify** `app/services/chat.py` — use `build_messages` instead of the inline list build (behaviour-preserving).
- **Modify** `app/db.py` — add `synthesis_messages` table + index to SCHEMA; add a settings-gated one-time clear of old `syntheses` rows.
- **Create** `app/repos/synthesis_messages.py` — append / history / mark_ready / mark_failed / first_pending.
- **Modify** `app/models.py` — add a `SynthesisMessage` dataclass.
- **Modify** `app/services/ask.py` — thread-aware: create first turn, `run_message`, keep `ask_now` working for API/MCP.
- **Modify** `app/routes/ask.py` — `POST /ask` (create thread), new `POST /ask/{id}/followup`, render all turns in show + fragment.
- **Create** `app/templates/ask/_thread.html` — renders the message sequence + sources + follow-up form (included by `_body.html`).
- **Modify** `app/templates/ask/_body.html`, `app/templates/ask/show.html` — use the thread render.
- **Test** `tests/test_services_chat_core.py`, `tests/test_repos_synthesis_messages.py`, `tests/test_db_migration_synthesis_messages.py`, `tests/test_services_ask.py` (extend), `tests/test_routes_ask.py` (extend).

---

## Task 1: Shared chat core (`build_messages`) + video-chat refactor

**Files:**
- Create: `app/services/chat_core.py`
- Create test: `tests/test_services_chat_core.py`
- Modify: `app/services/chat.py:38-43`

- [ ] **Step 1: Write the failing test**

Create `tests/test_services_chat_core.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_services_chat_core.py -q`
Expected: FAIL — module `app.services.chat_core` doesn't exist.

- [ ] **Step 3: Create the core**

Create `app/services/chat_core.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_services_chat_core.py -q`
Expected: 2 passed.

- [ ] **Step 5: Refactor the video chat to use it**

In `app/services/chat.py`, the current `stream_reply` body builds the list inline:
```python
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_TEMPLATE.format(transcript=transcript)},
    ]
    for m in history:
        messages.append({"role": m.role, "content": m.content})
    messages.append({"role": "user", "content": user_message})
```
Replace those lines with a call to the shared core (add `from app.services.chat_core import build_messages` to the imports at the top of chat.py):
```python
    messages = build_messages(
        system_prompt=SYSTEM_TEMPLATE.format(transcript=transcript),
        history=[(m.role, m.content) for m in history],
        user_message=user_message,
    )
```
Leave the `stream=True` litellm call and the streaming loop after it untouched.

- [ ] **Step 6: Prove the refactor is behaviour-preserving**

Run: `.venv/bin/python -m pytest tests/test_services_chat.py tests/test_routes_chat.py tests/test_services_chat_core.py -q`
Expected: ALL pass unchanged. If any video-chat test breaks, the extraction is wrong — fix it, don't change the tests.

- [ ] **Step 7: Lint + commit**

```bash
.venv/bin/ruff check app/services/chat_core.py app/services/chat.py tests/test_services_chat_core.py
git add app/services/chat_core.py app/services/chat.py tests/test_services_chat_core.py
git commit -m "refactor(chat): extract shared build_messages into chat_core"
```

---

## Task 2: `synthesis_messages` schema + one-time clear

**Files:**
- Modify: `app/db.py` (SCHEMA: add table+index after the `syntheses` index ~line 252; migration: add a settings-gated clear)
- Create test: `tests/test_db_migration_synthesis_messages.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_db_migration_synthesis_messages.py`:

```python
import aiosqlite

from app.db import connect, init_schema


async def _columns(conn, table):
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def test_creates_synthesis_messages_table(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        cols = await _columns(conn, "synthesis_messages")
        assert {
            "id", "synthesis_id", "role", "content",
            "status", "error", "created_at",
        } <= cols
    finally:
        await conn.close()


async def test_init_idempotent_for_synthesis_messages(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        await init_schema(conn)
        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='synthesis_messages'"
        )
        assert await cur.fetchone() is not None
    finally:
        await conn.close()


async def test_old_syntheses_rows_cleared_once(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        # Insert a legacy synthesis row directly, then re-run init_schema:
        # the one-time clear has already run (marker set), so this row
        # must SURVIVE (the clear must not run again).
        await conn.execute(
            "INSERT INTO syntheses (user_id, query, source_ids_json, status) "
            "VALUES (1, 'kept', '[]', 'ready')"
        )
        await conn.commit()
        await init_schema(conn)
        cur = await conn.execute("SELECT COUNT(*) FROM syntheses")
        assert (await cur.fetchone())[0] == 1
    finally:
        await conn.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_db_migration_synthesis_messages.py -q`
Expected: FAIL — `synthesis_messages` table absent.

- [ ] **Step 3: Add the table to SCHEMA**

In `app/db.py`, find the `syntheses` block in the `SCHEMA` string (ends with `CREATE INDEX IF NOT EXISTS idx_syntheses_user_created ON syntheses(user_id, created_at DESC);`). Immediately after that index line, add:

```sql
CREATE TABLE IF NOT EXISTS synthesis_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    synthesis_id INTEGER NOT NULL REFERENCES syntheses(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','ready','failed')),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_synthesis_messages_synthesis
    ON synthesis_messages(synthesis_id, created_at, id);
```

- [ ] **Step 4: Add the settings-gated one-time clear**

`init_schema` runs `_run_migrations(conn)` then `executescript(SCHEMA)`. The one-time clear must run AFTER the tables exist. Find where `init_schema` finishes creating tables (after the `executescript(SCHEMA)` call). Add this block there (it reads/writes the `settings` table, which SCHEMA has already created):

```python
    # Part C.2 follow-up threads: the syntheses row model changed from
    # "one query+result_md" to a thread container whose turns live in
    # synthesis_messages. Old rows have no messages, so clear them once.
    # Gated by a marker so it runs exactly once and never again.
    cur = await conn.execute(
        "SELECT value FROM settings WHERE key='syntheses_threads_migrated'"
    )
    if await cur.fetchone() is None:
        await conn.execute("DELETE FROM syntheses")
        await conn.execute(
            "INSERT INTO settings (key, value) "
            "VALUES ('syntheses_threads_migrated', '1')"
        )
        await conn.commit()
```

Read the actual end of `init_schema` first to place this correctly (after SCHEMA is applied, before the function returns). Confirm the `settings` table's column names are `key` / `value` by grepping the SCHEMA (`grep -n "CREATE TABLE IF NOT EXISTS settings" -A4 app/db.py`).

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_db_migration_synthesis_messages.py -q`
Expected: 3 passed.

- [ ] **Step 6: Regression (schema-wide)**

Run: `.venv/bin/python -m pytest tests/test_db.py tests/test_db_migration_syntheses.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/db.py tests/test_db_migration_synthesis_messages.py
git commit -m "feat(ask-threads): synthesis_messages table + one-time legacy clear"
```

---

## Task 3: `SynthesisMessage` model + repo

**Files:**
- Modify: `app/models.py` (add `SynthesisMessage` dataclass near `Synthesis`)
- Create: `app/repos/synthesis_messages.py`
- Create test: `tests/test_repos_synthesis_messages.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_repos_synthesis_messages.py`:

```python
from app.models import SynthesisStatus
from app.repos import synthesis_messages as sm_repo
from app.repos import syntheses as syntheses_repo


async def _thread(db, query="q"):
    s = await syntheses_repo.create_pending(
        db, user_id=1, query=query, source_ids=["v1"],
    )
    return s.id


async def test_append_user_is_ready(db):
    sid = await _thread(db)
    m = await sm_repo.append(
        db, synthesis_id=sid, role="user", content="hello",
        status=SynthesisStatus.READY,
    )
    assert m.role == "user"
    assert m.content == "hello"
    assert m.status == SynthesisStatus.READY


async def test_append_assistant_pending_then_ready(db):
    sid = await _thread(db)
    m = await sm_repo.append(
        db, synthesis_id=sid, role="assistant", content=None,
        status=SynthesisStatus.PENDING,
    )
    assert m.status == SynthesisStatus.PENDING
    assert m.content is None
    await sm_repo.mark_ready(db, message_id=m.id, content="the answer")
    got = await sm_repo.get(db, m.id)
    assert got.status == SynthesisStatus.READY
    assert got.content == "the answer"


async def test_mark_failed(db):
    sid = await _thread(db)
    m = await sm_repo.append(
        db, synthesis_id=sid, role="assistant", content=None,
        status=SynthesisStatus.PENDING,
    )
    await sm_repo.mark_failed(db, message_id=m.id, error="boom")
    got = await sm_repo.get(db, m.id)
    assert got.status == SynthesisStatus.FAILED
    assert got.error == "boom"


async def test_history_ordered(db):
    sid = await _thread(db)
    await sm_repo.append(db, synthesis_id=sid, role="user",
                         content="q1", status=SynthesisStatus.READY)
    await sm_repo.append(db, synthesis_id=sid, role="assistant",
                         content="a1", status=SynthesisStatus.READY)
    rows = await sm_repo.history(db, synthesis_id=sid)
    assert [(r.role, r.content) for r in rows] == [
        ("user", "q1"), ("assistant", "a1"),
    ]


async def test_first_pending_assistant(db):
    sid = await _thread(db)
    await sm_repo.append(db, synthesis_id=sid, role="user",
                         content="q", status=SynthesisStatus.READY)
    pend = await sm_repo.append(db, synthesis_id=sid, role="assistant",
                                content=None, status=SynthesisStatus.PENDING)
    found = await sm_repo.first_pending(db, synthesis_id=sid)
    assert found is not None and found.id == pend.id
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_repos_synthesis_messages.py -q`
Expected: FAIL — `app.repos.synthesis_messages` / `SynthesisMessage` missing.

- [ ] **Step 3: Add the model**

In `app/models.py`, right AFTER the `Synthesis` dataclass (which ends with `created_at: datetime`), add:

```python
@dataclass
class SynthesisMessage:
    id: int
    synthesis_id: int
    role: ChatRole  # 'user' | 'assistant'
    content: str | None
    status: SynthesisStatus
    error: str | None
    created_at: datetime
```

(`ChatRole` and `SynthesisStatus` are already defined in this module; `dataclass` and `datetime` are already imported.)

- [ ] **Step 4: Create the repo**

Create `app/repos/synthesis_messages.py`:

```python
"""CRUD for synthesis_messages — the turns of an ask-my-library thread.

Mirrors repos/chat.py plus a per-message status: user turns are inserted
'ready'; assistant turns start 'pending' (content NULL) and a background
job marks them 'ready' (with content) or 'failed'.
"""
from datetime import datetime

import aiosqlite

from app.models import ChatRole, SynthesisMessage, SynthesisStatus


def _row(r: aiosqlite.Row) -> SynthesisMessage:
    return SynthesisMessage(
        id=r["id"],
        synthesis_id=r["synthesis_id"],
        role=r["role"],
        content=r["content"],
        status=SynthesisStatus(r["status"]),
        error=r["error"],
        created_at=datetime.fromisoformat(r["created_at"]),
    )


async def append(
    db: aiosqlite.Connection, *, synthesis_id: int, role: ChatRole,
    content: str | None, status: SynthesisStatus,
) -> SynthesisMessage:
    cur = await db.execute(
        "INSERT INTO synthesis_messages "
        "(synthesis_id, role, content, status) VALUES (?, ?, ?, ?)",
        (synthesis_id, role, content, status.value),
    )
    await db.commit()
    assert cur.lastrowid is not None
    got = await get(db, cur.lastrowid)
    assert got is not None
    return got


async def get(
    db: aiosqlite.Connection, message_id: int,
) -> SynthesisMessage | None:
    cur = await db.execute(
        "SELECT * FROM synthesis_messages WHERE id=?", (message_id,)
    )
    row = await cur.fetchone()
    return _row(row) if row else None


async def history(
    db: aiosqlite.Connection, *, synthesis_id: int,
) -> list[SynthesisMessage]:
    cur = await db.execute(
        "SELECT * FROM synthesis_messages WHERE synthesis_id=? "
        "ORDER BY created_at ASC, id ASC",
        (synthesis_id,),
    )
    return [_row(r) for r in await cur.fetchall()]


async def mark_ready(
    db: aiosqlite.Connection, *, message_id: int, content: str,
) -> None:
    await db.execute(
        "UPDATE synthesis_messages SET status='ready', content=?, error=NULL "
        "WHERE id=?",
        (content, message_id),
    )
    await db.commit()


async def mark_failed(
    db: aiosqlite.Connection, *, message_id: int, error: str,
) -> None:
    await db.execute(
        "UPDATE synthesis_messages SET status='failed', error=? WHERE id=?",
        (error, message_id),
    )
    await db.commit()


async def first_pending(
    db: aiosqlite.Connection, *, synthesis_id: int,
) -> SynthesisMessage | None:
    cur = await db.execute(
        "SELECT * FROM synthesis_messages "
        "WHERE synthesis_id=? AND role='assistant' AND status='pending' "
        "ORDER BY created_at ASC, id ASC LIMIT 1",
        (synthesis_id,),
    )
    row = await cur.fetchone()
    return _row(row) if row else None
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_repos_synthesis_messages.py -q`
Expected: 5 passed.

- [ ] **Step 6: Lint + commit**

```bash
.venv/bin/ruff check app/repos/synthesis_messages.py tests/test_repos_synthesis_messages.py
git add app/models.py app/repos/synthesis_messages.py tests/test_repos_synthesis_messages.py
git commit -m "feat(ask-threads): SynthesisMessage model + repo"
```

---

## Task 4: Thread-aware ask service

**Files:**
- Modify: `app/services/ask.py` (add `start_thread`, `run_message`; keep `ask_now` working)
- Modify test: `tests/test_services_ask.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_services_ask.py`:

```python
async def test_start_thread_records_sources_and_pending_assistant(db, monkeypatch):
    await _default_model(db)
    await _seed_video(db, "1:a", title="Agent Eval", summary="agent eval golden")
    from app.repos import synthesis_messages as sm_repo
    from app.repos import syntheses as syntheses_repo

    s_id, assistant_id = await ask_svc.start_thread(
        db, user_id=1, query="agent eval",
    )
    s = await syntheses_repo.get(db, s_id)
    import json
    assert "1:a" in json.loads(s.source_ids_json)
    msgs = await sm_repo.history(db, synthesis_id=s_id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].content == "agent eval"
    assert msgs[1].status.value == "pending"
    assert msgs[1].id == assistant_id


async def test_run_message_answers_pending_with_thread_context(db, monkeypatch):
    await _default_model(db)
    await _seed_video(db, "1:a", title="Agent Eval", summary="agent eval golden")
    from app.repos import synthesis_messages as sm_repo

    s_id, assistant_id = await ask_svc.start_thread(db, user_id=1, query="agent eval")

    captured = {}
    async def fake_completion(*, system, messages, model, api_key, base_url):
        captured["system"] = system
        captured["messages"] = messages
        return "Answer [Agent Eval](/v/1:a)."
    monkeypatch.setattr(ask_svc, "_completion_messages", fake_completion)

    await ask_svc.run_message(db, message_id=assistant_id)
    done = await sm_repo.get(db, assistant_id)
    assert done.status.value == "ready"
    assert "[Agent Eval](/v/1:a)" in done.content
    # The user question is the final message handed to the model.
    assert captured["messages"][-1] == {"role": "user", "content": "agent eval"}


async def test_followup_reuses_fixed_sources_not_research(db, monkeypatch):
    await _default_model(db)
    await _seed_video(db, "1:a", title="Agent Eval", summary="agent eval golden")
    await _seed_video(db, "1:b", title="Bread", summary="how to bake bread")
    from app.repos import synthesis_messages as sm_repo
    from app.repos import syntheses as syntheses_repo

    s_id, a1 = await ask_svc.start_thread(db, user_id=1, query="agent eval")

    async def fake_completion(*, system, messages, model, api_key, base_url):
        return "ok"
    monkeypatch.setattr(ask_svc, "_completion_messages", fake_completion)
    await ask_svc.run_message(db, message_id=a1)

    # Now a follow-up — it must NOT re-search; it reuses the thread's set.
    a2 = await ask_svc.add_followup(db, synthesis_id=s_id, query="cooking?")
    s_before = await syntheses_repo.get(db, s_id)
    await ask_svc.run_message(db, message_id=a2)
    s_after = await syntheses_repo.get(db, s_id)
    assert s_before.source_ids_json == s_after.source_ids_json  # unchanged


async def test_run_message_marks_failed_on_error(db, monkeypatch):
    await _default_model(db)
    await _seed_video(db, "1:a", title="X", summary="agent eval stuff")
    from app.repos import synthesis_messages as sm_repo
    s_id, a1 = await ask_svc.start_thread(db, user_id=1, query="agent eval")

    async def boom(*, system, messages, model, api_key, base_url):
        raise RuntimeError("llm down")
    monkeypatch.setattr(ask_svc, "_completion_messages", boom)
    await ask_svc.run_message(db, message_id=a1)
    done = await sm_repo.get(db, a1)
    assert done.status.value == "failed"
    assert "llm down" in done.error
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_services_ask.py -k "thread or followup or run_message" -q`
Expected: FAIL — `start_thread` / `run_message` / `add_followup` / `_completion_messages` missing.

- [ ] **Step 3: Implement the thread service**

In `app/services/ask.py`, add these. First, a messages-list completion wrapper next to the existing `_completion` (keep `_completion` as is — `ask_now` still uses `run`... actually we will route `ask_now` through the thread path; see Step 4). Add after `_completion`:

```python
async def _completion_messages(
    *, system: str, messages: list[dict], model: str, api_key: str,
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
    from app.repos import synthesis_messages as sm_repo
    sources = await gather_sources(db, query, user_id=user_id)
    s = await syntheses_repo.create_pending(
        db, user_id=user_id, query=query,
        source_ids=[v.id for v in sources],
    )
    await sm_repo.append(
        db, synthesis_id=s.id, role="user", content=query,
        status=SynthesisStatus.READY,
    )
    assistant = await sm_repo.append(
        db, synthesis_id=s.id, role="assistant", content=None,
        status=SynthesisStatus.PENDING,
    )
    return s.id, assistant.id


async def add_followup(
    db: aiosqlite.Connection, *, synthesis_id: int, query: str,
) -> int:
    """Append a user turn + a pending assistant turn to an existing
    thread. Returns the assistant message id. Does NOT re-search — the
    thread's fixed source set is reused."""
    from app.repos import synthesis_messages as sm_repo
    await sm_repo.append(
        db, synthesis_id=synthesis_id, role="user", content=query,
        status=SynthesisStatus.READY,
    )
    assistant = await sm_repo.append(
        db, synthesis_id=synthesis_id, role="assistant", content=None,
        status=SynthesisStatus.PENDING,
    )
    return assistant.id


async def run_message(db: aiosqlite.Connection, *, message_id: int) -> None:
    """Answer one pending assistant message using the thread's fixed
    source set and prior turns as context. Marks the message ready or
    failed."""
    from app.repos import synthesis_messages as sm_repo
    from app.services.chat_core import build_messages
    msg = await sm_repo.get(db, message_id)
    if msg is None:
        return
    model_row = await llm_models_repo.get_default(db)
    if model_row is None:
        await sm_repo.mark_failed(
            db, message_id=message_id, error="No default LLM configured",
        )
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
        user_turn = prior[-1].content if prior else ""
        hist = [(t.role, t.content) for t in prior[:-1]]
        messages = build_messages(
            system_prompt=system, history=hist, user_message=user_turn,
        )
        answer = await _completion_messages(
            system=system, messages=messages,
            model=model_row.model, api_key=model_row.api_key or "",
            base_url=model_row.base_url or None,
        )
        await sm_repo.mark_ready(db, message_id=message_id, content=answer)
    except Exception as e:
        log.exception("ask: message %s failed", message_id)
        await sm_repo.mark_failed(
            db, message_id=message_id, error=f"{type(e).__name__}: {e}",
        )
```

- [ ] **Step 4: Keep `ask_now` working (API/MCP)**

`ask_now` (used by `POST /api/v1/ask` and the MCP `ask_library` tool) must still return a single answer. Rewrite it to use the thread path:
```python
async def ask_now(
    db: aiosqlite.Connection, *, user_id: int, query: str,
):
    """Create a one-turn thread, answer it synchronously, return the
    synthesis row (with result available via its assistant message)."""
    from app.repos import synthesis_messages as sm_repo
    s_id, assistant_id = await start_thread(db, user_id=user_id, query=query)
    await run_message(db, message_id=assistant_id)
    s = await syntheses_repo.get(db, s_id)
    answer = await sm_repo.get(db, assistant_id)
    return s, answer
```
NOTE: this changes `ask_now`'s return type from `Synthesis` to `(Synthesis, SynthesisMessage)`. The callers in `app/routes/api.py` (`api_ask`) and `app/routes/mcp.py` (`_tool_ask_library`) read `s.result_md` / `s.status` today — they must be updated to read from the returned `answer` message instead. Update both call sites:
  - In `app/routes/api.py` `api_ask`: change `s = await ask_svc.ask_now(...)` to `s, answer = await ask_svc.ask_now(...)`, and build the response from `answer.content` / `answer.status.value` / `answer.error`, with `sources` from `json.loads(s.source_ids_json)`.
  - In `app/routes/mcp.py` `_tool_ask_library`: same — unpack `s, answer`, return `{"id": s.id, "status": answer.status.value, "answer": answer.content, "sources": json.loads(s.source_ids_json), "error": answer.error}`.
The old `run` function (single-shot) can be removed since nothing calls it anymore — verify with `grep -rn "ask_svc.run\b\|ask_service.run\b\|\.run(db" app/ tests/` and remove `run` only if it has no remaining callers (the route's `_enqueue_ask_job` will be rewritten in Task 5 to call `run_message`).

- [ ] **Step 5: Run the new + existing ask service tests**

Run: `.venv/bin/python -m pytest tests/test_services_ask.py -q`
Expected: all pass. The pre-existing single-shot tests that referenced `ask_svc.run` / `_completion` may need updating to the thread API — update them to use `start_thread`+`run_message` and `_completion_messages`. (Read the existing tests first; adapt rather than delete coverage.)

- [ ] **Step 6: API + MCP regression**

Run: `.venv/bin/python -m pytest tests/test_routes_api_ask.py tests/test_routes_mcp.py -q`
Expected: pass after the call-site updates in Step 4.

- [ ] **Step 7: Lint + commit**

```bash
.venv/bin/ruff check app/services/ask.py app/routes/api.py app/routes/mcp.py tests/test_services_ask.py
git add app/services/ask.py app/routes/api.py app/routes/mcp.py tests/test_services_ask.py
git commit -m "feat(ask-threads): thread-aware service (start_thread, run_message, followup)"
```

---

## Task 5: Routes — followup + render all turns

**Files:**
- Modify: `app/routes/ask.py`
- Modify test: `tests/test_routes_ask.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routes_ask.py`:

```python
def test_post_ask_creates_thread_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    from app.routes import ask as ask_route

    async def fake_enqueue_first(db, *, user_id, query):
        # create a thread synchronously without running the LLM
        from app.services import ask as ask_svc
        s_id, _ = await ask_svc.start_thread(db, user_id=user_id, query=query)
        return s_id
    monkeypatch.setattr(ask_route, "_enqueue_first", fake_enqueue_first)

    with TestClient(app) as client:
        import asyncio
        async def seed():
            from app.repos import llm_models as r
            from app.repos import videos as v
            await r.insert(app.state.db, label="m", provider_id="openai",
                           model="openai/gpt-4o", api_key="k", base_url="",
                           make_default=True)
            await v.upsert_metadata(app.state.db, video_id="1:a", url="u",
                                    title="T", description="d",
                                    thumbnail_path=None, duration_seconds=None)
            await v.set_summary(app.state.db, "1:a", "agent eval", "m")
        asyncio.get_event_loop().run_until_complete(seed())
        resp = client.post("/ask", data={"query": "agent eval"},
                           follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/ask/")


def test_followup_appends_turn_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    from app.routes import ask as ask_route

    async def fake_enqueue_followup(db, *, synthesis_id, query):
        from app.services import ask as ask_svc
        return await ask_svc.add_followup(db, synthesis_id=synthesis_id, query=query)
    monkeypatch.setattr(ask_route, "_enqueue_followup", fake_enqueue_followup)

    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import syntheses as syntheses_repo
            from app.repos import synthesis_messages as sm_repo
            from app.models import SynthesisStatus
            s = await syntheses_repo.create_pending(
                app.state.db, user_id=1, query="q", source_ids=[])
            await sm_repo.append(app.state.db, synthesis_id=s.id, role="user",
                                 content="q", status=SynthesisStatus.READY)
            await sm_repo.append(app.state.db, synthesis_id=s.id,
                                 role="assistant", content="a1",
                                 status=SynthesisStatus.READY)
            return s.id
        sid = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post(f"/ask/{sid}/followup",
                           data={"query": "more?"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/ask/{sid}"


def test_ask_show_renders_all_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import syntheses as syntheses_repo
            from app.repos import synthesis_messages as sm_repo
            from app.models import SynthesisStatus
            s = await syntheses_repo.create_pending(
                app.state.db, user_id=1, query="first q", source_ids=[])
            await sm_repo.append(app.state.db, synthesis_id=s.id, role="user",
                                 content="first q", status=SynthesisStatus.READY)
            await sm_repo.append(app.state.db, synthesis_id=s.id,
                                 role="assistant", content="answer one",
                                 status=SynthesisStatus.READY)
            return s.id
        sid = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/ask/{sid}")
    assert resp.status_code == 200
    assert "first q" in resp.text
    assert "answer one" in resp.text
    # a follow-up input is present once nothing is pending
    assert f"/ask/{sid}/followup" in resp.text


def test_fragment_polls_while_a_turn_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import syntheses as syntheses_repo
            from app.repos import synthesis_messages as sm_repo
            from app.models import SynthesisStatus
            s = await syntheses_repo.create_pending(
                app.state.db, user_id=1, query="q", source_ids=[])
            await sm_repo.append(app.state.db, synthesis_id=s.id, role="user",
                                 content="q", status=SynthesisStatus.READY)
            await sm_repo.append(app.state.db, synthesis_id=s.id,
                                 role="assistant", content=None,
                                 status=SynthesisStatus.PENDING)
            return s.id
        sid = asyncio.get_event_loop().run_until_complete(setup())
        frag = client.get(f"/ask/{sid}/fragment")
    assert frag.status_code == 200
    assert "site-header" not in frag.text       # fragment, no chrome
    assert f"/ask/{sid}/fragment" in frag.text  # keeps polling
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_routes_ask.py -k "thread or followup or all_turns or fragment_polls" -q`
Expected: FAIL — routes/helpers not wired yet.

- [ ] **Step 3: Rewire the routes**

In `app/routes/ask.py`:

(a) Replace `_enqueue_ask_job` with two helpers — `_enqueue_first` (start a thread, spawn `run_message` for its assistant id) and `_enqueue_followup` (append a follow-up turn, spawn `run_message`). Both follow the existing `_PENDING_JOBS` + crash-safety-net pattern but call `ask_service.run_message(db, message_id=...)` and on crash call `sm_repo.mark_failed(db, message_id=..., error=...)`:

```python
from app.repos import synthesis_messages as sm_repo

async def _spawn_answer(db, *, message_id, user_id):
    async def _run(mid):
        try:
            await ask_service.run_message(db, message_id=mid)
        except Exception as e:
            log.exception("ask job crashed for user %s", user_id)
            try:
                await sm_repo.mark_failed(db, message_id=mid, error=f"{type(e).__name__}: {e}")
            except Exception:
                log.exception("ask job: could not mark message %s failed", mid)
    task = asyncio.create_task(_run(message_id))
    _PENDING_JOBS.add(task)
    task.add_done_callback(_PENDING_JOBS.discard)


async def _enqueue_first(db, *, user_id, query) -> int:
    s_id, assistant_id = await ask_service.start_thread(db, user_id=user_id, query=query)
    await _spawn_answer(db, message_id=assistant_id, user_id=user_id)
    return s_id


async def _enqueue_followup(db, *, synthesis_id, query) -> int:
    assistant_id = await ask_service.add_followup(db, synthesis_id=synthesis_id, query=query)
    await _spawn_answer(db, message_id=assistant_id, user_id=0)
    return assistant_id
```

(b) `POST /ask` calls `_enqueue_first` and redirects to `/ask/{s_id}`.

(c) Add `POST /ask/{synthesis_id}/followup`:
```python
@router.post("/ask/{synthesis_id}/followup")
async def ask_followup(
    synthesis_id: int,
    query: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    s = await _fetch_for_user(db, synthesis_id, user_id)  # 404s foreign
    q = query.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Question is required")
    await _enqueue_followup(db, synthesis_id=s.id, query=q)
    return RedirectResponse(url=f"/ask/{s.id}", status_code=303)
```

(d) Rewrite `_body_context` to load all turns and whether any is pending:
```python
async def _body_context(db, s):
    from app.repos import synthesis_messages as sm_repo
    import json
    turns = await sm_repo.history(db, synthesis_id=s.id)
    rendered = []
    for t in turns:
        rendered.append({
            "role": t.role,
            "status": t.status.value,
            "error": t.error,
            "html": render_markdown(t.content) if t.content else "",
        })
    any_pending = any(t.status.value == "pending" for t in turns)
    ids = []
    if s.source_ids_json:
        try:
            ids = json.loads(s.source_ids_json)
        except (ValueError, TypeError):
            ids = []
    by_id = await videos_repo.get_many(db, ids)
    sources = [by_id[i] for i in ids if i in by_id]
    return {"synthesis": s, "turns": rendered, "any_pending": any_pending,
            "sources": sources}
```

(e) The fragment endpoint: poll while `any_pending`; `HX-Refresh` when not. Change the terminal condition from the old single-status check to `not ctx["any_pending"]`:
```python
@router.get("/ask/{synthesis_id}/fragment", response_class=HTMLResponse)
async def ask_fragment(request, synthesis_id, db=..., user_id=...):
    s = await _fetch_for_user(db, synthesis_id, user_id)
    ctx = await _body_context(db, s)
    is_htmx_poll = request.headers.get("HX-Request") == "true"
    if is_htmx_poll and not ctx["any_pending"]:
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return templates.TemplateResponse(request, "ask/_body.html", ctx)
```
(Keep the existing function signature/deps; only the body logic changes.)

- [ ] **Step 4: Run the route tests**

Run: `.venv/bin/python -m pytest tests/test_routes_ask.py -q`
Expected: all pass (update any pre-existing test that asserted the old single-answer shape to the new turns shape; read them first).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check app/routes/ask.py tests/test_routes_ask.py
git add app/routes/ask.py tests/test_routes_ask.py
git commit -m "feat(ask-threads): followup route + render all turns + poll-any-pending"
```

---

## Task 6: Thread UI templates

**Files:**
- Create: `app/templates/ask/_thread.html`
- Modify: `app/templates/ask/_body.html`

- [ ] **Step 1: Create the thread partial**

Create `app/templates/ask/_thread.html`:

```jinja
{% import "macros/export_menu.html" as exp %}
<div class="ask-thread">
  {% for t in turns %}
    {% if t.role == 'user' %}
      <div class="ask-turn ask-turn-user"><p>{{ t.html | safe }}</p></div>
    {% elif t.status == 'pending' %}
      <div class="ask-turn ask-turn-assistant">
        <p class="status status-running">
          <span class="spinner" aria-hidden="true"></span>
          Synthesising across your library…
        </p>
      </div>
    {% elif t.status == 'failed' %}
      <div class="ask-turn ask-turn-assistant">
        <p class="status status-failed">⚠ {{ t.error or 'Synthesis failed.' }}</p>
      </div>
    {% else %}
      <div class="ask-turn ask-turn-assistant">
        <article class="ask-result">{{ t.html | safe }}</article>
      </div>
    {% endif %}
  {% endfor %}
</div>

{% if sources %}
  <section class="ask-sources">
    <h2 class="section-heading">Sources ({{ sources | length }})</h2>
    <ul>
      {% for v in sources %}
        <li><a href="/v/{{ v.id }}">{{ v.title }}</a></li>
      {% endfor %}
    </ul>
  </section>
{% endif %}

{% if not any_pending %}
  <form method="post" action="/ask/{{ synthesis.id }}/followup" class="ask-followup">
    <input type="text" name="query" autocomplete="off"
           placeholder="Ask a follow-up…" required>
    <button type="submit">Ask</button>
  </form>
{% endif %}
```

- [ ] **Step 2: Point `_body.html` at the thread partial**

Replace the entire contents of `app/templates/ask/_body.html` with:
```jinja
{# Inner body of the ask thread page. Polled by /ask/<id>/fragment while
   any assistant turn is pending; renders all turns, the fixed sources,
   and (when idle) the follow-up form. No base layout here — the fragment
   endpoint must not nest header/main into the polling div. #}
{% if any_pending %}
  <div hx-get="/ask/{{ synthesis.id }}/fragment"
       hx-trigger="every 1s"
       hx-swap="outerHTML"
       class="ask-rendering">
    {% include "ask/_thread.html" %}
  </div>
{% else %}
  {% include "ask/_thread.html" %}
{% endif %}
```

- [ ] **Step 3: Verify the page renders end-to-end**

Run: `.venv/bin/python -m pytest tests/test_routes_ask.py -q`
Expected: all pass (the show + fragment tests from Task 5 exercise these templates).

- [ ] **Step 4: Commit**

```bash
git add app/templates/ask/_thread.html app/templates/ask/_body.html
git commit -m "feat(ask-threads): thread UI — turns, sources, follow-up form, export menu"
```

---

## Task 7: Minimal thread styling

**Files:**
- Modify: `app/static/app.css`

- [ ] **Step 1: Append styles**

Append to `app/static/app.css`:
```css
/* Ask thread (multi-turn library Q&A). */
.ask-thread { display: flex; flex-direction: column; gap: 16px; margin: 1rem 0; }
.ask-turn-user p {
  margin: 0; font-weight: 500;
  padding: 10px 14px; border-radius: var(--rounded-md);
  background: rgba(127, 127, 127, 0.10);
}
.ask-turn-assistant { padding: 0 2px; }
.ask-followup { display: flex; gap: 8px; margin-top: 1rem; }
.ask-followup input { flex: 1; }
```

- [ ] **Step 2: Smoke test**

Run: `.venv/bin/python -m pytest tests/test_routes_ask.py -q`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add app/static/app.css
git commit -m "style(ask-threads): thread + follow-up styling"
```

---

## Task 8: Manual browser verification

Not a pytest task. Verify against the running server (`:8210`).

- [ ] Ask a question at `/ask`; confirm the answer renders with a Sources list and an export menu on the answer.
- [ ] Ask a follow-up; confirm a new user turn + a spinner appear, then the answer fills in (HTMX polling), and the sources list stays the same (fixed set).
- [ ] Confirm no recursive header/main nesting (view source: one `<header>`).
- [ ] Confirm `POST /api/v1/ask` still returns `{status, answer, sources}` (curl) and the MCP `ask_library` tool still works.

Report results with curl evidence.

---

## Self-Review Notes

- **Spec coverage:** shared core → Task 1; `synthesis_messages` table + one-time clear → Task 2; model+repo → Task 3; thread service (`start_thread`/`run_message`/`add_followup`, fixed sources reused, crash→failed, `ask_now` preserved for API/MCP) → Task 4; followup route + render-all-turns + poll-any-pending → Task 5; thread UI with export menu + follow-up form + sources-once → Task 6; styling → Task 7; manual verify → Task 8.
- **Critical regression guard:** Task 1 Step 6 requires the existing video-chat tests to stay green unchanged (proves the extraction is behaviour-preserving). Task 4 Step 6 guards the API/MCP `ask_now` consumers.
- **Type/name consistency:** `build_messages(system_prompt, history: list[tuple[str,str]], user_message)`; `SynthesisMessage(id, synthesis_id, role, content, status, error, created_at)`; repo `append/get/history/mark_ready/mark_failed/first_pending`; service `start_thread -> (sid, assistant_id)`, `add_followup -> assistant_id`, `run_message(message_id)`, `_completion_messages(system, messages, model, api_key, base_url)`, `ask_now -> (Synthesis, SynthesisMessage)`. Used consistently across tasks.
- **Deviation noted:** `ask_now`'s return type changes (single-answer thread), so its two call sites (api.py, mcp.py) are updated in Task 4 Step 4 — flagged explicitly so the API/MCP contract (response JSON shape) stays identical for clients.
- **`_completion` vs `_completion_messages`:** the old single-prompt `_completion` is superseded by `_completion_messages`; the old `run` single-shot function is removed once no caller remains (verified by grep in Task 4 Step 4).
