from app.services.url_classify import classify_url, web_id_from_url


def test_classify_url_youtube_short():
    assert classify_url("https://youtu.be/dQw4w9WgXcQ") == "youtube"


def test_classify_url_youtube_watch():
    assert classify_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "youtube"


def test_classify_url_youtube_shorts():
    assert classify_url("https://youtube.com/shorts/dQw4w9WgXcQ") == "youtube"


def test_classify_url_arbitrary_web_page():
    assert classify_url("https://example.com/articles/foo") == "web"


def test_classify_url_blog_with_slug():
    assert classify_url("https://blog.example.com/2026/05/some-post") == "web"


def test_classify_url_substack():
    assert classify_url("https://stratechery.com/2026/some-post/") == "web"


def test_web_id_is_deterministic():
    a = web_id_from_url("https://example.com/foo")
    b = web_id_from_url("https://example.com/foo")
    assert a == b


def test_web_id_different_for_different_urls():
    a = web_id_from_url("https://example.com/foo")
    b = web_id_from_url("https://example.com/bar")
    assert a != b


def test_web_id_format():
    wid = web_id_from_url("https://example.com")
    assert wid.startswith("web-")
    assert len(wid) == len("web-") + 11
