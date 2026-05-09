import logging

import aiosqlite
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.main import _onboarding_status, get_current_user, get_current_user_id, get_db
from app.repos import embeddings as embeddings_repo
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo
from app.repos import tags as tags_repo
from app.repos import videos as videos_repo
from app.services.embeddings import embed_text
from app.template_filters import register_filters

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)

# Page sizes for the home view. Kept as module constants so the
# load-more fragment route stays in lockstep with the initial render.
HOME_VIDEO_PAGE_SIZE = 25
HOME_PLAYLIST_LIMIT = 5


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
    current_user_id: int = Depends(get_current_user_id),
    current_user=Depends(get_current_user),
):
    # First-run nudge: send a brand-new install through the onboarding
    # wizard before showing the (empty) library. Only the home page does
    # this — every other route stays untouched so the wizard pages
    # themselves, /settings, and direct deep links keep working.
    status = await _onboarding_status(db)
    if status["pending"]:
        return RedirectResponse(str(status["next_step"]), status_code=303)

    tag = tag.strip() if tag else None
    if q:
        # Search results aren't paginated — they're already capped at
        # the search ranker's limit and tend to be small.
        vector_ids = await _vector_ids_for_query(db, q)
        videos = await videos_repo.search(
            db, q, tag=tag, vector_ids=vector_ids, user_id=current_user_id
        )
        has_more_videos = False
    else:
        # +1 trick: ask for one extra row so we can tell whether there's
        # another batch behind this one without a separate COUNT query.
        rows = await videos_repo.list_recent(
            db, limit=HOME_VIDEO_PAGE_SIZE + 1, tag=tag,
            user_id=current_user_id,
        )
        has_more_videos = len(rows) > HOME_VIDEO_PAGE_SIZE
        videos = rows[:HOME_VIDEO_PAGE_SIZE]

    # Same +1 trick for playlists: limit to 5 on home, but ask for 6
    # so the template knows whether to show the "More →" link.
    playlists_plus_one = await playlists_repo.list_for_user(
        db, current_user_id, limit=HOME_PLAYLIST_LIMIT + 1
    )
    has_more_playlists = len(playlists_plus_one) > HOME_PLAYLIST_LIMIT
    playlists = playlists_plus_one[:HOME_PLAYLIST_LIMIT]

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
            "has_more_videos": has_more_videos,
            "has_more_playlists": has_more_playlists,
            "video_page_size": HOME_VIDEO_PAGE_SIZE,
            "current_user": current_user,
        },
    )


@router.get("/videos/load-more", response_class=HTMLResponse)
async def load_more_videos(
    request: Request,
    offset: int = 0,
    tag: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    """Return the next page of video cards as an HTML fragment.

    The button at the bottom of the previous batch points here with
    ``offset=N``. We render the next ``HOME_VIDEO_PAGE_SIZE`` cards
    and, if there's still more behind them, a follow-up Load-more
    button with the next offset. The button replaces itself via
    ``hx-swap="outerHTML"``, so the cards land in the grid above
    while the button moves down.
    """
    tag = tag.strip() if tag else None
    offset = max(0, offset)
    rows = await videos_repo.list_recent(
        db, limit=HOME_VIDEO_PAGE_SIZE + 1, tag=tag, offset=offset,
        user_id=current_user_id,
    )
    has_more = len(rows) > HOME_VIDEO_PAGE_SIZE
    videos = rows[:HOME_VIDEO_PAGE_SIZE]
    video_ids = [v.id for v in videos]
    playlist_links = await playlists_repo.playlists_for_videos(db, video_ids)
    video_tags = await tags_repo.tags_for_videos(db, video_ids)
    next_offset = offset + HOME_VIDEO_PAGE_SIZE
    return templates.TemplateResponse(
        request,
        "_video_load_more.html",
        {
            "videos": videos,
            "playlist_links": playlist_links,
            "video_tags": video_tags,
            "has_more": has_more,
            "next_offset": next_offset,
            "active_tag": tag,
        },
    )
