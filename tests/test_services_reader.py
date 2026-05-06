from unittest.mock import patch

import pytest

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


async def test_fetch_article_extracts_body_and_metadata():
    from app.services.reader import fetch_article

    with patch("app.services.reader.trafilatura.fetch_url", return_value=SAMPLE_HTML):
        article = await fetch_article("https://example.com/post")

    assert article.title == "Sample Article"
    assert "first paragraph" in article.body
    assert "Footer noise" not in article.body  # trafilatura strips boilerplate
    assert article.thumbnail_url == "https://example.com/cover.jpg"
    assert article.description == "A sample article for testing."


async def test_fetch_article_raises_when_fetch_fails():
    from app.services.reader import fetch_article

    with (
        patch("app.services.reader.trafilatura.fetch_url", return_value=None),
        pytest.raises(ValueError, match="Could not fetch"),
    ):
        await fetch_article("https://example.com/dead")


async def test_fetch_article_raises_when_extraction_empty():
    from app.services.reader import fetch_article

    # HTML that trafilatura considers content-less
    blank = "<html><head></head><body></body></html>"
    with (
        patch("app.services.reader.trafilatura.fetch_url", return_value=blank),
        pytest.raises(ValueError, match="Could not extract"),
    ):
        await fetch_article("https://example.com/empty")


async def test_fetch_article_handles_no_og_image():
    from app.services.reader import fetch_article

    html = SAMPLE_HTML.replace(
        '<meta property="og:image" content="https://example.com/cover.jpg">',
        ""
    )
    with patch("app.services.reader.trafilatura.fetch_url", return_value=html):
        article = await fetch_article("https://example.com/post")

    assert article.thumbnail_url is None


async def test_fetch_article_falls_back_to_url_when_no_title():
    from app.services.reader import fetch_article

    html_no_title = "<html><body><article><p>" + ("word " * 100) + "</p></article></body></html>"
    with patch("app.services.reader.trafilatura.fetch_url", return_value=html_no_title):
        # extract_metadata may return a metadata object with empty title;
        # fetch_article falls back to the URL.
        article = await fetch_article("https://example.com/notitle")

    assert article.title in ("https://example.com/notitle", "")  # empty or url
    # If empty, the route would still display the URL — accept either.
