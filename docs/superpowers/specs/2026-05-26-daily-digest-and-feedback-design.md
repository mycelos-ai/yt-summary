# Daily Digest + Highlight Feedback Loop

**Status:** Draft — design phase
**Date:** 2026-05-26

## Goal

Turn the growing pile of summarized videos and articles into a daily,
profile-specific digest of the most relevant highlights, while
learning what each profile actually cares about from in-text feedback
(highlight a passage, mark interesting / not interesting, leave a
comment). The learned interests feed back into both future summaries
and future digests, so the system gets sharper the more it's used.

This is a single-spec feature with two tightly coupled subsystems
(pipeline-side highlight extraction and digest generation), held
together by a per-profile interest profile.

## Terminology

The DB table is named `users` for historical reasons, but every "user"
in yt-summary is a Netflix-style **Profile** (Stefan, his son, etc. —
multi-profile on one box). Throughout this spec, "Profile" is the
user-facing concept; `user_id` stays in code/schema to avoid breaking
existing conventions. Everything described below is per-Profile:
digest content, interest profile, feedback, settings.

## Subsystems

### A — Highlights at summarize time

Every time a Summary is generated for a Video or Article, the same LLM
call additionally returns a structured highlights list (3-5 most
notable insights). The Profile's current interest-profile markdown is
included in the prompt as context, so both the summary text and the
highlights are already shaped by what this Profile cares about. The
LLM is explicitly allowed to return an empty list when nothing in the
content is worth surfacing — better silence than filler.

### B — Daily Digest

A scheduled job (and an on-demand button) collects all items the
active Profile owns within a configurable window (default 24h) that
have non-empty highlights, then asks the LLM to:

1. write a 3-5-sentence TL;DR naming the thematic clusters of the day,
2. pick the Top 10 items and provide for each a short hook + a "why
   this matters for this Profile" sentence.

The result is persisted as a `digests` row and rendered as a Hybrid
TL;DR-plus-source-list page.

### C — Interest profile as the binding tissue

A Markdown document per Profile, distilled from the feedback trail.
Read at summarize time and at digest time. Updated live after every
feedback action by a short "consolidate" LLM call. Manually editable
from the Profile edit page.

## User flows

### Highlight feedback (Popover at cursor)

1. User selects ≥3 characters of text inside a Summary, Transcript, or
   Digest hook.
2. A small floating popover appears at the selection end with four
   buttons: 👍 Interesting · 👎 Not interesting · 💬 Comment · 📋 Copy.
3. 👍 / 👎: a single click POSTs the feedback and the popover
   disappears with a 2-second "Saved · profile will update" toast.
4. 💬: the popover expands inline with a textarea and a Save button.
   The user can pre-select 👍 or 👎 before opening 💬 (in which case
   the comment is attached to that sentiment). If 💬 is clicked
   without a sentiment chosen, the comment is saved with
   `sentiment='interesting'` as the default — the act of commenting
   itself is a positive signal.
5. 📋: native copy, no backend round-trip.
6. After a successful POST, an HTMX out-of-band swap shows a discreet
   "Profile updating…" pill in the header; the consolidate LLM call
   runs async in the worker.

### Highlight restoration on re-open

When a page that owns highlights is loaded, the template embeds the
Profile's feedbacks for that item and the JS re-applies coloured
backgrounds (yellow = interesting, grey = not interesting) at the
stored offsets. Hover reveals the comment as a tooltip. An X icon on
hover deletes the feedback.

### Daily Digest on Home and at /digest

* Home page (`/`) gets a top teaser card. If today's digest is `ready`,
  it shows the TL;DR's first line and "10 highlights · 17 items
  scanned · 2 min read"; click → `/digest/<id>`. If `pending`, it shows
  a spinner. If `failed`, it shows a Retry button. If `digest_enabled`
  is off, a dismissible hint shows once.
* `/digest` shows the latest digest in full plus a chronological
  archive list. `/digest/<id>` is the canonical permalink for a
  single digest. HTMX polls while `status='pending'`.
* `POST /digest/generate` lets the Profile trigger a fresh digest with
  an optional `period_hours` parameter (default 24).

### Interest profile management

On `/profiles/<id>/edit` (existing route), a new section "Interest
profile" exposes:

* a markdown textarea with the current profile and a Save button
  (manual override),
* a "Rebuild from feedback" button that wipes the profile and
  re-distills it from every feedback row for this Profile,
* a "Daily digest" subsection with a toggle (`digest_enabled`) and
  an hour-of-day picker (`digest_hour_local`).

The global `/settings` page stays untouched — providers, models, and
API keys remain global.

## Architecture

Routes → services → repos → DB, matching the existing pattern.

### New files

| Path | Responsibility |
|---|---|
| `app/repos/feedback.py` | CRUD for the `feedback` table |
| `app/repos/digests.py` | CRUD for the `digests` table |
| `app/services/digest.py` | Pool collection → LLM call → persist |
| `app/services/interest_profile.py` | Consolidate logic, profile lookup for prompts, optimistic-locking |
| `app/routes/digest.py` | `/digest`, `/digest/<id>`, `POST /digest/generate` |
| `app/routes/feedback.py` | `POST /feedback`, `DELETE /feedback/<id>` |
| `app/templates/digest/list.html` | `/digest` (latest + archive) |
| `app/templates/digest/show.html` | Single digest page (TL;DR + sources) |
| `app/templates/partials/digest_teaser.html` | Home top-card |
| `app/templates/partials/highlight_popover.html` | Popover markup |
| `app/static/highlight.js` | Selection handling, popover positioning, fetch |

### Extensions to existing files

| Path | Change |
|---|---|
| `app/services/summarizer.py` | Prompt requests `{summary, highlights[]}`; injects interest profile as system context; empty `highlights` allowed |
| `app/pipeline.py` | Parse highlights from LLM JSON, write to `videos.highlights_json` |
| `app/scheduler.py` | New hourly sweep that enqueues digest jobs for each Profile whose `digest_hour_local` matches the local hour and who has no digest yet today |
| `app/routes/videos.py` | Detail templates include `highlight.js` and pass embedded feedbacks |
| `app/routes/home.py` | Load today's digest for the active Profile, render teaser |
| `app/routes/profiles.py` | Profile edit page renders interest-profile and digest-settings sections; handles their POSTs |
| `app/models.py` | `Feedback`, `Digest`, `Highlight` dataclasses |
| `app/db.py` | Add `CREATE TABLE IF NOT EXISTS` for `feedback` and `digests`; `ALTER TABLE` add-column for new `videos.highlights_json` and `users.interest_profile_md / interest_profile_version / digest_enabled / digest_hour_local`. Follow the existing idempotent column-introspection pattern (`"col" not in cols → ALTER`). No version counter. |

### Component boundaries

* `digest_service` reads highlights + interest profile, knows nothing
  about feedback.
* `interest_profile_service` takes feedback in, produces markdown out,
  knows nothing about digest.
* `summarizer` takes the profile as a prompt input, knows nothing
  about feedback.

This isolation keeps each unit independently testable.

## Data model

### `videos` (extend)

* `highlights_json TEXT NULL` — JSON array
  `[{"text": "...", "rank": 1-5, "reason": "why noteworthy"}, ...]`.
  Empty array = LLM explicitly said "nothing noteworthy". NULL = not
  yet processed (pre-feature backlog under lazy backfill).

### `feedback` (new)

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | INTEGER NOT NULL | FK to `users` (i.e. Profile) |
| `video_id` | INTEGER NOT NULL | FK to `videos` (also covers articles) |
| `source` | TEXT NOT NULL | `'summary' \| 'transcript' \| 'digest'` |
| `selected_text` | TEXT NOT NULL | exact text, ≤ 1000 chars |
| `text_offset_start` | INTEGER NOT NULL | offset in source text |
| `text_offset_end` | INTEGER NOT NULL | offset in source text |
| `sentiment` | TEXT NOT NULL | `'interesting' \| 'not_interesting'` |
| `comment` | TEXT NULL | optional user note |
| `created_at` | TIMESTAMP NOT NULL | |

Indexes: `(user_id, created_at)`, `(video_id)`.

### `digests` (new)

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | INTEGER NOT NULL | |
| `period_start` | TIMESTAMP NOT NULL | |
| `period_end` | TIMESTAMP NOT NULL | |
| `tldr` | TEXT NULL | populated when `status='ready'` |
| `top_items_json` | TEXT NULL | `[{video_id, rank, hook, reason}, ...]` |
| `item_count` | INTEGER NOT NULL DEFAULT 0 | items in the pool that day |
| `status` | TEXT NOT NULL | `'pending' \| 'rendering' \| 'ready' \| 'failed'` |
| `error` | TEXT NULL | error message when `status='failed'` |
| `created_at` | TIMESTAMP NOT NULL | |

Index: `(user_id, created_at DESC)`.

### `users` (extend — these are Profiles)

* `interest_profile_md TEXT NULL` — current distilled markdown.
* `interest_profile_version INTEGER NOT NULL DEFAULT 0` — optimistic
  locking counter, incremented on every write.
* `digest_enabled INTEGER NOT NULL DEFAULT 0` — opt-in toggle.
* `digest_hour_local INTEGER NOT NULL DEFAULT 7` — 0–23, when the
  daily sweep should fire for this Profile.

## LLM prompts

### Summarize prompt (modified)

System block prepended:

```
The active Profile's interest profile (use to shape which points you
emphasize in the summary and which highlights you surface):

<interest_profile_md or "(no profile yet — produce a neutral summary)">
```

Output schema (JSON):

```json
{
  "summary": "<markdown>",
  "highlights": [
    {"text": "...", "rank": 1, "reason": "why this is noteworthy"}
  ]
}
```

The prompt explicitly states: "If nothing in this content is truly
noteworthy, return an empty highlights array. Silence is better than
filler." `rank` is 1 (most noteworthy) to 5 (still worth surfacing).

### Digest prompt

```
System: You are a daily-digest curator for one Profile of yt-summary.

Interest profile:
<markdown or "(none yet)">

Items (JSON):
[{video_id, title, source_type, url, highlights: [...]}]

Task:
1. Pick the Top 10 (fewer if the pool is smaller).
2. Write a 3-5 sentence TL;DR that names the thematic clusters.
3. For each top item: a 1-2 sentence hook and a "why for this Profile"
   sentence.
4. Reply as JSON: {tldr, top_items: [{video_id, rank, hook, reason}]}.
```

### Consolidate prompt (interest profile)

```
System: Maintain the Profile's interest profile as concise markdown
(< 2000 tokens). Merge duplicate ideas, keep contradictions explicit.

Current profile:
<markdown or "(empty)">

New feedback events since last consolidation:
[{selected_text, sentiment, comment, source_title}, ...]

Return: updated markdown profile only.
```

## Job orchestration

### Summarize pipeline

Unchanged at the job-orchestration level — the pipeline already runs
one LLM call per item. The change is internal to that call: ask for
JSON, parse highlights, save them.

### Digest cron sweep

`app/scheduler.py` gets a new task running once per hour:

```
for each Profile with digest_enabled = 1:
    if local_hour == profile.digest_hour_local
       and no digest exists for today with status in ('pending','ready'):
        insert digests row (status='pending')
        enqueue digest job
```

The "no digest today" guard makes the sweep idempotent across restarts
and double-firings.

### Digest job

```
1. update status='rendering'
2. pool = SELECT videos WHERE user_id=? AND created_at >= ?
                  AND highlights_json IS NOT NULL
                  AND highlights_json != '[]'
3. if pool is empty:
       write tldr="Nothing noteworthy in the last 24h.", top_items=[],
       status='ready', return
4. profile = users.interest_profile_md
5. payload = build prompt with pool + profile
6. call LLM, parse JSON
   - validate: drop hallucinated video_ids not in pool
   - if < 3 items remain, retry once with explicit id whitelist in prompt
7. if json invalid after one retry → status='failed', store error, stop
8. persist tldr, top_items_json, item_count = len(pool), status='ready'
```

Re-uses the existing worker-resurrection pattern (see
`docs/superpowers/specs/2026-05-15-worker-resurrection-design.md`) so
a restart mid-job doesn't lose the row.

### Consolidate job

Triggered by every `POST /feedback`. Single-flight per Profile: a
new feedback while a consolidate is running gets queued, not run in
parallel. Failure leaves the profile unchanged and logs to
`log_buffer`.

## Error handling

### Highlights generation

* LLM returns non-JSON → parse summary out of free text as today,
  store `highlights_json = NULL`. The item exists but won't reach the
  digest.
* LLM returns malformed highlights (empty strings, > 50 words each) →
  validator drops bad entries; if all are bad, store `[]`.
* Pre-feature items keep `highlights_json = NULL` (lazy backfill,
  decided in brainstorming).

### Feedback API

* Invalid offsets (`start ≥ end`, out of source range) → 422.
* `selected_text` > 1000 chars → 422.
* Cross-Profile access (feedback for a video owned by another
  Profile) → 403.

### Digest job

* Invalid JSON from LLM → one retry with explicit "return valid JSON"
  reminder; second failure → `status='failed'`, Retry button surfaces
  in UI.
* Provider down / 5xx / timeout → `status='failed'`, the next cron
  sweep will *not* retry automatically (the "today already" guard
  applies); user can re-trigger via `POST /digest/generate`.
* LLM hallucinates a `video_id` outside the pool → filter silently;
  if < 3 items remain, retry once with an explicit whitelist.

### Interest profile

* Profile grows > 4000 tokens → consolidate prompt enforces
  compaction; if still > 4000 after that, truncate the oldest portion
  and log a warning.
* Manual edit collides with a background consolidate →
  `interest_profile_version` mismatch → 409 to the slower writer,
  toast asks user to reload.

### UI

* Failed digest in teaser → kompakte Fehlermeldung + Retry button,
  no big modal.
* `digest_enabled = 0` → no teaser; dismissible hint about turning
  it on, shown once per Profile.

## Testing

In-memory SQLite + sqlite-vec, LLM calls mocked — existing CI pattern.

### Unit

* `repos/feedback.py` — CRUD round-trips, filters by `user_id`/`video_id`,
  offset preservation.
* `repos/digests.py` — CRUD, "today's digest exists" query, status
  transitions.
* `services/digest.py` — pool filters empty highlights; empty pool
  produces TL;DR without LLM call; hallucinated `video_id` filtered;
  LLM failure → `status='failed'`; idempotence on double trigger.
* `services/interest_profile.py` — first feedback builds profile;
  later feedback merges; consolidate failure leaves profile
  unchanged; token soft-limit honoured; optimistic-locking conflict
  detected.
* `services/summarizer.py` — prompt embeds profile when present;
  highlights parsed and stored; missing key → `NULL`; explicit `[]`
  stored as `[]`.

### Route (FastAPI TestClient)

* `POST /feedback` — happy path; 422 on bad offsets; 422 on long
  text; 403 on cross-Profile.
* `DELETE /feedback/<id>` — ownership check.
* `GET /digest`, `GET /digest/<id>` — own only, 404 on foreign,
  HTMX poll while `pending`.
* `POST /digest/generate` — creates a row, returns ID, respects
  `period_hours`.
* `GET /` — teaser variants: ready, pending, failed, disabled.

### Integration

* End-to-end smoke: URL submit → mocked summarize returns
  `summary + highlights` → row persisted → cron trigger → digest
  contains the item.
* Feedback loop: three feedbacks → consolidate (mocked) → next
  summarize call's prompt embeds the new profile.

### Migration

Test that running the migration twice on the same DB is a no-op (the
column-introspection guard works), and that an existing pre-feature
DB with populated `videos` rows still works — those rows keep
`highlights_json = NULL` and are silently excluded from digests.

## Out of scope (v1)

YAGNI, all easy to add later:

* Auto-backfill of pre-feature summaries (lazy backfill chosen)
* Weekly / period-N digest
* Push notifications or e-mail delivery
* Embedding-based pre-ranking layer
* Sharing a digest with other Profiles or as a public URL
* Chat-session snippets as digest source
* Tag-based filters within a digest
* Dedicated mobile-only selection UI (we'll keep the popover
  touch-tolerant but not build a separate layout)

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Highlights inflate every summarize call's output tokens | Cap to 5 entries × ~30 words; allow empty array; one prompt change, no extra call |
| Interest profile grows unbounded | Token soft-limit in consolidate prompt; hard truncation at 4000 |
| Selection offsets drift after re-summarize | Store `selected_text`; fall back to `indexOf` on restore; if not found, keep the feedback row but skip visual restore |
| LLM hallucinating `video_id`s | Validate against the actual pool; retry with explicit whitelist if too many drop out |
| Cron + on-demand racing | "Today already exists in (pending,ready)" guard on insert |
| Per-Profile data leakage | All queries scoped by `user_id`; route handlers re-check ownership; tests cover the foreign-Profile case |

## Future hooks the design preserves

* Weekly digest = same job with `period_hours=168`, separate cron entry.
* E-mail = a renderer that takes a `Digest` row and ships it via an
  outbound adapter; nothing in the data model changes.
* Embedding pre-rank = a layer between pool collection and LLM call,
  scoring items against the profile's embedding before sending the
  top-N to the LLM.
