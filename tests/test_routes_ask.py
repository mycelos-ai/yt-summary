"""Route tests for "ask my library" (Part C.2).

Background execution is monkeypatched out (we drive the synthesis row
directly), mirroring how the digest route tests avoid live LLM calls.
"""

import asyncio

from fastapi.testclient import TestClient

from app.main import create_app


def test_get_ask_renders_box_and_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/ask")
    assert resp.status_code == 200
    assert 'action="/ask"' in resp.text
    assert 'method="post"' in resp.text


def test_post_ask_enqueues_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    # Don't actually run the background job in the route test.
    from app.routes import ask as ask_route

    async def fake_enqueue(db, *, user_id, query):
        from app.repos import syntheses as syntheses_repo
        return await syntheses_repo.create_pending(
            db, user_id=user_id, query=query, source_ids=[],
        )
    monkeypatch.setattr(ask_route, "_enqueue_ask_job", fake_enqueue)

    with TestClient(app) as client:
        resp = client.post(
            "/ask", data={"query": "What about agent eval?"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/ask/")


def test_enqueue_marks_failed_when_run_crashes(tmp_path, monkeypatch):
    """Safety net: if the background synthesis task raises (e.g. a setup
    error before run()'s own try/except can mark it failed), the row must
    still end up 'failed' — never stuck forever on 'pending'."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    from app.repos import syntheses as syntheses_repo
    from app.routes import ask as ask_route
    from app.services import ask as ask_service

    async def boom(db, *, synthesis_id, user_id):
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(ask_service, "run", boom)

    with TestClient(app):
        async def scenario():
            s = await ask_route._enqueue_ask_job(
                app.state.db, user_id=1, query="q",
            )
            # Let the background task run to completion.
            for t in list(ask_route._PENDING_JOBS):
                await t
            return await syntheses_repo.get(app.state.db, s.id)
        got = asyncio.get_event_loop().run_until_complete(scenario())
    assert got is not None
    assert got.status.value == "failed"
    assert "simulated crash" in (got.error or "")


def test_post_ask_rejects_blank_query(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/ask", data={"query": "   "}, follow_redirects=False)
    assert resp.status_code == 400


def test_get_ask_show_renders_ready_result(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import syntheses as syntheses_repo
            s = await syntheses_repo.create_pending(
                app.state.db, user_id=1, query="q", source_ids=[],
            )
            await syntheses_repo.mark_ready(
                app.state.db, synthesis_id=s.id,
                result_md="Here is the **answer**.",
            )
            return s.id
        sid = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/ask/{sid}")
    assert resp.status_code == 200
    assert "answer" in resp.text


def test_get_ask_show_polls_while_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import syntheses as syntheses_repo
            s = await syntheses_repo.create_pending(
                app.state.db, user_id=1, query="q", source_ids=[],
            )
            return s.id
        sid = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/ask/{sid}")
    assert resp.status_code == 200
    # Self-polling while pending — and it must poll the FRAGMENT endpoint,
    # not the full page (which would nest header+main on every tick).
    assert f"/ask/{sid}/fragment" in resp.text


def _seed_pending(app, *, user_id=1, query="q"):
    async def setup():
        from app.repos import syntheses as syntheses_repo
        s = await syntheses_repo.create_pending(
            app.state.db, user_id=user_id, query=query, source_ids=[],
        )
        return s.id
    return asyncio.get_event_loop().run_until_complete(setup())


def test_ask_fragment_has_no_page_chrome(tmp_path, monkeypatch):
    """The polled fragment must NOT contain the base layout (header/main),
    otherwise hx-swap=outerHTML nests the whole page into itself on every
    tick — the recursive-duplication bug."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        sid = _seed_pending(app)
        resp = client.get(f"/ask/{sid}/fragment")
    assert resp.status_code == 200
    assert "site-header" not in resp.text
    assert "<main" not in resp.text
    # While pending it keeps polling itself.
    assert f"/ask/{sid}/fragment" in resp.text


def test_ask_fragment_hx_refresh_when_ready(tmp_path, monkeypatch):
    """Once ready, the HTMX poll gets HX-Refresh so the browser reloads
    the full page once (chrome stays in sync), mirroring the digest flow."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import syntheses as syntheses_repo
            s = await syntheses_repo.create_pending(
                app.state.db, user_id=1, query="q", source_ids=[],
            )
            await syntheses_repo.mark_ready(
                app.state.db, synthesis_id=s.id, result_md="done",
            )
            return s.id
        sid = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(
            f"/ask/{sid}/fragment", headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Refresh") == "true"


def test_ask_show_lists_sources_when_present(tmp_path, monkeypatch):
    """A ready synthesis with recorded source ids must render a Sources
    list linking to each item (so the user sees what was considered)."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import syntheses as syntheses_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="src1", url="u", title="Source One",
                description="d", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_summary(app.state.db, "src1", "s", "m")
            s = await syntheses_repo.create_pending(
                app.state.db, user_id=1, query="q", source_ids=["src1"],
            )
            await syntheses_repo.mark_ready(
                app.state.db, synthesis_id=s.id, result_md="answer",
            )
            return s.id
        sid = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/ask/{sid}")
    assert resp.status_code == 200
    assert "Source One" in resp.text
    assert "/v/src1" in resp.text


def test_get_ask_show_404_for_foreign_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import syntheses as syntheses_repo
            s = await syntheses_repo.create_pending(
                app.state.db, user_id=2, query="q", source_ids=[],
            )
            return s.id
        sid = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/ask/{sid}")
    assert resp.status_code == 404
