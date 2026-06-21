import asyncio

from app.repos import speaker_claims as repo


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


async def _seed_speaker_and_source(db, *, name="Chamath", sid="vid-1"):
    cur = await db.execute(
        "INSERT INTO speakers (user_id, name, name_key) VALUES (1, ?, ?)",
        (name, name.lower()),
    )
    speaker_id = cur.lastrowid
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title) "
        "VALUES (?, 1, 'youtube', 'u', 'Ep 1')",
        (sid,),
    )
    await db.commit()
    return speaker_id, sid


def test_insert_claim_defaults_unreviewed(db):
    async def go():
        speaker_id, sid = await _seed_speaker_and_source(db)
        cid = await repo.insert_claim(
            db, speaker_id=speaker_id, source_id=sid,
            claim="SPACs are mispriced", topic="markets",
            evidence_text="they're mispriced", evidence_start_s=42,
            confidence=0.8, attribution_method="explicit_name",
            attribution_confidence=0.9, attribution_reason="named in prior sentence",
        )
        rows = await repo.list_for_speaker(db, speaker_id)
        assert len(rows) == 1
        c = rows[0]
        assert c.id == cid
        assert c.claim == "SPACs are mispriced"
        assert c.topic == "markets"
        assert c.evidence_start_s == 42
        assert c.extraction_method == "llm"
        assert c.attribution_method == "explicit_name"
        assert c.attribution_confidence == 0.9
        assert c.attribution_reason == "named in prior sentence"
        assert c.review_status == "unreviewed"
    _run(go())


def test_list_for_speaker_grouped_by_topic(db):
    async def go():
        speaker_id, sid = await _seed_speaker_and_source(db)
        await repo.insert_claim(db, speaker_id=speaker_id, source_id=sid,
                                claim="A", topic="markets")
        await repo.insert_claim(db, speaker_id=speaker_id, source_id=sid,
                                claim="B", topic="markets")
        await repo.insert_claim(db, speaker_id=speaker_id, source_id=sid,
                                claim="C", topic="ai")
        grouped = await repo.list_for_speaker(db, speaker_id, grouped_by_topic=True)
        assert set(grouped.keys()) == {"markets", "ai"}
        assert len(grouped["markets"]) == 2
        assert len(grouped["ai"]) == 1
    _run(go())


def test_set_review_status_and_edit(db):
    async def go():
        speaker_id, sid = await _seed_speaker_and_source(db)
        cid = await repo.insert_claim(db, speaker_id=speaker_id, source_id=sid,
                                      claim="orig", topic="markets")
        await repo.set_review_status(db, cid, "accepted")
        await repo.edit_claim(db, cid, claim="corrected", topic="macro")
        c = (await repo.list_for_speaker(db, speaker_id))[0]
        assert c.review_status == "accepted"
        assert c.claim == "corrected"
        assert c.topic == "macro"
    _run(go())


def test_replace_for_source_speakers_clears_then_allows_reinsert(db):
    async def go():
        speaker_id, sid = await _seed_speaker_and_source(db)
        await repo.insert_claim(db, speaker_id=speaker_id, source_id=sid, claim="stale")
        await repo.replace_for_source_speakers(db, sid, [speaker_id])
        assert await repo.list_for_speaker(db, speaker_id) == []
        # other speakers / other sources are untouched
        sp2, _ = await _seed_speaker_and_source(db, name="Jason", sid="vid-2")
        await repo.insert_claim(db, speaker_id=sp2, source_id="vid-2", claim="keep")
        await repo.replace_for_source_speakers(db, sid, [speaker_id])
        assert len(await repo.list_for_speaker(db, sp2)) == 1
    _run(go())


def test_list_for_source_speakers_filters(db):
    async def go():
        sp1, sid = await _seed_speaker_and_source(db, name="A", sid="s1")
        sp2, _ = await _seed_speaker_and_source(db, name="B", sid="s2")
        await repo.insert_claim(db, speaker_id=sp1, source_id=sid, claim="x")
        await repo.insert_claim(db, speaker_id=sp2, source_id="s2", claim="y")
        out = await repo.list_for_source_speakers(db, sid, [sp1, sp2])
        assert [c.claim for c in out] == ["x"]
    _run(go())
