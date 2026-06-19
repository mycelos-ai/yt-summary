# Playlist Detail Ordering by YouTube Position — Design

**Date:** 2026-06-19
**Status:** Approved (brainstorming complete)

## Problem

The playlist detail page sorts videos by `playlist_videos.added_at DESC,
video_id DESC` ([app/repos/playlists.py:156](app/repos/playlists.py:156)).
Videos linked in the same sync share an `added_at`, so the tie-break
(`video_id DESC`) scatters them by video-id alphabet. Result: the newest
YouTube videos render at the BOTTOM of the page, and the user perceives them
as "missing". (Diagnosed 2026-06-19 alongside a separate yt-dlp capping issue,
which is out of scope here.)

## Solution

Persist each video's position in the YouTube playlist (the order yt-dlp
returns, which for the user's playlist is newest-first) and sort by it. A new
nullable `position` column on `playlist_videos` is written for every entry on
every sync (new and existing links), so the page mirrors the YouTube playlist
order. Pre-existing links start with `position = NULL` and self-heal on the
next refresh.

## Goals / Non-Goals

**Goals**
- Playlist detail lists videos in YouTube playlist order (yt-dlp index).
- Existing links get their position backfilled on the next sync (no manual
  step) because the refresh path already processes ALL entries.
- A refresh must NOT re-enqueue summary jobs for already-linked videos when it
  only updates their position.

**Non-Goals**
- NOT sorting by YouTube publish date (`upload_date` is not captured).
- NOT fixing the yt-dlp 101-of-105 capping (separate work item; this change
  orders whatever entries yt-dlp returns).
- No change to home/library ordering — only the playlist detail page.

## Components

### 1. DB column `playlist_videos.position` (INTEGER, nullable)
- Added via the existing additive `_ensure_column` migration pattern in
  `app/db.py`. NULL on all pre-existing links.

### 2. `PlaylistEntry.position` (`app/services/playlist.py`)
- Add `position: int` to the `PlaylistEntry` dataclass.
- When parsing yt-dlp's flat entries, capture the 1-based order
  (`playlist_index` when present, else the enumeration index). This is the
  order yt-dlp returns; we store it verbatim.

### 3. `link_video` writes position (`app/repos/playlists.py`)
- New signature: `link_video(db, playlist_id, video_id, position: int | None)
  -> bool`.
- Use upsert so existing links get their position refreshed:
  ```sql
  INSERT INTO playlist_videos (playlist_id, video_id, position)
  VALUES (?, ?, ?)
  ON CONFLICT(playlist_id, video_id) DO UPDATE SET position = excluded.position
  ```
- **Return-value caveat:** with `ON CONFLICT DO UPDATE`, `cursor.rowcount` is
  positive on both insert and update, so it can no longer signal "was new".
  Determine newness explicitly BEFORE the upsert (e.g. a `SELECT 1 FROM
  playlist_videos WHERE playlist_id=? AND video_id=?`), and return that
  boolean. The contract ("True iff newly linked") is unchanged for callers.

### 4. `_process_entries` passes position (`app/services/playlist_sync.py`)
- In the loop, pass `entry.position` to `link_video`. `newly_linked` /
  `newly_enqueued` continue to increment ONLY when `link_video` returns True
  (a genuinely new link), so position-only updates never re-enqueue jobs.

### 5. Sort query (`app/repos/playlists.py`, `videos_for_playlist`)
- Change the ORDER BY to put positioned videos first, in ascending position,
  NULLs last with the legacy fallback:
  ```sql
  ORDER BY pv.position IS NULL, pv.position ASC,
           pv.added_at DESC, pv.video_id DESC
  ```
  (`pv.position IS NULL` evaluates to 0 for positioned rows, 1 for NULLs, so
  positioned rows sort first.)

## Data Flow

```
sync_playlist → fetch_playlist → PlaylistEntry(position=yt-dlp index)
  → _process_entries: for each entry → link_video(..., position)
       (upsert: insert new OR refresh existing position;
        returns True only for genuinely new links)
  → videos_for_playlist: ORDER BY position (NULLs last)
  → page renders in YouTube order
```

## Error Handling

- `position` is a sort hint only. NULL (pre-existing links, or an entry with
  no index) falls back to the `added_at` ordering — the page always renders.
- The newness check + upsert run in one logical step; the existing
  per-call `commit()` is retained.
- A position-only update must not increment `newly_enqueued` (the critical
  correctness point — guarded by `link_video`'s boolean return).

## Testing

Follow existing conventions (migration test, repo test, sync test).
- **Migration:** `playlist_videos` gains `position` on an old DB; idempotent;
  existing rows are NULL.
- **`link_video`:** (a) new link → returns True, position stored;
  (b) re-linking an existing video with a different position → returns False,
  position is UPDATED (no duplicate row).
- **`videos_for_playlist`:** rows with a position sort (ascending) before
  rows with NULL position; the NULL group falls back to added_at ordering.
- **`_process_entries`:** after a sync, processed links carry the yt-dlp
  position; `newly_linked` counts only true new links (re-running the same
  entries does not re-enqueue).
- **Fetch/parse:** `PlaylistEntry.position` reflects yt-dlp order.

## Open Risks / Notes

- yt-dlp order is "as returned"; if YouTube reorders the playlist, a refresh
  re-numbers everything (upsert overwrites all positions) — desired.
- `load_older_videos` also calls `_process_entries`; it will write positions
  for the older entries it processes too, which is correct and harmless.
