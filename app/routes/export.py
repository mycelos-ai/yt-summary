"""Export routes — get summaries out of yt-summary as files.

Two audiences, two auth models:

* Web UI (cookie/profile scoped): per-item `/v/{id}/export.{md,json}` and
  bulk `/export.zip`. The active Profile is resolved from the cookie.
* API (key gated): `/api/v1/videos/{id}/export?format=` and
  `/api/v1/export?...` for scripts and MCP hosts.

All the text-building lives in services/export.py (pure functions); these
handlers just fetch, scope to the profile, and stream.
"""

from __future__ import annotations

import json
from datetime import date

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from app.main import get_current_user_id, get_db
from app.models import Video, VideoKind
from app.repos import feedback as feedback_repo
from app.repos import playlists as playlists_repo
from app.repos import tags as tags_repo
from app.repos import videos as videos_repo
from app.services import export as export_svc
from app.services.auth import authenticate

router = APIRouter()
api_router = APIRouter(prefix="/api/v1")


async def _api_user(
    request: Request, db: aiosqlite.Connection = Depends(get_db),
) -> int:
    return await authenticate(db, request)


async def _gather_item(
    db: aiosqlite.Connection, video: Video, *, user_id: int,
    want_feedback: bool,
) -> dict:
    """Collect everything one item's export needs: tags, playlists, and
    (optionally) the requesting profile's feedback + highlights."""
    tags = await tags_repo.tags_for_video(db, video.id)
    pls = await playlists_repo.playlists_for_videos(db, [video.id])
    playlists = pls.get(video.id, [])  # [(id, title), ...]
    feedback: list[dict] = []
    if want_feedback:
        rows = await feedback_repo.list_for_video(
            db, video_id=video.id, user_id=user_id,
        )
        feedback = [
            {
                "id": fb.id,
                "selected_text": fb.selected_text,
                "sentiment": fb.sentiment.value,
                "comment": fb.comment,
            }
            for fb in rows
        ]
    return {
        "video": video,
        "tags": tags,
        "playlists": playlists,
        "feedback": feedback,
        "highlights": export_svc.parse_highlights(video),
    }


async def _scoped_video(
    db: aiosqlite.Connection, video_id: str, user_id: int,
) -> Video:
    """Fetch a video and confirm it belongs to the active profile, else
    404 (don't leak another profile's items)."""
    video = await videos_repo.get(db, video_id)
    if video is None or video.user_id != user_id:
        raise HTTPException(404)
    return video


def _content_disposition(filename: str) -> dict[str, str]:
    return {"Content-Disposition": f'attachment; filename="{filename}"'}


# ----------------------------------------------------- per-item (web)

@router.get("/v/{video_id}/export.md")
async def export_item_md(
    video_id: str,
    transcript: bool = Query(False),
    highlights: bool = Query(False),
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    video = await _scoped_video(db, video_id, user_id)
    item = await _gather_item(db, video, user_id=user_id, want_feedback=False)
    md = export_svc.render_item_md(
        video,
        tags=item["tags"],
        playlists=[title for _, title in item["playlists"]],
        transcript=transcript,
        highlights=item["highlights"] if highlights else None,
    )
    fname = export_svc.export_filename(video)
    return PlainTextResponse(
        md, media_type="text/markdown; charset=utf-8",
        headers=_content_disposition(fname),
    )


@router.get("/v/{video_id}/export.json")
async def export_item_json(
    video_id: str,
    transcript: bool = Query(False),
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    video = await _scoped_video(db, video_id, user_id)
    item = await _gather_item(db, video, user_id=user_id, want_feedback=True)
    doc = export_svc.render_item_json(
        video, tags=item["tags"], playlists=item["playlists"],
        transcript=transcript, highlights=item["highlights"],
        feedback=item["feedback"],
    )
    fname = export_svc.export_filename(video)[:-3] + ".json"
    return Response(
        json.dumps(doc, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers=_content_disposition(fname),
    )


# ------------------------------------------------------ per-item (api)

@api_router.get("/videos/{video_id}/export")
async def api_export_item(
    video_id: str,
    format: str = Query("md", pattern="^(md|json)$"),
    transcript: bool = Query(False),
    highlights: bool = Query(False),
    user_id: int = Depends(_api_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "code": "NOT_FOUND"},
        )
    item = await _gather_item(
        db, video, user_id=user_id, want_feedback=(format == "json"),
    )
    if format == "json":
        doc = export_svc.render_item_json(
            video, tags=item["tags"], playlists=item["playlists"],
            transcript=transcript, highlights=item["highlights"],
            feedback=item["feedback"],
        )
        fname = export_svc.export_filename(video)[:-3] + ".json"
        return Response(
            json.dumps(doc, indent=2, ensure_ascii=False),
            media_type="application/json",
            headers=_content_disposition(fname),
        )
    md = export_svc.render_item_md(
        video, tags=item["tags"],
        playlists=[title for _, title in item["playlists"]],
        transcript=transcript,
        highlights=item["highlights"] if highlights else None,
    )
    return PlainTextResponse(
        md, media_type="text/markdown; charset=utf-8",
        headers=_content_disposition(export_svc.export_filename(video)),
    )


# ---------------------------------------------------------- bulk gather

async def _bulk_items(
    db: aiosqlite.Connection, *, user_id: int,
    tag: str | None, playlist_id: str | None, kind: str | None,
    since: str | None, until: str | None, fmt: str,
) -> list[dict]:
    """Apply the bulk filters and gather each surviving item. A Pi-sized
    library is small, so we fetch the profile's videos (optionally
    tag-filtered at the DB) and apply the rest in Python."""
    videos = await videos_repo.list_recent(
        db, limit=10_000, tag=tag, user_id=user_id,
    )
    if playlist_id is not None:
        linked = await playlists_repo.linked_video_ids(db, playlist_id)
        videos = [v for v in videos if v.id in linked]
    if kind is not None:
        videos = [v for v in videos if v.kind == VideoKind(kind)]
    if since is not None:
        s = date.fromisoformat(since)
        videos = [v for v in videos if v.created_at.date() >= s]
    if until is not None:
        u = date.fromisoformat(until)
        videos = [v for v in videos if v.created_at.date() <= u]
    return [
        await _gather_item(
            db, v, user_id=user_id, want_feedback=(fmt == "json"),
        )
        for v in videos
    ]


def _zip_response(items: list[dict], fmt: str) -> Response:
    raw = export_svc.build_export_zip(items, fmt=fmt)
    today = date.today().isoformat()
    fname = f"yt-summary-export-{today}.zip"
    return Response(
        raw, media_type="application/zip",
        headers=_content_disposition(fname),
    )


def _blank_to_none(value: str | None) -> str | None:
    """The settings Export form submits every filter field, empty ones
    included. Treat an empty/whitespace value as 'no filter'."""
    if value is None:
        return None
    value = value.strip()
    return value or None


_VALID_KINDS = {"youtube", "web", "email"}


def _normalize_filters(
    *, tag, playlist_id, kind, since, until,
) -> tuple:
    """Coerce blank form fields to None and validate kind/dates. Returns
    the cleaned (tag, playlist_id, kind, since, until)."""
    tag = _blank_to_none(tag)
    playlist_id = _blank_to_none(playlist_id)
    kind = _blank_to_none(kind)
    since = _blank_to_none(since)
    until = _blank_to_none(until)
    if kind is not None and kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail="kind must be one of: youtube, web, email",
        )
    for label, val in (("since", since), ("until", until)):
        if val is None:
            continue
        try:
            date.fromisoformat(val)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"{label} must be an ISO date (YYYY-MM-DD)",
            ) from None
    return tag, playlist_id, kind, since, until


# ---------------------------------------------------------- bulk (web)

@router.get("/export.zip")
async def export_zip(
    tag: str | None = Query(None),
    playlist_id: str | None = Query(None),
    kind: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    format: str = Query("md"),
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    fmt = "json" if _blank_to_none(format) == "json" else "md"
    tag, playlist_id, kind, since, until = _normalize_filters(
        tag=tag, playlist_id=playlist_id, kind=kind, since=since, until=until,
    )
    items = await _bulk_items(
        db, user_id=user_id, tag=tag, playlist_id=playlist_id, kind=kind,
        since=since, until=until, fmt=fmt,
    )
    return _zip_response(items, fmt)


# ---------------------------------------------------------- bulk (api)

@api_router.get("/export")
async def api_export_bulk(
    tag: str | None = Query(None),
    playlist_id: str | None = Query(None),
    kind: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    format: str = Query("md"),
    user_id: int = Depends(_api_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    fmt = "json" if _blank_to_none(format) == "json" else "md"
    tag, playlist_id, kind, since, until = _normalize_filters(
        tag=tag, playlist_id=playlist_id, kind=kind, since=since, until=until,
    )
    items = await _bulk_items(
        db, user_id=user_id, tag=tag, playlist_id=playlist_id, kind=kind,
        since=since, until=until, fmt=fmt,
    )
    return _zip_response(items, fmt)
