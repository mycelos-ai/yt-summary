import logging

import aiosqlite
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.main import get_db
from app.repos import embeddings as embeddings_repo
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo
from app.repos import tags as tags_repo
from app.repos import videos as videos_repo
from app.services.embeddings import embed_text

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


async def _vector_ids_for_query(
    db: aiosqlite.Connection, query: str, limit: int = 50
) -> list[str]:
    """Best-effort: embed `query`, return ids ranked by similarity.

    Returns [] on any failure so the route degrades to FTS-only.
    """
    try:
        settings = await settings_repo.get_all(db)
        embedding_model = settings.get("embedding_model", "").strip() or None
        embedding_base_url = (
            settings.get("embedding_base_url", "").strip()
            or settings.get("llm_base_url", "").strip()
            or None
        )
        api_key = settings.get("llm_api_key", "")
        vector = await embed_text(
            query,
            model=embedding_model,
            api_key=api_key,
            base_url=embedding_base_url,
        )
        hits = await embeddings_repo.search_by_summary_vector(db, vector, limit=limit)
        return [video_id for video_id, _distance in hits]
    except Exception as e:
        log.info(
            "vector search degraded to FTS-only: %s: %s", type(e).__name__, e
        )
        return []


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    q: str | None = None,
    tag: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
):
    tag = tag.strip() if tag else None
    if q:
        vector_ids = await _vector_ids_for_query(db, q)
        videos = await videos_repo.search(db, q, tag=tag, vector_ids=vector_ids)
    else:
        videos = await videos_repo.list_recent(db, tag=tag)
    playlists = await playlists_repo.list_for_user(db, 1)
    video_ids = [v.id for v in videos]
    playlist_links = await playlists_repo.playlists_for_videos(db, video_ids)
    video_tags = await tags_repo.tags_for_videos(db, video_ids)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "videos": videos,
            "q": q,
            "active_tag": tag,
            "playlists": playlists,
            "playlist_links": playlist_links,
            "video_tags": video_tags,
        },
    )
