# Embedding Normalization for Working KNN Similarity — Design

**Date:** 2026-06-18
**Status:** Approved (brainstorming complete)

## Problem (verified against production data)

The KNN "related items" path never finds candidates, so both the new curated
Related-Summaries block and the older live-KNN strip render empty.

Root cause — two compounding issues, both pre-existing (not introduced by the
related-summaries feature):

1. **The `video_embeddings` vec0 table uses L2 distance, not cosine.** It is
   declared `summary_vec FLOAT[384]` with no `distance_metric=cosine`, so
   sqlite-vec's `MATCH` returns Euclidean (L2) distance.
2. **Embeddings are not normalized.** `embeddings_local.embed_text` calls
   `model.encode(text)` without `normalize_embeddings=True`, so vectors have
   varying magnitudes.

Measured on the live DB (219 embeddings): the nearest neighbour of a sample
item sits at L2 ≈ 2.65, with the rest at 2.68–3.0. Meanwhile
`related.related_video_ids` filters with `max_distance=0.75`, a value chosen as
if `distance` were cosine in [0, 2]. Nothing ever passes the filter →
`candidate_ids` is always empty → `compute_related_links` short-circuits to
`[]` with no LLM call (exactly the behaviour observed in the logs).

## Solution (minimally invasive)

Normalize embeddings to unit length at encode time. For unit vectors, L2
distance is a monotonic function of cosine distance
(`L2² = 2 · cosine_distance`), so:

- The vec0 table stays unchanged (still L2) — no virtual-table schema
  migration.
- Neighbour *ordering* is identical to cosine ordering.
- The threshold is expressed as a cosine distance and converted to L2
  internally: `l2_threshold = sqrt(2 · max_cosine_distance)`.

Existing embeddings (computed un-normalized) become invalid and are
recomputed via the **existing** re-embed queue (the same mechanism used for
the 768d→384d migration): a one-time, idempotent migration nulls
`summary_embedded_at` for all summarized videos, and the scheduler re-embeds
them in the background. No manual step.

## Goals / Non-Goals

**Goals**
- `embed_text` returns unit-length vectors.
- `related.related_video_ids` filters by a cosine-distance threshold,
  converting to L2 internally; neighbour ordering and filtering become
  semantically correct.
- All 219 existing embeddings are recomputed automatically via the existing
  scheduler queue.
- Self-healing during the transition: un-migrated videos simply show no
  curated block until their re-embed completes.

**Non-Goals**
- No change to the vec0 table schema / `distance_metric` (deliberately not
  switching to `distance_metric=cosine`; normalization makes L2 sufficient).
- No new re-embed mechanism — reuse `videos_pending_reembed` + the scheduler.
- No change to the embedding model or its 384-dim output.
- No retroactive recomputation of related-links blocks beyond what reindex /
  the natural pipeline already do (out of scope; the related-summaries feature
  governs that).

## Components

### 1. `app/services/embeddings_local.py` — normalize at encode
- `_encode_sync` calls
  `model.encode(text, convert_to_numpy=True, show_progress_bar=False,
  normalize_embeddings=True)`.
- sentence-transformers then returns unit-length vectors. One-line change plus
  a docstring note.

### 2. `app/services/related.py` — cosine threshold, L2 internally
- Rename the parameter for clarity: `max_cosine_distance: float = 0.75`
  (keeping the same default semantic — a cosine-distance ceiling).
- Compute `l2_threshold = math.sqrt(2 * max_cosine_distance)` once.
- Filter candidates with `dist <= l2_threshold` against the L2 `distance`
  returned by vec0 (unchanged query path).
- Update the docstring: distances from the table are L2 over unit vectors;
  ordering equals cosine ordering; the threshold is a cosine value converted
  to L2.
- Note: `0.75` is a starting value; fine-tune against real normalized
  distances after a re-embed run if needed. (Stated as a tuning note, not a
  blocker.)

### 3. `app/db.py` — one-time re-embed migration
- In `_run_migrations` (or `init_schema`, matching the placement style of the
  existing `embedding_dim_migrated` / `syntheses_threads_migrated` markers),
  add an idempotent block gated by a `settings` marker
  `embeddings_normalized = "1"`:
  - If the marker is absent: `UPDATE videos SET summary_embedded_at = NULL
    WHERE summary IS NOT NULL`, then set the marker.
- The existing scheduler (`videos_pending_reembed` + `count_pending_reembed`)
  drains the queue in the background, re-embedding each summary through the
  now-normalizing `embed_text`. No new wiring.
- Must run in a place consistent with the other settings-gated one-shot
  migrations and must not collide with FTS triggers (follow the established
  ordering used by `_migrate_v7_embedding_dim`).

## Data Flow

```
App start
  → migration: marker 'embeddings_normalized' absent?
       → UPDATE videos SET summary_embedded_at = NULL WHERE summary NOT NULL
       → set marker '1'
  → scheduler loop (existing):
       videos_pending_reembed(limit) → embed_text (now normalized)
       → upsert_summary_embedding → summary_embedded_at = now()
  → over time, all 219 embeddings are unit-length

related_video_ids(video):
  own = get_summary_embedding(video.id)            # unit vector after re-embed
  hits = search_by_summary_vector(own)             # L2 distances, cosine order
  l2_threshold = sqrt(2 * max_cosine_distance)
  candidates = [v for v,d in hits if v != id and d <= l2_threshold]
  ... (existing profile / dedup / archived filtering unchanged)
```

## Error Handling

- Normalization is a pure encode flag — cannot fail on its own.
- `embed_text` keeps raising `ValueError` on empty input (unchanged contract).
- Re-embed uses the proven scheduler path: per-item failures are logged and
  retried on the next pass (existing behaviour, no new code).
- Migration is idempotent via the `embeddings_normalized` settings marker; a
  second start is a no-op and does not trigger a second re-embed.
- Transition period: while the queue drains, normalized and un-normalized
  vectors briefly coexist. Un-migrated videos show no curated block until
  re-embedded — self-healing, accepted.

## Testing

Follow existing project conventions (migration test, repo/service tests).
- **`embeddings_local`:** assert `embed_text` returns a vector whose L2 norm is
  ≈ 1.0. If loading the real model is too heavy/unstable in the sandbox
  (known HuggingFace-cache issues), mock the model and assert
  `normalize_embeddings=True` is passed through to `encode`.
- **`related`:** with stubbed embeddings/distances, assert the cosine→L2
  conversion filters correctly — one neighbour just inside and one just
  outside `max_cosine_distance`.
- **Migration:** on a DB with `summary_embedded_at` set, the migration nulls it
  for all rows with a summary and sets the marker; a second run is a no-op.

## Open Risks / Notes

- The `0.75` cosine ceiling is inherited and unverified against *normalized*
  data; after the first real re-embed run, measure the actual neighbour
  distribution and adjust if the block is too sparse or too noisy. Tuning, not
  a blocker.
- This fixes the substrate; the related-summaries feature itself was correct —
  it was starved of candidates. After re-embed, reindexing a video (or new
  ingests) should begin producing non-empty curated blocks where genuinely
  similar items exist.
