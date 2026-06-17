from pathlib import Path

import pytest

from app.config import Config
from app.models import VideoKind
from app.repos import videos as videos_repo
from app.services import stock_images


@pytest.mark.asyncio
async def test_fetch_pexels_no_key_returns_false(tmp_path):
    ok = await stock_images.fetch_pexels_thumbnail(
        query="x", api_key="", target=tmp_path / "v.jpg",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_fetch_pexels_hit_downloads(tmp_path, monkeypatch):
    target = tmp_path / "v.jpg"

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"photos": [{"src": {"large": "https://img/large.jpg"}}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            return FakeResp()

    monkeypatch.setattr(stock_images.httpx, "AsyncClient", FakeClient)

    async def fake_download(url, tgt):
        Path(tgt).write_bytes(b"jpeg")

    monkeypatch.setattr(stock_images, "download_thumbnail", fake_download)

    ok = await stock_images.fetch_pexels_thumbnail(
        query="solar", api_key="KEY", target=target,
    )
    assert ok is True
    assert target.exists()


@pytest.mark.asyncio
async def test_fetch_pexels_no_results_returns_false(tmp_path, monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"photos": []}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            return FakeResp()

    monkeypatch.setattr(stock_images.httpx, "AsyncClient", FakeClient)
    ok = await stock_images.fetch_pexels_thumbnail(
        query="solar", api_key="KEY", target=tmp_path / "v.jpg",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_fetch_pexels_http_error_returns_false(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            raise RuntimeError("429 boom")

    monkeypatch.setattr(stock_images.httpx, "AsyncClient", FakeClient)
    ok = await stock_images.fetch_pexels_thumbnail(
        query="solar", api_key="KEY", target=tmp_path / "v.jpg",
    )
    assert ok is False


async def _seed(db, vid, *, kind=VideoKind.EMAIL, thumb=None, iq="cats"):
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title="t", description="",
        thumbnail_path=thumb, duration_seconds=None, kind=kind,
    )
    if iq is not None:
        await videos_repo.set_image_query(db, vid, iq)


@pytest.mark.asyncio
async def test_ensure_sets_thumbnail_for_email(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1")

    async def fake_fetch(*, query, api_key, target):
        Path(target).write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(stock_images, "fetch_pexels_thumbnail", fake_fetch)
    v = await videos_repo.get(db, "v1")
    changed = await stock_images.ensure_stock_thumbnail(
        db, v, config=cfg, api_key="KEY", force=False,
    )
    assert changed is True
    v = await videos_repo.get(db, "v1")
    assert v.thumbnail_path is not None


@pytest.mark.asyncio
async def test_ensure_skips_when_thumbnail_present(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1", thumb="thumbnails/v1.jpg")
    called = False

    async def fake_fetch(*, query, api_key, target):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(stock_images, "fetch_pexels_thumbnail", fake_fetch)
    v = await videos_repo.get(db, "v1")
    changed = await stock_images.ensure_stock_thumbnail(
        db, v, config=cfg, api_key="KEY", force=False,
    )
    assert changed is False
    assert called is False


@pytest.mark.asyncio
async def test_ensure_force_overwrites(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1", thumb="thumbnails/v1.jpg")

    async def fake_fetch(*, query, api_key, target):
        Path(target).write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(stock_images, "fetch_pexels_thumbnail", fake_fetch)
    v = await videos_repo.get(db, "v1")
    changed = await stock_images.ensure_stock_thumbnail(
        db, v, config=cfg, api_key="KEY", force=True,
    )
    assert changed is True


@pytest.mark.asyncio
async def test_ensure_skips_youtube(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1", kind=VideoKind.YOUTUBE)
    called = False

    async def fake_fetch(*, query, api_key, target):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(stock_images, "fetch_pexels_thumbnail", fake_fetch)
    v = await videos_repo.get(db, "v1")
    changed = await stock_images.ensure_stock_thumbnail(
        db, v, config=cfg, api_key="KEY", force=False,
    )
    assert changed is False
    assert called is False


@pytest.mark.asyncio
async def test_ensure_no_query_skips(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1", iq=None)
    called = False

    async def fake_fetch(*, query, api_key, target):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(stock_images, "fetch_pexels_thumbnail", fake_fetch)
    v = await videos_repo.get(db, "v1")
    changed = await stock_images.ensure_stock_thumbnail(
        db, v, config=cfg, api_key="KEY", force=False,
    )
    assert changed is False
    assert called is False
