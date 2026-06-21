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


def test_retrieve_ranks_topic_overlap_then_recency(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        cur = await db.execute(
            "INSERT INTO speakers (user_id, name, name_key) VALUES (1,'C','c')")
        sid = cur.lastrowid
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) "
            "VALUES ('v1', 1, 'youtube', 'u', 'Markets Episode')")
        await db.commit()
        await repo.insert_claim(db, speaker_id=sid, source_id="v1",
                                claim="inflation will fall", topic="inflation rates",
                                evidence_start_s=10)
        await repo.insert_claim(db, speaker_id=sid, source_id="v1",
                                claim="AI is overhyped", topic="ai bubble",
                                evidence_start_s=20)
        out = await speaker_claims.retrieve_for_prompt(
            db, sid, query="what about inflation?", limit=12)
        # the topic-overlapping claim ranks first; both carry source title + ts
        assert out[0]["claim"] == "inflation will fall"
        assert out[0]["source_title"] == "Markets Episode"
        assert out[0]["evidence_start_s"] == 10
    _run(go())


def test_retrieve_respects_limit_and_is_cross_source(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        cur = await db.execute(
            "INSERT INTO speakers (user_id, name, name_key) VALUES (1,'C','c')")
        sid = cur.lastrowid
        for v in ("v1", "v2"):
            await db.execute(
                "INSERT INTO videos (id, user_id, kind, url, title) "
                "VALUES (?, 1, 'youtube', 'u', ?)", (v, f"Ep {v}"))
        await db.commit()
        for i in range(5):
            await repo.insert_claim(db, speaker_id=sid, source_id="v1",
                                    claim=f"a{i}", topic="x")
        for i in range(5):
            await repo.insert_claim(db, speaker_id=sid, source_id="v2",
                                    claim=f"b{i}", topic="y")
        out = await speaker_claims.retrieve_for_prompt(db, sid, query="x", limit=3)
        assert len(out) == 3
        sources = {c["source_id"] for c in await speaker_claims.retrieve_for_prompt(
            db, sid, query="", limit=12)}
        assert sources == {"v1", "v2"}  # cross-source
    _run(go())


def test_retrieve_excludes_rejected(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        cur = await db.execute(
            "INSERT INTO speakers (user_id, name, name_key) VALUES (1,'C','c')")
        sid = cur.lastrowid
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) "
            "VALUES ('v1', 1, 'youtube', 'u', 'Ep')")
        await db.commit()
        row_id = await repo.insert_claim(db, speaker_id=sid, source_id="v1",
                                         claim="rejected claim", topic="t")
        await db.execute(
            "UPDATE speaker_claims SET review_status='rejected' WHERE id=?", (row_id,))
        await repo.insert_claim(db, speaker_id=sid, source_id="v1",
                                claim="good claim", topic="t")
        await db.commit()
        out = await speaker_claims.retrieve_for_prompt(db, sid, query="", limit=12)
        assert len(out) == 1
        assert out[0]["claim"] == "good claim"
    _run(go())


def test_retrieve_returns_exactly_9_contract_keys(db):
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        cur = await db.execute(
            "INSERT INTO speakers (user_id, name, name_key) VALUES (1,'C','c')")
        sid = cur.lastrowid
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) "
            "VALUES ('v1', 1, 'youtube', 'u', 'Ep')")
        await db.commit()
        await repo.insert_claim(db, speaker_id=sid, source_id="v1",
                                claim="some claim", topic="finance")
        out = await speaker_claims.retrieve_for_prompt(db, sid, query="", limit=12)
        assert len(out) == 1
        assert set(out[0].keys()) == {
            "claim", "topic", "evidence_text", "evidence_start_s",
            "source_id", "source_title", "attribution_method",
            "attribution_confidence", "review_status",
        }
    _run(go())


def test_garbage_does_not_wipe_existing_claims(db):
    """Proves that when extraction fails (garbage response), previously-persisted
    claims are NOT deleted. The replace_for_source_speakers call happens AFTER
    successful parsing, so garbage responses skip the delete-then-re-insert cycle."""
    async def go():
        from app.services import speaker_claims
        from app.repos import speaker_claims as repo
        ids, source = await _seed(db)

        # First extraction: seed with good claims
        with patch("app.services.speaker_claims.litellm.acompletion", _completion(_CLEAN)):
            first_out = await speaker_claims.extract_claims_for_source(
                db, source, ids, model="m", api_key="k", base_url=None,
            )
        assert len(first_out) == 2
        assert len(await repo.list_for_speaker(db, ids[0])) == 1
        first_claim = (await repo.list_for_speaker(db, ids[0]))[0].claim
        assert first_claim == "SPACs are mispriced"

        # Second extraction: garbage response (invalid JSON)
        with patch("app.services.speaker_claims.litellm.acompletion", _completion("not json at all")):
            second_out = await speaker_claims.extract_claims_for_source(
                db, source, ids, model="m", api_key="k", base_url=None,
            )
        # Garbage path returns empty
        assert second_out == []

        # Verify: existing claim is still there (NOT wiped by garbage)
        persisted = await repo.list_for_speaker(db, ids[0])
        assert len(persisted) == 1
        assert persisted[0].claim == first_claim
    _run(go())
