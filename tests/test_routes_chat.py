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
            from app.repos import settings as settings_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vc1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_transcript(
                app.state.db, "vc1", "transcript text", TranscriptSource.MANUAL_SUBS,
            )
            await settings_repo.set(app.state.db, "llm_model", "openai/gpt-4o")
            await settings_repo.set(app.state.db, "llm_api_key", "k")
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
