"""MCP server mounted at /mcp/sse.

Tools delegate to services/api.py. We expose a smaller surface than
the REST API on purpose — Claude does best with a focused toolset.
"""

import logging
import os
from typing import Any

import aiosqlite
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.config import Config
from app.services import api as api_svc

log = logging.getLogger(__name__)


# These tool implementations don't depend on FastMCP — they're plain
# async functions taking explicit db/config args. The MCP wrappers
# below pull db/config from request scope.

async def _tool_submit_url(
    db: aiosqlite.Connection,
    config: Config,
    url: str,
    *,
    user_id: int = 1,
    wait_for_summary: bool = False,
    wait_timeout: int = 120,
    llm_model_id: int | None = None,
    additional_prompt: str = "",
) -> dict[str, Any]:
    resource = await api_svc.submit_video(
        db, config,
        url=url, user_id=user_id,
        wait=wait_for_summary, wait_timeout=wait_timeout,
        llm_model_id=llm_model_id,
        additional_prompt=additional_prompt.strip() or None,
    )
    out = {
        "video_id": resource["id"],
        "kind": resource["kind"],
        "summary_ready": resource["summary_ready"],
        "title": resource["title"],
    }
    if resource["summary_ready"]:
        from app.repos import videos as videos_repo
        v = await videos_repo.get(db, resource["id"])
        if v:
            out["summary"] = v.summary
    return out


async def _tool_search(
    db: aiosqlite.Connection,
    query: str,
    limit: int = 10,
    *,
    user_id: int = 1,
) -> list[dict[str, Any]]:
    hits = await api_svc.search_videos(
        db, query, limit=limit, user_id=user_id
    )
    out: list[dict[str, Any]] = []
    for h in hits:
        excerpt = ""
        if h.get("summary_ready"):
            from app.repos import videos as videos_repo
            v = await videos_repo.get(db, h["id"])
            if v and v.summary:
                excerpt = v.summary[:200]
        out.append({
            "video_id": h["id"],
            "title": h["title"],
            "url": h["url"],
            "summary_excerpt": excerpt,
        })
    return out


async def _tool_get_summary(
    db: aiosqlite.Connection, video_id: str
) -> str:
    from app.repos import videos as videos_repo
    v = await videos_repo.get(db, video_id)
    if v is None or not v.summary:
        raise ValueError(f"No summary for {video_id}")
    return v.summary


async def _tool_get_transcript(
    db: aiosqlite.Connection, video_id: str
) -> str:
    from app.repos import videos as videos_repo
    v = await videos_repo.get(db, video_id)
    if v is None or not v.transcript:
        raise ValueError(f"No transcript for {video_id}")
    return v.transcript


async def _tool_ask_video(
    db: aiosqlite.Connection,
    video_id: str,
    question: str,
    *,
    user_id: int = 1,
) -> str:
    result = await api_svc.chat_about_video(
        db, video_id, question, user_id=user_id
    )
    return result["answer"]


async def _tool_list_recent(
    db: aiosqlite.Connection,
    limit: int = 20,
    tag: str | None = None,
    *,
    user_id: int = 1,
) -> list[dict[str, Any]]:
    videos = await api_svc.list_videos(
        db, limit=limit, tag=tag, user_id=user_id
    )
    return [
        {
            "video_id": v["id"],
            "title": v["title"],
            "url": v["url"],
            "summary_ready": v["summary_ready"],
        }
        for v in videos
    ]


async def _tool_list_models(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Return all configured LLM models with id, label, model id,
    provider, and which one is the default. Useful when the user has
    just edited the Configured models card in Settings.

    Background work (auto-import from playlists, initial submit) always
    uses the default model. Pass an explicit ``llm_model_id`` to
    ``submit_url`` or ``resummarize`` to override per call.
    """
    from app.repos import llm_models as llm_models_repo
    rows = await llm_models_repo.list_all(db)
    return [
        {
            "id": r.id,
            "label": r.label,
            "model": r.model,
            "provider_id": r.provider_id,
            "is_default": r.is_default,
        }
        for r in rows
    ]


async def _tool_resummarize(
    db: aiosqlite.Connection,
    video_id: str,
    *,
    llm_model_id: int | None = None,
    additional_prompt: str = "",
) -> dict[str, Any]:
    """Re-run the summary for an existing video.

    llm_model_id=None falls back to the default model. additional_prompt
    is appended as a one-shot system instruction for this run only —
    it is not persisted. Returns ``{video_id, job_id, queued: True}``
    on success; raises ValueError if the video is unknown.
    """
    from app.repos import jobs as jobs_repo
    from app.repos import videos as videos_repo
    if await videos_repo.get(db, video_id) is None:
        raise ValueError(f"Unknown video: {video_id}")
    prompt = additional_prompt.strip() or None
    job_id = await jobs_repo.enqueue(
        db, video_id,
        llm_model_id=llm_model_id,
        additional_prompt=prompt,
    )
    return {"video_id": video_id, "job_id": job_id, "queued": True}


def build_mcp_server(app_state) -> FastMCP:
    """Wire the tool functions into FastMCP, threading the FastAPI
    app.state.db / app.state.config through."""
    # FastMCP's transport-security middleware defaults to host validation
    # with an empty allowlist (because our default host is "127.0.0.1"),
    # which 421s every request whose Host header isn't localhost. yt-summary
    # is a LAN tool with its own API-key auth — DNS-rebinding protection
    # is both redundant (no key → no access) and a foot-gun (every user
    # who hits /mcp/sse from another LAN machine sees a confusing error).
    # Disable it by default; set YTS_MCP_DISABLE_HOST_CHECK=0 to opt into
    # FastMCP's default behavior.
    disable_host_check = os.environ.get(
        "YTS_MCP_DISABLE_HOST_CHECK", "1"
    ) != "0"
    transport_security = (
        TransportSecuritySettings(enable_dns_rebinding_protection=False)
        if disable_host_check
        else None
    )
    mcp = FastMCP("yt-summary", transport_security=transport_security)

    @mcp.tool()
    async def submit_url(
        url: str,
        wait_for_summary: bool = False,
        wait_timeout: int = 120,
        llm_model_id: int | None = None,
        additional_prompt: str = "",
    ) -> dict[str, Any]:
        """Submit a YouTube or article URL and start processing.

        With wait_for_summary=True, the call blocks up to `wait_timeout`
        seconds and returns the summary inline if ready.

        llm_model_id (optional): override the default LLM for the
        generated summary. Use the `list_models` tool to discover
        configured models and their numeric ids. None = use the
        default. additional_prompt (optional): one-shot instruction
        appended to the system prompt for this run only; not
        persisted.
        """
        return await _tool_submit_url(
            app_state.db, app_state.config, url,
            wait_for_summary=wait_for_summary, wait_timeout=wait_timeout,
            llm_model_id=llm_model_id,
            additional_prompt=additional_prompt,
        )

    @mcp.tool()
    async def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search the library by keyword and meaning. Returns top hits."""
        return await _tool_search(app_state.db, query, limit=limit)

    @mcp.tool()
    async def get_summary(video_id: str) -> str:
        """Return the full Markdown summary for a video."""
        return await _tool_get_summary(app_state.db, video_id)

    @mcp.tool()
    async def get_transcript(video_id: str) -> str:
        """Return the full transcript / article body."""
        return await _tool_get_transcript(app_state.db, video_id)

    @mcp.tool()
    async def ask_video(video_id: str, question: str) -> str:
        """Ask a question about a video's content. Synchronous; persists
        the question + answer into the video's chat history."""
        return await _tool_ask_video(app_state.db, video_id, question)

    @mcp.tool()
    async def list_recent(
        limit: int = 20,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        """List recent videos in the library."""
        return await _tool_list_recent(app_state.db, limit=limit, tag=tag)

    @mcp.tool()
    async def list_models() -> list[dict[str, Any]]:
        """Return all configured LLM models — id, label, model id,
        provider, and which one is the default. Use the returned
        ``id`` values for the ``llm_model_id`` parameter on
        ``submit_url`` or ``resummarize``.
        """
        return await _tool_list_models(app_state.db)

    @mcp.tool()
    async def resummarize(
        video_id: str,
        llm_model_id: int | None = None,
        additional_prompt: str = "",
    ) -> dict[str, Any]:
        """Re-run the summary for an existing video. The new summary
        replaces the previous one when the worker picks up the queued
        job.

        Use ``list_models`` to discover configured llm_model_id values.
        Pass llm_model_id=None to use the default. additional_prompt is
        a one-shot instruction appended to the system prompt for this
        run only; not persisted.
        """
        return await _tool_resummarize(
            app_state.db, video_id,
            llm_model_id=llm_model_id,
            additional_prompt=additional_prompt,
        )

    return mcp
