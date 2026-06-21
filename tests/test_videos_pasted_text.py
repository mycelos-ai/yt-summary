"""Tests for the pasted-text add-tab (Task 8).

POST /videos with pasted_text creates a kind='text' library item
with the transcript pre-stored and a pipeline job enqueued.
"""

import asyncio

from fastapi.testclient import TestClient

from app.main import create_app


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_pasted_text_creates_text_item(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/videos",
            data={"pasted_text": "This is a transcribed interview body.",
                  "title": "My Interview"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        async def check():
            from app.models import VideoKind
            from app.repos import videos as videos_repo
            cur = await app.state.db.execute(
                "SELECT id FROM videos WHERE kind=? AND title=?",
                (VideoKind.TEXT.value, "My Interview"),
            )
            row = await cur.fetchone()
            assert row is not None, "Expected a kind='text' row in videos table"
            v = await videos_repo.get(app.state.db, row["id"])
            assert v.kind == VideoKind.TEXT
            assert v.transcript == "This is a transcribed interview body."
            return row["id"]
        item_id = _run(check())

        async def has_job():
            from app.repos import jobs as jobs_repo
            job = await jobs_repo.latest_for_video(app.state.db, item_id)
            assert job is not None, "Expected a pipeline job to be enqueued"
        _run(has_job())


def test_pasted_text_uses_content_hash_id(tmp_path, monkeypatch):
    """Re-pasting the same text must upsert the same row (stable id)."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        for _ in range(2):
            resp = client.post(
                "/videos",
                data={"pasted_text": "Stable content", "title": "Dedup Test"},
                headers={"HX-Request": "true"},
            )
            assert resp.status_code == 200

        async def check_one_row():
            cur = await app.state.db.execute(
                "SELECT COUNT(*) FROM videos WHERE title=?", ("Dedup Test",)
            )
            row = await cur.fetchone()
            assert row[0] == 1, "Same pasted text must not create duplicate rows"
        _run(check_one_row())


def test_pasted_text_fallback_title(tmp_path, monkeypatch):
    """When no title is supplied, a sensible default is used."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/videos",
            data={"pasted_text": "Some content with no title"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        async def check():
            from app.models import VideoKind
            cur = await app.state.db.execute(
                "SELECT title FROM videos WHERE kind=?",
                (VideoKind.TEXT.value,),
            )
            row = await cur.fetchone()
            assert row is not None
            assert row["title"]  # must not be empty
        _run(check())


def test_pasted_text_requires_a_body(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        # Neither url nor pasted_text → friendly error, not a crash.
        resp = client.post("/videos", data={}, headers={"HX-Request": "true"})
        assert resp.status_code in (200, 400)


def test_url_submit_still_works(tmp_path, monkeypatch):
    """Regression: existing URL submit must still produce a 422/redirect
    (or at least not 500). Since we don't have a real YouTube to hit we
    just verify the handler doesn't crash on url-only form input."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        # A valid-looking https URL that would be classified as web/youtube.
        # We don't want to make a real network call; just assert the handler
        # accepts url without pasted_text (old shape) and returns something
        # other than a 422 (which would mean 'url' is required again).
        resp = client.post(
            "/videos",
            data={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            headers={"HX-Request": "true"},
        )
        # 422 = form validation failure (url required) — that's the old bug.
        # Any other status (200 error page, 200 import card, etc.) is fine.
        assert resp.status_code != 422, (
            "url-only submit returned 422 — url param is required again"
        )
