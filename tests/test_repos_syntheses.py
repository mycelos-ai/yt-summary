"""CRUD tests for the syntheses repo (Part C.2)."""

import json

from app.models import SynthesisStatus
from app.repos import syntheses as syntheses_repo


async def test_create_pending_returns_row(db):
    s = await syntheses_repo.create_pending(
        db, user_id=1, query="What about agent eval?",
        source_ids=["1:a", "1:b"],
    )
    assert s.id > 0
    assert s.user_id == 1
    assert s.query == "What about agent eval?"
    assert s.status == SynthesisStatus.PENDING
    assert json.loads(s.source_ids_json) == ["1:a", "1:b"]
    assert s.result_md is None


async def test_mark_ready_sets_result(db):
    s = await syntheses_repo.create_pending(
        db, user_id=1, query="q", source_ids=["1:a"],
    )
    await syntheses_repo.mark_ready(db, synthesis_id=s.id, result_md="# Answer")
    got = await syntheses_repo.get(db, s.id)
    assert got is not None
    assert got.status == SynthesisStatus.READY
    assert got.result_md == "# Answer"
    assert got.error is None


async def test_mark_failed_sets_error(db):
    s = await syntheses_repo.create_pending(
        db, user_id=1, query="q", source_ids=[],
    )
    await syntheses_repo.mark_failed(db, synthesis_id=s.id, error="boom")
    got = await syntheses_repo.get(db, s.id)
    assert got is not None
    assert got.status == SynthesisStatus.FAILED
    assert got.error == "boom"


async def test_list_for_user_scoped_and_ordered(db):
    await syntheses_repo.create_pending(db, user_id=1, query="first", source_ids=[])
    await syntheses_repo.create_pending(db, user_id=1, query="second", source_ids=[])
    await syntheses_repo.create_pending(db, user_id=2, query="other", source_ids=[])
    rows = await syntheses_repo.list_for_user(db, user_id=1, limit=10)
    assert [r.query for r in rows] == ["second", "first"]  # newest first
    assert all(r.user_id == 1 for r in rows)


async def test_get_none_for_unknown(db):
    assert await syntheses_repo.get(db, 9999) is None
