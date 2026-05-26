import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import create_app
from app.repos import digests as digests_repo


def test_get_digest_list_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/digest")
    assert resp.status_code == 200
    assert "Daily digest" in resp.text


def test_get_digest_show_renders_ready_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=1, period_start=start, period_end=end,
            )
            await digests_repo.mark_ready(
                app.state.db, digest_id=d.id, tldr="Hello world.",
                top_items_json="[]", item_count=0,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/digest/{digest_id}")
    assert resp.status_code == 200
    assert "Hello world" in resp.text


def test_get_digest_show_polls_when_pending(tmp_path, monkeypatch):
    """While the digest is pending, the template should render the
    HTMX-polling div so the browser refreshes until ready."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=1, period_start=start, period_end=end,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/digest/{digest_id}")
    assert resp.status_code == 200
    assert "Building digest" in resp.text
    assert 'hx-get="/digest/' in resp.text


def test_get_digest_show_404_for_foreign_profile(tmp_path, monkeypatch):
    """A digest belonging to user_id=2 must return 404 to the
    default-user-1 caller. No leak via 403."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            await app.state.db.execute(
                "INSERT INTO users (id, name) VALUES (2, 'other')"
            )
            await app.state.db.commit()
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=2, period_start=start, period_end=end,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/digest/{digest_id}")
    assert resp.status_code == 404


def test_get_digest_show_404_for_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/digest/9999")
    assert resp.status_code == 404


def test_post_digest_generate_redirects_to_new_digest(
    tmp_path, monkeypatch,
):
    """The handler should create a pending digest synchronously and
    redirect (303) to /digest/<id>. The actual generation happens in
    a background task we don't need to wait for here — verify only
    the synchronous part."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    # Monkeypatch the route's `_enqueue_digest_job` so we don't fire
    # the real LLM-driven digest in the background.
    from app.routes import digest as digest_route

    async def fake_enqueue(db, *, user_id, period_hours):
        end = datetime.now(UTC).replace(microsecond=0)
        start = end - timedelta(hours=period_hours)
        return await digests_repo.create_pending(
            db, user_id=user_id, period_start=start, period_end=end,
        )

    monkeypatch.setattr(digest_route, "_enqueue_digest_job", fake_enqueue)

    with TestClient(app) as client:
        resp = client.post(
            "/digest/generate", data={"period_hours": "24"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/digest/")


def test_post_digest_generate_rejects_invalid_period_hours(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/digest/generate", data={"period_hours": "0"},
            follow_redirects=False,
        )
    assert resp.status_code == 422
