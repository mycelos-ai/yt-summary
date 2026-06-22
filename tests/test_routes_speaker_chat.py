import asyncio
from collections.abc import AsyncIterator
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app


async def _fake_stream(**kw) -> AsyncIterator[str]:
    for s in ("As ", "I ", "argued"):
        yield s


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    return create_app()


async def _setup(app, *, active=True):
    from app.models import TranscriptSource, VideoKind
    from app.repos import llm_models as llm_models_repo
    from app.repos import videos as videos_repo
    await videos_repo.upsert_metadata(
        app.state.db, video_id="vs1", url="u", title="All-In Ep",
        description="", thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.YOUTUBE, user_id=1)
    await videos_repo.set_transcript(
        app.state.db, "vs1", "transcript body", TranscriptSource.AUTO_SUBS)
    cur = await app.state.db.execute(
        "INSERT INTO speakers (user_id, name, name_key, is_active) VALUES (1,'Chamath','chamath',?)",
        (1 if active else 0,))
    speaker_id = cur.lastrowid
    await app.state.db.execute(
        "INSERT INTO source_speakers (source_id, speaker_id, detection_source) "
        "VALUES ('vs1', ?, 'show_rule')", (speaker_id,))
    await llm_models_repo.insert(
        app.state.db, label="Test", provider_id="openai", model="openai/gpt-4o",
        api_key="k", base_url="", make_default=True)
    await app.state.db.commit()
    return speaker_id


def test_per_episode_persona_turn_streams_and_persists(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        speaker_id = asyncio.get_event_loop().run_until_complete(_setup(app))
        with patch("app.routes.speakers.stream_speaker_reply", side_effect=_fake_stream):
            resp = client.post(
                f"/v/vs1/speaker/{speaker_id}/chat",
                data={"content": "what about SPACs?"})
        assert resp.status_code == 200
        assert "As I argued" in resp.text

        async def check():
            from app.repos import chat_threads as threads_repo
            from app.repos import chat as chat_repo
            tid = await threads_repo.get_or_create(
                app.state.db, scope="source_speaker", source_id="vs1",
                speaker_id=speaker_id)
            msgs = await chat_repo.history(app.state.db, "vs1", thread_id=tid)
            assert [m.role for m in msgs] == ["user", "assistant"]
            assert msgs[1].content == "As I argued"
        asyncio.get_event_loop().run_until_complete(check())


def test_whole_dossier_turn_uses_speaker_scope(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        speaker_id = asyncio.get_event_loop().run_until_complete(_setup(app))
        with patch("app.routes.speakers.stream_speaker_reply", side_effect=_fake_stream):
            resp = client.post(
                f"/speaker/{speaker_id}/chat", data={"content": "hi"})
        assert resp.status_code == 200
        assert "As I argued" in resp.text

        async def check():
            from app.repos import chat_threads as threads_repo
            from app.repos import chat as chat_repo
            tid = await threads_repo.get_or_create(
                app.state.db, scope="speaker", speaker_id=speaker_id)
            msgs = await chat_repo.history(app.state.db, thread_id=tid)
            # whichever video_id convention PR2 uses for speaker-scope rows,
            # the assistant reply is persisted on this thread:
            assert any(m.content == "As I argued" for m in msgs)
        asyncio.get_event_loop().run_until_complete(check())


def test_persona_turn_foreign_profile_404(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def setup_foreign():
            speaker_id = await _setup(app)
            # re-own the speaker to a different profile
            await app.state.db.execute(
                "UPDATE speakers SET user_id=999 WHERE id=?", (speaker_id,))
            await app.state.db.commit()
            return speaker_id
        speaker_id = asyncio.get_event_loop().run_until_complete(setup_foreign())
        with patch("app.routes.speakers.stream_speaker_reply", side_effect=_fake_stream):
            resp = client.post(f"/speaker/{speaker_id}/chat", data={"content": "hi"})
        assert resp.status_code == 404


def test_seed_ts_threaded_into_reply(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    captured: dict = {}

    async def capturing_stream(**kw):
        captured.update(kw)
        for s in ("ok",):
            yield s

    with TestClient(app) as client:
        speaker_id = asyncio.get_event_loop().run_until_complete(_setup(app))
        with patch("app.routes.speakers.stream_speaker_reply", side_effect=capturing_stream):
            client.post(
                f"/v/vs1/speaker/{speaker_id}/chat",
                data={"content": "this moment", "seed_ts": "12:04",
                      "seed_quote": "the quote"})
        assert captured["seed_ts"] == "12:04"
        assert captured["seed_quote"] == "the quote"
