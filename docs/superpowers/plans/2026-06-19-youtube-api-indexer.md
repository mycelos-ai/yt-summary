# YouTube Data API Playlist Indexer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Index playlists fully and in order via the YouTube Data API when a per-user API key is set, falling back to the existing yt-dlp path otherwise or on any API error.

**Architecture:** A new `playlist_index` service lists a playlist via `playlistItems.list` (httpx, paginated by `nextPageToken`). `playlist_sync` reads `youtube_api_key`; when set it indexes via the API, else/on error it uses the existing `fetch_playlist` (yt-dlp). Both return the same `PlaylistMetadata`/`PlaylistEntry` shape, so `_process_entries` is unchanged.

**Tech Stack:** Python 3.11+, httpx (already a dep), FastAPI, aiosqlite, pytest (asyncio_mode=auto).

## Global Constraints

- Use `httpx` (already a dependency). Do NOT add `googleapiclient` or any new dependency.
- Reuse the existing `PlaylistMetadata` / `PlaylistEntry` dataclasses from `app/services/playlist.py` (do not redefine them). `PlaylistEntry` fields: `id, title, description, thumbnail_url, duration_seconds, position`.
- `position = snippet.position + 1` (Data API is 0-based; +1 → 1-based, consistent with the playlist-order feature).
- `duration_seconds = None` on the API path (playlistItems lacks duration; out of scope).
- The API is an upgrade: any API failure (invalid/expired key, quota, 404, network, parse) must fall back to yt-dlp — never make a sync worse than today. No key → yt-dlp, no error.
- A mid-pagination failure discards the whole API result and raises (caught → fallback). `_process_entries` must never see a partial list.
- Skip items whose `contentDetails.videoId` is missing (deleted/private placeholders).
- Pagination safety cap: stop after 40 pages (2000 items).
- API base: `https://www.googleapis.com/youtube/v3/playlistItems` (and `.../playlists` for the title). `part=snippet,contentDetails`, `maxResults=50`.
- httpx mock pattern in tests: `monkeypatch.setattr(<module>.httpx, "AsyncClient", FakeClient)` with a `FakeClient` exposing async `__aenter__`/`__aexit__` and `get(url, params=...)` returning a `FakeResp` with `raise_for_status()` + `json()` (mirror `tests/test_services_stock_images.py`).
- Tests use `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` decorator. Run `pytest` from repo root. NO real network calls in tests.

---

### Task 1: `playlist_index` service — URL parsing + typed error

**Files:**
- Create: `app/services/playlist_index.py`
- Test: `tests/test_services_playlist_index.py` (create)

**Interfaces:**
- Produces:
  - `class PlaylistApiError(Exception)` — raised on any API failure.
  - `def _playlist_id_from_url(url: str) -> str` — returns the `list=` param, raises `PlaylistApiError` if absent.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_services_playlist_index.py`:

```python
import pytest

from app.services.playlist_index import PlaylistApiError, _playlist_id_from_url


def test_playlist_id_from_url_extracts_list_param():
    url = "https://www.youtube.com/playlist?list=PLabc123"
    assert _playlist_id_from_url(url) == "PLabc123"


def test_playlist_id_from_url_with_extra_params():
    url = "https://www.youtube.com/playlist?list=PLxyz&si=foo"
    assert _playlist_id_from_url(url) == "PLxyz"


def test_playlist_id_from_url_raises_without_list():
    with pytest.raises(PlaylistApiError):
        _playlist_id_from_url("https://www.youtube.com/watch?v=abc")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_services_playlist_index.py -q`
Expected: FAIL — `ModuleNotFoundError: app.services.playlist_index`.

- [ ] **Step 3: Create the service skeleton**

Create `app/services/playlist_index.py`:

```python
"""Index a YouTube playlist via the official Data API (playlistItems.list).

Used as the primary playlist indexer when a youtube_api_key is configured;
the caller (playlist_sync) falls back to yt-dlp's fetch_playlist when no key
is set or this raises PlaylistApiError. Returns the same PlaylistMetadata /
PlaylistEntry shape as fetch_playlist so the sync pipeline is unchanged.
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

import httpx

from app.services.playlist import PlaylistEntry, PlaylistMetadata

log = logging.getLogger(__name__)

_API_BASE = "https://www.googleapis.com/youtube/v3"
_MAX_PAGES = 40  # 40 * 50 = 2000 items — safety cap against infinite loops


class PlaylistApiError(Exception):
    """Any failure fetching/parsing the Data API response."""


def _playlist_id_from_url(url: str) -> str:
    qs = parse_qs(urlparse(url).query)
    values = qs.get("list")
    if not values or not values[0]:
        raise PlaylistApiError(f"No playlist id (list=) in URL: {url}")
    return values[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_services_playlist_index.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/playlist_index.py tests/test_services_playlist_index.py
git commit -m "feat(yt-api-indexer): playlist_index service skeleton + URL parsing"
```

---

### Task 2: `fetch_via_api` — pagination + item mapping

**Files:**
- Modify: `app/services/playlist_index.py`
- Test: `tests/test_services_playlist_index.py` (extend)

**Interfaces:**
- Consumes: `_playlist_id_from_url`, `PlaylistApiError` (Task 1); `PlaylistEntry`, `PlaylistMetadata` (existing).
- Produces: `async def fetch_via_api(url: str, *, api_key: str) -> PlaylistMetadata`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_services_playlist_index.py`:

```python
from app.services.playlist_index import fetch_via_api
from app.services import playlist_index


def _page(items, next_token=None):
    """Build a fake playlistItems.list response page."""
    page = {"items": items}
    if next_token:
        page["nextPageToken"] = next_token
    return page


def _item(vid, title, position, *, thumb="https://t/d.jpg", with_video_id=True):
    snippet = {
        "title": title,
        "description": "",
        "position": position,
        "thumbnails": {"high": {"url": thumb, "width": 480}},
    }
    content = {"videoId": vid} if with_video_id else {}
    return {"snippet": snippet, "contentDetails": content}


def _install_fake_http(monkeypatch, pages, *, playlist_title="My PL"):
    """Make playlist_index.httpx.AsyncClient return queued pages, then the
    playlists.list title response."""
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            if url.endswith("/playlists"):
                return FakeResp({"items": [{"snippet": {
                    "title": playlist_title, "description": "",
                    "thumbnails": {},
                }}]})
            # playlistItems pages, in order
            payload = pages[calls["n"]]
            calls["n"] += 1
            return FakeResp(payload)

    monkeypatch.setattr(playlist_index.httpx, "AsyncClient", FakeClient)


async def test_fetch_via_api_paginates_and_orders(monkeypatch):
    pages = [
        _page([_item("v1", "One", 0), _item("v2", "Two", 1)], next_token="T2"),
        _page([_item("v3", "Three", 2)]),
    ]
    _install_fake_http(monkeypatch, pages)
    meta = await fetch_via_api(
        "https://youtube.com/playlist?list=PLx", api_key="KEY",
    )
    assert [e.id for e in meta.entries] == ["v1", "v2", "v3"]
    assert [e.position for e in meta.entries] == [1, 2, 3]   # 0-based +1
    assert meta.entries[0].title == "One"
    assert meta.entries[0].thumbnail_url == "https://t/d.jpg"
    assert meta.entries[0].duration_seconds is None
    assert meta.title == "My PL"


async def test_fetch_via_api_skips_items_without_video_id(monkeypatch):
    pages = [_page([
        _item("v1", "One", 0),
        _item("x", "Deleted", 1, with_video_id=False),
        _item("v2", "Two", 2),
    ])]
    _install_fake_http(monkeypatch, pages)
    meta = await fetch_via_api(
        "https://youtube.com/playlist?list=PLx", api_key="KEY",
    )
    assert [e.id for e in meta.entries] == ["v1", "v2"]


async def test_fetch_via_api_raises_on_http_error(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            raise httpx.HTTPStatusError("403", request=None, response=None)

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None): return FakeResp()

    import httpx
    monkeypatch.setattr(playlist_index.httpx, "AsyncClient", FakeClient)
    with pytest.raises(PlaylistApiError):
        await fetch_via_api(
            "https://youtube.com/playlist?list=PLx", api_key="KEY",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_services_playlist_index.py -k fetch_via_api -q`
Expected: FAIL — `fetch_via_api` not defined.

- [ ] **Step 3: Implement `fetch_via_api`**

Append to `app/services/playlist_index.py`:

```python
def _pick_thumbnail(thumbs: dict) -> str | None:
    """Highest-resolution thumbnail url from a snippet.thumbnails dict."""
    if not isinstance(thumbs, dict) or not thumbs:
        return None
    best = None
    best_w = -1
    for t in thumbs.values():
        if isinstance(t, dict) and t.get("url"):
            w = t.get("width") or 0
            if w >= best_w:
                best_w = w
                best = t["url"]
    return best


def _entry_from_item(item: dict) -> PlaylistEntry | None:
    content = item.get("contentDetails") or {}
    vid = content.get("videoId")
    if not vid:
        return None  # deleted/private placeholder
    snippet = item.get("snippet") or {}
    pos = snippet.get("position")
    position = (pos + 1) if isinstance(pos, int) else 0
    return PlaylistEntry(
        id=vid,
        title=snippet.get("title") or "",
        description=snippet.get("description") or "",
        thumbnail_url=_pick_thumbnail(snippet.get("thumbnails") or {}),
        duration_seconds=None,
        position=position,
    )


async def _fetch_title(client: httpx.AsyncClient, playlist_id: str, api_key: str):
    """Best-effort playlist title/description/thumbnail; empty on failure."""
    try:
        resp = await client.get(
            f"{_API_BASE}/playlists",
            params={"part": "snippet", "id": playlist_id, "key": api_key},
        )
        resp.raise_for_status()
        items = resp.json().get("items") or []
        if items:
            sn = items[0].get("snippet") or {}
            return (
                sn.get("title") or "",
                sn.get("description") or "",
                _pick_thumbnail(sn.get("thumbnails") or {}),
            )
    except Exception:  # noqa: BLE001 — title is cosmetic
        pass
    return ("", "", None)


async def fetch_via_api(url: str, *, api_key: str) -> PlaylistMetadata:
    """List a playlist fully via the Data API. Raises PlaylistApiError on any
    HTTP / network / parse failure (caller falls back to yt-dlp)."""
    playlist_id = _playlist_id_from_url(url)
    entries: list[PlaylistEntry] = []
    page_token: str | None = None
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            for _ in range(_MAX_PAGES):
                params = {
                    "part": "snippet,contentDetails",
                    "playlistId": playlist_id,
                    "maxResults": 50,
                    "key": api_key,
                }
                if page_token:
                    params["pageToken"] = page_token
                resp = await client.get(
                    f"{_API_BASE}/playlistItems", params=params,
                )
                resp.raise_for_status()
                data = resp.json()
                for item in data.get("items") or []:
                    entry = _entry_from_item(item)
                    if entry is not None:
                        entries.append(entry)
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
            title, description, thumb = await _fetch_title(
                client, playlist_id, api_key,
            )
    except PlaylistApiError:
        raise
    except Exception as e:  # noqa: BLE001 — uniform fallback signal
        raise PlaylistApiError(f"YouTube API index failed: {e}") from e

    return PlaylistMetadata(
        id=playlist_id,
        url=url,
        title=title,
        description=description,
        thumbnail_url=thumb,
        entries=entries,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_services_playlist_index.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add app/services/playlist_index.py tests/test_services_playlist_index.py
git commit -m "feat(yt-api-indexer): paginated fetch_via_api + item mapping"
```

---

### Task 3: Settings — `youtube_api_key` field

**Files:**
- Modify: `app/templates/settings.html` (near the `pexels_api_key` field, ~line 523)
- Modify: `app/routes/settings.py` — `save_settings` Form param + save tuple
- Test: `tests/test_routes_settings.py` (extend)

**Interfaces:**
- Produces: a `youtube_api_key` settings value, saved/cleared via the existing settings POST, readable via `settings_repo.get_for_user(db, user_id, "youtube_api_key")`.

- [ ] **Step 1: Write the failing test**

Find an existing settings-save test in `tests/test_routes_settings.py` (grep `def test_` and how it POSTs `/settings`). Add a test that POSTs `youtube_api_key` and asserts it round-trips. Mirror the existing settings-save test's client/setup. Example shape (adapt to the file's fixtures):

```python
def test_save_youtube_api_key_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    from app.main import create_app
    from fastapi.testclient import TestClient
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/settings", data={"youtube_api_key": "YTKEY"})
        assert resp.status_code in (200, 303)
        page = client.get("/settings")
    assert "YTKEY" in page.text
```

If the existing settings-save tests pass many required Form fields, include whatever the handler requires (copy from a neighbouring save test) so the POST validates.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_routes_settings.py -k youtube_api_key -v`
Expected: FAIL — the value isn't saved/rendered (handler ignores the unknown form field).

- [ ] **Step 3: Add the Form param + save entry**

In `app/routes/settings.py`, `save_settings` signature, add a param (next to `pexels_api_key`):

```python
    pexels_api_key: str = Form(""),
    youtube_api_key: str = Form(""),
```

In the save tuple loop (where `("pexels_api_key", pexels_api_key.strip())` is), add:

```python
        ("pexels_api_key", pexels_api_key.strip()),
        ("youtube_api_key", youtube_api_key.strip()),
```

- [ ] **Step 4: Add the settings.html field**

In `app/templates/settings.html`, after the `pexels_api_key` `<label class="settings-field">…</label>` block (~line 528), add:

```html
      <label class="settings-field">
        <span class="settings-label">YouTube Data API Key <span class="settings-hint-inline">— optional, full playlist indexing</span></span>
        <input name="youtube_api_key" value="{{ settings.get('youtube_api_key', '') }}"
               placeholder="Your YouTube Data API v3 key">
        <small>Create a key at <code>console.cloud.google.com</code> and enable "YouTube Data API v3". When set, playlists are indexed via the official API (all videos, in order) instead of yt-dlp.</small>
      </label>
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_routes_settings.py -k youtube_api_key -v`
Expected: PASS.

- [ ] **Step 6: Run the full settings test file (no regression)**

Run: `pytest tests/test_routes_settings.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/routes/settings.py app/templates/settings.html tests/test_routes_settings.py
git commit -m "feat(yt-api-indexer): youtube_api_key settings field"
```

---

### Task 4: Sync branch — API-first with yt-dlp fallback

**Files:**
- Modify: `app/services/playlist_sync.py` — add `_index_playlist` helper; use it in `sync_playlist` (line ~84) and `load_older_videos` (line ~108)
- Test: `tests/test_services_playlist_sync.py` (extend)

**Interfaces:**
- Consumes: `playlist_index.fetch_via_api` + `PlaylistApiError` (Tasks 1-2); `fetch_playlist` (existing); `settings_repo.get_for_user`.
- Produces: `async def _index_playlist(db, config, playlist) -> PlaylistMetadata` — API when key set & succeeds, else yt-dlp.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_services_playlist_sync.py` (mirror its existing fixtures/imports):

```python
from unittest.mock import AsyncMock
from app.services import playlist_sync
from app.services.playlist import PlaylistMetadata
from app.services.playlist_index import PlaylistApiError
from app.repos import settings as settings_repo


def _meta(source):
    return PlaylistMetadata(
        id="PLx", url="u", title=source, description="",
        thumbnail_url=None, entries=[],
    )


async def _make_pl(db):
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="https://youtube.com/playlist?list=PLx",
        title="T", description="", thumbnail_path=None,
    )
    return await playlists_repo.get(db, "p1")


async def test_index_playlist_uses_api_when_key_set(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path); cfg.ensure_dirs()
    pl = await _make_pl(db)
    await settings_repo.set_for_user(db, 1, "youtube_api_key", "KEY")
    monkeypatch.setattr(
        playlist_sync.playlist_index, "fetch_via_api",
        AsyncMock(return_value=_meta("api")),
    )
    monkeypatch.setattr(
        playlist_sync, "fetch_playlist",
        AsyncMock(return_value=_meta("ytdlp")),
    )
    meta = await playlist_sync._index_playlist(db, cfg, pl)
    assert meta.title == "api"


async def test_index_playlist_falls_back_when_no_key(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path); cfg.ensure_dirs()
    pl = await _make_pl(db)
    # no youtube_api_key set
    monkeypatch.setattr(
        playlist_sync.playlist_index, "fetch_via_api",
        AsyncMock(return_value=_meta("api")),
    )
    monkeypatch.setattr(
        playlist_sync, "fetch_playlist",
        AsyncMock(return_value=_meta("ytdlp")),
    )
    meta = await playlist_sync._index_playlist(db, cfg, pl)
    assert meta.title == "ytdlp"


async def test_index_playlist_falls_back_on_api_error(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path); cfg.ensure_dirs()
    pl = await _make_pl(db)
    await settings_repo.set_for_user(db, 1, "youtube_api_key", "KEY")
    monkeypatch.setattr(
        playlist_sync.playlist_index, "fetch_via_api",
        AsyncMock(side_effect=PlaylistApiError("boom")),
    )
    monkeypatch.setattr(
        playlist_sync, "fetch_playlist",
        AsyncMock(return_value=_meta("ytdlp")),
    )
    meta = await playlist_sync._index_playlist(db, cfg, pl)
    assert meta.title == "ytdlp"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_services_playlist_sync.py -k _index_playlist -v`
Expected: FAIL — `_index_playlist` / `playlist_sync.playlist_index` not defined.

- [ ] **Step 3: Add the helper and wire it in**

In `app/services/playlist_sync.py`, add imports near the top:

```python
from app.repos import settings as settings_repo
from app.services import playlist_index
```

Add the helper (after `_resolve_cookies`):

```python
async def _index_playlist(db, config, playlist) -> "PlaylistMetadata":
    """Index a playlist's entries: Data API when a youtube_api_key is set and
    succeeds, else (or on API error) the yt-dlp fetch_playlist path."""
    api_key = await settings_repo.get_for_user(
        db, playlist.user_id, "youtube_api_key",
    )
    if api_key:
        try:
            return await playlist_index.fetch_via_api(
                playlist.url, api_key=api_key,
            )
        except playlist_index.PlaylistApiError as e:
            log.warning(
                "YouTube API index failed for %s, falling back to yt-dlp: %s",
                playlist.id, e,
            )
    cookies = await _resolve_cookies(config)
    return await fetch_playlist(playlist.url, cookies_path=cookies)
```

Add a module logger if absent (top of file): `import logging` and `log = logging.getLogger(__name__)`. Import `PlaylistMetadata` for the annotation: change the existing `from app.services.playlist import PlaylistEntry, fetch_playlist` to also import `PlaylistMetadata`.

In `sync_playlist`, replace lines ~83-84:

```python
    meta = await _index_playlist(db, config, playlist)
```

(removing the now-unused `cookies = await _resolve_cookies(config)` + `fetch_playlist(...)` lines there — `_index_playlist` resolves cookies internally on the fallback path).

In `load_older_videos`, replace the equivalent lines ~107-108:

```python
    meta = await _index_playlist(db, config, playlist)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_services_playlist_sync.py -k _index_playlist -v`
Expected: PASS.

- [ ] **Step 5: Run the full sync test file (no regression)**

Run: `pytest tests/test_services_playlist_sync.py -q`
Expected: PASS (existing sync/position tests still green — they don't set a key, so they take the yt-dlp path which they already mock).

Note: existing sync tests monkeypatch `fetch_playlist` (or `fetch_playlist` within playlist_sync). After this change the production call goes through `_index_playlist`, which with no key calls `fetch_playlist` — so those mocks still apply. If any existing test set a `youtube_api_key`, it would now take the API path; none do. Confirm in the run.

- [ ] **Step 6: Commit**

```bash
git add app/services/playlist_sync.py tests/test_services_playlist_sync.py
git commit -m "feat(yt-api-indexer): sync indexes via API when key set, else yt-dlp"
```

---

### Task 5: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the whole suite**

Run: `pytest -q`
Expected: all pass. (If `test_services_model_info.py` / `test_services_embeddings_local.py` fail ONLY due to HuggingFace-cache/SOCKS sandbox restrictions, re-run those outside the sandbox to confirm — those failures are environmental, not feature regressions.)

- [ ] **Step 2: Confirm end-to-end (optional, user-driven)**

After deploy: set a YouTube Data API key in Settings, refresh the playlist, and confirm the playlist now shows all entries (e.g. 105 instead of 101). Without a key, behaviour is unchanged.

---

## Self-Review

**Spec coverage:**
- `playlist_index.fetch_via_api` (pagination, mapping, title, skip-no-videoId, 40-page cap, typed error) → Tasks 1-2. ✓
- `position = snippet.position + 1` → Task 2 (`_entry_from_item`). ✓
- `duration_seconds = None` on API path → Task 2. ✓
- Settings `youtube_api_key` field + save + read → Task 3. ✓
- Sync branch: API when key set, fallback no-key AND on PlaylistApiError → Task 4 (`_index_playlist`). ✓
- Same PlaylistMetadata/Entry contract, `_process_entries` unchanged → Tasks 2+4 (reuse existing dataclasses; sync only swaps the fetch call). ✓
- httpx only, no googleapiclient → Tasks 1-2. ✓
- Mid-pagination failure discards all + raises → Task 2 (broad except → PlaylistApiError; entries only returned on full success). ✓
- Testing: index service (pagination/mapping/skip/error/url-parse), sync branch (3 paths), settings round-trip → Tasks 1-4. ✓

**Type/name consistency:** `PlaylistApiError`, `_playlist_id_from_url`, `fetch_via_api(url, *, api_key)`, `_index_playlist(db, config, playlist)` consistent across tasks. `PlaylistEntry`/`PlaylistMetadata` reused from `app/services/playlist.py` (not redefined). `position + 1` consistent with Task 2 mapping and the playlist-order feature.

**Placeholder scan:** No TBD/TODO. Task 3 Step 1 says "mirror the existing settings-save test / include required Form fields" — that's a concrete discovery instruction (the settings POST has many required-ish fields; the implementer copies a neighbouring test's field set), with a complete example test given. Not deferred work.

**Note:** Task 2's error test imports `httpx` inside the test body — that's intentional (it constructs an `httpx.HTTPStatusError`). The implementer should hoist `import httpx` to the test module top if cleaner; behavior is identical.
