"""Task 3: claims extracted by extract_claims_for_source get embedded best-effort.

Tests:
1. test_extracted_claims_get_embedded  — after extraction, search_claim_vectors
   returns a hit for a semantically-near query.
2. test_reprocess_drops_old_claim_vectors  — a second extraction with a different
   claim replaces the old claim's vector so only the new claim is findable.
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest


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
        from app.services import speaker_claims
        from app.repos import speaker_claim_embeddings as cve
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
        from app.services import speaker_claims
        from app.repos import speaker_claim_embeddings as cve
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
