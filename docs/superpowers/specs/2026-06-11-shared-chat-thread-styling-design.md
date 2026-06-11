# Shared Chat-Thread Styling

**Status:** Draft — design phase
**Date:** 2026-06-11

## Goal

Two surfaces show a chat conversation — the per-video chat and the new
"ask my library" thread — but they look different and the ask thread is
visually weak (flat grey question box, unstyled composer, no chat
structure, full-width text). Introduce **one shared chat-thread visual
language** used by both, lifting the conversation metaphor and fixing
the ask thread's rough edges.

This is styling only — no behaviour, route, or data changes. The shared
message logic was already extracted (`chat_core.build_messages`); this
unifies the *look*.

## Design decisions (settled)

- **One shared class set** `chat-thread-*`, both templates migrated to
  it; old `.chat-msg*` / `.ask-turn*` / `.chat-form` / `.ask-followup`
  CSS replaced.
- **Project palette only** — no hardcoded indigo. Accent is the existing
  `--brand-green` (#00d4a4); user bubble uses `--primary` (#0a0a0a) on
  white text; surfaces/borders use `--surface`/`--hairline`; radii use
  `--rounded-*`.
- **Sticky, not fixed** composer (fixed risks breaking the existing
  header/layout). Sticky to the bottom of the thread column.
- **Narrow column scoped to the chat**, NOT global. A `.chat-thread`
  wrapper caps width (~760px) and centres; `main` is untouched so other
  pages are unaffected.

## The shared classes

| Class | Role |
|---|---|
| `.chat-thread` | column wrapper: flex-column, gap, max-width ~760px, centred |
| `.chat-bubble-user` | the user's turn — right-aligned dark bubble (`--primary` bg, white text, asymmetric radius), max-width ~78% |
| `.chat-answer` | the assistant's answer — card offset with a left `--brand-green` accent border, `--surface-soft` bg |
| `.chat-answer-controls` | row under an answer (e.g. export menu) |
| `.chat-sources` | the sources block |
| `.chat-source-chip` | one source as a pill/chip (replaces inline links) |
| `.chat-composer` | the input row — sticky bottom, pill input + `--brand-green` send button |
| `.chat-pending` / `.chat-failed` | the spinner / error states for an in-flight or failed answer |

All defined once in `app/static/app.css`, replacing the current
`.chat-*` (≈ lines 640-700) and `.ask-thread`/`.ask-turn*`/`.ask-followup`
blocks.

## Template changes

### Ask thread (`app/templates/ask/_turns.html`)
Already renders user/pending/failed/answer turns + sources + follow-up
form. Re-class:
- user turn `<div class="ask-turn ask-turn-user"><p>` → `class="chat-bubble-user"`
- pending → `class="chat-pending"`; failed → `class="chat-failed"`
- answer `<article class="ask-result">` stays, wrapped in
  `class="chat-answer"`; its export-menu row → `class="chat-answer-controls"`
- sources `<section class="ask-sources">` → `class="chat-sources"`, each
  `<li><a>` → an `<a class="chat-source-chip">`
- follow-up `<form class="ask-followup">` → `class="chat-composer"`
- the `.ask-thread` wrapper → `class="chat-thread"`

### Video chat (`app/templates/_chat_message.html`, `video_detail.html`)
- `_chat_message.html`: `chat-msg chat-msg-{{role}}` → for `user`,
  `chat-bubble-user`; for `assistant`, `chat-answer`. The role-label
  `<strong>` and `chat-content` stay inside.
- `video_detail.html`: the chat history container keeps its id; the
  `chat-form` → `chat-composer`. The `.chat` section wrapper gains
  `chat-thread` (or wraps its history in it) so width/centering match.
- The video chat streams plaintext (`white-space: pre-wrap` on the
  answer content) — preserve that; `.chat-answer` must not break the
  pre-wrap rendering.

## Testing strategy

Styling is CSS — not unit-tested in pytest. Guard with:
- **Render assertions (TestClient):** the ask thread page contains
  `chat-bubble-user`, `chat-answer`, `chat-composer`, `chat-source-chip`
  (when sources exist); the video detail page's chat renders
  `chat-bubble-user`/`chat-answer` and `chat-composer`.
- **Regression:** the existing chat + ask route/service tests stay green
  (they assert behaviour and key markup like the export menu / fragment
  polling, which must survive the re-class).
- **Manual browser pass** against `:8210`: ask thread + video chat both
  show right-aligned user bubbles, green-accented answer cards, chip
  sources, a sticky pill composer; narrow centred column; dark mode of
  the existing vars still legible.

## Out of scope

Avatars, message timestamps, typing animations, markdown for the user's
own turn (kept as escaped plaintext), and any layout change outside the
two chat surfaces.

## Rollout

Single PR: shared CSS block + the two template re-classings + render
assertions. No schema/route/behaviour changes.
