# Embedding Search — Design Spec (Stufe 1)

**Date:** 2026-05-06
**Status:** Approved for implementation
**Owner:** Stefan
**Builds on:** [yt-summary core](2026-05-05-yt-summary-design.md)

## Purpose

Make library search work even when the user doesn't remember the exact
words from a video's title/description. Combines the existing FTS5
keyword search with a semantic vector search over each item's summary.

## Scope (Stufe 1)

In:
- Embed each item's `summary` whenever the summary is generated or
  regenerated. Store the vector in a `vec0`-virtual-table next to the
  videos table.
- Search route runs FTS5 and vector search in parallel, merges results
  via Reciprocal Rank Fusion (RRF), returns top-N.
- Settings: `embedding_model` (text, default `nomic-embed-text`),
  `embedding_base_url` (optional, falls back to `llm_base_url`).
- Embedding via LiteLLM's `aembedding` against the configured Ollama or
  OpenAI-compatible backend.
- Failures are logged and skipped — never block summarization.

Out (Stufe 2, deferred):
- Embedding the transcript in chunks (for finer-grained search hits and
  Chat-RAG).
- Backfill endpoint (existing rows get embeddings on next reindex).
- Cross-language search quality tuning.
- Hybrid weights configurable via Settings.

## Data Model

`sqlite-vec`'s `vec0` virtual table:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS video_embeddings USING vec0(
    video_id TEXT PRIMARY KEY,
    summary_vec FLOAT[768]
);
```

The dimension is fixed at table creation. Default is **768** for
`nomic-embed-text`. If the user switches to `mxbai-embed-large` (1024
dims), they have to manually drop the table and let it recreate, or
the schema auto-detects on first embedding (the simpler MVP-path:
auto-detect on the first successful embedding, drop+recreate the
table if dim differs).

`videos.summary_embedded_at TEXT NULL` — bookkeeping column so we can
tell at a glance whether a summary's embedding is up-to-date.

`init_schema` adds both. The vec0 table requires the `vec0` extension
to be loaded into the connection — done at `connect()` time.

## Components

### `app/services/embeddings.py` (new)

```python
async def embed_text(
    text: str, *, model: str, api_key: str, base_url: str | None
) -> list[float]
```

Wraps `litellm.aembedding`. Returns a flat list of floats. Raises
`ValueError` on empty text. Network errors propagate.

### `app/repos/embeddings.py` (new)

```python
async def upsert_summary_embedding(
    db, video_id: str, vector: list[float]
) -> None

async def delete_summary_embedding(db, video_id: str) -> None

async def search_by_summary_vector(
    db, vector: list[float], limit: int = 50
) -> list[tuple[str, float]]  # (video_id, distance)
```

Uses `vec_distance_cosine`. Returns rows sorted by ascending distance
(most similar first). Adapter handles `vector` → `BLOB` packing via
`struct.pack` or sqlite-vec's `vec_f32(JSON)` helper.

### `app/db.py` (modify)

- Load `sqlite-vec` extension in `connect()` before any query.
- Add `video_embeddings` virtual table to `SCHEMA`.
- Add `summary_embedded_at` column with idempotent migration.

### `app/pipeline.py` (modify)

After `videos_repo.set_summary(...)`:

```python
await _try_embed_summary(db, video_id, summary, settings)
```

Helper reads embedding settings, calls `embed_text`, stores via repo.
On any exception: log a warning, continue. Embedding is a nice-to-have,
not a blocker.

### `app/repos/videos.py` (modify)

`search()` becomes a thin coordinator:

```python
async def search(db, query, limit=50, *, tag=None) -> list[Video]:
    fts_ranks = await _search_fts(db, query, tag=tag, limit=limit)
    vec_ranks = await _search_vector(db, query, tag=tag, limit=limit, settings=...)
    fused = _rrf_fuse(fts_ranks, vec_ranks)
    return await _hydrate_videos(db, fused[:limit])
```

`_search_vector` reads embedding settings from the settings repo,
calls `embed_text(query)`, then `embeddings_repo.search_by_summary_vector`.
On any failure (no model configured, network down, etc.) returns an
empty list — search degrades gracefully to FTS-only.

`_rrf_fuse` is the textbook formula:
```
score(d) = sum over rankers r of 1 / (k + rank_r(d))
```
with `k = 60`. Document-IDs not in a ranker's top-N get rank `infinity`
(contributes 0).

### Settings (modify)

Two new keys:
- `embedding_model` (text, default empty → fallback to `nomic-embed-text`)
- `embedding_base_url` (text, default empty → fallback to `llm_base_url`)

### UI

No change to the search input. The hybrid happens server-side.
Optionally show a small caption under search results: "Hybrid search
across keywords and meaning" — but not required for V1.

## Failure Modes

1. **No embedding model configured / Ollama unreachable** — vector
   path returns empty, FTS still works, search query works as today.
2. **Embedding succeeds but DB has dim mismatch** — repo's upsert
   fails. We log + skip; existing row keeps its old embedding (or
   has none). Manual recovery: drop the `video_embeddings` table and
   the schema recreates with the new dim on next boot.
3. **Long summary** — most embedding models cap at ~512 tokens.
   `nomic-embed-text` handles 8K. We don't truncate ourselves;
   LiteLLM/Ollama handle the hard limit.
4. **Empty query** — bypass vector search (no embedding call), fall
   back to `list_recent`.

## Tests

- `tests/test_services_embeddings.py` — `embed_text` shapes, error paths
- `tests/test_repos_embeddings.py` — upsert/delete/search round-trips
- `tests/test_repos_videos_search.py` — RRF behavior with fixtures
- `tests/test_pipeline.py` — embed_summary called after set_summary
- `tests/test_db.py` — vec0 table exists, dim migration

~12 new tests.

## Stufe 2 (later)

- Chunk-level embedding (transcript split into 200-500 token chunks)
- Chat-RAG: pull top-K relevant chunks per question rather than
  feeding the whole transcript
- Cross-encoder reranking
- Settings to tune RRF weights or disable one branch

## Out of Scope (won't ever)

- Synchronous "live" semantic search as you type
- Embedding visualization / clustering UI
- Fine-tuned embedding models
