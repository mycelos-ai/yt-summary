import re
import time
from pathlib import Path

_COOKIE_HEADER_RE = re.compile(
    r"-H\s+['\"](?:cookie|Cookie):\s*(?P<value>[^'\"]+)['\"]"
)

# Browser exporters sometimes prefix httponly rows with '#HttpOnly_'.
_HTTPONLY_PREFIX = "#HttpOnly_"


def _extract_cookies_from_curl(curl_text: str) -> dict[str, str]:
    """Pull cookies out of the `-H 'cookie: ...'` header in a curl command."""
    match = _COOKIE_HEADER_RE.search(curl_text)
    if not match:
        return {}
    pairs = match.group("value").split(";")
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        out[name.strip()] = value.strip()
    return out


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


def write_netscape_cookies(
    cookies: dict[str, str], *, domain: str, target: Path
) -> None:
    expiry = int(time.time()) + 60 * 60 * 24 * 365  # 1 year
    lines = ["# Netscape HTTP Cookie File", ""]
    for name, value in cookies.items():
        lines.append("\t" + "\t".join([domain, "TRUE", "/", "FALSE", str(expiry), name, value]))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n")
