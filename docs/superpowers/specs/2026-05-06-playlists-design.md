# Playlists — Design Spec

**Date:** 2026-05-06
**Status:** Approved for planning
**Owner:** Stefan
**Builds on:** [yt-summary core](2026-05-05-yt-summary-design.md)

## Purpose

Bookmark a YouTube playlist and let yt-summary keep itself in sync with it.
Each playlist becomes a saved subscription whose videos appear in the main
library and on a dedicated playlist page. New videos posted to the playlist
get summarized automatically on a scheduled refresh.

The use case is a personal "things I want summarized as they appear"
mailbox — a podcast feed for the brain.

## Scope

In scope:

- Save and remove playlists by URL
- Per-playlist detail page listing all linked videos
- Background scheduler that refreshes every saved playlist on a global
  interval (Settings-configurable, default 6 h)
- Initial import limited to the most recent N videos (Settings-configurable,
  default 20) so the Pi isn't drowned by a 200-video back-catalogue
- "Load older" button on a playlist page to pull the next N older videos
  on demand
- Schema groundwork for multi-user (`user_id` columns now, no auth yet)

Out of scope (explicitly):

- Per-playlist refresh intervals
- Pause / cancel queue backlog
- Channel subscriptions (not playlists, but "all videos by a channel")
- Automatic cleanup of removed videos (we keep them in the library)
- Notifications when a new summary is ready
- User login / auth (the schema lays the foundation, no UI)

## Multi-User Schema Groundwork

Even though there is exactly one user today, several tables get a
`user_id INTEGER NOT NULL DEFAULT 1` column now so the later auth feature
doesn't need a data migration.

Tables that gain `user_id`:

- `videos` — owner / importer
- `chat_messages` — author of the conversation turn
- `settings` — per-user configuration (PRIMARY KEY becomes `(user_id, key)`)
- `playlists` — subscriber

Tables that intentionally do NOT gain `user_id`:

- `jobs` — pure infrastructure, video-bound, not user-bound
- `playlist_videos` — linking table; user is derivable via `playlists.user_id`

A future `users` table is not created yet. For now `user_id = 1` is the
hardcoded default everywhere.

If a second user ever imports a video that is already in the database for
user 1, the V1 plan is "re-import": the video gets a new row scoped to
that user. (The DB primary key on `videos` will need to become composite at
that point — that is a deliberate future migration, not part of this spec.)

## Data Model

New table:

```sql
CREATE TABLE playlists (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    thumbnail_path TEXT,
    last_refreshed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE playlist_videos (
    playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES videos(id),
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (playlist_id, video_id)
);

CREATE INDEX idx_playlist_videos_video ON playlist_videos(video_id);
```

Schema migrations on existing tables (executed inside `init_schema` so a
fresh boot brings the DB to the current shape, and an existing DB is
upgraded transparently):

- `ALTER TABLE videos ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1`
- `ALTER TABLE chat_messages ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1`
- `settings`: SQLite cannot change a primary key in place. Use the
  rename-and-copy idiom:
  ```sql
  CREATE TABLE settings_new (
      user_id INTEGER NOT NULL DEFAULT 1,
      key TEXT NOT NULL,
      value TEXT NOT NULL,
      PRIMARY KEY (user_id, key)
  );
  INSERT INTO settings_new (user_id, key, value)
      SELECT 1, key, value FROM settings;
  DROP TABLE settings;
  ALTER TABLE settings_new RENAME TO settings;
  ```
  Run only when the old `settings` table still has the single-column PK
  (detect via `PRAGMA table_info(settings)`).

`init_schema` becomes idempotent across two states (fresh, V1-already)
and runs the migrations conditionally.

## Components

### `app/services/playlist.py`

Wrapper around yt-dlp's playlist extraction.

```python
@dataclass(frozen=True)
class PlaylistEntry:
    id: str
    title: str
    description: str
    thumbnail_url: str | None
    duration_seconds: int | None

@dataclass(frozen=True)
class PlaylistMetadata:
    id: str
    url: str
    title: str
    description: str
    thumbnail_url: str | None
    entries: list[PlaylistEntry]   # ordered as YouTube returns them

async def fetch_playlist(url: str, cookies_path: Path | None) -> PlaylistMetadata
```

Internally calls yt-dlp with:

```python
{"extract_flat": "in_playlist", "skip_download": True, "quiet": True}
```

`extract_flat` is fast and avoids per-video metadata calls. We get IDs,
titles, and thumbnails in one shot, which is exactly what `sync_playlist`
needs.

### `app/repos/playlists.py`

Standard CRUD plus link operations:

- `create(db, *, playlist_id, user_id, url, title, description, thumbnail_path) -> None`
- `get(db, playlist_id) -> Playlist | None`
- `list_for_user(db, user_id) -> list[Playlist]`
- `delete(db, playlist_id) -> None`
- `set_last_refreshed(db, playlist_id) -> None`
- `link_video(db, playlist_id, video_id) -> bool` (returns True if newly inserted; False if already linked, idempotent)
- `videos_for_playlist(db, playlist_id) -> list[Video]` (joins `videos`)
- `linked_video_ids(db, playlist_id) -> set[str]` (cheap diff helper)

### `app/services/playlist_sync.py`

The core orchestrator:

```python
@dataclass
class SyncResult:
    total_in_playlist: int
    newly_linked: int
    newly_enqueued: int

async def sync_playlist(
    db: aiosqlite.Connection,
    config: Config,
    playlist_id: str,
    *,
    initial_limit: int | None = None,
) -> SyncResult
```

Algorithm:

1. Look up `playlists.url`, `playlists.title` in DB.
2. `fetch_playlist(url)` → entries (ordered).
3. Slice `entries[:initial_limit]` if `initial_limit` is set.
4. Build set of `playlist_videos` already linked for this playlist.
5. For each entry not yet linked:
   - If `videos.get(entry.id)` is None: download thumbnail,
     `videos.upsert_metadata` with `user_id = playlist.user_id`.
   - `playlist_videos.link(playlist_id, entry.id)` (idempotent).
   - If `videos.summary IS NULL`: `jobs.enqueue(video_id)`. We use the
     summary's existence as the single "is this video done?" check.
     A previously-failed video has no summary, so a fresh enqueue gives
     it another chance — this is the same retry semantics as the manual
     reindex button.
6. `playlists.set_last_refreshed(playlist_id)`.

Note: `initial_limit=None` means "all entries". The scheduler always uses
`None` (refresh sees the entire playlist; in practice the diff is small).
The initial-import POST handler passes the Settings-configured limit.
"Load older" passes the same limit but slices from `[N:]` instead of
`[:N]` (see below).

### `app/services/playlist_load_older.py`

Or, more pragmatically, a helper inside `playlist_sync.py`:

```python
async def load_older_videos(
    db, config, playlist_id, *, count: int
) -> SyncResult
```

Algorithm:

1. Fetch the full playlist.
2. Drop entries that are already linked to this playlist.
3. From the remaining list, take the first `count` entries.
4. Same upsert / link / enqueue logic as `sync_playlist`.

Why this is good enough: yt-dlp returns playlist entries in the same
order YouTube serves them. For channel "Uploads" playlists that's
newest-first. For curated playlists the owner's chosen order. Either
way, "the first N entries we haven't seen yet" is exactly what the
user means by "load some more from this playlist" — we don't need to
reason about which is older or newer.

### `app/scheduler.py`

A `PlaylistScheduler` class symmetric to `Worker`:

```python
class PlaylistScheduler:
    def __init__(self, db, config, *, sync_fn=sync_playlist):
        ...

    def stop(self) -> None: ...

    async def run(self) -> None:
        while not self._stopped.is_set():
            interval_h = await self._read_interval()
            await self._sleep_or_stop(interval_h * 3600)
            for playlist in await playlists_repo.list_for_user(self._db, 1):
                try:
                    await self._sync_fn(self._db, self._config, playlist.id)
                except Exception:
                    log.exception("playlist sync failed for %s", playlist.id)
```

- Read `playlist_refresh_interval_hours` from settings each tick (so a
  changed setting takes effect at the next tick, no restart needed).
- On startup, do NOT immediately sync — wait one interval. This avoids a
  refresh storm on container restart.
- Stop via `asyncio.Event`, same pattern as `Worker`.

### Routes (`app/routes/playlists.py`, new)

- `POST /playlists` — Form with `url`. Calls `fetch_playlist` synchronously
  (under 5 s for any reasonable playlist), upserts the playlist row, then
  delegates to `sync_playlist(initial_limit=settings.initial_import_limit)`.
  On success: 303 redirect to `/p/{id}`. On yt-dlp failure: 400 with the
  error message.
- `GET /p/{playlist_id}` — Detail page.
- `POST /p/{playlist_id}/refresh` — Manual trigger. Calls `sync_playlist`
  with `initial_limit=None` (full sync). Redirects back to `/p/{id}`.
- `POST /p/{playlist_id}/load-older` — Calls `load_older_videos` with the
  Settings-configured count. Redirects back to `/p/{id}`.
- `POST /p/{playlist_id}/remove` — Deletes the playlist row (CASCADE
  cleans `playlist_videos`). Videos themselves remain. Redirects to `/`.

### Home (`app/routes/home.py`, modified)

`home()` additionally fetches `playlists_repo.list_for_user(db, 1)` and
passes it to the template. The template renders a "Playlists" strip
above the search bar when the list is non-empty, plus a separate
"Add playlist" tile (links to `/playlists/new`).

`/playlists/new` is a tiny GET route that just renders a page with a
single URL form posting to `POST /playlists`. We don't reuse the home
hero form because the action target differs.

## UI

### Home

```
┌─ Hero ────────────────────────────────────┐
│  Submit individual URL                     │
└────────────────────────────────────────────┘

Playlists                              [+ Add]
┌──────┐  ┌──────┐  ┌──────┐
│ pl1  │  │ pl2  │  │ pl3  │   (cards)
└──────┘  └──────┘  └──────┘

[ Search ]

Library
[ video grid ]
```

Playlist cards show: thumbnail, title, video count, "last refreshed N h
ago". Clicking goes to `/p/<id>`.

### Playlist Detail (`/p/<id>`)

```
┌─ Header ──────────────────────────────────┐
│  [Thumb]  Playlist title                  │
│           Last refreshed: 3 h ago         │
│           42 videos · imported the latest 20 │
│           [Refresh] [Load older] [Remove] │
└────────────────────────────────────────────┘

[ video grid filtered to this playlist ]
```

The "Load older" button only renders when the playlist has more entries
on YouTube than we've linked locally. We can know this cheaply by
storing `total_seen_at_last_refresh` in the playlist row, or — pragmatic
shortcut — always show the button and let the click no-op if there's
nothing new (the `load_older` helper returns 0).

### Add Playlist (`/playlists/new`)

```
[Header]
"Add a playlist"
[input: paste playlist URL]
[Save]
```

After save: redirect to `/p/<id>`. There the user sees the freshly
imported videos already as cards (some with summary-ready, others with
queued/running spinners).

## Settings

Two new settings keys:

- `playlist_refresh_interval_hours` — default `6`
- `playlist_initial_import_limit` — default `20` (0 means "all")

Both go into the existing settings page form. Numeric inputs.

## Error Handling

- `fetch_playlist` raises on a private/deleted playlist URL. The route
  catches it and shows a 400 with the message.
- A single video inside a playlist fails the per-video pipeline. Same
  handling as today: job marked `failed`, badge in the library.
- The scheduler's per-playlist sync may raise. Catch, log, continue with
  the next playlist. A persistent failure shows up in the next manual
  refresh too.
- Network blip during `set_last_refreshed`: the playlist isn't marked as
  refreshed, so the next tick will retry. No data loss.
- Playlist URL pasted twice: `playlists.create` is idempotent on the
  primary key — second create is a no-op (UPSERT semantics). The same
  playlist isn't duplicated.

## Tests

New files:

- `tests/test_repos_playlists.py` — CRUD, link idempotency, list_for_user
- `tests/test_services_playlist.py` — `fetch_playlist` with recorded
  yt-dlp `extract_flat` output as fixture
- `tests/test_services_playlist_sync.py` — sync mocks `fetch_playlist`,
  asserts: existing videos are linked but not re-enqueued; new videos
  get a job; `initial_limit` truncates the input; `load_older` skips
  already-linked videos
- `tests/test_scheduler.py` — scheduler respects stop, calls sync for
  each playlist, swallows individual sync errors
- `tests/test_routes_playlists.py` — POST /playlists redirects to
  detail; refresh + remove buttons work; load-older POST returns to
  the detail page

Modifications to existing tests:

- `tests/test_db.py` — assert new tables and that the migration is
  idempotent across runs
- `tests/test_repos_settings.py` — verify the migrated PK form continues
  to work (add tests with `user_id=1` explicitly)

Total expected test additions: ~25 tests on top of the current 101.

## Future (explicitly deferred)

- `users` table + auth flow
- Composite primary key on `videos` for cross-user isolation
- Per-playlist refresh intervals (a `refresh_interval_hours` column on
  `playlists`)
- Bulk pause / resume of the entire job queue
- Channel subscriptions (different yt-dlp surface)
- Webhook / push notification when a summary is ready
- "What's new" Inbox view that highlights videos summarized since last
  visit
