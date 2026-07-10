from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from app.services import network_safety

SAMPLE_HTML = """
<!doctype html>
<html><head>
  <title>Sample Article</title>
  <meta name="description" content="A sample article for testing.">
  <meta property="og:image" content="https://example.com/cover.jpg">
</head><body>
  <article>
    <h1>Sample Article</h1>
    <p>This is the first paragraph of the body. It contains substantive
    text the reader should keep.</p>
    <p>Second paragraph with more content for trafilatura to find. The
    content has to be long enough to be considered an article.</p>
  </article>
  <footer>Footer noise we don't want.</footer>
</body></html>
"""


@pytest.fixture(autouse=True)
def _public_test_dns(monkeypatch):
    monkeypatch.setattr(
        network_safety, "_resolve_host", lambda host, port: {"93.184.216.34"},
    )


def _fake_response(*, status_code: int = 200, html: str = "", url: str | None = None):
    """Build a MagicMock-shaped object that quacks like httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = html
    resp.headers = {"content-type": "text/html; charset=utf-8"}
    resp.url = url or "https://example.com/post"
    return resp


def _patch_httpx_get(response):
    """Patch httpx.Client so its .get(url) returns the supplied response."""
    client_cm = MagicMock()
    client_cm.__enter__ = MagicMock(return_value=client_cm)
    client_cm.__exit__ = MagicMock(return_value=False)
    client_cm.get = MagicMock(return_value=response)
    return patch("app.services.reader.httpx.Client", return_value=client_cm)


async def test_fetch_article_extracts_body_and_metadata():
    from app.services.reader import fetch_article

    resp = _fake_response(html=SAMPLE_HTML, url="https://example.com/post")
    with _patch_httpx_get(resp):
        article = await fetch_article("https://example.com/post")

    assert article.title == "Sample Article"
    assert "first paragraph" in article.body
    assert "Footer noise" not in article.body
    assert article.thumbnail_url == "https://example.com/cover.jpg"
    assert article.description == "A sample article for testing."


async def test_fetch_article_raises_on_403():
    from app.services.reader import fetch_article

    resp = _fake_response(status_code=403, html="")
    with _patch_httpx_get(resp), pytest.raises(ValueError, match="403"):
        await fetch_article("https://example.com/blocked")


async def test_fetch_article_raises_on_404():
    from app.services.reader import fetch_article

    resp = _fake_response(status_code=404, html="")
    with _patch_httpx_get(resp), pytest.raises(ValueError, match="404"):
        await fetch_article("https://example.com/dead")


async def test_fetch_article_raises_on_429():
    from app.services.reader import fetch_article

    resp = _fake_response(status_code=429, html="")
    with _patch_httpx_get(resp), pytest.raises(ValueError, match="rate-limit"):
        await fetch_article("https://example.com/limited")


async def test_fetch_article_raises_on_network_error():
    from app.services.reader import fetch_article

    client_cm = MagicMock()
    client_cm.__enter__ = MagicMock(return_value=client_cm)
    client_cm.__exit__ = MagicMock(return_value=False)
    client_cm.get = MagicMock(side_effect=httpx.ConnectError("nope"))

    with (
        patch("app.services.reader.httpx.Client", return_value=client_cm),
        pytest.raises(ValueError, match="reach the page"),
    ):
        await fetch_article("https://no-such.invalid/post")


async def test_fetch_article_rejects_non_html_content_type():
    from app.services.reader import fetch_article

    resp = _fake_response(html="%PDF-1.4")
    resp.headers = {"content-type": "application/pdf"}
    with _patch_httpx_get(resp), pytest.raises(ValueError, match="HTML"):
        await fetch_article("https://example.com/file.pdf")


async def test_fetch_article_raises_when_extraction_empty():
    from app.services.reader import fetch_article

    blank = "<html><head></head><body></body></html>"
    resp = _fake_response(html=blank)
    with _patch_httpx_get(resp), pytest.raises(ValueError, match="couldn't pull"):
        await fetch_article("https://example.com/empty")


async def test_fetch_article_handles_no_og_image():
    from app.services.reader import fetch_article

    html = SAMPLE_HTML.replace(
        '<meta property="og:image" content="https://example.com/cover.jpg">',
        ""
    )
    resp = _fake_response(html=html)
    with _patch_httpx_get(resp):
        article = await fetch_article("https://example.com/post")

    assert article.thumbnail_url is None


async def test_fetch_article_uses_canonical_url_after_redirect():
    from app.services.reader import fetch_article

    resp = _fake_response(
        html=SAMPLE_HTML, url="https://example.com/canonical-after-redirect"
    )
    with _patch_httpx_get(resp):
        article = await fetch_article("https://example.com/short")

    assert article.url == "https://example.com/canonical-after-redirect"


async def test_fetch_article_rejects_redirect_to_private_address():
    from app.services.network_safety import UnsafeUrlError
    from app.services.reader import fetch_article

    with respx.mock:
        respx.get("https://example.com/short").mock(
            return_value=httpx.Response(
                302, headers={"location": "http://127.0.0.1/internal"},
            )
        )
        with pytest.raises(UnsafeUrlError, match="local or private"):
            await fetch_article("https://example.com/short")


# --- curl-supplied cookies + headers (paywall-behind-a-subscription) ---


def _capturing_client(response):
    """An httpx.Client mock that records the kwargs it was constructed
    with and the url passed to .get(), so tests can assert on what the
    reader actually sent."""
    captured = {}
    client_cm = MagicMock()
    client_cm.__enter__ = MagicMock(return_value=client_cm)
    client_cm.__exit__ = MagicMock(return_value=False)
    client_cm.get = MagicMock(return_value=response)

    def factory(**kwargs):
        captured["client_kwargs"] = kwargs
        return client_cm

    return factory, captured


async def test_fetch_article_sends_cookies_and_headers():
    """Cookies and headers parsed from a curl command are passed into the
    httpx client so a subscribed paywall page comes back."""
    from app.services.reader import fetch_article

    resp = _fake_response(html=SAMPLE_HTML, url="https://heise.de/article")
    factory, captured = _capturing_client(resp)
    with patch("app.services.reader.httpx.Client", side_effect=factory):
        await fetch_article(
            "https://heise.de/article",
            cookies={"sso": "tok"},
            headers={"referer": "https://heise.de/"},
        )

    kwargs = captured["client_kwargs"]
    assert kwargs["cookies"] == {"sso": "tok"}
    # curl headers are merged on top of the browser defaults.
    assert kwargs["headers"]["referer"] == "https://heise.de/"


async def test_fetch_article_retries_cookies_only_on_5xx():
    """First attempt (cookies + headers) hits a 5xx; the reader retries
    with cookies only and succeeds."""
    from app.services.reader import fetch_article

    bad = _fake_response(status_code=500, html="")
    good = _fake_response(html=SAMPLE_HTML, url="https://heise.de/article")

    calls = []

    client_cm = MagicMock()
    client_cm.__enter__ = MagicMock(return_value=client_cm)
    client_cm.__exit__ = MagicMock(return_value=False)
    client_cm.get = MagicMock(return_value=bad)

    def factory(**kwargs):
        calls.append(kwargs)
        # Second construction (cookies-only retry) returns the good resp.
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=cm)
        cm.__exit__ = MagicMock(return_value=False)
        cm.get = MagicMock(return_value=good if len(calls) >= 2 else bad)
        return cm

    with patch("app.services.reader.httpx.Client", side_effect=factory):
        article = await fetch_article(
            "https://heise.de/article",
            cookies={"sso": "tok"},
            headers={"referer": "https://heise.de/"},
        )

    assert "first paragraph" in article.body
    assert len(calls) == 2
    # First attempt carried the curl headers; the retry dropped them but
    # kept the cookies.
    assert "referer" in calls[0]["headers"]
    assert "referer" not in calls[1]["headers"]
    assert calls[1]["cookies"] == {"sso": "tok"}


async def test_fetch_article_no_retry_when_no_headers():
    """With no extra curl headers there's nothing to strip, so a 5xx is
    surfaced directly rather than retried."""
    from app.services.reader import fetch_article

    resp = _fake_response(status_code=503, html="")
    with _patch_httpx_get(resp), pytest.raises(ValueError, match="HTTP 503"):
        await fetch_article("https://example.com/down", cookies={"a": "1"})
