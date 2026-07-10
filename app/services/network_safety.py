"""Network-target validation for user-influenced fetches.

Article URLs and metadata-provided image URLs are fetched by the server.
Before opening a connection, reject targets that resolve to loopback,
private, link-local, reserved, multicast, or otherwise non-global IPs.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit


class UnsafeUrlError(ValueError):
    """Raised when a server-side fetch would target a non-public address."""


def _resolve_host(host: str, port: int) -> set[str]:
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeUrlError(f"Could not resolve host {host!r}") from e
    return {str(row[4][0]) for row in rows}


def validate_public_http_url(url: str) -> None:
    """Validate that an HTTP(S) URL resolves exclusively to public IPs.

    ``YTS_ALLOW_PRIVATE_FETCHES=1`` is an explicit escape hatch for trusted
    installations that intentionally import pages from their own LAN.
    """
    if os.environ.get("YTS_ALLOW_PRIVATE_FETCHES") == "1":
        return

    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeUrlError("Only absolute http(s) URLs can be fetched")

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeUrlError("Refusing to fetch a local or private network address")

    try:
        literal = ipaddress.ip_address(host)
        addresses = {str(literal)}
    except ValueError:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = _resolve_host(host, port)

    if not addresses:
        raise UnsafeUrlError(f"Could not resolve host {host!r}")
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeUrlError("Refusing to fetch a local or private network address")
