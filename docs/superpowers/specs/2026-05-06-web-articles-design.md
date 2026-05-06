# Web Articles — Design Spec (MVP)

**Date:** 2026-05-06
**Status:** Approved for implementation
**Owner:** Stefan
**Builds on:** [yt-summary core](2026-05-05-yt-summary-design.md)

## Purpose

Let users paste any HTTP(S) URL (not just YouTube) and get the same
summary/chat treatment for the article behind it.

MVP intentionally narrow:
- HTML pages only — no PDFs, no JS-rendered SPAs.
- Single Reader engine: `trafilatura`.
- Reuses the existing `videos` table; adds a `kind` discriminator.
- No new playlist semantics for web.
- No paywall workarounds.

## Data Model

Single new column on `videos`:

```sql
ALTER TABLE videos ADD COLUMN kind TEXT NOT NULL DEFAULT 'youtube'
    CHECK(kind IN ('youtube','web'));
```

ID format for web items: `web-<11 chars from sha256(url)>`. Same length
as YouTube IDs (11 alphanumeric), prefixed so a glance at the ID tells
you the kind.

`videos.transcript` keeps its name but its semantic meaning broadens:
"the body text we extracted from the source." For YouTube videos that's
still the spoken transcript; for web articles, the article body.

`videos.transcript_source` gets a fourth allowed value: `'web'`. Migration
adjusts the CHECK constraint.

Tags, chat, summary, jobs all work unchanged — they reference `videos.id`
and don't care about kind.

Playlists are skipped for web items: the `POST /playlists` route only
accepts YouTube playlist URLs (already enforced today via the
`?list=` regex). A `playlist_videos` row could in principle reference
a web id, but the only producer is `playlist_sync.py`, which always
inserts kind='youtube' videos.

## URL Classification

A new helper `classify_url(url)` returns `'youtube'` or `'web'`:

- `youtube` if the existing `parse_video_id(url)` succeeds.
- Otherwise `web`. (We don't validate the URL itself — yt-dlp / reader
  failures will surface the real error to the user.)

`POST /videos` calls `classify_url` and dispatches.

## Components

### `app/services/reader.py` (new)

```python
@dataclass(frozen=True)
class ArticleMetadata:
    url: str          # canonical URL (after redirects)
    title: str
    description: str  # site description / og:description
    body: str         # plain text, no HTML
    thumbnail_url: str | None  # og:image if present

async def fetch_article(url: str) -> ArticleMetadata
```

Implementation: trafilatura's `fetch_url` + `extract(..., output_format='txt')`.
Title and description come from trafilatura's metadata extractor. The
canonical URL is whatever trafilatura/requests landed on after redirects.
`thumbnail_url` reads from `og:image` if available; otherwise None.

If trafilatura returns None for the body (couldn't parse), raise
`ValueError("could not extract article body from <url>")`.

### `app/services/url_classify.py` (new)

```python
def classify_url(url: str) -> Literal["youtube", "web"]
def web_id_from_url(url: str) -> str  # "web-" + sha256(url)[:11]
```

Both are pure-sync helpers.

### `app/pipeline.py` (modify)

`process_video` branches on `video.kind`:

```python
if video.kind == "web":
    if not video.transcript:
        # Re-fetch the article body
        article = await fetch_article(video.url)
        await videos_repo.set_transcript(
            db, video_id, article.body, TranscriptSource.WEB,
        )
        text = article.body
    else:
        text = video.transcript
else:
    # existing youtube path
    ...
```

### `app/services/transcript.py` (modify)

Adds `TranscriptSource.WEB` to the model enum and DB CHECK, no
behavioural change to `obtain_transcript` itself (the orchestrator
remains youtube-only — the pipeline branches *before* it for web).

### `app/routes/videos.py` (modify)

`POST /videos` becomes:

```python
kind = classify_url(url)
if kind == "youtube":
    # existing flow
else:
    article = await fetch_article(url)
    video_id = web_id_from_url(article.url)
    # download thumbnail if any, upsert_metadata with kind='web',
    # set_tags_for_video (empty for web), enqueue job.
```

The detail route is unchanged structurally — it just needs to know the
kind so the template can adjust labels.

### Templates (modify)

- `video_detail.html`:
  - "Watch on YouTube" → "Open original ↗" if `video.kind == 'web'`.
  - "Transcript (manual_subs)" disclosure → "Article" for web.
  - The "Generate summary / Re-summarize / Retry" button label stays.
- `video_card.html`:
  - If `video.thumbnail_path` is present, show it as today.
  - Otherwise show a placeholder with a 🌐 emoji for web, ▶ for
    youtube (purely cosmetic — most YouTube items have thumbnails).

A small `kind`-pill in the card header would also be nice (e.g.
`YOUTUBE` / `WEB` in micro-uppercase) so you can scan the library by
type. V1: yes, included.

## Settings

No new settings. Reader uses defaults; future additions (different
extractors, paywall cookies) come later.

## Tests

- `tests/test_services_url_classify.py` — youtube/web classification, web_id determinism
- `tests/test_services_reader.py` — extract from a recorded HTML fixture, handle empty body, thumbnail extraction
- `tests/test_routes_videos.py` — POST /videos with web URL, redirect, persistence, kind='web'
- `tests/test_pipeline.py` — pipeline branches on kind, web path uses fetch_article instead of obtain_transcript
- `tests/test_db.py` — migration adds kind column, sets default for existing rows

~10 new tests.

## Out of Scope (V1, deferred)

- PDF / DOCX support
- JS-rendered SPAs (would need playwright)
- Paywall handling (cookies for paid news sites)
- Multi-extractor fallback (trafilatura → readability → raw)
- "Read later" / Pocket import
- RSS feed subscriptions (the web equivalent of playlists)
- Re-fetching when an article changes
- Print-stylesheet preference
