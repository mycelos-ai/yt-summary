from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


# --- parse_curl: URL + cookies + headers out of a pasted curl command ---


def test_parse_curl_extracts_url_cookies_and_headers():
    """A full 'Copy as cURL' yields the target URL, the cookie dict, and
    the remaining request headers (cookie header excluded — it lives in
    .cookies, not .headers)."""
    from app.services.curl_parser import parse_curl
    text = (FIXTURES / "curl_youtube.txt").read_text()
    parsed = parse_curl(text)
    assert parsed.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert parsed.cookies == {
        "VISITOR_INFO1_LIVE": "abc",
        "YSC": "def",
        "LOGIN_INFO": "xyz",
    }
    assert parsed.headers["accept"] == "text/html"
    assert parsed.headers["user-agent"] == "Mozilla/5.0"
    assert "cookie" not in {k.lower() for k in parsed.headers}


def test_parse_curl_url_without_quotes():
    """curl variants emit the URL bare (no surrounding quotes)."""
    from app.services.curl_parser import parse_curl
    parsed = parse_curl("curl https://example.com/a -H 'accept: text/html'")
    assert parsed.url == "https://example.com/a"


def test_parse_curl_double_quoted_headers():
    """Windows 'Copy as cURL' uses double quotes throughout."""
    from app.services.curl_parser import parse_curl
    text = 'curl "https://example.com/x" -H "Cookie: a=1" -H "Referer: https://example.com/"'
    parsed = parse_curl(text)
    assert parsed.url == "https://example.com/x"
    assert parsed.cookies == {"a": "1"}
    assert parsed.headers["referer"] == "https://example.com/"


def test_parse_curl_b_flag_cookies():
    """curl's -b/--cookie flag is an alternative to a cookie header."""
    from app.services.curl_parser import parse_curl
    parsed = parse_curl("curl 'https://example.com/y' -b 'a=1; b=2'")
    assert parsed.cookies == {"a": "1", "b": "2"}


def test_parse_curl_returns_none_url_when_absent():
    """No recognisable URL -> url is None, so callers can reject it."""
    from app.services.curl_parser import parse_curl
    parsed = parse_curl("curl -H 'accept: text/html'")
    assert parsed.url is None


def test_looks_like_curl_detects_command():
    from app.services.curl_parser import looks_like_curl
    assert looks_like_curl("curl 'https://x' -H 'a: b'")
    assert looks_like_curl("  curl https://x")
    assert not looks_like_curl("https://example.com/article")
    assert not looks_like_curl("not a curl at all")


def test_parse_curl_extracts_cookies():
    from app.services.curl_parser import extract_cookies
    text = (FIXTURES / "curl_youtube.txt").read_text()
    cookies = extract_cookies(text)
    assert cookies == {
        "VISITOR_INFO1_LIVE": "abc",
        "YSC": "def",
        "LOGIN_INFO": "xyz",
    }


def test_parse_curl_handles_missing_cookie_header():
    from app.services.curl_parser import extract_cookies
    assert extract_cookies("curl 'https://x' -H 'accept: text/html'") == {}


def test_parse_curl_with_capital_cookie_header():
    from app.services.curl_parser import extract_cookies
    text = "curl 'https://x' -H 'Cookie: a=1; b=2'"
    assert extract_cookies(text) == {"a": "1", "b": "2"}


def test_parse_netscape_cookie_file():
    """User can paste a Netscape HTTP Cookie File directly — the parser
    recognises the format from the header line and extracts cookies from
    the tab-separated rows."""
    from app.services.curl_parser import extract_cookies
    text = (FIXTURES / "netscape_cookies.txt").read_text()
    cookies = extract_cookies(text)
    assert cookies == {
        "VISITOR_INFO1_LIVE": "abc",
        "YSC": "def",
        "LOGIN_INFO": "xyz",
    }


def test_parse_netscape_without_header_line():
    """Some exporters omit the '# Netscape HTTP Cookie File' header.
    Detect the format from the tab-separated 7-field row shape instead."""
    from app.services.curl_parser import extract_cookies
    text = (
        ".youtube.com\tTRUE\t/\tTRUE\t1789999999\tSID\ttoken1\n"
        ".youtube.com\tTRUE\t/\tFALSE\t1789999999\tHSID\ttoken2\n"
    )
    assert extract_cookies(text) == {"SID": "token1", "HSID": "token2"}


def test_parse_netscape_skips_comments_and_blank_lines():
    from app.services.curl_parser import extract_cookies
    text = (
        "# Netscape HTTP Cookie File\n"
        "\n"
        "# a comment\n"
        ".youtube.com\tTRUE\t/\tTRUE\t1789999999\tSID\ttoken1\n"
        "\n"
    )
    assert extract_cookies(text) == {"SID": "token1"}


def test_parse_netscape_handles_httponly_prefix():
    """Some exporters emit a '#HttpOnly_' prefix on httponly cookie rows.
    Strip it so the cookie name comes out clean."""
    from app.services.curl_parser import extract_cookies
    text = (
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1789999999\tSID\ttoken1\n"
    )
    assert extract_cookies(text) == {"SID": "token1"}


def test_write_netscape_cookie_file(tmp_path):
    from app.services.curl_parser import write_netscape_cookies
    target = tmp_path / "cookies.txt"
    write_netscape_cookies(
        {"a": "1", "b": "2"},
        domain=".youtube.com",
        target=target,
    )
    content = target.read_text()
    assert content.startswith("# Netscape HTTP Cookie File")
    assert "\t.youtube.com\t" in content
    rows = [line for line in content.splitlines() if not line.startswith("#") and line.strip()]
    assert len(rows) == 2
