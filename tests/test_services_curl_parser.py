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
