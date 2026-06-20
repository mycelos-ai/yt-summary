# API Key Test Buttons (Pexels & YouTube) — Design

**Date:** 2026-06-20
**Status:** Approved (brainstorming complete)

## Summary

Add a "Test" button next to each of the two integration API-key fields on the
Settings page — Pexels and YouTube Data API — so the user can confirm the
**saved** key actually works instead of saving blind. Each button POSTs to a
new endpoint that runs a minimal probe call with the stored key and swaps an
HTML status fragment (✓ works / ✗ reason) into a result div, reusing the
existing test-button pattern already used for Whisper and the LLM provider
rows (`settings-test-row` / `settings-test-result` / `settings-test-hint`
classes, `status status-failed` for errors).

## Goals / Non-Goals

**Goals**
- A Test button beside the Pexels key field and the YouTube key field.
- Each tests the **stored** key (read via `settings_repo.get`) — consistent
  with the existing Whisper test.
- A minimal, cheap probe call per API: HTTP 200 + plausible body → success;
  401/403 → "key invalid / quota"; network/other → clear message.
- The API key never appears in the response fragment or logs.

**Non-Goals**
- NOT testing the just-typed (unsaved) field value — save first, then test
  (matches the Whisper pattern).
- No new dependency (httpx is already used by both services).
- No change to how keys are saved or to the indexer/stock-image behaviour.

## Components

### 1. Service probes (cheap "is the key valid?" calls)
- `app/services/stock_images.py`:
  `async def test_pexels_key(api_key: str) -> tuple[bool, str]`
  - GET `https://api.pexels.com/v1/search` with header
    `Authorization: <api_key>`, params `{"query": "test", "per_page": 1}`,
    httpx `timeout=10.0`.
  - `resp.status_code == 200` → `(True, "Pexels key works")`.
  - 401/403 → `(False, "Key rejected (invalid or quota exceeded)")`.
  - other status → `(False, f"Pexels returned HTTP {status}")`.
  - network/exception → `(False, f"Could not reach Pexels: {type(e).__name__}")`.
  - Empty `api_key` → `(False, "No key saved")` (no call).
- `app/services/playlist_index.py`:
  `async def test_youtube_key(api_key: str) -> tuple[bool, str]`
  - GET `https://www.googleapis.com/youtube/v3/playlists` with params
    `{"part": "id", "id": "PLh9GXHYeT6wUvWyDs6hjZQxJEnVEPpOOE",
    "maxResults": 1, "key": api_key}` (a known public playlist id; `playlists`
    is cheap and returns 200 even if the id matches nothing, but errors on a
    bad key).
  - `resp.status_code == 200` → `(True, "YouTube key works")`.
  - 400/403 → `(False, "Key rejected (invalid, not enabled, or quota)")`.
  - other status → `(False, f"YouTube API returned HTTP {status}")`.
  - network/exception → `(False, f"Could not reach YouTube API: {type(e).__name__}")`.
  - Empty `api_key` → `(False, "No key saved")` (no call).
- Both functions never raise (catch internally) and never include the key in
  their returned message.

### 2. Endpoints (`app/routes/settings.py`)
- `POST /settings/test-pexels` → reads `settings_repo.get(db, "pexels_api_key")`,
  calls `test_pexels_key`, returns an `HTMLResponse` fragment.
- `POST /settings/test-youtube` → reads
  `settings_repo.get(db, "youtube_api_key")`, calls `test_youtube_key`,
  returns an `HTMLResponse` fragment.
- Fragment shape (mirror the Whisper/LLM result markup):
  - success → `<p class="status status-done">✓ {message}</p>`
  - failure → `<p class="status status-failed">✗ {message}</p>`
  - The message comes only from the probe's returned string (no key, no raw
    URL).

### 3. Template (`app/templates/settings.html`)
- Beside the Pexels key field (~line 525): a `settings-test-row` with a
  `Test Pexels` button (`hx-post="/settings/test-pexels"`,
  `hx-target="#pexels-test-result"`, `hx-swap="innerHTML"`,
  `hx-disabled-elt="this"`), a `#pexels-test-result` div, and a
  `settings-test-hint`.
- Beside the YouTube key field (the `youtube_api_key` field added earlier): the
  same row with `Test YouTube`, `hx-post="/settings/test-youtube"`,
  `#youtube-test-result`. Exactly mirrors the existing Whisper test-row block.

## Data Flow

```
[Test Pexels] click → hx-post /settings/test-pexels
  → settings_repo.get(db, "pexels_api_key")
  → stock_images.test_pexels_key(key) → (ok, message)
  → HTMLResponse fragment → swapped into #pexels-test-result

[Test YouTube] click → hx-post /settings/test-youtube
  → settings_repo.get(db, "youtube_api_key")
  → playlist_index.test_youtube_key(key) → (ok, message)
  → HTMLResponse fragment → swapped into #youtube-test-result
```

## Error Handling

- No key saved → `(False, "No key saved")` → red fragment "✗ No key saved",
  no network call.
- Invalid key / quota → 401/403 → red fragment with a generic reason (never
  the key).
- Network/timeout → caught, red fragment naming the failure class only.
- The probe functions never raise and never embed the API key or the full
  request URL in the message (the YouTube URL carries `key=` — so the message
  must be status-only, like the indexer's redaction fix).

## Testing

Follow existing conventions (service test with mocked httpx, route render
test).
- **`test_pexels_key`** (mock httpx): 200 → (True, ...); 403 → (False, ...);
  network error → (False, ...); empty key → (False, "No key saved") with NO
  http call made.
- **`test_youtube_key`** (mock httpx): 200 → (True, ...); 403 → (False, ...);
  network error → (False, ...); empty key → (False, ...) no call; AND the
  returned message never contains the api_key string even when the mocked
  error carries a URL with `key=`.
- **Endpoints/render:** the settings page renders both Test buttons;
  `POST /settings/test-pexels` with a stored key returns a success fragment
  (mock the probe), with no key returns the "No key saved" fragment; same for
  `/settings/test-youtube`.

## Open Risks / Notes

- YouTube `playlists.list` with `part=id` is 1 quota unit — negligible.
- Pexels free tier rate-limits; a single `per_page=1` search is the cheapest
  documented call and is what the thumbnail path already uses.
- The probe deliberately returns a coarse reason (not the raw API error body)
  to avoid leaking the key embedded in error URLs — consistent with the
  YouTube-indexer key-redaction fix.
