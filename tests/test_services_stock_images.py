from pathlib import Path
from unittest.mock import AsyncMock

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


@pytest.mark.asyncio
async def test_generate_image_query_from_summary(monkeypatch):
    class Row:
        model = "openai/gpt-4o"
        api_key = "k"
        base_url = ""

    class Msg:
        content = "  wind turbines field  "

    class Choice:
        message = Msg()

    class Resp:
        choices = [Choice()]

    monkeypatch.setattr(
        stock_images.litellm, "acompletion", AsyncMock(return_value=Resp()),
    )
    q = await stock_images.generate_image_query(
        summary="An article about renewable energy.", model_row=Row(),
    )
    assert q == "wind turbines field"


@pytest.mark.asyncio
async def test_generate_image_query_no_summary(monkeypatch):
    class Row:
        model = "m"
        api_key = "k"
        base_url = ""

    called = False

    async def boom(**k):
        nonlocal called
        called = True

    monkeypatch.setattr(stock_images.litellm, "acompletion", boom)
    q = await stock_images.generate_image_query(summary="", model_row=Row())
    assert q is None
    assert called is False


@pytest.mark.asyncio
async def test_generate_image_query_no_model(monkeypatch):
    q = await stock_images.generate_image_query(
        summary="something", model_row=None,
    )
    assert q is None


@pytest.mark.asyncio
async def test_generate_image_query_llm_error_returns_none(monkeypatch):
    class Row:
        model = "m"
        api_key = "k"
        base_url = ""

    monkeypatch.setattr(
        stock_images.litellm, "acompletion",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    q = await stock_images.generate_image_query(
        summary="x", model_row=Row(),
    )
    assert q is None


# ── test_pexels_key (API key probe for the Settings test button) ──

@pytest.mark.asyncio
async def test_pexels_key_empty_returns_false_without_call():
    ok, msg = await stock_images.test_pexels_key("")
    assert ok is False
    assert "No key" in msg


@pytest.mark.asyncio
async def test_pexels_key_200_returns_true(monkeypatch):
    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None, params=None): return FakeResp()

    monkeypatch.setattr(stock_images.httpx, "AsyncClient", FakeClient)
    ok, msg = await stock_images.test_pexels_key("KEY")
    assert ok is True


@pytest.mark.asyncio
async def test_pexels_key_403_returns_false(monkeypatch):
    class FakeResp:
        status_code = 403

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None, params=None): return FakeResp()

    monkeypatch.setattr(stock_images.httpx, "AsyncClient", FakeClient)
    ok, msg = await stock_images.test_pexels_key("BADKEY")
    assert ok is False
    assert "BADKEY" not in msg          # key never leaks into the message


@pytest.mark.asyncio
async def test_pexels_key_network_error_returns_false(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None, params=None):
            raise httpx.ConnectError("boom")

    import httpx
    monkeypatch.setattr(stock_images.httpx, "AsyncClient", FakeClient)
    ok, msg = await stock_images.test_pexels_key("KEY")
    assert ok is False
