"""HTMX modal + render/status/delete/file routes for TTS audio.

The tests follow the established sync TestClient + run_until_complete
pattern from ``tests/test_routes_videos.py`` / ``tests/test_routes_chat.py``.
``_seed_*`` helpers run on the same event loop as the TestClient
lifespan, so ``app.state.db`` is populated by the time we touch it.
"""
import asyncio

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import TranscriptSource


def _await(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _seed_video(app, *, id: str, source_language: str | None = None):
    """Insert a video owned by user 1 (matches default cookie)."""
    from app.repos import videos as videos_repo

    async def setup():
        await videos_repo.upsert_metadata(
            app.state.db,
            video_id=id,
            url=f"https://yt/{id}",
            title="T",
            description="",
            thumbnail_path=None,
            duration_seconds=60,
        )
        if source_language is not None:
            # set_transcript with `language=...` also writes source_language
            # per the Task 7 migration semantics — exactly what the modal
            # uses as the default target_language fallback.
            await videos_repo.set_transcript(
                app.state.db, id, "x", TranscriptSource.AUTO_SUBS,
                language=source_language,
            )

    _await(setup())


def _seed_summary(app, *, video_id: str, text: str, language: str | None = None):
    from app.repos import videos as videos_repo

    async def setup():
        await videos_repo.set_summary(
            app.state.db, video_id, text, "gpt-4o", language=language,
        )

    _await(setup())


def _seed_tts_job(
    app,
    *,
    video_id: str,
    source: str,
    target_language: str,
    voice: str,
    quality: str,
    status: str = "queued",
    step: str | None = None,
    audio_path: str | None = None,
) -> int:
    """Raw SQL insert so we can pre-stage rows in non-queued states
    (the repo's enqueue only knows how to land at 'queued')."""
    async def setup():
        cursor = await app.state.db.execute(
            """
            INSERT INTO tts_jobs (
                video_id, source, target_language, voice, quality,
                status, step, audio_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (video_id, source, target_language, voice, quality,
             status, step, audio_path),
        )
        await app.state.db.commit()
        return cursor.lastrowid

    return _await(setup())


# -------------------------------------------------------------------- tests


def test_audio_modal_renders_form_for_video(tmp_path, monkeypatch):
    """GET /v/{id}/audio returns the modal form with voice options."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en")
        resp = client.get("/v/abc/audio")
    assert resp.status_code == 200
    assert "thorsten" in resp.text  # de voice available in catalogue
    assert "lessac" in resp.text    # en voice available
    assert "<select" in resp.text


def test_render_endpoint_enqueues_job_returns_polling_block(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en")
        _seed_summary(app, video_id="abc", text="Hi.", language="en")
        resp = client.post(
            "/v/abc/audio/render",
            data={"source": "summary", "target_language": "de",
                  "voice": "thorsten", "quality": "medium"},
            follow_redirects=False,
        )
    assert resp.status_code == 200
    # Response is the polling fragment
    assert "every 2s" in resp.text
    # The fresh job is in 'queued' (worker may not have claimed yet)
    # or 'translating' (if it did). Either is fine.
    body_lower = resp.text.lower()
    assert "queued" in body_lower or "translating" in body_lower or "preparing" in body_lower


def test_render_endpoint_returns_cached_done_block_when_repeated(tmp_path, monkeypatch):
    """Second submit with the same params should return the existing
    done block immediately, with the audio player."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en")
        _seed_summary(app, video_id="abc", text="Hi.", language="en")
        # Pre-fabricate a done job. enqueue's UPSERT conflict clause
        # will return this existing row rather than inserting a new one.
        _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten",
            quality="medium", status="done",
            audio_path="tts-audio/abc/summary-de-thorsten-medium.mp3",
        )
        resp = client.post(
            "/v/abc/audio/render",
            data={"source": "summary", "target_language": "de",
                  "voice": "thorsten", "quality": "medium"},
        )
    assert "<audio" in resp.text
    assert "/audio/file/" in resp.text  # download link present


def test_status_endpoint_returns_current_step(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc")
        # 'rendering' is the in-progress status that holds a step label —
        # the schema only allows queued/translating/rendering/done/failed.
        job_id = _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten", quality="medium",
            status="rendering", step="rendering audio",
        )
        resp = client.get(f"/v/abc/audio/status/{job_id}")
    assert resp.status_code == 200
    assert "rendering audio" in resp.text
    # Still polling
    assert "every 2s" in resp.text


def test_status_endpoint_stops_polling_when_done(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc")
        job_id = _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten", quality="medium",
            status="done",
            audio_path="tts-audio/abc/summary-de-thorsten-medium.mp3",
        )
        resp = client.get(f"/v/abc/audio/status/{job_id}")
    assert "<audio" in resp.text
    assert "every 2s" not in resp.text


def test_delete_rendering_removes_row_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    rel = "tts-audio/abc/summary-de-thorsten-medium.mp3"
    mp3 = tmp_path / rel
    mp3.parent.mkdir(parents=True)
    mp3.write_bytes(b"x")
    with TestClient(app) as client:
        _seed_video(app, id="abc")
        job_id = _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten", quality="medium",
            status="done", audio_path=rel,
        )
        resp = client.post(
            f"/v/abc/audio/{job_id}/delete", follow_redirects=False,
        )
    assert resp.status_code == 200
    assert not mp3.exists()


def test_audio_renderings_endpoint_returns_block(tmp_path, monkeypatch):
    """GET /v/{id}/audio/renderings returns the persistent list block
    with the <audio> element and delete button for a done job."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc")
        _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten", quality="medium",
            status="done",
            audio_path="tts-audio/abc/summary-de-thorsten-medium.mp3",
        )
        resp = client.get("/v/abc/audio/renderings")
    assert resp.status_code == 200
    assert "<audio" in resp.text
    assert "Delete" in resp.text
    # The block wraps itself with the same hx-trigger so subsequent
    # render completions keep refreshing it.
    assert "audio:rendered" in resp.text


def test_render_endpoint_emits_audio_rendered_event_when_cached_done(
    tmp_path, monkeypatch,
):
    """When the modal returns the done view (cached done case),
    HX-Trigger: audio:rendered fires so the renderings list refreshes."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en")
        _seed_summary(app, video_id="abc", text="Hi.", language="en")
        _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten",
            quality="medium", status="done",
            audio_path="tts-audio/abc/summary-de-thorsten-medium.mp3",
        )
        resp = client.post(
            "/v/abc/audio/render",
            data={"source": "summary", "target_language": "de",
                  "voice": "thorsten", "quality": "medium"},
        )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Trigger") == "audio:rendered"


def test_status_endpoint_emits_audio_rendered_event_when_done(
    tmp_path, monkeypatch,
):
    """A polling tick that lands on a `done` job emits HX-Trigger so
    the renderings block re-fetches itself."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc")
        job_id = _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten", quality="medium",
            status="done",
            audio_path="tts-audio/abc/summary-de-thorsten-medium.mp3",
        )
        resp = client.get(f"/v/abc/audio/status/{job_id}")
    assert resp.status_code == 200
    assert resp.headers.get("HX-Trigger") == "audio:rendered"


def test_status_endpoint_no_event_while_progress(tmp_path, monkeypatch):
    """While the job is still progressing, no HX-Trigger fires (the
    persistent list shouldn't refresh until the job is done)."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc")
        job_id = _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten", quality="medium",
            status="rendering", step="rendering audio",
        )
        resp = client.get(f"/v/abc/audio/status/{job_id}")
    assert resp.status_code == 200
    assert "HX-Trigger" not in resp.headers


def test_audio_modal_shows_source_language_select(tmp_path, monkeypatch):
    """GET /v/{id}/audio renders the new source-language select with
    an 'auto' option that's selected when the video's source_language
    is NULL."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc")  # no source_language seeded
        # Manually set summary so the form can render (we don't rely
        # on set_transcript here, which would also stamp source_language).
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.set_summary(
                app.state.db, "abc", "Hi.", "gpt-4o", language=None,
            )
        _await(setup())
        resp = client.get("/v/abc/audio")
    assert resp.status_code == 200
    # The new select is present and 'auto' is one of its options.
    assert 'name="source_language"' in resp.text
    assert 'value="auto"' in resp.text
    # When source_language is NULL the hint should be shown so the
    # user knows why translation might silently no-op otherwise.
    assert "pre-dates language auto-detection" in resp.text


def test_render_endpoint_persists_explicit_source_language(tmp_path, monkeypatch):
    """User picks an explicit source_language on a video where it's NULL.
    After the POST, video.source_language should be populated so the worker
    picks it up and translates correctly."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        # Seed video WITHOUT source_language (the broken-pre-Task-7 shape).
        _seed_video(app, id="abc")
        # Set a summary directly so we don't stamp source_language via
        # set_transcript's language= path.
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.set_summary(
                app.state.db, "abc", "Hello.", "gpt-4o", language=None,
            )
        _await(setup())
        resp = client.post(
            "/v/abc/audio/render",
            data={
                "source": "summary",
                "source_language": "en_US",
                "target_language": "de",
                "voice": "thorsten",
                "quality": "medium",
            },
        )
        assert resp.status_code == 200
        # The video's source_language column should now be populated.
        async def check():
            from app.repos import videos as videos_repo
            v = await videos_repo.get(app.state.db, "abc")
            return v.source_language
        assert _await(check()) == "en_US"


def test_render_endpoint_400_on_invalid_source_language(tmp_path, monkeypatch):
    """source_language not in catalogue → 400."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc")
        _seed_summary(app, video_id="abc", text="Hi.", language="en")
        resp = client.post(
            "/v/abc/audio/render",
            data={
                "source": "summary",
                "source_language": "xx",
                "target_language": "de",
                "voice": "thorsten",
                "quality": "medium",
            },
        )
    assert resp.status_code == 400


def test_render_endpoint_does_not_overwrite_existing_source_language(
    tmp_path, monkeypatch,
):
    """If source_language is already known, an explicit pick must NOT
    silently overwrite it (set_source_language is NULL-only)."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        # Seed with source_language='de' already set.
        _seed_video(app, id="abc", source_language="de")
        _seed_summary(app, video_id="abc", text="Hallo.", language="de")
        resp = client.post(
            "/v/abc/audio/render",
            data={
                "source": "summary",
                "source_language": "en_US",  # would-be overwrite
                "target_language": "de",
                "voice": "thorsten",
                "quality": "medium",
            },
        )
        assert resp.status_code == 200
        async def check():
            from app.repos import videos as videos_repo
            v = await videos_repo.get(app.state.db, "abc")
            return v.source_language
        assert _await(check()) == "de"


def test_audio_modal_preselects_source_from_query_param(tmp_path, monkeypatch):
    """GET /v/{id}/audio?source=summary returns the form with a hidden
    input for source and no Source <select> dropdown — the inline
    Audio button on the Summary heading already told us what to render."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en_US")
        _seed_summary(app, video_id="abc", text="Hi.", language="en_US")
        resp = client.get("/v/abc/audio?source=summary")
    assert resp.status_code == 200
    # Hidden input carries the source, the Source <select> is gone.
    assert '<input type="hidden" name="source" value="summary"' in resp.text
    assert '<select name="source"' not in resp.text
    # The friendly hint replaces the dropdown.
    assert "Generating audio from:" in resp.text
    assert "Summary" in resp.text


def test_audio_modal_preselects_transcript_source(tmp_path, monkeypatch):
    """Same as above but for ?source=transcript."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en_US")
        resp = client.get("/v/abc/audio?source=transcript")
    assert resp.status_code == 200
    assert '<input type="hidden" name="source" value="transcript"' in resp.text
    assert "Generating audio from:" in resp.text


def test_audio_modal_ignores_unknown_source_query_param(tmp_path, monkeypatch):
    """An unknown ?source= value falls back to the full Source select —
    defensive against URL tampering or future bugs in the caller."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en_US")
        _seed_summary(app, video_id="abc", text="Hi.", language="en_US")
        resp = client.get("/v/abc/audio?source=bogus")
    assert resp.status_code == 200
    # No hidden input, the full select fallback is rendered instead.
    assert '<input type="hidden" name="source"' not in resp.text
    assert '<select name="source"' in resp.text


def test_audio_modal_hides_source_language_select_when_detected(
    tmp_path, monkeypatch,
):
    """When the video's source_language column is populated, the form
    shows a static 'Source language: English (US)' line instead of
    the select — there's nothing for the user to pick."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en_US")
        resp = client.get("/v/abc/audio")
    assert resp.status_code == 200
    # The select is gone; the static label is present with the friendly
    # human label (not the raw 'en_US' code).
    assert '<select name="source_language"' not in resp.text
    assert "Source language:" in resp.text
    assert "English (US)" in resp.text


def test_audio_modal_shows_source_language_select_when_null(
    tmp_path, monkeypatch,
):
    """Conversely: a NULL source_language renders the full select with
    'auto' as the default, plus the hint explaining the pick will be
    persisted. Covers the pre-Task-7 video case."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc")  # no source_language
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.set_summary(
                app.state.db, "abc", "Hi.", "gpt-4o", language=None,
            )
        _await(setup())
        resp = client.get("/v/abc/audio")
    assert resp.status_code == 200
    assert '<select name="source_language"' in resp.text
    assert 'value="auto"' in resp.text
    assert "pre-dates language auto-detection" in resp.text


def test_audio_file_endpoint_serves_mp3(tmp_path, monkeypatch):
    """GET /v/{id}/audio/file/{job_id} → 200, content-type audio/mpeg."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    rel = "tts-audio/abc/summary-de-thorsten-medium.mp3"
    mp3 = tmp_path / rel
    mp3.parent.mkdir(parents=True)
    mp3.write_bytes(b"ID3FAKE")
    with TestClient(app) as client:
        _seed_video(app, id="abc")
        job_id = _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten", quality="medium",
            status="done", audio_path=rel,
        )
        resp = client.get(f"/v/abc/audio/file/{job_id}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/")
    assert resp.content == b"ID3FAKE"


def test_audio_modal_opens_via_dialog_element(tmp_path, monkeypatch):
    """The video detail page hosts a native <dialog id="audio-modal">
    with a `#audio-modal-content` div inside; the inline 🔊 Audio
    buttons target that inner div so HTMX's afterSwap hook can call
    `.showModal()` on the surrounding dialog. This keeps the modal
    centered in the viewport regardless of scroll position."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en_US")
        _seed_summary(app, video_id="abc", text="Hi.", language="en_US")
        resp = client.get("/v/abc")
    assert resp.status_code == 200
    # The dialog and its content slot are both present.
    assert '<dialog id="audio-modal"' in resp.text
    assert 'id="audio-modal-content"' in resp.text
    # The Summary section's 🔊 Audio button targets the inner div so
    # the surrounding dialog can wrap whatever HTMX injects.
    assert 'hx-target="#audio-modal-content"' in resp.text
    # The bare div target from the prior commit must be gone; if it
    # came back the JS hook below would never fire.
    assert 'class="audio-modal-target"' not in resp.text


def test_audio_modal_shows_existing_renderings_banner(tmp_path, monkeypatch):
    """When there are done renderings for this video, the modal form
    shows a banner indicating how many exist."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en_US")
        _seed_summary(app, video_id="abc", text="Hi.", language="en_US")
        # Two done rows — different voices so the unique constraint is
        # happy. The banner shows the count regardless of variant.
        _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten", quality="medium",
            status="done",
            audio_path="tts-audio/abc/summary-de-thorsten-medium.mp3",
        )
        _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="en_US", voice="lessac", quality="medium",
            status="done",
            audio_path="tts-audio/abc/summary-en_US-lessac-medium.mp3",
        )
        resp = client.get("/v/abc/audio")
    assert resp.status_code == 200
    assert "2</strong>" in resp.text  # the count strongly emphasised
    assert "existing rendering" in resp.text
    # The banner is plural for >1.
    assert "renderings" in resp.text


def test_audio_modal_embeds_cached_keys_for_live_badge(tmp_path, monkeypatch):
    """The form includes a JSON script tag with the cached (source,
    target, voice, quality) tuples for the JS to use."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en_US")
        _seed_summary(app, video_id="abc", text="Hi.", language="en_US")
        _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten", quality="medium",
            status="done",
            audio_path="tts-audio/abc/summary-de-thorsten-medium.mp3",
        )
        resp = client.get("/v/abc/audio")
    assert resp.status_code == 200
    # The script tag holding the cache keys is present and parseable.
    import json as _json
    import re
    m = re.search(
        r'<script type="application/json" id="audio-cached-keys">'
        r'(.*?)</script>',
        resp.text, re.DOTALL,
    )
    assert m is not None
    keys = _json.loads(m.group(1))
    assert keys == [
        {
            "source": "summary",
            "target_language": "de",
            "voice": "thorsten",
            "quality": "medium",
        }
    ]


def test_audio_modal_no_banner_when_no_renderings(tmp_path, monkeypatch):
    """If there are no done renderings yet, the banner is omitted."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en_US")
        _seed_summary(app, video_id="abc", text="Hi.", language="en_US")
        resp = client.get("/v/abc/audio")
    assert resp.status_code == 200
    assert "audio-existing-banner" not in resp.text
    # The script tag is always present (empty list when nothing cached).
    assert 'id="audio-cached-keys"' in resp.text


def test_audio_modal_form_uses_flag_prefixed_language_labels(
    tmp_path, monkeypatch,
):
    """Language dropdown labels are prefixed with country-flag emojis
    (Unicode, no asset work). Tests both the language <option>s and
    the static "Detected: …" hint for a video with a known source
    language pick up the same flag."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="de")
        resp = client.get("/v/abc/audio")
    assert resp.status_code == 200
    # German label gets the German flag in both the target-language
    # dropdown and the detected source-language hint.
    assert "🇩🇪 German" in resp.text
    # English options carry the matching flags too.
    assert "🇺🇸 English (US)" in resp.text
    assert "🇬🇧 English (UK)" in resp.text
