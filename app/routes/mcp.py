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
from app.services.export import SOURCE

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
        "source": SOURCE,
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
            "source": SOURCE,
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
            "source": SOURCE,
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


async def _tool_ask_library(
    db: aiosqlite.Connection,
    question: str,
    *,
    user_id: int = 1,
) -> dict[str, Any]:
    """Answer a question across the library's stored summaries, with
    citations. Runs synchronously and returns
    ``{id, status, answer, sources}``. The highest-value MCP addition:
    turns any MCP host into a front-end for the whole library."""
    from app.services import ask as ask_svc
    s, answer = await ask_svc.ask_now(db, user_id=user_id, query=question.strip())
    import json as _json
    try:
        sources = _json.loads(s.source_ids_json)
    except (ValueError, TypeError):
        sources = []
    return {
        "id": s.id,
        "status": answer.status.value,
        "answer": answer.content,
        "sources": sources,
        "error": answer.error,
    }


def _api_key_is_configured(config) -> bool:
    """Synchronous, best-effort check for whether any user has an API key.

    Read at build time, before the async app.state.db exists, so we open a
    short-lived sqlite connection against config.db_path. Returns False on
    any problem (missing config, missing db/table, fresh install) — i.e.
    "no key", which keeps the host-check ENABLED. Fail closed."""
    if config is None:
        return False
    import sqlite3
    try:
        path = config.db_path
        if not path.exists():
            return False
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT 1 FROM users WHERE api_key_hash IS NOT NULL LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        return row is not None
    except Exception:
        return False


def build_mcp_server(app_state) -> FastMCP:
    """Wire the tool functions into FastMCP, threading the FastAPI
    app.state.db / app.state.config through."""
    # FastMCP's transport-security middleware validates the Host header
    # (DNS-rebinding protection). For a LAN tool with API-key auth that's
    # redundant *once a key exists* — but the out-of-the-box state is "no
    # key configured" (auth disabled). Disabling the host-check there too
    # composes into a real hole: a malicious website can DNS-rebind to
    # http://<lan-host>:8200 and drive the open MCP surface from a victim's
    # browser. So couple the default to key presence:
    #   * no key   -> protection ENABLED  (fail closed)
    #   * key set  -> protection relaxed   (the key gates every request)
    # YTS_MCP_DISABLE_HOST_CHECK overrides both ways:
    #   "1" -> always disable, "0" -> always enable.
    env = os.environ.get("YTS_MCP_DISABLE_HOST_CHECK")
    if env == "1":
        disable_host_check = True
    elif env == "0":
        disable_host_check = False
    else:
        disable_host_check = _api_key_is_configured(
            getattr(app_state, "config", None)
        )
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

    @mcp.tool()
    async def ask_library(question: str) -> dict[str, Any]:
        """Answer a question across your saved library, citing the items
        it drew on. Returns {id, status, answer (Markdown with
        [title](/v/<id>) links), sources (the video ids used)}.

        Use this to query everything you've summarised at once — e.g.
        "What have I saved about agent evaluation?" — instead of reading
        items one by one.
        """
        return await _tool_ask_library(app_state.db, question)

    return mcp
