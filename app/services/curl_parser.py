import re
import time
from pathlib import Path

_COOKIE_HEADER_RE = re.compile(
    r"-H\s+['\"](?:cookie|Cookie):\s*(?P<value>[^'\"]+)['\"]"
)


def extract_cookies(curl_text: str) -> dict[str, str]:
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


def write_netscape_cookies(
    cookies: dict[str, str], *, domain: str, target: Path
) -> None:
    expiry = int(time.time()) + 60 * 60 * 24 * 365  # 1 year
    lines = ["# Netscape HTTP Cookie File", ""]
    for name, value in cookies.items():
        lines.append("\t" + "\t".join([domain, "TRUE", "/", "FALSE", str(expiry), name, value]))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n")
