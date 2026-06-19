import httpx
import pytest

from app.services.playlist_index import PlaylistApiError, _playlist_id_from_url
from app.services.playlist_index import fetch_via_api
from app.services import playlist_index


def _page(items, next_token=None):
    """Build a fake playlistItems.list response page."""
    page = {"items": items}
    if next_token:
        page["nextPageToken"] = next_token
    return page


def _item(vid, title, position, *, thumb="https://t/d.jpg", with_video_id=True):
    snippet = {
        "title": title,
        "description": "",
        "position": position,
        "thumbnails": {"high": {"url": thumb, "width": 480}},
    }
    content = {"videoId": vid} if with_video_id else {}
    return {"snippet": snippet, "contentDetails": content}


def _install_fake_http(monkeypatch, pages, *, playlist_title="My PL"):
    """Make playlist_index.httpx.AsyncClient return queued pages, then the
    playlists.list title response."""
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            if url.endswith("/playlists"):
                return FakeResp({"items": [{"snippet": {
                    "title": playlist_title, "description": "",
                    "thumbnails": {},
                }}]})
            # playlistItems pages, in order
            payload = pages[calls["n"]]
            calls["n"] += 1
            return FakeResp(payload)

    monkeypatch.setattr(playlist_index.httpx, "AsyncClient", FakeClient)


async def test_fetch_via_api_paginates_and_orders(monkeypatch):
    pages = [
        _page([_item("v1", "One", 0), _item("v2", "Two", 1)], next_token="T2"),
        _page([_item("v3", "Three", 2)]),
    ]
    _install_fake_http(monkeypatch, pages)
    meta = await fetch_via_api(
        "https://youtube.com/playlist?list=PLx", api_key="KEY",
    )
    assert [e.id for e in meta.entries] == ["v1", "v2", "v3"]
    assert [e.position for e in meta.entries] == [1, 2, 3]   # 0-based +1
    assert meta.entries[0].title == "One"
    assert meta.entries[0].thumbnail_url == "https://t/d.jpg"
    assert meta.entries[0].duration_seconds is None
    assert meta.title == "My PL"


async def test_fetch_via_api_skips_items_without_video_id(monkeypatch):
    pages = [_page([
        _item("v1", "One", 0),
        _item("x", "Deleted", 1, with_video_id=False),
        _item("v2", "Two", 2),
    ])]
    _install_fake_http(monkeypatch, pages)
    meta = await fetch_via_api(
        "https://youtube.com/playlist?list=PLx", api_key="KEY",
    )
    assert [e.id for e in meta.entries] == ["v1", "v2"]


async def test_fetch_via_api_raises_on_http_error(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            raise httpx.HTTPStatusError("403", request=None, response=None)

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None): return FakeResp()

    monkeypatch.setattr(playlist_index.httpx, "AsyncClient", FakeClient)
    with pytest.raises(PlaylistApiError):
        await fetch_via_api(
            "https://youtube.com/playlist?list=PLx", api_key="KEY",
        )


async def test_fetch_via_api_http_error_does_not_leak_api_key(monkeypatch):
    """I2: When an HTTP error occurs, the raised PlaylistApiError must NOT
    contain the api_key string, even if the underlying httpx error message
    includes the key (e.g. via the request URL)."""
    secret = "SECRETKEY"

    # Build a minimal fake response with status_code so the redacted path
    # can report "HTTP 403" instead of "HTTP ?".
    fake_response = httpx.Response(403)

    class FakeResp:
        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                f"403 Client Error for url with key={secret}",
                request=None,
                response=fake_response,
            )

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None): return FakeResp()

    monkeypatch.setattr(playlist_index.httpx, "AsyncClient", FakeClient)
    with pytest.raises(PlaylistApiError) as exc_info:
        await fetch_via_api(
            "https://youtube.com/playlist?list=PLx", api_key=secret,
        )
    error_msg = str(exc_info.value)
    assert secret not in error_msg, f"API key leaked into error message: {error_msg!r}"
    assert "403" in error_msg


def test_playlist_id_from_url_extracts_list_param():
    url = "https://www.youtube.com/playlist?list=PLabc123"
    assert _playlist_id_from_url(url) == "PLabc123"


def test_playlist_id_from_url_with_extra_params():
    url = "https://www.youtube.com/playlist?list=PLxyz&si=foo"
    assert _playlist_id_from_url(url) == "PLxyz"


def test_playlist_id_from_url_raises_without_list():
    with pytest.raises(PlaylistApiError):
        _playlist_id_from_url("https://www.youtube.com/watch?v=abc")
