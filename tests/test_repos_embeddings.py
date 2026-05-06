import aiosqlite

from app.repos import embeddings as embeddings_repo
from app.repos import videos as videos_repo


def _vec(x: float, dim: int = 768) -> list[float]:
    """Build a simple deterministic vector for tests."""
    return [x] * dim


async def _make_video(db: aiosqlite.Connection, vid: str) -> None:
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title=vid,
        description="", thumbnail_path=None, duration_seconds=None,
    )


async def test_upsert_and_search_round_trip(db: aiosqlite.Connection):
    await _make_video(db, "v1")
    await embeddings_repo.upsert_summary_embedding(db, "v1", _vec(0.5))

    hits = await embeddings_repo.search_by_summary_vector(db, _vec(0.5), limit=10)
    assert len(hits) == 1
    assert hits[0][0] == "v1"
    # Distance to identical vector should be ~0.
    assert hits[0][1] < 1e-6


async def test_upsert_replaces_existing(db: aiosqlite.Connection):
    await _make_video(db, "v1")
    await embeddings_repo.upsert_summary_embedding(db, "v1", _vec(0.1))
    await embeddings_repo.upsert_summary_embedding(db, "v1", _vec(0.9))

    cursor = await db.execute(
        "SELECT COUNT(*) FROM video_embeddings WHERE video_id = ?", ("v1",)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1


async def test_upsert_sets_summary_embedded_at(db: aiosqlite.Connection):
    await _make_video(db, "v1")
    await embeddings_repo.upsert_summary_embedding(db, "v1", _vec(0.5))

    cursor = await db.execute(
        "SELECT summary_embedded_at FROM videos WHERE id = ?", ("v1",)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] is not None  # populated by upsert


async def test_delete_removes_row_and_clears_timestamp(db: aiosqlite.Connection):
    await _make_video(db, "v1")
    await embeddings_repo.upsert_summary_embedding(db, "v1", _vec(0.5))
    await embeddings_repo.delete_summary_embedding(db, "v1")

    hits = await embeddings_repo.search_by_summary_vector(db, _vec(0.5))
    assert hits == []

    cursor = await db.execute(
        "SELECT summary_embedded_at FROM videos WHERE id = ?", ("v1",)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] is None


async def test_search_orders_by_distance(db: aiosqlite.Connection):
    await _make_video(db, "near")
    await _make_video(db, "far")
    # Two vectors that differ in just one entry — vec(1.0) is closer to
    # vec(0.95) than to vec(0.0).
    near_vec = [0.95] * 768
    far_vec = [0.0] * 768
    await embeddings_repo.upsert_summary_embedding(db, "near", near_vec)
    await embeddings_repo.upsert_summary_embedding(db, "far", far_vec)

    hits = await embeddings_repo.search_by_summary_vector(
        db, [1.0] * 768, limit=10
    )
    ids = [h[0] for h in hits]
    assert ids[0] == "near"
    assert ids[1] == "far"


async def test_search_limit_respected(db: aiosqlite.Connection):
    for i in range(5):
        await _make_video(db, f"v{i}")
        await embeddings_repo.upsert_summary_embedding(
            db, f"v{i}", [float(i) / 10] * 768
        )
    hits = await embeddings_repo.search_by_summary_vector(
        db, [0.1] * 768, limit=2
    )
    assert len(hits) == 2
