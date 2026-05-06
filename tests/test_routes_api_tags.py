from fastapi.testclient import TestClient

from app.main import create_app


def test_list_tags(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import tags as tags_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vt1", url="u", title="vt1",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await tags_repo.set_tags_for_video(
                app.state.db, "vt1", ["one", "two"]
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/tags")
    assert resp.status_code == 200
    body = resp.json()
    names = {t["name"] for t in body["tags"]}
    assert names == {"one", "two"}
