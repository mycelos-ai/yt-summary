import asyncio
from collections.abc import AsyncIterator
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app


async def _fake_stream() -> AsyncIterator[str]:
    for s in ("Hello", " ", "user"):
        yield s


def test_post_chat_persists_and_streams(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.models import TranscriptSource
            from app.repos import llm_models as llm_models_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vc1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_transcript(
                app.state.db, "vc1", "transcript text", TranscriptSource.MANUAL_SUBS,
            )
            await llm_models_repo.insert(
                app.state.db,
                label="Test", provider_id="openai", model="openai/gpt-4o",
                api_key="k", base_url="", make_default=True,
            )
        asyncio.get_event_loop().run_until_complete(setup())

        with patch("app.routes.chat.stream_reply", return_value=_fake_stream()):
            resp = client.post(
                "/v/vc1/chat",
                data={"content": "what is this about?"},
            )
        body = resp.text

        assert "Hello" in body

        async def check():
            from app.repos import chat as chat_repo
            msgs = await chat_repo.history(app.state.db, "vc1")
            assert [m.role for m in msgs] == ["user", "assistant"]
            assert msgs[1].content == "Hello user"
        asyncio.get_event_loop().run_until_complete(check())


def test_chat_escapes_user_html_and_tokens(tmp_path, monkeypatch):
    from collections.abc import AsyncIterator
    from unittest.mock import patch

    async def evil_stream() -> AsyncIterator[str]:
        for s in ("<script>", "alert(1)", "</script>"):
            yield s

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.models import TranscriptSource
            from app.repos import llm_models as llm_models_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vx1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_transcript(
                app.state.db, "vx1", "transcript", TranscriptSource.MANUAL_SUBS,
            )
            await llm_models_repo.insert(
                app.state.db,
                label="Test", provider_id="openai", model="openai/gpt-4o",
                api_key="k", base_url="", make_default=True,
            )

        asyncio.get_event_loop().run_until_complete(setup())

        with patch("app.routes.chat.stream_reply", return_value=evil_stream()):
            resp = client.post(
                "/v/vx1/chat",
                data={"content": "<img src=x onerror=alert(1)>"},
            )
        body = resp.text

    # User HTML must be escaped
    assert "<img src=x" not in body
    assert "&lt;img" in body
    # LLM tokens must be escaped
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_chat_works_without_api_key_for_local_models(tmp_path, monkeypatch):
    from collections.abc import AsyncIterator
    from unittest.mock import patch

    async def fake_stream() -> AsyncIterator[str]:
        yield "ok"

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.models import TranscriptSource
            from app.repos import llm_models as llm_models_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vl1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_transcript(
                app.state.db, "vl1", "transcript", TranscriptSource.MANUAL_SUBS,
            )
            await llm_models_repo.insert(
                app.state.db,
                label="Local", provider_id="ollama", model="ollama/llama3.1",
                api_key="", base_url="http://localhost:11434", make_default=True,
            )

        asyncio.get_event_loop().run_until_complete(setup())

        with patch("app.routes.chat.stream_reply", return_value=fake_stream()):
            resp = client.post("/v/vl1/chat", data={"content": "hi"})

    assert resp.status_code == 200
    assert "ok" in resp.text


def test_chat_uses_override_model_when_id_supplied(tmp_path, monkeypatch):
    """POST /v/{id}/chat with llm_model_id form field → routes
    use that model's credentials, not the default's."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        captured: dict = {}
        captured_ids: dict = {}

        async def setup():
            from app.models import TranscriptSource, VideoKind
            from app.repos import llm_models as llm_models_repo
            from app.repos import videos as videos_repo

            await llm_models_repo.insert(
                app.state.db,
                label="Default", provider_id="ollama",
                model="ollama_chat/llama3.1", api_key="",
                base_url="http://lan:11434", make_default=True,
            )
            override_id = await llm_models_repo.insert(
                app.state.db,
                label="Claude", provider_id="anthropic",
                model="anthropic/claude-sonnet-4-6",
                api_key="sk-claude", base_url="",
                make_default=False,
            )
            captured_ids["override"] = override_id
            await videos_repo.upsert_metadata(
                app.state.db, video_id="cv1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
                kind=VideoKind.YOUTUBE, user_id=1,
            )
            await videos_repo.set_transcript(
                app.state.db, "cv1", "body",
                TranscriptSource.AUTO_SUBS,
            )

        asyncio.get_event_loop().run_until_complete(setup())

        async def fake_stream(**kw):
            captured.update(kw)
            if False:
                yield ""  # make this an async generator

        with patch(
            "app.routes.chat.stream_reply",
            side_effect=fake_stream,
        ):
            resp = client.post(
                "/v/cv1/chat",
                data={
                    "content": "hi",
                    "llm_model_id": str(captured_ids["override"]),
                },
            )
        assert resp.status_code == 200
        assert captured["model"] == "anthropic/claude-sonnet-4-6"
        assert captured["api_key"] == "sk-claude"
