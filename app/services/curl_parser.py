import re
import time
from dataclasses import dataclass, field
from pathlib import Path

_COOKIE_HEADER_RE = re.compile(
    r"-H\s+['\"](?:cookie|Cookie):\s*(?P<value>[^'\"]+)['\"]"
)

# `curl -b '...'` / `--cookie '...'` is an alternative to a cookie header.
_COOKIE_FLAG_RE = re.compile(
    r"(?:-b|--cookie)\s+['\"](?P<value>[^'\"]+)['\"]"
)

# Any `-H '<name>: <value>'` / `--header "..."` pair.
_HEADER_RE = re.compile(
    r"(?:-H|--header)\s+['\"](?P<name>[^:'\"]+):\s*(?P<value>[^'\"]*)['\"]"
)

# The request URL: the first http(s) token, quoted or bare.
_URL_RE = re.compile(
    r"""(?:^|\s)['\"]?(?P<url>https?://[^\s'"]+)['\"]?"""
)

# Browser exporters sometimes prefix httponly rows with '#HttpOnly_'.
_HTTPONLY_PREFIX = "#HttpOnly_"


def _parse_cookie_string(value: str) -> dict[str, str]:
    """Turn a `name=value; name2=value2` cookie string into a dict."""
    out: dict[str, str] = {}
    for pair in value.split(";"):
        if "=" not in pair:
            continue
        name, _, val = pair.partition("=")
        out[name.strip()] = val.strip()
    return out


def _extract_cookies_from_curl(curl_text: str) -> dict[str, str]:
    """Pull cookies out of a curl command's `-H 'cookie: ...'` header or
    its `-b '...'` / `--cookie '...'` flag (header wins if both present)."""
    match = _COOKIE_HEADER_RE.search(curl_text) or _COOKIE_FLAG_RE.search(curl_text)
    if not match:
        return {}
    return _parse_cookie_string(match.group("value"))


def _extract_cookies_from_netscape(text: str) -> dict[str, str]:
    """Parse a Netscape HTTP Cookie File: tab-separated rows of
    `domain  flag  path  secure  expiry  name  value`."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        # Honour the '#HttpOnly_' prefix instead of skipping it as a comment.
        if line.startswith(_HTTPONLY_PREFIX):
            line = line[len(_HTTPONLY_PREFIX):]
        elif line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        name = fields[5].strip()
        value = fields[6].strip()
        if not name:
            continue
        out[name] = value
    return out


def _looks_like_netscape(text: str) -> bool:
    """True if the input looks like a Netscape cookie file rather than a
    curl command. Either the canonical header line is present, or at
    least one line is a tab-separated 7-field row."""
    if "# Netscape HTTP Cookie File" in text:
        return True
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line.startswith(_HTTPONLY_PREFIX):
            line = line[len(_HTTPONLY_PREFIX):]
        elif line.lstrip().startswith("#") or not line.strip():
            continue
        if len(line.split("\t")) >= 7:
            return True
    return False


def extract_cookies(text: str) -> dict[str, str]:
    """Extract cookies from either a pasted curl command (DevTools "Copy
    as cURL") or a Netscape HTTP Cookie File. Returns an empty dict if
    neither format is recognised."""
    if _looks_like_netscape(text):
        return _extract_cookies_from_netscape(text)
    return _extract_cookies_from_curl(text)


@dataclass(frozen=True)
class ParsedCurl:
    """The parts of a pasted curl command the reader can act on."""

    url: str | None
    cookies: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


def looks_like_curl(text: str) -> bool:
    """True if the input is a curl command rather than a plain URL.

    Deliberately strict: the string must *start* with `curl` (after
    optional whitespace), so a URL that merely contains the substring
    'curl' isn't misclassified."""
    return text.lstrip().lower().startswith("curl ") or text.strip().lower() == "curl"


def parse_curl(text: str) -> ParsedCurl:
    """Parse a 'Copy as cURL' command into its URL, cookies, and the
    remaining request headers.

    The cookie header is split out into `.cookies` and removed from
    `.headers`, so callers can decide independently whether to send
    cookies, headers, or both. Returns url=None when no http(s) URL is
    found, letting the caller surface a friendly error."""
    url_match = _URL_RE.search(text)
    url = url_match.group("url") if url_match else None

    cookies = _extract_cookies_from_curl(text)

    # Lower-case header names so callers can look them up predictably;
    # HTTP header names are case-insensitive and httpx normalises anyway.
    headers: dict[str, str] = {}
    for m in _HEADER_RE.finditer(text):
        name = m.group("name").strip().lower()
        if name == "cookie":
            continue
        headers[name] = m.group("value").strip()

    return ParsedCurl(url=url, cookies=cookies, headers=headers)


def write_netscape_cookies(
    cookies: dict[str, str], *, domain: str, target: Path
) -> None:
    expiry = int(time.time()) + 60 * 60 * 24 * 365  # 1 year
    lines = ["# Netscape HTTP Cookie File", ""]
    for name, value in cookies.items():
        lines.append("\t" + "\t".join([domain, "TRUE", "/", "FALSE", str(expiry), name, value]))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n")
