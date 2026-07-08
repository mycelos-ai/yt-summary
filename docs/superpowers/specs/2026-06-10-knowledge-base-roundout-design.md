# Knowledge-Base Round-Out: Export, Podcast Feed, Related Items, Bookmarklet

**Status:** Draft — design phase
**Date:** 2026-06-10

## Goal

yt-summary has become a personal knowledge store, but today the
knowledge only flows **in**. This spec rounds the product out along
three axes, plus a short list of hardening fixes surfaced by the
2026-06-10 system review:

1. **Get knowledge out** — Markdown/JSON export (Part A) and a
   personal podcast RSS feed of the existing TTS renderings (Part B).
2. **Connect knowledge** — "Related items" on every detail page and a
   cross-item synthesis ("ask my library") built on the embeddings we
   already store (Part C).
3. **Lower the entry barrier on desktop** — a one-click bookmarklet
   (Part D).
4. **Hardening fixes** (Part 0) — small, independent, must-do items.

Each part is independently shippable. Suggested order:
Part 0 → A → D → C → B (hardening first, then the cheapest
user-visible wins).

## Terminology

As in earlier specs: every "user" is a Netflix-style **Profile**;
`user_id` stays in code/schema. Export, podcast feed, related items,
and synthesis are all scoped to the active Profile's items.

---

## Part 0 — Hardening fixes (must-do)

These come out of the 2026-06-10 review. None changes behaviour for a
legitimate user; all are small and individually testable.

### 0.1 Constant-time API-key comparison

`app/services/auth.py` compares the SHA-256 hex of the presented token
against the stored hash with `!=`. Replace with
`hmac.compare_digest()`. Practically hard to exploit (both sides are
already hashes), but the hardened form costs one line.

### 0.2 `| tojson` instead of `| safe` for JSON in `<script>` blocks

Three templates embed server-built JSON with `| safe`:

* `video_detail.html` — `feedbacks_json`
* `digest/show.html` — `feedbacks_json`
* `audio_modal.html` — `done_keys_json`

`json.dumps` does **not** escape `</script>`. `selected_text` in the
feedback rows is user-selected text *from an LLM-generated summary* —
i.e. from content that can be prompt-injected by a video uploader or
newsletter sender. A summary containing `</script><script>…` would
break out of the script block and execute. Fix: render these values
with Jinja's `| tojson` filter (which escapes `<`, `>`, `&` as unicode
escapes) and drop the manual `json.dumps` in the routes where the
template can serialize directly. `{{ video.id | tojson }}` on the same
page already does it right — make the rest consistent.

Regression test: a feedback row whose `selected_text` contains
`</script><script>window.__pwned=1</script>` must render without an
unescaped `</script>` in the page source.

### 0.3 Couple the MCP host-check default to API-key presence

`app/routes/mcp.py` disables FastMCP's DNS-rebinding protection by
default (`YTS_MCP_DISABLE_HOST_CHECK=1`), justified by "no key → no
access". But the out-of-the-box state is *no API key configured*, i.e.
auth disabled — the two defaults compose into: a malicious website can
DNS-rebind to `http://<lan-host>:8200` and drive the MCP/REST surface
from a victim's browser.

Fix: make the default dynamic — host-check stays **enabled** while no
API key is configured, and is relaxed once a key exists (the key then
gates every request anyway). The explicit env var keeps overriding in
both directions. Update the boot warning in `main.py` to mention this.

### 0.4 Stop echoing stored LLM API keys into the edit form

The "Configured models" edit form returns `row.api_key` in plaintext
to the browser (`routes/settings.py`). The Whisper card already does
this right: a `has_key` boolean plus a write-only field ("leave blank
to keep"). Apply the same pattern to the model form. Additionally, add
a README note that `data/` contains all secrets (LLM keys, IMAP
password, YouTube cookies) in plaintext and should be treated
accordingly (backups, permissions).

---

## Part A — Export (Markdown / JSON)

### What it is

Get summaries (and optionally transcripts) out of yt-summary as plain
files — Obsidian-friendly Markdown with YAML frontmatter, or JSON for
programmatic use. Per-item and bulk.

### Per-item export

* **Web UI:** an "Export ⬇" affordance on the video detail page with
  two choices: Markdown, JSON.
* **Routes:**
  * `GET /v/{video_id}/export.md` — cookie/profile scoped (web UI).
  * `GET /api/v1/videos/{video_id}/export?format=md|json` — API-key
    gated, for scripts and MCP hosts.

**Markdown shape** (Obsidian-compatible):

```markdown
---
title: "…"
source_url: "https://youtu.be/…"
kind: youtube            # youtube | web | email
created: 2026-06-10
summary_model: "anthropic/claude-sonnet-4-6"
language: en
tags: [ai, agents]
playlists: ["AI", "Long-form interviews"]
duration_seconds: 3841
---

# {title}

{summary markdown verbatim}
```

Optional query params: `?transcript=1` appends the transcript under a
`## Transcript` heading; `?highlights=1` appends the structured
highlights as a list. Defaults off — the summary is the knowledge
artifact.

Inline `[MM:SS](#t=SECONDS)` links are rewritten on export to absolute
YouTube deep links (`https://youtube.com/watch?v=…&t=SECONDSs`) so
they stay clickable outside the app. Web/email items have none.

**JSON shape:** the existing `VideoResource` from `services/api.py`
plus `summary`, `transcript` (opt-in), `highlights`, and the feedback
rows for the requesting profile. One item = one self-contained
document.

### Bulk export

* **Route:** `GET /export.zip` (web) and `GET /api/v1/export`
  (API-key) with filters: `tag`, `playlist_id`, `kind`,
  `since`/`until` (ISO dates), `format=md|json`. No filter = the whole
  library of the active Profile.
* **Output:** a streamed ZIP. One file per item, named
  `YYYY-MM-DD-<slug-of-title>-<short-id>.md` (slug ASCII-folded,
  short-id suffix guarantees uniqueness). A `manifest.json` at the
  root lists every entry with id, title, url, and file name.
* Streaming matters: a Pi should not buffer a multi-hundred-item
  library in memory. Build entries one at a time
  (`zipfile.ZipFile` over a spooled temp file is fine at this scale).
* **UI:** an "Export" card on `/settings` with the filter dropdowns
  and a download button.

### Architecture

`routes/export.py` → `services/export.py` (pure functions:
`render_item_md(video, tags, playlists, …) -> str`,
`export_filename(video) -> str`) → existing repos. No schema changes.
The service functions are pure-text builders → easily unit-tested
against fixture videos.

### Out of scope

Automatic sync to external tools (Obsidian vault watching, Notion
API), import from export files, chat-history export.

---

## Part B — Personal podcast RSS feed

### What it is

The TTS pipeline already renders summaries/transcripts to MP3
(`tts_jobs` with `status='done'`, `audio_path`, `duration_seconds`).
A standard RSS 2.0 + iTunes-namespace feed over those renderings turns
yt-summary into "my watch-later list as a podcast" in any podcast app.

### The token problem

Podcast clients fetch plain URLs — no Bearer headers, no cookies. The
feed therefore uses a **capability URL** with a per-profile token:

* `users.podcast_token TEXT` (nullable) — generated on demand from
  `/settings` ("Enable podcast feed"), revocable/regeneratable there.
  32 chars urlsafe, same generator family as the API key.
* Stored **in plaintext** (deliberate deviation from the API-key-hash
  pattern): the settings page must be able to re-display the feed URL,
  and the token gates a read-only, audio-only surface. Regeneration
  invalidates old URLs. Document the tradeoff in the settings UI
  ("anyone with this URL can listen to your renderings").

### Routes

* `GET /podcast/{token}/feed.xml` — the feed. 404 on unknown token
  (no information leak about whether feeds exist).
* `GET /podcast/{token}/episode/{job_id}.mp3` — the enclosure.
  Token-gated, reuses the existing `_mp3_path` traversal guard from
  `routes/audio.py`. Must support HTTP Range requests (podcast apps
  seek); FastAPI's `FileResponse` handles this.

### Feed contents

* Channel: title `"yt-summary — {profile name}"`, link = app root,
  description, language from settings.
* Items: every `done` TTS job belonging to the profile's videos,
  newest `finished_at` first, capped at the most recent 100.
  * `title`: video title (+ ` — transcript` suffix when
    `source='transcript'`, and a language marker when translated).
  * `description`: first ~500 chars of the summary, plaintext.
  * `enclosure`: the episode URL, `audio/mpeg`, byte length via
    `stat()`.
  * `guid`: `yts-tts-{job_id}` (stable, non-URL).
  * `itunes:duration` from `duration_seconds`.
* Cover art: the app logo; per-episode `itunes:image` from the video
  thumbnail where present.

### Architecture

`routes/podcast.py` → `services/podcast.py` (pure
`build_feed_xml(profile, jobs, base_url) -> str`) → `tts_jobs_repo`
(new `list_done_for_user(db, user_id, limit)` join over
`videos.user_id`). Base URL derives from the request (proxy headers
are already honoured), so the feed works behind HTTPS proxies.

Schema change: one nullable column on `users` via the existing
`_ensure_column` migration helper.

### Out of scope (but not precluded)

Auto-rendering every new summary to audio ("subscribe and it just
appears") — a follow-up that only needs a per-profile toggle and a
scheduler hook; the feed shape above already accommodates it.
Per-playlist feeds.

---

## Part C — Related items + library synthesis

### C.1 Related items on the detail page

The 384-d summary embeddings already exist in `video_embeddings`; they
are only used for search today. Reuse them:

* On `GET /v/{id}`, when the video has an embedding, run the existing
  `search_by_summary_vector` KNN with the item's own stored vector
  (one extra `SELECT` to fetch it — add
  `get_summary_embedding(db, video_id)` to the embeddings repo).
* Filter: exclude self, exclude other profiles' copies of the same
  `youtube_id`/URL, keep `distance < 0.75` (tune empirically), take
  top 5.
* Render as a compact card strip "Related in your library" under the
  summary. Load it as an HTMX lazy fragment
  (`GET /v/{id}/related-fragment`) so the detail page render path
  stays untouched and the KNN cost is off the critical path.

No schema changes. Tests: repo-level KNN with fixture vectors;
route-level fragment rendering with a stubbed embedding.

### C.2 Library synthesis ("ask my library")

A question answered **across** items, with citations back into the
library — the digest machinery generalized from "last 24 h" to "this
question":

* **Flow:** user asks e.g. *"What have I saved about agent
  evaluation?"* → hybrid search (existing FTS + vector RRF fusion, as
  on home) selects the top N items (default 8) → their **summaries**
  (not transcripts — token budget) are packed into one LLM call →
  the answer is Markdown with `[title](/v/{id})` source links,
  rendered through the existing `render_markdown` (html=False).
* **Persistence:** syntheses are knowledge artifacts, not throwaway
  chat. New table:

  ```sql
  CREATE TABLE syntheses (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      query TEXT NOT NULL,
      result_md TEXT,
      source_ids_json TEXT NOT NULL,   -- ordered video ids used
      status TEXT NOT NULL CHECK(status IN ('pending','ready','failed')),
      error TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
  );
  ```

* **Routes/UI:** `GET /ask` (question box + archive list, mirroring
  `/digest`), `POST /ask` enqueues and HTMX-polls like the digest
  flow. `GET /ask/{id}` is the permalink.
* **Surfaces:** REST `POST /api/v1/ask`, and an MCP tool
  `ask_library(question)` — arguably the highest-value MCP addition,
  since it turns any MCP host into a front-end for the whole library.
* **Prompt:** system prompt instructs: answer only from the provided
  summaries, cite every claim with the item link, say explicitly when
  the library doesn't cover the question. The profile's interest
  profile is *not* injected here — a direct question shouldn't be
  biased.
* **Execution:** run through the existing summary worker as a new job
  kind (or a small dedicated asyncio task like the digest scheduler) —
  must not block a request handler, LLM calls take tens of seconds.

### Out of scope

Conversational multi-turn over the library (the per-video chat
pattern could be lifted later), clustering/topic-map visualizations.

---

## Part D — Bookmarklet

### What it is

One click in the desktop browser sends the current page (YouTube or
article) into the active profile's queue. Complements the
playlist-on-the-couch flow, which stays the mobile answer.

### Design

* **Settings card "Browser bookmarklet":** renders a draggable
  bookmarklet link with the app's origin baked in at render time
  (derived from the request, so HTTPS/proxy setups get the right
  host):

  ```js
  javascript:window.open(
    'https://<host>/submit?url=' + encodeURIComponent(location.href),
    '_blank', 'noopener,width=480,height=360')
  ```

* **New route `GET /submit?url=…`:** renders a minimal confirmation
  page (small popup-sized layout): the URL, the detected kind
  (YouTube / article via the existing `classify_url`), the active
  profile, and one **"Summarize" button** that POSTs to the existing
  submit handler, then shows "queued ✓" with a link to the detail
  page.

  The confirm click is deliberate: a `GET` that mutates state would be
  a drive-by-submission vector (any page could `<img src=…>` the
  endpoint). GET renders, POST submits — same CSRF posture as the rest
  of the app, one extra click for the user.

* The popup closes itself after the success state (a 2-second
  auto-close with a "keep open" link).

No schema changes, no new auth surface (cookie/profile scoped, same as
the home form). Out of scope: a real browser extension, a PWA
share-target manifest for Android (worth a follow-up spec — the
`/submit` route designed here is exactly what a share target needs).

---

## Testing strategy

Matches house style: no live LLM/network in tests.

* **Part 0:** unit tests per fix (timing-safe compare is behavioural —
  test acceptance/rejection; tojson via template render assertions;
  MCP default via the existing host-check test module).
* **Part A:** pure-function tests for frontmatter/filename/timestamp
  rewriting; route tests for ZIP structure (open the ZIP, assert
  manifest + file count).
* **Part B:** feed XML validated against fixture jobs (parse with
  `xml.etree`, assert enclosure URLs, guids, durations); token 404s;
  Range request smoke test.
* **Part C:** KNN repo tests with packed fixture vectors; synthesis
  service test with a stubbed `_completion`; route polling flow as in
  digest tests.
* **Part D:** route tests: GET renders confirmation with escaped URL,
  POST enqueues, invalid/non-http URL shows inline error.

## Rollout

Five independent PRs in the order 0 → A → D → C → B. Part 0 carries no
feature risk and should land immediately. Parts A and D have no schema
changes. Part C adds one table, Part B one column — both via the
existing idempotent migration helpers in `db.py`.
