from fastapi.testclient import TestClient

from app.main import create_app


def test_ask_thread_uses_shared_chat_classes(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.models import SynthesisStatus
            from app.repos import syntheses as syntheses_repo
            from app.repos import synthesis_messages as sm_repo
            from app.repos import videos as v
            await v.upsert_metadata(app.state.db, video_id="1:s", url="u",
                title="Src", description="d", thumbnail_path=None, duration_seconds=None)
            await v.set_summary(app.state.db, "1:s", "x", "m")
            s = await syntheses_repo.create_pending(app.state.db, user_id=1,
                query="q", source_ids=["1:s"])
            await sm_repo.append(app.state.db, synthesis_id=s.id, role="user",
                content="my question", status=SynthesisStatus.READY)
            await sm_repo.append(app.state.db, synthesis_id=s.id, role="assistant",
                content="the answer", status=SynthesisStatus.READY)
            return s.id
        sid = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/ask/{sid}")
    assert resp.status_code == 200
    assert "chat-thread" in resp.text
    assert "chat-bubble-user" in resp.text
    assert "chat-answer" in resp.text
    assert "chat-composer" in resp.text
    assert "chat-source-chip" in resp.text
    # old classes gone
    assert "ask-turn-user" not in resp.text
    assert "ask-followup" not in resp.text


def test_video_chat_uses_shared_chat_classes(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.models import ChatMessage  # noqa
            from app.repos import videos as v
            from app.repos import chat as chat_repo
            await v.upsert_metadata(app.state.db, video_id="cm1", url="u",
                title="T", description="d", thumbnail_path=None, duration_seconds=None)
            await v.set_summary(app.state.db, "cm1", "## TL;DR\nx", "m")
            await chat_repo.append(app.state.db, "cm1", "user", "hello")
            await chat_repo.append(app.state.db, "cm1", "assistant", "hi there")
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/cm1")
    assert resp.status_code == 200
    assert "chat-composer" in resp.text
    assert "chat-bubble-user" in resp.text
    assert "chat-answer" in resp.text
    assert 'class="chat-msg' not in resp.text  # old class gone
