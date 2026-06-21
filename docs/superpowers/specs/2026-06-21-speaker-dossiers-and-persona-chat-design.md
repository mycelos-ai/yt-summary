# Speaker Dossiers and Persona Chat - Design

**Status:** Superseded by the merged spec
[`2026-06-21-chat-with-speakers-v1_5-design.md`](2026-06-21-chat-with-speakers-v1_5-design.md).
That spec carries this dossier model forward as the grounding layer and
re-joins it with the roleplay hook. Kept for history only — do not build from
this.
**Date:** 2026-06-21

## Positioning

The feature is not "talk to the real person." The honest product is:

> **Question a speaker's record across your videos and sources.**

The chat surface can feel conversational and can use the speaker's style,
but the product value is the dossier: timestamped, sourced claims that let
the viewer ask whether a person has been consistent, where they changed
their mind, and what they actually said in the user's library.

The system always treats replies as simulated. The interface says that
clearly, and the prompt avoids claiming that the model is the real person.

## Product thesis

The previous draft had a strong hook but too much roleplay gravity. A
metadata-only v1 can identify that Chamath, Jason, Sacks, and Friedberg are
likely in an All-In episode, but it cannot reliably know which person said
which sentence without additional attribution work. If the app lets a user
pick "Chamath" and then feeds the full transcript to a first-person persona,
it risks putting other speakers' claims in Chamath's mouth.

The useful version needs an evidence layer early:

1. The app knows common shows and recurring speakers.
2. The user can open a speaker page.
3. Existing library items can be linked to that speaker.
4. The app extracts or records claims from those linked library items with
   citations.
5. The chat answers from this sourced dossier, not from a generic public
   position catalog.

That makes the feature defensible and more interesting. A Trump dossier,
for example, becomes useful because the app can show "in source A he said
X" and "in source B he said Y," then discuss the tension with links back to
the evidence.

## Scope - first useful release

This replaces the earlier "lean v1" with a small but more complete v1.5.

### In scope

- Seeded known shows: channel ids, title/description patterns, known hosts,
  and safe guest parsing rules.
- Seeded known speakers: name, normalized key, role, known shows, optional
  style note, and optional avatar metadata.
- No seeded known positions.
- Speaker chips on the video detail page for detected or manually added
  speakers.
- A simple speaker page where the speaker is clickable from the chip.
- Speaker-source links: any existing library item can include a speaker,
  including YouTube videos, web articles, and emails/newsletters.
- Evidence-first claims extracted from linked library items, with source type,
  timestamp or text location when available, quote/evidence text, confidence,
  and review status.
- Persona chat scoped either to the current video or to the speaker dossier,
  using explicit chat thread scoping.
- Manual correction: edit speaker, merge speakers, unlink appearances, delete
  or hide weak claims, and link sources by hand.

### Out of scope for the first release

- Audio diarization.
- Voice cloning or spoken persona output.
- Seeded public positions or political belief catalogs.
- Fully automatic contradiction scoring.
- Cross-profile speaker sharing.
- Automatic alias clustering beyond conservative name matching plus manual
  merge.
- Treating a whole transcript as one speaker's words when speaker
  attribution is unknown.

## Core terminology

- **Profile:** The app's Netflix-style user profile.
- **Known show:** A seeded or user-added show rule that helps detect
  speakers from metadata.
- **Known speaker:** A seeded or user-added person record containing
  identity and presentation metadata, not positions.
- **Speaker:** A profile-local person entity. It can link to a known speaker
  but remains editable by the profile.
- **Speaker appearance/source link:** A link between a speaker and one
  existing library item. The current `videos` table already represents
  YouTube videos, web articles, and emails/newsletters through `kind`.
- **Claim:** A sourced statement, position, promise, prediction, argument,
  or notable assertion attributed to a speaker.
- **Dossier:** The accumulated library-item links and claims for a speaker.
- **Persona reply:** A simulated answer grounded in the current source item and
  dossier, written in the user's language and optionally styled after the
  speaker.

## Non-negotiable product rules

1. **No known positions seed.** The app may seed who a person is and where
   they appear. It must not seed what they believe.
2. **Claims need evidence.** A track record item is only useful if it points
   back to a source.
3. **Attribution beats style.** If the app cannot attribute a statement to a
   speaker, it may use that text as source context, but not as a claim in
   that speaker's dossier.
4. **Roleplay is bounded.** The model may answer with the speaker's style,
   but must not imply it is the real person.
5. **The existing library is the source store.** URLs, articles, emails,
   newsletters, videos, and future pasted-text items should enter the normal
   library pipeline and then be linked to speakers. Speaker tables should not
   duplicate source bodies.
6. **Manual correction is part of the product.** Names, sources, claims, and
   merges must be easy to fix because automatic extraction will be imperfect.

## Data model

The data model below is intentionally more source-oriented than the earlier
draft.

### `known_shows`

Seeded and user-maintained show rules.

```sql
CREATE TABLE IF NOT EXISTS known_shows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    channel_id TEXT,
    title_pattern TEXT,
    description_pattern TEXT,
    hosts_json TEXT NOT NULL DEFAULT '[]',
    guest_rule TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    seed_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_known_shows_channel
    ON known_shows(channel_id);
```

`user_id IS NULL` means a shipped rule. Profile-owned rules can extend or
disable the shipped defaults.

### `known_speakers`

Seeded directory metadata only. No positions.

```sql
CREATE TABLE IF NOT EXISTS known_speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    role TEXT,
    known_shows TEXT,
    avatar_id TEXT,
    style_note TEXT,
    seed_version INTEGER NOT NULL DEFAULT 1
);
```

`style_note` must describe speaking style only, for example "blunt,
fast-moving investor tone." It must not encode claims such as "supports X"
or "believes Y."

### `speakers`

Profile-local speaker identity.

```sql
CREATE TABLE IF NOT EXISTS speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    known_speaker_id INTEGER REFERENCES known_speakers(id),
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    role TEXT,
    avatar_id TEXT,
    style_note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, name_key)
);
```

Exact `name_key` matching is the conservative anchor. Alias drift is handled
through manual merge rather than aggressive auto-clustering.

### `source_speakers`

Links between speakers and existing library items. The column name
`source_id` points to `videos(id)` because that table is already the app's
polymorphic content table (`kind IN ('youtube','web','email')`).

```sql
CREATE TABLE IF NOT EXISTS source_speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    role TEXT,
    detection_source TEXT NOT NULL
        CHECK(detection_source IN ('show_rule','manual','llm')),
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_id, speaker_id)
);
CREATE INDEX IF NOT EXISTS idx_source_speakers_source
    ON source_speakers(source_id, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_source_speakers_speaker
    ON source_speakers(speaker_id);
```

This replaces the earlier idea of a `speaker_sources` table with `body`
content. A web article, newsletter email, or YouTube video should be stored
once as a library item, summarized and embedded through the existing pipeline,
then linked to one or more speakers through `source_speakers`.

If pasted text becomes an input, it should be added as a normal library item
first, not stored only inside a speaker-specific table.

### `speaker_claims`

Evidence-backed track record items.

```sql
CREATE TABLE IF NOT EXISTS speaker_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    source_speaker_id INTEGER REFERENCES source_speakers(id) ON DELETE SET NULL,
    claim TEXT NOT NULL,
    topic TEXT,
    evidence_text TEXT,
    evidence_start_s INTEGER,
    evidence_end_s INTEGER,
    text_start_offset INTEGER,
    text_end_offset INTEGER,
    confidence REAL,
    extraction_method TEXT NOT NULL
        CHECK(extraction_method IN ('metadata','llm','manual')),
    review_status TEXT NOT NULL
        CHECK(review_status IN ('unreviewed','accepted','rejected'))
        DEFAULT 'unreviewed',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_speaker_claims_speaker
    ON speaker_claims(speaker_id, created_at);
CREATE INDEX IF NOT EXISTS idx_speaker_claims_source
    ON speaker_claims(source_id);
```

For v1.5, retrieval can use recency plus topic text matching. A later pass can
reuse existing source embeddings as a pre-filter and optionally add sqlite-vec
embeddings for `speaker_claims`.

### `chat_threads` and `chat_messages`

Avoid `NULL` or sentinel video ids for whole-speaker chats. Add explicit
thread scope instead.

```sql
CREATE TABLE IF NOT EXISTS chat_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    scope TEXT NOT NULL CHECK(scope IN ('source','source_speaker','speaker')),
    source_id TEXT REFERENCES videos(id) ON DELETE CASCADE,
    speaker_id INTEGER REFERENCES speakers(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, scope, source_id, speaker_id)
);

ALTER TABLE chat_messages ADD COLUMN thread_id INTEGER
    REFERENCES chat_threads(id);
```

Migration can backfill one `source` thread per existing `(user_id, video_id)`
chat history. Existing `chat_messages.video_id` remains for compatibility
until the repo layer is fully thread-based.

## Services

### `services/show_match.py`

`identify_from_metadata(db, video) -> list[DetectedSpeaker]`

Uses `known_shows` plus video metadata. This is cheap and deterministic:
channel id first, title/description pattern second. It returns known hosts
and parsed guests, but does not create claims.

This requires storing `videos.channel_id` from yt-dlp metadata.

### `services/speakers.py`

Responsibilities:

- Resolve speaker identity from known speaker, show match, or manual input.
- Link appearances.
- Merge speakers by re-pointing appearances/source links, claims, and chat
  threads.
- Link existing library items to speakers. URL, article, email, newsletter,
  and future pasted-text ingestion should happen through the normal library
  pipeline first.

### `services/speaker_claims.py`

Responsibilities:

- Extract candidate claims from a linked library item.
- Store evidence text and timestamp/offsets when available.
- Keep extracted claims `unreviewed` by default.
- Replace claims for a reprocessed source without duplicating stale rows.
- Retrieve relevant claims for a persona prompt.

For library items without speaker-level attribution, extraction should be
conservative. It may use the item as context, but should only create claims
when metadata, transcript markers, author/from metadata, or user correction
makes attribution plausible.

### `services/speaker_chat.py`

Persona chat mirrors the existing LiteLLM streaming path but changes the
system prompt posture.

Prompt shape:

```text
You are a clearly simulated conversational perspective for {name}.
You are not {name}, and you must not claim to be the real person.

Reply in the same language as the viewer's latest message.

Use the speaker's style only as presentation. Ground factual claims in the
CURRENT SOURCE CONTEXT and the SOURCED DOSSIER below.

Rules:
- Do not attribute words to {name} unless they appear in a sourced claim or
  an attributed source excerpt.
- If the source contains multiple speakers and attribution is unclear, say the
  source context is ambiguous.
- If the viewer asks about contradictions, compare sourced claims and cite
  the sources.
- Do not invent specific facts, quotes, numbers, or beliefs.
- Keep the answer concise and link back to evidence when available.

STYLE NOTE:
{style_note}

CURRENT SOURCE CONTEXT:
{episode_context}

SOURCED DOSSIER:
{claims}
```

The UI banner should still say the reply is simulated, but the prompt should
not depend on the banner as the only safety boundary.

## Routes

### Video speaker routes

- `POST /v/{video_id}/speakers/detect`
- `POST /v/{video_id}/speakers`
- `POST /v/{video_id}/speakers/{speaker_id}/unlink`
- `POST /v/{video_id}/speaker/{speaker_id}/chat`

The chat route loads the `source_speaker` thread, current source context,
and a capped dossier slice.

### Speaker routes

- `GET /speaker/{speaker_id}`
- `POST /speaker/{speaker_id}/edit`
- `POST /speaker/{speaker_id}/merge`
- `POST /speaker/{speaker_id}/sources/link`
- `POST /speaker/{speaker_id}/sources/{source_id}/extract`
- `POST /speaker/{speaker_id}/claims/{claim_id}/edit`
- `POST /speaker/{speaker_id}/claims/{claim_id}/review`
- `POST /speaker/{speaker_id}/chat`

The speaker page is intentionally part of the first useful release. It is
where the dossier becomes visible and correctable.

### Known show and known speaker settings

- `GET /settings/shows`
- `POST /settings/shows`
- `POST /settings/shows/{id}/edit`
- `POST /settings/shows/{id}/toggle`
- `GET /settings/speakers`
- `POST /settings/speakers`

Seeded rows are read-only except for profile-level disable/override
behavior. User rows are editable.

## UI

### Video detail

The existing chat box gets a mode picker:

```text
Ask: [the video] [Chamath] [Jason] [Sacks] [Friedberg] [+ Speaker]
```

Wording should avoid implying direct access to the real person. Prefer
"Ask as simulated" or "Perspective" over "Chat with" in sensitive contexts.

Each speaker chip opens that speaker's video-scoped thread. The speaker name
links to `/speaker/{id}`.

Jump-in controls:

- Transcript block: "Discuss this moment"
- Player position: "Discuss current moment"

If multiple speakers are present and the moment is not attributed, the UI
should ask which speaker perspective to use instead of guessing.

### Speaker page

The first useful version needs only:

- Header: name, role, avatar, style note, edit action.
- Appearances/sources: library items where the speaker appears.
- Sources: linked library items, including videos, articles, and emails.
- Add source/link form: search existing library items, paste a URL into the
  normal add-source pipeline and link the resulting item, or link an imported
  email/newsletter.
- Claims: grouped by topic, each with evidence, source, timestamp/link when
  available, confidence, and review state.
- Chat: ask the dossier.

The page should make weak extraction visible. `unreviewed` claims should not
look as authoritative as accepted claims.

## Rollout

### Phase 1.5 - first useful release

- Add known shows and known speakers seed data.
- Store YouTube `channel_id`.
- Add speakers, source-speaker links, speaker claims, and chat thread scoping.
- Add show matching from metadata.
- Add speaker chips to video detail.
- Add the simple speaker page.
- Add source linking from existing library items, plus URL ingestion through
  the normal source pipeline.
- Add conservative claim extraction for linked library items.
- Add simulated persona chat grounded in current source context plus
  sourced dossier.
- Keep existing video chat behavior unchanged.

### Phase 2 - stronger dossier intelligence

- Add sqlite-vec embeddings for claims.
- Improve claim retrieval by semantic relevance.
- Add contradiction discovery as an explicit analysis action.
- Add source comparison views.
- Add better attribution when transcript structure supports it.

### Phase 3 - refinement and scale

- Better alias suggestions and merge recommendations.
- Bulk backfill for existing videos.
- Optional source importers for pasted text, PDFs, or documents as normal
  library items.
- Better review workflows for large dossiers.

No phase includes seeded known positions unless this product decision is
explicitly reopened.

## Testing strategy

- Existing chat tests remain green.
- Migration tests cover idempotent creation of known shows, known speakers,
  speakers, source-speaker links, claims, and chat thread scoping.
- Show matching tests cover channel id, title fallback, disabled seeded
  rules, and user-added rules.
- Speaker resolution tests cover known speaker linking, profile scoping,
  exact `name_key`, and manual merge.
- Source-link tests cover YouTube videos, web articles, and emails/newsletters
  without duplicating source bodies in speaker tables.
- Claim extraction tests mock the LLM and verify evidence text, timestamp or
  offsets, confidence, and `unreviewed` status.
- Persona prompt tests verify that the prompt includes the simulation
  boundary, avoids real-person claims, carries current context, and includes
  only sourced dossier items.
- Route tests verify speaker page rendering, source linking, extraction action,
  claim review, and foreign-profile 404s.

## Open risks

- Metadata can identify participants, but not necessarily who said each
  sentence.
- LLM extraction will produce imperfect claims; review state and evidence are
  mandatory mitigations.
- User-added sources can be unreliable or adversarial. They should be
  user-owned library material, not global truth.
- Public figures invite politically charged interpretation. The app should
  make evidence visible instead of shipping its own view of their positions.
- The UX must not bury the simulation disclaimer, but it also should not
  interrupt every answer with repetitive disclaimers.
