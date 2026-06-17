"""Tests for related-item discovery (Part C.1).

Reuses the existing summary-vector KNN. The service excludes the item
itself and other profiles' copies of the same source, keeps only close
neighbours, and caps the count.
"""

import aiosqlite

from app.repos import embeddings as embeddings_repo
from app.repos import videos as videos_repo
from app.services import related as related_svc


def _vec(x: float, dim: int = 384) -> list[float]:
    return [x] * dim


async def _seed(db, vid, *, user_id=1, youtube_id=None, vecval=None):
    await videos_repo.upsert_metadata(
        db, video_id=vid, url=f"https://youtu.be/{vid}", title=vid,
        description="", thumbnail_path=None, duration_seconds=None,
        user_id=user_id, youtube_id=youtube_id,
    )
    await videos_repo.set_summary(db, vid, f"summary {vid}", "model")
    if vecval is not None:
        await embeddings_repo.upsert_summary_embedding(db, vid, _vec(vecval))


async def test_related_excludes_self_and_ranks_by_similarity(db: aiosqlite.Connection):
    await _seed(db, "a", vecval=0.50)
    await _seed(db, "b", vecval=0.51)   # very close
    await _seed(db, "c", vecval=0.95)   # far
    a = await videos_repo.get(db, "a")
    ids = await related_svc.related_video_ids(
        db, a, user_id=1, limit=5, max_distance=0.75,
    )
    assert "a" not in ids                 # never the item itself
    assert ids and ids[0] == "b"          # closest first


async def test_related_returns_empty_without_embedding(db: aiosqlite.Connection):
    await _seed(db, "a", vecval=None)     # no embedding
    a = await videos_repo.get(db, "a")
    ids = await related_svc.related_video_ids(db, a, user_id=1)
    assert ids == []


async def test_related_excludes_other_profile_copies_of_same_source(
    db: aiosqlite.Connection,
):
    # Same youtube_id imported under two profiles. From profile 1's item,
    # profile 2's near-identical copy must not surface.
    await _seed(db, "1:yt", user_id=1, youtube_id="ytABC123456", vecval=0.50)
    await _seed(db, "2:yt", user_id=2, youtube_id="ytABC123456", vecval=0.50)
    await _seed(db, "1:other", user_id=1, youtube_id="zzz99999999", vecval=0.52)
    a = await videos_repo.get(db, "1:yt")
    ids = await related_svc.related_video_ids(db, a, user_id=1)
    assert "2:yt" not in ids
    assert "1:other" in ids


async def test_related_respects_max_distance(db: aiosqlite.Connection):
    await _seed(db, "a", vecval=0.5)
    await _seed(db, "far", vecval=-0.5)   # opposite direction → large dist
    a = await videos_repo.get(db, "a")
    ids = await related_svc.related_video_ids(
        db, a, user_id=1, max_distance=0.1,
    )
    assert "far" not in ids


async def test_related_excludes_archived_neighbour(db: aiosqlite.Connection):
    await _seed(db, "a", vecval=0.50)
    await _seed(db, "b", vecval=0.51)  # closest neighbour, but archived
    await db.execute(
        "UPDATE videos SET archived_at=datetime('now') WHERE id='b'"
    )
    await db.commit()
    a = await videos_repo.get(db, "a")
    ids = await related_svc.related_video_ids(
        db, a, user_id=1, limit=5, max_distance=0.75,
    )
    assert "b" not in ids
