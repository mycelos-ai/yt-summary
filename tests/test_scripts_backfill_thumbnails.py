from pathlib import Path

import pytest

from app.config import Config
from app.models import VideoKind
from app.repos import videos as videos_repo
from app.scripts import backfill_thumbnails as bf


async def _seed(db, vid, *, kind=VideoKind.EMAIL, thumb=None, iq="cats"):
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title="t", description="",
        thumbnail_path=thumb, duration_seconds=None, kind=kind,
    )
    if iq is not None:
        await videos_repo.set_image_query(db, vid, iq)


@pytest.mark.asyncio
async def test_backfill_skips_items_with_thumbnail(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1", thumb="thumbnails/v1.jpg")

    async def fake_fetch(*, query, api_key, target):
        Path(target).write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(
        "app.services.stock_images.fetch_pexels_thumbnail", fake_fetch,
    )
    summary = await bf.run_backfill(
        db, cfg, api_key="K", force=False, dry_run=False,
    )
    assert summary["fetched"] == 0
    assert summary["skipped"] >= 1


@pytest.mark.asyncio
async def test_backfill_force_processes_all(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1", thumb="thumbnails/v1.jpg")

    async def fake_fetch(*, query, api_key, target):
        Path(target).write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(
        "app.services.stock_images.fetch_pexels_thumbnail", fake_fetch,
    )
    summary = await bf.run_backfill(
        db, cfg, api_key="K", force=True, dry_run=False,
    )
    assert summary["fetched"] == 1


@pytest.mark.asyncio
async def test_backfill_generates_missing_query(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1", iq=None)
    await videos_repo.set_summary(db, "v1", "A long summary about bridges", "m")

    async def fake_fetch(*, query, api_key, target):
        Path(target).write_bytes(b"jpeg")
        return True

    async def fake_gen(*, summary, model_row):
        return "suspension bridge"

    monkeypatch.setattr(
        "app.services.stock_images.fetch_pexels_thumbnail", fake_fetch,
    )
    monkeypatch.setattr(
        "app.services.stock_images.generate_image_query", fake_gen,
    )
    summary = await bf.run_backfill(
        db, cfg, api_key="K", force=False, dry_run=False,
    )
    v = await videos_repo.get(db, "v1")
    assert v.image_query == "suspension bridge"
    assert summary["query_generated"] == 1


@pytest.mark.asyncio
async def test_backfill_dry_run_writes_nothing(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1")
    called = False

    async def fake_fetch(*, query, api_key, target):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(
        "app.services.stock_images.fetch_pexels_thumbnail", fake_fetch,
    )
    summary = await bf.run_backfill(
        db, cfg, api_key="K", force=False, dry_run=True,
    )
    assert called is False
    v = await videos_repo.get(db, "v1")
    assert v.thumbnail_path is None
    assert summary["checked"] >= 1
