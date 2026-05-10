from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


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
