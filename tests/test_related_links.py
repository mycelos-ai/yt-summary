import json
from dataclasses import dataclass

import pytest

from app.services import related_links


@dataclass
class _FakeModelRow:
    model: str = "test/model"
    api_key: str = ""
    base_url: str = ""


def _video(vid, title, summary="some summary text"):
    # Minimal stand-in; compute_related_links only reads .id/.title/
    # .summary/.highlights_json on candidates and .id/.summary on the
    # subject. Use the real Video if the helper exists in conftest.
    from app.models import Video, VideoKind
    from datetime import datetime, UTC
    return Video(
        id=vid, url=f"https://x/{vid}", title=title, description="",
        thumbnail_path=None, duration_seconds=None, transcript=None,
        transcript_source=None, summary=summary, summary_model="m",
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        kind=VideoKind.YOUTUBE,
        user_id=1,
        transcript_segments=None,
        youtube_id=None,
        source_language=None,
        summary_language=None,
        transcript_language=None,
        highlights_json=None,
        archived_at=None,
        image_query=None,
        related_links_json=None,
    )


async def test_empty_candidates_returns_empty_without_llm(monkeypatch):
    async def fake_related_ids(*a, **k):
        return []
    monkeypatch.setattr(related_links.related_svc, "related_video_ids",
                        fake_related_ids)
    called = {"llm": False}
    async def fake_llm(*a, **k):
        called["llm"] = True
        return ""
    monkeypatch.setattr(related_links, "_llm_select", fake_llm)

    out = await related_links.compute_related_links(
        db=None, video=_video("v1", "Subject"), user_id=1,
        model_row=_FakeModelRow(),
    )
    assert out == []
    assert called["llm"] is False


async def test_hallucinated_ids_are_dropped(monkeypatch):
    async def fake_related_ids(*a, **k):
        return ["v2", "v3"]
    monkeypatch.setattr(related_links.related_svc, "related_video_ids",
                        fake_related_ids)

    async def fake_get_many(db, ids):
        return {
            "v2": _video("v2", "Two"),
            "v3": _video("v3", "Three"),
        }
    monkeypatch.setattr(related_links.videos_repo, "get_many", fake_get_many)

    # LLM returns one real id (v2) and one hallucinated id (v999).
    async def fake_llm(*a, **k):
        return json.dumps({"links": [
            {"video_id": "v2", "reason": "same topic"},
            {"video_id": "v999", "reason": "made up"},
        ]})
    monkeypatch.setattr(related_links, "_llm_select", fake_llm)

    out = await related_links.compute_related_links(
        db=None, video=_video("v1", "Subject"), user_id=1,
        model_row=_FakeModelRow(),
    )
    assert out == [
        {"video_id": "v2", "title": "Two", "reason": "same topic"},
    ]


async def test_invalid_json_raises(monkeypatch):
    async def fake_related_ids(*a, **k):
        return ["v2"]
    monkeypatch.setattr(related_links.related_svc, "related_video_ids",
                        fake_related_ids)
    async def fake_get_many(db, ids):
        return {"v2": _video("v2", "Two")}
    monkeypatch.setattr(related_links.videos_repo, "get_many", fake_get_many)
    async def fake_llm(*a, **k):
        return "not json at all"
    monkeypatch.setattr(related_links, "_llm_select", fake_llm)

    with pytest.raises(Exception):
        await related_links.compute_related_links(
            db=None, video=_video("v1", "Subject"), user_id=1,
            model_row=_FakeModelRow(),
        )
