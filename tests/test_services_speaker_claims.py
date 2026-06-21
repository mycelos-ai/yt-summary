import asyncio
import json
from unittest.mock import AsyncMock, patch


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _completion(text: str):
    """Mock a non-streaming litellm.acompletion return (mirrors
    summarizer._completion's response.choices[0].message.content shape)."""
    msg = type("M", (), {"content": text})
    choice = type("C", (), {"message": msg})
    resp = type("R", (), {"choices": [choice]})
    return AsyncMock(return_value=resp())


async def _seed(db, *, names=("Chamath", "Jason")):
    ids = []
    for n in names:
        cur = await db.execute(
            "INSERT INTO speakers (user_id, name, name_key) VALUES (1,?,?)",
            (n, n.lower()),
        )
        ids.append(cur.lastrowid)
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title, transcript) "
        "VALUES ('vid-1', 1, 'youtube', 'u', 'All-In Ep', 'transcript body')"
    )
    await db.commit()
    from app.repos import videos as videos_repo
    return ids, await videos_repo.get(db, "vid-1")


_CLEAN = json.dumps({
    "claims": [
        {"speaker": "Chamath", "claim": "SPACs are mispriced", "topic": "markets",
         "evidence_text": "SPACs are wildly mispriced", "evidence_start_s": 42,
         "confidence": 0.8, "attribution_method": "explicit_name",
         "attribution_confidence": 0.95, "attribution_reason": "named in prior sentence"},
        {"speaker": "Jason", "claim": "founders should stay scrappy", "topic": "startups",
         "evidence_text": "stay scrappy", "evidence_start_s": 120,
         "confidence": 0.7, "attribution_method": "speaker_marker",
         "attribution_confidence": 0.8, "attribution_reason": "speaker label in transcript"},
    ]
})


def test_clean_json_produces_attributed_claims(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        ids, source = await _seed(db)
        with patch("app.services.speaker_claims.litellm.acompletion", _completion(_CLEAN)):
            out = await speaker_claims.extract_claims_for_source(
                db, source, ids, model="openai/gpt-4o", api_key="k", base_url=None,
            )
        assert len(out) == 2
        # persisted, attributed to the right speakers
        cham = await repo.list_for_speaker(db, ids[0])
        jason = await repo.list_for_speaker(db, ids[1])
        assert [c.claim for c in cham] == ["SPACs are mispriced"]
        assert cham[0].attribution_method == "explicit_name"
        assert cham[0].attribution_confidence == 0.95
        assert cham[0].evidence_start_s == 42
        assert cham[0].review_status == "unreviewed"
        assert [c.claim for c in jason] == ["founders should stay scrappy"]
    _run(go())


def test_prose_wrapped_json_still_parsed(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        ids, source = await _seed(db)
        wrapped = "Sure, here are the claims:\n```json\n" + _CLEAN + "\n```\nDone."
        with patch("app.services.speaker_claims.litellm.acompletion", _completion(wrapped)):
            out = await speaker_claims.extract_claims_for_source(
                db, source, ids, model="m", api_key="k", base_url=None,
            )
        assert len(out) == 2
        assert len(await repo.list_for_speaker(db, ids[0])) == 1
    _run(go())


def test_garbage_returns_empty_no_raise(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        ids, source = await _seed(db)
        with patch("app.services.speaker_claims.litellm.acompletion", _completion("not json at all")):
            out = await speaker_claims.extract_claims_for_source(
                db, source, ids, model="m", api_key="k", base_url=None,
            )
        assert out == []
        assert await repo.list_for_speaker(db, ids[0]) == []
    _run(go())


def test_unattributable_statement_makes_no_claim(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        ids, source = await _seed(db)
        # speaker not in the expected list, or null → dropped
        payload = json.dumps({"claims": [
            {"speaker": "Unknown Person", "claim": "x", "attribution_method": "llm_inferred"},
            {"speaker": None, "claim": "y"},
            {"speaker": "Chamath", "claim": "kept", "attribution_method": "explicit_name",
             "attribution_confidence": 0.9},
        ]})
        with patch("app.services.speaker_claims.litellm.acompletion", _completion(payload)):
            out = await speaker_claims.extract_claims_for_source(
                db, source, ids, model="m", api_key="k", base_url=None,
            )
        assert [c["claim"] for c in out] == ["kept"]
        assert [c.claim for c in await repo.list_for_speaker(db, ids[0])] == ["kept"]
    _run(go())


def test_long_source_small_window_uses_summary_not_transcript(db):
    """Finding 5: when the transcript exceeds the model's context window, the
    grounding text handed to the LLM is the summary, not the (truncated)
    transcript. We capture the prompt the mock receives and assert it contains
    the summary text and NOT the long transcript body."""
    async def go():
        from app.services import speaker_claims
        ids, _ = await _seed(db)
        # Make the source long with a distinct transcript vs summary.
        long_transcript = "TRANSCRIPT_MARKER " * 5000   # ~tens of thousands of tokens
        await db.execute(
            "UPDATE videos SET transcript=?, summary=? WHERE id='vid-1'",
            (long_transcript, "SUMMARY_MARKER: the gist."),
        )
        await db.commit()
        from app.repos import videos as videos_repo
        source = await videos_repo.get(db, "vid-1")
        seen_prompt = {}

        def _capture(text):
            mock = _completion(_CLEAN)
            async def side_effect(**kwargs):
                seen_prompt["messages"] = kwargs["messages"]
                return mock.return_value
            return AsyncMock(side_effect=side_effect)

        with patch("app.services.speaker_claims.model_info.get_context_window",
                   AsyncMock(return_value=8192)), \
             patch("app.services.speaker_claims.litellm.acompletion", _capture(_CLEAN)):
            await speaker_claims.extract_claims_for_source(
                db, source, ids, model="tiny/model", api_key="k", base_url=None,
            )
        blob = json.dumps(seen_prompt["messages"])
        assert "SUMMARY_MARKER" in blob
        assert "TRANSCRIPT_MARKER" not in blob   # never blind-fed the long transcript
    _run(go())


def test_llm_exception_returns_empty(db):
    async def go():
        from app.services import speaker_claims
        ids, source = await _seed(db)
        with patch("app.services.speaker_claims.litellm.acompletion",
                   AsyncMock(side_effect=RuntimeError("boom"))):
            out = await speaker_claims.extract_claims_for_source(
                db, source, ids, model="m", api_key="k", base_url=None,
            )
        assert out == []
    _run(go())


def test_reprocess_replaces_prior_claims(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        ids, source = await _seed(db)
        with patch("app.services.speaker_claims.litellm.acompletion", _completion(_CLEAN)):
            await speaker_claims.extract_claims_for_source(
                db, source, ids, model="m", api_key="k", base_url=None)
            # second run must not duplicate
            await speaker_claims.extract_claims_for_source(
                db, source, ids, model="m", api_key="k", base_url=None)
        assert len(await repo.list_for_speaker(db, ids[0])) == 1
    _run(go())
