"""Tests for library synthesis — "ask my library" (Part C.2).

No live LLM/network: the completion is monkeypatched. The hybrid search
degrades to FTS when the embedder isn't available, which is fine here —
we seed summaries whose text matches the query.
"""

import json

from app.repos import llm_models as llm_models_repo
from app.repos import syntheses as syntheses_repo
from app.repos import videos as videos_repo
from app.services import ask as ask_svc


async def _seed_video(db, vid, *, title, summary, user_id=1):
    await videos_repo.upsert_metadata(
        db, video_id=vid, url=f"https://youtu.be/{vid}", title=title,
        description="", thumbnail_path=None, duration_seconds=None,
        user_id=user_id,
    )
    await videos_repo.set_summary(db, vid, summary, "model")


async def _default_model(db):
    await llm_models_repo.insert(
        db, label="m", provider_id="openai", model="openai/gpt-4o",
        api_key="sk-x", base_url="", make_default=True,
    )


def test_build_prompt_packs_summaries_and_demands_citations():
    from datetime import UTC, datetime

    from app.models import Video, VideoKind
    ts = datetime(2026, 6, 10, tzinfo=UTC)
    v = Video(
        id="1:abc", url="u", title="Agent Eval 101", description="",
        thumbnail_path=None, duration_seconds=None, transcript=None,
        transcript_source=None, summary="Eval needs golden sets.",
        summary_model="m", created_at=ts, updated_at=ts,
        kind=VideoKind.YOUTUBE,
    )
    system, user = ask_svc.build_prompt("How to eval agents?", [v])
    # System prompt forces grounding + citation behaviour.
    assert "only" in system.lower()
    assert "cite" in system.lower() or "citation" in system.lower()
    # The user message carries the question and the source link target.
    assert "How to eval agents?" in user
    assert "/v/1:abc" in user
    assert "Eval needs golden sets." in user


async def test_run_marks_ready_with_llm_markdown(db, monkeypatch):
    await _default_model(db)
    await _seed_video(db, "1:a", title="Agent Eval", summary="about agent eval")
    s = await syntheses_repo.create_pending(
        db, user_id=1, query="agent eval", source_ids=["1:a"],
    )

    async def fake_completion(*, system, user, model, api_key, base_url):
        return "Answer with [Agent Eval](/v/1:a)."
    monkeypatch.setattr(ask_svc, "_completion", fake_completion)

    await ask_svc.run(db, synthesis_id=s.id, user_id=1)
    got = await syntheses_repo.get(db, s.id)
    assert got is not None
    assert got.status.value == "ready"
    assert "[Agent Eval](/v/1:a)" in got.result_md


async def test_run_marks_failed_without_default_model(db, monkeypatch):
    await _seed_video(db, "1:a", title="X", summary="agent eval stuff")
    s = await syntheses_repo.create_pending(
        db, user_id=1, query="agent eval", source_ids=["1:a"],
    )
    # No default model configured.
    await ask_svc.run(db, synthesis_id=s.id, user_id=1)
    got = await syntheses_repo.get(db, s.id)
    assert got is not None
    assert got.status.value == "failed"
    assert got.error


async def test_run_records_source_ids_actually_used(db, monkeypatch):
    await _default_model(db)
    await _seed_video(db, "1:a", title="Agent Eval", summary="agent eval golden")
    await _seed_video(db, "1:b", title="Cooking", summary="how to bake bread")
    s = await syntheses_repo.create_pending(
        db, user_id=1, query="agent eval", source_ids=[],
    )

    async def fake_completion(*, system, user, model, api_key, base_url):
        return "ok"
    monkeypatch.setattr(ask_svc, "_completion", fake_completion)

    await ask_svc.run(db, synthesis_id=s.id, user_id=1)
    got = await syntheses_repo.get(db, s.id)
    assert got is not None
    used = json.loads(got.source_ids_json)
    # The relevant item must be among the sources; the unrelated one
    # need not be (FTS won't match "agent eval" against a bread summary).
    assert "1:a" in used
