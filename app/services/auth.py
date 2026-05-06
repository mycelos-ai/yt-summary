"""Authentication for the REST API and MCP server.

The API-key auth model is intentionally minimal: a single key per user,
generated in Settings, hashed before storage. The plaintext key is shown
to the user exactly once and never persisted in clear.

When no user has an api_key configured, the auth dependency returns
user_id=1 (the default user) so LAN-only setups work without setup. A
warning is logged at boot in that case (see app/main.py).
"""

import base64
import hashlib
import logging
import os

log = logging.getLogger(__name__)

API_KEY_PREFIX = "yts_"


def generate_api_key() -> tuple[str, str, str]:
    """Generate a new API key. Returns (plaintext, sha256_hex, prefix).

    Plaintext is `yts_` followed by 32 chars of urlsafe base32 (no padding).
    """
    raw = os.urandom(20)
    encoded = base64.b32encode(raw).rstrip(b"=").decode("ascii").lower()
    plaintext = f"{API_KEY_PREFIX}{encoded}"
    return plaintext, hash_api_key(plaintext), plaintext[:8]


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
