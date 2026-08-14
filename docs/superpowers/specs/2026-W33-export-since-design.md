# `export_since` — MCP Sync Export for yt-summary

Week 33 (2026). Specification for a new MCP tool that lets any MCP-speaking system incrementally sync summaries out of yt-summary.

**Status: IMPLEMENTED and merged to `main` (2026-08-14).** Kept as the record of
the reasoning; do not implement from it again.

The work shipped from a parallel spec/plan pair written independently in the same
week — [`2026-08-13-mcp-sync-export-design.md`](2026-08-13-mcp-sync-export-design.md)
and [`../plans/2026-08-13-mcp-sync-export.md`](../plans/2026-08-13-mcp-sync-export.md).
Both documents describe the same feature; that pair is the one the code follows.
Commits `cc71c47`..`57dfd8d`.

**Three deviations, decided 2026-08-14.** Where this document and the shipped code
disagree, the code is authoritative:

| §  | This spec says | Shipped | Why |
|---|---|---|---|
| 4.2 | `description` = first paragraph of the summary | `video.description` (the source's own description) | Needs no paragraph heuristic. Changing it now would silently redefine a published field. |
| 4.2 | Empty fields "omitted when empty" | Always present (`tags: []`, `summary_model: null`) | A consumer can rely on a fixed key set; no field ever disappears. |
| 2 | Deployment note only | `YTS_MCP_DISABLE_HOST_CHECK` now set in `docker-compose.yml` | The note was correct and load-bearing — without it the MCP surface is unreachable behind a proxy. Acted on, see the comment in that file for the security condition. |

One thing this document got right that the implemented spec missed: §4.1 names the
`id` tiebreaker in the cursor ordering explicitly, and it is genuinely required —
two items sharing an `updated_at` would otherwise be skipped or repeated forever.
The shipped code has it, and a dedicated test.

---

## 1. Purpose

yt-summary produces knowledge daily. Today that knowledge can only leave the system as a manual ZIP download (`GET /export.zip`) or one item at a time. There is no way for another system to ask *"what changed since I last looked?"*.

`export_since` closes that gap with **one generic MCP tool** — not a bespoke endpoint for one consumer. Any MCP client (a knowledge base, an agent, another tool) can then keep a synchronized copy of the library.

The first consumer is Mycelos (Stefan's knowledge brain), but nothing in this design is Mycelos-specific. That is deliberate: exposing sync over MCP makes yt-summary more useful to every system that speaks it, rather than adding a second interface for a single client.

## 2. What exists today (verified 2026-08-14)

- **REST API** at `/api/v1/*`, Bearer-token authenticated. `GET /api/v1/health` → `{"ok":true,"version":"0.4.0"}`.
- **MCP server** at `/mcp/sse` (SSE transport: an event stream at `/mcp/sse`, messages posted to `/mcp/messages/?session_id=…`). Live-tested and reachable.
- **Nine MCP tools:** `submit_url`, `search`, `get_summary`, `get_transcript`, `ask_video`, `ask_library`, `list_recent`, `list_models`, `resummarize`. No export/sync tool.
- **Export machinery** in `app/services/export.py` (pure renderers) and `app/routes/export.py` (four routes: per-item and bulk, each web and API). Markdown output is Obsidian-compatible: YAML frontmatter + the summary verbatim, with `[MM:SS](#t=SEC)` links rewritten to absolute YouTube deep links.
- **Storage:** SQLite, `videos` table. Primary key `id` is `"<user_id>:<youtube_id-or-web_id>"`. Columns include `created_at`, `updated_at`, `archived_at`, `summary`, `highlights_json`, `summary_model`, language fields, `duration_seconds`. Tags and playlists are normalized side tables.

**Deployment note:** the MCP server's DNS-rebinding protection rejects requests with `Invalid Host header` when running behind a reverse proxy. Setting `YTS_MCP_DISABLE_HOST_CHECK=1` **in the compose service's `environment:` block** (not only in a `.env` file, which compose may not read) plus a container recreate resolves it. This was diagnosed during the live test and is unrelated to the feature — but a fresh deployment will hit it.

## 3. The two gaps this closes

### 3.1 Wrong timestamp for sync

The bulk export filters `since`/`until` against **`created_at`** (`app/routes/export.py:209-214`, applied in Python after fetching). That is the wrong column for synchronization: summaries are updated **in place** — resummarize, highlight extraction, related-links backfill — without creating a new row. A consumer filtering on `created_at` would never see those updates.

Sync must key on **`updated_at`**.

### 3.2 No stable id in the Markdown

The item id (`"1:dQw4w9WgXcQ"`) appears in the JSON export and in the export filename, but **not in the Markdown frontmatter**. A consumer re-importing an exported file cannot tell it is the same item as last time, so it creates duplicates.

## 4. Design

Three thin additions, each following the layering the codebase already uses (repo = raw parametrized SQL; service = pure functions, no I/O; MCP tool = thin wrapper).

### 4.1 Repo query — `list_updated_since`

`app/repos/videos.py`, alongside `list_recent`:

```python
async def list_updated_since(
    db, *, user_id: int = 1, since: str | None = None,
    cursor: str | None = None, limit: int = 50,
) -> list[Video]:
    """Items changed at or after `since`, oldest first — for incremental sync."""
```

- `WHERE user_id = ? AND archived_at IS NULL`, optionally `AND updated_at >= ?`
- Cursor resume: `AND (updated_at > ? OR (updated_at = ? AND id > ?))`
- **`ORDER BY updated_at ASC, id ASC`** — the `id` tiebreaker is required, because several items can share an `updated_at` and a cursor without it would either skip or repeat rows.

This is a genuinely new query shape, not a parameter tweak: `list_recent` orders `created_at DESC, id DESC` for the newest-first UI feed.

### 4.2 Pure renderer — `render_item_okf`

`app/services/export.py`, alongside `render_item_md` / `render_item_json`:

```python
def render_item_okf(video, *, tags, playlists, highlights=None) -> dict
```

Returns one sync item using **Open Knowledge Format (OKF v0.1)** vocabulary, so consumers need no translation table:

| Field | Source | Note |
|---|---|---|
| `id` | `video.id` | stable external id, the dedup key |
| `type` | `"note"` | OKF's only required field |
| `title` | `video.title` | |
| `description` | first paragraph of the summary | |
| `resource` | `video.url` | OKF's "resource" = source url |
| `timestamp` | `video.updated_at` | the sync cursor *and* the staleness signal |
| `created` | `video.created_at` | |
| `content` | summary markdown, timestamp links rewritten | |
| `tags`, `playlists`, `kind`, `language`, `summary_model`, `duration_seconds`, `highlights` | as available | omitted when empty |

**No `transcript` key, ever.** Transcripts are long; including them would make pages too large to travel over MCP and would dilute a consumer's semantic search. The transcript stays reachable via `resource` and the existing `get_transcript` tool.

Additionally: add `id` and `updated` to `render_item_md`'s frontmatter (two lines), so the *file* export becomes idempotently importable too.

### 4.3 MCP tool — `export_since`

`app/routes/mcp.py`, registered in `build_mcp_server` like the existing tools:

```python
@mcp.tool()
async def export_since(since: str = "", cursor: str = "", limit: int = 50) -> dict:
    """Items created or updated since `since` (ISO 8601), for incremental sync."""
```

Returns:

```json
{
  "items": [ { …OKF fields… } ],
  "next_cursor": "2026-08-13T09:12:00|1:dQw4w9WgXcQ",
  "has_more": true
}
```

- Empty `since` → full initial sync from the beginning.
- `next_cursor` is opaque to the caller (internally `"<updated_at>|<id>"`).
- `limit` is **clamped server-side to 100** regardless of what the caller asks — MCP payload safety.
- The caller pages until `has_more` is false, then stores the newest `timestamp` it saw as the `since` for its next run.

**Why MCP and not a REST route:** the module docstring notes the MCP surface is deliberately smaller than REST. This tool earns its place because it is the *generic* sync surface — any consumer benefits, which is precisely the argument against adding a client-specific REST endpoint. Note that MCP tools return structured JSON, not file archives; that constraint is why the payload is summaries-and-metadata only.

## 5. Constraints

- **Every query scoped by `user_id`.** Multi-profile isolation is absolute — no "all users" default anywhere.
- Cursor ordering `(updated_at ASC, id ASC)`, always.
- No transcripts in sync output.
- Existing behavior unchanged: no route, tool, or renderer changes semantics. The only edit to existing code is the two frontmatter lines in `render_item_md`.
- English code, comments and docstrings. Conventional commit messages, no AI-attribution footers.

## 6. Testing

- **Repo:** `since` filtering; cursor resume including two items sharing an `updated_at`; `limit` caps the page; another user's items never appear; archived items excluded.
- **Renderer:** id and `updated_at` present; OKF vocabulary correct; **no transcript key**; description is the first summary paragraph; timestamp links rewritten; markdown frontmatter carries `id` and `updated`.
- **Tool:** oldest-first ordering; pagination via `next_cursor` with correct `has_more`; limit clamping; `since` filtering; empty library returns a valid empty result rather than an error.

Framework: `pytest` with `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed).

## 7. Out of scope

- No push direction — yt-summary never calls the consumer. Consumers pull.
- No new auth model; the existing MCP auth applies.
- No REST equivalent of the sync endpoint (MCP is the chosen surface).
- No transcript sync.
- No changes to the existing four export routes beyond the frontmatter addition.

## 8. Repo state note

At the time of writing this repo is on branch `agent/harden-runtime-safety` with untracked files present (`.codex/`, `.localdata/`). Create a fresh branch off the intended base before starting, and do not commit those untracked directories.
