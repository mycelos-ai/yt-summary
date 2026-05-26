from unittest.mock import AsyncMock

import pytest

from app.models import FeedbackSource, Sentiment
from app.repos import feedback as feedback_repo
from app.repos import users as users_repo
from app.repos import videos as videos_repo
from app.services import interest_profile as profile_service


async def _video(db) -> None:
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )


@pytest.mark.asyncio
async def test_consolidate_builds_profile_from_first_feedback(db, monkeypatch):
    await _video(db)
    await feedback_repo.create(
        db, user_id=1, video_id="v1", source=FeedbackSource.SUMMARY,
        selected_text="caching reduces cost by 3x",
        text_offset_start=0, text_offset_end=27,
        sentiment=Sentiment.INTERESTING, comment=None,
    )
    fake_llm = AsyncMock(return_value="- Cares about LLM cost optimization")
    monkeypatch.setattr(profile_service, "_call_consolidate_llm", fake_llm)

    await profile_service.consolidate(db, user_id=1)

    md, version = await users_repo.get_interest_profile(db, user_id=1)
    assert md == "- Cares about LLM cost optimization"
    assert version == 1
    fake_llm.assert_called_once()


@pytest.mark.asyncio
async def test_consolidate_merges_with_existing_profile(db, monkeypatch):
    await _video(db)
    await users_repo.set_interest_profile(
        db, user_id=1, markdown="- Old interest", expected_version=0,
    )
    await feedback_repo.create(
        db, user_id=1, video_id="v1", source=FeedbackSource.SUMMARY,
        selected_text="new topic", text_offset_start=0, text_offset_end=9,
        sentiment=Sentiment.INTERESTING, comment="really cool",
    )
    fake_llm = AsyncMock(
        return_value="- Old interest\n- Cares about new topic"
    )
    monkeypatch.setattr(profile_service, "_call_consolidate_llm", fake_llm)

    await profile_service.consolidate(db, user_id=1)

    md, version = await users_repo.get_interest_profile(db, user_id=1)
    assert "new topic" in md
    assert version == 2


@pytest.mark.asyncio
async def test_consolidate_skips_when_no_feedback(db, monkeypatch):
    fake_llm = AsyncMock()
    monkeypatch.setattr(profile_service, "_call_consolidate_llm", fake_llm)

    await profile_service.consolidate(db, user_id=1)

    fake_llm.assert_not_called()


@pytest.mark.asyncio
async def test_consolidate_failure_leaves_profile_unchanged(db, monkeypatch):
    await _video(db)
    await users_repo.set_interest_profile(
        db, user_id=1, markdown="stable", expected_version=0,
    )
    await feedback_repo.create(
        db, user_id=1, video_id="v1", source=FeedbackSource.SUMMARY,
        selected_text="x", text_offset_start=0, text_offset_end=1,
        sentiment=Sentiment.INTERESTING, comment=None,
    )
    fake_llm = AsyncMock(side_effect=RuntimeError("LLM down"))
    monkeypatch.setattr(profile_service, "_call_consolidate_llm", fake_llm)

    await profile_service.consolidate(db, user_id=1)

    md, version = await users_repo.get_interest_profile(db, user_id=1)
    assert md == "stable"
    assert version == 1  # unchanged from the manual set above


@pytest.mark.asyncio
async def test_rebuild_from_all_feedback_resets_profile(db, monkeypatch):
    await _video(db)
    await users_repo.set_interest_profile(
        db, user_id=1, markdown="stale profile", expected_version=0,
    )
    await feedback_repo.create(
        db, user_id=1, video_id="v1", source=FeedbackSource.SUMMARY,
        selected_text="fresh signal",
        text_offset_start=0, text_offset_end=12,
        sentiment=Sentiment.INTERESTING, comment=None,
    )
    fake_llm = AsyncMock(return_value="- fresh signal noted")
    monkeypatch.setattr(profile_service, "_call_consolidate_llm", fake_llm)

    await profile_service.rebuild(db, user_id=1)

    md, _ = await users_repo.get_interest_profile(db, user_id=1)
    assert md == "- fresh signal noted"
    # The first arg to the LLM stub should have been an empty profile.
    call_kwargs = fake_llm.call_args.kwargs
    assert call_kwargs["current_profile"] == ""
