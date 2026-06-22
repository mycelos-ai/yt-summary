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
        "INSERT INTO speakers (user_id, name, name_key, is_active) "
        "VALUES (1,'Chamath','chamath',?)",
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
            from app.repos import chat as chat_repo
            from app.repos import chat_threads as threads_repo
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
            from app.repos import chat as chat_repo
            from app.repos import chat_threads as threads_repo
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


# ---------------------------------------------------------------------------
# Task 8: on-demand extract + claim edit/review routes
# ---------------------------------------------------------------------------

def test_on_demand_extract_one_source(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)

    async def fake_extract(db_, source, speaker_ids, *, model, api_key, base_url):
        from app.repos import speaker_claims as repo
        await repo.insert_claim(db_, speaker_id=speaker_ids[0],
                                source_id=source.id, claim="extracted!", topic="t")
        return [{"claim": "extracted!"}]

    with TestClient(app) as client:
        speaker_id = asyncio.get_event_loop().run_until_complete(_setup(app))
        with patch("app.routes.speakers.extract_claims_for_source", side_effect=fake_extract):
            resp = client.post(f"/speaker/{speaker_id}/sources/vs1/extract")
        assert resp.status_code == 200
        assert "extracted!" in resp.text


def test_claim_review_sets_status(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def setup():
            speaker_id = await _setup(app)
            from app.repos import speaker_claims as repo
            cid = await repo.insert_claim(
                app.state.db, speaker_id=speaker_id, source_id="vs1",
                claim="c", topic="t")
            return speaker_id, cid
        speaker_id, cid = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post(
            f"/speaker/{speaker_id}/claims/{cid}/review",
            data={"status": "accepted"})
        assert resp.status_code == 200

        async def check():
            from app.repos import speaker_claims as repo
            c = (await repo.list_for_speaker(app.state.db, speaker_id))[0]
            assert c.review_status == "accepted"
        asyncio.get_event_loop().run_until_complete(check())


def test_claim_edit_updates_text(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def setup():
            speaker_id = await _setup(app)
            from app.repos import speaker_claims as repo
            cid = await repo.insert_claim(
                app.state.db, speaker_id=speaker_id, source_id="vs1",
                claim="old", topic="t")
            return speaker_id, cid
        speaker_id, cid = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post(
            f"/speaker/{speaker_id}/claims/{cid}/edit",
            data={"claim": "new text", "topic": "macro"})
        assert resp.status_code == 200
        assert "new text" in resp.text


def test_review_foreign_profile_404(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def setup():
            speaker_id = await _setup(app)
            from app.repos import speaker_claims as repo
            cid = await repo.insert_claim(
                app.state.db, speaker_id=speaker_id, source_id="vs1", claim="c")
            await app.state.db.execute(
                "UPDATE speakers SET user_id=999 WHERE id=?", (speaker_id,))
            await app.state.db.commit()
            return speaker_id, cid
        speaker_id, cid = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post(
            f"/speaker/{speaker_id}/claims/{cid}/review",
            data={"status": "accepted"})
        assert resp.status_code == 404


def test_claim_edit_rejects_claim_of_other_speaker(tmp_path, monkeypatch):
    """Finding 6: editing speaker A's claim via speaker B's URL must 404."""
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def setup():
            from app.models import VideoKind
            from app.repos import llm_models as llm_models_repo
            from app.repos import speaker_claims as repo
            from app.repos import speakers as sp_repo_local
            from app.repos import videos as videos_repo
            # Insert a video so source_id='v1' is valid
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
                kind=VideoKind.YOUTUBE, user_id=1)
            await llm_models_repo.insert(
                app.state.db, label="Test", provider_id="openai",
                model="openai/gpt-4o", api_key="k", base_url="", make_default=True)
            await app.state.db.commit()
            # Two speakers in the same profile; a claim on speaker A
            a_id = await sp_repo_local.resolve_speaker(app.state.db, name="Speaker A")
            b_id = await sp_repo_local.resolve_speaker(app.state.db, name="Speaker B")
            claim_id = await repo.insert_claim(
                app.state.db, speaker_id=a_id, source_id="v1",
                claim="A said this")
            return b_id, claim_id
        b_id, claim_id = asyncio.get_event_loop().run_until_complete(setup())
        # Editing speaker A's claim via speaker B's URL must 404 (not silently edit)
        resp = client.post(
            f"/speaker/{b_id}/claims/{claim_id}/edit",
            data={"claim": "hijacked"})
        assert resp.status_code == 404


def test_claim_review_rejects_claim_of_other_speaker(tmp_path, monkeypatch):
    """Reviewing speaker A's claim via speaker B's URL must 404."""
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def setup():
            from app.models import VideoKind
            from app.repos import llm_models as llm_models_repo
            from app.repos import speaker_claims as repo
            from app.repos import speakers as sp_repo_local
            from app.repos import videos as videos_repo
            # Insert a video so source_id='v1' is valid
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
                kind=VideoKind.YOUTUBE, user_id=1)
            await llm_models_repo.insert(
                app.state.db, label="Test", provider_id="openai",
                model="openai/gpt-4o", api_key="k", base_url="", make_default=True)
            await app.state.db.commit()
            # Two speakers in the same profile; a claim on speaker A
            a_id = await sp_repo_local.resolve_speaker(app.state.db, name="Speaker A")
            b_id = await sp_repo_local.resolve_speaker(app.state.db, name="Speaker B")
            claim_id = await repo.insert_claim(
                app.state.db, speaker_id=a_id, source_id="v1",
                claim="A said this")
            return b_id, claim_id
        b_id, claim_id = asyncio.get_event_loop().run_until_complete(setup())
        # Reviewing speaker A's claim via speaker B's URL must 404 (not silently mutate)
        resp = client.post(
            f"/speaker/{b_id}/claims/{claim_id}/review",
            data={"status": "accepted"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Task 9: dossier UI + persona disclaimer banner
# ---------------------------------------------------------------------------

def test_speaker_page_renders_dossier_with_review_state(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def setup():
            speaker_id = await _setup(app)
            from app.repos import speaker_claims as repo
            cid1 = await repo.insert_claim(
                app.state.db, speaker_id=speaker_id, source_id="vs1",
                claim="reviewed claim", topic="markets",
                evidence_text="ev", evidence_start_s=42)
            await repo.insert_claim(
                app.state.db, speaker_id=speaker_id, source_id="vs1",
                claim="raw claim", topic="ai")
            await repo.set_review_status(app.state.db, cid1, "accepted")
            return speaker_id
        speaker_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/speaker/{speaker_id}")
        assert resp.status_code == 200
        body = resp.text
        # claims grouped by topic, with evidence + the unreviewed marker
        assert "reviewed claim" in body
        assert "raw claim" in body
        assert "markets" in body and "ai" in body
        assert "unreviewed" in body          # the marker class/label for the raw claim
        # whole-dossier chat composer points at the speaker route
        assert f"/speaker/{speaker_id}/chat" in body


def test_video_detail_has_persona_disclaimer_banner(tmp_path, monkeypatch):
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        asyncio.get_event_loop().run_until_complete(_setup(app))
        resp = client.get("/v/vs1")
        assert resp.status_code == 200
        # the simulated-persona banner copy is present (hidden until persona mode)
        assert "Simulated" in resp.text or "AI impression" in resp.text


def test_per_episode_persona_does_not_leak_into_video_chat(tmp_path, monkeypatch):
    """Finding 1: persona rows written by the per-episode persona route must NOT
    appear in the video's regular chat history (no thread_id filter in the
    normal video-chat read path)."""
    app = _client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        speaker_id = asyncio.get_event_loop().run_until_complete(_setup(app))
        with patch("app.routes.speakers.stream_speaker_reply", side_effect=_fake_stream):
            resp = client.post(
                f"/v/vs1/speaker/{speaker_id}/chat",
                data={"content": "hi"})
        assert resp.status_code == 200

        async def check():
            from app.repos import chat as chat_repo
            from app.repos import chat_threads as threads_repo
            # Video-chat read path: no thread_id — exact path used by
            # app/routes/videos.py and app/routes/chat.py
            vid_hist = await chat_repo.history(app.state.db, "vs1")
            assert vid_hist == [], (
                f"persona rows leaked into video chat: {[m.content for m in vid_hist]}"
            )
            # Also verify the persona thread itself still has the 2 rows
            tid = await threads_repo.get_or_create(
                app.state.db, scope="source_speaker",
                source_id="vs1", speaker_id=speaker_id)
            thread_hist = await chat_repo.history(app.state.db, thread_id=tid)
            assert [m.role for m in thread_hist] == ["user", "assistant"], (
                f"persona thread lost messages: {[m.role for m in thread_hist]}"
            )
        asyncio.get_event_loop().run_until_complete(check())


# ---------------------------------------------------------------------------
# Finding #5: editing a claim must re-embed the new text
# ---------------------------------------------------------------------------

def test_edit_claim_reembeds(tmp_path, monkeypatch):
    """POST .../claims/{id}/edit with a changed claim text must call
    _embed_claim_best_effort so the vector stays in sync."""
    app = _client(tmp_path, monkeypatch)
    calls: list[tuple] = []

    async def recording_embed(db_, claim_id, claim_text):
        calls.append((claim_id, claim_text))

    with TestClient(app) as client:
        async def setup():
            speaker_id = await _setup(app)
            from app.repos import speaker_claims as repo
            cid = await repo.insert_claim(
                app.state.db, speaker_id=speaker_id, source_id="vs1",
                claim="original claim text", topic="t")
            return speaker_id, cid
        speaker_id, cid = asyncio.get_event_loop().run_until_complete(setup())

        with patch("app.routes.speakers._embed_claim_best_effort",
                   side_effect=recording_embed):
            resp = client.post(
                f"/speaker/{speaker_id}/claims/{cid}/edit",
                data={"claim": "completely new claim text", "topic": "macro"})

    assert resp.status_code == 200
    # The embed helper must have been called with the new text
    assert len(calls) == 1, f"expected 1 embed call, got {calls}"
    assert calls[0][0] == cid
    assert calls[0][1] == "completely new claim text"


# ---------------------------------------------------------------------------
# Finding #6: un-rejecting a claim must re-embed it
# ---------------------------------------------------------------------------

def test_reaccept_reembeds(tmp_path, monkeypatch):
    """Restoring a rejected claim (status -> accepted) must re-embed it so
    the vector that was deleted on rejection is recreated."""
    app = _client(tmp_path, monkeypatch)
    calls: list[tuple] = []

    async def recording_embed(db_, claim_id, claim_text):
        calls.append((claim_id, claim_text))

    with TestClient(app) as client:
        async def setup():
            speaker_id = await _setup(app)
            from app.repos import speaker_claims as repo
            cid = await repo.insert_claim(
                app.state.db, speaker_id=speaker_id, source_id="vs1",
                claim="claim to reject then restore", topic="t")
            # First: reject (this deletes the vector in the real code)
            await repo.set_review_status(app.state.db, cid, "rejected")
            await app.state.db.commit()
            return speaker_id, cid
        speaker_id, cid = asyncio.get_event_loop().run_until_complete(setup())

        with patch("app.routes.speakers._embed_claim_best_effort",
                   side_effect=recording_embed):
            resp = client.post(
                f"/speaker/{speaker_id}/claims/{cid}/review",
                data={"status": "accepted"})

    assert resp.status_code == 200
    # Re-accept must trigger a re-embed
    assert len(calls) == 1, f"expected 1 embed call on re-accept, got {calls}"
    assert calls[0][0] == cid


# ---------------------------------------------------------------------------
# Finding #7: empty stream must not persist "[error: None]" to history
# ---------------------------------------------------------------------------

def test_empty_stream_does_not_persist_error_none(tmp_path, monkeypatch):
    """When the model stream yields zero tokens with no exception, the persisted
    assistant message must NOT be '[error: None]'."""
    app = _client(tmp_path, monkeypatch)

    async def empty_stream(**kw):
        return
        yield  # make it an async generator

    with TestClient(app) as client:
        speaker_id = asyncio.get_event_loop().run_until_complete(_setup(app))
        with patch("app.routes.speakers.stream_speaker_reply", side_effect=empty_stream):
            resp = client.post(
                f"/v/vs1/speaker/{speaker_id}/chat",
                data={"content": "hi"})
        assert resp.status_code == 200

        async def check():
            from app.repos import chat as chat_repo
            from app.repos import chat_threads as threads_repo
            tid = await threads_repo.get_or_create(
                app.state.db, scope="source_speaker",
                source_id="vs1", speaker_id=speaker_id)
            msgs = await chat_repo.history(app.state.db, "vs1", thread_id=tid)
            assistant_msgs = [m for m in msgs if m.role == "assistant"]
            assert assistant_msgs, "no assistant message persisted"
            for m in assistant_msgs:
                assert m.content != "[error: None]", (
                    f"persisted '[error: None]' in history: {m.content!r}"
                )
        asyncio.get_event_loop().run_until_complete(check())
