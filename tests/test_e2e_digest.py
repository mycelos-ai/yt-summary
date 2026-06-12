"""End-to-end smoke: ingest a (mocked) video → feedback → consolidate → digest.

LLM is fully mocked. Walks the public flow at the service level so any
wiring break shows up here even if unit tests pass.
"""
import json
from unittest.mock import AsyncMock

import pytest

from app.models import FeedbackSource, Sentiment
from app.repos import feedback as feedback_repo
from app.repos import llm_models as llm_models_repo
from app.repos import users as users_repo
from app.repos import videos as videos_repo
from app.services import digest as digest_service
from app.services import interest_profile as profile_service


@pytest.mark.asyncio
async def test_full_loop_ingest_feedback_consolidate_digest(
    db, monkeypatch,
):
    # Seed a default LLM so consolidate() + generate() pass the
    # no-LLM-configured guard. Tests still mock the actual call seam.
    await llm_models_repo.insert(
        db, label="Test", provider_id="openai", model="openai/gpt-4o",
        api_key="key", base_url="", make_default=True,
    )

    # 1) Two summarised items land in the DB with highlights.
    for vid, hl in [
        ("v1", [{"text": "LLM caching reduces cost 3x", "rank": 1, "reason": "novel"}]),
        ("v2", [{"text": "Filler hardware news", "rank": 4, "reason": "minor"}]),
    ]:
        await videos_repo.upsert_metadata(
            db, video_id=vid, url=f"u/{vid}", title=f"Title {vid}",
            description="", thumbnail_path=None, duration_seconds=None,
        )
        await videos_repo.set_highlights(db, vid, json.dumps(hl))

    # 2) Profile gives positive feedback on v1's summary.
    await feedback_repo.create(
        db, user_id=1, video_id="v1", source=FeedbackSource.SUMMARY,
        selected_text="LLM caching reduces cost 3x",
        text_offset_start=0, text_offset_end=27,
        sentiment=Sentiment.INTERESTING, comment="exactly my interest",
    )

    # 3) Consolidate runs (LLM mocked).
    monkeypatch.setattr(
        profile_service, "_call_consolidate_llm",
        AsyncMock(return_value="- Cares about LLM cost optimization"),
    )
    await profile_service.consolidate(db, user_id=1)

    md, _ = await users_repo.get_interest_profile(db, user_id=1)
    assert md is not None
    assert "LLM cost" in md

    # 4) Digest generation (LLM mocked to honour the profile).
    monkeypatch.setattr(
        digest_service, "_call_digest_llm",
        AsyncMock(return_value=json.dumps({
            "tldr": "LLM cost optimization continues to dominate.",
            "top_items": [
                {"video_id": "v1", "rank": 1,
                 "hook": "Caching cuts cost 3x",
                 "reason": "fits LLM-cost interest"},
                {"video_id": "v2", "rank": 2,
                 "hook": "Filler", "reason": ""},
            ],
        })),
    )

    digest = await digest_service.generate(db, user_id=1)
    assert digest.status.value == "ready"
    assert digest.item_count == 2
    assert digest.top_items_json is not None
    top = json.loads(digest.top_items_json)
    assert top[0]["video_id"] == "v1"
