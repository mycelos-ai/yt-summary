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

import aiosqlite
from fastapi import HTTPException, Request

from app.repos import users as users_repo

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


def _extract_token(headers) -> str | None:
    """Pull the API key out of the request headers.

    Accepts both `Authorization: Bearer <token>` and `X-API-Key: <token>`.
    Headers are case-insensitive in FastAPI's Request.headers, but plain
    dicts (used in tests) are not — we check both casings.
    """
    auth = None
    if hasattr(headers, "get"):
        auth = headers.get("authorization") or headers.get("Authorization")
    if not auth:
        # Fall back to X-API-Key
        x_key = None
        if hasattr(headers, "get"):
            x_key = headers.get("x-api-key") or headers.get("X-API-Key")
        if x_key:
            return x_key.strip()
        return None
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


async def authenticate(
    db: aiosqlite.Connection, request: Request
) -> int:
    """FastAPI dependency: validate API key, return user_id.

    If no api_key is configured for the default user, returns user_id=1
    (auth disabled). Otherwise, requires a matching Bearer / X-API-Key
    header.
    """
    user = await users_repo.get_default_user(db)
    if user is None:
        # Should never happen if the migration ran, but be defensive.
        raise HTTPException(status_code=500, detail="No default user")

    if user.api_key_hash is None:
        # Auth disabled — return default user.
        return user.id

    token = _extract_token(request.headers)
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"error": "API key required", "code": "INVALID_API_KEY"},
        )
    if hash_api_key(token) != user.api_key_hash:
        raise HTTPException(
            status_code=401,
            detail={"error": "Invalid API key", "code": "INVALID_API_KEY"},
        )
    return user.id
