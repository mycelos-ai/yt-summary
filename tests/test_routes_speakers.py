import asyncio

from fastapi.testclient import TestClient

from app.main import create_app


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    return app, TestClient(app)


async def _seed_video(db, vid="vc1", user_id=1):
    from app.repos import videos as videos_repo
    from app.models import TranscriptSource
    # Use the real Lex Fridman channel_id so the already-seeded known_show
    # (inserted by seed_known_shows at schema-init) matches — avoids a
    # UNIQUE constraint error on (name) WHERE user_id IS NULL.
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title="Elon Musk: Mars | Lex Fridman Podcast #1",
        description="", thumbnail_path=None, duration_seconds=None, user_id=user_id,
        channel_id="UCSHZKyawb77ixDdsGog4iWA",
    )
    await videos_repo.set_transcript(db, vid, "body", TranscriptSource.MANUAL_SUBS)
    # Set a summary so the chat section (and chips) render
    await videos_repo.set_summary(db, vid, "Test summary", "test-model")
    await db.commit()


def test_detect_links_and_renders_chips(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        resp = client.post("/v/vc1/speakers/detect")
        assert resp.status_code == 200
        assert "Lex Fridman" in resp.text
        assert "Elon Musk" in resp.text


def test_manual_add_creates_chip(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        resp = client.post("/v/vc1/speakers", data={"name": "Guest Person"})
        assert resp.status_code == 200
        assert "Guest Person" in resp.text


def test_unlink_removes_chip(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers", data={"name": "Temp Person"})

        async def sid():
            from app.repos import speakers as sp_repo
            s = await sp_repo.resolve_speaker(app.state.db, user_id=1, name="Temp Person")
            return s
        speaker_id = _run(sid())
        resp = client.post(f"/v/vc1/speakers/{speaker_id}/unlink")
        assert resp.status_code == 200
        assert "Temp Person" not in resp.text


def test_detect_foreign_video_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db, vid="vforeign", user_id=999))
        resp = client.post("/v/vforeign/speakers/detect")
        assert resp.status_code == 404


def test_add_foreign_video_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db, vid="vforeign", user_id=999))
        resp = client.post("/v/vforeign/speakers", data={"name": "Anyone"})
        assert resp.status_code == 404


def test_unlink_foreign_video_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db, vid="vforeign", user_id=999))
        resp = client.post("/v/vforeign/speakers/42/unlink")
        assert resp.status_code == 404


def test_add_blank_name_rejected(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))

        # Get speaker count before
        async def count_speakers():
            from app.repos import source_speakers as ss_repo
            speakers = await ss_repo.list_for_source(app.state.db, "vc1")
            return len(speakers)

        count_before = _run(count_speakers())

        # Attempt to add blank name
        resp = client.post("/v/vc1/speakers", data={"name": "   "})
        assert resp.status_code == 400

        # Verify no speaker was created
        count_after = _run(count_speakers())
        assert count_before == count_after


def test_detail_page_shows_chips_after_detection(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers/detect")          # creates the links
        page = client.get("/v/vc1")                     # full detail page
        assert page.status_code == 200
        assert 'id="speaker-chips"' in page.text
        assert "Lex Fridman" in page.text


# ---------------------------------------------------------------------------
# Task 6 tests: speaker page + edit + photo
# ---------------------------------------------------------------------------

def test_speaker_page_renders_header_and_sources(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers/detect")

        async def sid():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(app.state.db, user_id=1, name="Lex Fridman")
        speaker_id = _run(sid())

        page = client.get(f"/speaker/{speaker_id}")
        assert page.status_code == 200
        assert "Lex Fridman" in page.text
        # confirmed source (the seeded video title) appears in the sources list
        assert "Lex Fridman Podcast" in page.text


def test_speaker_edit_updates_fields(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers", data={"name": "Editable Person"})

        async def sid():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(app.state.db, user_id=1, name="Editable Person")
        speaker_id = _run(sid())

        resp = client.post(
            f"/speaker/{speaker_id}/edit",
            data={"name": "Renamed Person", "role": "guest",
                  "avatar_id": "adult-scientist-m", "style_note": "calm"},
        )
        # TestClient follows redirects by default; we get 200 from the page
        assert resp.status_code == 200

        async def check():
            from app.repos import speakers as sp_repo
            return await sp_repo.get_speaker(app.state.db, speaker_id)
        sp = _run(check())
        assert sp.name == "Renamed Person"
        assert sp.role == "guest"
        assert sp.style_note == "calm"


def test_speaker_page_foreign_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        async def make_foreign():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(
                app.state.db, user_id=999, name="Not Yours"
            )
        speaker_id = _run(make_foreign())
        page = client.get(f"/speaker/{speaker_id}")
        assert page.status_code == 404


def test_speaker_edit_foreign_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        async def make_foreign():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(
                app.state.db, user_id=999, name="Foreign Speaker"
            )
        speaker_id = _run(make_foreign())
        resp = client.post(
            f"/speaker/{speaker_id}/edit",
            data={"name": "Hacked", "role": "", "avatar_id": "", "style_note": ""},
        )
        assert resp.status_code == 404


def test_photo_upload_and_serve(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers", data={"name": "Photo Person"})

        async def sid():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(app.state.db, user_id=1, name="Photo Person")
        speaker_id = _run(sid())

        # Upload a tiny valid PNG (1x1 white pixel)
        tiny_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
            b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
            b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        resp = client.post(
            f"/speaker/{speaker_id}/edit",
            data={"name": "Photo Person", "role": "", "avatar_id": "", "style_note": ""},
            files={"photo": ("test.png", tiny_png, "image/png")},
        )
        assert resp.status_code == 200

        photo_resp = client.get(f"/speaker/{speaker_id}/photo")
        assert photo_resp.status_code == 200
        assert photo_resp.headers["content-type"].startswith("image/")


def test_non_image_upload_rejected(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers", data={"name": "Bad Upload Person"})

        async def sid():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(app.state.db, user_id=1, name="Bad Upload Person")
        speaker_id = _run(sid())

        resp = client.post(
            f"/speaker/{speaker_id}/edit",
            data={"name": "Bad Upload Person", "role": "", "avatar_id": "", "style_note": ""},
            files={"photo": ("evil.txt", b"not an image", "text/plain")},
        )
        assert resp.status_code == 400


def test_photo_serve_no_photo_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers", data={"name": "No Photo Person"})

        async def sid():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(app.state.db, user_id=1, name="No Photo Person")
        speaker_id = _run(sid())

        resp = client.get(f"/speaker/{speaker_id}/photo")
        assert resp.status_code == 404


def test_photo_serve_foreign_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        async def make_foreign():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(
                app.state.db, user_id=999, name="Foreign Photo Speaker"
            )
        speaker_id = _run(make_foreign())
        resp = client.get(f"/speaker/{speaker_id}/photo")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Task 7 tests: activate/deactivate (flip is_active flag)
# ---------------------------------------------------------------------------

def test_activate_flips_flag(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers", data={"name": "Activatable"})

        async def sid():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(app.state.db, user_id=1, name="Activatable")
        speaker_id = _run(sid())

        resp = client.post(f"/speaker/{speaker_id}/activate")
        assert resp.status_code == 200

        async def check(expected):
            from app.repos import speakers as sp_repo
            sp = await sp_repo.get_speaker(app.state.db, speaker_id)
            assert sp.is_active is expected
        _run(check(True))

        client.post(f"/speaker/{speaker_id}/deactivate")
        _run(check(False))


def test_activate_foreign_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        async def make_foreign():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(
                app.state.db, user_id=999, name="Foreign Activatable"
            )
        speaker_id = _run(make_foreign())
        resp = client.post(f"/speaker/{speaker_id}/activate")
        assert resp.status_code == 404


def test_deactivate_foreign_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        async def make_foreign():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(
                app.state.db, user_id=999, name="Foreign Deactivatable"
            )
        speaker_id = _run(make_foreign())
        resp = client.post(f"/speaker/{speaker_id}/deactivate")
        assert resp.status_code == 404


def test_activate_from_page_returns_self_reproducing_fragment(tmp_path, monkeypatch):
    """Regression test: fragment must carry ?caller=page so second toggle works.

    Bug: first toggle from speaker page worked, but the returned fragment's button
    was missing ?caller=page, so the second toggle fell through to chip-panel logic
    and returned a <span class="speaker-chip">, breaking the UI.

    Fix: _speaker_actions.html buttons carry ?caller=page so the fragment reproduces
    its own caller context on every swap.
    """
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers", data={"name": "Toggle Tester"})

        async def sid():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(app.state.db, user_id=1, name="Toggle Tester")
        speaker_id = _run(sid())

        # First toggle: POST activate with ?caller=page (simulating the speaker page)
        resp1 = client.post(f"/speaker/{speaker_id}/activate?caller=page")
        assert resp1.status_code == 200
        # Must return _speaker_actions.html fragment (page-style), not chip span
        assert "speaker-actions" in resp1.text or "Deactivate" in resp1.text
        assert "speaker-chip" not in resp1.text
        # Crucially, the returned fragment must ALSO carry ?caller=page
        assert "?caller=page" in resp1.text

        # Second toggle: POST deactivate with ?caller=page (from swapped fragment)
        resp2 = client.post(f"/speaker/{speaker_id}/deactivate?caller=page")
        assert resp2.status_code == 200
        # Must still return page-actions fragment, not chip span
        assert "speaker-actions" in resp2.text or "Activate" in resp2.text
        assert "speaker-chip" not in resp2.text
        # Fragment must again carry ?caller=page (loop is stable)
        assert "?caller=page" in resp2.text


def test_activate_from_chips_returns_full_strip_with_unlink(tmp_path, monkeypatch):
    """Activating from chip strip returns full strip, preserving unlink button.

    Bug: After activating a chip inline, that chip can no longer be unlinked
    until a full page reload (the returned _speaker_chip_panel.html has no video
    context to render the unlink button).

    Fix: When activated via caller=chips&video_id=<vid>, return the full
    _speaker_chips.html strip (which has video context) instead of the
    single-chip panel.
    """
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers", data={"name": "Chip Activatable"})

        async def sid():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(app.state.db, user_id=1, name="Chip Activatable")
        speaker_id = _run(sid())

        # Activate via chip strip: POST with caller=chips&video_id=vc1
        resp = client.post(f"/speaker/{speaker_id}/activate?caller=chips&video_id=vc1")
        assert resp.status_code == 200
        # Must return the full chip strip (id="speaker-chips")
        assert 'id="speaker-chips"' in resp.text
        # Must contain the unlink button (targets /speakers/{id}/unlink)
        assert f"/speakers/{speaker_id}/unlink" in resp.text
        # Must NOT return just a single-chip panel (no chip-panel-specific marker)
        # (the panel has different structure than the strip)


# ---------------------------------------------------------------------------
# Seed photo URL routing tests (Part C): relative = /static/, absolute = /photo
# ---------------------------------------------------------------------------

def test_speaker_page_renders_static_photo_url(tmp_path, monkeypatch):
    """Speaker with a relative avatar_photo_path (seed photo) must render as
    /static/{path}, NOT as /speaker/{id}/photo."""
    app, client = _client(tmp_path, monkeypatch)
    with client:
        async def setup():
            from app.repos import speakers as sp_repo
            sp_id = await sp_repo.resolve_speaker(app.state.db, user_id=1, name="Static Photo Speaker")
            await app.state.db.execute(
                "UPDATE speakers SET avatar_photo_path=? WHERE id=?",
                ("podcasters/chamath-palihapitiya.png", sp_id),
            )
            await app.state.db.commit()
            return sp_id
        speaker_id = _run(setup())

        page = client.get(f"/speaker/{speaker_id}")
        assert page.status_code == 200
        assert 'src="/static/podcasters/chamath-palihapitiya.png"' in page.text, (
            "Relative seed photo must render as /static/... URL"
        )
        assert f'src="/speaker/{speaker_id}/photo"' not in page.text, (
            "Relative seed photo must NOT use the /speaker/{id}/photo route"
        )


def test_speaker_page_uploaded_photo_uses_photo_route(tmp_path, monkeypatch):
    """Speaker with an absolute avatar_photo_path (uploaded file) must render
    via /speaker/{id}/photo, NOT /static/."""
    app, client = _client(tmp_path, monkeypatch)
    with client:
        async def setup():
            from app.repos import speakers as sp_repo
            sp_id = await sp_repo.resolve_speaker(app.state.db, user_id=1, name="Upload Photo Speaker")
            await app.state.db.execute(
                "UPDATE speakers SET avatar_photo_path=? WHERE id=?",
                ("/data/speaker_photos/99.png", sp_id),
            )
            await app.state.db.commit()
            return sp_id
        speaker_id = _run(setup())

        page = client.get(f"/speaker/{speaker_id}")
        assert page.status_code == 200
        assert f'src="/speaker/{speaker_id}/photo"' in page.text, (
            "Absolute upload path must use /speaker/{id}/photo route"
        )
        assert '/static/data/' not in page.text, (
            "Absolute upload path must NOT render as /static/..."
        )


def test_activate_from_chips_foreign_video_is_404(tmp_path, monkeypatch):
    """Activating from chip strip with foreign video_id returns 404."""
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers", data={"name": "Chip Activatable 2"})

        async def sid():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(app.state.db, user_id=1, name="Chip Activatable 2")
        speaker_id = _run(sid())

        # Activate with foreign video_id
        resp = client.post(f"/speaker/{speaker_id}/activate?caller=chips&video_id=vforeign")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Wire-up tests: chip jump carries video context → speaker page (Part 1 + 2)
# ---------------------------------------------------------------------------

async def _seed_speaker_linked(db, vid="vc1", user_id=1, speaker_name="Alice"):
    """Seed a video + an active linked speaker. Returns speaker_id."""
    from app.repos import source_speakers as ss_repo
    from app.repos import speakers as sp_repo
    await _seed_video(db, vid=vid, user_id=user_id)
    speaker_id = await sp_repo.resolve_speaker(db, user_id=user_id, name=speaker_name)
    await sp_repo.set_active(db, speaker_id, True)
    await ss_repo.link_speaker(db, vid, speaker_id, detection_source="manual")
    await db.commit()
    return speaker_id


def test_speaker_page_with_video_id_posts_to_video_route(tmp_path, monkeypatch):
    """GET /speaker/{id}?video_id={vid} → composer posts to /v/{vid}/speaker/{id}/chat
    and a context note with the video title is shown."""
    app, client = _client(tmp_path, monkeypatch)
    with client:
        speaker_id = _run(_seed_speaker_linked(app.state.db))
        resp = client.get(f"/speaker/{speaker_id}?video_id=vc1")
        assert resp.status_code == 200
        body = resp.text
        # composer must target the video-context route
        assert f"/v/vc1/speaker/{speaker_id}/chat" in body
        # and must NOT target the generic dossier route (only)
        assert f'hx-post="/speaker/{speaker_id}/chat"' not in body
        # context note with video title
        assert "Test summary" not in body or "Lex Fridman" in body
        assert "chat-context-note" in body


def test_speaker_page_without_video_id_unchanged(tmp_path, monkeypatch):
    """GET /speaker/{id} (no query) → composer still posts to /speaker/{id}/chat;
    no context note rendered."""
    app, client = _client(tmp_path, monkeypatch)
    with client:
        speaker_id = _run(_seed_speaker_linked(app.state.db))
        resp = client.get(f"/speaker/{speaker_id}")
        assert resp.status_code == 200
        body = resp.text
        # generic dossier route
        assert f'hx-post="/speaker/{speaker_id}/chat"' in body
        # no video-context route
        assert "/v/vc1/speaker/" not in body
        # no context note
        assert "chat-context-note" not in body


def test_speaker_page_stale_video_id_degrades(tmp_path, monkeypatch):
    """GET /speaker/{id}?video_id=does-not-exist → 200, falls back to generic
    dossier composer (no 404)."""
    app, client = _client(tmp_path, monkeypatch)
    with client:
        speaker_id = _run(_seed_speaker_linked(app.state.db))
        resp = client.get(f"/speaker/{speaker_id}?video_id=does-not-exist")
        assert resp.status_code == 200
        body = resp.text
        # falls back to generic route
        assert f'hx-post="/speaker/{speaker_id}/chat"' in body
        # no context note
        assert "chat-context-note" not in body


def test_active_chip_links_with_video_id(tmp_path, monkeypatch):
    """An active linked speaker chip's name link must include ?video_id={vid}."""
    app, client = _client(tmp_path, monkeypatch)
    with client:
        speaker_id = _run(_seed_speaker_linked(app.state.db))
        # GET the full video detail page which includes the chips template
        resp = client.get("/v/vc1")
        assert resp.status_code == 200
        assert f"/speaker/{speaker_id}?video_id=vc1" in resp.text


def test_video_detail_has_chat_or_divider(tmp_path, monkeypatch):
    """A video detail page with summary + linked speaker must contain
    the chat-or-divider element."""
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_speaker_linked(app.state.db))
        resp = client.get("/v/vc1")
        assert resp.status_code == 200
        assert "chat-or-divider" in resp.text
