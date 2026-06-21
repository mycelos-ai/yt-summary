# Chat with the Speakers (Cross-Video Personas + Track Record + Seed Catalog) — Design

**Status:** Superseded. The product direction now lives in the merged spec
[`2026-06-21-chat-with-speakers-v1_5-design.md`](2026-06-21-chat-with-speakers-v1_5-design.md),
which reconciles this draft's "talk to them in real time" hook with the
evidence-first dossier model. Kept for history only — do not build from this.
**Date:** 2026-06-20

## Supersession note

This draft captured the original "Chat with the Speakers" concept. It is
kept for history, but the product direction has moved to a tighter,
evidence-first design:

- Seed known shows and known speakers, but do not seed known positions.
- Make speaker pages and user-added sources part of the first useful
  release.
- Treat the track record as sourced claims, not as generic roleplay
  memory.
- Avoid whole-history chats via `NULL`/sentinel videos; use explicit
  chat thread scoping instead.

See the successor spec and the accompanying change log:

- [`2026-06-21-speaker-dossiers-and-persona-chat-design.md`](2026-06-21-speaker-dossiers-and-persona-chat-design.md)
- [`2026-06-21-speaker-dossiers-change-log.md`](2026-06-21-speaker-dossiers-change-log.md)

## Positioning (the claim)

> **You follow them on Twitter. You adore them on YouTube.
> Now talk to them in real time.**

That's the hook. The product promise is *access* — stepping from passive
fandom (scrolling their takes, watching their episodes) into an actual
back-and-forth. The one honest asterisk the interface always carries:
it's a **simulation** — an AI impression grounded in what they actually
said, not the real person. The pitch sells the experience; the UI never
hides the asterisk.

## Goal

"Chat over a video" today talks to *the video* — a neutral assistant
grounded in one transcript. The next step people actually want: argue
with the **people in the video**, and do it *intelligently across
everything they've ever said in your library*.

You're watching the All-In podcast, Chamath says something that winds
you up, you pause, and you want to say "that's nonsense" — and get a
reply *in his voice*. But the real payoff: the system knows Chamath as a
**persistent person across every episode he appears in**, with a
**track record of the positions he's taken over time**. So you can
confront the persona with "but three episodes ago you argued the
opposite," and it actually has that history to work with.

Everything stays **clearly labelled as simulated** — these are AI
impressions, not the real person's words. That transparency is a
first-class design constraint, not a footnote.

This builds on the existing chat plumbing (`services/chat.py` →
`chat_core.build_messages` → LiteLLM stream, history in `chat_messages`).
Mechanically: same machinery, persona system prompt, speaker-scoped
history — now enriched with a cross-video statement dossier.

## Scope — leanest v1 (decided)

The feature only makes sense where **people talk** (podcasts /
interviews), and it's only worth it for shows people actually care
about. So v1 deliberately narrows:

1. **Only "the big ones" auto-enable.** A small, shipped **supported-
   shows registry** (by channel + title/description patterns) covers
   well-known talk formats — Diary of a CEO, Lex Fridman, the All-In
   pod, Joe Rogan, etc. Outside that registry the feature stays quiet
   unless the user opts in.
2. **Speakers come from metadata first, not the transcript.** For
   interview shows the participants are right there: the **host** is
   known from the registry, the **guest** is parsed from the
   description/title (Diary of a CEO "… with {Guest}", Lex's
   "{Guest}: {Topic} | Lex Fridman Podcast #N"). No heavy transcript
   diarization or statement-mining needed to ship.
3. **Manual add is the universal escape hatch.** On any video the user
   can add a speaker by hand (name + avatar) and chat with them — so the
   feature isn't hostage to the registry.
4. **It lives inside the existing chat box, not a new surface.** A
   "**Chat with:** [the video] · [{Host}] · [{Guest}]" selector sits at
   the top of the current chat section. Picking a speaker switches that
   same chat into persona mode (composer + disclaimer banner) — no
   separate page in v1.
5. **Jump in from where you are.** Reusing the time markers we already
   render, the "💬 Discuss this moment" affordance (off the live player
   position) and the per-block "💬 Discuss" both drop you into the
   speaker chat **with that moment as context**, so you talk about
   exactly the bit you just heard.

Everything below this line — cross-video identity, the track-record
dossier, the speaker page, the shipped knowledge catalog — is the
**fuller vision**, scheduled into later phases (see Rollout). v1 is just:
*known shows → host+guest from metadata (or manual) → persona chat in the
existing box, grounded in this one episode, seedable from the current
timestamp.*

### Refinements (decided 2026-06-20)

- **In-character voice.** The persona speaks the way the real person
  speaks — their tone and mannerisms — not as a neutral assistant.
- **Banner is enough** for transparency; no forced "I'm an AI" line in
  every reply.
- **A chip per speaker.** Panel shows (All-In) surface every host + guest
  as its own chip — no host/main-guest reduction.
- **Reply in the viewer's language**, matching the language of their
  question, regardless of the episode's language.
- **Registry lives in the DB**: shipped shows are *seeded*, and the user
  *maintains their own* on top (not a static code file).
- **Player entry point** exists; its exact visual placement is still to
  be eyeballed (feasibility is settled — the player exposes the hook).

## Terminology

As in earlier specs: every "user" is a Netflix-style **Profile**.
- A **speaker** is one named person (a podcast host/guest), a
  **profile-global** entity that persists across all videos they appear
  in — *not* a per-video row.
- An **appearance** links a speaker to one video they speak in.
- A **statement** is one notable claim/position that speaker made,
  attributed to the video + timestamp it came from. The accumulated
  statements are the speaker's **track record / dossier**.
- A **persona reply** is a simulated, in-character answer from one
  speaker, grounded in *this* episode plus their track record.

## Decisions (from brainstorming)

1. **Speakers are profile-global, identity-resolved across videos.**
   Detecting "Chamath" in a new video links to the *existing* Chamath if
   he's already known (matched on a normalised name key), otherwise
   creates him. One person → one row → one growing dossier.
2. **Auto-detected, manually editable** — rename, re-avatar, set a
   persona note, **merge** two rows that are the same person under
   different spellings, or delete. The LLM seeds; the user corrects.
3. **A track record per speaker.** During processing we also extract
   each speaker's notable statements from that episode (paraphrased
   claim + topic + timestamp) into a `speaker_statements` dossier.
4. **Persona chat is episode-first + track-record-aware.** The reply is
   grounded primarily in what the speaker said in *this* episode, and is
   additionally handed a relevant slice of their **prior** statements
   from other videos so it can be consistent — or be caught being
   inconsistent. Never fabricate episode-specific facts.
5. **Three entry points** (unchanged from the prior draft): speaker
   picker in the chat panel, "💬 Discuss" per transcript block, and
   "💬 Discuss this moment" off the live player position
   (`player.getCurrentTime()`), which seeds from the transcript block at
   that timestamp.
6. **A speaker page.** `/speaker/{id}` is the home of one person: their
   avatar/role, the dossier of what they've said (grouped by topic,
   each line linking back to the video + timestamp), the list of
   appearances, and a direct chat with the persona spanning their whole
   track record.
7. **A shipped seed catalog of well-known speakers.** The app ships with
   a curated catalog of recurring podcast figures (e.g. the All-In
   hosts, frequent guests like Elon Musk, big interview shows) carrying
   a baseline persona note + characteristic topics/positions. When
   detection names a person already in the catalog, the local speaker
   links to the catalog entry and the persona gets substance *before*
   you've processed many of their episodes. The user's own per-episode
   track record always takes precedence over the generic catalog
   baseline.

## Why an LLM detection step (and not diarization)

Neither faster-whisper nor YouTube captions give us speaker *names* —
captions only mark speaker *changes* (`>>`), already used in
`services/transcript_format.py` for paragraphing. So the reliable source
of "who is in this video" and "what did they claim" is the LLM reading
the transcript + metadata. For a named-host podcast this is easy; for an
anonymous video it yields nothing, and the feature simply stays quiet
there. Real audio diarization (pyannote) is out of scope (below).

## Data model

Four tables. The first three are new; `chat_messages` gains one column.

### `speakers` — the profile-global person

```sql
CREATE TABLE IF NOT EXISTS speakers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL DEFAULT 1,
    name         TEXT    NOT NULL,        -- "Chamath Palihapitiya"
    name_key     TEXT    NOT NULL,        -- normalised for matching/dedupe
    role         TEXT,                    -- general descriptor: "investor, co-host"
    avatar_id    TEXT,                    -- curated id from services/avatars.py
    persona_note TEXT,                    -- user-editable style/persona hint
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, name_key)
);
```

`name_key` = lower-cased, punctuation/whitespace-collapsed name. The
`UNIQUE(user_id, name_key)` is the identity anchor: detecting the same
person twice resolves to the same row. (Aliases like "Chamath" vs
"Chamath Palihapitiya" are an accepted fuzziness — see Open Risks; the
manual **merge** action is the escape hatch.)

An extra nullable column links a profile-local speaker to a shipped
catalog entry (see "Seed speaker catalog"):

```sql
ALTER TABLE speakers ADD COLUMN catalog_id INTEGER REFERENCES catalog_speakers(id);
```

### `video_speakers` — an appearance (video ↔ speaker link)

```sql
CREATE TABLE IF NOT EXISTS video_speakers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id   TEXT    NOT NULL REFERENCES videos(id)   ON DELETE CASCADE,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    role       TEXT,                    -- role in THIS video, if it differs
    source     TEXT NOT NULL CHECK(source IN ('auto','manual')) DEFAULT 'auto',
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE(video_id, speaker_id)
);
CREATE INDEX IF NOT EXISTS idx_video_speakers_video
    ON video_speakers(video_id, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_video_speakers_speaker
    ON video_speakers(speaker_id);
```

### `speaker_statements` — the dossier / track record

```sql
CREATE TABLE IF NOT EXISTS speaker_statements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    video_id   TEXT    NOT NULL REFERENCES videos(id)   ON DELETE CASCADE,
    user_id    INTEGER NOT NULL DEFAULT 1,
    statement  TEXT    NOT NULL,        -- a claim/position in their words (paraphrase or short quote)
    topic      TEXT,                    -- short topical tag, for grouping + retrieval
    ts_seconds INTEGER,                 -- where in the video (jump-back link)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_speaker_statements_speaker
    ON speaker_statements(speaker_id, created_at);
CREATE INDEX IF NOT EXISTS idx_speaker_statements_video
    ON speaker_statements(video_id);
```

Statements are re-derivable, so on re-summarize we delete this video's
rows for the speaker and re-insert (forward-only, no stale duplicates).

### `chat_messages` gains a nullable `speaker_id`

Per-video chat and each per-speaker thread share one table:

```sql
ALTER TABLE chat_messages ADD COLUMN speaker_id INTEGER REFERENCES speakers(id);
```

- `speaker_id IS NULL` → today's "chat with the video" thread
  (behaviour unchanged).
- `speaker_id = N` → the conversation with speaker `N` **on this video**
  (`video_id` already on the row scopes it to the episode). A
  whole-track-record chat from the speaker page uses the same column
  with a sentinel/`NULL` video — see Speaker page.

`repos/chat.{history,append}` gain an optional `speaker_id` param;
existing default-NULL call sites are untouched.

## Statement retrieval for the persona (the "intelligence")

The dossier can grow large, so the persona chat is handed only a
**relevant slice** of prior statements, not the whole thing. Two
options, **flagged for review**:

- **(Recommended) Embedding-ranked.** Reuse the existing local
  embeddings + sqlite-vec infra: embed each statement on insert into a
  `speaker_statement_embeddings` vec table, then KNN the user's question
  (and/or the current episode topic) against this speaker's statements,
  excluding the current video. Top-K (capped, e.g. 12) feed the prompt.
- **(MVP) Recency + topic-match.** No new vec table — select this
  speaker's most recent statements, optionally filtered by `topic`
  keyword overlap with the question. Simpler; less precise.

Either way: cross-video only (exclude the episode you're in, which is
already fully in-context), capped, each carrying its source video title
+ `ts_seconds` so the prompt — and a side "track record" panel — can
cite "in *{title}*, you said …".

## Seed speaker catalog (shipped knowledge base)

A curated, **profile-independent, read-only** catalog of well-known
recurring figures, so a persona has substance from the first chat —
before the user has processed many of that person's episodes.

### Catalog tables

```sql
CREATE TABLE IF NOT EXISTS catalog_speakers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    name_key     TEXT NOT NULL UNIQUE,   -- same normalisation as speakers.name_key
    role         TEXT,                   -- "investor / All-In co-host"
    shows        TEXT,                   -- e.g. "All-In; <other shows>"
    persona_note TEXT,                   -- characteristic style/voice
    seed_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS catalog_statements (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_speaker_id INTEGER NOT NULL REFERENCES catalog_speakers(id) ON DELETE CASCADE,
    statement          TEXT NOT NULL,    -- a characteristic position/topic, conservatively framed
    topic              TEXT
);
```

### Seeding mechanism

The catalog is a **versioned data file in the repo** (e.g.
`app/data/speaker_catalog.json`), loaded idempotently on boot like the
existing one-time migrations: a `settings` marker
(`speaker_catalog_seed_version`) gates re-seeding, so bumping the file's
version re-imports cleanly without duplicating. No network, no per-user
data — it's shipped content. Curating the file itself (which shows,
which people, what counts as a "characteristic position") is a content
task; the data file can be drafted with LLM assistance and then
hand-reviewed for tone and fairness before it ships.

### How the catalog feeds a persona

On `resolve_speaker`, also try to match the detected `name_key` against
`catalog_speakers`; on a hit, set `speakers.catalog_id`. The persona
prompt then blends, in strict priority order:

1. **This episode's transcript** (primary, factual grounding),
2. the user's **own cross-video track record** for this speaker,
3. the **catalog baseline** (persona_note + characteristic positions),
   clearly framed as "general, public-perception background — not
   something said in your library."

So a brand-new Elon Musk appearance already chats in character from the
catalog, and as the user accumulates real episodes, their own track
record progressively takes over.

## Services

### Supported-shows registry — DB-seeded + user-extensible (v1 source of truth)

The registry of "the big ones" lives **in the database**, not in a code
file: shipped shows are **seeded** on boot (idempotent, versioned), and
the user can **add and maintain their own** shows on top. New `shows`
table:

```sql
CREATE TABLE IF NOT EXISTS shows (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER,                    -- NULL = shipped/seeded (global); set = user-added
    name          TEXT NOT NULL,              -- "Lex Fridman Podcast"
    channel_id    TEXT,                       -- primary match signal
    title_pattern TEXT,                       -- optional secondary substring/regex match
    hosts_json    TEXT NOT NULL DEFAULT '[]', -- known host names
    guest_rule    TEXT,                       -- how to parse the guest (see below); NULL = hosts only
    enabled       INTEGER NOT NULL DEFAULT 1,
    seed_version  INTEGER NOT NULL DEFAULT 1, -- shipped rows only; gates idempotent re-seed
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_shows_channel ON shows(channel_id);
```

- **Seeded rows** (`user_id IS NULL`) come from a shipped data file
  (`app/data/shows.json`), loaded idempotently behind a settings marker
  (`shows_seed_version`) — bump the version to re-import cleanly, exactly
  like the planned catalog seed. This is the "we seed the speakers/shows"
  part.
- **User rows** (`user_id` set) are added/edited/disabled from the UI —
  the "maintain your own" part. A user row can be minimal: a
  `channel_id` (or `title_pattern`) + a host list, `guest_rule` NULL
  (fixed cast, no parsing).

**Matching signal (decided):** today we persist only `title`,
`description`, `url`, and tags — *not* the channel. yt-dlp's `info`
already carries `channel` / `channel_id` (see `services/youtube.py`,
which currently reads only `id`/`title`/`description`), so v1 **captures
`channel_id` during fetch** (one extra field read + a nullable
`videos.channel_id` column, backfilled NULL on existing rows). A video
matches a show by `channel_id` (primary); `title_pattern` is the
secondary signal (and the only one for pre-existing videos whose
`channel_id` is NULL until re-fetched). Both shipped rows and the
profile's own rows are consulted.

`guest_rule` is a tiny enumerated parser, not free code — e.g.
`after:with ` (Diary of a CEO: guest after "with " in title/desc),
`before:: ` (Lex: `"{Guest}: {Topic} | Lex Fridman Podcast #N"`), or
NULL for a fixed cast (All-In: hosts Chamath/Jason/Sacks/Friedberg,
guests only if a known rule later applies). Keeping it enumerated means
user-added shows stay safe (no arbitrary regex execution risk) and the
settings form is a simple dropdown.

### `repos/shows.py` + `services/show_match.py`

- `repos/shows.py` — CRUD + `seed_from_file(db, version)` (idempotent).
- `services/show_match.py`:
  `identify_from_metadata(db, video) -> list[DetectedSpeaker]` — finds the
  matching enabled show (shipped or the video-owner's own) and returns
  host(s) + parsed guest(s), or `[]` for an unsupported show. Pure
  string/pattern work — **no LLM, no transcript** needed for the v1 path.
  This is what makes v1 cheap and reliable: for interview formats the
  participants are already in the metadata we store.

### `services/speakers.py` — resolution, and (Phase 2) LLM extraction

- **v1:** glue that takes `identify_from_metadata` results (or a manual
  add), `resolve_speaker`s them, and links `video_speakers`. No
  transcript LLM call.
- **Phase 2:** `detect_and_extract(*, transcript, title, description,
  tags, model, api_key, base_url) -> list[SpeakerExtraction]` — one LLM
  call returns, per named speaker: `name`, `role`, and a list of
  `statements` (`{statement, topic, ts_seconds?}`). Robust JSON parse
  reusing `highlight_parser._extract_json_blob` (same path related-links
  and highlights use). Returns `[]` when no one can be confidently
  named. Never raises. This is the richer path that also feeds the
  track-record dossier, and the fallback for talk videos that aren't in
  the registry but clearly have named speakers.
- `resolve_speaker(db, *, user_id, name, role) -> speaker_id` — upsert
  on `(user_id, name_key)`: link to the existing person or create one.
- The pipeline glue (below) calls these, links `video_speakers`, and
  inserts `speaker_statements`.

### `services/speaker_chat.py` — the persona reply

Mirrors `chat.stream_reply` (same LiteLLM streaming kwargs, reuses
`chat_core.build_messages`) with a persona system prompt that now has a
**track-record block**:

```
You are role-playing as {name}{role_clause}, in conversation with a
viewer. Reply in the first person, fully in their voice: match how they
actually talk — their tone, rhetorical habits, bluntness or warmth,
favourite phrasings and pet topics. Be them, not a neutral narrator.

LANGUAGE: Reply in the SAME language the viewer's latest message is
written in, regardless of the language of the episode or the track
record. (A German question gets a German answer, in character.)

GROUNDING RULES:
- Base what you say FIRST on what {name} actually says in THIS episode's
  transcript below.
- You have a TRACK RECORD of positions {name} took in earlier videos
  (below). Stay consistent with it. If the viewer points out a
  contradiction between the episode and the track record, engage with it
  honestly in character — don't pretend it isn't there.
- You MAY draw on {name}'s generally-known public views to stay in
  character, but NEVER invent specific facts/numbers/quotes as things
  said in this episode or the track record.
- Don't break character to disclaim you're an AI — the interface already
  tells the viewer this is a simulation.

{persona_note_block}
{seed_block}              # only on a jump-in: "[12:04] '…the quote…'"

TRACK RECORD (earlier statements by {name} in the user's library, each
with its source):
{track_record}           # selected per "Statement retrieval" above

GENERAL BACKGROUND (public-perception baseline from the shipped catalog,
NOT something said in the user's library — use only to stay in voice):
{catalog_baseline}       # persona_note + characteristic positions, when catalog-linked

FORMAT AS MARKDOWN: short, scannable.

THIS EPISODE TRANSCRIPT:
{transcript}
```

`stream_speaker_reply(*, speaker, transcript, track_record, history,
user_message, seed_ts, seed_quote, model, api_key, base_url)` → async
token iterator, identical mechanics to `stream_reply`.

## Routes

### Speaker chat — `routes/speaker_chat.py`

`POST /v/{video_id}/speaker/{speaker_id}/chat` — the persona turn.
Ownership-checks video + speaker (foreign profile / not an appearance of
this video → 404), resolves the model, loads speaker-scoped history,
selects the track-record slice (retrieval above), appends the user
message (with the optional `(re: …)` seed prefix), streams via
`stream_speaker_reply`, persists the assistant turn with `speaker_id`,
returns the same `_msg_html` user+assistant fragment. Optional
`seed_ts`/`seed_quote` form fields drive the jump-in seed block.

### Speaker management — `routes/speakers.py`

- `POST /v/{video_id}/speakers/detect` — run detection on demand
  (older videos / re-detect); resolve + link + extract statements;
  return the refreshed picker fragment.
- `POST /v/{video_id}/speakers` — add a `manual` appearance (resolves or
  creates the speaker).
- `POST /speaker/{id}/edit` — name / role / avatar / persona_note.
- `POST /speaker/{id}/merge` — merge speaker B into A (re-point
  `video_speakers`, `speaker_statements`, `chat_messages`; delete B).
  The fix for alias drift.
- `POST /v/{video_id}/speakers/{id}/unlink` — remove an appearance
  (keeps the global speaker + dossier).

### Show management — `routes/shows.py` (Settings)

A small section on the Settings page to **maintain your own shows** on
top of the seeded ones (the "pflege eigene weiter" part):
- `GET /settings/shows` — list shipped (read-only) + the profile's own
  rows.
- `POST /settings/shows` — add a user show (name, channel_id and/or
  title_pattern, hosts, optional guest_rule from a dropdown).
- `POST /settings/shows/{id}/edit` / `.../delete` / `.../toggle` — edit,
  remove, or enable/disable a user row. Shipped rows can be **disabled**
  by the profile (a per-profile override flag) but not edited/deleted.

### Speaker page — `routes/speakers.py`

- `GET /speaker/{id}` — the person's home: avatar/role, the dossier
  grouped by `topic` (each statement linking to its video + `ts_seconds`),
  the appearances list, and a **whole-track-record chat** (a
  `speaker_id`-scoped thread not tied to one episode; the persona prompt
  uses the dossier as primary grounding instead of a single transcript).
- `POST /speaker/{id}/chat` — the track-record-wide persona turn.

All HTMX fragment swaps, consistent with the app.

## UI (`video_detail.html`, `speaker.html`, partials)

- **Speaker picker + mode switch** at the top of `<section class="chat">`:
  "Talk to: [ the video ] [ 🟦 Chamath ] [ 🟩 Jason ] … [ ⚙ ]". **Every
  speaker gets its own chip** — a panel show like All-In surfaces all
  four hosts (plus any guest) as separate chips, not a "host + main
  guest" reduction. Each speaker chip `hx-get`s a chat panel fragment in
  speaker mode; the chip carries its avatar (curated
  `services/avatars.py`) and its name deep-links to `/speaker/{id}`
  (Phase 2). The chips wrap to multiple rows when there are many.
- **Disclaimer banner** (transparency, non-negotiable) in speaker mode:
  "⚠️ **Simulated.** AI impression of *{name}* based on this episode and
  what they've said in your library — not their real words." Speaker
  bubbles tinted with the avatar colour.
- **Track-record peek**: a collapsible "What {name} has said before"
  list beside the speaker chat, populated from the dossier slice, each
  line linking back to its source video + timestamp.
- **Transcript jump-in**: a small `💬 Discuss` per `transcript-block`,
  seeding `seed_ts`/`seed_quote`.
- **"Discuss this moment"**: a button by the player; extend the existing
  player IIFE to expose `getCurrentTime()` + a nearest-block lookup over
  the rendered `[data-yt-timestamp]` blocks, opening the speaker chat
  seeded at the live position. No-ops gracefully before the player
  exists. (Exact visual placement of this entry point at the player is
  still to be eyeballed — the player already exposes the hook, so it's a
  styling/placement call, not a feasibility one.)
- **Speaker page** (`speaker.html`): dossier-by-topic, appearances,
  whole-history chat.

## Pipeline integration (`pipeline.py`)

After summarization, a best-effort `set_step("identifying speakers")`,
gated like the other enrichment steps (YouTube-kind, transcript present,
LLM configured): call `detect_and_extract`, `resolve_speaker` each,
upsert `video_speakers`, replace this video's `speaker_statements` for
each speaker, (recommended) embed new statements. Failure leaves the
video speaker-less and never fails the job (same posture as
`_store_related_links`). Older videos get speakers via the detect
endpoint.

## Testing strategy

House style: no live LLM/network (completions mocked); render via
TestClient; in-memory SQLite + sqlite-vec; no browser.

- **`detect_and_extract`** (mocked completion): clean JSON → speakers +
  statements parsed/capped; prose-wrapped JSON → still parsed (envelope
  unwrap); garbage → `[]`.
- **`resolve_speaker`:** same `name_key` across two videos → one row +
  two `video_speakers`; different names → two rows; normalisation of
  spacing/case/punctuation.
- **Schema/migration:** the three tables + `chat_messages.speaker_id`
  created idempotently; `init_schema` + migration run twice cleanly.
- **`speakers` / `speaker_statements` repos:** CRUD; per-profile
  scoping; statement replace-on-re-summarize; `merge` re-points
  appearances + statements + chat messages and deletes B.
- **Statement retrieval:** cross-video only (excludes current video);
  capped; (embedding variant) KNN ordering; (MVP variant) recency.
- **`stream_speaker_reply`:** prompt carries name + grounding rules +
  transcript + track-record block; seed block only when seeded; reuses
  `build_messages` ordering.
- **Routes:** per-episode + whole-history persona turns persist with
  `speaker_id` and render; detect/add/edit/merge/unlink update the UI;
  `/speaker/{id}` renders the dossier; foreign profile → 404.
- **Video-chat regression (critical):** existing
  `tests/test_services_chat.py` + `tests/test_routes_chat.py` stay green
  **unchanged** — proves the `speaker_id`-defaults-NULL extension is
  behaviour-preserving.

## Out of scope (v1)

- Real audio **diarization** (pyannote) / per-segment attribution.
- **Voice** for the persona (natural follow-up: pair with the existing
  Piper TTS to *hear* the simulated Chamath — later).
- Cross-**profile** speaker sharing (speakers stay per-profile, like
  videos).
- Automatic alias clustering beyond exact `name_key` (manual **merge**
  covers the rest in v1).
- Speaker detection for web articles / newsletters (YouTube-kind only).

## Open risks / notes

- **Identity resolution is fuzzy.** `name_key` exact-match will both
  over-merge (two different "John"s) and under-merge ("Chamath" vs
  "Chamath Palihapitiya"). v1 accepts this and ships a manual **merge**
  + rename as the correction path; smarter clustering is a later pass.
- **Statement extraction quality** depends on the model; statements are
  paraphrases, clearly framed as "positions taken," never presented as
  verbatim quotes unless the model copied exact transcript text.
- **Decision needed:** embedding-ranked vs recency/topic statement
  retrieval (see that section) — affects whether we add a
  `speaker_statement_embeddings` vec table this PR.
- **Transparency stays interface-owned**: the banner + styling + the
  "AI impression" framing carry the disclaimer; the persona never speaks
  as the real individual's actual words, and the prompt forbids
  fabricating episode- or record-specific facts.
- **Catalog content is the sharpest accuracy/fairness risk.** Shipping
  baked-in "what person X typically claims" about real, named public
  figures invites both inaccuracy and defamation concerns. Mitigations:
  keep catalog entries to *characteristic topics and speaking style*,
  not specific contestable factual assertions; frame everything as
  "general public perception, simulated"; hand-review the data file
  before shipping; make it easy to ship empty (the rest of the feature
  works fine with zero catalog rows). The catalog is deliberately the
  last, optional phase for exactly this reason.

## Rollout (phased)

**Phase 1 — leanest shippable: metadata speakers + in-chat persona.**
Tables `speakers` + `video_speakers` + `shows` and the
`chat_messages.speaker_id` column (we keep the profile-global `speakers`
table from the start to avoid a later migration, but defer
`speaker_statements`/embeddings), plus a nullable `videos.channel_id`
column and capturing `channel_id` from yt-dlp's `info` in
`services/youtube.py`. The seeded `shows` registry (shipped
`app/data/shows.json` + idempotent loader) with the Settings UI to add
your own; `show_match.identify_from_metadata` (no LLM, no transcript);
`resolve_speaker`; manual speaker add; the `speaker_chat` service
(this-episode grounding, viewer-language replies, in-character voice)
modelled on `stream_reply`; the speaker + show routes; and the
**in-chat** UI: the "Chat with: …" selector (a chip per speaker) at the
top of the existing chat box, the disclaimer banner, and the
seed-from-current-position jump-in (reusing the player's time markers).
The pipeline gains a cheap best-effort step that runs
`identify_from_metadata` and links speakers for supported shows. The
existing video chat is untouched (new column defaults NULL), guarded by
its current tests. **No track record, no catalog, no speaker page yet.**

**Phase 2 — cross-video track record + speaker page.** Add
`speaker_statements` (+ optional vec table), the LLM
`detect_and_extract` path (also a fallback for talk shows outside the
registry), the dossier retrieval in the persona prompt, and the
`/speaker/{id}` page with the whole-history chat.

**Phase 3 — shipped seed catalog (separate PR).** The two `catalog_*`
tables, the `speakers.catalog_id` link, the versioned seed data file +
idempotent loader, and the catalog-baseline block in the persona prompt.
Kept last so the accuracy/fairness review of the shipped content about
real public figures doesn't gate anything earlier.
