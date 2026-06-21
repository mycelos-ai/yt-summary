# Speaker Dossiers and Persona Chat - Design

**Status:** Draft - successor to the 2026-06-20 speaker chat draft
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
3. The user and pipeline can attach sources to that speaker.
4. The app extracts or records claims from those sources with citations.
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
- Speaker sources: videos, URLs, pasted text, and manual notes attached to a
  speaker.
- Evidence-first claims extracted from sources, with source type,
  timestamp or text location when available, quote/evidence text, confidence,
  and review status.
- Persona chat scoped either to the current video or to the speaker dossier,
  using explicit chat thread scoping.
- Manual correction: edit speaker, merge speakers, unlink appearance, delete
  or hide weak claims, and add material by hand.

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
- **Appearance:** A link between a speaker and a video.
- **Speaker source:** Any material attached to a speaker: a video, URL,
  pasted text, or manual note.
- **Claim:** A sourced statement, position, promise, prediction, argument,
  or notable assertion attributed to a speaker.
- **Dossier:** The accumulated speaker sources and claims.
- **Persona reply:** A simulated answer grounded in the current episode and
  dossier, written in the user's language and optionally styled after the
  speaker.

## Non-negotiable product rules

1. **No known positions seed.** The app may seed who a person is and where
   they appear. It must not seed what they believe.
2. **Claims need evidence.** A track record item is only useful if it points
   back to a source.
3. **Attribution beats style.** If the app cannot attribute a statement to a
   speaker, it may use that text as episode context, but not as a claim in
   that speaker's dossier.
4. **Roleplay is bounded.** The model may answer with the speaker's style,
   but must not imply it is the real person.
5. **User-provided sources are first-class.** Pasted material and URLs are
   valid dossier inputs, not a later add-on.
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

### `video_speakers`

Appearance links.

```sql
CREATE TABLE IF NOT EXISTS video_speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    role TEXT,
    source TEXT NOT NULL CHECK(source IN ('show_rule','manual','llm')),
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(video_id, speaker_id)
);
CREATE INDEX IF NOT EXISTS idx_video_speakers_video
    ON video_speakers(video_id, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_video_speakers_speaker
    ON video_speakers(speaker_id);
```

### `speaker_sources`

The source layer behind the dossier.

```sql
CREATE TABLE IF NOT EXISTS speaker_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL
        CHECK(source_type IN ('video','url','pasted_text','manual_note')),
    video_id TEXT REFERENCES videos(id) ON DELETE CASCADE,
    url TEXT,
    title TEXT NOT NULL DEFAULT '',
    body TEXT,
    added_by TEXT NOT NULL CHECK(added_by IN ('auto','manual')) DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_speaker_sources_speaker
    ON speaker_sources(speaker_id, created_at);
```

For video sources, `video_id` points to the existing video and `body` can be
NULL because the transcript already lives on `videos`. For pasted text and
manual notes, `body` contains the user-provided material.

### `speaker_claims`

Evidence-backed track record items.

```sql
CREATE TABLE IF NOT EXISTS speaker_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES speaker_sources(id) ON DELETE CASCADE,
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
add sqlite-vec embeddings for `speaker_claims`.

### `chat_threads` and `chat_messages`

Avoid `NULL` or sentinel video ids for whole-speaker chats. Add explicit
thread scope instead.

```sql
CREATE TABLE IF NOT EXISTS chat_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    scope TEXT NOT NULL CHECK(scope IN ('video','video_speaker','speaker')),
    video_id TEXT REFERENCES videos(id) ON DELETE CASCADE,
    speaker_id INTEGER REFERENCES speakers(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, scope, video_id, speaker_id)
);

ALTER TABLE chat_messages ADD COLUMN thread_id INTEGER
    REFERENCES chat_threads(id);
```

Migration can backfill one `video` thread per existing `(user_id, video_id)`
chat history. Existing `video_id` remains for compatibility until the repo
layer is fully thread-based.

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
- Merge speakers by re-pointing appearances, sources, claims, and chat
  threads.
- Create speaker sources from videos, URLs, pasted text, and manual notes.

### `services/speaker_claims.py`

Responsibilities:

- Extract candidate claims from a speaker source.
- Store evidence text and timestamp/offsets when available.
- Keep extracted claims `unreviewed` by default.
- Replace claims for a reprocessed source without duplicating stale rows.
- Retrieve relevant claims for a persona prompt.

For videos without speaker-level attribution, extraction should be
conservative. It may create source context, but should only create claims
when metadata, transcript markers, or user correction makes attribution
plausible.

### `services/speaker_chat.py`

Persona chat mirrors the existing LiteLLM streaming path but changes the
system prompt posture.

Prompt shape:

```text
You are a clearly simulated conversational perspective for {name}.
You are not {name}, and you must not claim to be the real person.

Reply in the same language as the viewer's latest message.

Use the speaker's style only as presentation. Ground factual claims in the
CURRENT EPISODE CONTEXT and the SOURCED DOSSIER below.

Rules:
- Do not attribute words to {name} unless they appear in a sourced claim or
  an attributed source excerpt.
- If the transcript contains multiple speakers and attribution is unclear,
  say the episode context is ambiguous.
- If the viewer asks about contradictions, compare sourced claims and cite
  the sources.
- Do not invent specific facts, quotes, numbers, or beliefs.
- Keep the answer concise and link back to evidence when available.

STYLE NOTE:
{style_note}

CURRENT EPISODE CONTEXT:
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

The chat route loads the `video_speaker` thread, current episode context,
and a capped dossier slice.

### Speaker routes

- `GET /speaker/{speaker_id}`
- `POST /speaker/{speaker_id}/edit`
- `POST /speaker/{speaker_id}/merge`
- `POST /speaker/{speaker_id}/sources`
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
- Appearances: videos where the speaker appears.
- Sources: videos, URLs, pasted text, and manual notes.
- Add source form with URL and pasted text support.
- Claims: grouped by topic, each with evidence, source, timestamp/link when
  available, confidence, and review state.
- Chat: ask the dossier.

The page should make weak extraction visible. `unreviewed` claims should not
look as authoritative as accepted claims.

## Rollout

### Phase 1.5 - first useful release

- Add known shows and known speakers seed data.
- Store YouTube `channel_id`.
- Add speakers, video appearances, speaker sources, speaker claims, and chat
  thread scoping.
- Add show matching from metadata.
- Add speaker chips to video detail.
- Add the simple speaker page.
- Add manual source input: URL and pasted text.
- Add conservative claim extraction for pasted text and video sources.
- Add simulated persona chat grounded in current episode context plus
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
- Optional source importers for PDFs or documents.
- Better review workflows for large dossiers.

No phase includes seeded known positions unless this product decision is
explicitly reopened.

## Testing strategy

- Existing chat tests remain green.
- Migration tests cover idempotent creation of known shows, known speakers,
  speakers, sources, claims, and chat thread scoping.
- Show matching tests cover channel id, title fallback, disabled seeded
  rules, and user-added rules.
- Speaker resolution tests cover known speaker linking, profile scoping,
  exact `name_key`, and manual merge.
- Source tests cover video, URL, pasted text, and manual note creation.
- Claim extraction tests mock the LLM and verify evidence text, timestamp or
  offsets, confidence, and `unreviewed` status.
- Persona prompt tests verify that the prompt includes the simulation
  boundary, avoids real-person claims, carries current context, and includes
  only sourced dossier items.
- Route tests verify speaker page rendering, source add, extraction action,
  claim review, and foreign-profile 404s.

## Open risks

- Metadata can identify participants, but not necessarily who said each
  sentence.
- LLM extraction will produce imperfect claims; review state and evidence are
  mandatory mitigations.
- Pasted text can be unreliable or adversarial. It should be user-owned
  source material, not global truth.
- Public figures invite politically charged interpretation. The app should
  make evidence visible instead of shipping its own view of their positions.
- The UX must not bury the simulation disclaimer, but it also should not
  interrupt every answer with repetitive disclaimers.
