"""Tests for library synthesis — "ask my library" (Part C.2).

No live LLM/network: the completion is monkeypatched. The hybrid search
degrades to FTS when the embedder isn't available, which is fine here —
we seed summaries whose text matches the query.
"""

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



async def test_start_thread_records_sources_and_pending_assistant(db, monkeypatch):
    await _default_model(db)
    await _seed_video(db, "1:a", title="Agent Eval", summary="agent eval golden")
    from app.repos import synthesis_messages as sm_repo

    s_id, assistant_id = await ask_svc.start_thread(db, user_id=1, query="agent eval")
    s = await syntheses_repo.get(db, s_id)
    import json
    assert "1:a" in json.loads(s.source_ids_json)
    msgs = await sm_repo.history(db, synthesis_id=s_id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].content == "agent eval"
    assert msgs[1].status.value == "pending"
    assert msgs[1].id == assistant_id


async def test_run_message_answers_pending_with_thread_context(db, monkeypatch):
    await _default_model(db)
    await _seed_video(db, "1:a", title="Agent Eval", summary="agent eval golden")
    from app.repos import synthesis_messages as sm_repo
    s_id, assistant_id = await ask_svc.start_thread(db, user_id=1, query="agent eval")
    captured = {}
    async def fake_completion(*, messages, model, api_key, base_url):
        captured["messages"] = messages
        return "Answer [Agent Eval](/v/1:a)."
    monkeypatch.setattr(ask_svc, "_completion_messages", fake_completion)
    await ask_svc.run_message(db, message_id=assistant_id)
    done = await sm_repo.get(db, assistant_id)
    assert done.status.value == "ready"
    assert "[Agent Eval](/v/1:a)" in done.content
    assert captured["messages"][-1] == {"role": "user", "content": "agent eval"}


async def test_followup_reuses_fixed_sources_not_research(db, monkeypatch):
    await _default_model(db)
    await _seed_video(db, "1:a", title="Agent Eval", summary="agent eval golden")
    await _seed_video(db, "1:b", title="Bread", summary="how to bake bread")
    s_id, a1 = await ask_svc.start_thread(db, user_id=1, query="agent eval")
    calls = []
    async def fake_completion(*, messages, model, api_key, base_url):
        calls.append(messages)
        return "ok"
    monkeypatch.setattr(ask_svc, "_completion_messages", fake_completion)
    await ask_svc.run_message(db, message_id=a1)
    a2 = await ask_svc.add_followup(db, synthesis_id=s_id, query="cooking?")
    s_before = await syntheses_repo.get(db, s_id)
    await ask_svc.run_message(db, message_id=a2)
    s_after = await syntheses_repo.get(db, s_id)
    assert s_before.source_ids_json == s_after.source_ids_json
    # The follow-up call sees the prior exchange (the first answer "ok")
    # plus the new user question last.
    assert any(m["role"] == "assistant" and m["content"] == "ok"
               for m in calls[1])
    assert calls[1][-1] == {"role": "user", "content": "cooking?"}


async def test_run_message_marks_failed_on_error(db, monkeypatch):
    await _default_model(db)
    await _seed_video(db, "1:a", title="X", summary="agent eval stuff")
    from app.repos import synthesis_messages as sm_repo
    s_id, a1 = await ask_svc.start_thread(db, user_id=1, query="agent eval")
    async def boom(*, messages, model, api_key, base_url):
        raise RuntimeError("llm down")
    monkeypatch.setattr(ask_svc, "_completion_messages", boom)
    await ask_svc.run_message(db, message_id=a1)
    done = await sm_repo.get(db, a1)
    assert done.status.value == "failed"
    assert "llm down" in done.error
