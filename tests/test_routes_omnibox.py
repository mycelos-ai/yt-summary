from fastapi.testclient import TestClient

from app.main import create_app


def _home(app, monkeypatch):
    # Configure a default model so the home page renders (not onboarding).
    import asyncio
    async def seed():
        from app.repos import llm_models as r
        await r.insert(app.state.db, label="m", provider_id="openai",
                       model="openai/gpt-4o", api_key="k", base_url="",
                       make_default=True)
    asyncio.get_event_loop().run_until_complete(seed())


def test_home_has_omnibox_with_search_and_ask(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _home(app, monkeypatch)
        resp = client.get("/")
    assert resp.status_code == 200
    assert "data-omnibox" in resp.text
    # search form (GET /?q=) present as the no-JS fallback
    assert 'name="q"' in resp.text
    # ask form posts to /ask
    assert 'action="/ask"' in resp.text
    # a Search/Ask toggle exists
    assert "omnibox-toggle" in resp.text


def test_home_has_add_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _home(app, monkeypatch)
        resp = client.get("/")
    assert resp.status_code == 200
    # add trigger + overlay form posting to /videos with name="url"
    assert "data-add-overlay" in resp.text
    assert 'action="/videos"' in resp.text
    assert 'name="url"' in resp.text


def test_home_old_standalone_affordances_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _home(app, monkeypatch)
        resp = client.get("/")
    # the old standalone search-form and the ask link text are gone (folded into omnibox)
    assert 'class="search-form"' not in resp.text
    assert "💬 Ask my library →" not in resp.text


def test_search_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _home(app, monkeypatch)
        resp = client.get("/?q=anything")
    assert resp.status_code == 200
