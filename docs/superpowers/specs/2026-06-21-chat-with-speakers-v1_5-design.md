# Chat with the Speakers — Merged v1.5 Design (Roleplay Hook + Evidence Dossier)

**Status:** Draft — implementation-ready merge of the two prior speaker-chat
specs; revised after design review (chat-thread NULL-uniqueness, candidate-based
source discovery, claim attribution metadata, softened persona framing, explicit
`videos` table-rebuild, PR-slicing note)
**Date:** 2026-06-21
**Merges / supersedes:**

- [`2026-06-20-video-speaker-chat-design.md`](2026-06-20-video-speaker-chat-design.md) (the "talk to them in real time" hook)
- [`2026-06-21-speaker-dossiers-and-persona-chat-design.md`](2026-06-21-speaker-dossiers-and-persona-chat-design.md) (the evidence-first dossier model)
- [`2026-06-21-speaker-dossiers-change-log.md`](2026-06-21-speaker-dossiers-change-log.md) (the change rationale)

This spec is the single source of truth going forward. The two specs above
were framed as if in tension — one sold the roleplay, the other pulled toward
sourced evidence. This document reconciles them: **the roleplay hook stays the
product promise, and the evidence dossier is the grounding layer that makes it
honest and defensible.** They are joined by attributing claims to named
speakers *at extraction time* — the bridge neither prior spec built.

## Current state (verified against the codebase)

The feature is **spec-only — nothing is built.** As of 2026-06-21 `main` holds
only `docs(...)` commits. Verified:

- `chat_messages` ([app/db.py](../../../app/db.py)) has only
  `id, user_id, video_id, role, content, created_at` — no `speaker_id`/`thread_id`.
- `videos` has `kind IN ('youtube','web','email')`, no `channel_id`.
- None of `services/speaker_chat.py`, `services/speakers.py`,
  `services/show_match.py`, `app/data/*speakers*.json`, `templates/speaker.html`
  exist.
- The remote branch `origin/claude/video-speaker-chat-9to030` is also docs-only
  and *older* than `main`.

The existing chat plumbing the feature builds on is real and verified:
`services/chat_core.build_messages` → `services/chat.stream_reply` →
`litellm.acompletion` stream; history via `repos/chat.{append,history}`. The
pipeline's best-effort enrichment posture (`pipeline._store_related_links`,
which swallows exceptions and never fails the job) is the template for the new
extraction step. Local embeddings + sqlite-vec already ship
(`services/embeddings_local.py`, `repos/embeddings.py`).

## Positioning and the resolved spagat

**Product promise (the hook, retained):** "Chat with {Name}" — you talk to the
person, in their voice, in real time. This sells the experience.

**How the spagat is resolved (the honesty layer):** every *assertible* thing the
persona says is anchored to **attributed claims** — sentences an LLM pass has
assigned, by name, to that speaker, each carrying a source + timestamp/offset.
The current episode's full transcript is given to the persona only as **context
for conversational flow and style**, never as the source of "they said X." A
hard prompt rule forbids putting other speakers' (or unattributed) words in the
persona's mouth; when attribution is unclear, the persona says so.

This is the **hybrid** the user chose: attributed claims are the citable
foreground (retrieved by embedding relevance), the transcript is style/flow
context only.

> **Guiding principle: roleplay is the *experience*, not the *data layer*.**
> The UI says "Chat with Chamath"; the reply feels like Chamath — fast, direct,
> pointed. The dossier and claims run underneath as grounding, and the
> track-record peek shows it isn't just theatre: there are sources beneath it.
> The product is an entertaining persona interface with a defensible memory —
> not a dry evidence-QA tool, and not ungrounded celebrity cosplay.

### Non-negotiable rules (carried from the evidence-first spec)

1. **No seeded positions.** The app may seed *who* a person is and *where* they
   appear. It must never seed *what they believe*.
2. **Claims need evidence** (source + timestamp/offset), start `unreviewed`, and
   are editable/rejectable.
3. **Attribution beats style.** Non-attributable text may become context, never
   a dossier claim.

**Transparency** stays interface-owned: a subtle but clear banner in persona
mode ("⚠️ Simulated — AI impression of {Name} based on your sources, not their
real words"). It carries the asterisk without interrupting every reply — but the
prompt does **not** depend on the banner as its only safety boundary.

## Terminology

- **Profile** — the app's Netflix-style user (every "user" is a profile).
- **Known show** — a seeded or user-added show rule used to detect speakers from
  metadata.
- **Known speaker** — a seeded/user-added person record holding identity +
  presentation metadata only (no positions).
- **Speaker** — a profile-local person entity; may link to a known speaker but
  stays profile-editable.
- **Source link (appearance)** — a link between a speaker and one existing
  library item (`videos` row; `kind IN ('youtube','web','email','text')`).
- **Claim** — a sourced, attributed statement/position/prediction by a speaker.
- **Dossier** — the accumulated source links + claims for a speaker.
- **Persona reply** — a simulated, in-character answer grounded in the current
  source plus the sourced dossier, in the viewer's language.
- **Activation** — the user's explicit opt-in to chat with a speaker; it triggers
  a library-wide backfill of that speaker's claims.

## Architecture: activation drives extraction

The core mechanism (user's chosen Approach A). Speaker **activation** is the
cost gate; one extraction service serves two triggers.

- **Activate** a speaker → set `speakers.is_active = 1` and enqueue a **backfill
  job** (existing job infrastructure) that walks the library for that speaker's
  sources and extracts claims. The page is populated by the job; it is *not*
  "instant," but runs in the background like the pipeline jobs.
- **Pipeline piggyback** (forward) → when a new episode from a recognized show is
  processed, claims are extracted for **all already-active speakers** in that
  episode in **one** LLM call. No active speakers in the episode → no expensive
  call.

Both triggers call the same `speaker_claims.extract_claims_for_source`. "One
call, many speakers" falls out naturally because the call takes a list of
expected speakers and the LLM attributes each claim by name.

## Data model

All tables in [app/db.py](../../../app/db.py), created idempotently like the
existing schema. Adapted from the evidence-first spec, with **two additions**
for the activation model (`speakers.is_active`, `speakers.avatar_photo_path`)
and a claims **embedding** table (the user chose embedding-ranked retrieval for
v1.5).

### `known_shows` — seeded + user-maintained show rules

```sql
CREATE TABLE IF NOT EXISTS known_shows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,                          -- NULL = shipped/seeded; set = user-added
    name TEXT NOT NULL,
    channel_id TEXT,                          -- primary match signal
    title_pattern TEXT,                       -- secondary substring/regex match
    description_pattern TEXT,
    hosts_json TEXT NOT NULL DEFAULT '[]',    -- known host names
    guest_rule TEXT,                          -- enumerated parser tag (see below); NULL = fixed cast
    enabled INTEGER NOT NULL DEFAULT 1,
    seed_version INTEGER NOT NULL DEFAULT 1,  -- shipped rows only; gates idempotent re-seed
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_known_shows_channel ON known_shows(channel_id);
```

`guest_rule` is a **tiny enumerated parser, not free code** — e.g. `after:with `
(Diary of a CEO: guest after "with "), `before:: ` (Lex:
`"{Guest}: {Topic} | Lex Fridman Podcast #N"`), or NULL for a fixed cast.
Enumerated keeps user-added shows safe (no arbitrary regex execution) and the
settings form a simple dropdown.

### `known_speakers` — seeded directory metadata only (NO positions)

```sql
CREATE TABLE IF NOT EXISTS known_speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,            -- normalised: lower, punct/space collapsed
    role TEXT,
    known_shows TEXT,                         -- e.g. "All-In; <other shows>"
    avatar_id TEXT,
    style_note TEXT,                          -- SPEAKING STYLE ONLY, e.g. "blunt, fast-moving investor tone"
    seed_version INTEGER NOT NULL DEFAULT 1
);
```

`style_note` must describe speaking style only. It must **not** encode claims
("supports X", "believes Y").

### `speakers` — profile-local identity

```sql
CREATE TABLE IF NOT EXISTS speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    known_speaker_id INTEGER REFERENCES known_speakers(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,                   -- identity anchor
    role TEXT,
    avatar_id TEXT,                           -- curated cartoon avatar (services/avatars.py)
    avatar_photo_path TEXT,                   -- NEW: optional user-uploaded real photo (overrides avatar_id when set)
    style_note TEXT,
    is_active INTEGER NOT NULL DEFAULT 0,     -- NEW: opt-in; drives backfill + pipeline piggyback
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, name_key)
);
```

`UNIQUE(user_id, name_key)` is the identity anchor: detecting the same person
twice resolves to the same row. Alias drift ("Chamath" vs "Chamath
Palihapitiya") is accepted and handled by **manual merge**, not aggressive
auto-clustering.

### `source_speakers` — appearance / speaker↔library-item link

```sql
CREATE TABLE IF NOT EXISTS source_speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,  -- polymorphic via videos.kind
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    role TEXT,
    detection_source TEXT NOT NULL CHECK(detection_source IN ('show_rule','manual','llm')),
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_id, speaker_id)
);
CREATE INDEX IF NOT EXISTS idx_source_speakers_source ON source_speakers(source_id, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_source_speakers_speaker ON source_speakers(speaker_id);
```

`source_id` points at `videos(id)` because that is already the app's polymorphic
content table. A web article, newsletter email, YouTube video, or pasted-text
item is stored **once** as a library item (summarized + embedded by the normal
pipeline) and then linked here. Speaker tables never duplicate source bodies.

**`source_speakers` holds only CONFIRMED links.** It is what the backfill and the
dossier read. Auto-discovered *guesses* live in a separate candidates table and
are never silently promoted — promotion is an explicit user action. This keeps
rule #3 (attribution beats style) intact: a fuzzy full-text hit must not pour a
stranger's statements into the dossier.

### `speaker_source_candidates` — suggested-but-unconfirmed source links

```sql
CREATE TABLE IF NOT EXISTS speaker_source_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    signal TEXT NOT NULL CHECK(signal IN ('email_from','title_match','fulltext','embedding')),
    score REAL,                              -- signal strength, for ranking the suggestions
    state TEXT NOT NULL CHECK(state IN ('pending','confirmed','dismissed')) DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(speaker_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_speaker_source_candidates_speaker
    ON speaker_source_candidates(speaker_id, state, score);
```

Discovery signals by source kind, weakest-last:
- `youtube` → handled by `show_match` directly (becomes a confirmed
  `source_speakers` link via `show_rule`, not a candidate).
- `email` → `email_from` (the newsletter sender) is a reasonably strong signal.
- `web` / `text` → only `title_match` / `fulltext` / `embedding`, all weak and
  false-positive-prone, so they enter as **candidates only**.

Confirming a candidate (`POST /speaker/{id}/candidates/{cid}/confirm`) creates the
`source_speakers` row (`detection_source='manual'`) and may trigger extraction;
dismissing sets `state='dismissed'`.

### `speaker_claims` — evidence-backed, attributed track record

```sql
CREATE TABLE IF NOT EXISTS speaker_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    source_speaker_id INTEGER REFERENCES source_speakers(id) ON DELETE SET NULL,
    claim TEXT NOT NULL,                      -- paraphrased position in their words
    topic TEXT,                              -- short topical tag, for grouping
    evidence_text TEXT,                      -- the supporting excerpt
    evidence_start_s INTEGER,                -- video timestamp (jump-back)
    evidence_end_s INTEGER,
    text_start_offset INTEGER,               -- article/email/text offset
    text_end_offset INTEGER,
    confidence REAL,                         -- overall claim quality (paraphrase fidelity)
    extraction_method TEXT NOT NULL CHECK(extraction_method IN ('metadata','llm','manual')),
    -- HOW the claim was tied to THIS speaker (separate from extraction quality).
    -- Drives how cautiously the persona may speak it (see speaker_chat.py).
    attribution_method TEXT CHECK(attribution_method IN
        ('explicit_name','speaker_marker','metadata_context','llm_inferred','manual')),
    attribution_confidence REAL,             -- 0..1 confidence in the speaker assignment
    attribution_reason TEXT,                 -- short why, for the review UI ("named in prior sentence")
    review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','accepted','rejected')) DEFAULT 'unreviewed',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_speaker_claims_speaker ON speaker_claims(speaker_id, created_at);
CREATE INDEX IF NOT EXISTS idx_speaker_claims_source ON speaker_claims(source_id);
```

Claims are re-derivable: on reprocess, delete this source's rows for the speaker
and re-insert (forward-only, no stale duplicates).

### `speaker_claim_embeddings` — claim vectors for relevance retrieval

A sqlite-vec table analogous to the existing summary-embedding table, populated
when a claim is extracted (via `services/embeddings_local.py`). Retrieval = KNN
of the viewer's question against **this speaker's** claims, top-K capped. This
is what makes contradiction-surfacing ("three episodes ago you argued the
opposite") robust rather than keyword luck. Recency + topic-text match is the
**fallback** when a claim has no embedding yet or the embedding backend is off —
best-effort, never a hard error.

### `chat_threads` + `chat_messages.thread_id` — explicit thread scoping

Avoids `NULL`/sentinel video ids for whole-speaker chats.

```sql
CREATE TABLE IF NOT EXISTS chat_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    scope TEXT NOT NULL CHECK(scope IN ('source','source_speaker','speaker')),
    source_id TEXT REFERENCES videos(id) ON DELETE CASCADE,
    speaker_id INTEGER REFERENCES speakers(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A table-level UNIQUE(user_id, scope, source_id, speaker_id) is WRONG here:
-- SQLite treats every NULL as distinct, so a 'speaker'-scope thread (source_id
-- NULL) or a 'source'-scope thread (speaker_id NULL) could be duplicated. Use
-- per-scope PARTIAL unique indexes, where the NULL column is excluded from the
-- key:
CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_threads_source
    ON chat_threads(user_id, source_id) WHERE scope = 'source';
CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_threads_source_speaker
    ON chat_threads(user_id, source_id, speaker_id) WHERE scope = 'source_speaker';
CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_threads_speaker
    ON chat_threads(user_id, speaker_id) WHERE scope = 'speaker';

ALTER TABLE chat_messages ADD COLUMN thread_id INTEGER REFERENCES chat_threads(id);
```

- `scope='source'` → today's "chat with the video" thread (behaviour unchanged).
- `scope='source_speaker'` → a speaker thread **on one episode**.
- `scope='speaker'` → the whole-dossier chat from the speaker page.

Migration backfills one `source` thread per existing `(user_id, video_id)` chat
history. `chat_messages.video_id` stays for compatibility until the repo layer is
fully thread-based. `repos/chat.{append,history}` gain an optional `thread_id`
param; existing default call sites are untouched.

### `videos.channel_id` — the primary show-match signal

A nullable `channel_id TEXT`, NULL on existing rows. **Added in the same
`videos` table-rebuild as the `kind='text'` CHECK widening above** — not as a
standalone `ALTER` — so the heavier rebuild touches `videos` exactly once.
yt-dlp's `info` already carries `channel_id`; `services/youtube.py` (which today
reads only `id`/`title`/`description` around line 97) captures it during fetch.
Pre-existing rows stay NULL until re-fetched and match shows by `title_pattern`
only.

### `videos.kind` gains `'text'` — pasted-text library items

```sql
-- Target CHECK: kind IN ('youtube','web','email','text')
```

**Migration is not a plain `ALTER`.** SQLite cannot alter a column's
`CHECK(kind IN (...))` in place. This needs a **table-rebuild migration** —
exactly the pattern `db.py` already uses for `settings`/`feedback` (create
`videos_new` with the widened CHECK, `INSERT … SELECT` the existing rows, drop
the old table, `RENAME videos_new TO videos`, recreate indexes/foreign keys).
Because this is a heavier, riskier rebuild than an `ADD COLUMN`, the new
`videos.channel_id` column should be added in the **same** rebuild (one migration
touches `videos` once) rather than as a separate `ALTER`. Run it behind the same
idempotent guard the other rebuilds use (detect the missing kind/`channel_id`
via `_table_columns` / a CHECK probe), and cover "runs twice cleanly" in tests.

A new general **"Paste text"** add-tab (see UI) creates a normal library item
with `kind='text'`. Mechanically this is "`email` without mailbox parsing": the
body is already present (no URL fetch), it runs the standard summarize +
embedding path and gets a Pexels thumbnail like `web`/`email`. Once it is a
normal library item, it links to speakers via `source_speakers` like any other
source — this is what lets a transcribed interview that exists nowhere as a URL
enter the dossier.

## Services (`app/services/`)

Each has one clear purpose and docks onto a verified existing pattern.

### `show_match.py`

`identify_from_metadata(db, video) -> list[DetectedSpeaker]`. Purely
deterministic, **no LLM, no transcript**: matches `known_shows` (+ user shows)
on `channel_id` (primary) / `title_pattern` (secondary), returns hosts + guests
parsed via `guest_rule`. Creates **no** claims — only "who is likely present."

### `speakers.py`

Identity + lifecycle:

- `resolve_speaker(db, *, user_id, name, role)` — upsert on `(user_id,
  name_key)`; link to `known_speakers` on a name_key hit.
- appearance linking into `source_speakers`.
- `merge(a, b)` — re-point `source_speakers`, `speaker_claims`, `chat_threads`
  from B to A, delete B. The alias-drift fix.
- source linking from the library (search across all `kind`).
- `activate`/`deactivate` — set `is_active`, enqueue/cancel the backfill job.

If this grows too large, `activate`/backfill is the natural extraction seam
(already split into `speaker_backfill.py`).

### `speaker_claims.py`

The extraction service — **one entry point for both triggers** (backfill +
piggyback):

`extract_claims_for_source(db, source, speaker_ids, model, api_key, base_url)`

- **Attributed**: the LLM pass is given the list of expected speakers and assigns
  each claim to one by name, with evidence text + timestamp/offset. Statements it
  cannot confidently attribute become **no** claim.
- Multiple active speakers in one source → **one** LLM call.
- Robust JSON parse via `highlight_parser._extract_json_blob` (the path
  related-links/highlights already use). Returns `[]` on garbage. **Never raises.**
- Claims default `unreviewed`. Replace-on-reprocess. Embeds new claims
  best-effort.
- Also exposes the retrieval used by the persona prompt: KNN over
  `speaker_claim_embeddings` for this speaker, capped; recency/topic fallback.

### `speaker_chat.py`

The persona reply. Mirrors `chat.stream_reply` (same LiteLLM streaming kwargs,
reuses `chat_core.build_messages`); only the system prompt differs. Prompt shape:

```text
You are a clearly simulated, in-character perspective of {name}{role_clause},
talking with a viewer. Speak in the first person, in their voice — match their
tone, rhetorical habits, bluntness or warmth. You are NOT the real {name} and
must not claim to be.

LANGUAGE: reply in the SAME language as the viewer's latest message, regardless
of the language of the source or the dossier.

GROUNDING:
- Anchor everything assertible in the ATTRIBUTED CLAIMS and the attributed
  excerpts below. These are attributed claims extracted from the viewer's
  sources, each with evidence — paraphrases of positions, not verbatim quotes.
- Each claim is tagged with how confidently it was attributed to {name}. For
  claims marked low-confidence or "inferred", speak more tentatively ("I think
  I've argued…", "as I recall…") rather than asserting them flatly.
- The CURRENT SOURCE TRANSCRIPT is context for flow and style ONLY. Do NOT
  present things from it as {name}'s statements unless they are attributed.
- NEVER put other speakers' words, or unattributed words, in {name}'s mouth. If
  attribution is unclear, say the source is ambiguous.
- If the viewer points out a contradiction across sources, engage honestly and
  cite the sources.
- NEVER invent specific facts, numbers, quotes, or beliefs.
- Don't break character to disclaim you're an AI — the interface already says so.

STYLE NOTE: {style_note}
{seed_block}                 # only on jump-in: "[12:04] '…the quote…'"

ATTRIBUTED CLAIMS (extracted from {name}'s sources, each with its source and an
attribution-confidence tag):
{claims}                     # embedding-ranked slice; each line carries attribution_method/confidence

CURRENT SOURCE CONTEXT (style/flow only — not a source of {name}'s claims):
{episode_context}
```

`stream_speaker_reply(*, speaker, source_context, claims, history, user_message,
seed_ts, seed_quote, model, api_key, base_url)` → async token iterator, identical
mechanics to `stream_reply`.

### `speaker_backfill.py`

Thin job logic for activation. It calls `extract_claims_for_source` per source
over the speaker's **confirmed** sources only — the union of:
- existing `source_speakers` links, and
- show-match hits over existing YouTube videos (which it first *confirms* as
  `source_speakers` rows).

It **never** reads `speaker_source_candidates` — unconfirmed guesses are out of
the dossier by construction. Runs as a background job over the existing job
infrastructure — non-blocking, like the pipeline jobs.

### `speaker_discovery.py`

Separate from the backfill: generates `speaker_source_candidates` for a speaker
by running the per-kind signals (email-from, title/fulltext/embedding). Surfaced
on the speaker page as "possible sources" for the user to confirm or dismiss.
Kept apart from backfill precisely so a weak signal can never auto-populate the
dossier.

### Repos

`repos/speakers.py`, `repos/known_shows.py`, `repos/speaker_claims.py`,
`repos/chat_threads.py`. `repos/chat.py` gains an optional `thread_id` param;
existing default-call sites untouched → existing chat tests stay green.

## Routes (all HTMX fragment swaps; ownership-checked, foreign profile → 404)

### Video speaker — `routes/speakers.py`

- `POST /v/{video_id}/speakers/detect` — run detection on demand (older videos /
  re-detect); resolve + link; return refreshed chip fragment.
- `POST /v/{video_id}/speakers` — add a `manual` appearance.
- `POST /v/{video_id}/speakers/{speaker_id}/unlink` — remove an appearance
  (global speaker + dossier kept).
- `POST /v/{video_id}/speaker/{speaker_id}/chat` — the persona turn. Loads the
  `source_speaker` thread, current source context + capped (embedding-ranked)
  claim slice, streams via `speaker_chat`, persists with `thread_id`. Optional
  `seed_ts`/`seed_quote`.

### Speaker page + dossier — `routes/speakers.py`

- `GET /speaker/{id}` — the full page (see UI).
- `POST /speaker/{id}/edit` — name / role / avatar / **photo upload** /
  style_note.
- `POST /speaker/{id}/activate` + `.../deactivate` — opt-in; `activate` enqueues
  the backfill job and returns a "backfill running…" fragment.
- `POST /speaker/{id}/merge` — merge speakers.
- `POST /speaker/{id}/sources/link` — link an existing library item (search all
  `kind`); creates a confirmed `source_speakers` row.
- `POST /speaker/{id}/sources/{source_id}/extract` — (re-)extract claims for one
  source on demand.
- `GET /speaker/{id}/candidates` — the discovered "possible sources" list.
- `POST /speaker/{id}/candidates/{cid}/confirm` — promote a candidate to a
  confirmed `source_speakers` link (then extractable).
- `POST /speaker/{id}/candidates/{cid}/dismiss` — mark a candidate dismissed.
- `POST /speaker/{id}/claims/{claim_id}/edit` + `.../review` — correct a claim /
  set `accepted`/`rejected`.
- `POST /speaker/{id}/chat` — the whole-dossier persona turn (`speaker` thread;
  grounding = dossier, not one episode).

(Whether the persona chat lives in `routes/speakers.py` or its own
`routes/speaker_chat.py` is decided at build time by file size; functionally
identical.)

### Settings — `routes/shows.py`

- `GET/POST /settings/shows`, `POST /settings/shows/{id}/edit|toggle` — maintain
  your own show rules on top of the seeded ones; seeded rows read-only except
  per-profile disable.
- `GET/POST /settings/speakers` — your own speaker entries; seeded read-only
  except disable.

### Add (pasted text) — `routes/videos.py`

The existing `/videos` add handler gains a pasted-text branch: accept raw text
(instead of a URL), create a `kind='text'` item directly (analogous to
`_import_web` but with no fetch), enqueue the normal pipeline job.

## UI (`video_detail.html`, `speaker.html`, `_add_overlay.html`, partials)

### Video detail — inside the existing chat section

- **Speaker picker + mode switch** at the top: `Chat with: [ the video ]
  [ 🟦 Chamath ] [ 🟩 Jason ] … [ ⚙ ]`. **Every speaker gets its own chip** (no
  host/guest reduction); chip carries its avatar (cartoon or uploaded photo) and
  its name deep-links to `/speaker/{id}`. Chips wrap to multiple rows.
- **Inactive chips** are visible (you see who was detected) but clicking opens an
  "Activate {Name}? We'll search your library for what they've said." panel with
  the Activate button — keeping the opt-in deliberate without hiding the speaker.
- **Disclaimer banner** in persona mode (subtle but clear, per Positioning).
  Speaker bubbles tinted with the avatar colour.
- **Track-record peek**: a collapsible "What {Name} has said before" list beside
  the chat, from the claim slice, each line linking back to its source +
  timestamp — the visible evidence behind the roleplay.
- **Jump-in**: a small `💬 Discuss` per transcript block (seeds
  `seed_ts`/`seed_quote`) and a "💬 Discuss this moment" at the player
  (`getCurrentTime()` + nearest-block lookup over the rendered
  `[data-yt-timestamp]` blocks). **If multiple speakers are present and the
  moment is not attributed, the UI asks which speaker** instead of guessing.

### Speaker page (`speaker.html`) — full

- **Header**: name, role, avatar, style_note, edit action, **activate/deactivate
  toggle** (shows backfill status).
- **Dossier**: claims grouped by `topic`; each with evidence text, source,
  timestamp/link, confidence, review status. **`unreviewed` looks visibly less
  authoritative** than `accepted` (dimmed + "unreviewed" marker) — weak
  extraction must not read as fact.
- **Sources/appearances**: library items where the speaker appears (all `kind`).
  Add-source form: search the existing library, paste a URL into the normal add
  pipeline and link the result, **or** paste raw text (creating a `kind='text'`
  item) and link it — the "transcribed interview as background material" path.
- **Possible sources** (candidates): a separate, visually distinct list of
  auto-discovered guesses (from `speaker_source_candidates`) with confirm/dismiss
  actions — never mixed into the confirmed sources, so the user always knows what
  is grounded vs. merely suggested.
- **Whole-dossier chat**: "Chat with {Name}" across the entire track record.

### Add overlay (`_add_overlay.html`)

A new **"Paste text"** tab beside URL/cURL: a textarea (+ optional title) that
POSTs to `/videos`, creating a `kind='text'` item.

## Pipeline integration (`pipeline.py`)

After summarization, a best-effort `set_step("identifying speakers")`, gated like
the other enrichments (YouTube-kind, transcript present, LLM configured):

1. `identify_from_metadata` → resolve speakers + link `source_speakers`
   (deterministic, no LLM — cheap, runs for every show episode).
2. **Only if active speakers are present in this episode**:
   `extract_claims_for_source` for all active speakers in **one** LLM call (the
   piggyback). No active speakers → no expensive call.

Also: `services/youtube.py` now reads `info["channel_id"]` → `videos.channel_id`.
Any failure in this block leaves the video speaker-less and **never** fails the
job (same posture as `_store_related_links`, verified).

## Testing strategy

House style: no live LLM/network (completions mocked), render via TestClient,
in-memory SQLite + sqlite-vec, no browser.

- **Migration/schema**: all new tables + `chat_messages.thread_id` created
  idempotently; the **`videos` table-rebuild** (widened `kind` CHECK +
  `channel_id`) runs twice cleanly and preserves existing rows/indexes; the
  `chat_threads` **partial unique indexes** actually reject duplicate
  `scope='speaker'` / `scope='source'` threads (the NULL-in-UNIQUE trap); the
  chat-history → `source` thread backfill.
- **`show_match`**: channel-id match, title fallback, disabled seeded rule,
  user-added rule, `guest_rule` parsing.
- **`resolve_speaker` + `merge`**: same name_key across two sources → one row +
  two `source_speakers`; different names → two rows; normalisation; merge
  re-points appearances + claims + threads and deletes B.
- **Attributed extraction** (mocked completion): clean JSON → claims with
  evidence + timestamp/offset + `unreviewed` + `attribution_method`/`confidence`;
  prose-wrapped JSON → still parsed; garbage → `[]`; unattributable statement →
  no claim.
- **Discovery / candidates**: signals produce `pending` candidates (never
  `source_speakers`); confirm promotes to a confirmed link; dismiss sets
  `dismissed`; backfill reads confirmed sources only and ignores candidates.
- **Claim retrieval**: cross-source, capped; embedding KNN ordering;
  recency/topic fallback when no embedding.
- **`stream_speaker_reply`**: prompt carries the simulation boundary, the
  anti-other-speaker rule, viewer-language instruction, attributed-claims block
  (with per-claim attribution tags + the low-confidence-hedging instruction),
  transcript-as-context-only framing, and the "extracted from your sources" (not
  "actually said") framing; seed block only when seeded; reuses `build_messages`
  ordering.
- **Source linking**: YouTube, web, email, and `text` items link without
  duplicating bodies in speaker tables.
- **Pasted text**: `/videos` creates a `kind='text'` item; pipeline summarizes +
  embeds it with no fetch.
- **Routes**: per-episode + whole-dossier persona turns persist with `thread_id`
  and render; detect/add/edit/merge/activate/review update the UI;
  `/speaker/{id}` renders the dossier; foreign profile → 404.
- **Video-chat regression (critical)**: existing `tests/test_services_chat.py` +
  `tests/test_routes_chat.py` stay green **unchanged** — proves the `thread_id`
  extension is behaviour-preserving.

## Rollout (phased)

### Phase 1.5 — first useful release (this spec)

All tables incl. `speaker_claim_embeddings`, `speaker_source_candidates` +
`chat_threads`; `videos.channel_id` + yt-dlp capture; `videos.kind='text'` + the
pasted-text add-tab; seeded `known_shows`/`known_speakers` (identity only, **no
positions**) + idempotent loaders; `show_match`; `speakers`
(resolve/link/merge/activate); `speaker_discovery` (candidates); `speaker_claims`
(attributed extraction with attribution metadata, embedding ranking, backfill
job over confirmed sources + pipeline piggyback); `speaker_chat`; all routes; the
full in-chat UI (chips + banner + track-record peek + jump-in) **and** the full
speaker page (dossier, confirmed sources + candidate list, multi-source linking,
whole-dossier chat); avatars = cartoon + photo-upload slot. Existing video chat
untouched, guarded by its tests.

**Phase 1.5 is one epic, not one PR.** The implementation plan should slice it so
each PR lands green on its own. A workable cut (refined in the plan):
1. **Schema + seeds** — all tables/migrations (incl. the `videos` rebuild),
   seeded `known_shows`/`known_speakers` + loaders, `show_match`, `resolve_speaker`.
2. **Chips + activation + basic speaker page** — detection in the pipeline,
   speaker chips, activate/deactivate, the speaker page header + confirmed-sources
   list (no claims yet), pasted-text add-tab.
3. **Claim extraction + persona chat** — `speaker_claims` (attributed,
   recency/topic retrieval), `speaker_chat`, both chat surfaces, disclaimer
   banner. Testable without embeddings via the recency fallback.
4. **Embeddings + backfill + discovery + peek** — `speaker_claim_embeddings` +
   embedding-ranked retrieval, the backfill job, `speaker_discovery` + candidate
   confirm/dismiss UI, the track-record peek.

### Phase 2 — stronger dossier intelligence

Contradiction discovery as an explicit analysis action; source-comparison views;
better attribution where transcript structure supports it; **automatic photo
lookup** for avatars (e.g. Wikimedia Commons — it has licensed portraits, unlike
Pexels' stock library).

### Phase 3 — refinement and scale

Alias suggestions / merge recommendations; bulk-backfill workflows;
**voice persona** (pair with the existing Piper TTS to *hear* the simulated
speaker); more source importers (PDF/documents as normal library items); review
workflows for large dossiers.

No phase includes seeded known positions unless that product decision is
explicitly reopened.

## Out of scope (v1.5)

- Real audio **diarization** (pyannote) / per-segment attribution.
- **Voice** for the persona (Phase 3).
- Seeded public **positions** / political belief catalogs (permanently banned
  unless reopened).
- Cross-**profile** speaker sharing (speakers stay per-profile, like videos).
- Automatic **alias clustering** beyond exact `name_key` + manual merge.
- Treating a whole transcript as one speaker's words when attribution is unknown.

## Open risks / notes

- **Metadata detects participants, not statement attribution.** The whole design
  leans on this: extraction attributes by name and drops what it cannot place;
  the persona never speaks unattributed text as the speaker's own.
- **Backfill batch size.** Activating a prolific speaker (Chamath across many
  episodes) is a noticeable batch of LLM calls — mitigated by running as a
  background job, not by avoiding the work.
- **Identity resolution is fuzzy.** `name_key` exact-match over- and under-merges;
  v1.5 ships manual merge + rename as the correction path.
- **Extraction quality depends on the model.** Claims are paraphrases framed as
  "positions taken," `unreviewed` by default, with evidence visible; the page
  makes weak extraction look weak.
- **User-added sources are user-owned evidence, not global truth.** Public
  figures invite politically charged interpretation; the app shows evidence
  instead of shipping its own view of anyone's positions.
- **Transparency vs. nagging.** The banner must not be buried, but must not
  interrupt every answer; the prompt safety does not depend on the banner alone.
