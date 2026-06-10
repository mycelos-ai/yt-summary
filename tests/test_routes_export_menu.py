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
