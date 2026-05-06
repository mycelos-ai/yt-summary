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


async def test_mcp_list_recent(seeded_db_and_config):
    db, _ = seeded_db_and_config
    from app.routes.mcp import _tool_list_recent
    rows = await _tool_list_recent(db, limit=10)
    assert any(r["video_id"] == "mcptest1" for r in rows)
