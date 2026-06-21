# Speaker Dossiers Change Log

**Date:** 2026-06-21
**Supersedes:** `2026-06-20-video-speaker-chat-design.md`
**New spec:** `2026-06-21-speaker-dossiers-and-persona-chat-design.md`

## Why this change exists

The original draft framed the feature as "Chat with the Speakers" and
planned a very lean first version: detect known show participants from
metadata, add speaker chips, and let the user chat with a simulated speaker
inside the existing video chat box.

The review found that this was too thin to support the product promise. A
metadata-only implementation can identify likely participants, but it cannot
reliably attribute every statement in a multi-speaker transcript. That makes
first-person persona chat risky and less useful.

The new direction moves the product center from celebrity-style roleplay to
evidence-backed speaker dossiers.

## Product changes

- Reframed the promise from "talk to them in real time" to "question a
  speaker's record across your videos and sources."
- Kept simulated persona chat, but made it secondary to the sourced dossier.
- Added speaker pages to the first useful release instead of deferring them.
- Added user-provided speaker sources: URLs, pasted text, and manual notes.
- Added sourced claims as the core track-record unit.
- Made claim evidence visible and reviewable.
- Tightened the persona posture so it does not claim to be the real person.

## Seed data changes

- Kept seeded known shows.
- Kept seeded known speakers.
- Removed seeded known positions from the product direction.
- Limited known speaker seed data to identity, role, shows, avatar, and
  style note.
- Explicitly banned public-position seed data unless the decision is reopened
  later.

## Data model changes

- Renamed the concept from a generic seed catalog to two narrower concepts:
  `known_shows` and `known_speakers`.
- Added `speaker_sources` so the dossier has source material beyond videos.
- Replaced thin `speaker_statements` with evidence-backed `speaker_claims`.
- Added evidence fields: evidence text, timestamp range, text offsets,
  confidence, extraction method, and review status.
- Added `chat_threads` to avoid whole-speaker chats using `NULL` or sentinel
  `video_id` values.

## Rollout changes

- Replaced the earlier lean v1 with Phase 1.5:
  known shows, known speakers, speaker chips, speaker page, sources, claims,
  and grounded simulated chat.
- Moved embeddings, contradiction discovery, and stronger retrieval to Phase
  2.
- Moved alias suggestions, bulk backfill, and richer source importers to
  Phase 3.

## Risk changes

- Called out that metadata detects participants, not statement attribution.
- Treated LLM claim extraction as imperfect by default.
- Made manual correction and review status part of the core workflow.
- Treated pasted text as user-owned evidence, not global truth.
- Reduced public-figure risk by removing seeded position catalogs.
