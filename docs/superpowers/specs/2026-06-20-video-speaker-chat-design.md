# Chat with the Speakers (Simulated Speaker Personas) — Design

**Status:** Draft — design phase (awaiting review)
**Date:** 2026-06-20

## Goal

"Chat over a video" today talks to *the video* — a neutral assistant
grounded in the transcript. The next step people actually want: argue
with the **people in the video**.

You're watching the All-In podcast, Chamath says something that winds
you up, you pause, and you want to say "that's nonsense, here's why" —
and get a reply *in his voice*, grounded first in what he actually said
in this episode and flavoured by how he generally talks. You can step
into a real back-and-forth with your podcast "heroes" (or villains).

Everything is **clearly labelled as simulated** — these are AI
impressions, not the real person's words. That transparency is a
first-class design constraint, not a footnote.

This builds directly on the existing chat plumbing
(`services/chat.py` → `chat_core.build_messages` → LiteLLM stream,
history in `chat_messages`). A speaker conversation is, mechanically,
"same machinery, persona system prompt, speaker-scoped history."

## Terminology

As in earlier specs: every "user" is a Netflix-style **Profile**.
A **speaker** is one named participant in a video (a host/guest of a
podcast, an interviewer/interviewee). A **persona reply** is a
simulated, in-character answer from one speaker.

## Decisions (from brainstorming)

1. **Speaker detection: auto via LLM, manually editable.** A pipeline
   step asks the LLM to name the speakers from the transcript + title +
   description + tags. The user can rename, re-avatar, delete, or add
   speakers by hand. Auto-detected names that are wrong are the user's
   to fix — we don't pretend the LLM is always right.
2. **Three entry points into a speaker chat** (all land in the same
   place — the chat panel switched into "speaker mode"):
   - a **speaker picker** in the chat panel ("Talk to: 🟦 Chamath /
     🟩 Jason / …" vs. the default "the video"),
   - a **"💬 Discuss" affordance per transcript block**, seeding the
     conversation with that block's timestamp + text,
   - the interesting one: **"💬 Discuss this moment"** next to the
     embedded player, which reads the *live* playback position
     (`player.getCurrentTime()`) and seeds from the transcript block at
     that timestamp — so pausing the video and jumping straight into
     "wait, what you just said…" works.
3. **Grounding: episode-first, persona-flavoured.** The persona answers
   primarily from what *that named speaker* said in this episode;
   it may draw on the speaker's generally-known public positions to stay
   in character, but must never fabricate specific facts/quotes as
   things they "said in this episode." (Prompt-enforced; see Service.)

## Why an LLM detection step (and not diarization)

Neither faster-whisper nor YouTube captions give us speaker *names*.
Auto-captions only mark speaker *changes* (`>>`), handled today in
`services/transcript_format.py` for paragraphing — there's no "this is
Chamath" anywhere. So the reliable source of "who is in this video" is
the LLM reading the transcript + metadata. For a named-host podcast this
is easy; for an anonymous video it may yield nothing, and that's fine —
the feature simply doesn't surface speakers there.

Real audio diarization (pyannote) is explicitly **out of scope** (see
below). We do not need per-segment speaker attribution for v1: the
persona is grounded in the whole transcript with a name-focus
instruction, not in a hard speaker-filtered slice.

## Data model

### New table `video_speakers`

Added to `db.SCHEMA` (`CREATE TABLE IF NOT EXISTS`, idempotent):

```sql
CREATE TABLE IF NOT EXISTS video_speakers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id    TEXT    NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL DEFAULT 1,
    name        TEXT    NOT NULL,        -- "Chamath Palihapitiya"
    role        TEXT,                    -- short descriptor: "co-host, investor"
    avatar_id   TEXT,                    -- curated id from services/avatars.py (cosmetic)
    persona_note TEXT,                   -- optional user-editable style hint
    source      TEXT NOT NULL CHECK(source IN ('auto','manual')) DEFAULT 'auto',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_video_speakers_video
    ON video_speakers(video_id, sort_order, id);
```

Scoped to a profile via `user_id` (videos are already per-profile;
`video_speakers` mirrors that for defence in depth and foreign-profile
404s).

### `chat_messages` gains a nullable `speaker_id`

The existing per-video chat thread and each per-speaker thread live in
the **same** table, distinguished by a new nullable column:

```sql
-- migration (matches the existing ALTER TABLE ADD COLUMN pattern in db.py)
ALTER TABLE chat_messages ADD COLUMN speaker_id INTEGER REFERENCES video_speakers(id);
```

- `speaker_id IS NULL` → the existing "chat with the video" thread
  (behaviour unchanged — see regression note).
- `speaker_id = N` → the conversation with speaker `N`.

`repos/chat.history` gains a `speaker_id: int | None = None` parameter
and filters `speaker_id IS NULL` / `speaker_id = ?` accordingly;
`append` gains the same optional `speaker_id`. The existing default-NULL
call sites keep working untouched.

The jump-in seed (timestamp + quote) is **not** a new column — it's
folded into the system prompt for that one reply (and, optionally,
prepended to the stored user message as a small "(re: 12:04 — '…')"
prefix so the thread reads sensibly later). Decided: prefix the stored
user message; keep the schema clean.

## Services

### New `services/speakers.py` — detection

```python
@dataclass(frozen=True)
class DetectedSpeaker:
    name: str
    role: str | None

async def detect_speakers(
    *, transcript: str, title: str, description: str | None,
    tags: list[str], model: str, api_key: str, base_url: str | None,
) -> list[DetectedSpeaker]:
    """Ask the LLM for the named speakers in this video. Returns [] when
    it can't confidently name anyone (anonymous/single-narrator video).
    Robust JSON parse — reuse the same envelope-unwrap the summarizer
    uses for highlights (the 'illegal JSON escapes' repair path), so a
    chatty model wrapping JSON in prose still parses; on total parse
    failure return []."""
```

- Caps the list (e.g. ≤ 8 speakers) and de-dupes by normalised name.
- No network beyond the one LiteLLM call; never raises (best-effort,
  like related-links / stock-image-query in the pipeline).

### New `services/speaker_chat.py` — the persona reply

Mirrors `chat.stream_reply` exactly (same LiteLLM streaming kwargs,
reuses `chat_core.build_messages`) but with a persona system prompt:

```python
SPEAKER_SYSTEM_TEMPLATE = (
    "You are role-playing as {name}{role_clause}, a speaker in this "
    "video, in a conversation with a viewer. Reply in the first person, "
    "in their voice and style.\n\n"
    "GROUNDING RULES (important):\n"
    "- Base what you say FIRST on what {name} actually says in the "
    "transcript below. Stay consistent with the positions they take in "
    "THIS episode.\n"
    "- You MAY draw on {name}'s generally-known public views to stay in "
    "character, but NEVER invent specific facts, numbers, or quotes and "
    "present them as things said in this episode.\n"
    "- If the viewer pushes you on something not covered in the episode, "
    "you may respond in character but make clear you're going beyond what "
    "was said here.\n"
    "- Don't break character to disclaim you're an AI — the interface "
    "already tells the viewer this is a simulation.\n\n"
    "{persona_note_block}"
    "{seed_block}"
    "FORMAT AS MARKDOWN: short, scannable paragraphs; **bold** key "
    "terms; bullets for lists.\n\n"
    "TRANSCRIPT:\n{transcript}"
)
```

- `seed_block` is present only on a jump-in: *"The viewer is reacting to
  this moment of the episode — [12:04] '…the actual quote…'. Take that
  as the thing they're responding to."*
- `persona_note_block` injects the user's optional `persona_note`.
- `stream_speaker_reply(*, speaker, transcript, history, user_message,
  seed_ts, seed_quote, model, api_key, base_url)` → async token
  iterator, identical mechanics to `stream_reply`.

The whole transcript is the context (same as today's video chat — no
retrieval step, consistent with the existing design).

## Routes

### Speaker chat (`routes/speaker_chat.py`, or extend `routes/chat.py`)

`POST /v/{video_id}/speaker/{speaker_id}/chat` — the persona turn.
Same shape as `post_chat`: ownership-checks the video AND the speaker
(foreign profile / wrong video → 404), resolves the model the same way,
loads **speaker-scoped** history, appends the user message (with the
optional `(re: …)` seed prefix), streams via `stream_speaker_reply`,
collects, persists the assistant turn with `speaker_id`, returns the
same `_msg_html` user+assistant fragment. Optional form fields
`seed_ts` (int seconds) and `seed_quote` (str) drive the seed block.

### Speaker management (`routes/speakers.py`)

- `POST /v/{video_id}/speakers/detect` — runs `detect_speakers`,
  inserts any new `auto` rows (skips names already present), returns the
  refreshed speaker-picker fragment. For older videos with no speakers
  yet, and as a "re-detect" button.
- `POST /v/{video_id}/speakers` — add a `manual` speaker (name, role,
  avatar_id) → refreshed picker fragment.
- `POST /v/{video_id}/speakers/{id}/edit` — rename / role / avatar /
  persona_note.
- `POST /v/{video_id}/speakers/{id}/delete` — remove (cascades its chat
  messages via the FK / explicit delete).

All HTMX fragment swaps, consistent with the rest of the app.

## UI (`video_detail.html` + a few partials)

### Speaker picker + mode switch

A row at the top of the existing `<section class="chat">`:

> **Talk to:** [ the video ] [ 🟦 Chamath ] [ 🟩 Jason ] … [ + ⚙ ]

- "the video" is the default and posts to the unchanged
  `/v/{id}/chat`. Each speaker chip, when selected, `hx-get`s a chat
  panel fragment (`/v/{id}/speaker/{sid}/panel`) that swaps the
  history + composer into speaker mode (composer now posts to the
  speaker endpoint; a **disclaimer banner** appears).
- Avatars reuse the curated `services/avatars.py` library — assign one
  per speaker (round-robin default at detection time, user-editable).
- "⚙" opens the small manage panel (detect / add / rename / delete).

### Disclaimer banner (transparency — non-negotiable)

In speaker mode, a persistent banner sits above the thread:

> ⚠️ **Simulated.** This is an AI impression of *{name}* based on this
> episode — not their real words or views.

Visually distinct speaker bubbles (tinted with the speaker's avatar
colour) reinforce "this isn't the neutral assistant."

### Transcript jump-in

Each `transcript-block` already renders a timestamp anchor. Add a small
`💬 Discuss` button per block. With a speaker selected it seeds that
speaker's composer with `seed_ts`/`seed_quote` from the block; with none
selected it opens the picker first. Pure progressive enhancement —
no behaviour change to the existing timestamp-seek links.

### "Discuss this moment" (live player position)

A button next to the player. The existing player IIFE in
`video_detail.html` owns the `YT.Player` instance and a `[data-yt-
timestamp]` click handler. Extend that script to expose a tiny helper
(e.g. set `window.__ytCurrentTime = () => player && player.getCurrentTime()`
once the player is ready, plus a nearest-block lookup over the rendered
`[data-yt-timestamp]` blocks). The button reads the current time, finds
the transcript block at/just-before it, and opens the speaker chat
seeded with that block — i.e. *pause the video, hit "discuss this
moment," start arguing.* Gracefully no-ops (falls back to the plain
picker) if the player hasn't been instantiated yet.

## Pipeline integration (`pipeline.py`)

Add a best-effort `set_step("identifying speakers")` step **after**
summarization, forward-only and gated like the other enrichment steps:
YouTube-kind, transcript present, an LLM configured. It calls
`detect_speakers` and inserts `auto` rows. Failure leaves the video with
no speakers (the picker just shows "the video" + a "Detect speakers"
button) — never fails the job. Older videos get speakers on demand via
the detect endpoint. (Same best-effort posture as `_store_related_links`
and the stock-image query.)

## Testing strategy

House style: no live LLM/network (completions mocked); render via
TestClient; in-memory SQLite + sqlite-vec; no browser.

- **`detect_speakers`** (mocked completion): clean JSON list → parsed &
  capped & de-duped; prose-wrapped JSON → still parsed (envelope
  unwrap); garbage → `[]`; empty/short transcript handled.
- **Schema/migration:** `video_speakers` exists; `chat_messages.speaker_id`
  added idempotently; `init_schema` + migration run twice cleanly.
- **`video_speakers` repo:** CRUD; per-profile scoping; ordering by
  `sort_order, id`.
- **`chat` repo:** `history(video_id)` (speaker_id IS NULL) excludes
  speaker turns; `history(video_id, speaker_id=N)` returns only that
  speaker's; `append` with `speaker_id` round-trips.
- **`stream_speaker_reply`** (stubbed completion): persona system prompt
  carries the speaker name + grounding rules + transcript; seed block
  present only when `seed_ts`/`seed_quote` given; reuses
  `build_messages` ordering.
- **Routes:** `POST /v/{id}/speaker/{sid}/chat` persists user+assistant
  with `speaker_id`, renders the fragment; foreign profile / mismatched
  video → 404; detect/add/edit/delete endpoints update the picker;
  seed fields produce the `(re: …)` user-message prefix.
- **Video-chat regression (critical):** existing
  `tests/test_services_chat.py` + `tests/test_routes_chat.py` stay green
  **unchanged** — proves the `speaker_id`-defaults-NULL extension is
  behaviour-preserving for the existing thread.

## Out of scope (v1)

- Real audio **diarization** (pyannote) / per-segment speaker
  attribution. The persona is whole-transcript-grounded with a name
  focus.
- **Voice** for the persona (could later pair with the existing Piper
  TTS to *hear* the simulated reply — natural follow-up, not now).
- Cross-episode / cross-video persona memory (each thread is one video).
- Live token streaming (keep the current collect-then-render, same as
  today's video chat).
- Auto-detecting speakers for web articles / newsletters (YouTube-kind
  only in v1).

## Transparency & ethics (design constraint)

Simulating identifiable public figures is acceptable here **because of
explicit, persistent labelling**: the disclaimer banner, the distinct
speaker styling, and a prompt rule against fabricating episode-specific
facts. The model speaks in-character but the *interface* — never the
fake person — owns the "this is a simulation" message. No persona reply
is presented as the real individual's actual words.

## Rollout

Single PR. One new table + one `ALTER TABLE ADD COLUMN` (both via the
existing SCHEMA / migration mechanism), one new detection service, one
persona-chat service modelled on `stream_reply`, a speakers repo, two
small route modules, and the `video_detail.html` additions. The existing
video chat is untouched (new column defaults NULL), guarded by its
current tests. The pipeline gains one best-effort enrichment step.
