# yt-summary — Design Spec

**Date:** 2026-05-05
**Status:** Approved for planning
**Owner:** Stefan

## Purpose

A self-hosted web UI around `yt-dlp` that turns a YouTube URL into a written summary, with a per-video chat interface for follow-up questions. Designed to run on a Mac for development and on a Raspberry Pi (4/5, ARM64) in daily use, deployed as a single Docker container.

Single user. Local network only. No authentication, no multi-user concerns, no security boundary beyond "don't expose it to the internet".

## Core User Flows

1. **Submit URL → Summary**: User pastes a YouTube URL. The system fetches metadata immediately (title, thumbnail, description), then runs the pipeline asynchronously and shows the summary when ready.
2. **Re-query a video**: Each video has a chat interface seeded with its transcript as context. User asks follow-up questions (e.g. "what does the speaker say at minute 12?", "explain point 3 again").
3. **Public permalink**: Each video has a stable URL serving its summary + transcript. A `.md` variant returns raw Markdown for copying into other tools.
4. **Search**: Browse and search past videos by title and description.

## Pipeline

```
URL submitted
   │
   ├─► [1] yt-dlp metadata (title, thumbnail URL, description, duration, video ID)
   │       Persisted immediately. UI shows the card right away.
   │
   ├─► [2] Job enqueued in SQLite jobs table. Worker picks it up.
   │
   ├─► [3] Transcript acquisition (in priority order):
   │         a) Manual subtitles via yt-dlp
   │         b) Auto-generated captions via yt-dlp
   │         c) Audio download (yt-dlp -x) + faster-whisper transcription
   │
   ├─► [4] Transcript persisted to DB.
   │
   ├─► [5] LLM summary via LiteLLM:
   │         - Count tokens against `litellm.get_max_tokens(model)`
   │         - If transcript + prompt fits: single-shot
   │         - Else: map-reduce (chunk → per-chunk summary → final summary)
   │
   ├─► [6] Summary persisted. UI updates live (HTMX polling).
   │
   └─► [7] Chat enabled. Transcript is the system context. SSE streaming.
```

## Tech Stack

- **Language:** Python 3.12
- **Web framework:** FastAPI
- **Templates:** Jinja2
- **Frontend interactivity:** HTMX + Alpine.js for small client-side state. No build step.
- **Database:** SQLite (single file, bind-mounted from host)
- **YouTube:** yt-dlp
- **ASR fallback:** faster-whisper (CTranslate2 backend), default model `small`, language auto-detect
- **LLM client:** LiteLLM (provider-agnostic; OpenAI / Anthropic / Gemini / Groq / Ollama / etc.)
- **Job queue:** SQLite-backed, polled by an in-process asyncio worker. No Redis.
- **Container:** single Docker image, multi-arch (linux/amd64 + linux/arm64)

## Data Model

SQLite schema (column types abbreviated):

- **`videos`**: `id` (YouTube video ID, PK), `url`, `title`, `description`, `thumbnail_path`, `duration_seconds`, `transcript`, `transcript_source` (`manual_subs` | `auto_subs` | `whisper`), `summary`, `summary_model`, `created_at`, `updated_at`.
- **`jobs`**: `id`, `video_id` (FK), `state` (`pending` | `running` | `done` | `failed`), `step` (free-text current step for UI display, e.g. "downloading audio", "transcribing"), `error_message`, `created_at`, `updated_at`. Composite index on (state, created_at) for FIFO worker pickup.
- **`chat_messages`**: `id`, `video_id` (FK), `role` (`user` | `assistant`), `content`, `created_at`.
- **`settings`**: simple key-value table. Keys: `llm_provider`, `llm_model`, `llm_api_key`, `llm_base_url` (optional), `whisper_model` (default `small`), `youtube_cookies_path` (path to Netscape-format file written by curl-paste flow).

Search: SQLite FTS5 virtual table over `videos.title || videos.description` for fast text search.

## Filesystem Layout (inside container)

```
/data/                       # bind-mounted from host
  app.db                     # SQLite
  cookies.txt                # Netscape-format YouTube cookies
  thumbnails/<video_id>.jpg
  audio/<video_id>.<ext>     # downloaded only when whisper fallback runs; deleted after transcription
```

The `/data` directory is the only piece of host state.

## HTTP Surface

- `GET /` — Home: search bar, list of videos (cards with thumbnail, title, status).
- `POST /videos` — Submit a new URL. Returns the freshly created video card via HTMX.
- `GET /v/{id}` — Video detail page (summary, transcript collapsible, chat).
- `GET /v/{id}.md` — Public Markdown export (title, summary, transcript). No auth.
- `GET /v/{id}/status` — HTMX status fragment, polled every 2s while job is `pending` or `running`.
- `POST /v/{id}/chat` — SSE endpoint streaming the assistant response.
- `GET /settings` — Settings UI.
- `POST /settings` — Save settings.
- `POST /settings/youtube-curl` — Accepts a pasted curl command, parses cookies from the `Cookie:` header, writes Netscape-format `cookies.txt`.

## Settings UI

A gear icon in the header opens a settings page with these fields:

- **LLM Model** (text, in LiteLLM format: `openai/gpt-4o`, `anthropic/claude-sonnet-4-6`, `gemini/gemini-2.0-flash`, `groq/llama-3.3-70b-versatile`, `ollama/llama3.1`, etc. — the prefix tells LiteLLM which provider to route to)
- **LLM API Key** (password input)
- **LLM Base URL** (optional, for self-hosted OpenAI-compatible servers like LM Studio / vLLM)
- **Whisper Model** (dropdown: `tiny` / `base` / `small` / `medium` / `large-v3`)
- **YouTube Cookies** — single textarea labeled "Paste curl from DevTools". Backend extracts the `Cookie:` header and writes `cookies.txt`. Status indicator below: "✓ cookies set" with a "clear" button.

## Worker Behavior

A single asyncio task started at app boot:

```
loop forever:
  pick oldest pending job (UPDATE ... RETURNING with state='running')
  run pipeline steps, updating job.step as we go
  on success: state='done'
  on exception: state='failed', error_message=str(e)
  if no job available: sleep 1s
```

Jobs run sequentially. This is intentional: on a Pi, parallel whisper transcription would thrash. On a Mac it's fine — humans don't paste 5 URLs in 5 seconds anyway.

Worker survives container restarts because state lives in SQLite. Jobs left in `running` state at startup are reset to `pending` (assumed interrupted).

## Error Handling

- Network failure during yt-dlp: job → `failed`, `error_message` shown in UI with a "retry" button that re-enqueues.
- Whisper OOM / failure: same.
- LLM error (rate limit, bad key, model not found): same. Settings page link in error message.
- Cookies expired (yt-dlp returns sign-in error): job → `failed` with message "YouTube cookies expired, please re-paste in Settings".

No automatic retries. Errors surface immediately. User decides to retry.

## Deployment

### Docker

A single multi-stage `Dockerfile` produces one image. ARM64 + AMD64 via Docker Buildx.

`docker-compose.yml` for local use:

```yaml
services:
  yt-summary:
    image: ghcr.io/<user>/yt-summary:latest
    ports: ["8000:8000"]
    volumes: ["./data:/data"]
    restart: unless-stopped
```

Host requirements: Docker. Nothing else.

### GitHub Actions

Two workflows in `.github/workflows/`:

1. **`ci.yml`** — On push and PR: lint (ruff), type-check (mypy or pyright), tests (pytest).
2. **`release.yml`** — On tag push (`v*`): build multi-arch image with Buildx, push to GHCR (`ghcr.io/<user>/yt-summary`) tagged with version + `latest`.

## Out of Scope (V1)

Captured here so they don't sneak in:

- Multi-user / authentication / authorization
- Multi-language summaries (re-querying handles this if needed)
- Sharing controls on permalinks (they're public on the LAN; that's the model)
- Bulk import (playlists, channel subscriptions)
- Scheduled re-summarization or notifications
- External LLM cost tracking dashboard (LiteLLM exposes the data; we don't surface it yet)
- Mobile-optimized layout polish (works on mobile, not optimized)

## Open Implementation Questions

These are decisions to make during implementation, not now:

- Exact Jinja layout structure (base template with header/main slots).
- Whether to use `aiosqlite` or thread-pool wrapping for SQLite from FastAPI. Default to `aiosqlite` unless it complicates queries.
- HTMX SSE vs. plain SSE for chat streaming.
- Transcript chunk size for map-reduce: start with ~50% of model context, tune later.
