# tests/test_pipeline_piggyback.py
import asyncio
from unittest.mock import AsyncMock, patch


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


async def _seed_video_with_speakers(db, *, active):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title, transcript) "
        "VALUES ('vp1', 1, 'youtube', 'u', 'Ep', 'body')")
    cur = await db.execute(
        "INSERT INTO speakers (user_id, name, name_key, is_active) VALUES (1,'C','c',?)",
        (1 if active else 0,))
    sid = cur.lastrowid
    await db.execute(
        "INSERT INTO source_speakers (source_id, speaker_id, detection_source) "
        "VALUES ('vp1', ?, 'show_rule')", (sid,))
    await db.commit()
    from app.repos import videos as videos_repo
    return sid, await videos_repo.get(db, "vp1")


def test_piggyback_extracts_when_active_speaker_present(db):
    async def go():
        from app import pipeline
        sid, video = await _seed_video_with_speakers(db, active=True)
        called = {}

        async def fake_extract(db_, source, speaker_ids, *, model, api_key, base_url):
            called["speaker_ids"] = speaker_ids
            called["model"] = model
            return []

        with patch("app.pipeline.extract_claims_for_source", side_effect=fake_extract):
            await pipeline._extract_active_speaker_claims(
                db, video, model="m", api_key="k", base_url=None)
        assert called["speaker_ids"] == [sid]
        assert called["model"] == "m"
    _run(go())


def test_piggyback_skips_when_no_active_speaker(db):
    async def go():
        from app import pipeline
        _sid, video = await _seed_video_with_speakers(db, active=False)
        extract = AsyncMock(return_value=[])
        with patch("app.pipeline.extract_claims_for_source", extract):
            await pipeline._extract_active_speaker_claims(
                db, video, model="m", api_key="k", base_url=None)
        extract.assert_not_called()  # no expensive call when nobody is active
    _run(go())


def test_piggyback_never_raises(db):
    async def go():
        from app import pipeline
        _sid, video = await _seed_video_with_speakers(db, active=True)
        with patch("app.pipeline.extract_claims_for_source",
                   AsyncMock(side_effect=RuntimeError("boom"))):
            # must swallow — pipeline integrity over enrichment
            await pipeline._extract_active_speaker_claims(
                db, video, model="m", api_key="k", base_url=None)
    _run(go())
