# Speaker Chat — PR 4: Embeddings, Backfill & Discovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out the "Chat with Speakers" epic — make the dossier *smart* and *self-populating*. Add claim embeddings + embedding-ranked retrieval (behind PR 3's existing `retrieve_for_prompt` signature, with a recency/topic fallback); the library-wide **backfill** triggered when a speaker is activated (over **confirmed** sources only); **source discovery** that proposes `speaker_source_candidates` the user must confirm or dismiss; and the **track-record peek** beside the video persona chat plus the candidate list on the speaker page.

**Architecture:**
- **Embeddings** mirror the existing summary-embedding stack exactly: a sqlite-vec `vec0` virtual table (`speaker_claim_embeddings`, `FLOAT[384]`, keyed by `claim_id`) created in `db.SCHEMA` the same way `video_embeddings` is; vectors packed with `struct.pack` and KNN-searched with `… MATCH ? AND k = ? ORDER BY distance` (the `app/repos/embeddings.py` + `app/services/related.py` pattern). The embedder is the local one already shipped (`app/services/embeddings_local.embed_text`, 384-d unit-length, warm in tests via the existing `_warmup_local_embedder` session fixture). Claim embedding on insert is **best-effort**, identical posture to `pipeline._try_embed_summary` (try / log / never raise).
- **Backfill job — DECISION (A): a dedicated `speaker_jobs` table + a tiny async runner.** The existing `jobs` table is **video-centric** (`video_id NOT NULL REFERENCES videos(id)`, no job-kind column — verified in `app/repos/jobs.py`), so a speaker backfill (which has *no* single video) cannot reuse it without abusing the FK. The two candidates were:
  - **(A)** a separate `speaker_jobs(id, speaker_id, state, step, created_at, updated_at, error_message)` table + a small `claim_next`/`set_step`/`complete`/`fail` runner, mirroring `repos/jobs.py` and `repos/tts_jobs.py`.
  - **(B)** a detached `asyncio.create_task(...)` coroutine writing progress to a new `speakers.backfill_state` column — no new table.

  **We choose (A).** The spec is explicit that activation "enqueue[s] a **backfill job** (existing job infrastructure)" that "[r]uns as a background job over the existing job infrastructure — non-blocking, like the pipeline jobs" (spec §"Architecture: activation drives extraction" and §`speaker_backfill.py`). (A) gives the same **durability** (a row survives restart and can be re-driven, as `jobs.reset_orphaned_running` does), **queue visibility** (the diagnostics page can count/list it later), and **testability** (drive one step synchronously in a test, no event-loop task juggling) that the rest of the app's job machinery already has. **Trade-off:** (A) costs one extra table + ~60 lines of runner that parallel `repos/jobs.py`; (B) costs only a column but is fire-and-forget — progress is lost on restart and there is no queue to inspect. We accept (A)'s small schema cost to stay consistent with `jobs`/`tts_jobs`. **Either way, the backfill reads CONFIRMED `source_speakers` only** (existing links + show-match hits over existing YouTube videos, which it *first writes as* `source_speakers` rows); it **never** reads `speaker_source_candidates`.
- **Discovery is strictly separate from backfill** (`services/speaker_discovery.py`) precisely so a weak signal can never auto-populate the dossier. It only ever writes `speaker_source_candidates` rows with `state='pending'`; promotion to a confirmed `source_speakers` link is an explicit route action.
- **Retrieval stays behind PR 3's signature.** `speaker_claims.retrieve_for_prompt(db, speaker_id, *, query, limit=12)` keeps its exact shape; PR 4 swaps its body to KNN-rank by `embed_text(query)` against `speaker_claim_embeddings`, with PR 3's recency/topic path as the fallback when a claim has no embedding or the embedder is unavailable.

**Tech Stack:** Python 3.12, aiosqlite, sqlite-vec (`vec0`), `sentence-transformers` (local 384-d embedder), FastAPI + HTMX fragment swaps, Jinja2 templates, pytest + pytest-asyncio. House test style: in-memory SQLite + sqlite-vec via the `db`/`config` fixtures, completions mocked (no live LLM / network), TestClient for routes, no browser.

## Global Constraints

- Python ≥ 3.12; `StrEnum` for enums and `@dataclass` for records (matches `app/models.py`).
- Every repo function takes `db: aiosqlite.Connection` as the first positional arg and acts as `user_id=1` by default (matches `app/repos/chat.py`, `app/repos/speakers.py`).
- **Idempotent:** new tables are `CREATE … IF NOT EXISTS` in `db.SCHEMA` (run on every boot); the `speaker_claim_embeddings` `vec0` table follows the exact create pattern of `video_embeddings`. Re-running `init_schema` twice must be clean.
- **Commit after every green test** (failing test → run-to-fail → minimal impl → run-to-pass → commit).
- **Ownership → `HTTPException(404)`** on every route (foreign profile is indistinguishable from "not found"), exactly like `app/routes/chat.py`.
- **NO live LLM / network in tests.** Mock `extract_claims_for_source` and any `litellm` call. Embeddings run locally and are warm via the session-scoped `_warmup_local_embedder` fixture; a test that needs the embedder absent monkeypatches it.
- **Best-effort never breaks the caller:** claim-embedding writes and discovery signals log-and-continue on failure (the `pipeline._try_embed_summary` / `_store_related_links` posture).
- **Nothing fuzzy auto-links into the dossier.** `source_speakers` only ever gains rows via show-rule (PR 1/2), explicit manual link (PR 2), or explicit candidate confirm (this PR). Discovery writes candidates, never links.
- Source of truth: [`docs/superpowers/specs/2026-06-21-chat-with-speakers-v1_5-design.md`](../specs/2026-06-21-chat-with-speakers-v1_5-design.md).

---

## File Structure

- `app/db.py` — **modify**: add the `speaker_claim_embeddings` `vec0` virtual table to `SCHEMA` (next to `video_embeddings`, line ~166) and to the `_migrate_v7_embedding_dim` DROP+CREATE block (line ~728) so a dimension change rebuilds it too; add the `speaker_jobs` table to `SCHEMA`.
- `app/models.py` — **modify**: add `SpeakerJob` + `SpeakerJobState` and `SpeakerSourceCandidate` dataclasses (the `SpeakerClaim` dataclass is produced by PR 3; do not redefine it).
- `app/repos/speaker_claim_embeddings.py` — **create**: pack/upsert/delete/KNN-search for claim vectors (mirrors `app/repos/embeddings.py`).
- `app/repos/speaker_jobs.py` — **create**: `enqueue`/`claim_next`/`get`/`latest_for_speaker`/`set_step`/`complete`/`fail`/`reset_orphaned_running` (mirrors `app/repos/jobs.py`).
- `app/repos/speaker_source_candidates.py` — **create**: insert-pending / list-pending / get / set-state for candidates.
- `app/services/speaker_claims.py` — **modify** (created by PR 3): on claim insert in `extract_claims_for_source`, embed the claim text best-effort into `speaker_claim_embeddings`; swap `retrieve_for_prompt`'s body to embedding-ranked KNN with the PR 3 fallback.
- `app/services/speaker_backfill.py` — **create**: `run_backfill(db, speaker_id, *, model, api_key, base_url)` over confirmed sources; `enqueue_backfill`/`run_pending_backfills` runner glue.
- `app/services/speaker_discovery.py` — **create**: `discover_candidates(db, speaker_id) -> list[int]` generating `pending` candidates by per-kind signal.
- `app/services/speakers.py` — **modify** (created by PR 2): `activate` now calls `speaker_backfill.enqueue_backfill`.
- `app/routes/speakers.py` — **modify** (created by PR 2): add `GET /speaker/{id}/candidates`, `POST /speaker/{id}/candidates/{cid}/confirm`, `POST /speaker/{id}/candidates/{cid}/dismiss`; the `activate` route already enqueues via the service change.
- `app/templates/speaker.html` — **modify** (created by PR 2): add the visually-distinct "Possible sources" candidate list with confirm/dismiss actions.
- `app/templates/video_detail.html` — **modify**: add the collapsible "What {Name} has said before" track-record peek beside the persona chat.
- `app/main.py` — **modify**: register `speaker_jobs.reset_orphaned_running` at startup alongside the existing `jobs.reset_orphaned_running`; drive the speaker-job runner from the existing scheduler loop.
- `app/scheduler.py` — **modify**: drain pending speaker-backfill jobs each tick (mirrors how the video-job worker drains `jobs`).
- Tests — **create**: `tests/test_repos_speaker_claim_embeddings.py`, `tests/test_speaker_claim_retrieval.py`, `tests/test_repos_speaker_jobs.py`, `tests/test_speaker_backfill.py`, `tests/test_speaker_discovery.py`, `tests/test_routes_speaker_candidates.py`, `tests/test_routes_speaker_peek.py`.

> **Dependency note (read before starting).** This PR EXTENDS files created by PR 2 and PR 3 that do not exist on `main` yet: `app/services/speaker_claims.py` (PR 3 — `extract_claims_for_source`, `retrieve_for_prompt`), `app/services/speakers.py` + `app/routes/speakers.py` + `app/templates/speaker.html` (PR 2 — `activate`/`deactivate`, the speaker page, the `source_speakers` repo), and the `speakers`/`source_speakers`/`speaker_claims`/`speaker_source_candidates` tables (PR 1). The **Consumes** block of each task lists the exact upstream signatures relied on. **Before finalizing, re-read PR 2's and PR 3's `## Interfaces this PR PRODUCES` blocks and reconcile any signature drift** — they may have been written in parallel with this plan.

---

## Interfaces this PR PRODUCES

```python
# app/models.py
class SpeakerJobState(StrEnum):
    PENDING = "pending"; RUNNING = "running"; DONE = "done"; FAILED = "failed"

@dataclass
class SpeakerJob:
    id: int
    speaker_id: int
    state: SpeakerJobState
    step: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

@dataclass
class SpeakerSourceCandidate:
    id: int
    user_id: int
    speaker_id: int
    source_id: str
    signal: str          # 'email_from' | 'title_match' | 'fulltext' | 'embedding'
    score: float | None
    state: str           # 'pending' | 'confirmed' | 'dismissed'
    created_at: datetime

# app/repos/speaker_claim_embeddings.py
async def upsert_claim_embedding(db, claim_id: int, vector: list[float]) -> None: ...
async def delete_claim_embedding(db, claim_id: int) -> None: ...
async def delete_for_source(db, speaker_id: int, source_id: str) -> None: ...   # drop a source's claim vectors on reprocess
async def search_claim_vectors(db, speaker_id: int, vector: list[float], *, limit: int = 12) -> list[tuple[int, float]]: ...   # (claim_id, distance), this speaker only, closest-first

# app/repos/speaker_jobs.py  (mirrors app/repos/jobs.py)
async def enqueue(db, speaker_id: int) -> int: ...
async def claim_next(db) -> SpeakerJob | None: ...
async def get(db, job_id: int) -> SpeakerJob | None: ...
async def latest_for_speaker(db, speaker_id: int) -> SpeakerJob | None: ...
async def set_step(db, job_id: int, step: str) -> None: ...
async def complete(db, job_id: int) -> None: ...
async def fail(db, job_id: int, message: str) -> None: ...
async def reset_orphaned_running(db) -> None: ...

# app/repos/speaker_source_candidates.py
async def upsert_pending(db, *, user_id: int = 1, speaker_id: int, source_id: str, signal: str, score: float | None) -> int: ...
async def list_for_speaker(db, speaker_id: int, *, state: str = "pending") -> list[SpeakerSourceCandidate]: ...
async def get(db, candidate_id: int) -> SpeakerSourceCandidate | None: ...
async def set_state(db, candidate_id: int, state: str) -> None: ...

# app/services/speaker_discovery.py
async def discover_candidates(db, speaker_id: int) -> list[int]: ...   # returns inserted/updated candidate ids; writes ONLY pending candidates

# app/services/speaker_backfill.py
async def enqueue_backfill(db, speaker_id: int) -> int: ...            # called by speakers.activate
async def run_backfill(db, speaker_id: int, *, model: str, api_key: str, base_url: str | None) -> int: ...   # confirmed sources only; returns #sources processed
async def run_pending_backfills(db, *, model: str, api_key: str, base_url: str | None, limit: int = 1) -> int: ...   # scheduler glue

# app/services/speaker_claims.py  (retrieval signature UNCHANGED from PR 3; body becomes embedding-ranked)
# RETURN TYPE STAYS list[dict] — exactly PR 3's contract. speaker_chat's prompt
# builder, the route layer, the persona-turn tests, and PR 4's own track-record
# peek all consume the SAME fixed-key dicts PR 3 builds via _claim_to_prompt_dict
# (keys: claim, topic, evidence_text, evidence_start_s, source_id, source_title,
# attribution_method, attribution_confidence, review_status).
# Do NOT switch to list[SpeakerClaim] — that would break every consumer.
async def retrieve_for_prompt(db, speaker_id: int, *, query: str, limit: int = 12) -> list[dict]: ...
```

---

### Task 1: `speaker_claim_embeddings` vec0 table

Mirror `video_embeddings` exactly. Keyed by `claim_id` (INTEGER, since `speaker_claims.id` is an autoincrement int), one `FLOAT[384]` column. Add it to `SCHEMA` *and* to the `_migrate_v7_embedding_dim` DROP+CREATE block so a future dimension change rebuilds it consistently.

**Files:**
- Modify: `app/db.py` — `SCHEMA` (next to `video_embeddings`, ~line 166) and `_migrate_v7_embedding_dim` (~line 728).
- Test: `tests/test_repos_speaker_claim_embeddings.py`

**Interfaces:**
- Consumes: `EMBEDDING_DIM == 384` from `app/services/embeddings_local.py`; the sqlite-vec extension already loaded in `db.connect`.
- Produces: the `speaker_claim_embeddings` virtual table.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_repos_speaker_claim_embeddings.py
import asyncio


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_speaker_claim_embeddings_table_exists(db):
    async def go():
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE name='speaker_claim_embeddings'"
        )
        assert await cur.fetchone() is not None
        # vec0 INSERT round-trip with a 384-d vector must not raise.
        import struct
        vec = [0.0] * 384
        blob = struct.pack("384f", *vec)
        await db.execute(
            "INSERT INTO speaker_claim_embeddings (claim_id, claim_vec) VALUES (?, ?)",
            (1, blob),
        )
        await db.commit()
    _run(go())
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_repos_speaker_claim_embeddings.py::test_speaker_claim_embeddings_table_exists -v`
Expected: FAIL — `no such table: speaker_claim_embeddings` (or the INSERT errors).

- [ ] **Step 3: Add the vec0 DDL**

In `app/db.py`, immediately after the `video_embeddings` `CREATE VIRTUAL TABLE` in `SCHEMA`:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS speaker_claim_embeddings USING vec0(
    claim_id INTEGER PRIMARY KEY,
    claim_vec FLOAT[384]
);
```

In `_migrate_v7_embedding_dim`, after the existing `DROP TABLE IF EXISTS video_embeddings` / recreate, add the same rebuild so a dimension migration keeps the two vec tables aligned:

```python
    await conn.execute("DROP TABLE IF EXISTS speaker_claim_embeddings")
    await conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS speaker_claim_embeddings USING vec0(
            claim_id INTEGER PRIMARY KEY,
            claim_vec FLOAT[384]
        )
        """
    )
```

> Use the same `FLOAT[384]` literal as `video_embeddings`; if the live `video_embeddings` dimension differs at implementation time, match *it* (the embedder is the single source of `EMBEDDING_DIM`).

- [ ] **Step 4: Run to verify pass + idempotency**

Run: `.venv/bin/pytest tests/test_repos_speaker_claim_embeddings.py::test_speaker_claim_embeddings_table_exists -v`
Expected: PASS.

Append an idempotency check and run it:

```python
# tests/test_repos_speaker_claim_embeddings.py (append)
from app.config import Config
from app.db import connect, init_schema


def test_init_schema_twice_keeps_vec_table(tmp_path):
    cfg = Config(data_dir=tmp_path); cfg.ensure_dirs()
    async def go():
        conn = await connect(cfg)
        await init_schema(conn)
        await init_schema(conn)   # second pass must be clean
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE name='speaker_claim_embeddings'"
        )
        assert await cur.fetchone() is not None
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())
```

Run: `.venv/bin/pytest tests/test_repos_speaker_claim_embeddings.py::test_init_schema_twice_keeps_vec_table -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_repos_speaker_claim_embeddings.py
git commit -m "feat(speakers): add speaker_claim_embeddings vec0 table"
```

---

### Task 2: claim-embedding repo (pack / upsert / delete / KNN)

Mirror `app/repos/embeddings.py`: pack with `struct.pack`, delete-then-insert (vec0 has no upsert), KNN via `MATCH ? AND k = ? ORDER BY distance`. KNN is restricted to **one speaker's** claims by joining `speaker_claims` and filtering `speaker_id`.

**Files:**
- Create: `app/repos/speaker_claim_embeddings.py`
- Test: `tests/test_repos_speaker_claim_embeddings.py` (append)

**Interfaces:**
- Consumes: `speaker_claim_embeddings` (Task 1), `speaker_claims` (PR 1).
- Produces: `upsert_claim_embedding`, `delete_claim_embedding`, `delete_for_source`, `search_claim_vectors`.

- [ ] **Step 1: Write the failing test** (uses the warm local embedder)

```python
# tests/test_repos_speaker_claim_embeddings.py (append)
from app.repos import speaker_claim_embeddings as cve
from app.repos import speakers as speakers_repo
from app.services.embeddings_local import embed_text


async def _seed_claim(db, speaker_id, source_id, claim_text):
    cur = await db.execute(
        "INSERT INTO speaker_claims (user_id, speaker_id, source_id, claim, "
        "extraction_method) VALUES (1, ?, ?, ?, 'llm')",
        (speaker_id, source_id, claim_text),
    )
    await db.commit()
    return cur.lastrowid


async def _seed_video(db, vid):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title) VALUES (?,1,'youtube','',?)",
        (vid, vid),
    )
    await db.commit()


def test_search_ranks_by_semantic_distance(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Test Person")
        await _seed_video(db, "vA")
        c_ai = await _seed_claim(db, sid, "vA", "AI regulation will slow innovation")
        c_food = await _seed_claim(db, sid, "vA", "Sourdough bread needs a long ferment")
        for cid, txt in ((c_ai, "AI regulation will slow innovation"),
                         (c_food, "Sourdough bread needs a long ferment")):
            await cve.upsert_claim_embedding(db, cid, await embed_text(txt))
        q = await embed_text("what do you think about regulating artificial intelligence")
        hits = await cve.search_claim_vectors(db, sid, q, limit=2)
        assert hits, "expected KNN hits"
        assert hits[0][0] == c_ai, "the AI claim must rank ahead of the bread claim"
    _run(go())


def test_search_scopes_to_one_speaker(db):
    async def go():
        a = await speakers_repo.resolve_speaker(db, name="Alice A")
        b = await speakers_repo.resolve_speaker(db, name="Bob B")
        await _seed_video(db, "vS")
        ca = await _seed_claim(db, a, "vS", "interest rates should stay high")
        cb = await _seed_claim(db, b, "vS", "interest rates should stay high")
        v = await embed_text("monetary policy and interest rates")
        await cve.upsert_claim_embedding(db, ca, v)
        await cve.upsert_claim_embedding(db, cb, v)
        hits = await cve.search_claim_vectors(db, a, v, limit=10)
        ids = {cid for cid, _ in hits}
        assert ca in ids and cb not in ids
    _run(go())


def test_delete_for_source_drops_only_that_source(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Carol C")
        await _seed_video(db, "v1"); await _seed_video(db, "v2")
        c1 = await _seed_claim(db, sid, "v1", "claim one")
        c2 = await _seed_claim(db, sid, "v2", "claim two")
        v = await embed_text("seed")
        await cve.upsert_claim_embedding(db, c1, v)
        await cve.upsert_claim_embedding(db, c2, v)
        await cve.delete_for_source(db, sid, "v1")
        hits = await cve.search_claim_vectors(db, sid, v, limit=10)
        ids = {cid for cid, _ in hits}
        assert c1 not in ids and c2 in ids
    _run(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_repos_speaker_claim_embeddings.py -k "ranks or scopes or delete_for_source" -v`
Expected: FAIL — module `app.repos.speaker_claim_embeddings` does not exist.

- [ ] **Step 3: Implement the repo**

```python
# app/repos/speaker_claim_embeddings.py
"""Vector storage for speaker claims using sqlite-vec's vec0 table.

Analogous to app/repos/embeddings.py (summary vectors). Claim vectors
are packed float32 BLOBs keyed by speaker_claims.id. KNN search is
scoped to a single speaker by joining speaker_claims and filtering
speaker_id, so one speaker's question only ranks their own claims.
"""
import struct

import aiosqlite


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


async def upsert_claim_embedding(
    db: aiosqlite.Connection, claim_id: int, vector: list[float]
) -> None:
    # vec0 has no ON CONFLICT — delete-then-insert (same as embeddings.py).
    blob = _pack_vector(vector)
    await db.execute(
        "DELETE FROM speaker_claim_embeddings WHERE claim_id = ?", (claim_id,)
    )
    await db.execute(
        "INSERT INTO speaker_claim_embeddings (claim_id, claim_vec) VALUES (?, ?)",
        (claim_id, blob),
    )
    await db.commit()


async def delete_claim_embedding(db: aiosqlite.Connection, claim_id: int) -> None:
    await db.execute(
        "DELETE FROM speaker_claim_embeddings WHERE claim_id = ?", (claim_id,)
    )
    await db.commit()


async def delete_for_source(
    db: aiosqlite.Connection, speaker_id: int, source_id: str
) -> None:
    """Drop a (speaker, source) pair's claim vectors before reprocess.

    Mirrors the replace-on-reprocess contract of speaker_claims: the
    claim rows themselves are deleted by the extraction service; this
    keeps the vec table from accumulating orphans.
    """
    await db.execute(
        """
        DELETE FROM speaker_claim_embeddings
        WHERE claim_id IN (
            SELECT id FROM speaker_claims
            WHERE speaker_id = ? AND source_id = ?
        )
        """,
        (speaker_id, source_id),
    )
    await db.commit()


async def search_claim_vectors(
    db: aiosqlite.Connection,
    speaker_id: int,
    vector: list[float],
    *,
    limit: int = 12,
) -> list[tuple[int, float]]:
    """Return (claim_id, distance) for THIS speaker's claims, closest first.

    The vec0 KNN is global, so over-fetch and then filter to the
    speaker via a join — the same over-fetch+filter shape as
    services/related.related_video_ids.
    """
    blob = _pack_vector(vector)
    cursor = await db.execute(
        """
        SELECT e.claim_id, e.distance
        FROM speaker_claim_embeddings e
        JOIN speaker_claims c ON c.id = e.claim_id
        WHERE e.claim_vec MATCH ?
          AND k = ?
          AND c.speaker_id = ?
        ORDER BY e.distance
        """,
        (blob, max(limit * 5, 25), speaker_id),
    )
    rows = await cursor.fetchall()
    return [(r[0], r[1]) for r in rows][:limit]
```

> **vec0 KNN + extra predicates:** sqlite-vec applies `k` to the raw index scan, then SQLite filters the joined `speaker_id`. Over-fetching `k = max(limit*5, 25)` (the `related.py` ratio) makes the per-speaker slice reliable even when other speakers' claims are nearer in the global index. Trim to `limit` in Python.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_repos_speaker_claim_embeddings.py -v`
Expected: PASS (all four tests).

- [ ] **Step 5: Commit**

```bash
git add app/repos/speaker_claim_embeddings.py tests/test_repos_speaker_claim_embeddings.py
git commit -m "feat(speakers): claim-embedding repo (pack/upsert/delete/KNN per speaker)"
```

---

### Task 3: embed claims on insert (extend PR 3's extraction path)

When `extract_claims_for_source` inserts a claim, embed its text best-effort into `speaker_claim_embeddings`. On reprocess, the existing replace-on-reprocess delete must also drop the source's claim vectors.

**Files:**
- Modify: `app/services/speaker_claims.py` (PR 3).
- Test: `tests/test_speaker_claim_retrieval.py`

**Interfaces:**
- Consumes: PR 3's `extract_claims_for_source(db, source, speaker_ids, model, api_key, base_url)` and its internal "insert claim" + "delete this source's claims on reprocess" steps; `embeddings_local.embed_text`; Task 2's `upsert_claim_embedding` / `delete_for_source`.
- Produces: claims carry vectors after extraction; reprocess leaves no orphan vectors.

- [ ] **Step 1: Write the failing test** (mock the LLM, real local embedder)

```python
# tests/test_speaker_claim_retrieval.py
import asyncio

from app.repos import speakers as speakers_repo
from app.repos import speaker_claim_embeddings as cve
from app.services import speaker_claims


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


async def _video(db, vid):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title, transcript) "
        "VALUES (?,1,'youtube','',?,?)",
        (vid, vid, "some transcript"),
    )
    await db.commit()


def _fake_completion(payload_json):
    """Patch whatever PR 3 uses to call the model so it returns payload_json.

    PR 3 parses the model's text via highlight_parser._extract_json_blob,
    so returning a JSON string in the assistant message is enough.
    """
    async def _call(*a, **k):
        return payload_json
    return _call


def test_extracted_claims_get_embedded(db, monkeypatch):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Eddie E")
        await _video(db, "vE")
        payload = (
            '{"claims": [{"speaker": "Eddie E", '
            '"claim": "AI safety must come before scaling", '
            '"evidence_text": "we need safety first", '
            '"attribution_method": "explicit_name", '
            '"attribution_confidence": 0.9, "confidence": 0.8}]}'
        )
        # Adjust the patch target to PR 3's actual model-call seam.
        monkeypatch.setattr(
            speaker_claims, "_complete_claims", _fake_completion(payload), raising=False
        )
        source = await _fetch_video(db, "vE")
        await speaker_claims.extract_claims_for_source(
            db, source, [sid], model="m", api_key="", base_url=None
        )
        # The claim now has a vector — a semantically-near query finds it.
        q = await _embed("artificial intelligence safety")
        hits = await cve.search_claim_vectors(db, sid, q, limit=5)
        assert hits, "extracted claim should have been embedded"
    _run(go())


async def _fetch_video(db, vid):
    from app.repos import videos as videos_repo
    return await videos_repo.get(db, vid)


async def _embed(text):
    from app.services.embeddings_local import embed_text
    return await embed_text(text)
```

> **At implementation time:** open PR 3's `app/services/speaker_claims.py`, find (a) the function that calls the model (patch *that* in the test — the name `_complete_claims` above is a placeholder), and (b) the exact spot where a parsed claim becomes a `speaker_claims` INSERT. Embed right after the INSERT using the returned `lastrowid`.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_speaker_claim_retrieval.py::test_extracted_claims_get_embedded -v`
Expected: FAIL — claims inserted but not embedded; `search_claim_vectors` returns `[]`.

- [ ] **Step 3: Implement best-effort embedding on insert**

In `extract_claims_for_source`, at the replace-on-reprocess delete (PR 3 deletes this `(speaker_id, source_id)`'s claims), add the vector cleanup first:

```python
        from app.repos import speaker_claim_embeddings as _cve
        # Reprocess: drop this source's old claim vectors before re-inserting.
        await _cve.delete_for_source(db, speaker_id, source.id)
```

After each claim INSERT (where PR 3 has the `lastrowid`):

```python
        await _embed_claim_best_effort(db, claim_id, claim_text)
```

Add the helper (mirrors `pipeline._try_embed_summary` — try / log / never raise):

```python
import logging
log = logging.getLogger(__name__)


async def _embed_claim_best_effort(db, claim_id: int, claim_text: str) -> None:
    """Embed one claim into speaker_claim_embeddings. Never raises.

    A failure here only degrades retrieval ranking — the recency/topic
    fallback in retrieve_for_prompt still works. Same posture as
    pipeline._try_embed_summary.
    """
    from app.repos import speaker_claim_embeddings as cve
    from app.services.embeddings import embed_text
    try:
        vector = await embed_text(claim_text)
        await cve.upsert_claim_embedding(db, claim_id, vector)
    except Exception as e:  # noqa: BLE001 — best-effort, must not break extraction
        log.warning(
            "claim embedding failed for claim %s: %s: %s",
            claim_id, type(e).__name__, e,
        )
```

- [ ] **Step 4: Run to verify pass + PR 3 regression**

Run: `.venv/bin/pytest tests/test_speaker_claim_retrieval.py::test_extracted_claims_get_embedded -v`
Expected: PASS.

Run PR 3's extraction suite to prove the embedding addition didn't break extraction:

Run: `.venv/bin/pytest tests/test_speaker_claims.py -q`
Expected: PASS (PR 3's tests unchanged and green).

- [ ] **Step 5: Commit**

```bash
git add app/services/speaker_claims.py tests/test_speaker_claim_retrieval.py
git commit -m "feat(speakers): embed claims best-effort on extraction"
```

---

### Task 4: embedding-ranked `retrieve_for_prompt` (same signature, recency fallback)

Swap the body of PR 3's `retrieve_for_prompt(db, speaker_id, *, query, limit=12)` to KNN-rank by `embed_text(query)` against this speaker's claim vectors, cross-source, capped — falling back to PR 3's recency/topic path when a claim lacks an embedding or the embedder is unavailable.

**Files:**
- Modify: `app/services/speaker_claims.py` (PR 3).
- Test: `tests/test_speaker_claim_retrieval.py` (append)

**Interfaces:**
- Consumes: PR 3's `retrieve_for_prompt` signature + its existing recency/topic implementation (kept as the fallback path); Task 2's `search_claim_vectors`; the `SpeakerClaim` model + claim-row loader from PR 3.
- Produces: `retrieve_for_prompt` returns `list[dict]` (PR 3's fixed-key contract via `_claim_to_prompt_dict`) ordered semantically-closest-first when embeddings exist, recency/topic order otherwise. The internal `SpeakerClaim` rows are mapped to dicts before returning — no model objects leak to callers.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_speaker_claim_retrieval.py (append)
from app.repos import speaker_claim_embeddings as cve


async def _claim(db, sid, vid, text, topic=None):
    cur = await db.execute(
        "INSERT INTO speaker_claims (user_id, speaker_id, source_id, claim, topic, "
        "extraction_method) VALUES (1,?,?,?,?,'llm')",
        (sid, vid, text, topic),
    )
    await db.commit()
    return cur.lastrowid


def test_retrieval_prefers_semantic_over_recency(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Fred F")
        await _video(db, "vF")
        # Insert an OLD on-topic claim, then a NEWER off-topic claim.
        on_topic = await _claim(db, sid, "vF", "Bitcoin is a hedge against inflation")
        off_topic = await _claim(db, sid, "vF", "I prefer hiking on weekends")
        await cve.upsert_claim_embedding(db, on_topic, await _embed("Bitcoin is a hedge against inflation"))
        await cve.upsert_claim_embedding(db, off_topic, await _embed("I prefer hiking on weekends"))
        out = await speaker_claims.retrieve_for_prompt(
            db, sid, query="is crypto a good inflation hedge", limit=2
        )
        assert out, "expected retrieved claims"
        # retrieve_for_prompt returns list[dict] (Finding 3), not SpeakerClaim.
        assert out[0]["claim"].startswith("Bitcoin"), \
            "semantically-closest claim must outrank the more recent off-topic one"
    _run(go())


def test_retrieval_falls_back_to_recency_without_embeddings(db, monkeypatch):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Gina G")
        await _video(db, "vG")
        # No embeddings inserted at all.
        old = await _claim(db, sid, "vG", "older position")
        new = await _claim(db, sid, "vG", "newer position")
        # Force the embedder to be 'unavailable' so the fallback path runs.
        async def _boom(_text):
            raise RuntimeError("embedder offline")
        monkeypatch.setattr(speaker_claims, "_embed_query", _boom, raising=False)
        out = await speaker_claims.retrieve_for_prompt(
            db, sid, query="anything", limit=5
        )
        texts = {c["claim"] for c in out}   # list[dict] contract (Finding 3)
        assert {"older position", "newer position"} <= texts, \
            "fallback must still return claims when embeddings are absent"
    _run(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_speaker_claim_retrieval.py -k "prefers_semantic or falls_back" -v`
Expected: FAIL — PR 3's body is recency-only (semantic test fails); the `_embed_query` seam doesn't exist yet (fallback test errors).

- [ ] **Step 3: Implement embedding-ranked retrieval with fallback**

Rename PR 3's existing recency/topic body to a private `_retrieve_recency(db, speaker_id, *, query, limit) -> list[SpeakerClaim]` (it returns model objects so we can de-dupe by `.id` and re-rank), and have it produce rows that still carry the `source_title` JOIN alias. `retrieve_for_prompt` ranks, then maps EVERY returned row through `_claim_to_prompt_dict` so the public return stays `list[dict]` (Finding 3 — the KNN path must not leak `SpeakerClaim` objects to `speaker_chat`/the peek). The KNN/recency rows must expose the same attributes `_claim_to_prompt_dict` reads (`claim, topic, evidence_text, evidence_start_s, source_id, source_title, attribution_method, attribution_confidence, review_status`); the `_load_claims_by_id` loader must JOIN `videos` for `source_title` just like PR 3's retrieval query.

```python
async def _embed_query(text: str) -> list[float]:
    from app.services.embeddings import embed_text
    return await embed_text(text)


async def retrieve_for_prompt(
    db, speaker_id: int, *, query: str, limit: int = 12,
) -> list[dict]:
    """Claims most relevant to `query`, embedding-ranked, cross-source, capped.

    RETURN SHAPE IS PR 3's list[dict] (fixed keys: claim, topic, source_id,
    source_title, ts_seconds, attribution_method, attribution_confidence) — the
    body changes (KNN instead of recency) but the contract does NOT. Reuse PR 3's
    `_claim_to_prompt_dict` to build each entry so the keys never drift.

    Tries KNN over speaker_claim_embeddings (this speaker only); falls
    back to the recency/topic path when the embedder is unavailable or
    no claim is embedded yet. Best-effort: any embedding error degrades
    to the fallback rather than raising.
    """
    from app.repos import speaker_claim_embeddings as cve
    try:
        qvec = await _embed_query(query)
        hits = await cve.search_claim_vectors(db, speaker_id, qvec, limit=limit)
    except Exception as e:  # noqa: BLE001 — degrade to recency, never raise
        log.warning("claim KNN unavailable (%s: %s); using recency fallback",
                    type(e).__name__, e)
        hits = []
    if not hits:
        # Fallback already returns SpeakerClaim objects → map to the dict contract.
        recency = await _retrieve_recency(db, speaker_id, query=query, limit=limit)
        return [_claim_to_prompt_dict(c) for c in recency]
    claim_ids = [cid for cid, _ in hits]
    by_id = await _load_claims_by_id(db, claim_ids)   # PR 3 loader; preserve hit order
    ranked = [by_id[cid] for cid in claim_ids if cid in by_id]
    if len(ranked) < limit:
        # Top up from recency for claims that have no vector yet, de-duped.
        seen = {c.id for c in ranked}
        for c in await _retrieve_recency(db, speaker_id, query=query, limit=limit):
            if c.id not in seen:
                ranked.append(c)
                if len(ranked) >= limit:
                    break
    # Public contract is list[dict] — map every row, never leak SpeakerClaim.
    return [_claim_to_prompt_dict(c) for c in ranked[:limit]]
```

> `_claim_to_prompt_dict` and `_retrieve_recency` both live in
> `app/services/speaker_claims.py` (PR 3). `_load_claims_by_id` and
> `_claim_to_prompt_dict` BOTH need `source_title` on the row, so the loader's
> `SELECT` must `JOIN videos v ON v.id = c.source_id` and alias `v.title AS
> source_title` — identical to PR 3's retrieval query. Preserve KNN order by
> iterating `claim_ids` (the `related.py` order-preservation pattern). If PR 3
> has no `_load_claims_by_id`, add it with that JOIN, returning `{id: SpeakerClaim}`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_speaker_claim_retrieval.py -v`
Expected: PASS (semantic-beats-recency + fallback).

- [ ] **Step 5: Commit**

```bash
git add app/services/speaker_claims.py tests/test_speaker_claim_retrieval.py
git commit -m "feat(speakers): embedding-ranked claim retrieval with recency fallback"
```

---

### Task 5: `speaker_jobs` table + repo (Decision A)

A dedicated speaker-scoped job table + a runner that mirrors `app/repos/jobs.py` (minus the video FK). This is the durable backfill queue the Architecture decision selected.

**Files:**
- Modify: `app/db.py` — add `speaker_jobs` to `SCHEMA`.
- Modify: `app/models.py` — add `SpeakerJobState`, `SpeakerJob`.
- Create: `app/repos/speaker_jobs.py`
- Test: `tests/test_repos_speaker_jobs.py`

**Interfaces:**
- Consumes: `speakers` table (PR 1).
- Produces: the repo functions in the PRODUCES block.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_repos_speaker_jobs.py
import asyncio

from app.models import SpeakerJobState
from app.repos import speaker_jobs as sj
from app.repos import speakers as speakers_repo


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_enqueue_and_claim_next(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Hank H")
        jid = await sj.enqueue(db, sid)
        assert jid > 0
        job = await sj.claim_next(db)
        assert job is not None
        assert job.id == jid
        assert job.state == SpeakerJobState.RUNNING
        assert await sj.claim_next(db) is None   # queue now empty
    _run(go())


def test_set_step_complete_fail(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Ivy I")
        jid = await sj.enqueue(db, sid)
        await sj.set_step(db, jid, "extracting source 1/3")
        got = await sj.get(db, jid)
        assert got.step == "extracting source 1/3"
        await sj.complete(db, jid)
        assert (await sj.get(db, jid)).state == SpeakerJobState.DONE
        jid2 = await sj.enqueue(db, sid)
        await sj.fail(db, jid2, "boom")
        failed = await sj.get(db, jid2)
        assert failed.state == SpeakerJobState.FAILED and failed.error_message == "boom"
    _run(go())


def test_reset_orphaned_running(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Jo J")
        jid = await sj.enqueue(db, sid)
        await sj.claim_next(db)   # -> running
        await sj.reset_orphaned_running(db)
        assert (await sj.get(db, jid)).state == SpeakerJobState.PENDING
    _run(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_repos_speaker_jobs.py -v`
Expected: FAIL — model/table/repo absent.

- [ ] **Step 3: Add the table, models, and repo**

In `app/db.py` `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS speaker_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','running','done','failed')),
    step TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_speaker_jobs_state ON speaker_jobs(state, created_at, id);
CREATE INDEX IF NOT EXISTS idx_speaker_jobs_speaker ON speaker_jobs(speaker_id, id);
```

In `app/models.py`:

```python
class SpeakerJobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class SpeakerJob:
    id: int
    speaker_id: int
    state: SpeakerJobState
    step: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
```

`app/repos/speaker_jobs.py` (mirror `repos/jobs.py`, single-statement `claim_next`):

```python
from datetime import datetime

import aiosqlite

from app.models import SpeakerJob, SpeakerJobState


def _row(r: aiosqlite.Row) -> SpeakerJob:
    return SpeakerJob(
        id=r["id"], speaker_id=r["speaker_id"],
        state=SpeakerJobState(r["state"]), step=r["step"],
        error_message=r["error_message"],
        created_at=datetime.fromisoformat(r["created_at"]),
        updated_at=datetime.fromisoformat(r["updated_at"]),
    )


async def enqueue(db: aiosqlite.Connection, speaker_id: int) -> int:
    cur = await db.execute(
        "INSERT INTO speaker_jobs (speaker_id, state) VALUES (?, 'pending')",
        (speaker_id,),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


async def claim_next(db: aiosqlite.Connection) -> SpeakerJob | None:
    # Single statement (no manual BEGIN/COMMIT) — same safety note as jobs.claim_next.
    cur = await db.execute(
        """
        UPDATE speaker_jobs
        SET state='running', updated_at=datetime('now')
        WHERE id = (
            SELECT id FROM speaker_jobs
            WHERE state='pending'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
        )
        RETURNING *
        """
    )
    row = await cur.fetchone()
    await db.commit()
    return _row(row) if row else None


async def get(db: aiosqlite.Connection, job_id: int) -> SpeakerJob | None:
    cur = await db.execute("SELECT * FROM speaker_jobs WHERE id=?", (job_id,))
    row = await cur.fetchone()
    return _row(row) if row else None


async def latest_for_speaker(
    db: aiosqlite.Connection, speaker_id: int
) -> SpeakerJob | None:
    cur = await db.execute(
        "SELECT * FROM speaker_jobs WHERE speaker_id=? ORDER BY id DESC LIMIT 1",
        (speaker_id,),
    )
    row = await cur.fetchone()
    return _row(row) if row else None


async def set_step(db: aiosqlite.Connection, job_id: int, step: str) -> None:
    await db.execute(
        "UPDATE speaker_jobs SET step=?, updated_at=datetime('now') WHERE id=?",
        (step, job_id),
    )
    await db.commit()


async def complete(db: aiosqlite.Connection, job_id: int) -> None:
    await db.execute(
        "UPDATE speaker_jobs SET state='done', updated_at=datetime('now') WHERE id=?",
        (job_id,),
    )
    await db.commit()


async def fail(db: aiosqlite.Connection, job_id: int, message: str) -> None:
    await db.execute(
        "UPDATE speaker_jobs SET state='failed', error_message=?, "
        "updated_at=datetime('now') WHERE id=?",
        (message, job_id),
    )
    await db.commit()


async def reset_orphaned_running(db: aiosqlite.Connection) -> None:
    await db.execute(
        "UPDATE speaker_jobs SET state='pending', updated_at=datetime('now') "
        "WHERE state='running'"
    )
    await db.commit()
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_repos_speaker_jobs.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db.py app/models.py app/repos/speaker_jobs.py tests/test_repos_speaker_jobs.py
git commit -m "feat(speakers): speaker_jobs table + repo (backfill queue, decision A)"
```

---

### Task 6: `speaker_source_candidates` repo

CRUD for discovered candidates. The table itself ships in PR 1; this PR adds the repo + model. Candidates are never promoted here — only state transitions.

**Files:**
- Modify: `app/models.py` — add `SpeakerSourceCandidate`.
- Create: `app/repos/speaker_source_candidates.py`
- Test: `tests/test_speaker_discovery.py` (shared file; candidate-repo cases first)

**Interfaces:**
- Consumes: `speaker_source_candidates` table (PR 1), `speakers`/`videos`.
- Produces: `upsert_pending`, `list_for_speaker`, `get`, `set_state`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_speaker_discovery.py
import asyncio

from app.repos import speaker_source_candidates as cand
from app.repos import speakers as speakers_repo


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


async def _video(db, vid, kind="web", title="t", url="u"):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title) VALUES (?,1,?,?,?)",
        (vid, kind, url, title),
    )
    await db.commit()


def test_upsert_pending_is_idempotent(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Kim K")
        await _video(db, "v1")
        a = await cand.upsert_pending(db, speaker_id=sid, source_id="v1",
                                      signal="title_match", score=0.4)
        b = await cand.upsert_pending(db, speaker_id=sid, source_id="v1",
                                      signal="title_match", score=0.6)
        assert a == b   # UNIQUE(speaker_id, source_id) — one row, score updated
        rows = await cand.list_for_speaker(db, sid)
        assert len(rows) == 1 and rows[0].state == "pending"
    _run(go())


def test_set_state(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Lou L")
        await _video(db, "v2")
        cid = await cand.upsert_pending(db, speaker_id=sid, source_id="v2",
                                        signal="email_from", score=0.9)
        await cand.set_state(db, cid, "dismissed")
        assert (await cand.get(db, cid)).state == "dismissed"
        assert await cand.list_for_speaker(db, sid, state="pending") == []
    _run(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_speaker_discovery.py -k "upsert_pending or set_state" -v`
Expected: FAIL — module/model absent.

- [ ] **Step 3: Implement model + repo**

`app/models.py`:

```python
@dataclass
class SpeakerSourceCandidate:
    id: int
    user_id: int
    speaker_id: int
    source_id: str
    signal: str
    score: float | None
    state: str
    created_at: datetime
```

`app/repos/speaker_source_candidates.py`:

```python
from datetime import datetime

import aiosqlite

from app.models import SpeakerSourceCandidate


def _row(r: aiosqlite.Row) -> SpeakerSourceCandidate:
    return SpeakerSourceCandidate(
        id=r["id"], user_id=r["user_id"], speaker_id=r["speaker_id"],
        source_id=r["source_id"], signal=r["signal"], score=r["score"],
        state=r["state"], created_at=datetime.fromisoformat(r["created_at"]),
    )


async def upsert_pending(
    db: aiosqlite.Connection, *, user_id: int = 1, speaker_id: int,
    source_id: str, signal: str, score: float | None,
) -> int:
    """Insert a pending candidate, or update its signal/score if one
    already exists for (speaker_id, source_id). NEVER touches
    source_speakers — a candidate is a suggestion only."""
    cur = await db.execute(
        "SELECT id FROM speaker_source_candidates WHERE speaker_id=? AND source_id=?",
        (speaker_id, source_id),
    )
    row = await cur.fetchone()
    if row is not None:
        await db.execute(
            "UPDATE speaker_source_candidates SET signal=?, score=? WHERE id=?",
            (signal, score, row["id"]),
        )
        await db.commit()
        return row["id"]
    cur = await db.execute(
        "INSERT INTO speaker_source_candidates "
        "(user_id, speaker_id, source_id, signal, score, state) "
        "VALUES (?,?,?,?,?, 'pending')",
        (user_id, speaker_id, source_id, signal, score),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


async def list_for_speaker(
    db: aiosqlite.Connection, speaker_id: int, *, state: str = "pending",
) -> list[SpeakerSourceCandidate]:
    cur = await db.execute(
        "SELECT * FROM speaker_source_candidates WHERE speaker_id=? AND state=? "
        "ORDER BY score DESC NULLS LAST, id ASC",
        (speaker_id, state),
    )
    return [_row(r) for r in await cur.fetchall()]


async def get(
    db: aiosqlite.Connection, candidate_id: int
) -> SpeakerSourceCandidate | None:
    cur = await db.execute(
        "SELECT * FROM speaker_source_candidates WHERE id=?", (candidate_id,)
    )
    row = await cur.fetchone()
    return _row(row) if row else None


async def set_state(db: aiosqlite.Connection, candidate_id: int, state: str) -> None:
    await db.execute(
        "UPDATE speaker_source_candidates SET state=? WHERE id=?",
        (state, candidate_id),
    )
    await db.commit()
```

> `ORDER BY … NULLS LAST` is supported by the SQLite version sqlite-vec ships against; if the target build rejects it, use `ORDER BY score IS NULL, score DESC, id ASC`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_speaker_discovery.py -k "upsert_pending or set_state" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models.py app/repos/speaker_source_candidates.py tests/test_speaker_discovery.py
git commit -m "feat(speakers): speaker_source_candidates repo (suggestions only)"
```

---

### Task 7: `speaker_discovery.discover_candidates` (per-kind signals, pending only)

Generate `pending` candidates for a speaker by per-kind signal — `email_from` strong, `title_match`/`fulltext`/`embedding` weak. **Writes only candidates; never `source_speakers`.** YouTube is excluded (it is handled by show-match → confirmed links, not candidates).

**Files:**
- Create: `app/services/speaker_discovery.py`
- Test: `tests/test_speaker_discovery.py` (append)

**Interfaces:**
- Consumes: `videos` (kind, title, description; the newsletter-sender signal reads the `email`-kind row's stored sender — use `videos.url`/`description` per how PR 1/email ingest stores the from-address; if a dedicated column exists, read it), Task 6's `upsert_pending`, Task 2's `search_claim_vectors` (optional embedding signal), the speaker's `name`/`name_key` from `speakers_repo.get_speaker`.
- Produces: `discover_candidates(db, speaker_id) -> list[int]`; nothing promoted to `source_speakers`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_speaker_discovery.py (append)
from app.services import speaker_discovery


def test_title_match_creates_pending_candidate_not_link(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Morgan Housel")
        await _video(db, "wA", kind="web",
                     title="An interview with Morgan Housel on risk", url="http://x/a")
        ids = await speaker_discovery.discover_candidates(db, sid)
        assert ids, "title match should yield a candidate"
        rows = await cand.list_for_speaker(db, sid)
        assert any(r.source_id == "wA" and r.signal in {"title_match", "fulltext"}
                   for r in rows)
        # CRITICAL: nothing was auto-linked into the dossier.
        link = await db.execute(
            "SELECT COUNT(*) FROM source_speakers WHERE speaker_id=?", (sid,)
        )
        assert (await link.fetchone())[0] == 0
    _run(go())


def test_email_from_is_a_strong_signal(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Packy McCormick")
        # An email-kind library item whose sender is this speaker.
        await _video(db, "eA", kind="email", title="Not Boring: weekly",
                     url="mailto:packy@notboring.co")
        ids = await speaker_discovery.discover_candidates(db, sid)
        rows = await cand.list_for_speaker(db, sid)
        em = [r for r in rows if r.signal == "email_from"]
        assert em, "newsletter sender should produce an email_from candidate"
        # Strong signal ⇒ higher score than a weak title hit.
        assert (em[0].score or 0) >= 0.7
    _run(go())


def test_discovery_never_touches_youtube(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Lex Fridman")
        await _video(db, "yA", kind="youtube", title="Lex Fridman Podcast #1",
                     url="http://y/yA")
        await speaker_discovery.discover_candidates(db, sid)
        rows = await cand.list_for_speaker(db, sid)
        assert all(r.source_id != "yA" for r in rows), \
            "youtube is handled by show-match, not discovery candidates"
    _run(go())
```

> **At implementation time:** confirm how an `email`-kind row stores its from-address (column vs. `url`/`description`). The test above assumes the sender is discoverable from the email row; adjust the assertion and the implementation read to the real storage. The *contract* under test — strong `email_from`, weak `title_match`, no YouTube, no auto-link — is what must hold.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_speaker_discovery.py -k "title_match or email_from or never_touches_youtube" -v`
Expected: FAIL — service absent.

- [ ] **Step 3: Implement discovery**

```python
# app/services/speaker_discovery.py
"""Suggest possible sources for a speaker as CANDIDATES the user confirms.

Kept strictly separate from speaker_backfill: a weak signal here can
never auto-populate the dossier. Every row written is a
speaker_source_candidates row with state='pending'. This module NEVER
writes source_speakers. YouTube is excluded — show_match handles it as
a confirmed link.
"""
import logging

import aiosqlite

from app.repos import speaker_source_candidates as cand
from app.repos import speakers as speakers_repo

log = logging.getLogger(__name__)

# Signal strengths (also the candidate score). email_from is the only
# reasonably trustworthy signal; the rest are weak and false-positive-prone.
_SCORE = {"email_from": 0.85, "title_match": 0.4, "fulltext": 0.3, "embedding": 0.5}


async def discover_candidates(db: aiosqlite.Connection, speaker_id: int) -> list[int]:
    speaker = await speakers_repo.get_speaker(db, speaker_id)
    if speaker is None:
        return []
    name = speaker.name
    name_lower = name.lower()
    out: list[int] = []

    # email_from: an email-kind item whose sender matches the speaker name.
    cur = await db.execute(
        "SELECT id, title, description, url FROM videos "
        "WHERE user_id=? AND kind='email'",
        (speaker.user_id,),
    )
    for r in await cur.fetchall():
        sender_blob = f"{r['url'] or ''} {r['description'] or ''}".lower()
        if name_lower and name_lower in sender_blob:
            out.append(await cand.upsert_pending(
                db, user_id=speaker.user_id, speaker_id=speaker_id,
                source_id=r["id"], signal="email_from", score=_SCORE["email_from"],
            ))

    # title_match / fulltext: web + text items mentioning the name. Weak.
    cur = await db.execute(
        "SELECT id, title, description FROM videos "
        "WHERE user_id=? AND kind IN ('web','text')",
        (speaker.user_id,),
    )
    for r in await cur.fetchall():
        title = (r["title"] or "").lower()
        body = (r["description"] or "").lower()
        if name_lower and name_lower in title:
            out.append(await cand.upsert_pending(
                db, user_id=speaker.user_id, speaker_id=speaker_id,
                source_id=r["id"], signal="title_match", score=_SCORE["title_match"],
            ))
        elif name_lower and name_lower in body:
            out.append(await cand.upsert_pending(
                db, user_id=speaker.user_id, speaker_id=speaker_id,
                source_id=r["id"], signal="fulltext", score=_SCORE["fulltext"],
            ))
    return out
```

> The `embedding` signal (nearest non-YouTube items to the speaker's existing claim centroid) is an optional enhancement; the three required signals (`email_from`, `title_match`, `fulltext`) cover the spec's "weakest-last" set. If added later, score it `_SCORE["embedding"]` and write it as a candidate like the rest — never a link.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_speaker_discovery.py -v`
Expected: PASS (repo + discovery cases).

- [ ] **Step 5: Commit**

```bash
git add app/services/speaker_discovery.py tests/test_speaker_discovery.py
git commit -m "feat(speakers): source discovery -> pending candidates (never auto-link)"
```

---

### Task 8: `speaker_backfill` over CONFIRMED sources + activation wiring

The activation-triggered job. It (1) confirms show-match hits over existing YouTube videos as `source_speakers` rows, (2) takes the union with existing `source_speakers`, (3) calls `extract_claims_for_source` per confirmed source. It **never** reads `speaker_source_candidates`. `speakers.activate` enqueues it.

**Files:**
- Create: `app/services/speaker_backfill.py`
- Modify: `app/services/speakers.py` (PR 2) — `activate` calls `enqueue_backfill`.
- Test: `tests/test_speaker_backfill.py`

**Interfaces:**
- Consumes: PR 1 `source_speakers` repo (list confirmed sources for a speaker; create a `show_rule`/`manual` link), PR 1 `show_match.identify_from_metadata`, PR 3 `extract_claims_for_source`, Task 5 `speaker_jobs`, PR 2 `speakers.activate` / `set_active`, `videos_repo`.
- Produces: `enqueue_backfill`, `run_backfill`, `run_pending_backfills`.

- [ ] **Step 1: Write the failing tests** (mock extraction; assert one call per confirmed source, and candidates ignored)

```python
# tests/test_speaker_backfill.py
import asyncio

from app.repos import speakers as speakers_repo
from app.repos import speaker_source_candidates as cand
from app.services import speaker_backfill


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


async def _video(db, vid, kind="youtube"):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title, transcript) "
        "VALUES (?,1,?,?,?,?)",
        (vid, kind, vid, vid, "transcript"),
    )
    await db.commit()


async def _link(db, sid, vid):
    await db.execute(
        "INSERT INTO source_speakers (source_id, speaker_id, detection_source) "
        "VALUES (?,?, 'manual')",
        (vid, sid),
    )
    await db.commit()


def test_backfill_extracts_each_confirmed_source(db, monkeypatch):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Nate N")
        await _video(db, "c1"); await _video(db, "c2")
        await _link(db, sid, "c1"); await _link(db, sid, "c2")

        calls = []
        async def fake_extract(db_, source, speaker_ids, **kw):
            calls.append(source.id)
            return []
        monkeypatch.setattr(
            "app.services.speaker_backfill.extract_claims_for_source", fake_extract
        )
        n = await speaker_backfill.run_backfill(
            db, sid, model="m", api_key="", base_url=None
        )
        assert n == 2
        assert set(calls) == {"c1", "c2"}
    _run(go())


def test_backfill_ignores_candidates(db, monkeypatch):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Olive O")
        await _video(db, "confirmed"); await _video(db, "guess", kind="web")
        await _link(db, sid, "confirmed")
        # A pending candidate must NOT be extracted by the backfill.
        await cand.upsert_pending(db, speaker_id=sid, source_id="guess",
                                  signal="title_match", score=0.4)

        calls = []
        async def fake_extract(db_, source, speaker_ids, **kw):
            calls.append(source.id); return []
        monkeypatch.setattr(
            "app.services.speaker_backfill.extract_claims_for_source", fake_extract
        )
        await speaker_backfill.run_backfill(db, sid, model="m", api_key="", base_url=None)
        assert calls == ["confirmed"], "backfill must ignore candidate sources"
    _run(go())


def test_activate_enqueues_backfill(db, monkeypatch):
    async def go():
        from app.services import speakers as speakers_svc
        from app.repos import speaker_jobs as sj
        sid = await speakers_repo.resolve_speaker(db, name="Pam P")
        await speakers_svc.activate(db, sid)   # PR 2 entry point
        job = await sj.latest_for_speaker(db, sid)
        assert job is not None and job.state.value in {"pending", "running"}
        sp = await speakers_repo.get_speaker(db, sid)
        assert sp.is_active is True
    _run(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_speaker_backfill.py -v`
Expected: FAIL — `speaker_backfill` absent; `activate` doesn't enqueue yet.

- [ ] **Step 3: Implement the backfill + wire activation**

```python
# app/services/speaker_backfill.py
"""Activation-triggered, library-wide claim backfill for one speaker.

Reads CONFIRMED sources only — the union of existing source_speakers
links and show-match hits over existing YouTube videos (which it first
writes as source_speakers rows). It NEVER reads
speaker_source_candidates: unconfirmed guesses are out of the dossier
by construction (spec rule #3, "attribution beats style").

Runs as a durable speaker_jobs job (decision A) so progress survives a
restart and is inspectable, mirroring the pipeline job posture.
"""
import logging

import aiosqlite

from app.repos import source_speakers as source_speakers_repo  # PR 1/2
from app.repos import speaker_jobs as jobs_repo
from app.repos import speakers as speakers_repo
from app.repos import videos as videos_repo
from app.services import show_match
from app.services.speaker_claims import extract_claims_for_source

log = logging.getLogger(__name__)


async def enqueue_backfill(db: aiosqlite.Connection, speaker_id: int) -> int:
    return await jobs_repo.enqueue(db, speaker_id)


async def _confirmed_source_ids(db: aiosqlite.Connection, speaker_id: int) -> list[str]:
    speaker = await speakers_repo.get_speaker(db, speaker_id)
    if speaker is None:
        return []
    # 1) Show-match over existing YouTube videos -> CONFIRM as source_speakers.
    cur = await db.execute(
        "SELECT id FROM videos WHERE user_id=? AND kind='youtube'",
        (speaker.user_id,),
    )
    for r in await cur.fetchall():
        video = await videos_repo.get(db, r["id"])
        if video is None:
            continue
        detected = await show_match.identify_from_metadata(db, video)
        if any(_same_person(d.name, speaker.name) for d in detected):
            # Confirmed link (show rule). Idempotent via source_speakers UNIQUE.
            await source_speakers_repo.link(
                db, source_id=video.id, speaker_id=speaker_id,
                detection_source="show_rule",
            )
    # 2) Union with all existing confirmed links.
    rows = await source_speakers_repo.list_source_ids_for_speaker(db, speaker_id)
    return list(dict.fromkeys(rows))   # de-dupe, preserve order


def _same_person(a: str, b: str) -> bool:
    from app.repos.speakers import normalize_name_key
    return normalize_name_key(a) == normalize_name_key(b)


async def run_backfill(
    db: aiosqlite.Connection, speaker_id: int, *,
    model: str, api_key: str, base_url: str | None,
) -> int:
    """Extract claims for every CONFIRMED source. Returns #sources processed."""
    source_ids = await _confirmed_source_ids(db, speaker_id)
    processed = 0
    for sid in source_ids:
        source = await videos_repo.get(db, sid)
        if source is None:
            continue
        try:
            await extract_claims_for_source(
                db, source, [speaker_id],
                model=model, api_key=api_key, base_url=base_url,
            )
        except Exception as e:  # noqa: BLE001 — one bad source must not abort the rest
            log.warning("backfill extract failed for %s: %s: %s",
                        sid, type(e).__name__, e)
        processed += 1
    return processed


async def run_pending_backfills(
    db: aiosqlite.Connection, *,
    model: str, api_key: str, base_url: str | None, limit: int = 1,
) -> int:
    """Drain up to `limit` pending speaker_jobs (scheduler glue)."""
    done = 0
    for _ in range(limit):
        job = await jobs_repo.claim_next(db)
        if job is None:
            break
        try:
            await jobs_repo.set_step(db, job.id, "extracting claims")
            await run_backfill(db, job.speaker_id,
                               model=model, api_key=api_key, base_url=base_url)
            await jobs_repo.complete(db, job.id)
        except Exception as e:  # noqa: BLE001
            await jobs_repo.fail(db, job.id, f"{type(e).__name__}: {e}")
        done += 1
    return done
```

In `app/services/speakers.py` (PR 2's `activate`), after `set_active(..., True)`:

```python
    from app.services import speaker_backfill
    await speaker_backfill.enqueue_backfill(db, speaker_id)
```

> **Consumes-reconciliation:** this task assumes PR 1/2 expose `source_speakers.link(db, *, source_id, speaker_id, detection_source)` and `source_speakers.list_source_ids_for_speaker(db, speaker_id)`. If PR 1/2 named these differently (e.g. `add_appearance` / `sources_for_speaker`), adjust the imports/calls here to match — the behaviour (idempotent confirmed link + list confirmed source ids) is the contract.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_speaker_backfill.py -v`
Expected: PASS (per-source extraction; candidates ignored; activate enqueues).

- [ ] **Step 5: Commit**

```bash
git add app/services/speaker_backfill.py app/services/speakers.py tests/test_speaker_backfill.py
git commit -m "feat(speakers): activation backfill over confirmed sources (ignores candidates)"
```

---

### Task 9: drive the backfill queue (startup reset + scheduler tick)

Make the durable queue actually run: reset orphaned `running` speaker-jobs at startup (like `jobs.reset_orphaned_running`) and drain pending speaker-jobs from the existing scheduler loop with the default model's creds.

**Files:**
- Modify: `app/main.py` — call `speaker_jobs.reset_orphaned_running` at startup.
- Modify: `app/scheduler.py` — call `speaker_backfill.run_pending_backfills` each tick.
- Test: `tests/test_speaker_backfill.py` (append a runner test)

**Interfaces:**
- Consumes: Task 5 `speaker_jobs.reset_orphaned_running`, Task 8 `run_pending_backfills`, `llm_models_repo.get_default` (for creds).
- Produces: an enqueued backfill is drained on the next scheduler tick.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_speaker_backfill.py (append)
def test_run_pending_backfills_drains_queue(db, monkeypatch):
    async def go():
        from app.repos import speaker_jobs as sj
        sid = await speakers_repo.resolve_speaker(db, name="Quinn Q")
        await _video(db, "qv"); await _link(db, sid, "qv")
        await sj.enqueue(db, sid)

        async def fake_extract(db_, source, speaker_ids, **kw):
            return []
        monkeypatch.setattr(
            "app.services.speaker_backfill.extract_claims_for_source", fake_extract
        )
        n = await speaker_backfill.run_pending_backfills(
            db, model="m", api_key="", base_url=None, limit=5
        )
        assert n == 1
        job = await sj.latest_for_speaker(db, sid)
        assert job.state.value == "done"
    _run(go())
```

- [ ] **Step 2: Run to verify it fails (or is unwired)**

Run: `.venv/bin/pytest tests/test_speaker_backfill.py::test_run_pending_backfills_drains_queue -v`
Expected: PASS for the service call if Task 8 is in (this test exercises `run_pending_backfills` directly). If it fails, fix the runner. The *wiring* (startup + scheduler) is verified by inspection below since the loop is time-driven.

- [ ] **Step 3: Wire startup + scheduler**

In `app/main.py`, next to the existing `jobs.reset_orphaned_running` startup call:

```python
    from app.repos import speaker_jobs as speaker_jobs_repo
    await speaker_jobs_repo.reset_orphaned_running(app.state.db)
```

In `app/scheduler.py`'s tick (where video jobs are drained / `embeddings_service.embed_text` is called around line 162), add a best-effort drain using the default model's creds:

```python
    try:
        model_row = await llm_models_repo.get_default(db)
        if model_row is not None:
            from app.services import speaker_backfill
            await speaker_backfill.run_pending_backfills(
                db,
                model=model_row.model,
                api_key=model_row.api_key or "",
                base_url=model_row.base_url or None,
                limit=1,
            )
    except Exception as e:  # noqa: BLE001 — backfill must not break the scheduler
        log.warning("speaker backfill tick failed: %s: %s", type(e).__name__, e)
```

> Match the scheduler's existing per-tick structure and its `db`/logger handles; `limit=1` keeps one speaker's backfill per tick so a prolific speaker doesn't monopolize the loop (spec "Backfill batch size" risk).

- [ ] **Step 4: Run to verify pass + scheduler import sanity**

Run: `.venv/bin/pytest tests/test_speaker_backfill.py -q`
Expected: PASS.

Run a smoke import to ensure the scheduler/main edits are syntactically wired:

Run: `.venv/bin/python -c "import app.main, app.scheduler"`
Expected: no ImportError.

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/scheduler.py tests/test_speaker_backfill.py
git commit -m "feat(speakers): drive backfill queue from startup reset + scheduler tick"
```

---

### Task 10: candidate routes — confirm (promote) / dismiss / list

`GET /speaker/{id}/candidates`, `POST /speaker/{id}/candidates/{cid}/confirm` (promote to a confirmed `source_speakers` link via `detection_source='manual'`, then extractable), `POST /speaker/{id}/candidates/{cid}/dismiss`. Ownership → 404.

**Files:**
- Modify: `app/routes/speakers.py` (PR 2).
- Test: `tests/test_routes_speaker_candidates.py`

**Interfaces:**
- Consumes: PR 2 router + `get_db`/`get_current_user_id` deps + speaker-ownership check; Task 6 candidate repo; PR 1/2 `source_speakers.link`; PR 3 `extract_claims_for_source` (optional re-extract on confirm); `speakers_repo.get_speaker`.
- Produces: the three candidate routes returning HTMX fragments; confirm creates a confirmed link; dismiss sets `dismissed`.

- [ ] **Step 1: Write the failing tests** (TestClient; mock extraction)

```python
# tests/test_routes_speaker_candidates.py
import asyncio

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(config, monkeypatch):
    from app.main import create_app
    app = create_app(config)
    with TestClient(app) as client:
        yield client


def _seed(db_coro_fn):
    asyncio.get_event_loop().run_until_complete(db_coro_fn())


def test_confirm_promotes_candidate_to_link(app_client):
    app = app_client.app
    db = app.state.db
    from app.repos import speakers as speakers_repo
    from app.repos import speaker_source_candidates as cand

    async def setup():
        sid = await speakers_repo.resolve_speaker(db, name="Rita R")
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) "
            "VALUES ('wR',1,'web','u','Rita R interview')"
        )
        cid = await cand.upsert_pending(db, speaker_id=sid, source_id="wR",
                                        signal="title_match", score=0.4)
        await db.commit()
        return sid, cid
    sid, cid = asyncio.get_event_loop().run_until_complete(setup())

    r = app_client.post(f"/speaker/{sid}/candidates/{cid}/confirm")
    assert r.status_code == 200

    async def check():
        cur = await db.execute(
            "SELECT detection_source FROM source_speakers "
            "WHERE speaker_id=? AND source_id='wR'", (sid,)
        )
        row = await cur.fetchone()
        assert row is not None and row["detection_source"] == "manual"
        from app.repos import speaker_source_candidates as c2
        assert (await c2.get(db, cid)).state == "confirmed"
    asyncio.get_event_loop().run_until_complete(check())


def test_dismiss_sets_state(app_client):
    db = app_client.app.state.db
    from app.repos import speakers as speakers_repo
    from app.repos import speaker_source_candidates as cand

    async def setup():
        sid = await speakers_repo.resolve_speaker(db, name="Sam S")
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) VALUES ('wS',1,'web','u','t')"
        )
        cid = await cand.upsert_pending(db, speaker_id=sid, source_id="wS",
                                        signal="fulltext", score=0.3)
        await db.commit()
        return sid, cid
    sid, cid = asyncio.get_event_loop().run_until_complete(setup())

    r = app_client.post(f"/speaker/{sid}/candidates/{cid}/dismiss")
    assert r.status_code == 200

    async def check():
        from app.repos import speaker_source_candidates as c2
        assert (await c2.get(db, cid)).state == "dismissed"
    asyncio.get_event_loop().run_until_complete(check())


def test_candidates_foreign_profile_404(app_client):
    db = app_client.app.state.db
    from app.repos import speakers as speakers_repo

    async def setup():
        # Speaker owned by user 2; default request acts as user 1.
        return await speakers_repo.resolve_speaker(db, user_id=2, name="Tom T")
    sid = asyncio.get_event_loop().run_until_complete(setup())

    assert app_client.get(f"/speaker/{sid}/candidates").status_code == 404
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_routes_speaker_candidates.py -v`
Expected: FAIL — routes not defined.

- [ ] **Step 3: Implement the routes** (reuse PR 2's ownership helper)

```python
# app/routes/speakers.py (append; reuse PR 2 imports + ownership helper)
from app.repos import speaker_source_candidates as candidates_repo
from app.repos import source_speakers as source_speakers_repo
from app.repos import speakers as speakers_repo


async def _owned_speaker_or_404(db, speaker_id: int, user_id: int):
    sp = await speakers_repo.get_speaker(db, speaker_id)
    if sp is None or sp.user_id != user_id:
        raise HTTPException(404, "Speaker not found")
    return sp


@router.get("/speaker/{speaker_id}/candidates", response_class=HTMLResponse)
async def get_candidates(
    speaker_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    await _owned_speaker_or_404(db, speaker_id, current_user_id)
    cands = await candidates_repo.list_for_speaker(db, speaker_id, state="pending")
    return HTMLResponse(_render_candidates_fragment(speaker_id, cands))


@router.post("/speaker/{speaker_id}/candidates/{cid}/confirm", response_class=HTMLResponse)
async def confirm_candidate(
    speaker_id: int, cid: int,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    await _owned_speaker_or_404(db, speaker_id, current_user_id)
    cand = await candidates_repo.get(db, cid)
    if cand is None or cand.speaker_id != speaker_id:
        raise HTTPException(404, "Candidate not found")
    # Promote to a CONFIRMED source_speakers link (manual), then mark confirmed.
    await source_speakers_repo.link(
        db, source_id=cand.source_id, speaker_id=speaker_id,
        detection_source="manual",
    )
    await candidates_repo.set_state(db, cid, "confirmed")
    cands = await candidates_repo.list_for_speaker(db, speaker_id, state="pending")
    return HTMLResponse(_render_candidates_fragment(speaker_id, cands))


@router.post("/speaker/{speaker_id}/candidates/{cid}/dismiss", response_class=HTMLResponse)
async def dismiss_candidate(
    speaker_id: int, cid: int,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    await _owned_speaker_or_404(db, speaker_id, current_user_id)
    cand = await candidates_repo.get(db, cid)
    if cand is None or cand.speaker_id != speaker_id:
        raise HTTPException(404, "Candidate not found")
    await candidates_repo.set_state(db, cid, "dismissed")
    cands = await candidates_repo.list_for_speaker(db, speaker_id, state="pending")
    return HTMLResponse(_render_candidates_fragment(speaker_id, cands))


def _render_candidates_fragment(speaker_id: int, cands) -> str:
    if not cands:
        return '<div class="speaker-candidates" data-empty="1">No possible sources right now.</div>'
    rows = []
    for c in cands:
        rows.append(
            f'<li class="candidate" data-cid="{c.id}">'
            f'<span class="candidate-signal">{escape(c.signal)}</span> '
            f'<span class="candidate-source">{escape(c.source_id)}</span>'
            f'<button hx-post="/speaker/{speaker_id}/candidates/{c.id}/confirm" '
            f'hx-target="#speaker-candidates" hx-swap="outerHTML">Confirm</button>'
            f'<button hx-post="/speaker/{speaker_id}/candidates/{c.id}/dismiss" '
            f'hx-target="#speaker-candidates" hx-swap="outerHTML">Dismiss</button>'
            f'</li>'
        )
    return (
        '<div class="speaker-candidates" id="speaker-candidates">'
        '<h3>Possible sources</h3>'
        '<p class="hint">Suggested by discovery — confirm to add to the dossier.</p>'
        f'<ul>{"".join(rows)}</ul></div>'
    )
```

> Confirm intentionally does **not** auto-run extraction here (keeps the route fast and the test free of LLM mocks); the user can hit the existing `POST /speaker/{id}/sources/{source_id}/extract` (PR 3), or the next activation backfill will pick up the now-confirmed link. If PR 3 wants confirm to auto-extract, call `extract_claims_for_source` best-effort after `link` — guard it behind a default-model lookup and a try/except so the route never 500s without an LLM.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_routes_speaker_candidates.py -v`
Expected: PASS (confirm promotes to `manual` link + `confirmed`; dismiss sets state; foreign profile 404).

- [ ] **Step 5: Commit**

```bash
git add app/routes/speakers.py tests/test_routes_speaker_candidates.py
git commit -m "feat(speakers): candidate confirm/dismiss/list routes (explicit promotion)"
```

---

### Task 11: track-record peek + candidate list UI

Two render surfaces: (1) the collapsible "What {Name} has said before" peek beside the video persona chat (`video_detail.html`), each line linking to source + timestamp, from the retrieval slice; (2) the visually-distinct "Possible sources" candidate block on the speaker page (`speaker.html`), never mixed with confirmed sources. Cover them with a render test.

**Files:**
- Modify: `app/templates/video_detail.html` (PR 2/3 added the persona chat section).
- Modify: `app/templates/speaker.html` (PR 2 created it).
- Modify: `app/routes/speakers.py` if a small peek-fragment endpoint is needed (otherwise render inline where the persona claims slice is already loaded).
- Test: `tests/test_routes_speaker_peek.py`

**Interfaces:**
- Consumes: PR 3's claim slice (the same `retrieve_for_prompt` output the persona prompt uses) for the peek; Task 10's `_render_candidates_fragment` (or `GET /candidates`) for the speaker page; `SpeakerClaim` fields `claim`, `source_id`, `evidence_start_s`.
- Produces: the peek renders claim lines with source+timestamp deep-links; the speaker page renders the candidate block distinctly from confirmed sources.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_routes_speaker_peek.py
import asyncio

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(config):
    from app.main import create_app
    app = create_app(config)
    with TestClient(app) as client:
        yield client


def test_speaker_page_shows_candidates_block_separately(app_client):
    db = app_client.app.state.db
    from app.repos import speakers as speakers_repo
    from app.repos import speaker_source_candidates as cand

    async def setup():
        sid = await speakers_repo.resolve_speaker(db, name="Uma U")
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) VALUES ('wU',1,'web','u','Uma U on X')"
        )
        await cand.upsert_pending(db, speaker_id=sid, source_id="wU",
                                  signal="title_match", score=0.4)
        await db.commit()
        return sid
    sid = asyncio.get_event_loop().run_until_complete(setup())

    html = app_client.get(f"/speaker/{sid}").text
    assert "Possible sources" in html
    # The candidate block must be distinct from the confirmed-sources block.
    assert 'speaker-candidates' in html


def test_track_record_peek_renders_claim_links(app_client):
    db = app_client.app.state.db
    from app.repos import speakers as speakers_repo
    from app.repos import speaker_claim_embeddings as cve

    async def setup():
        sid = await speakers_repo.resolve_speaker(db, name="Vic V")
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title, transcript) "
            "VALUES ('yV',1,'youtube','http://y/yV','Pod', 't')"
        )
        cur = await db.execute(
            "INSERT INTO speaker_claims (user_id, speaker_id, source_id, claim, "
            "evidence_start_s, extraction_method) VALUES (1,?, 'yV', "
            "'Markets are cyclical', 742, 'llm')", (sid,)
        )
        await db.commit()
        from app.services.embeddings_local import embed_text
        await cve.upsert_claim_embedding(db, cur.lastrowid, await embed_text("Markets are cyclical"))
        # Link the speaker to the video so the persona chat surface renders.
        await db.execute(
            "INSERT INTO source_speakers (source_id, speaker_id, detection_source) "
            "VALUES ('yV',?, 'manual')", (sid,)
        )
        await db.commit()
        return sid
    sid = asyncio.get_event_loop().run_until_complete(setup())

    # The video detail page hosts the persona chat + peek.
    html = app_client.get("/v/yV").text
    assert "What" in html and "said before" in html      # peek heading
    assert "Markets are cyclical" in html                 # claim line
    assert "742" in html or "12:22" in html               # timestamp deep-link
```

> **At implementation time:** confirm the real video-detail route path (PR 2/3 may mount the persona chat at `/v/{id}`); adjust the GET path and the peek-presence assertions to the actual template hooks. The contract: the peek shows the retrieved claim text + a source/timestamp link, and the speaker page shows candidates in a `speaker-candidates` block separate from confirmed sources.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_routes_speaker_peek.py -v`
Expected: FAIL — templates don't render the peek / candidate block yet.

- [ ] **Step 3: Implement the templates**

In `app/templates/speaker.html`, after the confirmed-sources/appearances block, add a **separate** candidate section (render `_render_candidates_fragment`'s markup, or include the candidate list the page route already loads):

```html
{# Possible sources — auto-discovered guesses, NEVER mixed with confirmed sources #}
<section class="speaker-candidates-wrap" aria-label="Possible sources">
  <div class="speaker-candidates" id="speaker-candidates"
       hx-get="/speaker/{{ speaker.id }}/candidates" hx-trigger="load"
       hx-swap="outerHTML">
    Loading possible sources…
  </div>
</section>
```

In `app/templates/video_detail.html`, beside the persona chat panel, add the collapsible peek fed by the claim slice the route already computed for the prompt (expose it to the template as `track_record` — a list of `{claim, source_id, evidence_start_s}`):

```html
{% if track_record %}
<details class="track-record-peek" open>
  <summary>What {{ persona_name }} has said before</summary>
  <ul>
    {% for c in track_record %}
    <li>
      <a href="/v/{{ c.source_id }}{% if c.evidence_start_s is not none %}#t={{ c.evidence_start_s }}{% endif %}">
        {{ c.claim }}
        {% if c.evidence_start_s is not none %}
          <span class="peek-ts">[{{ (c.evidence_start_s // 60) }}:{{ '%02d' % (c.evidence_start_s % 60) }}]</span>
        {% endif %}
      </a>
    </li>
    {% endfor %}
  </ul>
</details>
{% endif %}
```

In the video-detail route handler (PR 2/3), populate `track_record` from `speaker_claims.retrieve_for_prompt` (the same slice the persona prompt uses) so the peek shows exactly the evidence behind the roleplay. If the persona chat is loaded per-speaker via a fragment, compute `track_record` there and pass it to that fragment's template.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_routes_speaker_peek.py -v`
Expected: PASS (candidate block distinct; peek renders claim + timestamp link).

- [ ] **Step 5: Commit**

```bash
git add app/templates/speaker.html app/templates/video_detail.html app/routes/speakers.py tests/test_routes_speaker_peek.py
git commit -m "feat(speakers): track-record peek + distinct candidate list UI"
```

---

### Task 12: full-suite regression + epic close-out

Prove the whole epic is green and that the embedding addition didn't disturb the existing video chat or the summary-embedding stack.

**Files:** none (verification only).

- [ ] **Step 1: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS — all PR 1–4 tests plus the pre-existing suite.

- [ ] **Step 2: Prove the critical regressions explicitly**

Run: `.venv/bin/pytest tests/test_services_chat.py tests/test_routes_chat.py tests/test_repos_embeddings.py tests/test_related.py -q`
Expected: PASS unchanged — the `speaker_claim_embeddings` table and the claim-embedding path do not touch `video_embeddings`, related-links, or the video chat.

- [ ] **Step 3: Commit (if any incidental fixups were needed)**

```bash
git add -A
git commit -m "test(speakers): full-suite green for PR 4 (embeddings/backfill/discovery)"
```

---

## PR 4 done-criteria

- `.venv/bin/pytest -q` is **fully green** (PR 1–4 plus the entire pre-existing suite; video chat + summary-embedding stack unchanged).
- **Embedding-ranked retrieval beats recency for topical queries** (`test_retrieval_prefers_semantic_over_recency`) **and falls back** cleanly to recency/topic when embeddings are absent or the embedder is unavailable (`test_retrieval_falls_back_to_recency_without_embeddings`) — all behind PR 3's unchanged `retrieve_for_prompt` signature.
- **Activation triggers a backfill over CONFIRMED sources only:** `speakers.activate` enqueues a durable `speaker_jobs` row (decision A); `run_backfill` confirms show-match YouTube hits as `source_speakers`, unions existing links, calls `extract_claims_for_source` once per confirmed source, and **ignores `speaker_source_candidates`** (`test_backfill_ignores_candidates`). The queue is reset at startup and drained by the scheduler.
- **Discovery produces candidates that require explicit confirmation:** `discover_candidates` writes only `state='pending'` rows (strong `email_from`, weak `title_match`/`fulltext`, no YouTube) and **never** `source_speakers` (`test_title_match_creates_pending_candidate_not_link`); confirm promotes to a `manual` link, dismiss sets `dismissed`.
- **Track-record peek + candidate list render:** the video detail page shows "What {Name} has said before" with source+timestamp deep-links from the retrieval slice; the speaker page shows a visually-distinct "Possible sources" block, never mixed with confirmed sources.
- **Nothing fuzzy auto-links into the dossier:** the only writers of `source_speakers` remain show-rule (PR 1/2), explicit manual link (PR 2), and explicit candidate confirm (this PR). Verified by the "0 links after discovery" and "candidates ignored by backfill" assertions.
