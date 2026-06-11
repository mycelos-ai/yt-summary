"""Part D — bookmarklet /submit confirmation page.

GET /submit renders a tiny popup-sized confirmation (URL, detected kind,
active profile, a Summarize button that POSTs to the existing /videos
handler). The GET must NOT mutate state — that's why submission is a
separate POST (a GET that enqueued would be a drive-by vector).
"""

from fastapi.testclient import TestClient

from app.main import create_app


def test_submit_get_renders_confirmation_for_youtube(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get(
            "/submit", params={"url": "https://youtu.be/dQw4w9WgXcQ"},
        )
    assert resp.status_code == 200
    body = resp.text
    # The URL is echoed (escaped) and the kind detected.
    assert "youtu.be/dQw4w9WgXcQ" in body
    assert "YouTube" in body
    # A form that POSTs to the existing submit handler with the url.
    assert 'action="/videos"' in body
    assert 'method="post"' in body
    assert "dQw4w9WgXcQ" in body  # url carried in a hidden field


def test_submit_get_detects_web_article(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get(
            "/submit", params={"url": "https://example.com/an-article"},
        )
    assert resp.status_code == 200
    assert "example.com/an-article" in resp.text
    # Detected as an article, not YouTube.
    assert "Article" in resp.text


def test_submit_get_does_not_create_a_video(tmp_path, monkeypatch):
    """GET must be side-effect free — no item enqueued just by opening
    the confirmation page."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        client.get("/submit", params={"url": "https://youtu.be/abc12345678"})

        async def count():
            from app.repos import videos as videos_repo
            return len(await videos_repo.list_recent(app.state.db, limit=100))
        n = asyncio.get_event_loop().run_until_complete(count())
    assert n == 0


def test_submit_get_rejects_non_http_url(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/submit", params={"url": "javascript:alert(1)"})
    # Renders an inline error, not a Summarize form.
    assert resp.status_code == 200
    assert 'action="/videos"' not in resp.text
    assert "http" in resp.text.lower()  # explains it needs an http(s) URL


def test_submit_get_escapes_url_to_prevent_injection(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    payload = "https://example.com/?x=</script><script>window.__pwned=1"
    with TestClient(app) as client:
        resp = client.get("/submit", params={"url": payload})
    assert resp.status_code == 200
    assert "</script><script>window.__pwned" not in resp.text


def test_settings_renders_bookmarklet_with_request_origin(tmp_path, monkeypatch):
    """The settings bookmarklet bakes the app origin in at render time
    and targets the /submit confirmation route via window.open."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app, base_url="https://yts.example.com") as client:
        resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Browser bookmarklet" in resp.text
    # Origin baked in (from the request), targets /submit, uses
    # encodeURIComponent over location.href.
    assert "https://yts.example.com/submit?url=" in resp.text
    assert "encodeURIComponent(location.href)" in resp.text
