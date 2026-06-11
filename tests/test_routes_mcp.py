"""MCP-tool dispatch tests.

We exercise the tool functions directly (not the SSE wire protocol) —
the SDK is responsible for serialization, but we want confidence the
yt-summary side returns sensible payloads.
"""

import pytest

from app.config import Config


@pytest.fixture
async def seeded_db_and_config(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    from app.repos import videos as videos_repo
    await videos_repo.upsert_metadata(
        db, video_id="mcptest1", url="https://youtu.be/mcptest1",
        title="MCP test", description="d",
        thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.set_summary(
        db, "mcptest1", "## TL;DR\nyes", "model"
    )
    return db, config


async def test_mcp_get_summary(seeded_db_and_config):
    db, _ = seeded_db_and_config
    from app.routes.mcp import _tool_get_summary
    out = await _tool_get_summary(db, video_id="mcptest1")
    assert "TL;DR" in out


async def test_mcp_get_summary_unknown(seeded_db_and_config):
    db, _ = seeded_db_and_config
    from app.routes.mcp import _tool_get_summary
    with pytest.raises(ValueError):
        await _tool_get_summary(db, video_id="nope")


async def test_mcp_search(seeded_db_and_config):
    db, _ = seeded_db_and_config
    from app.routes.mcp import _tool_search
    hits = await _tool_search(db, query="MCP", limit=5)
    assert any(h["video_id"] == "mcptest1" for h in hits)


async def test_mcp_ask_library(seeded_db_and_config, monkeypatch):
    db, _ = seeded_db_and_config
    from app.repos import llm_models as llm_models_repo
    from app.services import ask as ask_svc
    await llm_models_repo.insert(
        db, label="m", provider_id="openai", model="openai/gpt-4o",
        api_key="sk-x", base_url="", make_default=True,
    )

    async def fake_completion(*, system, user, model, api_key, base_url):
        return "Per [MCP test](/v/mcptest1), yes."
    monkeypatch.setattr(ask_svc, "_completion", fake_completion)

    # Query is a single FTS-matchable token against the seeded title
    # ("MCP test") — the embedder is unavailable in the sandbox, so
    # retrieval falls back to FTS-only here. (Vector retrieval is
    # exercised in test_services_ask via the real search path.)
    from app.routes.mcp import _tool_ask_library
    out = await _tool_ask_library(db, question="MCP")
    assert out["status"] == "ready"
    assert "/v/mcptest1" in out["answer"]
    assert "mcptest1" in out["sources"]


async def test_mcp_list_recent(seeded_db_and_config):
    db, _ = seeded_db_and_config
    from app.routes.mcp import _tool_list_recent
    rows = await _tool_list_recent(db, limit=10)
    assert any(r["video_id"] == "mcptest1" for r in rows)


async def test_tool_list_models_returns_rows(db):
    from app.repos import llm_models as llm_models_repo
    from app.routes.mcp import _tool_list_models

    await llm_models_repo.insert(
        db, label="Default", provider_id="anthropic",
        model="anthropic/claude-sonnet-4-6",
        api_key="k", base_url="", make_default=True,
    )
    await llm_models_repo.insert(
        db, label="Local", provider_id="ollama",
        model="ollama_chat/llama3.1",
        api_key="", base_url="http://lan:11434",
        make_default=False,
    )
    rows = await _tool_list_models(db)
    assert {r["label"] for r in rows} == {"Default", "Local"}
    default_row = next(r for r in rows if r["is_default"])
    assert default_row["label"] == "Default"
    assert default_row["model"] == "anthropic/claude-sonnet-4-6"


async def test_tool_resummarize_enqueues_job_with_overrides(db, config):
    from app.models import VideoKind
    from app.repos import jobs as jobs_repo
    from app.repos import llm_models as llm_models_repo
    from app.repos import videos as videos_repo
    from app.routes.mcp import _tool_resummarize

    mid = await llm_models_repo.insert(
        db, label="X", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    await videos_repo.upsert_metadata(
        db, video_id="rs1", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.YOUTUBE, user_id=1,
    )
    out = await _tool_resummarize(
        db, "rs1",
        llm_model_id=mid,
        additional_prompt="be terse",
    )
    assert out["queued"] is True
    assert out["video_id"] == "rs1"
    job = await jobs_repo.latest_for_video(db, "rs1")
    assert job is not None
    assert job.llm_model_id == mid
    assert job.additional_prompt == "be terse"


async def test_tool_resummarize_default_path_leaves_columns_null(db, config):
    from app.models import VideoKind
    from app.repos import jobs as jobs_repo
    from app.repos import videos as videos_repo
    from app.routes.mcp import _tool_resummarize

    await videos_repo.upsert_metadata(
        db, video_id="rs2", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.YOUTUBE, user_id=1,
    )
    out = await _tool_resummarize(db, "rs2")
    assert out["queued"] is True
    job = await jobs_repo.latest_for_video(db, "rs2")
    assert job is not None
    assert job.llm_model_id is None
    assert job.additional_prompt is None


async def test_tool_resummarize_unknown_video_raises(db, config):
    import pytest

    from app.routes.mcp import _tool_resummarize
    with pytest.raises(ValueError):
        await _tool_resummarize(db, "no-such-video")


async def test_tool_submit_url_forwards_overrides_to_jobs(db, config):
    """When submit_url is called with override params, the enqueued
    job carries them through (same plumbing as the HTTP route)."""
    from unittest.mock import patch

    from app.repos import jobs as jobs_repo
    from app.repos import llm_models as llm_models_repo
    from app.routes.mcp import _tool_submit_url

    mid = await llm_models_repo.insert(
        db, label="X", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )

    # We don't want submit_url to actually hit yt-dlp / fetch metadata —
    # short-circuit submit_video to return a stub resource and a known
    # video id. The override fields are forwarded to jobs_repo.enqueue
    # via the api_svc.submit_video kwargs, which we let pass through to
    # the real implementation by monkeypatching it minimally.
    async def fake_submit_video(db, config, *, url, user_id,
                                wait, wait_timeout,
                                llm_model_id, additional_prompt):
        from app.models import VideoKind
        from app.repos import videos as videos_repo
        await videos_repo.upsert_metadata(
            db, video_id="su1", url=url, title="t", description="",
            thumbnail_path=None, duration_seconds=None,
            kind=VideoKind.YOUTUBE, user_id=user_id,
        )
        await jobs_repo.enqueue(
            db, "su1",
            llm_model_id=llm_model_id,
            additional_prompt=additional_prompt,
        )
        return {
            "id": "su1", "kind": "youtube",
            "summary_ready": False, "title": "t",
        }

    with patch("app.routes.mcp.api_svc.submit_video", side_effect=fake_submit_video):
        out = await _tool_submit_url(
            db, config, "https://youtube.com/watch?v=su1",
            llm_model_id=mid,
            additional_prompt="be terse",
        )
    assert out["video_id"] == "su1"
    job = await jobs_repo.latest_for_video(db, "su1")
    assert job is not None
    assert job.llm_model_id == mid
    assert job.additional_prompt == "be terse"
