# MCP Sync Export + Item Identity — Design

**Status:** Approved in discussion, ready for implementation planning
**Date:** 2026-08-13 (Week 33, 2026)
**Scope:** yt-summary only. The Mycelos side (Part B of the original
discussion note) is a separate plan in a separate repo.

## Goal

Two things, one change set:

1. **Sync surface.** yt-summary gains one generic MCP tool, `export_since`,
   that any MCP-speaking system can use for incremental sync. Mycelos is the
   first consumer, but nothing here is Mycelos-specific.
2. **Item identity.** Every item that leaves yt-summary — through any export
   path or any MCP tool — says what it is and where it came from.

The second point is what makes the first one safe to repeat. Without a stable
identity a consumer cannot tell a re-export from a new item.

## Non-Goals

- **No transcript in the sync payload.** Keeps semantic search sharp on the
  consumer side and keeps MCP payloads paginable. The transcript stays
  reachable through `get_transcript` and the item's `resource` URL.
- **No deletion or archive propagation.** See "Archived items" below. This is
  a known, accepted gap for v1, not an oversight.
- **No push direction.** yt-summary never calls out. Consumers pull.
- **No new auth model.** The existing MCP auth applies unchanged.
- **No ID renaming.** `videos.id` keeps its current value and, in the MCP
  tools, its current field name (`video_id`). Renaming would break existing
  hosts for no gain.

## Decisions taken (2026-08-13)

| Question | Decision | Rationale |
|---|---|---|
| Transport | MCP tool in yt-summary | Serves any MCP consumer, not just Mycelos; avoids a second interface. |
| Content | Summary + metadata, no transcript | Payload size and search quality. |
| Pagination | Cursor + clamped page size | Bounded responses; robust against concurrent edits. |
| Provenance | Separate `source` field, IDs unchanged | Additive. Does not fork the ID format across REST/MCP/file exports. |
| `source` value | Module constant, not env var | One instance today. A second one is a hypothetical; YAGNI. |
| Reach | All export paths **and** the existing MCP tools | A consumer that uses `search` today and syncs tomorrow must not see two identity models. |
| Archived items | Excluded from sync | Consistent with `list_recent`. Post-sync archiving does not propagate. |

## Part 1 — Item identity

Every outgoing item carries three identity fields:

| Field | Example | Purpose |
|---|---|---|
| `id` | `"1:dQw4w9WgXcQ"` | Unchanged. The existing composite primary key. |
| `source` | `"yt-summary"` | Where the item came from. |
| `updated_at` | `"2026-08-13T09:12:00Z"` | Change detection on re-import. |

The consumer's key is the pair `(source, id)`.

### Why not put the instance into the ID

`videos.id` is already an external, stable identifier: `"<user_id>:<youtube_id>"`
(`app/services/api.py:156`). It is not an autoincrement integer. It is already
the primary key, already in the JSON export (`app/services/export.py:150`),
already in the ZIP manifest, and already in every MCP tool result as
`video_id`.

Folding a prefix into it (`"yt-summary:1:dQw4w9WgXcQ"`) would create two ID
forms in one system: one in the sync payload, another in REST/MCP/JSON. A
separate `source` field adds the missing information without forking the
format.

`source` is a module-level constant in `app/services/export.py`. If a second
instance ever appears, that constant is the single place an `YTS_INSTANCE_ID`
env var would replace.

### Note on the `user_id` prefix

The ID prefix is the local profile number. It leaves the system as part of the
ID. For a single-user Pi install this is inert, but it is a deliberate choice
rather than an accident: it is a local ordinal, not an account identifier, and
it carries no name, address or contact data. Recorded here so a future reader
does not have to re-derive it.

## Part 2 — Where the fields appear

Every output path routes through the pure renderers in
`app/services/export.py`, so the change lands in one module and propagates to
single-item export, bulk ZIP, web UI and API alike.

| Path | Location | Today | Added |
|---|---|---|---|
| Markdown frontmatter | `render_item_md:91-108` | no identity at all | `id`, `source`, `updated` |
| JSON document | `render_item_json:149-164` | `id`, `created_at` | `source`, `updated_at` |
| ZIP manifest | `build_export_zip:232-237` | `id` | `source` |
| MCP `search` | `app/routes/mcp.py:75-80` | `video_id` | `source` |
| MCP `list_recent` | `app/routes/mcp.py:127-135` | `video_id` | `source` |
| MCP `submit_url` | `app/routes/mcp.py:43-48` | `video_id` | `source` |

The Markdown case is the largest gap: an exported `.md` currently carries no
identifier at all — the ID appears only in the filename. Adding `id` and
`updated` to the frontmatter makes the file export idempotently re-importable,
which it is not today.

**Every change is additive.** No field is renamed or removed. The existing
tests assert on the presence of individual keys, not on dict equality, so
extra keys break nothing.

## Part 3 — The sync surface

### 3.1 Repo: `list_updated_since`

New in `app/repos/videos.py`, next to `list_recent` (~line 336):

```python
async def list_updated_since(
    db, *, user_id: int, since: str | None, cursor: str | None, limit: int
) -> list[Video]:
    """Items changed at or after `since`, for incremental sync.

    Ordered by (updated_at ASC, id ASC) so a cursor can resume exactly.
    `cursor` is the last seen "<updated_at>|<id>" pair.
    """
```

Constraints:

- Scoped by `user_id`, like every other query in that module.
- `archived_at IS NULL`, matching `list_recent`.
- Row mapping reuses `_row_to_video` with its guarded column access
  (`app/repos/videos.py:9-87`).

**Why `updated_at` and not `created_at`:** summaries are updated in place —
resummarize, language detection, highlights, thumbnail — without a new row
(`app/repos/videos.py:119,155,164,242,251,627`). The existing bulk export
filters on `created_at` (`app/routes/export.py:210-214`) and would therefore
never re-emit a changed summary. This is a genuinely new query shape:
`list_recent` orders `created_at DESC, id DESC` for the UI feed.

### 3.2 Service: `render_item_okf`

New pure function in `app/services/export.py`, beside the existing renderers.
No I/O.

```python
def render_item_okf(video: Video, *, tags: list[str], playlists: list[str]) -> dict:
    """One sync item: OKF frontmatter fields + summary body, no transcript."""
```

Returns the OKF vocabulary (`type`, `title`, `description`, `resource`,
`timestamp`, `tags`) plus the identity fields from Part 1 and the yt-summary
metadata (`kind`, `language`, `summary_model`, `playlists`,
`duration_seconds`, `highlights`, `content`).

`type` is `"note"`. `resource` is the source URL. `timestamp` is `updated_at`,
which is also the sync cursor. `content` is the summary Markdown.

**No `transcript` key.** Tested explicitly for its absence, so a later
refactor cannot reintroduce it silently.

### 3.3 MCP tool: `export_since`

Registered in `build_mcp_server`, following the `list_recent` / `ask_library`
pattern:

```python
@mcp.tool()
async def export_since(since: str = "", cursor: str = "", limit: int = 50) -> dict:
    """Items created or updated since `since` (ISO 8601), for incremental sync.

    Returns {"items": [...], "next_cursor": str, "has_more": bool}.
    Summaries and metadata only — no transcripts. Call repeatedly with
    the returned next_cursor until has_more is false.
    """
```

- `limit` is clamped to 100 regardless of what the caller asks for — MCP
  payload safety.
- Empty `since` means "from the beginning" (initial full sync).
- `next_cursor` is opaque to the caller (`"<updated_at>|<id>"`).
- Thin wrapper: delegates to `list_updated_since` + `render_item_okf`, exactly
  as the other tools delegate to repo and service functions.

The module docstring notes that the MCP surface is deliberately smaller than
REST. This tool earns its place: it is the sync surface for *any* consumer,
which is the argument for putting it in MCP rather than adding a
consumer-specific REST route.

## Archived items

`list_updated_since` excludes archived items, consistent with `list_recent`.

This has a known consequence: an item archived **after** it synced stays as a
note on the consumer side. yt-summary never signals the removal. The export
was correct at the moment it happened; deletion propagation is a separate
concern and out of scope for v1.

Recorded here as an accepted gap, not an oversight.

## Error handling

- An item that cannot be rendered is skipped, not fatal to the batch.
- `limit` above the cap is clamped, never rejected — a consumer asking for too
  much gets a valid smaller page rather than an error.
- Cursor parse failure falls back to "from the beginning" rather than raising:
  a resync is cheap, a crashed sync loop is not.

## Testing

Following the patterns in `tests/test_services_export.py` and
`tests/test_routes_mcp.py`.

**`list_updated_since`**
- ordering by `(updated_at ASC, id ASC)`
- cursor resumes exactly, including two items sharing an `updated_at`
- `user_id` scoping: another profile's items never appear
- archived items excluded

**`render_item_okf`**
- field mapping against the OKF vocabulary
- **no** `transcript` key
- identity fields present and correct

**`export_since`**
- pagination across multiple pages
- `has_more` correct on the last page
- `limit` clamping

**Identity fields**
- present in `render_item_md` frontmatter, `render_item_json`, the ZIP
  manifest, and each of `search` / `list_recent` / `submit_url`

## Rollout

This spec covers yt-summary alone and is independently useful: once it ships,
any MCP client can sync the library. The Mycelos ingest side is a separate
plan, in a separate repo, on its own branch.
