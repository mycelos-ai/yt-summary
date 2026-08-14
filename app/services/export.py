"""Pure builders for exporting a stored item as Markdown or JSON.

Everything here is a pure function over a Video (+ its tags/playlists):
no DB, no network, no I/O. The routes in routes/export.py do the
fetching and streaming; this module just turns data into text/dicts so
it stays trivially unit-testable.

Markdown is Obsidian-compatible: YAML frontmatter + the summary verbatim.
Inline `[MM:SS](#t=SECONDS)` timestamp links (which only make sense
inside the app) are rewritten to absolute YouTube deep links so they
stay clickable in an exported note.
"""

from __future__ import annotations

import io
import json
import re
import unicodedata
import zipfile
from datetime import datetime

from app.models import Video, VideoKind

# Matches the `[MM:SS](#t=SECONDS)` / `[HH:MM:SS](#t=SECONDS)` links the
# summarizer emits (see services/summarizer.py). We keep the visible
# label (group 1) and rewrite only the href target (group 2).
_TS_LINK_RE = re.compile(r"(\[\d{1,2}(?::\d{2}){1,2}\])\(#t=(\d+)\)")

# Provenance stamped onto every outgoing item. A consumer keys items by
# the pair (source, id): `id` is unique within this instance, `source`
# says which instance it came from. A module constant on purpose — if a
# second instance ever exists, this is the single line an
# YTS_INSTANCE_ID env var would replace.
SOURCE = "yt-summary"


def _utc_iso(value: datetime) -> str:
    """ISO-8601 with an explicit `Z`.

    Stored timestamps are UTC but parse back naive (tzinfo=None), so a
    consumer would otherwise be free to read them as local time.
    """
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_highlights(video: Video) -> list[dict] | None:
    """Parse `video.highlights_json` into a list, or None if absent/invalid.

    None on a missing/empty column, on malformed JSON, or when the parsed
    value isn't a list — callers treat None the same as "no highlights"."""
    if not video.highlights_json:
        return None
    try:
        data = json.loads(video.highlights_json)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, list) else None


def _slug(text: str) -> str:
    """ASCII-fold, lowercase, collapse to dash-separated tokens."""
    folded = (
        unicodedata.normalize("NFKD", text)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    folded = folded.lower()
    folded = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    return folded or "untitled"


def _short_id(video: Video) -> str:
    """The bare id without the `<user>:` profile prefix, for filenames."""
    return video.id.split(":", 1)[-1]


def export_filename(video: Video) -> str:
    """`YYYY-MM-DD-<slug-of-title>-<short-id>.md`.

    The short-id suffix guarantees uniqueness even for same-titled items
    on the same day."""
    date = video.created_at.strftime("%Y-%m-%d")
    return f"{date}-{_slug(video.title)}-{_short_id(video)}.md"


def rewrite_timestamp_links(md: str, *, youtube_id: str | None) -> str:
    """Rewrite in-app `(#t=N)` timestamp links to absolute YouTube deep
    links so they stay clickable outside the app. No-op when there's no
    youtube_id (web/email items have no such links)."""
    if not youtube_id:
        return md

    def _sub(m: re.Match) -> str:
        label, seconds = m.group(1), m.group(2)
        return f"{label}(https://youtube.com/watch?v={youtube_id}&t={seconds}s)"

    return _TS_LINK_RE.sub(_sub, md)


def _yaml_quote(value: str) -> str:
    """Double-quoted YAML scalar with embedded quotes/backslashes escaped."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_item_md(
    video: Video,
    *,
    tags: list[str],
    playlists: list[str],
    transcript: bool = False,
    highlights: list[dict] | None = None,
) -> str:
    """Render one item as Obsidian-compatible Markdown.

    Frontmatter carries the metadata; the body is the summary verbatim
    (with timestamp links rewritten). `transcript=True` appends a
    `## Transcript` section; `highlights` (a list of {text, rank, reason})
    appends a `## Highlights` list. Both default off — the summary is the
    knowledge artifact."""
    fm: list[str] = ["---"]
    fm.append(f"id: {_yaml_quote(video.id)}")
    fm.append(f"source: {_yaml_quote(SOURCE)}")
    fm.append(f"title: {_yaml_quote(video.title)}")
    fm.append(f"source_url: {_yaml_quote(video.url)}")
    fm.append(f"kind: {video.kind.value}")
    fm.append(f"created: {video.created_at.strftime('%Y-%m-%d')}")
    fm.append(f"updated: {_utc_iso(video.updated_at)}")
    if video.summary_model:
        fm.append(f"summary_model: {_yaml_quote(video.summary_model)}")
    lang = video.summary_language or video.source_language
    if lang:
        fm.append(f"language: {lang}")
    if tags:
        fm.append(f"tags: [{', '.join(tags)}]")
    if playlists:
        joined = ", ".join(_yaml_quote(p) for p in playlists)
        fm.append(f"playlists: [{joined}]")
    if video.duration_seconds is not None:
        fm.append(f"duration_seconds: {video.duration_seconds}")
    fm.append("---")

    body: list[str] = ["", f"# {video.title}", ""]
    if video.summary:
        body.append(
            rewrite_timestamp_links(video.summary, youtube_id=video.youtube_id)
        )
        body.append("")

    if highlights:
        body += ["## Highlights", ""]
        for h in highlights:
            text = h.get("text", "").strip()
            reason = h.get("reason", "").strip()
            line = f"- {text}"
            if reason:
                line += f" — {reason}"
            body.append(line)
        body.append("")

    if transcript and video.transcript:
        label = (
            "Article body" if video.kind == VideoKind.WEB else "Transcript"
        )
        body += [f"## {label}", "", video.transcript, ""]

    return "\n".join(fm + body)


def render_item_json(
    video: Video,
    *,
    tags: list[str],
    playlists: list[tuple[str, str]],
    transcript: bool = False,
    highlights: list[dict] | None = None,
    feedback: list[dict] | None = None,
) -> dict:
    """One item as a self-contained JSON document: identity + metadata +
    summary, with transcript opt-in, plus highlights and the requesting
    profile's feedback rows. `playlists` is a list of (id, title)."""
    doc: dict = {
        "id": video.id,
        "source": SOURCE,
        "kind": video.kind.value,
        "url": video.url,
        "title": video.title,
        "description": video.description,
        "duration_seconds": video.duration_seconds,
        "summary_model": video.summary_model,
        "language": video.summary_language or video.source_language,
        "created_at": video.created_at.isoformat(),
        "updated_at": _utc_iso(video.updated_at),
        "tags": list(tags),
        "playlists": [{"id": pid, "title": title} for pid, title in playlists],
        "summary": video.summary,
        "highlights": highlights or [],
        "feedback": feedback or [],
    }
    if transcript:
        doc["transcript"] = video.transcript
    return doc


def render_item_okf(
    video: Video,
    *,
    tags: list[str],
    playlists: list[str],
    highlights: list[dict] | None = None,
) -> dict:
    """One sync item: OKF vocabulary + summary body, no transcript.

    Field names follow OKF (`type`, `title`, `description`, `resource`,
    `timestamp`, `tags`) so a consumer maps them without a translation
    table. `timestamp` is `updated_at` — the same value the sync cursor
    orders by.

    Deliberately carries no transcript: it would bloat every MCP page
    and blunt semantic search on the consumer side. The transcript stays
    reachable through the `get_transcript` tool and the `resource` URL.
    """
    return {
        # Identity: a consumer keys on (source, id).
        "id": video.id,
        "source": SOURCE,
        # OKF vocabulary.
        "type": "note",
        "title": video.title,
        "description": video.description,
        "resource": video.url,
        "timestamp": _utc_iso(video.updated_at),
        "created": _utc_iso(video.created_at),
        "tags": list(tags),
        # yt-summary metadata.
        "kind": video.kind.value,
        "language": video.summary_language or video.source_language,
        "summary_model": video.summary_model,
        "playlists": list(playlists),
        "duration_seconds": video.duration_seconds,
        "highlights": highlights or [],
        "content": rewrite_timestamp_links(
            video.summary or "", youtube_id=video.youtube_id,
        ),
    }


def _export_basename(video: Video, fmt: str) -> str:
    """Filename for an item inside a bulk export, with the right suffix."""
    name = export_filename(video)  # ends with .md
    if fmt == "json":
        return name[:-3] + ".json"
    return name


def build_export_zip(items: list[dict], *, fmt: str) -> bytes:
    """Build a bulk-export ZIP in memory.

    Each item is a dict {video, tags, playlists, feedback?, highlights?,
    transcript?}. Writes one file per item (Markdown or JSON per `fmt`)
    plus a `manifest.json` listing every entry (id, title, url, file).

    In-memory is fine at this scale (a Pi-sized library); the route can
    stream the resulting bytes. Filenames are de-duplicated defensively —
    export_filename already suffixes the short id, but identical short ids
    across profiles shouldn't collide silently."""
    buf = io.BytesIO()
    manifest: list[dict] = []
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            video: Video = item["video"]
            tags = item.get("tags", [])
            playlists = item.get("playlists", [])
            want_transcript = item.get("transcript", False)
            highlights = item.get("highlights")
            feedback = item.get("feedback", [])

            fname = _export_basename(video, fmt)
            if fname in used:
                stem, _, ext = fname.rpartition(".")
                n = 2
                while f"{stem}-{n}.{ext}" in used:
                    n += 1
                fname = f"{stem}-{n}.{ext}"
            used.add(fname)

            if fmt == "json":
                # playlists may arrive as (id, title) tuples or bare names.
                pl = [
                    p if isinstance(p, tuple) else (p, p) for p in playlists
                ]
                content = json.dumps(
                    render_item_json(
                        video, tags=tags, playlists=pl,
                        transcript=want_transcript, highlights=highlights,
                        feedback=feedback,
                    ),
                    indent=2, ensure_ascii=False,
                )
            else:
                pl_names = [
                    p[1] if isinstance(p, tuple) else p for p in playlists
                ]
                content = render_item_md(
                    video, tags=tags, playlists=pl_names,
                    transcript=want_transcript, highlights=highlights,
                )
            zf.writestr(fname, content)
            manifest.append({
                "id": video.id,
                "source": SOURCE,
                "title": video.title,
                "url": video.url,
                "file": fname,
            })
        zf.writestr(
            "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False)
        )
    return buf.getvalue()
