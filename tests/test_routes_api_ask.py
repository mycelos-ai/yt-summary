"""REST API for ask-my-library (Part C.2). Runs synchronously and
returns the answer; LLM call is monkeypatched."""

from fastapi.testclient import TestClient

from app.main import create_app


def _setup(app, monkeypatch):
    import asyncio

    from app.services import ask as ask_svc

    async def fake_completion(*, system, user, model, api_key, base_url):
        return "Answer with [Agent Eval](/v/1:a)."
    monkeypatch.setattr(ask_svc, "_completion", fake_completion)

    async def seed():
        from app.repos import llm_models as llm_models_repo
        from app.repos import videos as videos_repo
        await llm_models_repo.insert(
            app.state.db, label="m", provider_id="openai",
            model="openai/gpt-4o", api_key="sk-x", base_url="",
            make_default=True,
        )
        await videos_repo.upsert_metadata(
            app.state.db, video_id="1:a", url="u", title="Agent Eval",
            description="", thumbnail_path=None, duration_seconds=None,
        )
        await videos_repo.set_summary(app.state.db, "1:a", "agent eval golden", "m")
    asyncio.get_event_loop().run_until_complete(seed())


def test_api_ask_returns_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _setup(app, monkeypatch)
        resp = client.post("/api/v1/ask", json={"question": "agent eval"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert "[Agent Eval](/v/1:a)" in body["answer"]
    assert "1:a" in body["sources"]


def test_api_ask_rejects_blank(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/api/v1/ask", json={"question": "  "})
    assert resp.status_code == 400
