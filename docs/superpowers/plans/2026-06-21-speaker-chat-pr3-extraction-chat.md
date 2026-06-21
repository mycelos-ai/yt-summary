# Speaker Chat — PR 3: Claim Extraction & Persona Chat — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the speakers talk. Add the **attributed claim extraction** LLM pass (`speaker_claims.extract_claims_for_source` + recency/topic retrieval), the **persona chat** for both surfaces (per-episode + whole-dossier) via `speaker_chat.stream_speaker_reply`, the **pipeline piggyback** that extracts claims for already-active speakers of a freshly processed episode in one call, the **routes** that drive both chat surfaces plus on-demand extraction and claim review, and the **UI**: a disclaimer banner in persona mode, avatar-tinted speaker bubbles, and a track-record dossier on the speaker page where `unreviewed` claims read as visibly less authoritative.

**Architecture:** Extraction is a single best-effort LLM call per source that lists the expected speakers by name and asks the model to attribute each claim to exactly one of them with evidence, a timestamp/offset, and `attribution_method`/`attribution_confidence`/`attribution_reason`; the JSON envelope is parsed with the same `highlight_parser._extract_json_blob` the summarizer/related-links paths use, and unattributable statements yield no claim. Persona chat mirrors `services/chat.stream_reply` exactly (same LiteLLM streaming kwargs, reuses `chat_core.build_messages`) — only the system prompt differs, carrying the spec's grounded in-character prompt. Retrieval in PR 3 is **recency + topic-text overlap only** (the recency fallback); the embedding-ranked path, the standalone library-wide backfill JOB, and candidate discovery are PR 4 (same `retrieve_for_prompt` signature, extended later). Routes are HTMX fragment swaps that reuse the `_msg_html` rendering shape from `routes/chat.py`, ownership-checked (foreign profile → 404). Persistence reuses `chat_threads.get_or_create` + the thread-aware `chat_repo.append`/`history` (both produced by PR 2).

**Tech Stack:** Python 3.12, aiosqlite, FastAPI + HTMX/Jinja2, litellm (mocked in tests), pytest + pytest-asyncio (`asyncio_mode = "auto"`). House test style: in-memory SQLite via the `db` fixture (`tests/conftest.py`), `TestClient` for routes, **no live LLM / no network** — every completion is mocked with the `_stream_chunks` / `AsyncMock(return_value=...)` pattern from `tests/test_services_chat.py`.

## Global Constraints

- Python ≥ 3.12; use `@dataclass` for any new record types and `StrEnum` for enums (matches `app/models.py`).
- All repo functions take `db: aiosqlite.Connection` as the first positional arg and default to `user_id=1` (matches `app/repos/chat.py`, `app/repos/settings.py`, and PR 1's `app/repos/speakers.py`).
- Migrations/tables are **idempotent**; PR 3 adds **no new tables** — all speaker tables (incl. `speaker_claims`, `chat_threads`, `chat_messages.thread_id`) already exist from PR 1. If a needed column is missing at implementation time, stop and reconcile with PR 1 rather than adding it here.
- Commit after every green test (one logical step per commit). Branch base: the PR 2 branch (or a fresh `feat/speaker-chat-pr3`).
- Routes are ownership-checked: load the owning record, and if its `user_id` (or the speaker's `user_id`) ≠ `current_user_id`, `raise HTTPException(404, ...)` — never 403, mirroring `routes/chat.py`.
- **NO live LLM / network in tests** — completions are mocked. Extraction and persona-chat tests patch `app.services.speaker_claims.litellm.acompletion` / `app.services.speaker_chat.litellm.acompletion` (and at the route layer, patch the service functions, as `tests/test_routes_chat.py` patches `app.routes.chat.stream_reply`).
- Extraction **never raises** and returns `[]` on garbage; the pipeline piggyback is best-effort and **never fails the job** (same posture as `pipeline._store_related_links`, verified).
- Source of truth: [`docs/superpowers/specs/2026-06-21-chat-with-speakers-v1_5-design.md`](../specs/2026-06-21-chat-with-speakers-v1_5-design.md) — especially the `speaker_claims.py` / `speaker_chat.py` sections, the persona prompt block, and the `speaker_claims` schema with `attribution_method`/`attribution_confidence`/`attribution_reason`.

---

## Interfaces this PR CONSUMES (must match PR 1 / PR 2 `PRODUCES` exactly)

> **PR 2 was not yet written when this plan was authored.** The signatures below are what PR 3 depends on, derived from the v1.5 spec. **Before finalizing/implementing, re-read PR 2's `Interfaces this PR PRODUCES` block and reconcile any drift** (names, arg order, return shapes). If a name differs, change PR 3's call sites — do not fork a parallel helper.

```python
# app/models.py (PR 1)
class VideoKind(StrEnum): YOUTUBE="youtube"; WEB="web"; EMAIL="email"; TEXT="text"
@dataclass
class Speaker:                      # PR 1
    id: int; user_id: int; known_speaker_id: int | None
    name: str; name_key: str; role: str | None
    avatar_id: str | None; avatar_photo_path: str | None
    style_note: str | None; is_active: bool
    created_at: datetime; updated_at: datetime

# app/repos/speakers.py (PR 1)
async def get_speaker(db, speaker_id: int) -> Speaker | None: ...
async def list_for_user(db, *, user_id: int = 1, active_only: bool = False) -> list[Speaker]: ...

# app/repos/videos.py (existing)
async def get(db, video_id: str) -> Video | None: ...      # Video has .title, .user_id, .transcript, .kind, .duration_seconds

# app/repos/chat_threads.py (PR 2) — REQUIRED
async def get_or_create(
    db, *, user_id: int = 1, scope: str,            # 'source' | 'source_speaker' | 'speaker'
    source_id: str | None = None, speaker_id: int | None = None,
) -> int: ...                                       # returns thread_id (honours the partial unique indexes)

# app/repos/chat.py (PR 2 extends — optional thread_id added; default call sites unchanged)
async def append(db, video_id, role, content, *, user_id: int = 1, thread_id: int | None = None) -> ChatMessage: ...
async def history(db, video_id: str, *, thread_id: int | None = None) -> list[ChatMessage]: ...
# When thread_id is given, history scopes to that thread; when None, behaviour is exactly today's (per-video).

# app/repos/source_speakers.py (PR 2) — REQUIRED
async def list_for_source(db, source_id: str) -> list[Speaker]: ...    # confirmed speakers linked to this source
async def get_link(db, source_id: str, speaker_id: int) -> int | None: # source_speakers.id or None

# app/routes/speakers.py (PR 2) — exists; PR 3 ADDS routes to this same router
# app/templates/speaker.html (PR 2) — exists (header + confirmed-sources list); PR 3 EXTENDS it with the dossier
# app/templates/video_detail.html chat section (existing) — PR 2 adds speaker chips; PR 3 adds banner + persona target
# app/pipeline.py (PR 2) — adds the detection step (identify_from_metadata → resolve + link source_speakers);
#   PR 3 EXTENDS that step with the piggyback extraction call.
```

If `chat_threads.get_or_create` or `source_speakers.list_for_source` is absent when PR 3 starts (PR 2 slipped), implement the minimal version inside PR 3 **in the PR-2-owned module** with the exact signature above and leave a `# TODO(pr2-merge)` note — never invent a divergent helper in a PR-3 file.

---

## Interfaces this PR PRODUCES (PR 4 depends on these exact signatures)

```python
# app/repos/speaker_claims.py
@dataclass  # app/models.py
class SpeakerClaim:
    id: int; user_id: int; speaker_id: int; source_id: str
    source_speaker_id: int | None
    claim: str; topic: str | None
    evidence_text: str | None
    evidence_start_s: int | None; evidence_end_s: int | None
    text_start_offset: int | None; text_end_offset: int | None
    confidence: float | None
    extraction_method: str          # 'metadata' | 'llm' | 'manual'
    attribution_method: str | None  # 'explicit_name'|'speaker_marker'|'metadata_context'|'llm_inferred'|'manual'
    attribution_confidence: float | None
    attribution_reason: str | None
    review_status: str              # 'unreviewed' | 'accepted' | 'rejected'
    created_at: datetime

async def insert_claim(db, *, user_id: int = 1, speaker_id: int, source_id: str,
                       source_speaker_id: int | None = None, claim: str,
                       topic: str | None = None, evidence_text: str | None = None,
                       evidence_start_s: int | None = None, evidence_end_s: int | None = None,
                       text_start_offset: int | None = None, text_end_offset: int | None = None,
                       confidence: float | None = None, extraction_method: str = "llm",
                       attribution_method: str | None = None, attribution_confidence: float | None = None,
                       attribution_reason: str | None = None) -> int: ...
async def list_for_speaker(db, speaker_id: int, *, grouped_by_topic: bool = False) -> list[SpeakerClaim] | dict[str, list[SpeakerClaim]]: ...
async def list_for_source_speakers(db, source_id: str, speaker_ids: list[int]) -> list[SpeakerClaim]: ...
async def set_review_status(db, claim_id: int, status: str) -> None: ...
async def edit_claim(db, claim_id: int, **fields) -> None: ...   # whitelist: claim, topic, evidence_text, confidence
async def replace_for_source_speakers(db, source_id: str, speaker_ids: list[int]) -> None: ...  # delete then caller re-inserts

# app/services/speaker_claims.py
async def extract_claims_for_source(db, source, speaker_ids: list[int], *,
                                    model: str, api_key: str, base_url: str | None) -> list[dict]: ...
async def retrieve_for_prompt(db, speaker_id: int, *, query: str, limit: int = 12) -> list[dict]: ...
#   PR 3: recency + topic-text overlap (no embeddings). PR 4 adds an embedding-ranked path behind THIS signature.

# app/services/speaker_chat.py
def build_speaker_system_prompt(*, speaker, claims: list[dict], source_context: str,
                                seed_ts: str | None = None, seed_quote: str | None = None) -> str: ...
async def stream_speaker_reply(*, speaker, source_context: str, claims: list[dict],
                               history, user_message: str, seed_ts: str | None = None,
                               seed_quote: str | None = None, model: str, api_key: str,
                               base_url: str | None) -> AsyncIterator[str]: ...

# app/routes/speakers.py (added to PR 2's router)
POST /v/{video_id}/speaker/{speaker_id}/chat          # per-episode persona turn (scope='source_speaker')
POST /speaker/{speaker_id}/chat                        # whole-dossier persona turn (scope='speaker')
POST /speaker/{speaker_id}/sources/{source_id}/extract # on-demand extraction for one confirmed source
POST /speaker/{speaker_id}/claims/{claim_id}/edit      # correct a claim
POST /speaker/{speaker_id}/claims/{claim_id}/review    # set accepted/rejected
```

---

## File Structure

- `app/models.py` — **modify**: add the `SpeakerClaim` dataclass.
- `app/repos/speaker_claims.py` — **create**: claim CRUD (`insert_claim`, `list_for_speaker`, `list_for_source_speakers`, `set_review_status`, `edit_claim`, `replace_for_source_speakers`).
- `app/services/speaker_claims.py` — **create**: `extract_claims_for_source` (LLM, attributed, never raises) + `retrieve_for_prompt` (recency/topic).
- `app/services/speaker_chat.py` — **create**: `build_speaker_system_prompt` + `stream_speaker_reply` (mirrors `chat.stream_reply`).
- `app/routes/speakers.py` — **modify** (PR 2 created it): add the two persona-chat routes, the on-demand extract route, and the claim edit/review routes; add a `_speaker_msg_html` fragment.
- `app/pipeline.py` — **modify**: extend the PR-2 detection step with the best-effort piggyback extraction call for active speakers.
- `app/templates/speaker.html` — **modify** (PR 2 created it): add the topic-grouped dossier (evidence / source / timestamp / confidence / review-status, `unreviewed` dimmed) + the whole-dossier chat composer.
- `app/templates/video_detail.html` — **modify**: in the chat section, add the persona disclaimer banner (shown in persona mode) and avatar-tinted speaker bubbles; point the persona composer at the per-episode route.
- `app/templates/_speaker_claims.html` — **create**: a partial rendering the topic-grouped claim list (reused by the dossier + the on-demand-extract fragment response).
- `tests/test_repos_speaker_claims.py`, `tests/test_services_speaker_claims.py`, `tests/test_services_speaker_chat.py`, `tests/test_pipeline_piggyback.py`, `tests/test_routes_speaker_chat.py` — **create**.

---

### Task 1: `SpeakerClaim` model + claim repo CRUD

**Files:**
- Modify: `app/models.py` — add the `SpeakerClaim` dataclass (place it near the other speaker dataclasses PR 1 added).
- Create: `app/repos/speaker_claims.py`
- Test: `tests/test_repos_speaker_claims.py`

**Interfaces:**
- Consumes: the `speaker_claims` table (PR 1), the `speakers` table (PR 1), `videos` (existing).
- Produces: `SpeakerClaim`, `insert_claim`, `list_for_speaker`, `list_for_source_speakers`, `set_review_status`, `edit_claim`, `replace_for_source_speakers` (signatures in the PRODUCES block).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_repos_speaker_claims.py
import asyncio

from app.repos import speaker_claims as repo


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


async def _seed_speaker_and_source(db, *, name="Chamath", sid="vid-1"):
    cur = await db.execute(
        "INSERT INTO speakers (user_id, name, name_key) VALUES (1, ?, ?)",
        (name, name.lower()),
    )
    speaker_id = cur.lastrowid
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title) "
        "VALUES (?, 1, 'youtube', 'u', 'Ep 1')",
        (sid,),
    )
    await db.commit()
    return speaker_id, sid


def test_insert_claim_defaults_unreviewed(db):
    async def go():
        speaker_id, sid = await _seed_speaker_and_source(db)
        cid = await repo.insert_claim(
            db, speaker_id=speaker_id, source_id=sid,
            claim="SPACs are mispriced", topic="markets",
            evidence_text="they're mispriced", evidence_start_s=42,
            confidence=0.8, attribution_method="explicit_name",
            attribution_confidence=0.9, attribution_reason="named in prior sentence",
        )
        rows = await repo.list_for_speaker(db, speaker_id)
        assert len(rows) == 1
        c = rows[0]
        assert c.id == cid
        assert c.claim == "SPACs are mispriced"
        assert c.topic == "markets"
        assert c.evidence_start_s == 42
        assert c.extraction_method == "llm"
        assert c.attribution_method == "explicit_name"
        assert c.attribution_confidence == 0.9
        assert c.attribution_reason == "named in prior sentence"
        assert c.review_status == "unreviewed"
    _run(go())


def test_list_for_speaker_grouped_by_topic(db):
    async def go():
        speaker_id, sid = await _seed_speaker_and_source(db)
        await repo.insert_claim(db, speaker_id=speaker_id, source_id=sid,
                                claim="A", topic="markets")
        await repo.insert_claim(db, speaker_id=speaker_id, source_id=sid,
                                claim="B", topic="markets")
        await repo.insert_claim(db, speaker_id=speaker_id, source_id=sid,
                                claim="C", topic="ai")
        grouped = await repo.list_for_speaker(db, speaker_id, grouped_by_topic=True)
        assert set(grouped.keys()) == {"markets", "ai"}
        assert len(grouped["markets"]) == 2
        assert len(grouped["ai"]) == 1
    _run(go())


def test_set_review_status_and_edit(db):
    async def go():
        speaker_id, sid = await _seed_speaker_and_source(db)
        cid = await repo.insert_claim(db, speaker_id=speaker_id, source_id=sid,
                                      claim="orig", topic="markets")
        await repo.set_review_status(db, cid, "accepted")
        await repo.edit_claim(db, cid, claim="corrected", topic="macro")
        c = (await repo.list_for_speaker(db, speaker_id))[0]
        assert c.review_status == "accepted"
        assert c.claim == "corrected"
        assert c.topic == "macro"
    _run(go())


def test_replace_for_source_speakers_clears_then_allows_reinsert(db):
    async def go():
        speaker_id, sid = await _seed_speaker_and_source(db)
        await repo.insert_claim(db, speaker_id=speaker_id, source_id=sid, claim="stale")
        await repo.replace_for_source_speakers(db, sid, [speaker_id])
        assert await repo.list_for_speaker(db, speaker_id) == []
        # other speakers / other sources are untouched
        sp2, _ = await _seed_speaker_and_source(db, name="Jason", sid="vid-2")
        await repo.insert_claim(db, speaker_id=sp2, source_id="vid-2", claim="keep")
        await repo.replace_for_source_speakers(db, sid, [speaker_id])
        assert len(await repo.list_for_speaker(db, sp2)) == 1
    _run(go())


def test_list_for_source_speakers_filters(db):
    async def go():
        sp1, sid = await _seed_speaker_and_source(db, name="A", sid="s1")
        sp2, _ = await _seed_speaker_and_source(db, name="B", sid="s2")
        await repo.insert_claim(db, speaker_id=sp1, source_id=sid, claim="x")
        await repo.insert_claim(db, speaker_id=sp2, source_id="s2", claim="y")
        out = await repo.list_for_source_speakers(db, sid, [sp1, sp2])
        assert [c.claim for c in out] == ["x"]
    _run(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_repos_speaker_claims.py -v`
Expected: FAIL — `ModuleNotFoundError: app.repos.speaker_claims` (module/functions don't exist).

- [ ] **Step 3: Add the model**

In `app/models.py` add (after PR 1's speaker dataclasses):

```python
@dataclass
class SpeakerClaim:
    id: int
    user_id: int
    speaker_id: int
    source_id: str
    source_speaker_id: int | None
    claim: str
    topic: str | None
    evidence_text: str | None
    evidence_start_s: int | None
    evidence_end_s: int | None
    text_start_offset: int | None
    text_end_offset: int | None
    confidence: float | None
    extraction_method: str
    attribution_method: str | None
    attribution_confidence: float | None
    attribution_reason: str | None
    review_status: str
    created_at: datetime
```

- [ ] **Step 4: Implement the repo**

```python
# app/repos/speaker_claims.py
from datetime import datetime

import aiosqlite

from app.models import SpeakerClaim

_DEFAULT_USER = 1
# Columns the user may correct from the review UI. Anything else is ignored.
_EDITABLE = {"claim", "topic", "evidence_text", "confidence"}


def _row(r: aiosqlite.Row) -> SpeakerClaim:
    return SpeakerClaim(
        id=r["id"], user_id=r["user_id"], speaker_id=r["speaker_id"],
        source_id=r["source_id"], source_speaker_id=r["source_speaker_id"],
        claim=r["claim"], topic=r["topic"], evidence_text=r["evidence_text"],
        evidence_start_s=r["evidence_start_s"], evidence_end_s=r["evidence_end_s"],
        text_start_offset=r["text_start_offset"], text_end_offset=r["text_end_offset"],
        confidence=r["confidence"], extraction_method=r["extraction_method"],
        attribution_method=r["attribution_method"],
        attribution_confidence=r["attribution_confidence"],
        attribution_reason=r["attribution_reason"],
        review_status=r["review_status"],
        created_at=datetime.fromisoformat(r["created_at"]),
    )


async def insert_claim(
    db: aiosqlite.Connection, *, user_id: int = _DEFAULT_USER,
    speaker_id: int, source_id: str, source_speaker_id: int | None = None,
    claim: str, topic: str | None = None, evidence_text: str | None = None,
    evidence_start_s: int | None = None, evidence_end_s: int | None = None,
    text_start_offset: int | None = None, text_end_offset: int | None = None,
    confidence: float | None = None, extraction_method: str = "llm",
    attribution_method: str | None = None, attribution_confidence: float | None = None,
    attribution_reason: str | None = None,
) -> int:
    cur = await db.execute(
        "INSERT INTO speaker_claims ("
        "user_id, speaker_id, source_id, source_speaker_id, claim, topic, "
        "evidence_text, evidence_start_s, evidence_end_s, text_start_offset, "
        "text_end_offset, confidence, extraction_method, attribution_method, "
        "attribution_confidence, attribution_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, speaker_id, source_id, source_speaker_id, claim, topic,
         evidence_text, evidence_start_s, evidence_end_s, text_start_offset,
         text_end_offset, confidence, extraction_method, attribution_method,
         attribution_confidence, attribution_reason),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


async def list_for_speaker(
    db: aiosqlite.Connection, speaker_id: int, *, grouped_by_topic: bool = False,
):
    cur = await db.execute(
        "SELECT * FROM speaker_claims WHERE speaker_id=? "
        "ORDER BY topic IS NULL, topic COLLATE NOCASE, created_at DESC, id DESC",
        (speaker_id,),
    )
    rows = [_row(r) for r in await cur.fetchall()]
    if not grouped_by_topic:
        return rows
    grouped: dict[str, list[SpeakerClaim]] = {}
    for c in rows:
        grouped.setdefault(c.topic or "Other", []).append(c)
    return grouped


async def list_for_source_speakers(
    db: aiosqlite.Connection, source_id: str, speaker_ids: list[int],
) -> list[SpeakerClaim]:
    if not speaker_ids:
        return []
    marks = ",".join("?" for _ in speaker_ids)
    cur = await db.execute(
        f"SELECT * FROM speaker_claims WHERE source_id=? AND speaker_id IN ({marks}) "
        "ORDER BY created_at DESC, id DESC",
        (source_id, *speaker_ids),
    )
    return [_row(r) for r in await cur.fetchall()]


async def set_review_status(db: aiosqlite.Connection, claim_id: int, status: str) -> None:
    if status not in ("unreviewed", "accepted", "rejected"):
        raise ValueError(f"bad review_status: {status}")
    await db.execute(
        "UPDATE speaker_claims SET review_status=? WHERE id=?", (status, claim_id)
    )
    await db.commit()


async def edit_claim(db: aiosqlite.Connection, claim_id: int, **fields) -> None:
    cols = {k: v for k, v in fields.items() if k in _EDITABLE}
    if not cols:
        return
    sets = ", ".join(f"{k}=?" for k in cols)
    await db.execute(
        f"UPDATE speaker_claims SET {sets} WHERE id=?",
        (*cols.values(), claim_id),
    )
    await db.commit()


async def replace_for_source_speakers(
    db: aiosqlite.Connection, source_id: str, speaker_ids: list[int],
) -> None:
    """Delete THIS source's claims for the given speakers (forward-only
    re-derivation: the extractor re-inserts immediately after). No-op on
    an empty speaker list."""
    if not speaker_ids:
        return
    marks = ",".join("?" for _ in speaker_ids)
    await db.execute(
        f"DELETE FROM speaker_claims WHERE source_id=? AND speaker_id IN ({marks})",
        (source_id, *speaker_ids),
    )
    await db.commit()
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/test_repos_speaker_claims.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/models.py app/repos/speaker_claims.py tests/test_repos_speaker_claims.py
git commit -m "feat(speakers): SpeakerClaim model + claim repo CRUD"
```

---

### Task 2: `speaker_claims.extract_claims_for_source` — attributed extraction (mocked LLM)

The single extraction entry point. Builds **one** prompt that lists the expected speakers by name and instructs the model to attribute each claim to exactly one of them with evidence + timestamp/offset + `attribution_method`/`attribution_confidence`/`attribution_reason`. Parses the JSON envelope with `highlight_parser._extract_json_blob` (the path related-links/highlights use). Statements not confidently attributable → no claim. Garbage → `[]`. **Never raises.** Persists via replace-on-reprocess (`replace_for_source_speakers` then `insert_claim`). Claims default `review_status='unreviewed'` (DB default).

**Long-source handling (Finding 5) — context-window-aware, NOT blind chunking.**
The grounding text is chosen by what fits the *configured model's* context window, because the risk is real only for small-context models — with Claude/modern 128k+ models even a 3-hour transcript fits, and chunking would only add cost and lose timestamp precision. So:

1. Resolve the window with the EXISTING helper `model_info.get_context_window(model, base_url) -> int` (already used by `summarizer.py` — do NOT build a new one).
2. Estimate the transcript's token cost (reuse whatever token estimate `summarizer`/`translation` already uses; a chars/4 heuristic is acceptable if none is exposed). Reserve headroom for the prompt scaffold + expected JSON output (e.g. keep transcript ≤ ~60% of the window).
3. If the transcript **fits** → extract from the full transcript (precise evidence + `evidence_start_s`).
4. If it **does not fit** → fall back to **summary-first**: extract from the item's already-computed `summary` (+ `highlights_json` if present). Evidence is coarser (the summary's `[MM:SS]` markers, mapped to `evidence_start_s` when parseable; else NULL) and `attribution_method` is at best `metadata_context`/`llm_inferred`. NEVER blind-truncate the transcript — a hard cut drops late-episode claims silently.
5. No map-reduce/chunking in v1.5 (explicitly deferred; revisit only if small-context models prove common in practice).

**Files:**
- Create: `app/services/speaker_claims.py`
- Test: `tests/test_services_speaker_claims.py`

**Interfaces:**
- Consumes: `repos.speaker_claims` (Task 1), `repos.speakers.get_speaker` (PR 1), `repos.source_speakers.get_link` (PR 2; optional — used to set `source_speaker_id`), `highlight_parser._extract_json_blob`, `model_info.get_context_window` (existing), `litellm.acompletion` (mocked). The `source` arg is a `Video` (has `.id`, `.title`, `.transcript`, `.summary`, `.kind`, `.duration_seconds`, `.user_id`).
- Produces: `extract_claims_for_source(db, source, speaker_ids, *, model, api_key, base_url) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_services_speaker_claims.py
import asyncio
import json
from unittest.mock import AsyncMock, patch


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _completion(text: str):
    """Mock a non-streaming litellm.acompletion return (mirrors
    summarizer._completion's response.choices[0].message.content shape)."""
    msg = type("M", (), {"content": text})
    choice = type("C", (), {"message": msg})
    resp = type("R", (), {"choices": [choice]})
    return AsyncMock(return_value=resp())


async def _seed(db, *, names=("Chamath", "Jason")):
    ids = []
    for n in names:
        cur = await db.execute(
            "INSERT INTO speakers (user_id, name, name_key) VALUES (1,?,?)",
            (n, n.lower()),
        )
        ids.append(cur.lastrowid)
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title, transcript) "
        "VALUES ('vid-1', 1, 'youtube', 'u', 'All-In Ep', 'transcript body')"
    )
    await db.commit()
    from app.repos import videos as videos_repo
    return ids, await videos_repo.get(db, "vid-1")


_CLEAN = json.dumps({
    "claims": [
        {"speaker": "Chamath", "claim": "SPACs are mispriced", "topic": "markets",
         "evidence_text": "SPACs are wildly mispriced", "evidence_start_s": 42,
         "confidence": 0.8, "attribution_method": "explicit_name",
         "attribution_confidence": 0.95, "attribution_reason": "named in prior sentence"},
        {"speaker": "Jason", "claim": "founders should stay scrappy", "topic": "startups",
         "evidence_text": "stay scrappy", "evidence_start_s": 120,
         "confidence": 0.7, "attribution_method": "speaker_marker",
         "attribution_confidence": 0.8, "attribution_reason": "speaker label in transcript"},
    ]
})


def test_clean_json_produces_attributed_claims(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        ids, source = await _seed(db)
        with patch("app.services.speaker_claims.litellm.acompletion", _completion(_CLEAN)):
            out = await speaker_claims.extract_claims_for_source(
                db, source, ids, model="openai/gpt-4o", api_key="k", base_url=None,
            )
        assert len(out) == 2
        # persisted, attributed to the right speakers
        cham = await repo.list_for_speaker(db, ids[0])
        jason = await repo.list_for_speaker(db, ids[1])
        assert [c.claim for c in cham] == ["SPACs are mispriced"]
        assert cham[0].attribution_method == "explicit_name"
        assert cham[0].attribution_confidence == 0.95
        assert cham[0].evidence_start_s == 42
        assert cham[0].review_status == "unreviewed"
        assert [c.claim for c in jason] == ["founders should stay scrappy"]
    _run(go())


def test_prose_wrapped_json_still_parsed(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        ids, source = await _seed(db)
        wrapped = "Sure, here are the claims:\n```json\n" + _CLEAN + "\n```\nDone."
        with patch("app.services.speaker_claims.litellm.acompletion", _completion(wrapped)):
            out = await speaker_claims.extract_claims_for_source(
                db, source, ids, model="m", api_key="k", base_url=None,
            )
        assert len(out) == 2
        assert len(await repo.list_for_speaker(db, ids[0])) == 1
    _run(go())


def test_garbage_returns_empty_no_raise(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        ids, source = await _seed(db)
        with patch("app.services.speaker_claims.litellm.acompletion", _completion("not json at all")):
            out = await speaker_claims.extract_claims_for_source(
                db, source, ids, model="m", api_key="k", base_url=None,
            )
        assert out == []
        assert await repo.list_for_speaker(db, ids[0]) == []
    _run(go())


def test_unattributable_statement_makes_no_claim(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        ids, source = await _seed(db)
        # speaker not in the expected list, or null → dropped
        payload = json.dumps({"claims": [
            {"speaker": "Unknown Person", "claim": "x", "attribution_method": "llm_inferred"},
            {"speaker": None, "claim": "y"},
            {"speaker": "Chamath", "claim": "kept", "attribution_method": "explicit_name",
             "attribution_confidence": 0.9},
        ]})
        with patch("app.services.speaker_claims.litellm.acompletion", _completion(payload)):
            out = await speaker_claims.extract_claims_for_source(
                db, source, ids, model="m", api_key="k", base_url=None,
            )
        assert [c["claim"] for c in out] == ["kept"]
        assert [c.claim for c in await repo.list_for_speaker(db, ids[0])] == ["kept"]
    _run(go())


def test_long_source_small_window_uses_summary_not_transcript(db):
    """Finding 5: when the transcript exceeds the model's context window, the
    grounding text handed to the LLM is the summary, not the (truncated)
    transcript. We capture the prompt the mock receives and assert it contains
    the summary text and NOT the long transcript body."""
    async def go():
        from app.services import speaker_claims
        ids, _ = await _seed(db)
        # Make the source long with a distinct transcript vs summary.
        long_transcript = "TRANSCRIPT_MARKER " * 5000   # ~tens of thousands of tokens
        await db.execute(
            "UPDATE videos SET transcript=?, summary=? WHERE id='vid-1'",
            (long_transcript, "SUMMARY_MARKER: the gist."),
        )
        await db.commit()
        from app.repos import videos as videos_repo
        source = await videos_repo.get(db, "vid-1")
        seen_prompt = {}

        def _capture(text):
            mock = _completion(_CLEAN)
            async def side_effect(**kwargs):
                seen_prompt["messages"] = kwargs["messages"]
                return mock.return_value
            return AsyncMock(side_effect=side_effect)

        with patch("app.services.speaker_claims.model_info.get_context_window",
                   AsyncMock(return_value=8192)), \
             patch("app.services.speaker_claims.litellm.acompletion", _capture(_CLEAN)):
            await speaker_claims.extract_claims_for_source(
                db, source, ids, model="tiny/model", api_key="k", base_url=None,
            )
        blob = json.dumps(seen_prompt["messages"])
        assert "SUMMARY_MARKER" in blob
        assert "TRANSCRIPT_MARKER" not in blob   # never blind-fed the long transcript
    _run(go())


def test_llm_exception_returns_empty(db):
    async def go():
        from app.services import speaker_claims
        ids, source = await _seed(db)
        with patch("app.services.speaker_claims.litellm.acompletion",
                   AsyncMock(side_effect=RuntimeError("boom"))):
            out = await speaker_claims.extract_claims_for_source(
                db, source, ids, model="m", api_key="k", base_url=None,
            )
        assert out == []
    _run(go())


def test_reprocess_replaces_prior_claims(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        ids, source = await _seed(db)
        with patch("app.services.speaker_claims.litellm.acompletion", _completion(_CLEAN)):
            await speaker_claims.extract_claims_for_source(
                db, source, ids, model="m", api_key="k", base_url=None)
            # second run must not duplicate
            await speaker_claims.extract_claims_for_source(
                db, source, ids, model="m", api_key="k", base_url=None)
        assert len(await repo.list_for_speaker(db, ids[0])) == 1
    _run(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_services_speaker_claims.py -v`
Expected: FAIL — `app.services.speaker_claims` doesn't exist.

- [ ] **Step 3: Implement the service**

```python
# app/services/speaker_claims.py
"""Attributed claim extraction + persona-prompt retrieval.

ONE entry point (extract_claims_for_source) serves both extraction
triggers (the pipeline piggyback now; the standalone backfill job in
PR 4). The LLM is given the expected speakers BY NAME and must attribute
each claim to exactly one of them with evidence + a timestamp/offset +
how confidently it tied the claim to that speaker. Statements it can't
confidently attribute become NO claim — attribution beats style
(spec rule #3). Best-effort: returns [] on garbage and NEVER raises, so
the pipeline piggyback can call it without a guard of its own.

retrieve_for_prompt is the slice the persona prompt grounds on. PR 3 =
recency + topic-text overlap only (the fallback). PR 4 swaps in an
embedding-ranked path behind the SAME signature.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import litellm

from app.repos import speaker_claims as claims_repo
from app.repos import speakers as speakers_repo
from app.services.highlight_parser import _extract_json_blob

log = logging.getLogger(__name__)

# Attribution methods the persona prompt understands. An LLM value
# outside this set is coerced to None (the prompt then hedges).
_ATTR_METHODS = {
    "explicit_name", "speaker_marker", "metadata_context", "llm_inferred", "manual",
}


def _system_prompt(speaker_names: list[str]) -> str:
    names = ", ".join(speaker_names)
    return (
        "You extract ATTRIBUTED claims from a transcript or article for a "
        "track-record dossier. The following named people are expected to "
        f"appear in this source: {names}.\n\n"
        "For each substantive position, prediction, or factual assertion, "
        "attribute it to EXACTLY ONE of those named people and record the "
        "evidence. A claim you cannot confidently tie to one of those named "
        "people MUST be dropped — never guess, never attribute to someone not "
        "in the list. Attribution beats coverage: fewer, well-attributed "
        "claims are better than many shaky ones.\n\n"
        "Return ONE JSON object, no prose, with this exact shape:\n"
        "{\n"
        '  "claims": [\n'
        "    {\n"
        '      "speaker": "<one of the expected names, verbatim>",\n'
        '      "claim": "<the position in their words, paraphrased, <40 words>",\n'
        '      "topic": "<short topical tag for grouping, e.g. \\"markets\\">",\n'
        '      "evidence_text": "<the supporting excerpt>",\n'
        '      "evidence_start_s": <integer seconds into the video, or null>,\n'
        '      "text_start_offset": <integer char offset for article/text, or null>,\n'
        '      "confidence": <0..1 paraphrase fidelity>,\n'
        '      "attribution_method": "<explicit_name|speaker_marker|metadata_context|llm_inferred>",\n'
        '      "attribution_confidence": <0..1 confidence the claim is THIS speaker\'s>,\n'
        '      "attribution_reason": "<short why, e.g. \\"named in prior sentence\\">"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "If nothing is confidently attributable, return {\"claims\": []}."
    )


def _user_message(source) -> str:
    body = source.transcript or ""
    return f"SOURCE TITLE: {source.title}\n\nSOURCE BODY:\n{body}"


def _coerce_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    return None


def _coerce_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


async def extract_claims_for_source(
    db, source, speaker_ids: list[int], *,
    model: str, api_key: str, base_url: str | None,
) -> list[dict]:
    """Extract + persist attributed claims for `source`. Returns the list
    of accepted claim dicts (also persisted). [] on garbage / no model /
    any error. Never raises."""
    if not speaker_ids or not model:
        return []
    # Map expected speakers by name (lowered) → id, for attribution.
    speakers = []
    for sid in speaker_ids:
        sp = await speakers_repo.get_speaker(db, sid)
        if sp is not None:
            speakers.append(sp)
    if not speakers:
        return []
    by_name = {sp.name.strip().lower(): sp for sp in speakers}
    names = [sp.name for sp in speakers]

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_prompt(names)},
            {"role": "user", "content": _user_message(source)},
        ],
        "api_key": api_key,
    }
    if base_url:
        kwargs["api_base"] = base_url

    try:
        response = await litellm.acompletion(**kwargs)
        raw = response.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001 — extraction is best-effort
        log.warning("claim extraction LLM call failed for %s: %s: %s",
                    getattr(source, "id", None), type(e).__name__, e)
        return []

    blob = _extract_json_blob(raw)
    if blob is None:
        return []
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    items = payload.get("claims")
    if not isinstance(items, list):
        return []

    accepted: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("speaker")
        claim = item.get("claim")
        if not isinstance(name, str) or not isinstance(claim, str) or not claim.strip():
            continue
        sp = by_name.get(name.strip().lower())
        if sp is None:
            continue  # not one of the expected speakers → drop (rule #3)
        method = item.get("attribution_method")
        if method not in _ATTR_METHODS:
            method = None
        accepted.append({
            "speaker_id": sp.id,
            "claim": claim.strip(),
            "topic": item.get("topic") if isinstance(item.get("topic"), str) else None,
            "evidence_text": item.get("evidence_text") if isinstance(item.get("evidence_text"), str) else None,
            "evidence_start_s": _coerce_int(item.get("evidence_start_s")),
            "evidence_end_s": _coerce_int(item.get("evidence_end_s")),
            "text_start_offset": _coerce_int(item.get("text_start_offset")),
            "text_end_offset": _coerce_int(item.get("text_end_offset")),
            "confidence": _coerce_float(item.get("confidence")),
            "attribution_method": method,
            "attribution_confidence": _coerce_float(item.get("attribution_confidence")),
            "attribution_reason": item.get("attribution_reason") if isinstance(item.get("attribution_reason"), str) else None,
        })

    # Replace-on-reprocess: clear THIS source's rows for these speakers,
    # then insert fresh. Forward-only, no stale duplicates.
    await claims_repo.replace_for_source_speakers(db, source.id, speaker_ids)
    for c in accepted:
        await claims_repo.insert_claim(
            db, user_id=source.user_id, source_id=source.id,
            speaker_id=c["speaker_id"], claim=c["claim"], topic=c["topic"],
            evidence_text=c["evidence_text"], evidence_start_s=c["evidence_start_s"],
            evidence_end_s=c["evidence_end_s"], text_start_offset=c["text_start_offset"],
            text_end_offset=c["text_end_offset"], confidence=c["confidence"],
            extraction_method="llm", attribution_method=c["attribution_method"],
            attribution_confidence=c["attribution_confidence"],
            attribution_reason=c["attribution_reason"],
        )
    return accepted
```

> `_extract_json_blob` is imported from `app.services.highlight_parser` exactly as related-links/highlights consume it — do not re-implement JSON envelope detection.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_services_speaker_claims.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/speaker_claims.py tests/test_services_speaker_claims.py
git commit -m "feat(speakers): attributed claim extraction (LLM, best-effort)"
```

---

### Task 3: `speaker_claims.retrieve_for_prompt` — recency + topic-text retrieval

PR 3's retrieval is **recency + topic-text overlap with `query`**, cross-source, capped at `limit`. Returns claim dicts carrying the source **title** and **timestamp** for citation. No embeddings — PR 4 adds an embedding-ranked path behind this same signature.

**Files:**
- Modify: `app/services/speaker_claims.py` — add `retrieve_for_prompt`.
- Test: `tests/test_services_speaker_claims.py` (append).

**Interfaces:**
- Consumes: `speaker_claims` + `videos` tables.
- Produces: `retrieve_for_prompt(db, speaker_id, *, query, limit=12) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_services_speaker_claims.py (append)
def test_retrieve_ranks_topic_overlap_then_recency(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        cur = await db.execute(
            "INSERT INTO speakers (user_id, name, name_key) VALUES (1,'C','c')")
        sid = cur.lastrowid
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) "
            "VALUES ('v1', 1, 'youtube', 'u', 'Markets Episode')")
        await db.commit()
        await repo.insert_claim(db, speaker_id=sid, source_id="v1",
                                claim="inflation will fall", topic="inflation rates",
                                evidence_start_s=10)
        await repo.insert_claim(db, speaker_id=sid, source_id="v1",
                                claim="AI is overhyped", topic="ai bubble",
                                evidence_start_s=20)
        out = await speaker_claims.retrieve_for_prompt(
            db, sid, query="what about inflation?", limit=12)
        # the topic-overlapping claim ranks first; both carry source title + ts
        assert out[0]["claim"] == "inflation will fall"
        assert out[0]["source_title"] == "Markets Episode"
        assert out[0]["evidence_start_s"] == 10
    _run(go())


def test_retrieve_respects_limit_and_is_cross_source(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        cur = await db.execute(
            "INSERT INTO speakers (user_id, name, name_key) VALUES (1,'C','c')")
        sid = cur.lastrowid
        for v in ("v1", "v2"):
            await db.execute(
                "INSERT INTO videos (id, user_id, kind, url, title) "
                "VALUES (?, 1, 'youtube', 'u', ?)", (v, f"Ep {v}"))
        await db.commit()
        for i in range(5):
            await repo.insert_claim(db, speaker_id=sid, source_id="v1",
                                    claim=f"a{i}", topic="x")
        for i in range(5):
            await repo.insert_claim(db, speaker_id=sid, source_id="v2",
                                    claim=f"b{i}", topic="y")
        out = await speaker_claims.retrieve_for_prompt(db, sid, query="x", limit=3)
        assert len(out) == 3
        sources = {c["source_id"] for c in await speaker_claims.retrieve_for_prompt(
            db, sid, query="", limit=12)}
        assert sources == {"v1", "v2"}  # cross-source
    _run(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_services_speaker_claims.py -k retrieve -v`
Expected: FAIL — `retrieve_for_prompt` doesn't exist.

- [ ] **Step 3: Implement**

Append to `app/services/speaker_claims.py`:

```python
import re

_WORD = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "")}


async def retrieve_for_prompt(
    db, speaker_id: int, *, query: str, limit: int = 12,
) -> list[dict]:
    """Cross-source claim slice for the persona prompt.

    PR 3 ranking: claims whose topic/claim text overlaps the viewer's
    query first, then most-recent. Capped at `limit`. Each row carries
    the source title + timestamp for in-reply citation.

    PR 4 will add an embedding-ranked branch behind this signature; the
    recency/topic path stays as the fallback when no embedding exists or
    the embedding backend is off.
    """
    cur = await db.execute(
        "SELECT c.*, v.title AS source_title "
        "FROM speaker_claims c JOIN videos v ON v.id = c.source_id "
        "WHERE c.speaker_id=? AND c.review_status != 'rejected' "
        "ORDER BY c.created_at DESC, c.id DESC",
        (speaker_id,),
    )
    rows = await cur.fetchall()
    q = _tokens(query)

    def score(r) -> int:
        if not q:
            return 0
        hay = _tokens(f"{r['topic'] or ''} {r['claim']}")
        return len(q & hay)

    # Stable sort: overlap score desc, then the recency order from SQL.
    ranked = sorted(enumerate(rows), key=lambda iz: (-score(iz[1]), iz[0]))
    return [_claim_to_prompt_dict(r) for _i, r in ranked[:limit]]


def _claim_to_prompt_dict(r) -> dict:
    """THE fixed-key contract for a retrieved claim. speaker_chat's prompt
    builder, the routes, the persona-turn tests, and PR 4's track-record peek +
    embedding-ranked retrieval all consume exactly these keys. PR 4 reuses this
    function so the shape never drifts (Finding 3). The row `r` must expose
    `source_title` (the JOIN alias) alongside the speaker_claims columns.
    """
    return {
        "claim": r["claim"], "topic": r["topic"],
        "evidence_text": r["evidence_text"],
        "evidence_start_s": r["evidence_start_s"],
        "source_id": r["source_id"], "source_title": r["source_title"],
        "attribution_method": r["attribution_method"],
        "attribution_confidence": r["attribution_confidence"],
        "review_status": r["review_status"],
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_services_speaker_claims.py -v`
Expected: PASS (all extraction + retrieval tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/speaker_claims.py tests/test_services_speaker_claims.py
git commit -m "feat(speakers): retrieve_for_prompt (recency + topic fallback)"
```

---

### Task 4: `speaker_chat.build_speaker_system_prompt` — the grounded persona prompt

The prompt is the safety boundary, so it gets its own pure, asserted builder. It MUST carry every clause from the spec's prompt block: in-character first-person voice + "you are NOT the real {name}"; **viewer-language** instruction; "extracted from your sources" / paraphrase framing (NOT "actually said"); per-claim attribution tags + **hedge-on-low-confidence**; transcript-as-context-only; the anti-other-speaker / unattributed rule; "engage honestly on contradictions"; "never invent"; and "don't break character to disclaim you're an AI". Seed block only when `seed_ts`/`seed_quote` are present.

**Files:**
- Create: `app/services/speaker_chat.py`
- Test: `tests/test_services_speaker_chat.py`

**Interfaces:**
- Consumes: `Speaker` (PR 1).
- Produces: `build_speaker_system_prompt(*, speaker, claims, source_context, seed_ts=None, seed_quote=None) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_services_speaker_chat.py
from app.models import Speaker


def _speaker(**kw):
    base = dict(
        id=1, user_id=1, known_speaker_id=None, name="Chamath",
        name_key="chamath", role="investor", avatar_id="adult-techreviewer-m",
        avatar_photo_path=None, style_note="blunt, fast-moving investor tone",
        is_active=True, created_at=None, updated_at=None,
    )
    base.update(kw)
    return Speaker(**base)


_CLAIMS = [
    {"claim": "SPACs are mispriced", "topic": "markets", "source_title": "All-In Ep 1",
     "evidence_start_s": 42, "attribution_method": "explicit_name",
     "attribution_confidence": 0.95, "review_status": "accepted"},
    {"claim": "rates stay higher for longer", "topic": "macro", "source_title": "All-In Ep 9",
     "evidence_start_s": 600, "attribution_method": "llm_inferred",
     "attribution_confidence": 0.4, "review_status": "unreviewed"},
]


def test_prompt_carries_all_grounding_clauses():
    from app.services.speaker_chat import build_speaker_system_prompt
    p = build_speaker_system_prompt(
        speaker=_speaker(), claims=_CLAIMS, source_context="some transcript text")
    # in-character + simulation boundary
    assert "Chamath" in p
    assert "NOT the real" in p
    assert "first person" in p.lower()
    # viewer-language
    assert "SAME language" in p
    # extracted-from-sources framing, NOT "actually said"
    assert "extracted from" in p.lower()
    assert "actually said" not in p.lower()
    # attribution tags + low-confidence hedge
    assert "attribut" in p.lower()
    assert "tentativ" in p.lower() or "hedge" in p.lower() or "more tentatively" in p.lower()
    # transcript = context only
    assert "context" in p.lower() and "ONLY" in p
    # anti-other-speaker rule
    assert "other speakers" in p.lower()
    # contradictions handled honestly
    assert "contradiction" in p.lower()
    # never invent
    assert "invent" in p.lower()
    # do NOT self-disclaim as AI in the reply
    assert "AI" in p
    assert "style_note" not in p           # the label, not the literal placeholder
    assert "blunt, fast-moving investor tone" in p
    # the claims are rendered with their attribution + source
    assert "SPACs are mispriced" in p
    assert "All-In Ep 1" in p


def test_prompt_seed_block_only_when_seeded():
    from app.services.speaker_chat import build_speaker_system_prompt
    p_no = build_speaker_system_prompt(
        speaker=_speaker(), claims=[], source_context="ctx")
    assert "12:04" not in p_no
    p_seed = build_speaker_system_prompt(
        speaker=_speaker(), claims=[], source_context="ctx",
        seed_ts="12:04", seed_quote="the quote")
    assert "12:04" in p_seed
    assert "the quote" in p_seed
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_services_speaker_chat.py -v`
Expected: FAIL — `app.services.speaker_chat` doesn't exist.

- [ ] **Step 3: Implement the prompt builder**

```python
# app/services/speaker_chat.py
"""Persona reply — a clearly simulated, in-character perspective of a
speaker, grounded in their ATTRIBUTED CLAIMS.

Mirrors services/chat.stream_reply mechanics exactly (same litellm
streaming kwargs, reuses chat_core.build_messages); only the system
prompt differs. The prompt is the safety boundary — it forbids putting
other speakers' / unattributed words in the persona's mouth and frames
claims as "extracted from your sources" paraphrases, not verbatim
quotes. The interface owns the AI-disclaimer banner, so the prompt tells
the model NOT to self-disclaim in the reply.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import litellm

from app.models import ChatMessage
from app.services.chat_core import build_messages


def _render_claims(claims: list[dict]) -> str:
    if not claims:
        return "(no attributed claims retrieved for this question yet)"
    lines: list[str] = []
    for c in claims:
        conf = c.get("attribution_confidence")
        method = c.get("attribution_method") or "unspecified"
        tag = f"[attribution: {method}"
        if isinstance(conf, (int, float)):
            tag += f", confidence {conf:.2f}"
        tag += "]"
        src = c.get("source_title") or "a source"
        ts = c.get("evidence_start_s")
        where = f" ({src}" + (f" @ {int(ts)}s" if isinstance(ts, (int, float)) else "") + ")"
        lines.append(f"- {c['claim']} {tag}{where}")
    return "\n".join(lines)


def build_speaker_system_prompt(
    *, speaker, claims: list[dict], source_context: str,
    seed_ts: str | None = None, seed_quote: str | None = None,
) -> str:
    name = speaker.name
    role_clause = f", {speaker.role}" if getattr(speaker, "role", None) else ""
    style_note = getattr(speaker, "style_note", None) or "(no style note on file)"
    seed_block = ""
    if seed_ts or seed_quote:
        ts = f"[{seed_ts}] " if seed_ts else ""
        quote = f"'{seed_quote}'" if seed_quote else ""
        seed_block = f"\nThe viewer is jumping in at this moment: {ts}{quote}\n"

    return (
        f"You are a clearly simulated, in-character perspective of {name}"
        f"{role_clause}, talking with a viewer. Speak in the first person, in "
        f"their voice — match their tone, rhetorical habits, bluntness or "
        f"warmth. You are NOT the real {name} and must not claim to be.\n\n"
        "LANGUAGE: reply in the SAME language as the viewer's latest message, "
        "regardless of the language of the source or the dossier.\n\n"
        "GROUNDING:\n"
        "- Anchor everything assertible in the ATTRIBUTED CLAIMS and the "
        "attributed excerpts below. These are attributed claims extracted "
        f"from the viewer's sources, each with evidence — paraphrases of "
        f"{name}'s positions, not verbatim quotes. Do NOT frame them as what "
        f"{name} 'actually said' word-for-word.\n"
        f"- Each claim is tagged with how confidently it was attributed to "
        f"{name}. For claims marked low-confidence or 'llm_inferred', speak "
        "more tentatively (\"I think I've argued…\", \"as I recall…\") rather "
        "than asserting them flatly.\n"
        "- The CURRENT SOURCE TRANSCRIPT is context for flow and style ONLY. "
        f"Do NOT present things from it as {name}'s statements unless they are "
        "attributed.\n"
        f"- NEVER put other speakers' words, or unattributed words, in {name}'s "
        "mouth. If attribution is unclear, say the source is ambiguous.\n"
        "- If the viewer points out a contradiction across sources, engage "
        "honestly and cite the sources.\n"
        "- NEVER invent specific facts, numbers, quotes, or beliefs.\n"
        "- Don't break character to disclaim you're an AI — the interface "
        "already says so.\n\n"
        f"STYLE NOTE: {style_note}\n"
        f"{seed_block}\n"
        f"ATTRIBUTED CLAIMS (extracted from {name}'s sources, each with its "
        "source and an attribution-confidence tag):\n"
        f"{_render_claims(claims)}\n\n"
        "CURRENT SOURCE CONTEXT (style/flow only — not a source of "
        f"{name}'s claims):\n"
        f"{source_context or '(none)'}"
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_services_speaker_chat.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/speaker_chat.py tests/test_services_speaker_chat.py
git commit -m "feat(speakers): grounded in-character persona system prompt"
```

---

### Task 5: `speaker_chat.stream_speaker_reply` — streaming token iterator (mocked LLM)

Mirror `services/chat.stream_reply` exactly: build messages via `chat_core.build_messages`, the same `model`/`messages`/`api_key`/`stream=True` (+ optional `api_base`) kwargs, await `litellm.acompletion`, and yield `chunk.choices[0].delta.content` deltas. Copy the `tests/test_services_chat.py` mocked-completion pattern verbatim.

**Files:**
- Modify: `app/services/speaker_chat.py` — add `stream_speaker_reply`.
- Test: `tests/test_services_speaker_chat.py` (append).

**Interfaces:**
- Consumes: `build_speaker_system_prompt` (Task 4), `chat_core.build_messages`, `litellm.acompletion` (mocked).
- Produces: `stream_speaker_reply(*, speaker, source_context, claims, history, user_message, seed_ts, seed_quote, model, api_key, base_url) -> AsyncIterator[str]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_services_speaker_chat.py (append)
from unittest.mock import AsyncMock, MagicMock, patch


def _stream_chunks(*texts: str):
    async def gen():
        for t in texts:
            choice = MagicMock()
            choice.delta.content = t
            chunk = MagicMock()
            chunk.choices = [choice]
            yield chunk
    return gen()


async def test_stream_speaker_reply_yields_tokens():
    from app.services.speaker_chat import stream_speaker_reply
    with patch(
        "app.services.speaker_chat.litellm.acompletion",
        AsyncMock(return_value=_stream_chunks("As ", "I ", "argued")),
    ):
        out: list[str] = []
        async for tok in stream_speaker_reply(
            speaker=_speaker(), source_context="ctx", claims=_CLAIMS,
            history=[], user_message="what about SPACs?",
            seed_ts=None, seed_quote=None,
            model="openai/gpt-4o", api_key="k", base_url=None,
        ):
            out.append(tok)
        assert "".join(out) == "As I argued"


async def test_stream_speaker_reply_passes_system_prompt_and_history():
    from app.services.speaker_chat import build_speaker_system_prompt, stream_speaker_reply
    from app.models import ChatMessage
    from datetime import datetime

    captured: dict = {}

    async def fake_acompletion(**kw):
        captured.update(kw)
        return _stream_chunks("ok")

    hist = [ChatMessage(id=1, video_id="v1", role="user", content="hi",
                        created_at=datetime.now()),
            ChatMessage(id=2, video_id="v1", role="assistant", content="hello",
                        created_at=datetime.now())]
    with patch("app.services.speaker_chat.litellm.acompletion", side_effect=fake_acompletion):
        async for _ in stream_speaker_reply(
            speaker=_speaker(), source_context="ctx", claims=_CLAIMS,
            history=hist, user_message="now?", seed_ts=None, seed_quote=None,
            model="m", api_key="k", base_url=None,
        ):
            pass
    msgs = captured["messages"]
    # [system] + 2 history turns + new user message (build_messages ordering)
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == build_speaker_system_prompt(
        speaker=_speaker(), claims=_CLAIMS, source_context="ctx")
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[-1]["content"] == "now?"
    assert captured["stream"] is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_services_speaker_chat.py -k stream -v`
Expected: FAIL — `stream_speaker_reply` doesn't exist.

- [ ] **Step 3: Implement**

Append to `app/services/speaker_chat.py`:

```python
async def stream_speaker_reply(
    *, speaker, source_context: str, claims: list[dict],
    history: list[ChatMessage], user_message: str,
    seed_ts: str | None = None, seed_quote: str | None = None,
    model: str, api_key: str, base_url: str | None,
) -> AsyncIterator[str]:
    messages = build_messages(
        system_prompt=build_speaker_system_prompt(
            speaker=speaker, claims=claims, source_context=source_context,
            seed_ts=seed_ts, seed_quote=seed_quote,
        ),
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

    response = await litellm.acompletion(**kwargs)
    async for chunk in response:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_services_speaker_chat.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/speaker_chat.py tests/test_services_speaker_chat.py
git commit -m "feat(speakers): stream_speaker_reply (mirrors chat.stream_reply)"
```

---

### Task 6: Pipeline piggyback — extract for active speakers in one call

Extend the PR-2 detection step in `app/pipeline.py`. After speakers are linked, if **any** linked speaker `is_active`, call `extract_claims_for_source` for **all active speakers in this episode** in one LLM call, reusing the `model`/`api_key`/`base_url` already resolved at the top of `process_video`. Best-effort — wrapped so it **never fails the job** (same posture as `_store_related_links`). No active speakers → no expensive call.

> **PR-2 dependency:** PR 2 owns the detection block (`identify_from_metadata` → resolve → link `source_speakers`). PR 3 adds the extraction tail to that block. If PR 2's detection lives in a helper (e.g. `_detect_and_link_speakers`), add the piggyback there; the wiring below assumes a helper `_extract_active_speaker_claims(db, video, model, api_key, base_url)` invoked from the detection step. Confirm the exact PR-2 seam before editing.

**Files:**
- Modify: `app/pipeline.py` — add `_extract_active_speaker_claims` + call it from the detection step (after `_store_related_links`, end of `process_video`, gated like the other enrichments).
- Test: `tests/test_pipeline_piggyback.py`

**Interfaces:**
- Consumes: `repos.source_speakers.list_for_source` (PR 2), `speaker_claims.extract_claims_for_source` (Task 2).
- Produces: `_extract_active_speaker_claims(db, video, *, model, api_key, base_url) -> None` (best-effort, never raises).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pipeline_piggyback.py
import asyncio
from unittest.mock import AsyncMock, patch


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


async def _seed_video_with_speakers(db, *, active):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title, transcript) "
        "VALUES ('vp1', 1, 'youtube', 'u', 'Ep', 'body')")
    cur = await db.execute(
        "INSERT INTO speakers (user_id, name, name_key, is_active) VALUES (1,'C','c',?)",
        (1 if active else 0,))
    sid = cur.lastrowid
    await db.execute(
        "INSERT INTO source_speakers (source_id, speaker_id, detection_source) "
        "VALUES ('vp1', ?, 'show_rule')", (sid,))
    await db.commit()
    from app.repos import videos as videos_repo
    return sid, await videos_repo.get(db, "vp1")


def test_piggyback_extracts_when_active_speaker_present(db):
    async def go():
        from app import pipeline
        sid, video = await _seed_video_with_speakers(db, active=True)
        called = {}

        async def fake_extract(db_, source, speaker_ids, *, model, api_key, base_url):
            called["speaker_ids"] = speaker_ids
            called["model"] = model
            return []

        with patch("app.pipeline.extract_claims_for_source", side_effect=fake_extract):
            await pipeline._extract_active_speaker_claims(
                db, video, model="m", api_key="k", base_url=None)
        assert called["speaker_ids"] == [sid]
        assert called["model"] == "m"
    _run(go())


def test_piggyback_skips_when_no_active_speaker(db):
    async def go():
        from app import pipeline
        _sid, video = await _seed_video_with_speakers(db, active=False)
        extract = AsyncMock(return_value=[])
        with patch("app.pipeline.extract_claims_for_source", extract):
            await pipeline._extract_active_speaker_claims(
                db, video, model="m", api_key="k", base_url=None)
        extract.assert_not_called()  # no expensive call when nobody is active
    _run(go())


def test_piggyback_never_raises(db):
    async def go():
        from app import pipeline
        _sid, video = await _seed_video_with_speakers(db, active=True)
        with patch("app.pipeline.extract_claims_for_source",
                   AsyncMock(side_effect=RuntimeError("boom"))):
            # must swallow — pipeline integrity over enrichment
            await pipeline._extract_active_speaker_claims(
                db, video, model="m", api_key="k", base_url=None)
    _run(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_pipeline_piggyback.py -v`
Expected: FAIL — `_extract_active_speaker_claims` doesn't exist.

- [ ] **Step 3: Implement the helper + wire it**

In `app/pipeline.py`, add the import near the other service imports:

```python
from app.repos import source_speakers as source_speakers_repo
from app.services.speaker_claims import extract_claims_for_source
```

Add the helper (best-effort, mirrors `_store_related_links`):

```python
async def _extract_active_speaker_claims(
    db, video, *, model: str | None, api_key: str, base_url: str | None,
) -> None:
    """Pipeline piggyback: after speakers are linked, extract claims for
    the episode's ACTIVE speakers in ONE LLM call. No active speakers →
    no call. Best-effort: never fails the job (claim extraction is
    enrichment, like related links)."""
    if not model:
        return
    try:
        linked = await source_speakers_repo.list_for_source(db, video.id)
        active_ids = [s.id for s in linked if getattr(s, "is_active", False)]
        if not active_ids:
            return
        await extract_claims_for_source(
            db, video, active_ids, model=model, api_key=api_key, base_url=base_url,
        )
    except Exception as e:  # noqa: BLE001 — enrichment must not break the pipeline
        log.warning(
            "speaker claim piggyback failed for %s: %s: %s",
            getattr(video, "id", None), type(e).__name__, e,
        )
```

Call it from PR 2's detection step. PR 2's step ends with `source_speakers` linked; append (using the already-resolved `model`/`api_key`/`base_url` and the refreshed video):

```python
    # Piggyback: extract claims for this episode's active speakers (best-
    # effort, one call). Reuses the model resolved for the summary above.
    refreshed = await videos_repo.get(db, video_id)
    if refreshed is not None:
        await _extract_active_speaker_claims(
            db, refreshed, model=model, api_key=api_key, base_url=base_url,
        )
```

> If PR 2 already added a `set_step("identifying speakers")` block, place this call at its tail (after linking), still inside the same try/guard PR 2 used so the whole speaker block stays best-effort. `extract_claims_for_source` is itself non-raising, but the `_extract_active_speaker_claims` wrapper double-guards the `list_for_source` read.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_pipeline_piggyback.py -v`
Expected: PASS.

- [ ] **Step 5: Run the pipeline's existing tests to prove no regression**

Run: `.venv/bin/pytest tests/test_pipeline.py -q` (and any `tests/test_pipeline_*.py`).
Expected: PASS — the piggyback is additive and guarded.

- [ ] **Step 6: Commit**

```bash
git add app/pipeline.py tests/test_pipeline_piggyback.py
git commit -m "feat(speakers): pipeline piggyback extraction for active speakers"
```

---

### Task 7: Persona chat routes (per-episode + whole-dossier) — REGRESSION GATE

Two routes added to PR 2's `app/routes/speakers.py`. Both load source/dossier context, retrieve a capped claim slice via `retrieve_for_prompt`, stream via `stream_speaker_reply`, persist the user + assistant messages with the thread id from `chat_threads.get_or_create`, and return an `_msg_html`-style fragment. Ownership-checked → 404. Per-episode accepts optional `seed_ts`/`seed_quote`.

**This task also runs the critical regression gate**: existing `tests/test_services_chat.py` + `tests/test_routes_chat.py` must stay green **unchanged**.

**Files:**
- Modify: `app/routes/speakers.py` — add `_speaker_msg_html`, `POST /v/{video_id}/speaker/{speaker_id}/chat`, `POST /speaker/{speaker_id}/chat`.
- Test: `tests/test_routes_speaker_chat.py`

**Interfaces:**
- Consumes: `videos_repo.get`, `speakers_repo.get_speaker`, `chat_threads.get_or_create` (PR 2), `chat_repo.append`/`history` with `thread_id` (PR 2), `source_speakers.list_for_source` (PR 2; whole-dossier context build), `speaker_claims.retrieve_for_prompt` (Task 3), `speaker_chat.stream_speaker_reply` (Task 5), `llm_models_repo.get`/`get_default` (existing), `get_db`/`get_current_user_id` (existing DI).
- Produces: the two persona routes.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_routes_speaker_chat.py
import asyncio
from collections.abc import AsyncIterator
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app


async def _fake_stream(**kw) -> AsyncIterator[str]:
    for s in ("As ", "I ", "argued"):
        yield s


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    return create_app()


async def _setup(app, *, active=True):
    from app.models import TranscriptSource, VideoKind
    from app.repos import llm_models as llm_models_repo
    from app.repos import videos as videos_repo
    await videos_repo.upsert_metadata(
        app.state.db, video_id="vs1", url="u", title="All-In Ep",
        description="", thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.YOUTUBE, user_id=1)
    await videos_repo.set_transcript(
        app.state.db, "vs1", "transcript body", TranscriptSource.AUTO_SUBS)
    cur = await app.state.db.execute(
        "INSERT INTO speakers (user_id, name, name_key, is_active) VALUES (1,'Chamath','chamath',?)",
        (1 if active else 0,))
    speaker_id = cur.lastrowid
    await app.state.db.execute(
        "INSERT INTO source_speakers (source_id, speaker_id, detection_source) "
        "VALUES ('vs1', ?, 'show_rule')", (speaker_id,))
    await llm_models_repo.insert(
        app.state.db, label="Test", provider_id="openai", model="openai/gpt-4o",
        api_key="k", base_url="", make_default=True)
    await app.state.db.commit()
    return speaker_id


def test_per_episode_persona_turn_streams_and_persists(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        speaker_id = asyncio.get_event_loop().run_until_complete(_setup(app))
        with patch("app.routes.speakers.stream_speaker_reply", side_effect=_fake_stream):
            resp = client.post(
                f"/v/vs1/speaker/{speaker_id}/chat",
                data={"content": "what about SPACs?"})
        assert resp.status_code == 200
        assert "As I argued" in resp.text

        async def check():
            from app.repos import chat_threads as threads_repo
            from app.repos import chat as chat_repo
            tid = await threads_repo.get_or_create(
                app.state.db, scope="source_speaker", source_id="vs1",
                speaker_id=speaker_id)
            msgs = await chat_repo.history(app.state.db, "vs1", thread_id=tid)
            assert [m.role for m in msgs] == ["user", "assistant"]
            assert msgs[1].content == "As I argued"
        asyncio.get_event_loop().run_until_complete(check())


def test_whole_dossier_turn_uses_speaker_scope(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        speaker_id = asyncio.get_event_loop().run_until_complete(_setup(app))
        with patch("app.routes.speakers.stream_speaker_reply", side_effect=_fake_stream):
            resp = client.post(
                f"/speaker/{speaker_id}/chat", data={"content": "hi"})
        assert resp.status_code == 200
        assert "As I argued" in resp.text

        async def check():
            from app.repos import chat_threads as threads_repo
            from app.repos import chat as chat_repo
            tid = await threads_repo.get_or_create(
                app.state.db, scope="speaker", speaker_id=speaker_id)
            msgs = await chat_repo.history(app.state.db, None, thread_id=tid) \
                if False else await chat_repo.history(app.state.db, "", thread_id=tid)
            # whichever video_id convention PR2 uses for speaker-scope rows,
            # the assistant reply is persisted on this thread:
            assert any(m.content == "As I argued" for m in msgs)
        asyncio.get_event_loop().run_until_complete(check())


def test_persona_turn_foreign_profile_404(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def setup_foreign():
            speaker_id = await _setup(app)
            # re-own the speaker to a different profile
            await app.state.db.execute(
                "UPDATE speakers SET user_id=999 WHERE id=?", (speaker_id,))
            await app.state.db.commit()
            return speaker_id
        speaker_id = asyncio.get_event_loop().run_until_complete(setup_foreign())
        with patch("app.routes.speakers.stream_speaker_reply", side_effect=_fake_stream):
            resp = client.post(f"/speaker/{speaker_id}/chat", data={"content": "hi"})
        assert resp.status_code == 404


def test_seed_ts_threaded_into_reply(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    captured: dict = {}

    async def capturing_stream(**kw):
        captured.update(kw)
        for s in ("ok",):
            yield s

    with TestClient(app) as client:
        speaker_id = asyncio.get_event_loop().run_until_complete(_setup(app))
        with patch("app.routes.speakers.stream_speaker_reply", side_effect=capturing_stream):
            client.post(
                f"/v/vs1/speaker/{speaker_id}/chat",
                data={"content": "this moment", "seed_ts": "12:04",
                      "seed_quote": "the quote"})
        assert captured["seed_ts"] == "12:04"
        assert captured["seed_quote"] == "the quote"
```

> The `test_whole_dossier_turn` history check straddles PR 2's choice of `video_id` for `scope='speaker'` rows (the spec keeps `chat_messages.video_id` for compatibility; a speaker-scope row may carry `''`/NULL). At implementation time, assert against PR 2's actual `history(thread_id=...)` contract — the key invariant is "the reply is persisted on the speaker thread."

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_routes_speaker_chat.py -v`
Expected: FAIL — routes don't exist (404 on unknown path / `AttributeError` on the patch target).

- [ ] **Step 3: Implement the routes**

In `app/routes/speakers.py` (PR 2's file), add imports and the fragment helper + routes. Mirror `routes/chat.py`'s `_msg_html` and model-resolution exactly:

```python
from app.repos import chat as chat_repo
from app.repos import chat_threads as threads_repo
from app.repos import llm_models as llm_models_repo
from app.repos import source_speakers as source_speakers_repo
from app.repos import speaker_claims as claims_repo
from app.repos import speakers as speakers_repo
from app.repos import videos as videos_repo
from app.services.markdown import render_markdown
from app.services.speaker_chat import stream_speaker_reply
from app.services.speaker_claims import retrieve_for_prompt
from markupsafe import escape


def _speaker_msg_html(role: str, content: str, *, avatar_id: str | None = None,
                      is_error: bool = False) -> str:
    """Persona chat bubble. User text escaped; assistant rendered as
    markdown. Assistant bubbles tinted with the speaker's avatar colour
    via an inline --avatar-bg var (see services/avatars.bg_color_for)."""
    if role == "user":
        return f'<div class="chat-bubble-user">{escape(content)}</div>'
    if is_error:
        return (f'<div class="chat-answer chat-msg-error">'
                f'<div class="chat-answer-content">{escape(content)}</div></div>')
    from app.services import avatars
    bg = avatars.bg_color_for(avatar_id or "")
    body = render_markdown(content)
    return (f'<div class="chat-answer chat-answer-speaker" '
            f'style="--avatar-bg: {bg}">'
            f'<div class="chat-answer-content">{body}</div></div>')


async def _resolve_model(db, llm_model_id: str):
    chosen_id: int | None = None
    if llm_model_id.strip():
        try:
            chosen_id = int(llm_model_id)
        except ValueError as e:
            raise HTTPException(400, f"invalid llm_model_id: {e}") from None
    row = (await llm_models_repo.get(db, chosen_id) if chosen_id is not None
           else await llm_models_repo.get_default(db))
    if row is None:
        raise HTTPException(400, "LLM not configured")
    return row.model, (row.api_key or ""), (row.base_url or None)


async def _run_persona_turn(
    db, *, speaker, source_context: str, content: str, thread_id: int,
    video_id: str | None, model: str, api_key: str, base_url: str | None,
    seed_ts: str | None, seed_quote: str | None,
) -> str:
    # video_id is the episode id for per-episode turns, None for whole-dossier
    # (scope='speaker') turns. PR 1 made chat_messages.video_id nullable; when
    # thread_id is set, history() selects by thread_id and ignores video_id.
    claims = await retrieve_for_prompt(db, speaker.id, query=content, limit=12)
    history = await chat_repo.history(db, video_id, thread_id=thread_id)
    await chat_repo.append(db, video_id, "user", content,
                           user_id=speaker.user_id, thread_id=thread_id)
    collected: list[str] = []
    error: str | None = None
    try:
        async for tok in stream_speaker_reply(
            speaker=speaker, source_context=source_context, claims=claims,
            history=history, user_message=content, seed_ts=seed_ts,
            seed_quote=seed_quote, model=model, api_key=api_key, base_url=base_url,
        ):
            collected.append(tok)
    except Exception as e:  # noqa: BLE001 — surface as an error bubble
        error = f"{type(e).__name__}: {e}"
    answer = "".join(collected)
    await chat_repo.append(
        db, video_id, "assistant", answer if answer else f"[error: {error}]",
        user_id=speaker.user_id, thread_id=thread_id)
    parts = [_speaker_msg_html("user", content)]
    if answer:
        parts.append(_speaker_msg_html("assistant", answer, avatar_id=speaker.avatar_id))
    if error:
        parts.append(_speaker_msg_html("assistant", error, is_error=True))
    elif not answer:
        parts.append(_speaker_msg_html("assistant", "(empty response from model)", is_error=True))
    return "".join(parts)


@router.post("/v/{video_id}/speaker/{speaker_id}/chat", response_class=HTMLResponse)
async def post_speaker_chat(
    video_id: str, speaker_id: int,
    content: str = Form(...), llm_model_id: str = Form(""),
    seed_ts: str = Form(""), seed_quote: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    video = await videos_repo.get(db, video_id)
    speaker = await speakers_repo.get_speaker(db, speaker_id)
    if video is None or speaker is None:
        raise HTTPException(404, "Not found")
    if video.user_id != current_user_id or speaker.user_id != current_user_id:
        raise HTTPException(404, "Not found")
    model, api_key, base_url = await _resolve_model(db, llm_model_id)
    thread_id = await threads_repo.get_or_create(
        db, user_id=current_user_id, scope="source_speaker",
        source_id=video_id, speaker_id=speaker_id)
    html = await _run_persona_turn(
        db, speaker=speaker, source_context=(video.transcript or ""),
        content=content, thread_id=thread_id, video_id=video_id,
        model=model, api_key=api_key, base_url=base_url,
        seed_ts=seed_ts.strip() or None, seed_quote=seed_quote.strip() or None)
    return HTMLResponse(html)


@router.post("/speaker/{speaker_id}/chat", response_class=HTMLResponse)
async def post_dossier_chat(
    speaker_id: int,
    content: str = Form(...), llm_model_id: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    speaker = await speakers_repo.get_speaker(db, speaker_id)
    if speaker is None or speaker.user_id != current_user_id:
        raise HTTPException(404, "Not found")
    model, api_key, base_url = await _resolve_model(db, llm_model_id)
    thread_id = await threads_repo.get_or_create(
        db, user_id=current_user_id, scope="speaker", speaker_id=speaker_id)
    # Whole-dossier chat has no single episode → no transcript context.
    html = await _run_persona_turn(
        db, speaker=speaker, source_context="", content=content,
        thread_id=thread_id, video_id=None, model=model, api_key=api_key,
        base_url=base_url, seed_ts=None, seed_quote=None)
    return HTMLResponse(html)
```

> **`video_id=None`** for the dossier turn (RESOLVED — was an open question when
> this plan was drafted in parallel with PR 1/2): PR 1 makes
> `chat_messages.video_id` nullable and PR 2's `chat_repo.append` accepts
> `video_id=None`, persisting `scope='speaker'` rows keyed by `thread_id` with
> `video_id=NULL`. Never pass `""` — there is no `videos` row with id `''`, so the
> FK would reject it. The ownership check uses `speaker.user_id`, never the URL.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_routes_speaker_chat.py -v`
Expected: PASS.

- [ ] **Step 5: CRITICAL — run the existing chat suite UNCHANGED**

Run: `.venv/bin/pytest tests/test_services_chat.py tests/test_routes_chat.py -q`
Expected: PASS — both files untouched and green. The persona work must not regress the video chat. If either fails, STOP and fix the regression before continuing.

- [ ] **Step 6: Register the router if PR 2 hasn't already**

Confirm `app/main.py` includes the speakers router (`app.include_router(speakers_router)`). PR 2 should have added it; if not, add the import + `include_router` call next to the other `app.include_router(...)` lines, and add a smoke assertion that `/speaker/{id}/chat` resolves (it already does via the route test).

- [ ] **Step 7: Commit**

```bash
git add app/routes/speakers.py tests/test_routes_speaker_chat.py app/main.py
git commit -m "feat(speakers): persona chat routes (per-episode + whole-dossier)"
```

---

### Task 8: On-demand extraction + claim review routes

Three more routes on PR 2's router: re-extract one confirmed source, edit a claim, and set its review status. All ownership-checked → 404. The extract + edit/review responses return the refreshed topic-grouped claim fragment (`_speaker_claims.html`, built in Task 9) so HTMX swaps the dossier in place.

**Files:**
- Modify: `app/routes/speakers.py` — add the three routes.
- Test: `tests/test_routes_speaker_chat.py` (append).

**Interfaces:**
- Consumes: `speakers_repo.get_speaker`, `videos_repo.get`, `source_speakers.get_link`/`list_for_source` (PR 2), `speaker_claims.extract_claims_for_source` (Task 2), `claims_repo.edit_claim`/`set_review_status`/`list_for_speaker` (Task 1), `_resolve_model` (Task 7).
- Produces: `POST /speaker/{id}/sources/{source_id}/extract`, `POST /speaker/{id}/claims/{claim_id}/edit`, `POST /speaker/{id}/claims/{claim_id}/review`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_routes_speaker_chat.py (append)
def test_on_demand_extract_one_source(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)

    async def fake_extract(db_, source, speaker_ids, *, model, api_key, base_url):
        from app.repos import speaker_claims as repo
        await repo.insert_claim(db_, speaker_id=speaker_ids[0],
                                source_id=source.id, claim="extracted!", topic="t")
        return [{"claim": "extracted!"}]

    with TestClient(app) as client:
        speaker_id = asyncio.get_event_loop().run_until_complete(_setup(app))
        with patch("app.routes.speakers.extract_claims_for_source", side_effect=fake_extract):
            resp = client.post(f"/speaker/{speaker_id}/sources/vs1/extract")
        assert resp.status_code == 200
        assert "extracted!" in resp.text


def test_claim_review_sets_status(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def setup():
            speaker_id = await _setup(app)
            from app.repos import speaker_claims as repo
            cid = await repo.insert_claim(
                app.state.db, speaker_id=speaker_id, source_id="vs1",
                claim="c", topic="t")
            return speaker_id, cid
        speaker_id, cid = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post(
            f"/speaker/{speaker_id}/claims/{cid}/review",
            data={"status": "accepted"})
        assert resp.status_code == 200

        async def check():
            from app.repos import speaker_claims as repo
            c = (await repo.list_for_speaker(app.state.db, speaker_id))[0]
            assert c.review_status == "accepted"
        asyncio.get_event_loop().run_until_complete(check())


def test_claim_edit_updates_text(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def setup():
            speaker_id = await _setup(app)
            from app.repos import speaker_claims as repo
            cid = await repo.insert_claim(
                app.state.db, speaker_id=speaker_id, source_id="vs1",
                claim="old", topic="t")
            return speaker_id, cid
        speaker_id, cid = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post(
            f"/speaker/{speaker_id}/claims/{cid}/edit",
            data={"claim": "new text", "topic": "macro"})
        assert resp.status_code == 200
        assert "new text" in resp.text


def test_review_foreign_profile_404(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def setup():
            speaker_id = await _setup(app)
            from app.repos import speaker_claims as repo
            cid = await repo.insert_claim(
                app.state.db, speaker_id=speaker_id, source_id="vs1", claim="c")
            await app.state.db.execute(
                "UPDATE speakers SET user_id=999 WHERE id=?", (speaker_id,))
            await app.state.db.commit()
            return speaker_id, cid
        speaker_id, cid = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post(
            f"/speaker/{speaker_id}/claims/{cid}/review",
            data={"status": "accepted"})
        assert resp.status_code == 404
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_routes_speaker_chat.py -k "extract or review or edit" -v`
Expected: FAIL — routes don't exist.

- [ ] **Step 3: Implement the routes**

Add to `app/routes/speakers.py`. The claim fragment helper renders Task 9's partial via the templates env (PR 2 exposes the Jinja `templates` object; reuse it):

```python
async def _claims_fragment(db, request, speaker) -> str:
    grouped = await claims_repo.list_for_speaker(db, speaker.id, grouped_by_topic=True)
    return templates.get_template("_speaker_claims.html").render(
        request=request, speaker=speaker, grouped=grouped)


@router.post("/speaker/{speaker_id}/sources/{source_id}/extract",
             response_class=HTMLResponse)
async def post_extract_source(
    speaker_id: int, source_id: str, request: Request,
    llm_model_id: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    speaker = await speakers_repo.get_speaker(db, speaker_id)
    source = await videos_repo.get(db, source_id)
    if speaker is None or source is None:
        raise HTTPException(404, "Not found")
    if speaker.user_id != current_user_id or source.user_id != current_user_id:
        raise HTTPException(404, "Not found")
    model, api_key, base_url = await _resolve_model(db, llm_model_id)
    await extract_claims_for_source(
        db, source, [speaker_id], model=model, api_key=api_key, base_url=base_url)
    return HTMLResponse(await _claims_fragment(db, request, speaker))


@router.post("/speaker/{speaker_id}/claims/{claim_id}/review",
             response_class=HTMLResponse)
async def post_claim_review(
    speaker_id: int, claim_id: int, request: Request,
    status: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    speaker = await speakers_repo.get_speaker(db, speaker_id)
    if speaker is None or speaker.user_id != current_user_id:
        raise HTTPException(404, "Not found")
    if status not in ("unreviewed", "accepted", "rejected"):
        raise HTTPException(400, "bad status")
    await claims_repo.set_review_status(db, claim_id, status)
    return HTMLResponse(await _claims_fragment(db, request, speaker))


@router.post("/speaker/{speaker_id}/claims/{claim_id}/edit",
             response_class=HTMLResponse)
async def post_claim_edit(
    speaker_id: int, claim_id: int, request: Request,
    claim: str = Form(""), topic: str = Form(""),
    evidence_text: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    speaker = await speakers_repo.get_speaker(db, speaker_id)
    if speaker is None or speaker.user_id != current_user_id:
        raise HTTPException(404, "Not found")
    # Finding 6: also verify the claim BELONGS to this speaker. The
    # profile-ownership gate above blocks foreign profiles, but within one
    # profile a hand-edited URL (/speaker/{B}/claims/{A}/edit) could otherwise
    # mutate speaker A's claim under speaker B's page. This is MANDATORY, not
    # optional. Use a helper to avoid repeating it in edit + review.
    claim_row = await claims_repo.get(db, claim_id)
    if claim_row is None or claim_row.speaker_id != speaker_id:
        raise HTTPException(404, "Not found")
    fields: dict = {}
    if claim.strip():
        fields["claim"] = claim.strip()
    if topic.strip():
        fields["topic"] = topic.strip()
    if evidence_text.strip():
        fields["evidence_text"] = evidence_text.strip()
    await claims_repo.edit_claim(db, claim_id, **fields)
    return HTMLResponse(await _claims_fragment(db, request, speaker))
```

> `templates` and `Request` come from PR 2's `routes/speakers.py` imports (`from fastapi import Request`; the shared `Jinja2Templates` instance). Reuse them — don't construct a second templates env.
> **Both** `claims/{id}/edit` and `claims/{id}/review` MUST run the
> `claim_row.speaker_id != speaker_id → 404` check above (Finding 6). Add
> `claims_repo.get(db, claim_id) -> SpeakerClaim | None` to the claims repo if it
> isn't already there. Consider a small `_owned_claim(db, speaker_id, claim_id,
> current_user_id)` helper returning `(speaker, claim)` or raising 404, shared by
> both routes.

- [ ] **Step 3b: Write the cross-speaker ownership test (Finding 6)**

```python
# tests/test_routes_speaker_chat.py (append)
def test_claim_edit_rejects_claim_of_other_speaker(client, db):
    async def setup():
        # Two speakers in the SAME profile; a claim on speaker A.
        a = await speakers_repo.resolve_speaker(db, name="Speaker A")
        b = await speakers_repo.resolve_speaker(db, name="Speaker B")
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) VALUES ('v1',1,'youtube','u','t')")
        await db.execute(
            "INSERT INTO speaker_claims (user_id, speaker_id, source_id, claim, extraction_method) "
            "VALUES (1, ?, 'v1', 'A said this', 'manual')", (a,))
        await db.commit()
        cur = await db.execute("SELECT id FROM speaker_claims WHERE speaker_id=?", (a,))
        return b, (await cur.fetchone())[0]
    b_id, claim_id = asyncio.get_event_loop().run_until_complete(setup())
    # Editing speaker A's claim via speaker B's URL must 404 (not silently edit).
    resp = client.post(f"/speaker/{b_id}/claims/{claim_id}/edit", data={"claim": "hijacked"})
    assert resp.status_code == 404
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_routes_speaker_chat.py -v`
Expected: PASS — including `test_claim_edit_rejects_claim_of_other_speaker` (Task 9's partial must exist first; if you implement routes before the template, do Task 9's Step 3 partial creation now, then return).

- [ ] **Step 5: Commit**

```bash
git add app/routes/speakers.py tests/test_routes_speaker_chat.py
git commit -m "feat(speakers): on-demand extract + claim edit/review routes (claim ownership check)"
```

---

### Task 9: UI — dossier on the speaker page + disclaimer banner + tinted bubbles

Extend PR 2's `speaker.html` with the topic-grouped dossier and the whole-dossier chat composer; create the `_speaker_claims.html` partial (reused by Task 8's fragment responses); add the persona disclaimer banner + the per-episode persona composer wiring to `video_detail.html`. `unreviewed` claims must look visibly less authoritative (dimmed + a marker), `accepted` authoritative, `rejected` struck/hidden.

**Files:**
- Create: `app/templates/_speaker_claims.html`
- Modify: `app/templates/speaker.html` (PR 2) — include the dossier partial + whole-dossier chat composer + banner.
- Modify: `app/templates/video_detail.html` — persona disclaimer banner (persona mode) + per-episode persona composer (`hx-post="/v/{{ video.id }}/speaker/{{ active_speaker.id }}/chat"`, `hx-target="#chat-history"`), reusing the existing chat-section structure.
- Test: `tests/test_routes_speaker_chat.py` (append a render assertion) — these are template renders exercised through the routes, matching house style (no browser).

**Interfaces:**
- Consumes: `claims_repo.list_for_speaker(grouped_by_topic=True)` (Task 1), `services/avatars.bg_color_for` (existing), the persona routes (Task 7).
- Produces: rendered dossier + banner markup.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_routes_speaker_chat.py (append)
def test_speaker_page_renders_dossier_with_review_state(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def setup():
            speaker_id = await _setup(app)
            from app.repos import speaker_claims as repo
            await repo.insert_claim(
                app.state.db, speaker_id=speaker_id, source_id="vs1",
                claim="reviewed claim", topic="markets",
                evidence_text="ev", evidence_start_s=42)
            cid2 = await repo.insert_claim(
                app.state.db, speaker_id=speaker_id, source_id="vs1",
                claim="raw claim", topic="ai")
            await repo.set_review_status(app.state.db, cid2 - 1, "accepted")
            return speaker_id
        speaker_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/speaker/{speaker_id}")
        assert resp.status_code == 200
        body = resp.text
        # claims grouped by topic, with evidence + the unreviewed marker
        assert "reviewed claim" in body
        assert "raw claim" in body
        assert "markets" in body and "ai" in body
        assert "unreviewed" in body          # the marker class/label for the raw claim
        # whole-dossier chat composer points at the speaker route
        assert f"/speaker/{speaker_id}/chat" in body


def test_video_detail_has_persona_disclaimer_banner(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        asyncio.get_event_loop().run_until_complete(_setup(app))
        resp = client.get("/v/vs1")
        assert resp.status_code == 200
        # the simulated-persona banner copy is present (hidden until persona mode)
        assert "Simulated" in resp.text or "AI impression" in resp.text
```

> `GET /speaker/{id}` is PR 2's route. PR 3 enriches the page body; if PR 2's page test already asserts the header, keep it green. The `GET /v/vs1` detail route is the existing video detail page — PR 3 only adds banner markup to its chat section.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_routes_speaker_chat.py -k "dossier or banner" -v`
Expected: FAIL — dossier/banner markup absent.

- [ ] **Step 3: Create the claims partial**

```jinja
{# app/templates/_speaker_claims.html — topic-grouped dossier fragment.
   Reused by the speaker page and the extract/edit/review HTMX responses.
   `unreviewed` rows are dimmed + marked; `rejected` rows are struck. #}
<div id="speaker-claims" class="speaker-dossier">
  {% if not grouped %}
    <p class="dossier-empty">No claims yet. Activate this speaker or
      extract from a source to build the track record.</p>
  {% else %}
    {% for topic, claims in grouped.items() %}
      <section class="dossier-topic">
        <h3 class="dossier-topic-title">{{ topic | e }}</h3>
        <ul class="dossier-claims">
          {% for c in claims %}
            <li class="dossier-claim dossier-claim--{{ c.review_status }}"
                data-claim-id="{{ c.id }}">
              <p class="dossier-claim-text">{{ c.claim | e }}</p>
              {% if c.evidence_text %}
                <blockquote class="dossier-evidence">{{ c.evidence_text | e }}</blockquote>
              {% endif %}
              <p class="dossier-meta">
                {% if c.review_status == 'unreviewed' %}
                  <span class="dossier-badge dossier-badge--unreviewed">unreviewed</span>
                {% elif c.review_status == 'accepted' %}
                  <span class="dossier-badge dossier-badge--accepted">reviewed</span>
                {% elif c.review_status == 'rejected' %}
                  <span class="dossier-badge dossier-badge--rejected">rejected</span>
                {% endif %}
                {% if c.confidence is not none %}
                  <span class="dossier-confidence">confidence {{ '%.0f' % (c.confidence * 100) }}%</span>
                {% endif %}
                <a class="dossier-source"
                   href="/v/{{ c.source_id }}{% if c.evidence_start_s is not none %}#t={{ c.evidence_start_s }}{% endif %}">
                  source{% if c.evidence_start_s is not none %} @ {{ c.evidence_start_s }}s{% endif %}</a>
              </p>
              <div class="dossier-claim-actions">
                <button hx-post="/speaker/{{ speaker.id }}/claims/{{ c.id }}/review"
                        hx-vals='{"status": "accepted"}'
                        hx-target="#speaker-claims" hx-swap="outerHTML">Accept</button>
                <button hx-post="/speaker/{{ speaker.id }}/claims/{{ c.id }}/review"
                        hx-vals='{"status": "rejected"}'
                        hx-target="#speaker-claims" hx-swap="outerHTML">Reject</button>
              </div>
            </li>
          {% endfor %}
        </ul>
      </section>
    {% endfor %}
  {% endif %}
</div>
```

> The `dossier-claim--unreviewed` / `--accepted` / `--rejected` classes carry the visual weight (dim/struck) in `app/static/app.css`. Add the minimal CSS there (e.g. `.dossier-claim--unreviewed { opacity:.6 } .dossier-claim--rejected { text-decoration:line-through; opacity:.4 }`). The badge text "unreviewed" is what the test asserts.

- [ ] **Step 4: Extend `speaker.html`**

Inside PR 2's `speaker.html`, after the confirmed-sources section, add the dossier include and the whole-dossier chat composer:

```jinja
  <section class="speaker-section speaker-track-record">
    <h2>Track record</h2>
    {% include "_speaker_claims.html" %}
  </section>

  <section class="speaker-section speaker-chat">
    <div class="persona-disclaimer">
      ⚠️ Simulated — AI impression of {{ speaker.name | e }} based on your
      sources, not their real words.
    </div>
    <div id="chat-history" class="chat-thread"></div>
    <form class="chat-composer"
          hx-post="/speaker/{{ speaker.id }}/chat"
          hx-target="#chat-history" hx-swap="beforeend">
      <input type="text" name="content" placeholder="Chat with {{ speaker.name | e }}…" required>
      <button type="submit">Send</button>
    </form>
  </section>
```

> Pass `grouped = await claims_repo.list_for_speaker(db, speaker_id, grouped_by_topic=True)` into the `GET /speaker/{id}` template context in PR 2's handler (extend the context dict). If PR 2's handler doesn't yet pass `grouped`, add it there.

- [ ] **Step 5: Add the banner + persona composer to `video_detail.html`**

In the existing chat `<section class="chat">` (around line 176), add the disclaimer banner (hidden until persona mode by a CSS class/JS toggle PR 2's chip switch drives) and, when an `active_speaker` is in persona mode, point the composer at the per-episode route. Minimal additive markup:

```jinja
      <div class="persona-disclaimer" data-persona-banner hidden>
        ⚠️ Simulated — AI impression based on your sources, not their real words.
      </div>
```

And the persona composer variant (the existing chip-switch JS swaps `hx-post`; if PR 2 already rewires the composer's `hx-post` per selected chip, only the banner is added here):

```jinja
      {# When a speaker chip is active, the composer targets the persona
         route; the video composer (PR-existing) stays the default. #}
```

> The banner is interface-owned transparency (spec Positioning). It must be present in the DOM (the test asserts the copy) but visually shown only in persona mode — the chip switch from PR 2 toggles the `hidden` attribute / a `persona-mode` class. Speaker bubbles are tinted via `_speaker_msg_html`'s inline `--avatar-bg` (Task 7).

- [ ] **Step 6: Run to verify pass**

Run: `.venv/bin/pytest tests/test_routes_speaker_chat.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/templates/_speaker_claims.html app/templates/speaker.html app/templates/video_detail.html app/static/app.css tests/test_routes_speaker_chat.py
git commit -m "feat(speakers): dossier UI + persona disclaimer banner + tinted bubbles"
```

---

### Task 10: Full-suite green + regression proof

**Files:** none (verification only).

- [ ] **Step 1: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS — all new PR 3 tests plus the entire existing suite.

- [ ] **Step 2: Explicit regression re-confirmation**

Run: `.venv/bin/pytest tests/test_services_chat.py tests/test_routes_chat.py -q`
Expected: PASS, with **no diff** to either file (confirm `git status` shows neither modified). This proves the `thread_id` extension + persona work is behaviour-preserving for the video chat.

- [ ] **Step 3: Lint / type gates (house style)**

Run: `.venv/bin/ruff check app/services/speaker_claims.py app/services/speaker_chat.py app/repos/speaker_claims.py app/routes/speakers.py app/pipeline.py`
Expected: clean (or only pre-existing warnings). Fix any new issues, re-run the relevant tests, and commit.

- [ ] **Step 4: Commit any lint fixups**

```bash
git add -A
git commit -m "chore(speakers): lint + full-suite green for PR 3"
```

---

## PR 3 done-criteria

- `.venv/bin/pytest -q` is fully green (all new PR 3 tests + the entire existing suite).
- **Extraction produces attributed claims** (mocked LLM): clean JSON → claims carrying `attribution_method`/`attribution_confidence`/`attribution_reason` + evidence + timestamp, defaulting to `review_status='unreviewed'`; prose-wrapped JSON is still parsed via `_extract_json_blob`; garbage → `[]`; a statement that can't be confidently attributed to an expected speaker yields **no** claim; re-running replaces rather than duplicates; the LLM raising → `[]` (never propagates).
- **Persona chat streams in-character with the correct grounded prompt**: `stream_speaker_reply` mirrors `chat.stream_reply` (reuses `build_messages`, same streaming kwargs) and the system prompt carries the simulation boundary, viewer-language instruction, "extracted from your sources" (not "actually said") framing, per-claim attribution tags + low-confidence hedging, transcript-as-context-only, the anti-other-speaker rule, contradiction-honesty, never-invent, and the no-AI-self-disclaimer instruction; the seed block appears only when seeded.
- **Pipeline piggyback** extracts claims for the episode's active speakers in **one** call during processing, reusing the summary's resolved model, and never fails the job; no active speakers → no call.
- **Both chat surfaces work** through the routes (per-episode `scope='source_speaker'`, whole-dossier `scope='speaker'`), persist user + assistant turns with the correct `thread_id`, render an `_msg_html`-style fragment with avatar-tinted speaker bubbles, and 404 for a foreign profile.
- **Claim review works**: on-demand extraction for one confirmed source, claim edit, and accept/reject review all update the DB and return the refreshed topic-grouped dossier fragment; the speaker page renders claims grouped by topic with evidence/source/timestamp/confidence/review-status, where `unreviewed` reads as visibly less authoritative (dimmed + marker); the persona disclaimer banner is present in persona mode.
- **Existing chat tests unchanged**: `tests/test_services_chat.py` + `tests/test_routes_chat.py` stay green with no edits.
- **Out of scope (PR 4):** embedding-ranked retrieval (`speaker_claim_embeddings` + KNN), the standalone library-wide backfill job, and candidate discovery/confirm/dismiss — `retrieve_for_prompt` keeps its signature so PR 4 swaps in the embedding path behind it.
