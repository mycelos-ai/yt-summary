from pathlib import Path

import pytest

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
