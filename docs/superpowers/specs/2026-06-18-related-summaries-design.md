# Related Summaries — Design

**Date:** 2026-06-18
**Status:** Approved (brainstorming complete)

## Summary

After each summary is generated, the pipeline computes a curated "Related
Summaries" block once and stores it as a JSON column on the video (mirroring
`highlights_json`). The block is a list of links to other videos in the user's
library, chosen by a two-stage **hybrid** process: existing KNN embeddings
pre-filter candidates, then a single LLM call selects the genuinely relevant
ones and supplies a one-line reason for each.

On the detail page, a single "Related" section renders the curated block when
present, and falls back to the existing live-KNN related fragment when it is
absent (older videos predating the feature, or videos where computation failed).

This is deliberately **forward-only**: a newly summarized video links to older
videos that already exist; older videos are not retroactively updated to point
at newer ones. Re-summarizing a video (`/reindex`) re-runs the pipeline and
therefore refreshes its block as a side effect — this is the manual mechanism
for "pulling an old video forward" if desired.

## Goals / Non-Goals

**Goals**
- One curated, cached related-links block per video, computed at generation time.
- Hybrid quality: KNN candidates → LLM curation with reasons.
- Never block or break summary generation if related-link computation fails.
- Reuse existing KNN infrastructure (`services/related.py`, `video_embeddings`).
- Single "Related" UI section with graceful KNN fallback.

**Non-Goals**
- No inline links inside the summary body text (explicitly out of scope —
  block at the end only).
- No retroactive backfill across the whole library, and no standalone backfill
  script. Reindex is the only mechanism to refresh an old video.
- No new search/embedding model; reuse the 384-dim local embeddings.
- No live recomputation on page view.

## Data Flow

```
process_video()
  → [summary generated + embedded]            (existing steps, unchanged)
  → compute_related_links(db, config, video)  (NEW, wrapped in try/except)
       ├─ KNN:  related_video_ids(db, video, user_id, limit=10)   (existing)
       ├─ load compact context for each candidate (title + highlights or
       │        truncated summary, NOT the full summary)
       ├─ LLM:  "Which of these summaries are genuinely worth linking from
       │         this one? Return {video_id, reason} for each; may be empty."
       └─ validate: keep only video_ids that were in the KNN candidate set
  → set_related_links(db, video_id, links_json)   (NEW repo fn, new column)
```

## Components

### 1. DB column `related_links_json` (table `videos`)
- Sibling of `highlights_json`. Format:
  `[{"video_id": str, "title": str, "reason": str}]`.
- `NULL` = not yet computed (→ UI falls back to KNN).
- `[]` = computed, nothing relevant found (→ UI shows nothing / KNN fallback per
  rendering rule below).
- `title` is denormalized (snapshot at compute time) so rendering needs no join.
- Added via the project's existing additive-migration pattern in `app/db.py`
  (`ALTER TABLE ... ADD COLUMN` guarded by a column-existence check, matching
  e.g. the `image_query` / `archived_at` migrations).

### 2. Service `app/services/related_links.py`
- `async def compute_related_links(db, config, video, *, user_id) -> list[dict]`
- Step A — candidates: `related_video_ids(db, video, user_id=user_id, limit=10)`.
  If empty → return `[]` (no LLM call).
- Step B — context: for each candidate load title + a compact context
  (highlights if present, else summary truncated to ~500 chars). Keep total
  prompt small.
- Step C — LLM: single call via the project's existing LLM plumbing
  (`litellm` through the summarizer's completion path / config-selected model),
  with a JSON schema requesting an array of `{video_id, reason}`. The LLM may
  return an empty array.
- Step D — validate (anti-hallucination): drop any `video_id` not in the KNN
  candidate set; attach the known `title` from the candidate set (never trust an
  LLM-supplied title). Mirrors the existing timestamp-link validation pattern.
- Returns the validated list of `{video_id, title, reason}`.

### 3. Pipeline hook (`app/pipeline.py`, `process_video`)
- After the summary is generated and embedded, before completion:
  - `try: links = await compute_related_links(...)` then
    `set_related_links(db, video_id, links)`.
  - `except Exception: log and continue` — leave the column `NULL`.
- Because reindex routes back through `process_video`, the block refreshes on
  reindex automatically. No separate code path.

### 4. Repo functions (`app/repos/videos.py`)
- `set_related_links(db, video_id, links: list[dict]) -> None` — serialize to
  JSON, store in `related_links_json`, bump `updated_at` (consistent with
  `set_summary` / highlights writes).
- Reading: extend the existing video row→`Video` mapping to parse
  `related_links_json` into a typed field on the `Video` dataclass
  (`related_links: list[dict] | None`), mirroring how `highlights_json` is
  surfaced.

### 5. Rendering (detail page)
- New partial (e.g. `app/templates/related_summaries_section.html`) rendering a
  clickable list: each item is a link `title → /v/{video_id}` with the `reason`
  as muted subtext.
- Rendering rule (single "Related" section):
  - If `related_links_json` is present **and non-empty** → render the curated
    block.
  - Otherwise → fall back to the existing live-KNN `related-fragment`
    (lazy-loaded HTMX), unchanged.
- The existing KNN fragment code is retained as the fallback path.

## Error Handling

Related links are a nice-to-have and must never block summary generation.
- LLM failure / invalid JSON / timeout → caught in `process_video`, logged,
  column left `NULL`, pipeline proceeds. UI falls back to KNN.
- No KNN neighbours / empty library → no LLM call, store `[]`.
- Anti-hallucination: only candidate-set IDs are persisted; titles come from the
  candidate set, not the LLM.

## Testing

Follow existing project conventions (one migration test, per-repo test, etc.).
- **Migration:** `tests/test_db_migration_related_links.py` — column added on an
  old DB, idempotent, existing rows get `NULL`.
- **Service:** `tests/test_related_links.py` with a mocked LLM —
  (a) hallucinated IDs (not in candidates) are dropped;
  (b) empty candidate list → `[]` with no LLM call;
  (c) invalid JSON from LLM → raises, and is shown to be swallowed by the
      pipeline-level handler (or tested at the pipeline boundary).
- **Repo:** extend `tests/test_repos_videos.py` — `set_related_links` round-trip
  and `Video.related_links` mapping.
- **Rendering/route:** detail page renders curated block when JSON present;
  falls back to KNN fragment when `NULL`/empty.

## Open Risks / Notes

- Forward-only is an accepted limitation (user-confirmed). Reindex is the escape
  hatch.
- Candidate context size: keep compact to control token cost; highlights are the
  preferred compact context when available.
- The LLM model used is the same config-selected model as summarization; no new
  model configuration is introduced.
