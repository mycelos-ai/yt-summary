# Local embeddings (`paraphrase-multilingual-MiniLM-L12-v2`) — design

**Status:** draft
**Date:** 2026-05-15
**Author:** Stefan + Claude

## Problem

Embeddings today are produced via LiteLLM, defaulting to
`ollama/nomic-embed-text` (768d). Two consequences:

- The embedding stack is provider-coupled: switching the LLM provider
  in Settings can silently break vector search if the user also
  changes `embedding_model` to something with a different dimension.
  The `video_embeddings` table is `vec0(... FLOAT[768])` — the first
  insert with a 1536d vector raises, the pipeline marks the job
  `failed`, and the operator has no obvious recovery path.
- The user wants to "set up the LLM and forget about embeddings" —
  having a separate Embeddings configuration card in Settings is
  cognitive overhead for a piece of infrastructure that should just
  work.

The plan: replace the LiteLLM-backed embedder with a local
`sentence-transformers` model bundled into the app. No more
configuration, no more provider coupling, no more dimension surprise.
On-device on a Pi5, multilingual (German+English), small enough to
ship.

Hybrid search (FTS5 + vector + reciprocal-rank-fusion) already
works in `app/repos/videos.py`. FTS5 over `(title, description)`
catches keyword matches independent of the embedding stack. We do
NOT touch the search ranker — only the embedding source.

## Goals

- Replace LiteLLM-based embedding with
  `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
  (384d, ~120 MB, German+English).
- Remove `embedding_model` / `embedding_base_url` from Settings UI
  and from the provider preset wizard.
- Migrate existing 768d vectors: drop the table, recreate at 384d,
  re-embed all videos with summaries in the background.
- Re-embed runs inside the existing `PlaylistScheduler` tick (no
  new worker, no new queue).
- Diagnostics page surfaces re-embed progress.

## Non-goals

- Configurable embedding model. The point of this PR is to remove
  that knob. Future model swaps are code changes, not user choices.
- A new background worker for re-embedding. Piggybacking on the
  scheduler tick keeps the operational surface small.
- Pre-bundling the model in the Docker image. On-demand download
  from Hugging Face on first use is good enough for LAN deployment;
  the user has Internet at install time.
- Provider-agnostic re-embedding migration framework. We do this
  exactly once, hard-coded for the 768→384 transition. Future
  embedder changes get their own migration.
- Touching FTS5 or the hybrid ranker. They already work.
- Embedding the transcript (today only the summary is embedded —
  out of scope to expand).
- An external "embedding rack" (Option D). Deferred until the Pi5
  shows real CPU pressure.

## Surface

### Settings UI changes

`app/templates/settings.html`:

- Remove the entire `embedding_model` / `embedding_base_url`
  block from the LLM card.
- Remove the `<button>Test embedding</button>` and its target
  `<div>`.
- Remove embedding fields from the Quick Setup wizard's per-provider
  preset detail panel.
- Add a one-line note in the LLM card or somewhere visible:
  > "Embeddings run locally — no configuration needed."

### New module

`app/services/embeddings_local.py`:

```python
"""Local embedding via sentence-transformers.

Loads `paraphrase-multilingual-MiniLM-L12-v2` (384d) lazily on first
use, keeps it in a process-wide singleton. Inference runs in a
worker thread (`asyncio.to_thread`) so the event loop stays
responsive — single embedding takes ~1 s on a Pi5.

The model auto-downloads to `~/.cache/huggingface/` on first use
(~120 MB). Subsequent process starts hit the cache.
"""

EMBEDDING_DIM = 384
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

async def embed_text(text: str) -> list[float]:
    """Return the 384d embedding vector for `text`.

    Empty input raises ValueError (matches existing contract).
    """
```

The module owns the singleton (`_model: SentenceTransformer | None`)
and the lock that prevents two concurrent first-time loads.

### Refactored shim

`app/services/embeddings.py` becomes a thin compatibility wrapper:

```python
"""Compatibility shim — delegates to embeddings_local.

The legacy `model` / `api_key` / `base_url` parameters are accepted
but ignored. They will be removed in a follow-up cleanup once all
callers stop passing them.
"""

async def embed_text(
    text: str,
    *,
    model: str | None = None,    # ignored, kept for compat
    api_key: str = "",            # ignored
    base_url: str | None = None,  # ignored
) -> list[float]:
    from app.services import embeddings_local
    return await embeddings_local.embed_text(text)
```

This avoids churning every caller (`pipeline.py`, `home.py`,
`routes/settings.py`) in this PR. A follow-up can drop the unused
kwargs.

### Schema migration

`app/db.py`:

The migration runs `_migrate_v7_embedding_dim(db)` BEFORE the
`CREATE VIRTUAL TABLE IF NOT EXISTS` statements, so it can see and
drop the old 768d table cleanly before the up-to-date DDL recreates
it at 384d.

```python
async def _migrate_v7_embedding_dim(db: aiosqlite.Connection) -> None:
    """One-shot migration from 768d to 384d embeddings.

    Idempotent — guarded by the `embedding_dim_migrated=384` setting.
    Runs BEFORE the schema CREATEs so the old table can be dropped
    cleanly; the subsequent IF NOT EXISTS CREATE uses the new shape.
    """
    if await settings.get(db, "embedding_dim_migrated") == "384":
        return  # already migrated
    # On a fresh install the table doesn't exist yet — DROP IF EXISTS
    # is a no-op. On an upgrade from 768d, this drops the old table
    # so the upcoming CREATE recreates it with FLOAT[384].
    await db.execute("DROP TABLE IF EXISTS video_embeddings")
    # Mark every video with a summary as needing re-embedding. The
    # scheduler's _reembed_pending_batch will pick them up.
    await db.execute(
        "UPDATE videos SET summary_embedded_at = NULL "
        "WHERE summary IS NOT NULL"
    )
    await settings.set(db, "embedding_dim_migrated", "384")
    await db.commit()
```

This avoids needing to introspect sqlite-vec's stored dimension —
the settings flag is the single source of truth for "have we
migrated yet?". On a brand-new install the flag is absent, the
DROP IF EXISTS is harmless, the UPDATE matches no rows, and the
flag gets set.

Order in `init_schema`:

1. `_migrate_v7_embedding_dim(db)` — drops the old table (if any).
2. `db.executescript(SCHEMA)` — runs the (updated) CREATE
   statements, including the new 384d vector table.

### New repo functions

`app/repos/embeddings.py`:

```python
async def videos_pending_reembed(
    db: aiosqlite.Connection, limit: int
) -> list[str]:
    """Video IDs with a summary but no current embedding.

    Source of truth: `videos.summary IS NOT NULL AND summary_embedded_at IS NULL`.
    """

async def count_pending_reembed(db: aiosqlite.Connection) -> int:
    """Same predicate, COUNT(*). For diagnostics display."""
```

### Scheduler integration

`app/scheduler.py`:

The `PlaylistScheduler.run()` loop adds a re-embed pass after the
playlist refresh and before `_record_tick`:

```python
# After the per-playlist sync loop:
n_reembedded = await self._reembed_pending_batch(limit=10)
if n_reembedded:
    self._touch(current_step=f"re-embedded {n_reembedded} videos")
await self._record_tick()
```

`_reembed_pending_batch(limit)` is a new method that:

1. Calls `embeddings_repo.videos_pending_reembed(db, limit)`.
2. For each video id: load the summary via `videos_repo.get`,
   call `embeddings.embed_text(summary)`, write via
   `embeddings_repo.upsert_summary_embedding`.
3. Logs failures per-video and continues (one bad video doesn't
   stop the batch).
4. Returns the count of successfully re-embedded videos.

The scheduler ticks every 60 minutes by default. After the migration,
re-embedding 10 videos per hour means a typical install (a few
hundred videos) is fully re-embedded within a day. The user can
force progress via the diagnostics page's "Jetzt prüfen" button.

This is a deliberate scope-cut: we don't add a re-embed-specific
worker because we don't need throughput. Vector search is degraded
(falls back to FTS-only for not-yet-embedded videos) during the
window — acceptable for a self-hosted single-user tool.

### Diagnostics page

`app/templates/diagnostics.html` and `app/routes/settings.py`:

The Scheduler card gets one extra line:

```
Re-embed pending: 47 videos
```

(Hidden when the count is 0.)

The route handler reads
`embeddings_repo.count_pending_reembed(db)` and passes it as
`reembed_pending` into the template context.

### Provider presets

`app/services/providers.py`:

- Drop `default_embedding` field from `ProviderPreset`.
- Drop `list_embedding_models(provider_id)`.
- Drop `embedding_model_override` from `apply_preset`.
- Drop the embedding-handling branch in
  `quick_setup_ollama_models`.

### Routes

`app/routes/settings.py`:

- Remove `POST /settings/test-embedding` handler.
- Remove `embedding_model` / `embedding_base_url` form fields from
  `save_settings`. The fields just disappear from POST payloads.
  Existing DB rows are harmless cruft — a follow-up cleanup can
  delete them; doing it here is YAGNI.
- The `apply_preset` mapping also stops writing those keys.

### Dependency

`pyproject.toml`:

```
"sentence-transformers>=3.0",
```

This pulls in `torch` (CPU only by default), `transformers`,
`tokenizers`, etc. Total install footprint is ~700 MB on disk
(torch dominates), but at runtime only ~250 MB RAM is used by the
loaded MiniLM model.

## Failure modes considered

- **First-call download is slow / fails.** The first `embed_text`
  call after fresh install blocks for ~30 s while ~120 MB streams
  from Hugging Face. Pipeline's `_try_embed_summary` already swallows
  embedding exceptions (logs and continues). Search degrades to
  FTS-only. Next call retries. Diagnostics log shows the error. The
  scheduler's `_reembed_pending_batch` won't crash either — per-video
  try/except.
- **Migration on a fresh install.** No old `video_embeddings` table
  exists; the new `CREATE … FLOAT[384]` runs; the migration probe
  sees an empty table, sets `embedding_dim_migrated=384`, returns.
- **Migration runs twice in a row** (e.g., container restart between
  init_schema and the first re-embed). The settings flag prevents
  re-running. If somehow the flag isn't set but the table is already
  384, the probe still detects the dimension and marks migrated.
- **Container restart mid-batch.** Each video is upserted in its
  own transaction. A crash leaves earlier videos done, later ones
  pending. Next scheduler tick continues.
- **`sentence-transformers` import fails** (missing torch on a weird
  platform). The entire `embeddings_local.py` import raises on
  first call to `embed_text`, the pipeline logs it, search runs
  FTS-only forever. Diagnostics log surfaces the import error. This
  is a deployment misconfiguration we accept will need manual
  diagnosis.
- **Pi3/Pi4 RAM pressure.** Loading the 250 MB model can OOM on a
  Pi3. Documented in README: "Pi5 with ≥4 GB RAM recommended."
  Existing user is on a Pi5, so not blocking.
- **Search query during re-embed window.** The vector pass returns
  fewer results than usual. Reciprocal-rank-fusion still merges
  with FTS results, so search continues to work — just less
  semantic-similarity-driven for the not-yet-embedded slice.

## Testing strategy

Each new module gets its own focused tests. Existing
`test_services_embeddings.py` becomes obsolete (covers LiteLLM
delegation that we're deleting) — adapt it to the shim's new
behaviour or delete and replace.

- `tests/test_services_embeddings_local.py` (new)
  - First call returns a 384d vector for "hello".
  - Second call within the same process re-uses the model
    singleton (no second load — assert via instance identity or
    a load-counter).
  - German input ("Hallo Welt") returns a 384d vector
    (sanity check that the multilingual model loaded correctly).
  - Empty string raises `ValueError` (matches existing contract).
- `tests/test_db_migration_v7_embedding_dim.py` (new)
  - Seed a DB with the OLD `vec0(... FLOAT[768])` schema and a few
    videos with summaries + filled `summary_embedded_at`. Run
    `init_schema` (which invokes the migration). Assert:
    - The new `video_embeddings` table accepts a 384d INSERT (and
      rejects a 768d INSERT — proves the dimension changed).
    - All `videos.summary_embedded_at` for rows with a summary are
      `NULL`. Rows without a summary are untouched.
    - `settings.get(db, "embedding_dim_migrated") == "384"`.
  - Migration on fresh install (flag absent, no old table): sets
    the flag, DROP IF EXISTS is a harmless no-op, UPDATE matches
    no rows.
  - Idempotent: re-running `init_schema` doesn't re-mark videos
    as pending (the flag short-circuits before the UPDATE).
- `tests/test_repos_embeddings.py` (extend)
  - `videos_pending_reembed` returns ids with summary + NULL
    `summary_embedded_at`, in some stable order (by id, no
    promise on which).
  - `count_pending_reembed` matches `len(videos_pending_reembed(…, limit=large))`.
- `tests/test_scheduler.py` (extend)
  - Seed two videos in "pending re-embed" state; run the scheduler
    one tick; assert both have `summary_embedded_at` set and a
    vector in `video_embeddings`.
  - One video with a deliberately-broken state (e.g., summary
    becomes empty between fetch and embed) doesn't stop the batch
    — the other completes.
- `tests/test_pipeline.py` (verify, no changes)
  - Existing `_try_embed_summary` path still works through the
    shim. No mocks need updating because the shim signature is
    backwards-compatible.
- `tests/test_routes_settings.py` (adapt)
  - The `/settings/test-embedding` test goes away.
  - The settings save test stops asserting `embedding_model`
    persistence.

Model-download cost in CI: the first test in a fresh CI environment
downloads ~120 MB. We use a session-scoped pytest fixture to share
the loaded model across all tests in the run (mirroring the existing
`amy_low_voice` pattern in `conftest.py`). On developer machines, the
model lives in `~/.cache/huggingface/` and is cached forever.

## Open questions / future work

- A follow-up PR should clean up the `embed_text` shim's unused
  `model`/`api_key`/`base_url` parameters and the dead
  `embedding_model`/`embedding_base_url` rows in `settings`. Out
  of scope here to keep the diff focused.
- If users complain that re-embedding takes too long, a manual
  "force re-embed all now" button on the diagnostics page would
  be a 10-line addition. Deferred.
- A future "embedding rack" (Option D) — moving inference to a
  GPU-equipped sidecar — would replace `embeddings_local.py` and
  nothing else. The migration framework, scheduler integration,
  and FTS-hybrid all carry over.
- If MiniLM-multilingual proves too weak on German content,
  swapping to `BAAI/bge-m3` (1024d, ~570 MB) is a one-constant
  change in `embeddings_local.py` plus a re-embed migration. Same
  pattern as this PR.
