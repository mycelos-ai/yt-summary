# Audio (TTS) — Design Spec

**Date:** 2026-05-12
**Status:** Approved for planning
**Owner:** Stefan
**Builds on:** [yt-summary core](2026-05-05-yt-summary-design.md), [playlists](2026-05-06-playlists-design.md)

## Purpose

Turn any summary or transcript into a downloadable MP3, optionally
translated into another language first. The driving use case: a user
watches an English-language video, the app summarizes it, and the
user wants to send the summary as audio to someone who only speaks
German. Two operations chained:

1. Translate the source text into the target language (only if the
   source language differs).
2. Render the resulting text as an MP3 with a chosen voice.

The whole pipeline runs locally on the Pi — no cloud TTS dependency,
no second API key, no extra credentials to manage. The user already
has an LLM configured for summarization; translation reuses that
exact configuration.

## Scope

In scope:

- Render audio from a summary OR a transcript
- Five target languages: German (de), English-US (en-US), English-UK
  (en-GB), French (fr), Spanish (es)
- Local TTS via Piper running in the same yt-summary container —
  no sidecar
- On-demand voice download from Hugging Face when the user picks a
  voice that isn't on disk yet
- LLM-based translation using the user's already-configured LLM
  provider, with overlap-context chunking for transcripts long
  enough to exceed a single LLM call
- Background TTS worker, separate from the existing video worker so
  long Whisper runs don't block audio renders
- Cache: identical (source, target_language, voice, quality) renders
  reuse a previously generated MP3
- In-page audio player + MP3 download link on the video detail page
- Cleanup: TTS files for a video are deleted when the video itself
  is deleted (FK cascade)

Out of scope (explicitly):

- Cloud TTS providers (OpenAI / Gemini / Google Cloud TTS) — added
  later if real demand surfaces. Piper covers the stated use case.
- Voice cloning, custom voices, SSML markup, prosody control
- Backfill of historical TTS for old videos — the user explicitly
  said no
- Sentence-level alignment between original and translated audio
- Per-paragraph re-listen / per-segment seek inside the rendered MP3
  (the MP3 plays linearly; the only seek control is the browser's
  default audio scrubber)
- Background pre-rendering when a summary is created. TTS is opt-in
  per video, never automatic.
- Per-user TTS settings (one household default; multi-user comes
  with auth)
- Streaming partial audio while the render is still in progress.
  The render is short enough (Piper is ~5× realtime on Pi 5 for
  medium voices) that polling for a finished file is fine UX.

## Chunk-Based Render Progress

The "polling for a finished file is fine UX" assumption above turned
out to be optimistic for hour-long transcript renders. On a Pi 5, a
60-minute audio rendering means ~12 minutes wall time during which the
modal's `step` would otherwise read `rendering audio` with no further
detail.

`PiperVoice.synthesize_wav` is atomic from the caller's perspective —
no internal progress callback. The worker therefore splits the final
text at sentence boundaries (regex `(?<=[.!?])(?=\s+[A-ZÄÖÜ])` —
robust against "Mr. Smith", URLs, and other false positives) and
calls `render_chunks_to_mp3(chunks, …, progress=cb)`. The callback
fires after each chunk's WAV is synthesised and translates to
`set_step(f"rendering audio chunk {done}/{total}")` so the modal's
polling reflects per-chunk progress.

Chunking at sentence boundaries means the concat point lands at a
silence Piper would have emitted anyway — no audible artifacts.
Per-chunk synthesis does lose some cross-sentence prosody context,
but at the default 25 sentences per chunk it's not perceptible in
informal listening tests. Texts without parseable sentence structure
(non-Latin scripts, no terminators) fall back to a single chunk —
same behaviour as before the chunking change.

**Future enhancement (not implemented):** writing per-chunk WAVs to a
stable per-job partial dir (e.g. `tts-audio/<video_id>/.partial/`)
would enable crash-resume on long Pi renders. Out of scope for the
progress patch; chunks land in a `TemporaryDirectory()` and are
discarded after concat.

## Crash recovery: orphan reset on startup

When the container restarts while a TTS job is mid-flight, the
in-memory worker is gone but the DB row still says
`status='translating'` or `status='rendering'`. The new worker can't
re-pick it (it only claims `'queued'`), and the row would be a
permanent ghost — UI shows it as in-progress, the UNIQUE constraint
blocks a manual retry.

`tts_jobs_repo.reset_orphaned_active(db)` runs at app boot (in
`lifespan` before the worker task starts) and moves any row in
`'translating'`/`'rendering'` back to `'queued'` (also nulling
`started_at`). Mirrors the existing `jobs_repo.reset_orphaned_running`
pattern. A boot-log warning fires when rows are reset, useful for
debugging.

## Data Model

### Language metadata on the `videos` table

In the current schema, summary and transcript are columns ON the
`videos` table — there are no separate `summaries` or `transcripts`
tables. Add three nullable language columns next to the existing
content columns. Populated for new videos going forward; old rows
stay `NULL` and every code path that needs the language has a
fallback.

```sql
ALTER TABLE videos ADD COLUMN source_language     TEXT;
ALTER TABLE videos ADD COLUMN summary_language    TEXT;
ALTER TABLE videos ADD COLUMN transcript_language TEXT;
```

Conventions:

- `source_language`: BCP-47-ish two-letter code of the original
  spoken audio. `en`, `de`, `fr`, `es`. From Whisper's `info.language`
  when we transcribe; from the YouTube VTT `Language:` header when we
  pull subs; from a one-shot LLM detection on the summary text as a
  fallback if neither is available.
- `summary_language`: language the summary text is ACTUALLY in.
  Equals the `summary_language` setting unless it was `auto`, in
  which case it equals `videos.source_language`. (Note: the schema
  column has the same name as the setting — they live in different
  tables so there's no ambiguity, but mind the difference in code.)
- `transcript_language`: language of the transcript text. Equals
  `videos.source_language` in 100% of the cases we know about. The
  column exists for symmetry and future-proofing (e.g. dubbed-audio
  videos where Whisper picks up the dub rather than the captions).

The pipeline writes these columns on every NEW summary / transcript
it creates. Existing rows are not migrated — they stay `NULL`, and
the TTS code treats `NULL` as "language unknown, default the dropdown
to the user's primary content language, let them pick".

### TTS jobs

A new worker queue separate from `jobs` (which is video-processing).

```sql
CREATE TABLE tts_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('summary', 'transcript')),
    target_language TEXT NOT NULL,
    voice TEXT NOT NULL,              -- 'thorsten' / 'lessac' / …
    quality TEXT NOT NULL CHECK (quality IN ('low', 'medium', 'high')),
    status TEXT NOT NULL DEFAULT 'queued',
        -- 'queued' | 'translating' | 'rendering' | 'done' | 'failed'
    step TEXT,                        -- free-text progress for UI
    translated_text TEXT,             -- NULL when no translation needed
    audio_path TEXT,                  -- relative to data_dir
    duration_seconds REAL,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    UNIQUE (video_id, source, target_language, voice, quality),
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
);

CREATE INDEX idx_tts_jobs_status ON tts_jobs(status);
CREATE INDEX idx_tts_jobs_video ON tts_jobs(video_id);
```

The `UNIQUE` constraint is the cache: re-requesting the same
combination returns the existing row rather than enqueuing a
duplicate render.

The `ON DELETE CASCADE` ensures stale rows go away when a video is
removed. The on-disk MP3 cleanup happens in the cascade-delete
trigger (see "Cleanup" section).

### TTS voices catalogue

Hardcoded in code, NOT in the DB. A small Python module ships a
curated list of `(language, voice_id, available_qualities,
display_name)` tuples. Initial list:

| Lang   | Voice ID                            | Qualities             | Display name           |
|--------|-------------------------------------|-----------------------|------------------------|
| de     | thorsten                            | low, medium, high     | Thorsten (m, neutral)  |
| de     | thorsten_emotional                  | medium                | Thorsten (m, emotional)|
| de     | kerstin                             | low                   | Kerstin (f)            |
| en_US  | lessac                              | low, medium, high     | Lessac (f)             |
| en_US  | amy                                 | low, medium           | Amy (f)                |
| en_US  | ryan                                | low, medium, high     | Ryan (m)               |
| en_GB  | alba                                | medium                | Alba (f)               |
| en_GB  | southern_english_female             | low, medium           | Southern English (f)   |
| fr     | siwis                               | low, medium           | Siwis (f)              |
| es     | sharvard                            | medium                | Sharvard (m)           |

When the user picks a voice in the modal, the quality dropdown only
shows what's listed for that voice — no invalid combinations.

Voice file naming on Hugging Face follows
`{lang}_{REGION}-{voice}-{quality}` (e.g. `de_DE-thorsten-medium`).
We compute the URL from these three fields and download two files
per voice: `.onnx` and `.onnx.json`. They land in
`data/tts-voices/`. Existing files on disk skip the download.

## On-Disk Layout

```
data/
├── tts-voices/                                 # NEW
│   ├── de_DE-thorsten-medium.onnx
│   ├── de_DE-thorsten-medium.onnx.json
│   ├── en_US-lessac-high.onnx
│   ├── en_US-lessac-high.onnx.json
│   └── …
└── tts-audio/                                  # NEW
    └── <video_id>/
        ├── summary-de-thorsten-medium.mp3
        ├── transcript-en-lessac-high.mp3
        └── …
```

Voice cache: ~80 MB per medium voice, ~130 MB per high voice. Lazy:
nothing downloads until the user explicitly picks an uncached
combination. Voices stay on disk forever — no eviction.

Audio cache: keyed by `(source, target_language, voice, quality)`.
Stays on disk until the parent video is deleted.

## Worker Architecture

Today there is exactly one worker (`app.worker.Worker`) processing
the `jobs` queue. TTS introduces a second worker:

```
                    ┌──── existing Worker ── consumes `jobs`
yt-summary main ────┤
                    └──── NEW TtsWorker ── consumes `tts_jobs`
```

Both are `asyncio.create_task` tasks started in `app.main.lifespan`.
The TTS worker is structurally identical to the existing one:

- Single concurrent job (a Pi has one chance to do CPU work at a
  time anyway)
- Polls every 1 s when idle
- Calls `tts_jobs_repo.claim_next` which atomically transitions
  `queued → running` with a started_at timestamp
- Updates the `step` column with a free-text label
  (`'translating chunk 2/5'`, `'rendering audio'`, `'concatenating
  parts'`) so the UI poll endpoint can show progress
- On exception: marks the row `failed` with the exception's string

The two workers are independent because TTS for a 30-minute video
can take 6+ minutes on a Pi (translation + Piper render). Forcing it
behind a Whisper run that just landed in the video queue would be a
bad user experience for the one feature the user is actively waiting
on.

## Translation

Reuses the user's configured LLM provider (`llm_model`, `llm_api_key`,
`llm_base_url`). No new settings, no new provider keys.

### Chunking

Transcripts can run to 50k+ characters for a 1h video. Even though
modern context windows comfortably hold that, two reasons to chunk:

1. **Quality.** A 50k-char one-shot translation degrades noticeably
   on smaller / older / cheaper LLM models. Per-chunk translation
   with overlap context preserves naming consistency without
   relying on the model holding the whole text in attention.
2. **Cost / latency robustness.** A single 50k-char call that 502s
   loses everything. Chunked retries are scoped.

Algorithm:

1. **Split** on paragraph boundaries (`\n\n`). If a paragraph is
   itself larger than the per-chunk cap, split it on sentence
   boundaries (heuristic: `. `, `! `, `? `, German `: ` etc.).
2. **Target chunk size:** 1500 words (~10k chars), large enough that
   most summaries are one chunk and a 1h transcript becomes ~5
   chunks.
3. **Overlap context window:** 50 words from the end of the
   previous chunk + 50 words from the start of the next chunk.
   Both are passed to the LLM with a clear "context only — do not
   translate" marker.

Prompt template (per chunk):

```
You are a professional translator from {source_lang_name} to
{target_lang_name}. Translate ONLY the text inside the
<TRANSLATE> tags. Use the <CONTEXT_BEFORE> and <CONTEXT_AFTER>
sections for continuity — they show what comes before and after
in the original text, but you must not include them in your output.

Preserve paragraph breaks. Keep technical terms and proper names
consistent across chunks. Do not add a preamble, do not summarize,
do not add explanations. Output only the translation.

<CONTEXT_BEFORE>
{previous_chunk_tail}
</CONTEXT_BEFORE>

<TRANSLATE>
{chunk_text}
</TRANSLATE>

<CONTEXT_AFTER>
{next_chunk_head}
</CONTEXT_AFTER>
```

The first chunk omits `<CONTEXT_BEFORE>`; the last omits
`<CONTEXT_AFTER>`. The translated chunks are concatenated with
`\n\n` — no further smoothing.

### Skip translation when source == target

If `source_language == target_language`, the translation step is
skipped entirely. The status transitions `queued → rendering →
done`, and `translated_text` stays `NULL`. The TTS step uses the
original text directly.

## Piper TTS

### Runtime

Piper Python package (`piper-tts`) is installed in the existing
yt-summary image via `pyproject.toml`. Adds ~50 MB to the image
(ONNX runtime + glue code). The voice models themselves are not
bundled — too much weight for a 99%-of-users-don't-use-it default.

### Rendering

Piper produces 16-bit PCM WAV at 22050 Hz (medium) or 22050 Hz
(high). We pipe that into ffmpeg (already in the image for Whisper
audio extraction) to produce MP3 at 128 kbps:

```
piper-tts text → WAV bytes → ffmpeg → MP3 file
```

For chunked translations: each translated chunk renders to its own
WAV, then ffmpeg concatenates the WAVs into a single MP3 using the
concat-demuxer pattern. No silence padding between chunks — Piper
already emits trailing silence that matches sentence rhythm.

### Voice downloads

When a TTS job starts and `data/tts-voices/{voice_filename}.onnx`
doesn't exist:

1. Compute the Hugging Face URL:
   `https://huggingface.co/rhasspy/piper-voices/resolve/main/{lang}/{lang}_{REGION}/{voice}/{quality}/{lang}_{REGION}-{voice}-{quality}.onnx`
   (and `.onnx.json` alongside)
2. Download both files atomically: `.partial` suffix during
   transfer, atomic rename when complete.
3. Update the job `step` to `'downloading voice ({size} MB)'`.

If the download fails, the job fails with a clear error message
that includes the URL the user can try in their browser.

## UI

### Trigger

A single button labelled `🔊 Audio` next to the existing `📋 Copy`
button on the video detail page (`/v/{video_id}`), above the summary
content.

Not on the listing pages — the click-to-render flow doesn't belong
in a card grid.

### Modal

Clicking the button opens an HTMX-driven modal with five fields:

1. **Source:** radio buttons — `Summary` (default) / `Transcript`
2. **Target language:** dropdown — de / en_US / en_GB / fr / es.
   Default resolution order (first non-`NULL` wins):
   1. If `default_tts_language` is one of the five codes → that.
   2. If `default_tts_language` is `auto` (the install default):
      `videos.summary_language` when source=summary, else
      `videos.transcript_language`, else `videos.source_language`.
   3. Hard fallback: `en_US`.
3. **Voice:** dropdown filtered by the chosen language. Shows the
   `display_name` from the catalogue.
4. **Quality:** dropdown — populated from the voice's
   `available_qualities`. Default `medium`.
5. **Submit button:** `Generate audio` → POSTs to
   `/v/{video_id}/audio/render`

A small `<p>` underneath shows estimated render time, e.g.
`Estimated time: ~2 minutes (transcript, ~9k chars, high quality on
this Pi)`. Computed from text length × per-voice-quality factor
(rough hard-coded multipliers; refined empirically later).

### Progress

After submit, the modal swaps to a polling status block (HTMX
`hx-trigger="every 2s"`). Steps shown:

```
⏳ queued
🌐 translating (chunk 2 of 5)
🎙 rendering audio
✅ done
```

When `status == 'done'`, the modal swaps one final time to show:

```
<audio controls src="..."></audio>
<a href="..." download>Download MP3</a>
```

That's all. The user can close the modal; the audio stays on the
video detail page in a persistent `Audio renderings` section that
lists all previously-rendered tracks for that video.

### Audio renderings section on the detail page

Below the summary, a collapsed-by-default `<details>` element:

```
▶ Audio renderings (3)
   – Summary, German, Thorsten medium · 2:14 · [▶] [⬇ MP3] [🗑]
   – Transcript, German, Thorsten medium · 18:46 · [▶] [⬇ MP3] [🗑]
   – Summary, English, Lessac high · 2:08 · [▶] [⬇ MP3] [🗑]
```

`🗑` deletes the rendering (job row + MP3 file). The voice file
itself stays — other videos might reuse it.

### Settings

A new `Audio (TTS)` card in `/settings`:

- **Default language:** dropdown (de / en_US / en_GB / fr / es / auto)
  — pre-selected in the render modal. Default `auto` means "let the
  modal use the source language".
- **Default voice per language:** five small lines, one per
  language, each with a dropdown of available voices. Default
  `(first in catalogue)`.
- **Default quality:** `low` / `medium` / `high`. Default `medium`.
- **Voice cache:** static text — `X voices installed, taking Y MB`.
  No delete button in V1; users can purge via `rm
  data/tts-voices/*`.

These settings drive the *defaults* in the modal only. The modal
always allows overriding them per render.

## API & MCP

One new REST endpoint (gated by the same API key, like the existing
ones):

```
POST /api/v1/videos/{video_id}/audio
Body: { "source": "summary"|"transcript",
        "target_language": "de",
        "voice": "thorsten",
        "quality": "medium" }
→ 202 Accepted, body: { "job_id": 42, "audio_url": null,
                        "cached": false }
   OR
→ 200 OK, body:      { "job_id": 17, "audio_url": "/data/.../mp3",
                        "cached": true }

GET /api/v1/videos/{video_id}/audio/{job_id}
→ { "status": "rendering", "step": "rendering audio",
    "audio_url": null }
```

No MCP tool in V1 — adding it later is a one-line registration. The
REST endpoint is enough to script "summarize, then narrate" pipelines.

## Cleanup

Two policies:

1. **Cascade on video delete.** `tts_jobs.video_id REFERENCES
   videos(id) ON DELETE CASCADE` removes the DB rows. A SQLite
   `AFTER DELETE` trigger on `tts_jobs` reads `audio_path` and
   unlinks the file. Same pattern as existing `transcripts` /
   `summaries` audio cleanup.
2. **User-triggered per-rendering delete.** The `🗑` button on the
   detail page POSTs to `/v/{video_id}/audio/{job_id}/delete`,
   which removes the row + file.

No automatic age-based purging — voice files and audio renderings
both live on the user's `data/` volume which they manage themselves.

## Settings: Defaults Summary

```
default_tts_language          # de | en_US | en_GB | fr | es | auto
default_tts_voice_de          # voice id from catalogue
default_tts_voice_en_US       # …
default_tts_voice_en_GB
default_tts_voice_fr
default_tts_voice_es
default_tts_quality           # low | medium | high
```

All keyed under `(user_id, key)` in the existing `settings` table,
honouring the playlists-era schema groundwork.

## Estimated Performance (Pi 5, CPU-only)

Reference numbers from the upstream Piper benchmarks plus the
Kokoro report — must be empirically validated during implementation:

| Scenario                          | Approx time   |
|-----------------------------------|---------------|
| 500-word summary, no translation, medium | ~10 s         |
| 500-word summary, translated, medium     | ~25 s         |
| 5000-word transcript, no translation, medium | ~60–90 s |
| 5000-word transcript, translated, medium     | ~3–4 minutes |
| Same as above, high quality              | ~6–8 minutes  |

The modal's "estimated time" text uses these numbers. Translation
time depends on the user's LLM provider; for cloud LLMs (OpenAI,
Gemini, Groq) the overhead is dwarfed by Piper rendering.

## Risks

1. **Translation quality on small local LLMs.** A 3B-parameter
   Ollama model is borderline for long-form translation; the user
   may get worse output than they'd get from a cloud LLM. Mitigation:
   the rendered text is shown alongside the audio in the modal so
   the user can spot-check. Tagging the rendering with the LLM model
   used would be a nice-to-have, but is out of scope.
2. **Voice file size on slow connections.** A `high` voice is
   ~130 MB. The first download from a freshly-installed container
   could take a couple of minutes on a slow link. Mitigation: the
   download step shows `'downloading voice (130 MB)'` in the
   progress label.
3. **Disk usage growth.** Each translated transcript adds an MP3
   that can run 20+ MB. With dozens of renderings the data volume
   grows. Mitigation: a Settings line that shows total TTS audio
   size, so the user can spot the growth before it becomes a
   problem. Out-of-scope for V1: automatic cleanup.
4. **Piper voice quality vs cloud TTS.** Piper medium is
   "comfortable to listen to" but distinctly below OpenAI / Gemini
   TTS quality. Users who try this and aren't impressed will be
   the ones asking for cloud TTS — which we plan to add in a
   follow-up. The README documents this expectation.

## Testing Strategy

- **Translation chunking:** unit-test the chunker with a 30k-char
  paragraph stream, verify chunks ≤ target size and overlap
  windows are correct.
- **Voice URL builder:** unit-test the Hugging Face URL
  construction for every catalogue entry.
- **Voice download:** mock httpx response, verify atomic-rename
  behaviour and `.partial` cleanup on failure.
- **Piper render:** unit-test on a small voice (`en_US-amy-low`,
  ~20 MB) that ships in `tests/fixtures/voices/` — committed to git
  because it's small enough.
- **TtsWorker:** end-to-end-with-mocks test that a queued job
  transitions through `translating` → `rendering` → `done` and
  that the MP3 lands at the expected path.
- **Cache behaviour:** request the same render twice, verify the
  second call returns the existing row without re-rendering.
- **Cascade delete:** delete a video, verify the `tts_jobs` rows
  and the MP3 files both disappear.

## Migration

Schema migration is straightforward — three new columns, one new
table, one new trigger. No backfill of `source_language` / `language`
for existing rows: the TTS modal handles `NULL` by defaulting to the
user's setting. The user can re-process a video manually if they
care about it specifically.
