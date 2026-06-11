"""Render tests for the unified export menu macro (no live network)."""

from fastapi.testclient import TestClient

from app.main import create_app


def _seed_video_with_summary(app, vid="em1", title="Export Me"):
    import asyncio

    async def setup():
        from app.repos import videos as videos_repo
        await videos_repo.upsert_metadata(
            app.state.db, video_id=vid, url="u", title=title,
            description="d", thumbnail_path=None, duration_seconds=None,
        )
        await videos_repo.set_summary(app.state.db, vid, "## TL;DR\nhi", "m")
    asyncio.get_event_loop().run_until_complete(setup())


def test_summary_renders_one_export_menu(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_with_summary(app, "em1")
        resp = client.get("/v/em1")
    assert resp.status_code == 200
    assert resp.text.count('class="export-menu" data-export-menu') == 1
    assert 'data-md-url="/v/em1/export.md"' in resp.text
    assert 'data-json-url="/v/em1/export.json"' in resp.text


def test_summary_drops_legacy_buttons(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_with_summary(app, "em2")
        resp = client.get("/v/em2")
    assert "/v/em2/summary.md" not in resp.text
    assert "⬇ Export .md" not in resp.text
    assert "⬇ Export .json" not in resp.text


def test_export_menu_has_nojs_download_link(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_with_summary(app, "em3")
        resp = client.get("/v/em3")
    assert 'href="/v/em3/export.md"' in resp.text


def test_export_menu_script_included(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_with_summary(app, "em4")
        resp = client.get("/v/em4")
    assert "/static/export-menu.js" in resp.text


def test_transcript_renders_export_menu(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        _seed_video_with_summary(app, "tm1")

        async def add_transcript():
            from app.models import TranscriptSource
            from app.repos import videos as videos_repo
            await videos_repo.set_transcript(
                app.state.db, "tm1", "the transcript",
                TranscriptSource.AUTO_SUBS, language="en",
            )
        asyncio.get_event_loop().run_until_complete(add_transcript())
        resp = client.get("/v/tm1")
    assert resp.status_code == 200
    assert 'data-md-url="/v/tm1/transcript.md"' in resp.text
    assert '>↓ .md</a>' not in resp.text


def test_export_menu_script_loaded_without_summary(tmp_path, monkeypatch):
    """A video with a transcript but no summary still renders an export
    menu (for the transcript), so the clipboard JS must load even when
    there's no summary."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.models import TranscriptSource
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="nosum1", url="u", title="No Summary",
                description="d", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_transcript(
                app.state.db, "nosum1", "the transcript",
                TranscriptSource.AUTO_SUBS, language="en",
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/nosum1")
    assert resp.status_code == 200
    assert 'data-md-url="/v/nosum1/transcript.md"' in resp.text  # menu present
    assert "/static/export-menu.js" in resp.text                  # JS loaded
