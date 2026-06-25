"""Task 3 + Task 4: claim embedding and embedding-ranked retrieval tests.

Tests:
1. test_extracted_claims_get_embedded  — after extraction, search_claim_vectors
   returns a hit for a semantically-near query.
2. test_reprocess_drops_old_claim_vectors  — a second extraction with a different
   claim replaces the old claim's vector so only the new claim is findable.
3. test_retrieval_prefers_semantic_over_recency  — embedding-ranked retrieve_for_prompt
   returns the semantically-closest claim first, even if it was inserted earlier.
4. test_retrieval_falls_back_to_recency_without_embeddings  — when _embed_query raises,
   retrieve_for_prompt falls back to recency and still returns all claims.
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

from app.repos import speaker_claim_embeddings as cve


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _completion(text: str):
    """Mock a non-streaming litellm.acompletion return.

    Mirrors the shape used in test_services_speaker_claims.py so the
    seam (response.choices[0].message.content) is identical.
    """
    msg = type("M", (), {"content": text})
    choice = type("C", (), {"message": msg})
    resp = type("R", (), {"choices": [choice]})
    return AsyncMock(return_value=resp())


async def _seed_speaker_and_video(db, name: str, vid: str) -> tuple[int, object]:
    """Insert a speaker + video and return (speaker_id, source)."""
    from app.repos import speakers as speakers_repo
    from app.repos import videos as videos_repo

    sid = await speakers_repo.resolve_speaker(db, name=name)
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title, transcript) "
        "VALUES (?,1,'youtube','u',?,?)",
        (vid, f"Test Video {vid}", "some transcript body"),
    )
    await db.commit()
    source = await videos_repo.get(db, vid)
    return sid, source


def test_extracted_claims_get_embedded(db):
    """Claims inserted by extract_claims_for_source must be embedded so
    search_claim_vectors can find them with a semantically-near query."""
    async def go():
        from app.repos import speaker_claim_embeddings as cve
        from app.services import speaker_claims
        from app.services.embeddings_local import embed_text

        sid, source = await _seed_speaker_and_video(db, "Eddie E", "vE")

        payload = json.dumps({
            "claims": [{
                "speaker": "Eddie E",
                "claim": "AI safety must come before scaling",
                "topic": "ai safety",
                "evidence_text": "we need safety first",
                "evidence_start_s": 30,
                "confidence": 0.8,
                "attribution_method": "explicit_name",
                "attribution_confidence": 0.9,
                "attribution_reason": "named explicitly",
            }]
        })

        with patch("app.services.speaker_claims.litellm.acompletion", _completion(payload)):
            out = await speaker_claims.extract_claims_for_source(
                db, source, [sid], model="m", api_key="", base_url=None,
            )

        assert len(out) == 1, f"expected 1 accepted claim, got {out}"

        # Embed a semantically-near query and verify the claim is found.
        q = await embed_text("artificial intelligence safety")
        hits = await cve.search_claim_vectors(db, sid, q, limit=5)
        assert hits, "extracted claim should have been embedded into speaker_claim_embeddings"

    _run(go())


def test_reprocess_drops_old_claim_vectors(db):
    """A second extraction with a DIFFERENT claim payload must replace the old
    claim's vector. After reprocess:
    - the OLD claim text's topic is NOT findable via vector search
    - the NEW claim's topic IS findable
    """
    async def go():
        from app.repos import speaker_claim_embeddings as cve
        from app.services import speaker_claims
        from app.services.embeddings_local import embed_text

        sid, source = await _seed_speaker_and_video(db, "Frank F", "vF")

        old_payload = json.dumps({
            "claims": [{
                "speaker": "Frank F",
                "claim": "Inflation will spike due to supply chain disruptions",
                "topic": "inflation economics",
                "evidence_text": "supply chain collapse",
                "evidence_start_s": 10,
                "confidence": 0.8,
                "attribution_method": "explicit_name",
                "attribution_confidence": 0.9,
                "attribution_reason": "named in sentence",
            }]
        })

        # First extraction — embeds the "inflation" claim.
        with patch("app.services.speaker_claims.litellm.acompletion", _completion(old_payload)):
            first_out = await speaker_claims.extract_claims_for_source(
                db, source, [sid], model="m", api_key="", base_url=None,
            )
        assert len(first_out) == 1

        # Sanity: old claim is findable before reprocess.
        q_inflation = await embed_text("inflation supply chain economics")
        hits_before = await cve.search_claim_vectors(db, sid, q_inflation, limit=5)
        assert hits_before, "old claim vector should exist before reprocess"
        old_claim_id = hits_before[0][0]

        new_payload = json.dumps({
            "claims": [{
                "speaker": "Frank F",
                "claim": "Renewable energy is the only path to energy independence",
                "topic": "renewable energy",
                "evidence_text": "solar and wind are the future",
                "evidence_start_s": 60,
                "confidence": 0.85,
                "attribution_method": "explicit_name",
                "attribution_confidence": 0.9,
                "attribution_reason": "named in sentence",
            }]
        })

        # Second extraction — should replace old claim + its vector.
        with patch("app.services.speaker_claims.litellm.acompletion", _completion(new_payload)):
            second_out = await speaker_claims.extract_claims_for_source(
                db, source, [sid], model="m", api_key="", base_url=None,
            )
        assert len(second_out) == 1

        # OLD claim vector must be gone — its claim_id should no longer appear.
        cur = await db.execute(
            "SELECT claim_id FROM speaker_claim_embeddings WHERE claim_id=?",
            (old_claim_id,),
        )
        row = await cur.fetchone()
        assert row is None, (
            f"old claim vector (claim_id={old_claim_id}) still present after reprocess"
        )

        # NEW claim's topic (renewable energy) should be findable.
        q_renewable = await embed_text("renewable energy solar wind independence")
        hits_after = await cve.search_claim_vectors(db, sid, q_renewable, limit=5)
        assert hits_after, "new claim should be embedded and findable after reprocess"

    _run(go())


# ---------------------------------------------------------------------------
# Task 4 helpers — direct claim + video insertion (bypass LLM extraction)
# ---------------------------------------------------------------------------


async def _video(db, vid: str) -> None:
    """Insert a minimal video row (idempotent — ignore if already present)."""
    await db.execute(
        "INSERT OR IGNORE INTO videos (id, user_id, kind, url, title, transcript) "
        "VALUES (?,1,'youtube','u',?,?)",
        (vid, f"Test Video {vid}", "some transcript"),
    )
    await db.commit()


async def _claim(db, sid: int, vid: str, text: str, topic=None) -> int:
    """Insert a speaker_claim row directly and return its id."""
    cur = await db.execute(
        "INSERT INTO speaker_claims (user_id, speaker_id, source_id, claim, topic, "
        "extraction_method) VALUES (1,?,?,?,?,'llm')",
        (sid, vid, text, topic),
    )
    await db.commit()
    return cur.lastrowid


async def _embed(text: str) -> list[float]:
    """Embed text using the same local embedder as T3 claim extraction."""
    from app.services.embeddings_local import embed_text
    return await embed_text(text)


# ---------------------------------------------------------------------------
# Task 4 tests
# ---------------------------------------------------------------------------


def test_retrieval_prefers_semantic_over_recency(db):
    """Embedding-ranked retrieve_for_prompt must return the semantically-closest
    claim first, even when it was inserted BEFORE a more recent but off-topic one."""
    async def go():
        from app.repos import speakers as speakers_repo
        from app.services import speaker_claims

        sid = await speakers_repo.resolve_speaker(db, name="Fred F")
        await _video(db, "vF")
        # Insert an OLD on-topic claim, then a NEWER off-topic claim.
        on_topic = await _claim(db, sid, "vF", "Bitcoin is a hedge against inflation")
        off_topic = await _claim(db, sid, "vF", "I prefer hiking on weekends")
        await cve.upsert_claim_embedding(
            db, on_topic, await _embed("Bitcoin is a hedge against inflation")
        )
        await cve.upsert_claim_embedding(
            db, off_topic, await _embed("I prefer hiking on weekends")
        )
        out = await speaker_claims.retrieve_for_prompt(
            db, sid, query="is crypto a good inflation hedge", limit=2
        )
        assert out, "expected retrieved claims"
        # retrieve_for_prompt returns list[dict] (9-key contract), not SpeakerClaim.
        assert out[0]["claim"].startswith("Bitcoin"), (
            "semantically-closest claim must outrank the more recent off-topic one"
        )

    _run(go())


def test_retrieval_falls_back_to_recency_without_embeddings(db, monkeypatch):
    """When _embed_query raises (embedder offline), retrieve_for_prompt must fall
    back to the recency path and still return all claims as list[dict]."""
    async def go():
        from app.repos import speakers as speakers_repo
        from app.services import speaker_claims

        sid = await speakers_repo.resolve_speaker(db, name="Gina G")
        await _video(db, "vG")
        # No embeddings inserted at all.
        await _claim(db, sid, "vG", "older position")
        await _claim(db, sid, "vG", "newer position")
        # Force the embedder to be 'unavailable' so the fallback path runs.
        async def _boom(_text):
            raise RuntimeError("embedder offline")
        monkeypatch.setattr(speaker_claims, "_embed_query", _boom, raising=False)
        out = await speaker_claims.retrieve_for_prompt(
            db, sid, query="anything", limit=5
        )
        texts = {c["claim"] for c in out}   # list[dict] contract (9 keys)
        assert {"older position", "newer position"} <= texts, (
            "fallback must still return claims when embeddings are absent"
        )

    _run(go())


def test_rejected_claim_excluded_from_retrieval(db):
    """Rejected claims must NOT appear in retrieve_for_prompt, even via the KNN path.

    Regression for the PR-4 bug: _load_claims_by_id (KNN path) previously
    had no review_status filter, so a rejected claim whose vector was still
    in speaker_claim_embeddings would be re-injected via KNN.
    """
    async def go():
        from app.repos import speaker_claims as repo
        from app.repos import speakers as speakers_repo
        from app.services import speaker_claims

        sid = await speakers_repo.resolve_speaker(db, name="Hannah H")
        await _video(db, "vH")

        # Insert the claim and embed it (vector must exist to trigger the KNN path).
        claim_id = await _claim(
            db, sid, "vH", "Bitcoin is a hedge against inflation", topic="crypto"
        )
        await cve.upsert_claim_embedding(
            db, claim_id, await _embed("Bitcoin is a hedge against inflation")
        )

        # Sanity: the claim is returned before rejection (vector hit via KNN).
        out_before = await speaker_claims.retrieve_for_prompt(
            db, sid, query="crypto inflation hedge", limit=5
        )
        assert any(c["claim"].startswith("Bitcoin") for c in out_before), (
            "sanity: Bitcoin claim should be present before rejection"
        )

        # Reject the claim via the repo.
        await repo.set_review_status(db, claim_id, "rejected")

        # After rejection the claim must NOT appear — the vector still exists in the
        # embeddings table, so only a proper filter in _load_claims_by_id can block it.
        out_after = await speaker_claims.retrieve_for_prompt(
            db, sid, query="crypto inflation hedge", limit=5
        )
        assert not any(c["claim"].startswith("Bitcoin") for c in out_after), (
            "rejected claim must not appear via KNN path after set_review_status('rejected')"
        )

    _run(go())
